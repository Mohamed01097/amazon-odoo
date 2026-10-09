from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'


@tagged('post_install', '-at_install', 'amazon_fbm_lifecycle')
class TestFbmLifecycle(TransactionCase):
    """FBM (merchant-fulfilled) local lifecycle: import -> draft SO -> confirm ->
    native delivery -> invoice -> status sync. FBM must NOT create FBA stock events."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        WH = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.fbm_wh = WH.create({'name': 'FBM WH', 'code': 'FBMWH', 'company_id': self.company.id})
        base_sale = self.env['account.tax'].sudo().search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', self.company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (fbm)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True})
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'FBM Egypt', 'company_id': self.company.id, 'seller_id': 'FBM-SELLER',
            'marketplace_id': EG, 'region': 'eu', 'refresh_token': 'm', 'client_id': 'm',
            'client_secret': 'm', 'fbm_warehouse_id': self.fbm_wh.id,
            'amazon_sales_tax_id': self.vat14.id})
        self.product, self.ap = self._product('FBM-SKU')
        self._put_stock(self.product, self.fbm_wh.lot_stock_id, 100)
        self.SE = self.env['amazon.fba.sale.stock.event']

    def _product(self, sku):
        p = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku, 'default_code': sku, 'type': 'consu', 'is_storable': True,
            'company_id': self.company.id})
        ap = self.env['amazon.product'].sudo().create({
            'name': 'AZ %s' % sku, 'instance_id': self.instance.id, 'sku': sku,
            'asin': 'B0%s' % sku.replace('-', ''), 'fulfillment_channel': 'MFN',
            'odoo_product_id': p.id})
        return p, ap

    def _put_stock(self, product, dest, qty):
        supplier = self.env.ref('stock.stock_location_suppliers')
        pick = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fbm_wh.in_type_id.id, 'location_id': supplier.id,
            'location_dest_id': dest.id, 'company_id': self.company.id, 'origin': 'OPENING',
            'move_ids': [Command.create({'product_id': product.id, 'product_uom_qty': qty,
                'product_uom': product.uom_id.id, 'location_id': supplier.id,
                'location_dest_id': dest.id, 'company_id': self.company.id})]})
        pick.action_confirm(); pick.action_assign()
        pick.with_context(picking_ids_not_to_backorder=pick.ids, skip_backorder=True).button_validate()
        return pick

    def _raw(self, oid, items, status='Unshipped'):
        return {'orderId': oid, 'createdTime': '2026-10-01T12:00:00Z',
                'lastUpdatedTime': '2026-10-02T00:00:00Z',
                'salesChannel': {'marketplaceName': 'Amazon.eg'},
                'orderStatus': status,
                'fulfillment': {'fulfilledBy': 'MERCHANT', 'fulfillmentStatus': status.upper()},
                'orderItems': [{'orderItemId': 'IT-%s-%s' % (oid, it['sku']),
                    'quantityOrdered': it['qty'], 'quantityShipped': it.get('shipped', 0),
                    'product': {'sellerSku': it['sku'], 'asin': 'B0', 'title': it['sku'],
                        'price': {'unitPrice': {'amount': it['unit'], 'currencyCode': 'EGP'}}},
                    'fulfillment': {'quantityFulfilled': it.get('shipped', 0)}} for it in items]}

    def _job(self):
        return self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id, 'fulfillment_channel': 'MFN',
            'date_from': datetime(2026, 10, 1), 'date_to': datetime(2026, 10, 3)})

    def _import(self, raw):
        od = AmazonAPI._normalize_order_2026(raw)
        self._job()._import_one_order(MagicMock(spec=AmazonAPI), 'tok', od)
        return self.env['amazon.sale.order'].sudo().search(
            [('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', raw['orderId'])], limit=1)

    def _deliveries(self, so):
        return self.env['stock.picking'].sudo().search([('sale_id', '=', so.id)])

    # ---------- STEP 2: full E2E ----------
    def test_01_import_creates_draft_so_fbm_wh_no_fba_event(self):
        aso = self._import(self._raw('F-1', [dict(sku='FBM-SKU', qty=3, unit='100.0')]))
        self.assertEqual(aso.fulfillment_channel, 'MFN')
        so = aso.sale_order_id
        self.assertTrue(so)
        self.assertEqual(so.state, 'draft')
        self.assertEqual(so.warehouse_id, self.fbm_wh)
        self.assertFalse(self.SE.search([('amazon_order_ref', '=', 'F-1')]),
                         "FBM order must NOT create an FBA sale stock event")
        self.assertFalse(self._deliveries(so), "no delivery before confirmation")

    def test_02_confirm_creates_native_delivery(self):
        aso = self._import(self._raw('F-2', [dict(sku='FBM-SKU', qty=2, unit='100.0')]))
        so = aso.sale_order_id
        so.action_confirm()
        deliv = self._deliveries(so)
        self.assertEqual(len(deliv), 1)
        self.assertEqual(deliv.location_id, self.fbm_wh.lot_stock_id)
        self.assertEqual(sum(deliv.move_ids.mapped('product_uom_qty')), 2)

    def test_03_delivery_validation_deducts_stock(self):
        before = self.product.with_context(location=self.fbm_wh.lot_stock_id.id).qty_available
        aso = self._import(self._raw('F-3', [dict(sku='FBM-SKU', qty=4, unit='100.0')]))
        so = aso.sale_order_id
        so.action_confirm()
        deliv = self._deliveries(so)
        deliv.action_assign()
        deliv.with_context(picking_ids_not_to_backorder=deliv.ids, skip_backorder=True).button_validate()
        self.assertEqual(deliv.state, 'done')
        self.product.invalidate_recordset()
        after = self.product.with_context(location=self.fbm_wh.lot_stock_id.id).qty_available
        self.assertEqual(before - after, 4)

    def test_04_invoice_after_confirm(self):
        aso = self._import(self._raw('F-4', [dict(sku='FBM-SKU', qty=1, unit='114.0')]))
        so = aso.sale_order_id
        so.action_confirm()
        inv = so._create_invoices()
        self.assertTrue(inv)
        self.assertEqual(inv.move_type, 'out_invoice')
        self.assertIn(so.id, inv.invoice_line_ids.sale_line_ids.order_id.ids)

    def test_05_status_sync_no_duplicate(self):
        aso = self._import(self._raw('F-5', [dict(sku='FBM-SKU', qty=1, unit='100.0')]))
        so = aso.sale_order_id
        self._job()._upsert_order_items(aso, [{
            'OrderItemId': 'IT-F-5-FBM-SKU', 'SellerSKU': 'FBM-SKU', 'ASIN': 'B0',
            'Title': 'x', 'QuantityOrdered': 1, 'QuantityShipped': 0}])
        self.assertEqual(len(self.env['amazon.sale.order'].search([('amazon_order_ref', '=', 'F-5')])), 1)
        self.assertEqual(aso.sale_order_id, so)

    # ---------- STEP 3: edge cases ----------
    def test_06_multiple_products(self):
        p2, _ = self._product('FBM-SKU2')
        self._put_stock(p2, self.fbm_wh.lot_stock_id, 50)
        aso = self._import(self._raw('F-6', [
            dict(sku='FBM-SKU', qty=2, unit='100.0'), dict(sku='FBM-SKU2', qty=3, unit='50.0')]))
        so = aso.sale_order_id
        self.assertEqual(len(so.order_line), 2)
        so.action_confirm()
        self.assertEqual(sum(self._deliveries(so).move_ids.mapped('product_uom_qty')), 5)

    def test_07_reimport_idempotent_no_dup(self):
        self._import(self._raw('F-7', [dict(sku='FBM-SKU', qty=1, unit='100.0')]))
        self._import(self._raw('F-7', [dict(sku='FBM-SKU', qty=1, unit='100.0')]))
        self.assertEqual(len(self.env['amazon.sale.order'].search([('amazon_order_ref', '=', 'F-7')])), 1)
        self.assertEqual(len(self.env['sale.order'].search([('client_order_ref', '=', 'F-7')])), 1)

    def test_08_reimport_after_delivery_keeps_delivery(self):
        aso = self._import(self._raw('F-8', [dict(sku='FBM-SKU', qty=2, unit='100.0')]))
        so = aso.sale_order_id
        so.action_confirm()
        deliv = self._deliveries(so)
        deliv.action_assign()
        deliv.with_context(picking_ids_not_to_backorder=deliv.ids, skip_backorder=True).button_validate()
        self._import(self._raw('F-8', [dict(sku='FBM-SKU', qty=2, unit='100.0', shipped=2)], status='Shipped'))
        self.assertEqual(len(self._deliveries(so)), 1)
        self.assertEqual(self._deliveries(so).state, 'done')

    def test_09_missing_mapping_skips_so(self):
        raw = self._raw('F-9', [dict(sku='UNMAPPED-FBM', qty=1, unit='100.0')])
        aso = self._import(raw)
        self.assertFalse(aso.sale_order_id, "no SO when product mapping missing")

    def test_10_insufficient_stock_delivery_not_done(self):
        p3, _ = self._product('FBM-LOW')
        # only 1 in stock, order 5
        self._put_stock(p3, self.fbm_wh.lot_stock_id, 1)
        aso = self._import(self._raw('F-10', [dict(sku='FBM-LOW', qty=5, unit='100.0')]))
        so = aso.sale_order_id
        so.action_confirm()
        deliv = self._deliveries(so)
        deliv.action_assign()
        # merchant-managed: delivery exists but cannot be fully reserved; never auto-done
        self.assertTrue(deliv)
        self.assertNotEqual(deliv.state, 'done')

    def test_11_shipment_confirm_api_failure_is_handled(self):
        aso = self._import(self._raw('F-11', [dict(sku='FBM-SKU', qty=1, unit='100.0')]))
        aso.write({'tracking_number': 'TRK1', 'carrier_name': 'DHL'})
        with patch.object(type(self.instance), '_confirm_order_shipment',
                          side_effect=Exception('Amazon feed failure (test)')):
            with self.assertRaises(Exception):
                aso.action_confirm_shipment()
        # order record is intact after the failure
        self.assertTrue(aso.exists())
        self.assertEqual(aso.sale_order_id.state, 'draft')

    def test_12_afn_guard_rejects_shipment_confirm(self):
        # FBM-only action must refuse an AFN order (FBA is Amazon-fulfilled).
        afn = self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': 'F-12A', 'instance_id': self.instance.id,
            'fulfillment_channel': 'AFN', 'amazon_status': 'Shipped', 'tracking_number': 'T'})
        with self.assertRaises(Exception):
            afn.action_confirm_shipment()
