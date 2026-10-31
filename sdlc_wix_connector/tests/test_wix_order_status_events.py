"""Tests for Wix `updated` and `canceled` order events."""

import json

from odoo.tests.common import TransactionCase, tagged

from .test_wix_jwt import generate_keypair


def _envelope(slug, inner, event_id):
    """Wrap an inner payload in Wix's nested JWT claim shape."""
    return {
        'data': json.dumps({
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': slug,
            'id': event_id,
            'data': json.dumps(inner),
        }),
    }


def created_claims(order_id='evt-order-1', sku='WIX-EVT-1', event_id='evt-created-1'):
    entity = {
        'id': order_id,
        'number': 2001,
        'status': 'APPROVED',
        'paymentStatus': 'NOT_PAID',
        'fulfillmentStatus': 'NOT_FULFILLED',
        'buyerInfo': {'email': 'evt.buyer@example.com'},
        'recipientInfo': {'contactDetails': {
            'firstName': 'Evt', 'lastName': 'Buyer', 'phone': '+201222222222',
        }},
        'lineItems': [{
            'quantity': 1,
            'physicalProperties': {'sku': sku},
            'price': {'amount': '80.00'},
        }],
    }
    return _envelope('created', {
        'entityId': order_id, 'createdEvent': {'entity': entity},
    }, event_id)


def updated_claims(order_id='evt-order-1', event_id='evt-updated-1',
                   payment_status='PAID', status='APPROVED',
                   fulfillment_status='FULFILLED'):
    """`updated` nests the order under updatedEvent.currentEntity."""
    entity = {
        'id': order_id,
        'number': 2001,
        'status': status,
        'paymentStatus': payment_status,
        'fulfillmentStatus': fulfillment_status,
    }
    return _envelope('updated', {
        'entityId': order_id,
        'updatedEvent': {
            'currentEntity': entity,
            'modifiedFields': {'paymentStatus': 'NOT_PAID'},
        },
    }, event_id)


def canceled_claims(order_id='evt-order-1', event_id='evt-canceled-1'):
    """`canceled` nests the order under actionEvent.body.order."""
    entity = {
        'id': order_id,
        'number': 2001,
        'status': 'CANCELED',
        'paymentStatus': 'NOT_PAID',
        'fulfillmentStatus': 'NOT_FULFILLED',
    }
    return _envelope('canceled', {
        'entityId': order_id,
        'actionEvent': {'body': {
            'order': entity,
            'restockAllItems': False,
            'customMessage': 'Ordered by mistake',
        }},
    }, event_id)


@tagged('post_install', '-at_install')
class TestWixOrderStatusEvents(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, public_pem = generate_keypair()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['wix.instance'].create({
            'name': 'Wix Events Store',
            'webhook_public_key': public_pem,
            'order_import_enabled': True,
            'order_status_sync_enabled': True,
            'default_warehouse_id': cls.warehouse.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Wix Event Product',
            'default_code': 'WIX-EVT-1',
            'is_storable': True,
            'list_price': 80.0,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _queue(self, claims, slug, order_id='evt-order-1', event_id=None):
        return self.env['wix.order.import.job'].create({
            'instance_id': self.instance.id,
            'payload_json': json.dumps(claims),
            'wix_order_id': order_id,
            'wix_event_id': event_id or f'{slug}-{order_id}',
            'event_slug': slug,
        })

    def _run(self):
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

    def _imported_order(self):
        self._queue(created_claims(), 'created', event_id='evt-created-1')
        self._run()
        return self.env['sale.order'].search([('wix_order_id', '=', 'evt-order-1')])

    # ------------------------------------------------------------------
    # Envelope extraction
    # ------------------------------------------------------------------

    def test_each_event_shape_is_extracted(self):
        """All three documented envelopes must yield the order object."""
        Job = self.env['wix.order.import.job']
        cases = (
            ('created', created_claims(), 'APPROVED'),
            ('updated', updated_claims(), 'APPROVED'),
            ('canceled', canceled_claims(), 'CANCELED'),
        )
        for slug, claims, expected_status in cases:
            job = Job.new({
                'instance_id': self.instance.id,
                'payload_json': json.dumps(claims),
                'event_slug': slug,
            })
            data = job._extract_order()
            self.assertEqual(data['wix_order_id'], 'evt-order-1', slug)
            self.assertEqual(data['status'], expected_status, slug)

    # ------------------------------------------------------------------
    # created still works, now with traceability
    # ------------------------------------------------------------------

    def test_created_links_order_to_instance(self):
        order = self._imported_order()
        self.assertTrue(order)
        self.assertEqual(order.wix_instance_id, self.instance)
        self.assertEqual(order.wix_order_id, 'evt-order-1')
        self.assertEqual(order.wix_payment_status, 'NOT_PAID')
        self.assertEqual(order.state, 'sale')

    # ------------------------------------------------------------------
    # updated
    # ------------------------------------------------------------------

    def test_updated_refreshes_status_fields(self):
        order = self._imported_order()
        job = self._queue(updated_claims(), 'updated', event_id='evt-updated-1')
        self._run()

        self.assertEqual(job.state, 'done')
        self.assertEqual(job.sale_order_id, order)
        self.assertEqual(order.wix_payment_status, 'PAID')
        self.assertEqual(order.wix_fulfillment_status, 'FULFILLED')
        self.assertTrue(order.wix_last_event_at)

    def test_updated_does_not_touch_accounting(self):
        """A Wix payment status is evidence, never a posting instruction."""
        order = self._imported_order()
        before = self.env['account.move'].search_count([])
        self._queue(updated_claims(), 'updated', event_id='evt-updated-2')
        self._run()
        self.assertEqual(self.env['account.move'].search_count([]), before)
        self.assertFalse(order.invoice_ids)

    def test_update_before_import_is_deferred_not_lost(self):
        job = self._queue(
            updated_claims(order_id='evt-unknown'), 'updated',
            order_id='evt-unknown', event_id='evt-updated-orphan',
        )
        self._run()
        self.assertEqual(job.state, 'draft')
        self.assertTrue(job.next_run_at)
        self.assertTrue(job.payload_json)

    def test_status_sync_disabled_defers(self):
        self._imported_order()
        self.instance.sudo().write({'order_status_sync_enabled': False})
        job = self._queue(updated_claims(), 'updated', event_id='evt-updated-off')
        self._run()
        self.assertEqual(job.state, 'draft')
        self.assertTrue(job.next_run_at)

    # ------------------------------------------------------------------
    # canceled
    # ------------------------------------------------------------------

    def test_cancel_flags_for_review_by_default(self):
        """Default policy must never change the order, only flag it."""
        order = self._imported_order()
        self.assertEqual(self.instance.order_cancel_policy, 'flag_only')

        job = self._queue(canceled_claims(), 'canceled', event_id='evt-cancel-1')
        self._run()

        self.assertEqual(job.state, 'done')
        self.assertTrue(order.wix_review_needed)
        self.assertEqual(order.wix_order_status, 'CANCELED')
        # Still a live Odoo order: cancelling it is a human decision here.
        self.assertEqual(order.state, 'sale')

    def test_cancel_when_safe_cancels_the_order(self):
        self.instance.sudo().write({'order_cancel_policy': 'cancel_when_safe'})
        order = self._imported_order()

        self._queue(canceled_claims(), 'canceled', event_id='evt-cancel-2')
        self._run()

        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.wix_review_needed)

    def test_cancel_is_blocked_by_a_validated_delivery(self):
        """Stock that physically left must not be cancelled behind the scenes."""
        self.instance.sudo().write({'order_cancel_policy': 'cancel_when_safe'})
        order = self._imported_order()

        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.picking_type_id.sudo().write({'create_backorder': 'never'})
        picking.button_validate()
        self.assertEqual(picking.state, 'done')

        self._queue(canceled_claims(), 'canceled', event_id='evt-cancel-3')
        self._run()

        self.assertEqual(order.state, 'sale')
        self.assertTrue(order.wix_review_needed)

    def test_cancel_is_blocked_by_a_posted_invoice(self):
        self.instance.sudo().write({'order_cancel_policy': 'cancel_when_safe'})
        order = self._imported_order()
        order.order_line.write({'qty_delivered': 1})
        invoice = order._create_invoices()
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')

        self._queue(canceled_claims(), 'canceled', event_id='evt-cancel-4')
        self._run()

        self.assertEqual(order.state, 'sale')
        self.assertTrue(order.wix_review_needed)

    def test_updated_with_canceled_status_is_treated_as_a_cancellation(self):
        order = self._imported_order()
        claims = updated_claims(status='CANCELED', event_id='evt-updated-cancel')
        self._queue(claims, 'updated', event_id='evt-updated-cancel')
        self._run()
        self.assertTrue(order.wix_review_needed)

    # ------------------------------------------------------------------
    # Idempotency across event types
    # ------------------------------------------------------------------

    def test_several_events_for_one_order_are_all_allowed(self):
        """The old order-level constraint would have blocked these.

        The final status reflects the last event processed, which here is the
        cancellation reporting an unpaid order.
        """
        order = self._imported_order()
        self._queue(updated_claims(), 'updated', event_id='evt-u1')
        self._queue(canceled_claims(), 'canceled', event_id='evt-c1')
        self._run()

        jobs = self.env['wix.order.import.job'].search([
            ('wix_order_id', '=', 'evt-order-1'),
        ])
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job.state == 'done' for job in jobs))
        self.assertEqual(
            self.env['sale.order'].search_count([('wix_order_id', '=', 'evt-order-1')]),
            1,
        )
        self.assertEqual(order.wix_order_status, 'CANCELED')
        self.assertEqual(order.wix_payment_status, 'NOT_PAID')
        self.assertTrue(order.wix_review_needed)

    def test_an_update_applied_alone_reaches_paid(self):
        """Isolating the update proves it is applied, not overwritten here."""
        order = self._imported_order()
        self._queue(updated_claims(), 'updated', event_id='evt-u-only')
        self._run()
        self.assertEqual(order.wix_payment_status, 'PAID')

    def test_same_event_id_cannot_be_queued_twice(self):
        self._queue(updated_claims(), 'updated', event_id='evt-dup')
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self._queue(updated_claims(), 'updated', event_id='evt-dup')

    def test_redelivered_created_event_reuses_the_order(self):
        order = self._imported_order()
        self._queue(created_claims(), 'created', event_id='evt-created-again')
        self._run()
        self.assertEqual(
            self.env['sale.order'].search_count([('wix_order_id', '=', 'evt-order-1')]),
            1,
        )
        jobs = self.env['wix.order.import.job'].search([
            ('event_slug', '=', 'created'),
        ])
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(job.sale_order_id == order for job in jobs))
