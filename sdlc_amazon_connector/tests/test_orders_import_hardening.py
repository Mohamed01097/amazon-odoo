from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'


@tagged('post_install', '-at_install')
class TestOrdersImportHardening(TransactionCase):
    """Phase 2 hardening: real Amazon Egypt response shape, controlled import."""

    def setUp(self):
        super().setUp()
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        base_sale = self.env['account.tax'].search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', self.env.company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (hardening)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Orders Hardening Instance',
            'company_id': self.env.company.id,
            'marketplace_id': EG,
            'region': 'eu',
            'fba_warehouse_id': warehouse.id,
            'fbm_warehouse_id': warehouse.id,
            'amazon_sales_tax_id': self.vat14.id,
        })
        self.product = self.env['product.product'].sudo().create({
            'name': 'Hardening Towel',
            'type': 'consu',
            'is_storable': True,
            'list_price': 0.0,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Hardening Towel',
            'instance_id': self.instance.id,
            'sku': 'PE-FCJU-3D68',
            'odoo_product_id': self.product.id,
        })

    # ---- raw response builders (REAL Amazon Egypt shape) ----
    @staticmethod
    def _raw_order(order_id='402-REAL-0001', qty=1, unit='295.0',
                   proceeds=None, recipient=None, include_product_price=True,
                   status='SHIPPED', shipped=None):
        product = {'sellerSku': 'PE-FCJU-3D68', 'asin': 'B0FPG8TY71', 'title': 'Towel'}
        if include_product_price:
            product['price'] = {'unitPrice': {'amount': unit, 'currencyCode': 'EGP'}}
        item = {
            'orderItemId': 'ITEM-%s' % order_id,
            'quantityOrdered': qty,
            'product': product,
            'fulfillment': {'quantityFulfilled': qty if shipped is None else shipped},
        }
        if proceeds is not None:
            item['proceeds'] = proceeds
        order = {
            'orderId': order_id,
            'createdTime': '2026-10-01T12:29:48Z',
            'lastUpdatedTime': '2026-10-02T00:38:04Z',
            'salesChannel': {'marketplaceName': 'Amazon.eg'},
            'fulfillment': {'fulfilledBy': 'AMAZON', 'fulfillmentStatus': status,
                            'fulfillmentServiceLevel': 'STANDARD'},
            'orderItems': [item],
        }
        if recipient is not None:
            order['recipient'] = recipient
        return order

    # ---------------- normalization ----------------
    def test_01_unitprice_primary_null_proceeds(self):
        """Case A: proceeds null, recipient null, unitPrice 295, qty 1."""
        norm = AmazonAPI._normalize_order_2026(self._raw_order())
        self.assertEqual(float(norm['OrderItems'][0]['ItemPrice']['Amount']), 295.0)
        self.assertEqual(float(norm['OrderTotal']['Amount']), 295.0)
        self.assertEqual(norm['OrderItems'][0]['ItemPrice']['CurrencyCode'], 'EGP')
        self.assertEqual(norm['ShippingAddress'], {})

    def test_02_unitprice_extended_by_quantity(self):
        """Case B: qty > 1 -> staging ItemPrice is extended; unit price preserved."""
        norm = AmazonAPI._normalize_order_2026(self._raw_order(qty=2, unit='100.0'))
        self.assertEqual(float(norm['OrderItems'][0]['ItemPrice']['Amount']), 200.0)
        self.assertEqual(float(norm['OrderTotal']['Amount']), 200.0)

    def test_03_proceeds_fallback_when_no_product_price(self):
        """Case F/G: no unitPrice, proceeds present -> use proceeds, grandTotal."""
        raw = self._raw_order(
            include_product_price=False,
            proceeds={'breakdowns': [
                {'type': 'ITEM', 'subtotal': {'amount': '100.00', 'currencyCode': 'EGP'}},
            ]},
        )
        raw['proceeds'] = {'grandTotal': {'amount': '116.00', 'currencyCode': 'EGP'}}
        norm = AmazonAPI._normalize_order_2026(raw)
        self.assertEqual(norm['OrderItems'][0]['ItemPrice']['Amount'], '100.00')
        self.assertEqual(norm['OrderTotal']['Amount'], '116.00')

    def test_04_both_price_sources_absent_is_zero(self):
        """Case H (normalization half): no price anywhere -> zero (SO guard tested below)."""
        norm = AmazonAPI._normalize_order_2026(self._raw_order(include_product_price=False))
        self.assertEqual(float(norm['OrderItems'][0]['ItemPrice'].get('Amount') or 0), 0.0)
        self.assertEqual(float(norm['OrderTotal']['Amount']), 0.0)

    # ---------------- import pipeline ----------------
    def _import(self, raw, hold=False):
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 10, 1),
            'date_to': datetime(2026, 10, 3),
        })
        order_data = AmazonAPI._normalize_order_2026(raw)
        api = MagicMock(spec=AmazonAPI)
        job.with_context(amazon_hold_fba_stock=hold)._import_one_order(api, 'tok', order_data)
        return self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', raw['orderId']),
        ], limit=1)

    def test_05_import_sets_correct_sale_order_price_unit(self):
        order = self._import(self._raw_order(qty=2, unit='100.0'))
        so = order.sale_order_id
        self.assertTrue(so)
        self.assertEqual(so.state, 'draft')
        line = so.order_line[0]
        self.assertEqual(line.product_uom_qty, 2)
        self.assertEqual(line.price_unit, 100.0)          # extended 200 / qty 2
        # VAT-inclusive Egypt tax now applied; gross total stays 200 (never +14%).
        self.assertEqual(line.tax_ids, self.vat14)
        self.assertEqual(so.amount_total, 200.0)

    def test_06_import_single_unit_price(self):
        order = self._import(self._raw_order(qty=1, unit='295.0'))
        self.assertEqual(order.sale_order_id.amount_total, 295.0)

    def test_07_zero_price_does_not_create_sale_order(self):
        order = self._import(self._raw_order(include_product_price=False))
        self.assertFalse(order.sale_order_id)
        self.assertTrue(order.requires_status_review)
        # Manual creation must also refuse a zero-value order.
        with self.assertRaisesRegex(UserError, 'no positive item price'):
            order.action_create_sale_order()

    def test_08_recipient_null_uses_scoped_generic_partner(self):
        order = self._import(self._raw_order())
        partner = order.sale_order_id.partner_id
        self.assertIn('Amazon Customer - Orders Hardening Instance', partner.name)
        self.assertEqual(partner.company_id, self.instance.company_id)

    def test_09_afn_confirm_creates_no_normal_delivery(self):
        order = self._import(self._raw_order())
        so = order.sale_order_id
        so.action_confirm()
        self.assertEqual(so.state, 'sale')
        self.assertFalse(so.picking_ids)                  # Amazon owns FBA fulfilment

    def test_10_held_fba_event_not_processed(self):
        order = self._import(self._raw_order(), hold=True)
        event = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', order.amazon_order_ref),
        ], limit=1)
        self.assertTrue(event)
        self.assertEqual(event.state, 'manual_review')
        self.assertEqual(event.last_error_code, 'HELD_FOR_CONTROLLED_IMPORT')
        self.assertFalse(event.picking_ids)

    def test_11_import_idempotent(self):
        raw = self._raw_order()
        first = self._import(raw)
        second = self._import(raw)
        self.assertEqual(first, second)
        orders = self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', raw['orderId']),
        ])
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders.order_line_ids), 1)
        sos = self.env['sale.order'].sudo().search([
            ('amazon_instance_id', '=', self.instance.id),
            ('client_order_ref', '=', raw['orderId']),
        ])
        self.assertEqual(len(sos), 1)

    # ---------------- controlled wizard ----------------
    def _run_wizard(self, raw, hold=True):
        wiz = self.env['amazon.import.order.by.id.wizard'].sudo().create({
            'instance_id': self.instance.id,
            'amazon_order_ref': raw['orderId'],
            'hold_fba_stock': hold,
        })
        payload = {'payload': AmazonAPI._normalize_order_2026(raw)}
        with (
            patch.object(type(self.instance), '_check_amazon_manager_access', return_value=True),
            patch.object(type(self.instance), '_auto_fix_region', return_value=True),
            patch.object(type(self.instance), '_check_required_fields', return_value=True),
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='tok'),
            patch.object(AmazonAPI, 'get_order', autospec=True, return_value=payload),
            patch.object(AmazonAPI, 'get_orders', autospec=True) as get_orders,
        ):
            wiz.action_import()
            self.get_orders_mock = get_orders
        return self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', raw['orderId']),
        ], limit=1)

    def test_12_wizard_imports_exactly_one_order(self):
        raw = self._raw_order(order_id='402-WIZ-0001')
        order = self._run_wizard(raw)
        self.assertEqual(len(order), 1)
        self.assertEqual(order.sale_order_id.amount_total, 295.0)
        # getOrder path only; never the broad search.
        self.get_orders_mock.assert_not_called()

    def test_13_wizard_is_idempotent(self):
        raw = self._raw_order(order_id='402-WIZ-0002')
        self._run_wizard(raw)
        self._run_wizard(raw)
        orders = self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', '402-WIZ-0002'),
        ])
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders.order_line_ids), 1)

    def test_14_wizard_does_not_touch_sync_state(self):
        before_sync = self.instance.last_order_sync
        before_auto = self.instance.auto_sync_enabled
        cron = self.env.ref('sdlc_amazon_connector.cron_amazon_process_order_import_jobs')
        cron.sudo().active = False
        self._run_wizard(self._raw_order(order_id='402-WIZ-0003'))
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.last_order_sync, before_sync)
        self.assertEqual(self.instance.auto_sync_enabled, before_auto)
        self.assertFalse(cron.active)                     # wizard did not re-activate it

    def test_15_wizard_hold_keeps_fba_event_unprocessed(self):
        order = self._run_wizard(self._raw_order(order_id='402-WIZ-0004'), hold=True)
        event = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', order.amazon_order_ref),
        ], limit=1)
        self.assertEqual(event.state, 'manual_review')
        self.assertFalse(event.picking_ids)

    def test_16_wizard_job_is_terminal_and_cron_invisible(self):
        self._run_wizard(self._raw_order(order_id='402-WIZ-0005'))
        job = self.env['amazon.order.import.job'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('single_order_ref', '=', '402-WIZ-0005'),
        ], limit=1)
        self.assertTrue(job.is_single_order)
        self.assertEqual(job.state, 'done')               # cron selects draft/running only
        self.assertFalse(job.next_run_at)
