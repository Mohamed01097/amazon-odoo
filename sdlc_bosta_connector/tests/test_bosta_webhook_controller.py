"""HTTP-level tests for the public Bosta status webhook."""

import json

from odoo.tests.common import HttpCase, tagged

from odoo.addons.sdlc_bosta_connector.models import bosta_state


@tagged('post_install', '-at_install')
class TestBostaWebhookController(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env['bosta.instance'].create({
            'name': 'Bosta Webhook Test',
            'api_key': 'webhook-test-key',
            'webhook_secret': 'correct-secret',
            'webhook_auth_header_name': 'Authorization',
        })

    def _post(self, body, headers=None):
        all_headers = {'Content-Type': 'application/json'}
        all_headers.update(headers or {})
        return self.url_open(
            '/bosta/webhook/status',
            method='POST',
            data=json.dumps(body).encode() if not isinstance(body, bytes) else body,
            headers=all_headers,
        )

    @staticmethod
    def _payload(state=bosta_state.STATE_DELIVERED, tracking='BST-WH-1',
                 delivery_id='wh-dlv-1', timestamp='1700000001'):
        return {
            '_id': delivery_id,
            'trackingNumber': tracking,
            'state': state,
            'timeStamp': timestamp,
            'businessReference': 'WH/OUT/0001',
        }

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def test_correct_secret_queues_event(self):
        response = self._post(
            self._payload(), {'Authorization': 'correct-secret'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'queued')

        job = self.env['bosta.tracking.event.job'].search([
            ('bosta_delivery_id', '=', 'wh-dlv-1'),
        ])
        self.assertEqual(len(job), 1)
        self.assertEqual(job.instance_id, self.instance)
        self.assertEqual(job.bosta_state_code, bosta_state.STATE_DELIVERED)
        # The endpoint must not apply business logic inline.
        self.assertEqual(job.state, 'draft')
        self.assertFalse(job.picking_id)

    def test_wrong_secret_is_rejected(self):
        response = self._post(self._payload(), {'Authorization': 'wrong-secret'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['result'], 'unauthorized')
        self.assertEqual(self.env['bosta.tracking.event.job'].search_count([]), 0)

    def test_missing_header_is_rejected(self):
        response = self._post(self._payload())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.env['bosta.tracking.event.job'].search_count([]), 0)

    def test_account_without_secret_is_never_matched(self):
        """An unconfigured account must not make the endpoint anonymous."""
        self.instance.sudo().write({'webhook_secret': False})
        response = self._post(self._payload(), {'Authorization': ''})
        self.assertEqual(response.status_code, 401)

    def test_inactive_account_is_not_matched(self):
        self.instance.sudo().write({'active': False})
        response = self._post(self._payload(), {'Authorization': 'correct-secret'})
        self.assertEqual(response.status_code, 401)

    def test_custom_header_name_is_honoured(self):
        self.instance.sudo().write({'webhook_auth_header_name': 'X-Bosta-Token'})
        rejected = self._post(self._payload(), {'Authorization': 'correct-secret'})
        self.assertEqual(rejected.status_code, 401)

        accepted = self._post(
            self._payload(timestamp='1700000002'),
            {'X-Bosta-Token': 'correct-secret'},
        )
        self.assertEqual(accepted.status_code, 200)

    # ------------------------------------------------------------------
    # Payload handling
    # ------------------------------------------------------------------

    def test_non_object_body_is_rejected(self):
        response = self._post(b'[1,2,3]', {'Authorization': 'correct-secret'})
        self.assertEqual(response.status_code, 400)

    def test_garbage_body_is_rejected(self):
        response = self._post(b'not json at all', {'Authorization': 'correct-secret'})
        self.assertIn(response.status_code, (400, 401, 500))
        self.assertEqual(self.env['bosta.tracking.event.job'].search_count([]), 0)

    def test_redelivery_does_not_queue_twice(self):
        payload = self._payload()
        first = self._post(payload, {'Authorization': 'correct-secret'})
        second = self._post(payload, {'Authorization': 'correct-secret'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            self.env['bosta.tracking.event.job'].search_count([
                ('bosta_delivery_id', '=', 'wh-dlv-1'),
            ]), 1,
        )

    def test_get_method_is_not_allowed(self):
        response = self.url_open('/bosta/webhook/status')
        self.assertIn(response.status_code, (404, 405))

    def test_secret_is_not_stored_in_the_log(self):
        self._post(self._payload(), {'Authorization': 'correct-secret'})
        logs = self.env['bosta.sync.log'].search([('operation', '=', 'webhook_received')])
        self.assertTrue(logs)
        self.assertNotIn('correct-secret', logs.request_data or '')
