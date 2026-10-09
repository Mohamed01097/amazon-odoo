from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'
HOLD = 'HELD_FOR_CONTROLLED_IMPORT'
PRE = 'PRE_CUTOVER_FBA_ORDER'
MISSING = 'MISSING_FBA_PURCHASE_DATE'
NOCFG = 'FBA_CUTOVER_NOT_CONFIGURED'

CUTOVER = '2026-10-05 12:00:00'
PRE_DT = '2026-10-01 09:00:00'
POST_DT = '2026-10-09 09:00:00'


@tagged('post_install', '-at_install', 'amazon_fba_auto_cutover')
class TestFbaAutoCutover(TransactionCase):
    """Phase 11C: explicit, auditable FBA automatic-stock cutover boundary."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        WH = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.src_wh = WH.create({'name': 'Cutover Src', 'code': 'COSRC', 'company_id': self.company.id})
        self.fba_wh = WH.create({'name': 'Cutover FBA', 'code': 'COFBA', 'company_id': self.company.id})
        locations = self._fba_locations()
        base_sale = self.env['account.tax'].sudo().search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', self.company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (cutover)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True})
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Cutover Egypt', 'company_id': self.company.id,
            'seller_id': 'CO-SELLER', 'marketplace_id': EG, 'region': 'eu',
            'refresh_token': 'mock', 'client_id': 'mock', 'client_secret': 'mock',
            'fba_warehouse_id': self.fba_wh.id,
            'fba_source_location_id': self.src_wh.lot_stock_id.id,
            'amazon_sales_tax_id': self.vat14.id,
            **locations,
        })
        self.instance.action_create_fba_stock_structure()
        self.product, self.amazon_product = self._product('CUT-SKU')
        self._put_stock(self.product, self.instance.fba_sellable_location_id, 100)
        self.SE = self.env['amazon.fba.sale.stock.event']

    # ---------- fixtures ----------
    def _location(self, name, usage, parent=False):
        return self.env['stock.location'].sudo().create({
            'name': name,
            'usage': usage,
            'company_id': self.company.id,
            'location_id': parent.id if parent else False,
        })

    def _fba_locations(self):
        stock = self.fba_wh.lot_stock_id
        return {
            'fba_transit_location_id': self._location('Cutover FBA Transit', 'transit').id,
            'fba_received_location_id': self._location('Cutover FBA Received', 'internal', stock).id,
            'fba_sellable_location_id': self._location('Cutover FBA Sellable', 'internal', stock).id,
            'fba_reserved_location_id': self._location('Cutover FBA Reserved', 'internal', stock).id,
            'fba_unsellable_location_id': self._location('Cutover FBA Unsellable', 'internal', stock).id,
            'fba_return_source_location_id': self._location('Cutover FBA Customer Returns', 'customer').id,
            'fba_sold_customer_location_id': self._location('Cutover FBA Sold', 'customer').id,
            'fba_removal_transit_location_id': self._location('Cutover FBA Removal Transit', 'transit').id,
            'fba_disposal_location_id': self._location('Cutover FBA Disposal', 'inventory').id,
        }

    def _product(self, sku):
        p = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku, 'default_code': sku, 'type': 'consu', 'is_storable': True,
            'company_id': self.company.id})
        ap = self.env['amazon.product'].sudo().create({
            'name': 'AZ %s' % sku, 'instance_id': self.instance.id, 'sku': sku,
            'asin': 'B0%s' % sku.replace('-', ''), 'fulfillment_channel': 'AFN',
            'odoo_product_id': p.id})
        return p, ap

    def _put_stock(self, product, dest, qty):
        supplier = self.env.ref('stock.stock_location_suppliers')
        pick = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_wh.in_type_id.id, 'location_id': supplier.id,
            'location_dest_id': dest.id, 'company_id': self.company.id, 'origin': 'OPENING',
            'move_ids': [Command.create({
                'product_id': product.id, 'product_uom_qty': qty,
                'product_uom': product.uom_id.id, 'location_id': supplier.id,
                'location_dest_id': dest.id, 'company_id': self.company.id})]})
        pick.action_confirm(); pick.action_assign()
        pick.with_context(picking_ids_not_to_backorder=pick.ids, skip_backorder=True).button_validate()
        return pick

    def _set_cutover(self, dt=CUTOVER):
        self.instance.sudo().write({'fba_auto_process_from': fields.Datetime.to_datetime(dt) if dt else False})

    def _qty(self, product, location):
        product.invalidate_recordset()
        return product.sudo().with_company(self.company).with_context(location=location.id).qty_available

    def _order_line(self, ref, sku='CUT-SKU', qty=5, product=None, ap=None, purchase=POST_DT):
        product = product or self.product
        ap = ap or self.amazon_product
        vals = {'amazon_order_ref': ref, 'instance_id': self.instance.id,
                'fulfillment_channel': 'AFN', 'amazon_status': 'Shipped'}
        if purchase:
            vals['purchase_date'] = fields.Datetime.to_datetime(purchase)
        order = self.env['amazon.sale.order'].sudo().create(vals)
        line = self.env['amazon.sale.order.line'].sudo().create({
            'order_id': order.id, 'amazon_order_item_id': '%s-IT-%s' % (ref, sku),
            'amazon_product_id': ap.id, 'odoo_product_id': product.id, 'sku': sku, 'quantity': qty})
        return order, line

    def _event(self, cumulative, ref, purchase=POST_DT, qty=5, sku='CUT-SKU', product=None, ap=None):
        order, line = self._order_line(ref, sku=sku, qty=qty, product=product, ap=ap, purchase=purchase)
        ev = self.SE.sudo().upsert_from_order_line(line, cumulative, fields.Datetime.now())
        return order, line, ev

    def _reupsert(self, line, cumulative):
        return self.SE.sudo().upsert_from_order_line(line, cumulative, fields.Datetime.now())

    # import-job path (lineage tests)
    def _raw(self, oid, created=POST_DT, qty=1, shipped=1, sku='CUT-SKU', unit='100.0'):
        created_iso = fields.Datetime.to_datetime(created).strftime('%Y-%m-%dT%H:%M:%SZ')
        return {'orderId': oid, 'createdTime': created_iso, 'lastUpdatedTime': created_iso,
                'salesChannel': {'marketplaceName': 'Amazon.eg'},
                'fulfillment': {'fulfilledBy': 'AMAZON', 'fulfillmentStatus': 'SHIPPED'},
                'orderItems': [{'orderItemId': 'IT-' + oid, 'quantityOrdered': qty,
                    'product': {'sellerSku': sku, 'asin': 'B0', 'title': sku,
                        'price': {'unitPrice': {'amount': unit, 'currencyCode': 'EGP'}}},
                    'fulfillment': {'quantityFulfilled': shipped}}]}

    def _job(self):
        return self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 10, 1), 'date_to': datetime(2026, 10, 10)})

    def _import(self, raw, hold=None):
        od = AmazonAPI._normalize_order_2026(raw)
        job = self._job()
        if hold is not None:
            job = job.with_context(amazon_hold_fba_stock=hold)
        job._import_one_order(MagicMock(spec=AmazonAPI), 'tok', od)
        return self.SE.search([('instance_id', '=', self.instance.id),
                               ('amazon_order_ref', '=', raw['orderId'])])

    def _run_cron(self):
        return self.SE.cron_process_fba_sale_stock_events()

    # ======================= TESTS =======================
    def test_01_pre_cutover_held(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-1', purchase=PRE_DT)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, PRE)
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertFalse(ev.picking_ids)

    def test_02_post_cutover_pending(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-2', purchase=POST_DT)
        self.assertEqual(ev.state, 'pending')
        self.assertFalse(ev.last_error_code)

    def test_03_exactly_at_cutover_eligible(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-3', purchase=CUTOVER)
        self.assertEqual(ev.state, 'pending')

    def test_04_missing_purchase_date_held(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-4', purchase=False)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, MISSING)
        self.assertFalse(ev.picking_ids)

    def test_05_missing_date_blocked_by_processor(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-5', purchase=False)
        ev.sudo().write({'state': 'pending', 'last_error_code': False})  # simulate unintended pending
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, MISSING)
        self.assertFalse(ev.picking_ids)

    def test_06_existing_controlled_hold_stays_held(self):
        # HELD controlled-import event must never be converted by the cutover gate.
        ev = self._import(self._raw('C-6', created=POST_DT), hold=True)
        self.assertEqual(ev.last_error_code, HOLD)
        self._set_cutover()
        line = ev.order_line_id
        self._reupsert(line, 1)
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertEqual(ev.processed_fulfilled_qty, 0)

    def test_07_reimport_cannot_release_pre_cutover(self):
        self._set_cutover()
        _o, line, ev = self._event(1, 'C-7', purchase=PRE_DT, qty=5)
        self.assertEqual(ev.last_error_code, PRE)
        self._reupsert(line, 3)  # cumulative increase re-import
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, PRE)
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertFalse(ev.picking_ids)

    def test_08_status_sync_cannot_release_pre_cutover(self):
        self._set_cutover()
        _o, line, ev = self._event(1, 'C-8', purchase=PRE_DT)
        job = self._job()
        job._upsert_order_items(ev.order_id, [{
            'OrderItemId': line.amazon_order_item_id, 'SellerSKU': line.sku,
            'ASIN': 'B0', 'Title': 'x', 'QuantityOrdered': line.quantity, 'QuantityShipped': 1}])
        ev.invalidate_recordset()
        self.assertEqual(ev.last_error_code, PRE)
        self.assertEqual(ev.state, 'manual_review')

    def test_09_fresh_holdfalse_job_respects_cutover(self):
        self._set_cutover()
        ev_pre = self._import(self._raw('C-9A', created=PRE_DT), hold=False)
        ev_post = self._import(self._raw('C-9B', created=POST_DT), hold=False)
        self.assertEqual(ev_pre.state, 'manual_review')
        self.assertEqual(ev_pre.last_error_code, PRE)
        self.assertEqual(ev_post.state, 'pending')

    def test_10_continuation_holdtrue_still_holds(self):
        self._set_cutover()
        ev = self._import(self._raw('C-10', created=POST_DT), hold=True)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)

    def test_11_holdfalse_no_cutover_fails_closed(self):
        self._set_cutover(False)  # no cutover
        ev = self._import(self._raw('C-11', created=POST_DT), hold=False)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, NOCFG)
        self.assertFalse(ev.picking_ids)

    def test_12_completed_event_idempotent(self):
        self._set_cutover()
        _o, line, ev = self._event(2, 'C-12', purchase=POST_DT)
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(ev.processed_fulfilled_qty, 2)
        n = len(ev.picking_ids)
        self._reupsert(line, 2)  # same cumulative
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(ev.processed_fulfilled_qty, 2)
        self.assertEqual(len(ev.picking_ids), n)

    def test_13_partial_delta_only(self):
        self._set_cutover()
        _o, line, ev = self._event(1, 'C-13', purchase=POST_DT, qty=5)
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.processed_fulfilled_qty, 1)
        self._reupsert(line, 3)
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.processed_fulfilled_qty, 3)
        moved = sum(ev.picking_ids.mapped(lambda p: sum(p.move_ids.mapped('quantity'))))
        self.assertEqual(moved, 3)
        self.assertEqual(len(ev.picking_ids), 2)

    def test_14_canceled_zero_qty_no_deduction(self):
        self._set_cutover()
        _o, _l, ev = self._event(0, 'C-14', purchase=POST_DT)
        self.assertEqual(ev.state, 'done')
        self.assertEqual(ev.processed_fulfilled_qty, 0)
        self.assertFalse(ev.picking_ids)

    def test_15_multi_item_independent(self):
        self._set_cutover()
        p2, ap2 = self._product('CUT-SKU2')
        self._put_stock(p2, self.instance.fba_sellable_location_id, 50)
        _o1, _l1, e1 = self._event(2, 'C-15', purchase=POST_DT)
        _o2, _l2, e2 = self._event(3, 'C-15b', purchase=POST_DT, sku='CUT-SKU2', product=p2, ap=ap2)
        e1._process_one(); e2._process_one()
        e1.invalidate_recordset(); e2.invalidate_recordset()
        self.assertEqual(e1.processed_fulfilled_qty, 2)
        self.assertEqual(e2.processed_fulfilled_qty, 3)

    def test_16_cron_cannot_bypass_cutover(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-16', purchase=PRE_DT)
        ev.sudo().write({'state': 'pending', 'last_error_code': False})  # unintended pending
        sell_before = self._qty(self.product, self.instance.fba_sellable_location_id)
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, PRE)
        self.assertFalse(ev.picking_ids)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), sell_before)

    def test_17_manual_retry_authorizes_processing(self):
        self._set_cutover()
        _o, _l, ev = self._event(4, 'C-17', purchase=PRE_DT)
        self.assertEqual(ev.last_error_code, PRE)
        ev.action_retry()
        self.assertTrue(ev.cutover_manual_release)
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(ev.processed_fulfilled_qty, 4)
        self.assertTrue(ev.picking_ids)

    def test_18_changing_cutover_does_not_release(self):
        self._set_cutover()
        _o, _l, ev = self._event(5, 'C-18', purchase=PRE_DT)
        self.assertEqual(ev.last_error_code, PRE)
        # Move the cutover earlier so the order would now be "post-cutover".
        self._set_cutover('2026-09-01 00:00:00')
        # A pure config change must not flip a held event to pending by itself.
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertFalse(ev.picking_ids)
        self.assertEqual(ev.processed_fulfilled_qty, 0)

    def test_19_no_so_delivery_invoice_payment(self):
        self._set_cutover()
        ev = self._import(self._raw('C-19', created=POST_DT), hold=False)
        self.assertEqual(ev.state, 'pending')
        ev._process_one()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'done')
        aso = self.env['amazon.sale.order'].search([('amazon_order_ref', '=', 'C-19')], limit=1)
        so = aso.sale_order_id
        self.assertEqual(so.state, 'draft')
        self.assertFalse(so.invoice_ids)
        self.assertFalse(self.env['stock.picking'].search([('sale_id', '=', so.id)]))
        self.assertFalse(self.env['account.payment'].search([('partner_id', '=', so.partner_id.id)]))

    def test_20_no_amazon_writes_during_processing(self):
        self._set_cutover()
        _o, _l, ev = self._event(3, 'C-20', purchase=POST_DT)
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as mock_req:
            ev._process_one()
            mock_req.assert_not_called()
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'done')
        self.assertEqual(ev.processed_fulfilled_qty, 3)
