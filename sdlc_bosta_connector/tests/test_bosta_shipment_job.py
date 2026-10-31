"""Shipment-booking tests. Every Bosta call is mocked; nothing hits the API."""

import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.sdlc_bosta_connector.models.bosta_api import BostaAPI, BostaError

CREATE_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.create_delivery'


@tagged('post_install', '-at_install')
class TestBostaShipmentJob(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['bosta.instance'].create({
            'name': 'Bosta Booking Test',
            'api_key': 'test-key-not-real',
            'api_base_url': 'https://app.bosta.co',
            'shipment_booking_enabled': True,
            'warehouse_id': cls.warehouse.id,
        })
        cls.delivery_product = cls.env['product.product'].create({
            'name': 'Bosta Shipping Fee',
            'type': 'service',
            'invoice_policy': 'order',
        })
        cls.carrier = cls.env['delivery.carrier'].create({
            'name': 'Bosta Standard',
            'delivery_type': 'bosta',
            'bosta_instance_id': cls.instance.id,
            'bosta_flat_rate': 60.0,
            'integration_level': 'rate',
            'product_id': cls.delivery_product.id,
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mona Customer',
            'street': '12 Nile St',
            'city': 'Cairo',
            'phone': '01000000001',
            'email': 'mona@example.com',
        })
        # Bosta will not accept a free-text city, so the address must resolve
        # against a cached catalogue entry.
        cls.bosta_city = cls.env['bosta.city'].create({
            'name': 'Cairo', 'code': 'EG-01', 'bosta_id': 'c1',
            'instance_id': cls.instance.id,
        })
        cls.partner.bosta_city_id = cls.bosta_city.id
        cls.product = cls.env['product.product'].create({
            'name': 'Bosta Shippable Item',
            'is_storable': True,
            'list_price': 150.0,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _confirmed_order(self, carrier=None):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'carrier_id': (carrier or self.carrier).id if carrier is not False else False,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 2,
            })],
        })
        order.action_confirm()
        return order

    def _validate_picking(self, picking):
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.picking_type_id.sudo().write({'create_backorder': 'never'})
        picking.with_context(skip_backorder=True).button_validate()
        return picking

    @staticmethod
    def _ok_response(tracking='BST-1001', delivery_id='abc123'):
        return {'_id': delivery_id, 'trackingNumber': tracking}

    # ------------------------------------------------------------------
    # Enqueue behaviour
    # ------------------------------------------------------------------

    def test_validating_bosta_delivery_enqueues_one_job(self):
        order = self._confirmed_order()
        picking = order.picking_ids
        self._validate_picking(picking)

        jobs = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs.state, 'draft')
        self.assertEqual(picking.bosta_shipment_state, 'queued')
        # The API must not be touched during validation.
        self.assertFalse(jobs.tracking_number)

    def test_validation_does_not_call_the_api(self):
        order = self._confirmed_order()
        with patch(CREATE_PATH) as mocked:
            self._validate_picking(order.picking_ids)
        mocked.assert_not_called()

    def test_non_bosta_carrier_enqueues_nothing(self):
        fixed = self.env['delivery.carrier'].create({
            'name': 'Fixed Carrier',
            'delivery_type': 'fixed',
            'fixed_price': 20.0,
            'product_id': self.delivery_product.id,
        })
        order = self._confirmed_order(carrier=fixed)
        picking = self._validate_picking(order.picking_ids)
        self.assertFalse(
            self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        )

    def test_no_carrier_enqueues_nothing(self):
        order = self._confirmed_order(carrier=False)
        picking = self._validate_picking(order.picking_ids)
        self.assertFalse(
            self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        )

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------

    def test_cron_books_shipment_and_stores_identifiers(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        with patch(CREATE_PATH, return_value=self._ok_response()) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_called_once()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.tracking_number, 'BST-1001')
        self.assertEqual(job.bosta_delivery_id, 'abc123')
        # Mirrored onto the picking, including core's tracking field.
        self.assertEqual(picking.carrier_tracking_ref, 'BST-1001')
        self.assertEqual(picking.bosta_delivery_id, 'abc123')
        self.assertEqual(picking.bosta_shipment_state, 'booked')

    def test_payload_uses_documented_fields(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])

        payload = job._build_payload()
        self.assertEqual(payload['type'], 10)
        self.assertEqual(payload['businessReference'], picking.name)
        self.assertEqual(payload['receiver']['phone'], '01000000001')
        self.assertEqual(payload['dropOffAddress']['firstLine'], '12 Nile St')
        self.assertIn('packageDetails', payload['specs'])
        self.assertGreaterEqual(payload['specs']['packageDetails']['itemsCount'], 1)

    def test_webhook_is_registered_per_order_when_configured(self):
        self.instance.sudo().write({
            'webhook_url': 'https://example.test/bosta/webhook/status',
            'webhook_secret': 'shhh',
            'webhook_auth_header_name': 'Authorization',
        })
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])

        payload = job._build_payload()
        self.assertEqual(payload['webhookUrl'], 'https://example.test/bosta/webhook/status')
        self.assertEqual(payload['webhookCustomHeaders'], {'Authorization': 'shhh'})

    def test_booking_disabled_defers_without_calling_api(self):
        self.instance.sudo().write({'shipment_booking_enabled': False})
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        with patch(CREATE_PATH) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_not_called()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'draft')
        self.assertTrue(job.next_run_at)
        self.assertFalse(picking.carrier_tracking_ref)

    def test_already_booked_job_is_not_rebooked(self):
        """Guards against double-booking after a crash mid-call."""
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        job.sudo().write({'tracking_number': 'BST-EXISTING'})

        with patch(CREATE_PATH) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_not_called()
        self.assertEqual(job.state, 'done')
        self.assertEqual(picking.carrier_tracking_ref, 'BST-EXISTING')

    def test_one_job_per_picking_enforced(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['bosta.shipment.job'].create({
                    'picking_id': picking.id,
                    'instance_id': self.instance.id,
                })

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def test_retryable_error_backs_off_and_stays_queued(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        error = BostaError("rate limited", status_code=429, retryable=True)
        with patch(CREATE_PATH, side_effect=error):
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'draft')
        self.assertEqual(job.retry_count, 1)
        self.assertTrue(job.next_run_at)
        self.assertFalse(picking.carrier_tracking_ref)

    def test_non_retryable_error_fails_immediately(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        error = BostaError("bad address", status_code=400, retryable=False)
        with patch(CREATE_PATH, side_effect=error):
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'failed')
        self.assertEqual(picking.bosta_shipment_state, 'failed')
        self.assertFalse(job.next_run_at)

    def test_retries_are_bounded(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])

        error = BostaError("still down", status_code=503, retryable=True)
        with patch(CREATE_PATH, side_effect=error):
            for _attempt in range(6):
                job.sudo().write({'next_run_at': False})
                self.env['bosta.shipment.job'].cron_process_shipment_jobs()

        self.assertEqual(job.state, 'failed')
        self.assertFalse(job.next_run_at)

    def test_missing_phone_fails_with_clear_message(self):
        self.partner.sudo().write({'phone': False})
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        with patch(CREATE_PATH) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_not_called()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'failed')
        self.assertIn('phone', (job.last_error_message or '').lower())

    def test_response_without_tracking_number_is_an_error(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)

        with patch(
            'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI._request',
            return_value={'data': {'_id': 'x'}},
        ):
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        self.assertEqual(job.state, 'failed')
        self.assertFalse(picking.carrier_tracking_ref)

    def test_action_retry_requeues(self):
        order = self._confirmed_order()
        picking = self._validate_picking(order.picking_ids)
        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        job.sudo().write({'state': 'failed', 'retry_count': 5})

        picking.action_retry_bosta_shipment()
        self.assertEqual(job.state, 'draft')
        self.assertEqual(job.retry_count, 0)
        self.assertEqual(picking.bosta_shipment_state, 'queued')

    # ------------------------------------------------------------------
    # Log safety
    # ------------------------------------------------------------------

    def test_credentials_are_never_stored_in_logs(self):
        order = self._confirmed_order()
        self.instance.sudo().write({
            'webhook_url': 'https://example.test/hook',
            'webhook_secret': 'top-secret-value',
        })
        picking = self._validate_picking(order.picking_ids)

        with patch(CREATE_PATH, return_value=self._ok_response()):
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        stored = (job.request_payload_json or '') + (job.sync_log_id.request_data or '')
        self.assertNotIn('top-secret-value', stored)
        self.assertNotIn('test-key-not-real', stored)

    def test_sanitize_redacts_known_secret_keys(self):
        cleaned = BostaAPI.sanitize({
            'Authorization': 'raw-key',
            'nested': {'webhookCustomHeaders': {'Authorization': 'x'}, 'keep': 'ok'},
        })
        self.assertEqual(cleaned['Authorization'], '***redacted***')
        self.assertEqual(cleaned['nested']['webhookCustomHeaders'], '***redacted***')
        self.assertEqual(cleaned['nested']['keep'], 'ok')
