# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections

import yaml
from oslo_log import log as logging
from pycadf import cadftaxonomy as taxonomy
from pycadf import cadftype
from pycadf import credential
from pycadf import eventfactory
from pycadf import host
from pycadf import reason
from pycadf import resource

ResourceSpec = collections.namedtuple('ResourceSpec',
                                      ['type_uri', 'el_type_uri', 'singleton',
                                       'custom_actions',
                                       'children'])


class ConfigError(Exception):
    """Error raised when pyCADF fails to configure correctly."""

    pass


class ClientResource(resource.Resource):
    def __init__(self, project_id=None, **kwargs):
        super(ClientResource, self).__init__(**kwargs)
        if project_id is not None:
            self.project_id = project_id


class KeystoneCredential(credential.Credential):
    def __init__(self, identity_status=None, **kwargs):
        super(KeystoneCredential, self).__init__(**kwargs)
        if identity_status is not None:
            self.identity_status = identity_status


def _put_hier(dct, hier_name, object):
    """
    Puts an object with an hierarchical name (a/b/c/...) into a dictionary.
    The hierarchy implied by the name is mapped to the dictionary hierarchy.
    :param dct: target dict
    :param hier_name: hierarchical name h1/h2/.../hn/myname
    :param object: the object to be placed at the leaf
    """

    pos = hier_name.find('/')
    if pos >= 0:
        segment, rest = hier_name[0:pos], hier_name[pos + 1:]
        if segment not in dct:
            dct[segment] = {}
        _put_hier(dct[segment], rest, object)
    else:
        dct[hier_name] = object


class OpenStackAuditMiddleware(object):
    def __init__(self, cfg_file, log=logging.getLogger(__name__)):
        """Configure to recognize and map known api paths."""
        self._log = log

        try:
            conf = yaml.safe_load(open(cfg_file, 'r'))

            self._service_type = conf['service_type']
            self._service_name = conf['service_name']
            self._prefix_template = conf['prefix']
            # default_target_endpoint_type = conf.get('target_endpoint_type')
            # self._service_endpoints = conf.get('service_endpoints', {})
            self._resource_specs = self._parse_resources(conf['resources'])

        except KeyError as err:
            raise ConfigError('Missing config property in %s: %s', cfg_file,
                              str(err))
        except (OSError, yaml.YAMLError) as err:
            raise ConfigError('Error opening config file %s: %s',
                              cfg_file, err)

    def _parse_resources(self, res_dict, parentTypeURI=None):
        result = {}

        for name, s in res_dict.iteritems():
            if not s:
                spec = {}
            else:
                spec = s

            if parentTypeURI:
                pfx = parentTypeURI
            else:
                pfx = self._service_type

            singleton = spec.get('singleton', False)
            type_uri = spec.get('type_uri', pfx + "/" + name)
            el_type_uri = type_uri + '/' + name[:-1] if not singleton else None

            spec = ResourceSpec(type_uri, el_type_uri,
                                singleton,
                                spec.get('custom_actions', {}),
                                self._parse_resources(spec.get('children', {}),
                                                      type_uri))
            _put_hier(result, name, spec)

        return result

    def _build_event(self, res_node, res_id, res_parent_id, request, response,
                     path, cursor=0):
        """ Parse a resource item

        :param res_tree:
        :param path:
        :param cursor:
        :return: the event
        """

        # Check if the end of path is reached and event can be created finally
        if cursor == -1:
            # end of path reached, create the event
            event = self._create_event(res_node, res_id or res_parent_id,
                                       request, response, None)
            if request.method == 'POST' and response and response.json:
                payload = response.json
                name = payload.get('name')
                if name is None:
                    name = payload.get('displayName')
                event.target = resource.Resource(payload.get('id'),
                                                 res_node.type_uri, name)

            return event

        # Find next path segment (skip leading / with +1)
        next_pos = path.find('/', cursor + 1)
        token = None
        if next_pos != -1:
            # that means there are more path segments
            token = path[cursor + 1:next_pos]
        else:
            token = path[cursor + 1:]

        # handle the current token
        if isinstance(res_node, dict):
            # the node contains a dict => handle token as resource name
            res_node = res_node.get(token)
            if res_node is None:
                # no such name, ignore/filter the resource
                self._log.warning(
                    "Incomplete resource path after segment %s: %s", token,
                    request.path)
                return None

            return self._build_event(res_node, None, None, request, response,
                                     path, next_pos)
        elif isinstance(res_node, ResourceSpec):
            # if the ID is set or it is a singleton
            # next up is an action or child
            if res_id or res_node.singleton:
                child_res = res_node.children.get(token)
                if child_res:
                    # the ID is still the one of the parent
                    return self._build_event(child_res, None,
                                             res_id or res_parent_id, request,
                                             response, path, next_pos)
                elif next_pos == -1:
                    # this must be an action
                    return self._create_event(res_node,
                                              res_id or res_parent_id,
                                              request, response, token)
            else:
                # next up should be an ID
                return self._build_event(res_node, token, res_parent_id,
                                         request, response, path, next_pos)

        self._log.warning(
            "Unexpected continuation of resource path after segment %s: %s",
            token, request.path)
        return None

    def _get_action(self, res_spec, res_id, request, action_suffix):
        """Given a resource spec, a request and a path suffix, deduct
        the correct CADF action.

        Depending on req.method:

        if POST:

        - path ends with 'action', read the body and use as action;
        - path ends with known custom_action, take action from config;
        - request ends with known (child-)resource type, assume is create
        action
        - request ends with unknown path, assume is update action.

        if GET:

        - request ends with known path, assume is list action;
        - request ends with unknown path, assume is read action.

        if PUT, assume update action.
        if DELETE, assume delete action.
        if HEAD, assume read action.

        """
        method = request.method

        if method == 'POST':
            if action_suffix is None:
                return taxonomy.ACTION_CREATE

            return self._get_custom_action(res_spec, action_suffix, request)
        elif method == 'GET':
            if action_suffix is None:
                return taxonomy.ACTION_READ if res_id else taxonomy.ACTION_LIST

            return self._get_custom_action(res_spec, action_suffix, request)
        elif method == 'PUT' or method == 'PATCH':
            return taxonomy.ACTION_UPDATE
        elif method == 'DELETE':
            return taxonomy.ACTION_DELETE
        elif method == 'HEAD':
            return taxonomy.ACTION_READ
        else:
            return None

    def _get_custom_action(self, res_spec, action_suffix, request):
        rest_action = ''
        if action_suffix == 'action':
            try:
                payload = request.json
                if payload:
                    rest_action = next(iter(payload))
                else:
                    return None
            except ValueError:
                self._log.warning("unexpected empty action payload",
                                  request.path)
                return None
        else:
            rest_action = action_suffix

        # check for individual mapping of action
        action = res_spec.custom_actions.get(rest_action)
        if action is not None:
            return action

        # check for generic mapping
        action = res_spec.custom_actions.get('*')
        if action is not None and action is not '':
            return action.replace('*', rest_action)

        # use defaults if no custom action mapping exists
        if not res_spec.custom_actions:
            # if there are no custom_actions defined, we will just
            return taxonomy.ACTION_UPDATE + "/" + rest_action
        else:
            self._log.debug("action %s is filtered out", rest_action)
            return None

    def create_event(self, request, response=None):
        # drop the endpoint's path prefix
        path = self._strip_url_prefix(request)
        path = path[:-1] if path.endswith('/') else path
        return self._build_event(self._resource_specs, None, None, request,
                                 response, path, 0)

    def _create_event(self, res_spec, res_id, request, response,
                      action_suffix):
        action = self._get_action(res_spec, res_id, request, action_suffix)
        if not action:
            # skip if action filtered out
            return

        project_or_domain_id = request.environ.get(
            'HTTP_X_PROJECT_ID') or request.environ.get(
            'HTTP_X_DOMAIN_ID', taxonomy.UNKNOWN)
        initiator = ClientResource(
            typeURI=taxonomy.ACCOUNT_USER,
            id=request.environ.get('HTTP_X_USER_ID', taxonomy.UNKNOWN),
            name=request.environ.get('HTTP_X_USER_NAME', taxonomy.UNKNOWN),
            host=host.Host(address=request.client_addr,
                           agent=request.user_agent),
            credential=KeystoneCredential(
                token=request.environ.get('HTTP_X_AUTH_TOKEN', ''),
                identity_status=request.environ.get('HTTP_X_IDENTITY_STATUS',
                                                    taxonomy.UNKNOWN)),
            project_id=project_or_domain_id)

        action_result = None
        event_reason = None
        if response:
            if 200 <= response.status_int < 400:
                action_result = taxonomy.OUTCOME_SUCCESS
            else:
                action_result = taxonomy.OUTCOME_FAILURE

            event_reason = reason.Reason(
                reasonType='HTTP', reasonCode=str(response.status_int))
        else:
            action_result = taxonomy.UNKNOWN

        target = None
        if res_id:
            rtype = None
            if action == taxonomy.ACTION_LIST or res_spec.singleton:
                rtype = res_spec.type_uri
            else:
                rtype = res_spec.el_type_uri
            target = resource.Resource(id=res_id, typeURI=rtype)
        else:
            # use the service as resource if element has been addressed
            target = self._build_target_service_resource(request, res_spec)
        event = eventfactory.EventFactory().new_event(
            eventType=cadftype.EVENTTYPE_ACTIVITY,
            outcome=action_result,
            action=action,
            initiator=initiator,
            # TODO add observer again?
            reason=event_reason,
            target=target)
        event.requestPath = request.path_qs
        # TODO add reporter step again?
        # event.add_reporterstep(
        #    reporterstep.Reporterstep(
        #        role=cadftype.REPORTER_ROLE_MODIFIER,
        #        reporter=resource.Resource(id='observer'),
        #        reporterTime=timestamp.get_utc_now()))

        return event

    def _build_target_service_resource(self, res_spec, req):
        """Build target resource."""
        target_type_uri = 'service/' + res_spec.type_uri
        target = resource.Resource(typeURI=target_type_uri,
                                   id=self._service_name)
        return target.add_address(req.path_url)

    def _strip_url_prefix(self, request):
        """ Removes the prefix from the URL paths, e.g. '/V2/{project_id}/'
        :param req: incoming request
        :return: URL request path without the leading prefix or None if prefix
        was missing
        """
        project_or_domain_id = request.environ.get(
            'HTTP_X_PROJECT_ID') or request.environ.get(
            'HTTP_X_DOMAIN_ID', taxonomy.UNKNOWN)
        prefix = self._prefix_template.format(project_id=project_or_domain_id)
        return request.path[len(prefix):] if request.path.startswith(prefix) \
            else None
