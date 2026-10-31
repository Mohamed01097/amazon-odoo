"""HTTP-level tests for the public Wix webhook endpoint."""

import json

from odoo.tests.common import HttpCase, tagged

from .test_wix_jwt import generate_keypair, make_jwt
from .test_wix_order_import_job import build_claims


@tagged('post_install', '-at_install')
class TestWixWebhookController(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key, cls.public_pem = generate_keypair()
        cls.other_private_key, _ = generate_keypair()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['wix.instance'].create({
            'name': 'Webhook Test Store',
            'webhook_public_key': cls.public_pem,
            'order_import_enabled': True,
            'default_warehouse_id': cls.warehouse.id,
        })

    def _post(self, body):
        # `method` is forced: url_open falls back to GET when the body is
        # empty, which would exercise the wrong code path.
        return self.url_open(
            '/wix/webhook/order',
            method='POST',
            data=body if isinstance(body, bytes) else body.encode(),
            headers={'Content-Type': 'application/jwt'},
        )

    def test_valid_signature_queues_job(self):
        token = make_jwt(self.private_key, build_claims(order_id='ctrl-order-1'))
        response = self._post(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'queued')

        job = self.env['wix.order.import.job'].search([('wix_order_id', '=', 'ctrl-order-1')])
        self.assertEqual(len(job), 1)
        self.assertEqual(job.instance_id, self.instance)
        self.assertEqual(job.state, 'draft')
        # The endpoint must not do business work inline.
        self.assertFalse(job.sale_order_id)

    def test_signature_from_unknown_key_is_rejected(self):
        token = make_jwt(self.other_private_key, build_claims(order_id='ctrl-order-bad'))
        response = self._post(token)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['result'], 'invalid_signature')
        self.assertFalse(
            self.env['wix.order.import.job'].search([('wix_order_id', '=', 'ctrl-order-bad')])
        )

    def test_garbage_body_is_rejected(self):
        response = self._post('this is not a jwt at all')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.env['wix.order.import.job'].search_count([]), 0)

    def test_empty_body_is_rejected(self):
        response = self._post('')
        self.assertEqual(response.status_code, 401)

    def test_redelivery_does_not_queue_twice(self):
        token = make_jwt(self.private_key, build_claims(order_id='ctrl-order-dup'))
        first = self._post(token)
        second = self._post(token)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            self.env['wix.order.import.job'].search_count(
                [('wix_order_id', '=', 'ctrl-order-dup')]
            ),
            1,
        )

    def test_unsupported_entity_is_ignored_not_queued(self):
        claims = {'data': json.dumps({
            'entityFqdn': 'wix.bookings.v2.booking',
            'slug': 'created',
            'entityId': 'booking-1',
            'data': json.dumps({'entityId': 'booking-1'}),
        })}
        response = self._post(make_jwt(self.private_key, claims))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'ignored')
        self.assertFalse(
            self.env['wix.order.import.job'].search([('wix_order_id', '=', 'booking-1')])
        )

    def test_unsupported_slug_is_ignored_not_queued(self):
        """Subscribing to an extra event in Wix must not silently act on it."""
        claims = {'data': json.dumps({
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': 'approved',
            'entityId': 'ctrl-order-approved',
            'data': json.dumps({'entityId': 'ctrl-order-approved'}),
        })}
        response = self._post(make_jwt(self.private_key, claims))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'ignored')
        self.assertFalse(
            self.env['wix.order.import.job'].search(
                [('wix_order_id', '=', 'ctrl-order-approved')]
            )
        )

    def test_canceled_slug_is_accepted(self):
        claims = {'data': json.dumps({
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': 'canceled',
            'id': 'ctrl-evt-cancel',
            'entityId': 'ctrl-order-canceled',
            'data': json.dumps({
                'entityId': 'ctrl-order-canceled',
                'actionEvent': {'body': {'order': {
                    'id': 'ctrl-order-canceled', 'status': 'CANCELED',
                }}},
            }),
        })}
        response = self._post(make_jwt(self.private_key, claims))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'queued')
        job = self.env['wix.order.import.job'].search(
            [('wix_order_id', '=', 'ctrl-order-canceled')]
        )
        self.assertEqual(job.event_slug, 'canceled')

    def test_webhook_is_rejected_when_no_public_key_configured(self):
        self.instance.sudo().write({'webhook_public_key': False})
        token = make_jwt(self.private_key, build_claims(order_id='ctrl-order-nokey'))
        response = self._post(token)
        self.assertEqual(response.status_code, 401)

    def test_inactive_connection_is_not_used(self):
        self.instance.sudo().write({'active': False})
        token = make_jwt(self.private_key, build_claims(order_id='ctrl-order-inactive'))
        response = self._post(token)
        self.assertEqual(response.status_code, 401)

    def test_get_method_is_not_allowed(self):
        response = self.url_open('/wix/webhook/order')
        self.assertIn(response.status_code, (404, 405))
