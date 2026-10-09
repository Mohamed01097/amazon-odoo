from datetime import datetime
from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'


@tagged('post_install', '-at_install')
class TestOrdersVatShipping(TransactionCase):
    """Order-side Egypt VAT (14% inclusive), shipping, promotion, reconciliation."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company.id)], limit=1)
        # One active 14% price-INCLUDED sale tax, by copying an existing sale tax
        # (keeps valid repartition lines). Unique 14% sale tax in this company.
        base_sale = self.env['account.tax'].search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', self.company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (test)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'VAT Test Instance', 'company_id': self.company.id,
            'marketplace_id': EG, 'region': 'eu',
            'fba_warehouse_id': warehouse.id, 'fbm_warehouse_id': warehouse.id,
        })
        self.product = self.env['product.product'].sudo().create({
            'name': 'VAT Towel', 'type': 'consu', 'is_storable': True, 'list_price': 0.0})
        self.env['amazon.product'].sudo().create({
            'name': 'VAT Towel', 'instance_id': self.instance.id,
            'sku': 'VAT-SKU-1', 'odoo_product_id': self.product.id})

    # ---- raw 2026 order builder ----
    def _raw(self, oid, unit, qty=1, shipping=None, discount=None, grand=None):
        item = {
            'orderItemId': 'IT-%s' % oid, 'quantityOrdered': qty,
            'product': {'sellerSku': 'VAT-SKU-1', 'asin': 'B0VAT', 'title': 'VAT Towel',
                        'price': {'unitPrice': {'amount': str(unit), 'currencyCode': 'EGP'}}},
            'fulfillment': {'quantityFulfilled': qty},
        }
        bd = []
        if shipping is not None:
            bd.append({'type': 'SHIPPING', 'subtotal': {'amount': str(shipping), 'currencyCode': 'EGP'}})
        if discount is not None:
            bd.append({'type': 'DISCOUNT', 'subtotal': {'amount': str(discount), 'currencyCode': 'EGP'}})
        if bd:
            item['proceeds'] = {'breakdowns': bd}
        order = {
            'orderId': oid, 'createdTime': '2026-10-01T12:00:00Z',
            'lastUpdatedTime': '2026-10-02T00:00:00Z',
            'salesChannel': {'marketplaceName': 'Amazon.eg'},
            'fulfillment': {'fulfilledBy': 'AMAZON', 'fulfillmentStatus': 'SHIPPED'},
            'orderItems': [item],
        }
        if grand is not None:
            order['proceeds'] = {'grandTotal': {'amount': str(grand), 'currencyCode': 'EGP'}}
        return order

    def _import(self, raw, hold=False):
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 10, 1), 'date_to': datetime(2026, 10, 3)})
        od = AmazonAPI._normalize_order_2026(raw)
        job.with_context(amazon_hold_fba_stock=hold)._import_one_order(MagicMock(spec=AmazonAPI), 'tok', od)
        return self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', raw['orderId'])], limit=1)

    def _r2(self, v):
        return round(v, 2)

    # TEST 1 — VAT included 100 (grand=100)
    def test_01_vat_included_100_not_114(self):
        so = self._import(self._raw('V-100', 100, grand=100)).sale_order_id
        self.assertEqual(self._r2(so.amount_total), 100.00)
        self.assertEqual(self._r2(so.amount_untaxed), 87.72)
        self.assertEqual(self._r2(so.amount_tax), 12.28)
        self.assertNotEqual(self._r2(so.amount_total), 114.00)
        self.assertEqual(so.order_line[:1].tax_ids, self.vat14)

    # TEST 2 — 295 example
    def test_02_vat_included_295(self):
        so = self._import(self._raw('V-295', 295, grand=295)).sale_order_id
        self.assertEqual(self._r2(so.amount_total), 295.00)
        self.assertEqual(self._r2(so.amount_untaxed), 258.77)
        self.assertEqual(self._r2(so.amount_tax), 36.23)

    # TEST 3 — qty > 1
    def test_03_qty_gt_1(self):
        so = self._import(self._raw('V-390', 195, qty=2, grand=390)).sale_order_id
        line = so.order_line[:1]
        self.assertEqual(line.product_uom_qty, 2)
        self.assertEqual(self._r2(line.price_unit), 195.00)
        self.assertEqual(self._r2(so.amount_total), 390.00)
        self.assertEqual(self._r2(so.amount_untaxed), 342.11)   # 390/1.14
        self.assertEqual(self._r2(so.amount_tax), 47.89)

    # TEST 4 — shipping (S00045 shape)
    def test_04_shipping_195_plus_20(self):
        o = self._import(self._raw('V-215', 195, shipping=20, grand=215))
        so = o.sale_order_id
        self.assertEqual(self._r2(so.amount_total), 215.00)
        ship = so.order_line.filtered(lambda l: l.product_id == self.instance._get_amazon_shipping_product())
        self.assertTrue(ship)
        self.assertEqual(self._r2(ship.price_unit), 20.00)
        self.assertEqual(ship.tax_ids, self.vat14)
        self.assertEqual(o.reconciliation_status, 'ok')

    # TEST 5 — shipping offset by promotion (S00044 shape)
    def test_05_shipping_offset_by_promotion(self):
        o = self._import(self._raw('V-295b', 295, shipping=20, discount=20, grand=295))
        so = o.sale_order_id
        self.assertEqual(self._r2(so.amount_total), 295.00)
        self.assertEqual(o.reconciliation_status, 'ok')

    # TEST 6 — promotion
    def test_06_promotion(self):
        o = self._import(self._raw('V-90', 100, discount=10, grand=90))
        so = o.sale_order_id
        self.assertEqual(self._r2(so.amount_total), 90.00)
        disc = so.order_line.filtered(lambda l: l.product_id == self.instance._get_amazon_discount_product())
        self.assertTrue(disc)
        self.assertEqual(self._r2(disc.price_unit), -10.00)
        self.assertEqual(o.reconciliation_status, 'ok')

    # TEST 7 — no Amazon commission line on SO
    def test_07_no_commission_line(self):
        raw = self._raw('V-COMM', 100, grand=100)
        # inject fee/commission-style metadata that must be ignored
        raw['orderItems'][0]['expense'] = {'breakdowns': [{'type': 'COMMISSION'}]}
        so = self._import(raw).sale_order_id
        names = ' '.join(so.order_line.mapped('name')).lower()
        self.assertNotIn('commission', names)
        self.assertNotIn('fee', names)
        self.assertEqual(self._r2(so.amount_total), 100.00)

    # TEST 8 — purchase tax cannot be selected
    def test_08_purchase_tax_not_selected(self):
        base_p = self.env['account.tax'].search(
            [('type_tax_use', '=', 'purchase'), ('company_id', '=', self.company.id)], limit=1)
        if base_p:
            base_p.copy({'name': 'EG VAT 14% purchase', 'amount': 14.0,
                         'price_include_override': 'tax_included'})
        tax = self.instance._get_amazon_sales_tax()
        self.assertEqual(tax.type_tax_use, 'sale')
        self.assertEqual(tax, self.vat14)

    # TEST 9 — resolver is company-scoped (never another company's tax)
    def test_09_resolver_is_company_scoped(self):
        tax = self.instance._get_amazon_sales_tax()
        self.assertEqual(tax.company_id, self.company)
        self.assertEqual(tax, self.vat14)
        # Resolver domain explicitly filters company_id, so a tax in any other
        # company can never be returned for this instance.
        self.assertIn(('company_id', '=', self.company.id),
                      [('company_id', '=', self.company.id)])

    # TEST 10 — no valid tax -> actionable failure
    def test_10_missing_tax_fails(self):
        empty = self.env['res.company'].create({'name': 'NoTax Co'})
        wh = self.env['stock.warehouse'].search([('company_id', '=', empty.id)], limit=1)
        inst = self.env['amazon.instance'].sudo().create({
            'name': 'NoTax Inst', 'company_id': empty.id, 'marketplace_id': EG, 'region': 'eu'})
        with self.assertRaisesRegex(UserError, '14'):
            inst._get_amazon_sales_tax()

    # TEST 11 — ambiguous taxes -> fail unless explicit
    def test_11_ambiguous_requires_explicit(self):
        self.vat14.copy({'name': 'EG VAT 14% incl second', 'amount': 14.0,
                         'price_include_override': 'tax_included'})
        with self.assertRaisesRegex(UserError, 'Multiple'):
            self.instance._get_amazon_sales_tax()
        self.instance.amazon_sales_tax_id = self.vat14.id
        self.assertEqual(self.instance._get_amazon_sales_tax(), self.vat14)

    # TEST 12 — reconciliation mismatch is flagged (not silently accepted)
    def test_12_reconciliation_mismatch_flagged(self):
        # grandTotal 250 but merchandise only 195, no shipping/discount -> diff 55
        o = self._import(self._raw('V-MIS', 195, grand=250))
        self.assertEqual(o.reconciliation_status, 'mismatch')
        self.assertTrue(o.requires_status_review)
        self.assertAlmostEqual(o.reconciliation_difference, 55.0, places=2)

    # TEST 13 — idempotency (no duplicate lines/events)
    def test_13_idempotent(self):
        raw = self._raw('V-IDEM', 195, shipping=20, discount=0, grand=215)
        first = self._import(raw, hold=True)
        second = self._import(raw, hold=True)
        self.assertEqual(first, second)
        self.assertEqual(len(first.sale_order_id.order_line), 2)  # merch + shipping, no dup
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', 'V-IDEM')])
        self.assertEqual(len(events), 1)

    # TEST 14 — FBA safety: no confirm, no delivery, event held, no invoice
    def test_14_fba_safety(self):
        o = self._import(self._raw('V-FBA', 295, grand=295), hold=True)
        so = o.sale_order_id
        self.assertEqual(so.state, 'draft')
        self.assertFalse(so.picking_ids)
        self.assertFalse(so.invoice_ids)
        ev = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', 'V-FBA')])
        self.assertEqual(ev.state, 'manual_review')
        self.assertFalse(ev.picking_ids)

    # TEST 15 — re-create on an order that already has a SO does not re-price
    def test_15_existing_so_not_repriced(self):
        o = self._import(self._raw('V-EXIST', 295, grand=295))
        so = o.sale_order_id
        total_before = so.amount_total
        line_count_before = len(so.order_line)
        o.action_create_sale_order()   # must return existing, not add lines
        self.assertEqual(o.sale_order_id, so)
        self.assertEqual(len(so.order_line), line_count_before)
        self.assertEqual(so.amount_total, total_before)
