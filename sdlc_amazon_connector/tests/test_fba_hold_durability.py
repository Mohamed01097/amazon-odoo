from datetime import datetime
from unittest.mock import MagicMock

from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'
HOLD = 'HELD_FOR_CONTROLLED_IMPORT'


@tagged('post_install', '-at_install')
class TestFbaHoldDurability(TransactionCase):
    """Controlled-import FBA hold must survive normal sync/import/upsert paths."""

    def setUp(self):
        super().setUp()
        company = self.env.company
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        base_sale = self.env['account.tax'].search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (hold)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True})
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Hold Durability Instance', 'company_id': company.id,
            'marketplace_id': EG, 'region': 'eu',
            'fba_warehouse_id': warehouse.id, 'fbm_warehouse_id': warehouse.id,
            'amazon_sales_tax_id': self.vat14.id})
        self.product = self.env['product.product'].sudo().create({
            'name': 'Hold Towel', 'type': 'consu', 'is_storable': True})
        self.env['amazon.product'].sudo().create({
            'name': 'Hold Towel', 'instance_id': self.instance.id,
            'sku': 'HOLD-SKU', 'odoo_product_id': self.product.id})
        self.SE = self.env['amazon.fba.sale.stock.event']

    def _raw(self, oid, qty=1, shipped=None, unit='100.0'):
        shipped = qty if shipped is None else shipped
        return {
            'orderId': oid, 'createdTime': '2026-10-01T12:00:00Z',
            'lastUpdatedTime': '2026-10-02T00:00:00Z',
            'salesChannel': {'marketplaceName': 'Amazon.eg'},
            'fulfillment': {'fulfilledBy': 'AMAZON', 'fulfillmentStatus': 'SHIPPED'},
            'orderItems': [{
                'orderItemId': 'IT-' + oid, 'quantityOrdered': qty,
                'product': {'sellerSku': 'HOLD-SKU', 'asin': 'B0', 'title': 'Hold Towel',
                            'price': {'unitPrice': {'amount': unit, 'currencyCode': 'EGP'}}},
                'fulfillment': {'quantityFulfilled': shipped}}]}

    def _job(self):
        return self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 10, 1), 'date_to': datetime(2026, 10, 3)})

    def _controlled_import(self, raw):
        """Import exactly like the controlled wizard (hold ON)."""
        od = AmazonAPI._normalize_order_2026(raw)
        self._job().with_context(amazon_hold_fba_stock=True)._import_one_order(
            MagicMock(spec=AmazonAPI), 'tok', od)
        return self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', raw['orderId'])], limit=1)

    def _normal_reimport(self, raw):
        """Re-run the ordinary import pipeline WITHOUT the hold context."""
        od = AmazonAPI._normalize_order_2026(raw)
        self._job()._import_one_order(MagicMock(spec=AmazonAPI), 'tok', od)

    def _status_sync_upsert(self, order_rec, raw):
        """Replicate amazon_order_status_sync_job: _upsert_order_items, no hold ctx."""
        od = AmazonAPI._normalize_order_2026(raw)
        self._job()._upsert_order_items(order_rec, od['OrderItems'])

    def _event(self, oid):
        return self.SE.search([('instance_id', '=', self.instance.id),
                               ('amazon_order_ref', '=', oid)])

    # 1
    def test_01_controlled_import_creates_held_event(self):
        self._controlled_import(self._raw('H-1'))
        ev = self._event('H-1')
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertTrue(ev._is_controlled_import_hold())

    # 2
    def test_02_reimport_does_not_release(self):
        self._controlled_import(self._raw('H-2'))
        self._normal_reimport(self._raw('H-2'))
        ev = self._event('H-2')
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertFalse(ev.picking_ids)

    # 3
    def test_03_status_sync_upsert_does_not_release(self):
        o = self._controlled_import(self._raw('H-3'))
        self._status_sync_upsert(o, self._raw('H-3'))
        ev = self._event('H-3')
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertEqual(ev.processed_fulfilled_qty, 0)

    # 4
    def test_04_repeated_upserts_idempotent(self):
        o = self._controlled_import(self._raw('H-4'))
        for _ in range(3):
            self._status_sync_upsert(o, self._raw('H-4'))
        self.assertEqual(len(self._event('H-4')), 1)

    # 5
    def test_05_held_event_has_no_picking(self):
        self._controlled_import(self._raw('H-5'))
        self.assertFalse(self._event('H-5').picking_ids)

    # 6
    def test_06_processor_skips_held_event(self):
        self._controlled_import(self._raw('H-6'))
        ev = self._event('H-6')
        result = ev._process_one()
        self.assertFalse(result)
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertFalse(ev.picking_ids)

    # 7
    def test_07_processed_qty_unchanged_while_held(self):
        o = self._controlled_import(self._raw('H-7'))
        self._status_sync_upsert(o, self._raw('H-7'))
        self.assertEqual(self._event('H-7').processed_fulfilled_qty, 0)

    # 8
    def test_08_cumulative_increase_while_held(self):
        # ordered 3, initially 2 shipped, later 3 shipped — hold must remain.
        o = self._controlled_import(self._raw('H-8', qty=3, shipped=2))
        ev = self._event('H-8')
        self.assertEqual(ev.amazon_cumulative_fulfilled_qty, 2)
        self._status_sync_upsert(o, self._raw('H-8', qty=3, shipped=3))
        ev.invalidate_recordset()
        self.assertEqual(ev.amazon_cumulative_fulfilled_qty, 3)   # safe info update
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertFalse(ev.picking_ids)

    # 9
    def test_09_retry_releases_hold(self):
        self._controlled_import(self._raw('H-9'))
        ev = self._event('H-9')
        self.assertTrue(ev._is_controlled_import_hold())
        ev.action_retry()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'pending')
        self.assertFalse(ev.last_error_code)
        self.assertFalse(ev._is_controlled_import_hold())   # release boundary crossed

    # 10
    def test_10_non_held_pending_not_blocked_by_guard(self):
        # A normal (non-held) pending AFN event is not a controlled-import hold,
        # so the durable-hold guard never blocks it.
        self._controlled_import(self._raw('H-10'))
        ev = self._event('H-10')
        ev.write({'state': 'pending', 'last_error_code': False})
        ev.invalidate_recordset()
        self.assertFalse(ev._is_controlled_import_hold())

    # 11
    def test_11_done_event_idempotent_under_resync(self):
        o = self._controlled_import(self._raw('H-11'))
        ev = self._event('H-11')
        # Simulate an already-processed (done) event, then a normal re-sync.
        ev.write({'state': 'done', 'last_error_code': False, 'processed_fulfilled_qty': 1})
        self._status_sync_upsert(o, self._raw('H-11'))
        ev.invalidate_recordset()
        self.assertEqual(len(self._event('H-11')), 1)
        self.assertFalse(ev.picking_ids)

    # 13 (no Amazon call needed for hold behavior) — all above use MagicMock API.
    def test_13_hold_requires_no_amazon_call(self):
        api = MagicMock(spec=AmazonAPI)
        od = AmazonAPI._normalize_order_2026(self._raw('H-13'))
        self._job().with_context(amazon_hold_fba_stock=True)._import_one_order(api, 'tok', od)
        # The embedded-items path must not call per-item Amazon fetches.
        api.get_order_items.assert_not_called()
        self.assertEqual(self._event('H-13').last_error_code, HOLD)
