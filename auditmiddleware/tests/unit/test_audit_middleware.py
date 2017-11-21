#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import fixtures
import mock
import webob

from auditmiddleware.tests.unit import base


class AuditMiddlewareTest(base.BaseAuditMiddlewareTest):
    def setUp(self):
        self.notifier = mock.MagicMock()

        p = 'auditmiddleware._notifier.create_notifier'
        f = fixtures.MockPatch(p, return_value=self.notifier)
        self.notifier_fixture = self.useFixture(f)

        super(AuditMiddlewareTest, self).setUp()

    def test_api_request(self):
        path = '/v2/' + self.project_id + "/servers"
        self.create_simple_app().get(path,
                                     extra_environ=self.get_environ_header())

        # Check notification with request + response
        call_args = self.notifier.notify.call_args_list[0][0]
        self.assertEqual(path, call_args[1]['requestPath'])
        self.assertEqual('success', call_args[1]['outcome'])
        self.assertIn('reason', call_args[1])
        # self.assertIn('reporterchain', call_args[1])

    def test_api_request_failure(self):

        class CustomException(Exception):
            pass

        path = '/v2/' + self.project_id + "/servers"

        def cb(req):
            raise CustomException('It happens!')

        try:
            self.create_app(cb).get(path,
                                    extra_environ=self.get_environ_header())

            self.fail('Application exception has not been re-raised')
        except CustomException:
            pass

        # Check notification with request + response
        call_args = self.notifier.notify.call_args_list[0][0]
        self.assertEqual(path, call_args[1]['requestPath'])
        self.assertEqual('unknown', call_args[1]['outcome'])
        # self.assertIn('reporterchain', call_args[1])

    def test_process_request_fail(self):
        path = '/v2/' + self.project_id + "/servers"

        req = webob.Request.blank(path,
                                  environ=self.get_environ_header('GET'))
        req.context = {}

        middleware = self.create_simple_middleware()
        middleware._process_request(req, webob.response.Response())
        self.assertTrue(self.notifier.notify.called)

    def test_ignore_req_opt(self):
        path = '/v2/' + self.project_id + "/servers"

        app = self.create_simple_app(ignore_req_list='get, PUT')

        # Check GET/PUT request does not send notification
        app.get(path, extra_environ=self.get_environ_header())
        app.put(path, extra_environ=self.get_environ_header())

        self.assertFalse(self.notifier.notify.called)

        # Check non-GET/PUT request does send notification
        app.post(path, extra_environ=self.get_environ_header())

        self.assertEqual(1, self.notifier.notify.call_count)

        call_args = self.notifier.notify.call_args_list[0][0]
        self.assertEqual(path, call_args[1]['requestPath'])

    def test_cadf_event_context_scoped(self):
        path = '/v2/' + self.project_id + "/servers"

        self.create_simple_app().get(path,
                                     extra_environ=self.get_environ_header())

        self.assertEqual(1, self.notifier.notify.call_count)

        call_args = self.notifier.notify.call_args_list[0][0]

        # the Context is the first argument. Let's verify it.
        self.assertIsInstance(call_args[0], dict)

    def test_cadf_event_scoped_to_request_on_error(self):
        path = '/v2/' + self.project_id + "/servers"

        middleware = self.create_simple_middleware()

        req = webob.Request.blank(path,
                                  environ=self.get_environ_header('GET'))
        req.context = {}
        self.notifier.notify.side_effect = Exception('error')

        middleware(req)
        self.assertTrue(self.notifier.notify.called)
        event1 = self.notifier.notify.call_args_list[0][0][1]

        req2 = webob.Request.blank(path,
                                   environ=self.get_environ_header('GET'))
        req2.context = {}
        self.notifier.reset_mock()

        middleware._process_request(req2, webob.response.Response())
        self.assertTrue(self.notifier.notify.called)
        # ensure event is not the same across requests
        self.assertNotEqual(event1['id'],
                            self.notifier.notify.call_args_list[0][0][1]['id'])

    def test_no_response(self):
        middleware = self.create_simple_middleware()
        url = 'http://admin_host:8774/v2/' + self.project_id + '/servers'
        req = webob.Request.blank(url,
                                  environ=self.get_environ_header('GET'),
                                  remote_addr='192.168.0.1')
        req.context = {}
        middleware._process_request(req)
        payload = self.notifier.notify.call_args_list[0][0][1]
        self.assertEqual(payload['outcome'], 'unknown')
        self.assertNotIn('reason', payload)
        # self.assertEqual(len(payload['reporterchain']), 1)
        # self.assertEqual(payload['reporterchain'][0]['role'], 'modifier')
        # self.assertEqual(payload['reporterchain'][0]['reporter']['id'],
        # 'target')
