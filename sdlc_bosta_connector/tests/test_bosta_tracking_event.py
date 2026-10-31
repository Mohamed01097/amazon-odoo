"""Status-callback tests: state mapping, returns, and idempotency."""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.sdlc_bosta_connector.models import bosta_state

CREATE_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.create_delivery'


@tagged('post_install', '-at_install')
class TestBostaTrackingEvent(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['bosta.instance'].create({
            'name': 'Bosta Tracking Test',
            'api_key': 'tracking-test-key',
            'shipment_booking_enabled': True,
            'webhook_secret': 'hook-secret',
            'warehouse_id': cls.warehouse.id,
        })
        cls.delivery_product = cls.env['product.product'].create({
            'name': 'Bosta Shipping Fee T',
            'type': 'service',
            'invoice_policy': 'order',
        })
        cls.carrier = cls.env['delivery.carrier'].create({
            'name': 'Bosta Tracking Carrier',
            'delivery_type': 'bosta',
            'bosta_instance_id': cls.instance.id,
            'bosta_flat_rate': 50.0,
            'integration_level': 'rate',
            'product_id': cls.delivery_product.id,
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Tracking Customer',
            'street': '5 Tahrir St',
            'city': 'Cairo',
            'phone': '01000000009',
        })
        # Bosta will not accept a free-text city, so the address must resolve
        # against a cached catalogue entry.
        cls.bosta_city = cls.env['bosta.city'].create({
            'name': 'Cairo', 'code': 'EG-01', 'bosta_id': 'c1',
            'instance_id': cls.instance.id,
        })
        cls.partner.bosta_city_id = cls.bosta_city.id
        cls.product = cls.env['product.product'].create({
            'name': 'Tracked Item',
            'is_storable': True,
            'list_price': 90.0,
        })

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    def _booked_picking(self, tracking='BST-TRACK-1', delivery_id='dlv-1'):
        """A validated delivery with a Bosta shipment already booked."""
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'carrier_id': self.carrier.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.picking_type_id.sudo().write({'create_backorder': 'never'})
        picking.button_validate()

        with patch(CREATE_PATH, return_value={
            '_id': delivery_id, 'trackingNumber': tracking,
        }):
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        return picking

    def _event(self, state, tracking='BST-TRACK-1', delivery_id='dlv-1',
               timestamp='1700000000', **extra):
        payload = {
            '_id': delivery_id,
            'trackingNumber': tracking,
            'state': state,
            'timeStamp': timestamp,
        }
        payload.update(extra)
        return payload

    def _queue_and_run(self, payload):
        job = self.env['bosta.tracking.event.job'].queue_event(self.instance, payload)
        self.env['bosta.tracking.event.job'].cron_process_tracking_event_jobs()
        return job

    # ------------------------------------------------------------------
    # State mapping
    # ------------------------------------------------------------------

    def test_delivered_state_is_applied(self):
        picking = self._booked_picking()
        job = self._queue_and_run(self._event(bosta_state.STATE_DELIVERED))

        self.assertEqual(job.state, 'done')
        self.assertEqual(job.picking_id, picking)
        self.assertEqual(picking.bosta_shipment_state, 'delivered')
        self.assertTrue(picking.bosta_delivered_at)

    def test_in_transit_states_are_applied(self):
        picking = self._booked_picking()
        for index, code in enumerate((
            bosta_state.STATE_PICKED_UP,
            bosta_state.STATE_IN_TRANSIT,
            bosta_state.STATE_RECEIVED_AT_WAREHOUSE,
        )):
            self._queue_and_run(self._event(code, timestamp=f'ts-{index}'))
            self.assertEqual(picking.bosta_shipment_state, 'in_transit')

    def test_exception_state_is_applied(self):
        picking = self._booked_picking()
        self._queue_and_run(self._event(
            bosta_state.STATE_EXCEPTION,
            exceptionReason='Customer not reachable', exceptionCode=12,
        ))
        self.assertEqual(picking.bosta_shipment_state, 'exception')

    def test_unknown_state_is_recorded_not_guessed(self):
        """An undocumented code must never be mapped onto a nearby state."""
        picking = self._booked_picking()
        job = self._queue_and_run(self._event(999))

        self.assertEqual(job.state, 'ignored')
        self.assertEqual(picking.bosta_shipment_state, 'booked')
        self.assertIn('999', job.last_error_message or '')

    def test_state_helpers_match_documented_codes(self):
        self.assertEqual(
            bosta_state.shipment_state_for(bosta_state.STATE_DELIVERED), 'delivered',
        )
        self.assertIsNone(bosta_state.shipment_state_for(999))
        self.assertIsNone(bosta_state.shipment_state_for('not-a-number'))
        self.assertTrue(bosta_state.is_terminal(bosta_state.STATE_DELIVERED))
        self.assertFalse(bosta_state.is_terminal(bosta_state.STATE_IN_TRANSIT))

    # ------------------------------------------------------------------
    # Returns
    # ------------------------------------------------------------------

    def test_returned_state_creates_unvalidated_reverse_picking(self):
        picking = self._booked_picking()
        job = self._queue_and_run(self._event(bosta_state.STATE_TERMINATED))

        return_picking = job.return_picking_id
        self.assertTrue(return_picking)
        self.assertEqual(picking.bosta_return_picking_id, return_picking)
        self.assertEqual(picking.bosta_shipment_state, 'returned')
        self.assertTrue(picking.bosta_returned_at)

        # The parcel has not physically arrived: receiving stays a human step.
        self.assertNotEqual(return_picking.state, 'done')
        self.assertIn(return_picking.state, ('confirmed', 'assigned', 'waiting'))
        self.assertEqual(return_picking.picking_type_id.code, 'incoming')

    def test_return_is_created_only_once(self):
        picking = self._booked_picking()
        self._queue_and_run(self._event(bosta_state.STATE_TERMINATED, timestamp='t1'))
        first_return = picking.bosta_return_picking_id
        self.assertTrue(first_return)

        self._queue_and_run(self._event(bosta_state.STATE_TERMINATED, timestamp='t2'))
        self.assertEqual(picking.bosta_return_picking_id, first_return)
        self.assertEqual(
            self.env['stock.picking'].search_count([
                ('id', '=', first_return.id),
            ]), 1,
        )

    def test_delivered_state_creates_no_return(self):
        picking = self._booked_picking()
        job = self._queue_and_run(self._event(bosta_state.STATE_DELIVERED))
        self.assertFalse(job.return_picking_id)
        self.assertFalse(picking.bosta_return_picking_id)

    # ------------------------------------------------------------------
    # Matching and idempotency
    # ------------------------------------------------------------------

    def test_duplicate_event_is_not_applied_twice(self):
        self._booked_picking()
        payload = self._event(bosta_state.STATE_DELIVERED)
        first = self.env['bosta.tracking.event.job'].queue_event(self.instance, payload)
        second = self.env['bosta.tracking.event.job'].queue_event(self.instance, payload)
        self.assertEqual(first, second)
        self.assertEqual(
            self.env['bosta.tracking.event.job'].search_count([
                ('bosta_delivery_id', '=', 'dlv-1'),
            ]), 1,
        )

    def test_event_matched_by_tracking_number(self):
        picking = self._booked_picking(tracking='BST-BYTN', delivery_id=False)
        picking.sudo().write({'bosta_delivery_id': False})
        job = self._queue_and_run(self._event(
            bosta_state.STATE_DELIVERED, tracking='BST-BYTN', delivery_id='',
        ))
        self.assertEqual(job.picking_id, picking)

    def test_event_matched_by_business_reference(self):
        picking = self._booked_picking(tracking='BST-REF', delivery_id='dlv-ref')
        picking.sudo().write({
            'bosta_delivery_id': False, 'carrier_tracking_ref': False,
        })
        job = self._queue_and_run(self._event(
            bosta_state.STATE_DELIVERED, tracking='', delivery_id='',
            businessReference=picking.name,
        ))
        self.assertEqual(job.picking_id, picking)

    def test_unmatched_event_is_deferred_not_lost(self):
        """A callback can arrive before the booking is recorded."""
        job = self._queue_and_run(self._event(
            bosta_state.STATE_DELIVERED, tracking='BST-NOBODY', delivery_id='ghost',
        ))
        self.assertEqual(job.state, 'draft')
        self.assertTrue(job.next_run_at)
        self.assertFalse(job.picking_id)
        self.assertTrue(job.payload_json)

    def test_unmatched_event_eventually_fails_but_keeps_payload(self):
        job = self.env['bosta.tracking.event.job'].queue_event(
            self.instance,
            self._event(bosta_state.STATE_DELIVERED, delivery_id='ghost2'),
        )
        for _attempt in range(6):
            job.sudo().write({'next_run_at': False})
            self.env['bosta.tracking.event.job'].cron_process_tracking_event_jobs()

        self.assertEqual(job.state, 'failed')
        self.assertTrue(job.payload_json)
        self.assertIn('ghost2', job.payload_json)

    def test_retry_requeues_an_ignored_event(self):
        self._booked_picking()
        job = self._queue_and_run(self._event(999))
        self.assertEqual(job.state, 'ignored')
        job.action_retry()
        self.assertEqual(job.state, 'draft')
        self.assertEqual(job.retry_count, 0)

    def test_accounting_is_untouched(self):
        """Status changes must not post anything: Odoo's own flow owns that."""
        picking = self._booked_picking()
        before = self.env['account.move'].search_count([])
        self._queue_and_run(self._event(bosta_state.STATE_DELIVERED))
        self.assertEqual(self.env['account.move'].search_count([]), before)
