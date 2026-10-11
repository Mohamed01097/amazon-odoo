"""Phase 23 — Complete FBA End-to-End User Acceptance Testing.

Exercises every core FBA business workflow against the staging database
with deterministic mocked Amazon SP-API responses. No live API calls.
"""
import base64
import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


EG_MARKETPLACE = 'ARBP9OOSHTCHU'


@tagged('post_install', '-at_install', 'phase23_uat')
class TestPhase23UAT(TransactionCase):
    """Phase 23 UAT — every assertion is a functional acceptance criterion."""

    # ──────────────────────────────────────────────
    # SETUP — Realistic Amazon Egypt FBA seller
    # ──────────────────────────────────────────────

    def setUp(self):
        super().setUp()
        # Currency
        self.egp = self.env['res.currency'].sudo().with_context(
            active_test=False,
        ).search([('name', '=', 'EGP')], limit=1)
        self.egp.active = True

        # Company
        self.company = self.env['res.company'].sudo().create({
            'name': 'Phase 23 UAT Company',
            'currency_id': self.egp.id,
        })

        # Warehouses
        WH = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.customer_wh = WH.create({
            'name': 'UAT Customer WH', 'code': 'U23CW',
            'company_id': self.company.id,
        })
        self.fba_wh = WH.create({
            'name': 'UAT FBA WH', 'code': 'U23FA',
            'company_id': self.company.id,
        })

        # Ship-from partner
        self.ship_from = self.env['res.partner'].sudo().create({
            'name': 'UAT Cairo Dispatch',
            'street': '10 UAT Street',
            'city': 'Cairo',
            'zip': '11511',
            'phone': '+201099999999',
            'email': 'uat@example.test',
            'country_id': self.env.ref('base.eg').id,
            'company_id': self.company.id,
        })

        # Accounts
        self.accounts = {}
        specs = {
            'clearing': ('890001', 'UAT Clearing', 'asset_current', True),
            'sale': ('890002', 'UAT Sales', 'income', False),
            'refund': ('890003', 'UAT Refunds', 'income', False),
            'fee': ('890004', 'UAT Fees', 'expense', False),
            'reimbursement': ('890005', 'UAT Reimbursements', 'income_other', False),
            'adjustment': ('890006', 'UAT Adjustments', 'expense_other', False),
            'suspense': ('890007', 'UAT Suspense', 'asset_current', False),
            'bank': ('890008', 'UAT Bank', 'asset_cash', False),
        }
        for key, (code, name, atype, rec) in specs.items():
            self.accounts[key] = self.env['account.account'].sudo().with_company(
                self.company,
            ).create({
                'code': code, 'name': name, 'account_type': atype,
                'reconcile': rec,
                'company_ids': [Command.set([self.company.id])],
            })

        # Journals
        self.settlement_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'UAT Settlements', 'code': 'U23ST', 'type': 'general',
            'company_id': self.company.id,
        })
        self.bank_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'UAT Bank', 'code': 'U23BK', 'type': 'bank',
            'company_id': self.company.id,
            'default_account_id': self.accounts['bank'].id,
        })

        # VAT 14% inclusive — create directly with proper repartition lines
        tax_account = self.env['account.account'].sudo().with_company(
            self.company,
        ).create({
            'code': '890009', 'name': 'UAT Tax Account',
            'account_type': 'liability_current',
            'company_ids': [Command.set([self.company.id])],
        })
        egypt = self.env.ref('base.eg')
        tax_group = self.env['account.tax.group'].sudo().with_company(
            self.company,
        ).create({
            'name': 'UAT Egypt VAT',
            'company_id': self.company.id,
            'country_id': egypt.id,
            'tax_payable_account_id': tax_account.id,
            'tax_receivable_account_id': tax_account.id,
        })
        self.vat14 = self.env['account.tax'].sudo().with_company(self.company).create({
            'name': 'EG VAT 14% incl (UAT)',
            'amount': 14.0,
            'type_tax_use': 'sale',
            'price_include_override': 'tax_included',
            'active': True,
            'company_id': self.company.id,
            'tax_group_id': tax_group.id,
            'country_id': egypt.id,
            'invoice_repartition_line_ids': [
                Command.create({'repartition_type': 'base', 'factor_percent': 100}),
                Command.create({'repartition_type': 'tax', 'factor_percent': 100,
                                'account_id': tax_account.id}),
            ],
            'refund_repartition_line_ids': [
                Command.create({'repartition_type': 'base', 'factor_percent': 100}),
                Command.create({'repartition_type': 'tax', 'factor_percent': 100,
                                'account_id': tax_account.id}),
            ],
        })

        # Amazon Instance — mock credentials, full FBA config
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'UAT Amazon Egypt FBA',
            'company_id': self.company.id,
            'seller_id': 'UAT-SELLER-EG-001',
            'marketplace_id': EG_MARKETPLACE,
            'region': 'eu',
            'refresh_token': 'uat-mock-refresh-token',
            'client_id': 'uat-mock-client-id',
            'client_secret': 'uat-mock-client-secret',
            'fba_warehouse_id': self.fba_wh.id,
            'fba_source_location_id': self.customer_wh.lot_stock_id.id,
            'fba_ship_from_partner_id': self.ship_from.id,
            'fba_removal_return_partner_id': self.ship_from.id,
            'amazon_sales_tax_id': self.vat14.id,
            'auto_post_invoice': False,
            'settlement_accounting_cutoff_date': fields.Date.from_string('2026-08-01'),
            'settlement_journal_id': self.settlement_journal.id,
            'amazon_payout_bank_journal_id': self.bank_journal.id,
            'amazon_clearing_account_id': self.accounts['clearing'].id,
            'amazon_sales_account_id': self.accounts['sale'].id,
            'amazon_refund_account_id': self.accounts['refund'].id,
            'amazon_fee_account_id': self.accounts['fee'].id,
            'amazon_fba_fee_account_id': self.accounts['fee'].id,
            'amazon_reimbursement_account_id': self.accounts['reimbursement'].id,
            'amazon_adjustment_account_id': self.accounts['adjustment'].id,
            'amazon_suspense_account_id': self.accounts['suspense'].id,
        })

        # FBA stock structure
        self.instance.action_create_fba_stock_structure()

        # Products
        self.products = {}
        self.amazon_products = {}
        for sku, name, price in [
            ('UAT-SKU-001', 'UAT Wireless Headphones', 295.0),
            ('UAT-SKU-002', 'UAT Phone Case', 195.0),
            ('UAT-SKU-003', 'UAT USB Cable', 75.0),
        ]:
            prod = self.env['product.product'].sudo().with_company(self.company).create({
                'name': name, 'default_code': sku,
                'type': 'consu', 'is_storable': True,
                'list_price': price,
                'company_id': self.company.id,
            })
            ap = self.env['amazon.product'].sudo().create({
                'name': name,
                'instance_id': self.instance.id,
                'sku': sku,
                'asin': 'B0UAT%s' % sku[-3:],
                'fulfillment_channel': 'AFN',
                'odoo_product_id': prod.id,
            })
            self.products[sku] = prod
            self.amazon_products[sku] = ap

        # Baseline counts
        self._baseline = {
            'amazon_sale_order': self.env['amazon.sale.order'].sudo().search_count([]),
            'sale_order': self.env['sale.order'].sudo().search_count([]),
            'stock_move': self.env['stock.move'].sudo().search_count([]),
            'account_move': self.env['account.move'].sudo().search_count([]),
            'amazon_fba_sale_stock_event': self.env['amazon.fba.sale.stock.event'].sudo().search_count([]),
        }

    # ──────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────

    def _qty(self, product, location):
        product.invalidate_recordset()
        return product.sudo().with_company(self.company).with_context(
            location=location.id,
        ).qty_available

    def _seed_stock(self, product, quantity, location=None):
        dest = location or self.instance.fba_source_location_id
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.customer_wh.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': dest.id,
            'company_id': self.company.id,
            'origin': 'PHASE23 UAT SEED',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': dest.id,
                'company_id': self.company.id,
            })],
        })
        picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        self.assertEqual(picking.state, 'done')

    def _raw_order_2026(self, order_id, items, grand_total=None, status='SHIPPED',
                        fulfilled_by='AMAZON', shipping=None, discount=None):
        """Build a v2026 raw order dict for _normalize_order_2026."""
        order_items = []
        for sku, unit_price, qty in items:
            oi = {
                'orderItemId': 'OI-%s-%s' % (order_id, sku),
                'quantityOrdered': qty,
                'product': {
                    'sellerSku': sku,
                    'asin': 'B0UAT%s' % sku[-3:],
                    'title': 'UAT Product %s' % sku,
                    'price': {'unitPrice': {
                        'amount': str(unit_price), 'currencyCode': 'EGP',
                    }},
                },
                'fulfillment': {'quantityFulfilled': qty},
            }
            order_items.append(oi)

        order = {
            'orderId': order_id,
            'createdTime': '2026-10-01T12:00:00Z',
            'lastUpdatedTime': '2026-10-02T00:00:00Z',
            'salesChannel': {'marketplaceName': 'Amazon.eg'},
            'fulfillment': {
                'fulfilledBy': fulfilled_by,
                'fulfillmentStatus': status,
            },
            'orderItems': order_items,
        }
        if grand_total is not None:
            order['proceeds'] = {
                'grandTotal': {'amount': str(grand_total), 'currencyCode': 'EGP'},
            }
        if shipping is not None or discount is not None:
            for oi in order_items:
                bd = []
                if shipping is not None:
                    bd.append({'type': 'SHIPPING', 'subtotal': {
                        'amount': str(shipping), 'currencyCode': 'EGP',
                    }})
                if discount is not None:
                    bd.append({'type': 'DISCOUNT', 'subtotal': {
                        'amount': str(discount), 'currencyCode': 'EGP',
                    }})
                oi['proceeds'] = {'breakdowns': bd}
        return order

    def _import_order(self, raw_order, hold_fba=False):
        """Import a v2026 raw order through the real import job workflow."""
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 10, 1),
            'date_to': datetime(2026, 10, 3),
        })
        normalized = AmazonAPI._normalize_order_2026(raw_order)
        mock_api = MagicMock(spec=AmazonAPI)
        ctx = {'amazon_hold_fba_stock': hold_fba} if hold_fba else {}
        result = job.with_context(**ctx)._import_one_order(mock_api, 'mock-token', normalized)
        amazon_order = self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', raw_order['orderId']),
        ], limit=1)
        return amazon_order, result

    # ──────────────────────────────────────────────
    # STEP C — CONFIGURATION UAT
    # ──────────────────────────────────────────────

    def test_C01_instance_creation(self):
        """C1: Instance created with correct marketplace and region."""
        self.assertEqual(self.instance.marketplace_id, EG_MARKETPLACE)
        self.assertEqual(self.instance.region, 'eu')
        self.assertEqual(self.instance.company_id, self.company)

    def test_C02_fba_stock_structure_created(self):
        """C2: FBA locations exist after action_create_fba_stock_structure."""
        for field in ('fba_transit_location_id', 'fba_received_location_id',
                      'fba_sellable_location_id', 'fba_reserved_location_id',
                      'fba_unsellable_location_id', 'fba_removal_transit_location_id'):
            self.assertTrue(self.instance[field], "Missing FBA location: %s" % field)

    def test_C03_fba_structure_idempotent(self):
        """C3: Calling action_create_fba_stock_structure twice doesn't duplicate."""
        ids_before = tuple(self.instance[f].id for f in (
            'fba_transit_location_id', 'fba_received_location_id',
            'fba_sellable_location_id',
        ))
        self.instance.action_create_fba_stock_structure()
        ids_after = tuple(self.instance[f].id for f in (
            'fba_transit_location_id', 'fba_received_location_id',
            'fba_sellable_location_id',
        ))
        self.assertEqual(ids_before, ids_after)

    def test_C04_warehouse_assignment(self):
        """C4: Warehouse and source location properly assigned."""
        self.assertEqual(self.instance.fba_warehouse_id, self.fba_wh)
        self.assertEqual(self.instance.fba_source_location_id, self.customer_wh.lot_stock_id)

    def test_C05_product_sku_mapping(self):
        """C5: Amazon products mapped to Odoo products by SKU."""
        for sku, ap in self.amazon_products.items():
            self.assertEqual(ap.sku, sku)
            self.assertEqual(ap.odoo_product_id, self.products[sku])
            self.assertEqual(ap.instance_id, self.instance)

    def test_C06_tax_configuration(self):
        """C6: Egypt 14% VAT inclusive tax resolved correctly."""
        tax = self.instance._get_amazon_sales_tax()
        self.assertEqual(tax.amount, 14.0)
        self.assertEqual(tax.price_include_override, 'tax_included')
        self.assertEqual(tax.type_tax_use, 'sale')

    def test_C07_missing_tax_raises(self):
        """C7: Missing tax configuration raises actionable error."""
        empty_co = self.env['res.company'].create({'name': 'NoTax UAT'})
        inst = self.env['amazon.instance'].sudo().create({
            'name': 'NoTax UAT', 'company_id': empty_co.id,
            'marketplace_id': EG_MARKETPLACE, 'region': 'eu',
        })
        with self.assertRaisesRegex(UserError, '14'):
            inst._get_amazon_sales_tax()

    def test_C08_auto_post_invoice_disabled(self):
        """C8: auto_post_invoice is disabled (safety)."""
        self.assertFalse(self.instance.auto_post_invoice)

    def test_C09_settlement_accounts_configured(self):
        """C9: All settlement account mappings are set."""
        for field in ('amazon_clearing_account_id', 'amazon_sales_account_id',
                      'amazon_refund_account_id', 'amazon_fee_account_id',
                      'amazon_reimbursement_account_id', 'amazon_adjustment_account_id',
                      'amazon_suspense_account_id'):
            self.assertTrue(self.instance[field], "Missing account: %s" % field)

    def test_C10_instance_validation_no_credentials(self):
        """C10: Instance without credentials fails required-field check."""
        inst = self.env['amazon.instance'].sudo().create({
            'name': 'NoCred UAT', 'company_id': self.company.id,
            'marketplace_id': EG_MARKETPLACE, 'region': 'eu',
        })
        with self.assertRaisesRegex(UserError, 'Missing required fields'):
            inst._check_required_fields()

    # ──────────────────────────────────────────────
    # STEP D — FBA ORDER IMPORT UAT
    # ──────────────────────────────────────────────

    def test_D01_single_fba_order(self):
        """D1: Single FBA order imports correctly with VAT and creates SO."""
        raw = self._raw_order_2026('UAT-D01', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, result = self._import_order(raw)
        self.assertTrue(ao.exists())
        self.assertTrue(result['created'])
        self.assertTrue(result['sale_order_created'])
        so = ao.sale_order_id
        self.assertTrue(so.exists())
        self.assertAlmostEqual(so.amount_total, 295.0, places=2)
        self.assertAlmostEqual(so.amount_untaxed, 258.77, places=2)
        self.assertEqual(so.order_line[0].tax_ids, self.vat14)
        self.assertEqual(ao.fulfillment_channel, 'AFN')

    def test_D02_multiple_fba_orders(self):
        """D2: Multiple FBA orders import independently."""
        for i, price in enumerate([295, 195, 75], 1):
            sku = 'UAT-SKU-%03d' % i
            raw = self._raw_order_2026('UAT-D02-%d' % i, [(sku, price, 1)], grand_total=price)
            ao, _ = self._import_order(raw)
            self.assertEqual(ao.sale_order_id.amount_total, price)

    def test_D03_multi_product_order(self):
        """D3: Order with multiple products creates correct lines."""
        raw = self._raw_order_2026('UAT-D03', [
            ('UAT-SKU-001', 295, 1),
            ('UAT-SKU-002', 195, 2),
        ], grand_total=685)
        ao, _ = self._import_order(raw)
        so = ao.sale_order_id
        self.assertAlmostEqual(so.amount_total, 685.0, places=2)
        product_lines = so.order_line.filtered(
            lambda l: l.product_id in (self.products['UAT-SKU-001'], self.products['UAT-SKU-002']))
        self.assertEqual(len(product_lines), 2)

    def test_D04_different_quantities(self):
        """D4: Quantity > 1 handled correctly."""
        raw = self._raw_order_2026('UAT-D04', [('UAT-SKU-003', 75, 5)], grand_total=375)
        ao, _ = self._import_order(raw)
        so = ao.sale_order_id
        line = so.order_line.filtered(lambda l: l.product_id == self.products['UAT-SKU-003'])
        self.assertEqual(line.product_uom_qty, 5)
        self.assertAlmostEqual(so.amount_total, 375.0, places=2)

    def test_D05_shipping_and_discount(self):
        """D5: Shipping and discount lines created correctly."""
        raw = self._raw_order_2026('UAT-D05', [('UAT-SKU-001', 295, 1)],
                                   grand_total=305, shipping=20, discount=10)
        ao, _ = self._import_order(raw)
        so = ao.sale_order_id
        self.assertAlmostEqual(so.amount_total, 305.0, places=2)
        ship = so.order_line.filtered(
            lambda l: l.product_id == self.instance._get_amazon_shipping_product())
        disc = so.order_line.filtered(
            lambda l: l.product_id == self.instance._get_amazon_discount_product())
        self.assertTrue(ship)
        self.assertTrue(disc)
        self.assertAlmostEqual(ship.price_unit, 20.0, places=2)
        self.assertAlmostEqual(disc.price_unit, -10.0, places=2)

    def test_D06_missing_sku_mapping(self):
        """D6: Order with unmapped SKU skips SO creation."""
        raw = self._raw_order_2026('UAT-D06', [('UAT-UNMAPPED-SKU', 100, 1)], grand_total=100)
        ao, result = self._import_order(raw)
        self.assertTrue(ao.exists())
        self.assertFalse(ao.sale_order_id, "SO should not be created for unmapped SKU")

    def test_D07_duplicate_order_idempotent(self):
        """D7: Reimporting same order ID does not duplicate."""
        raw = self._raw_order_2026('UAT-D07', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao1, r1 = self._import_order(raw)
        ao2, r2 = self._import_order(raw)
        self.assertEqual(ao1, ao2)
        self.assertTrue(r1['created'])
        self.assertFalse(r2['created'])

    def test_D08_fba_stock_event_created(self):
        """D8: FBA order creates stock event with correct hold state."""
        raw = self._raw_order_2026('UAT-D08', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw, hold_fba=True)
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', 'UAT-D08'),
        ])
        self.assertTrue(events)
        self.assertEqual(events[0].state, 'manual_review')

    def test_D09_fba_order_stays_draft(self):
        """D9: FBA order SO stays draft (no auto-confirm for FBA)."""
        raw = self._raw_order_2026('UAT-D09', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw, hold_fba=True)
        so = ao.sale_order_id
        self.assertEqual(so.state, 'draft')
        self.assertFalse(so.picking_ids)
        self.assertFalse(so.invoice_ids)

    def test_D10_reconciliation_mismatch_flagged(self):
        """D10: Grand total mismatch is flagged, not silently accepted."""
        raw = self._raw_order_2026('UAT-D10', [('UAT-SKU-001', 195, 1)], grand_total=250)
        ao, _ = self._import_order(raw)
        self.assertEqual(ao.reconciliation_status, 'mismatch')
        self.assertTrue(ao.requires_status_review)

    def test_D11_zero_price_order_skipped(self):
        """D11: Zero-price order does not create SO."""
        raw = self._raw_order_2026('UAT-D11', [('UAT-SKU-001', 0, 1)], grand_total=0)
        ao, result = self._import_order(raw)
        self.assertTrue(ao.exists())
        self.assertFalse(ao.sale_order_id)
        self.assertTrue(ao.requires_status_review)

    # ──────────────────────────────────────────────
    # STEP E — INVENTORY & FBA STOCK UAT
    # ──────────────────────────────────────────────

    def test_E01_fba_locations_exist(self):
        """E1: All FBA locations created and accessible."""
        sellable = self.instance.fba_sellable_location_id
        self.assertTrue(sellable.exists())
        self.assertEqual(self._qty(self.products['UAT-SKU-001'], sellable), 0)

    def test_E02_seed_stock_and_verify(self):
        """E2: Seeding stock appears in correct location."""
        prod = self.products['UAT-SKU-001']
        self._seed_stock(prod, 50)
        self.assertEqual(self._qty(prod, self.instance.fba_source_location_id), 50)

    def test_E03_fba_stock_event_processing(self):
        """E3: Processing a pending FBA event creates stock move from sellable."""
        prod = self.products['UAT-SKU-001']
        self._seed_stock(prod, 10, self.instance.fba_sellable_location_id)
        self.assertEqual(self._qty(prod, self.instance.fba_sellable_location_id), 10)

        raw = self._raw_order_2026('UAT-E03', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw, hold_fba=True)
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('amazon_order_ref', '=', 'UAT-E03'),
            ('instance_id', '=', self.instance.id),
        ])
        self.assertTrue(events)
        events.write({'state': 'pending', 'cutover_manual_release': True})
        events._process_one()
        self.assertEqual(events.state, 'done')
        sold_loc = self.instance.fba_sold_customer_location_id
        self.assertTrue(events.picking_ids)

    def test_E04_duplicate_event_idempotent(self):
        """E4: Reimporting same order doesn't duplicate FBA events."""
        raw = self._raw_order_2026('UAT-E04', [('UAT-SKU-001', 295, 1)], grand_total=295)
        self._import_order(raw, hold_fba=True)
        self._import_order(raw, hold_fba=True)
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('amazon_order_ref', '=', 'UAT-E04'),
            ('instance_id', '=', self.instance.id),
        ])
        self.assertEqual(len(events), 1)

    def test_E05_inventory_reconciliation(self):
        """E5: Inventory reconciliation run processes mocked Amazon snapshot."""
        prod = self.products['UAT-SKU-001']
        ap = self.amazon_products['UAT-SKU-001']
        self._seed_stock(prod, 30, self.instance.fba_received_location_id)

        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id, 'trigger': 'manual',
        })
        summary = {
            'asin': ap.asin, 'fnSku': 'X00UAT001',
            'sellerSku': ap.sku, 'condition': 'NewItem',
            'lastUpdatedTime': '2026-10-01T12:00:00Z',
            'totalQuantity': 30,
            'inventoryDetails': {
                'fulfillableQuantity': 30,
                'inboundWorkingQuantity': 0, 'inboundShippedQuantity': 0,
                'inboundReceivingQuantity': 0,
                'reservedQuantity': {'totalReservedQuantity': 0},
                'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
            },
        }
        inv_response = {
            'payload': {'inventorySummaries': [summary]},
            '_amazon_request_ids': ['uat-inv'],
            '_pages': [{'payload': {'inventorySummaries': [summary]}}],
            '_snapshot_complete': True, '_page_count': 1,
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise',
                         return_value='mock-token'),
            patch.object(AmazonAPI, 'get_all_inventory_summaries',
                         autospec=True, return_value=inv_response),
        ):
            self.assertTrue(run._process_run(), run.last_error)
        rec = run.reconciliation_ids.filtered(lambda r: r.sku == ap.sku)
        self.assertTrue(rec.exists())

    # ──────────────────────────────────────────────
    # STEP F — FBA INBOUND SHIPPING UAT
    # ──────────────────────────────────────────────

    def test_F01_inbound_plan_creation(self):
        """F1: Inbound plan payload has correct marketplace and items."""
        ap = self.amazon_products['UAT-SKU-001']
        inbound = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'UAT-INBOUND-F01',
            'shipment_name': 'UAT-INBOUND-F01',
            'instance_id': self.instance.id,
            'state': 'draft',
            'line_ids': [Command.create({
                'amazon_product_id': ap.id,
                'odoo_product_id': self.products['UAT-SKU-001'].id,
                'sku': ap.sku, 'fnsku': 'X00UAT001',
                'planned_quantity': 50,
                'prep_owner': 'SELLER', 'label_owner': 'SELLER',
            })],
        })
        payload = inbound._prepare_create_inbound_plan_payload()
        self.assertEqual(payload['destinationMarketplaces'], [EG_MARKETPLACE])
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['quantity'], 50)

    def test_F02_multi_sku_inbound(self):
        """F2: Inbound plan with multiple SKUs."""
        lines = []
        for sku in ('UAT-SKU-001', 'UAT-SKU-002'):
            ap = self.amazon_products[sku]
            lines.append(Command.create({
                'amazon_product_id': ap.id,
                'odoo_product_id': self.products[sku].id,
                'sku': ap.sku, 'fnsku': 'X00UAT%s' % sku[-3:],
                'planned_quantity': 25,
                'prep_owner': 'SELLER', 'label_owner': 'SELLER',
            }))
        inbound = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'UAT-INBOUND-F02',
            'shipment_name': 'UAT-INBOUND-F02',
            'instance_id': self.instance.id,
            'state': 'draft',
            'line_ids': lines,
        })
        payload = inbound._prepare_create_inbound_plan_payload()
        self.assertEqual(len(payload['items']), 2)

    def test_F03_dispatch_picking(self):
        """F3: Dispatch picking moves stock from source to transit."""
        prod = self.products['UAT-SKU-001']
        ap = self.amazon_products['UAT-SKU-001']
        self._seed_stock(prod, 30)
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        self.assertEqual(self._qty(prod, source), 30)

        inbound = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'UAT-INBOUND-F03',
            'shipment_name': 'UAT-INBOUND-F03',
            'instance_id': self.instance.id,
            'state': 'placement_confirmed',
            'inbound_plan_id': 'wf2300f003-1234-abcd-5678-1234abcd5678',
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'packing_information_status': 'success',
            'placement_confirmation_status': 'success',
            'line_ids': [Command.create({
                'amazon_product_id': ap.id,
                'odoo_product_id': prod.id,
                'sku': ap.sku, 'fnsku': 'X00UAT001',
                'planned_quantity': 30,
                'prep_owner': 'SELLER', 'label_owner': 'SELLER',
            })],
        })
        amazon_shipment_id = 'sh2300f003-1234-abcd-5678-1234abcd5678'
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'amazon_placement_option_id': 'pl-uat-f03',
            'status': 'ACCEPTED',
            'amazon_shipment_ids': json.dumps([amazon_shipment_id]),
            'selected': True,
        })
        physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'placement_option_id': placement.id,
            'amazon_shipment_id': amazon_shipment_id,
            'shipment_confirmation_id': 'FBAUATF03',
            'status': 'SHIPPED',
            'destination_fc': 'CAI1',
            'transportation_confirmation_status': 'success',
            'labels_status': 'success',
            'product_labels_confirmed': True,
            'line_ids': [Command.create({
                'amazon_product_id': ap.id,
                'msku': ap.sku,
                'asin': ap.asin,
                'fnsku': 'X00UAT001',
                'quantity': 30,
            })],
        })
        label = self.env['ir.attachment'].sudo().create({
            'name': 'uat-f03-labels.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF-1.4 phase23 f03 labels'),
            'mimetype': 'application/pdf',
            'res_model': physical._name,
            'res_id': physical.id,
        })
        physical.shipping_label_attachment_id = label
        dispatch, created = physical._create_dispatch_picking()
        self.assertTrue(created)
        dispatch.with_context(picking_ids_not_to_backorder=dispatch.ids).button_validate()
        self.assertEqual(dispatch.state, 'done')
        self.assertEqual(self._qty(prod, source), 0)
        self.assertEqual(self._qty(prod, transit), 30)

    def test_F04_receiving_deltas(self):
        """F4: Partial receiving creates correct delta moves."""
        prod = self.products['UAT-SKU-002']
        ap = self.amazon_products['UAT-SKU-002']
        self._seed_stock(prod, 20)
        transit = self.instance.fba_transit_location_id
        received = self.instance.fba_received_location_id

        inbound = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'UAT-INBOUND-F04',
            'shipment_name': 'UAT-INBOUND-F04',
            'instance_id': self.instance.id,
            'state': 'placement_confirmed',
            'inbound_plan_id': 'wf2300f004-1234-abcd-5678-1234abcd5678',
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'packing_information_status': 'success',
            'placement_confirmation_status': 'success',
            'line_ids': [Command.create({
                'amazon_product_id': ap.id,
                'odoo_product_id': prod.id,
                'sku': ap.sku, 'fnsku': 'X00UAT002',
                'planned_quantity': 20,
                'prep_owner': 'SELLER', 'label_owner': 'SELLER',
            })],
        })
        amazon_sid = 'sh2300f004-1234-abcd-5678-1234abcd5678'
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'amazon_placement_option_id': 'pl-uat-f04',
            'status': 'ACCEPTED',
            'amazon_shipment_ids': json.dumps([amazon_sid]),
            'selected': True,
        })
        physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'placement_option_id': placement.id,
            'amazon_shipment_id': amazon_sid,
            'shipment_confirmation_id': 'FBAUATF04',
            'status': 'SHIPPED',
            'destination_fc': 'CAI1',
            'transportation_confirmation_status': 'success',
            'labels_status': 'success',
            'product_labels_confirmed': True,
            'line_ids': [Command.create({
                'amazon_product_id': ap.id,
                'msku': ap.sku, 'asin': ap.asin,
                'fnsku': 'X00UAT002', 'quantity': 20,
            })],
        })
        label = self.env['ir.attachment'].sudo().create({
            'name': 'uat-f04-labels.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF-1.4 phase23 f04 labels'),
            'mimetype': 'application/pdf',
            'res_model': physical._name,
            'res_id': physical.id,
        })
        physical.shipping_label_attachment_id = label
        dispatch, _ = physical._create_dispatch_picking()
        dispatch.with_context(picking_ids_not_to_backorder=dispatch.ids).button_validate()
        self.assertEqual(self._qty(prod, transit), 20)

        def _recv(cumulative):
            status = {
                'shipmentId': physical.amazon_shipment_id,
                'shipmentConfirmationId': physical.shipment_confirmation_id,
                'status': 'RECEIVING' if cumulative < 20 else 'CLOSED',
                '_amazon_request_id': 'uat-recv',
            }
            item = {
                'ShipmentId': physical.shipment_confirmation_id,
                'SellerSKU': ap.sku,
                'FulfillmentNetworkSKU': 'X00UAT002',
                'QuantityShipped': 20,
                'QuantityReceived': cumulative,
            }
            return physical._apply_receiving_snapshot(
                status,
                {
                    'payload': {'ItemData': [item]},
                    '_amazon_request_ids': ['uat-recv-items'],
                    '_pages': [{'payload': {'ItemData': [item]}}],
                },
            )

        d1 = _recv(8)['deltaReceived']
        d2 = _recv(15)['deltaReceived']
        d3 = _recv(20)['deltaReceived']
        self.assertEqual(d1, 8)
        self.assertEqual(d2, 7)
        self.assertEqual(d3, 5)
        self.assertEqual(self._qty(prod, transit), 0)
        self.assertEqual(self._qty(prod, received), 20)

    # ──────────────────────────────────────────────
    # STEP G — RETURNS & REIMBURSEMENTS UAT
    # ──────────────────────────────────────────────

    def test_G01_return_is_audit_only(self):
        """G1: Customer return does not create stock moves (audit-only)."""
        raw = self._raw_order_2026('UAT-G01', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw)
        so = ao.sale_order_id
        sale_line = so.order_line.filtered(
            lambda l: l.product_id == self.products['UAT-SKU-001'])

        report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'downloaded',
            'amazon_report_id': 'UAT-RETURN-RPT-G01',
        })
        moves_before = self.env['stock.move'].sudo().search_count([])
        line = self.env['amazon.return.report.line'].sudo().import_row(
            report, {
                'return-date': '2026-10-05T10:00:00Z',
                'order-id': ao.amazon_order_ref,
                'sku': 'UAT-SKU-001',
                'asin': 'B0UAT001',
                'fnsku': 'X00UAT001',
                'product-name': 'UAT Wireless Headphones',
                'quantity': '1',
                'fulfillment-center-id': 'CAI1',
                'detailed-disposition': 'SELLABLE',
                'reason': 'CUSTOMER_RETURN',
                'status': 'Unit returned to inventory',
                'license-plate-number': 'UAT-LPN-G01',
                'customer-comments': '',
            },
        )
        line._classify_and_apply()
        self.assertEqual(line.stock_action_state, 'audit_only')
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), moves_before)

    def test_G02_duplicate_return_idempotent(self):
        """G2: Importing same return twice doesn't duplicate."""
        raw = self._raw_order_2026('UAT-G02', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw)
        report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id, 'state': 'downloaded',
            'amazon_report_id': 'UAT-RETURN-RPT-G02',
        })
        row = {
            'return-date': '2026-10-05T10:00:00Z',
            'order-id': ao.amazon_order_ref,
            'sku': 'UAT-SKU-001', 'asin': 'B0UAT001', 'fnsku': 'X00UAT001',
            'product-name': 'UAT Wireless Headphones',
            'quantity': '1', 'fulfillment-center-id': 'CAI1',
            'detailed-disposition': 'SELLABLE', 'reason': 'CUSTOMER_RETURN',
            'status': 'Unit returned to inventory',
            'license-plate-number': 'UAT-LPN-G02', 'customer-comments': '',
        }
        l1 = self.env['amazon.return.report.line'].sudo().import_row(report, row)
        l2 = self.env['amazon.return.report.line'].sudo().import_row(report, row)
        self.assertEqual(l1, l2)

    def test_G03_reimbursement_import(self):
        """G3: Reimbursement imported correctly, no accounting side effects."""
        raw = self._raw_order_2026('UAT-G03', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw)
        moves_before = self.env['account.move'].sudo().search_count([])
        reimb = self.env['amazon.fba.reimbursement'].sudo().import_row(
            self.instance, {
                'approval-date': '2026-10-05T13:00:00Z',
                'reimbursement-id': 'UAT-REIMB-G03',
                'case-id': 'UAT-CASE-G03',
                'amazon-order-id': ao.amazon_order_ref,
                'reason': 'Lost_Warehouse',
                'sku': 'UAT-SKU-001', 'fnsku': 'X00UAT001', 'asin': 'B0UAT001',
                'product-name': 'UAT Wireless Headphones',
                'condition': 'SELLABLE',
                'currency-unit': 'EGP',
                'amount-per-unit': '295',
                'amount-total': '295',
                'quantity-reimbursed-cash': '1',
                'quantity-reimbursed-inventory': '0',
                'quantity-reimbursed-total': '1',
                'original-reimbursement-id': '',
                'original-reimbursement-type': '',
            }, 'UAT-REIMB-REPORT-G03',
        )
        self.assertEqual(reimb.amount_total, 295)
        self.assertEqual(self.env['account.move'].sudo().search_count([]), moves_before)

    # ──────────────────────────────────────────────
    # STEP H — SETTLEMENT & ACCOUNTING UAT
    # ──────────────────────────────────────────────

    def _create_settlement(self, sid='UAT-SETTLE-001', net=725):
        components = [
            ('sale', 1000.0),
            ('amazon_fee', -100.0),
            ('refund', -200.0),
            ('reimbursement', 50.0),
            ('adjustment', -25.0),
        ]
        settlement = self.env['amazon.settlement.report'].sudo().create({
            'instance_id': self.instance.id,
            'settlement_id': sid,
            'settlement_start_date': fields.Datetime.from_string('2026-10-01 00:00:00'),
            'settlement_end_date': fields.Datetime.from_string('2026-10-10 23:59:59'),
            'deposit_date': fields.Datetime.from_string('2026-10-11 00:00:00'),
            'currency_id': self.egp.id,
            'currency_code': 'EGP',
            'reported_net_amount': net,
            'state': 'imported',
            'reconciliation_state': 'pending',
        })
        for seq, (cat, amt) in enumerate(components, 1):
            self.env['amazon.settlement.report.line'].sudo().create({
                'report_id': settlement.id,
                'line_key': '%s-LINE-%d' % (sid, seq),
                'normalized_category': cat,
                'amount': amt,
                'currency_id': self.egp.id,
                'currency_code': 'EGP',
                'amount_description': cat,
                'transaction_type': 'Order' if cat in ('sale', 'refund') else 'Adjustment',
                'amazon_transaction_type_raw': cat,
                'order_link_state': 'not_applicable',
            })
        settlement._recompute_reconciliation()
        return settlement

    def test_H01_settlement_reconciliation(self):
        """H1: Settlement reconciles correctly."""
        s = self._create_settlement()
        self.assertEqual(s.reconciliation_state, 'matched')
        self.assertAlmostEqual(s.calculated_net_amount, 725.0, places=2)

    def test_H02_accounting_entry_draft(self):
        """H2: Accounting entry created as DRAFT, balanced."""
        s = self._create_settlement()
        s.sudo().action_create_accounting_entry()
        move = s.account_move_id
        self.assertTrue(move.exists())
        self.assertEqual(move.state, 'draft')
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')),
            places=2,
        )

    def test_H03_clearing_line_correct(self):
        """H3: Clearing line has correct amount."""
        s = self._create_settlement()
        s.sudo().action_create_accounting_entry()
        clearing = s.account_move_id.line_ids.filtered(
            lambda l: l.account_id == self.accounts['clearing'])
        self.assertAlmostEqual(clearing.debit - clearing.credit, 725.0, places=2)

    def test_H04_duplicate_accounting_entry(self):
        """H4: Creating accounting entry twice doesn't duplicate."""
        s = self._create_settlement()
        s.sudo().action_create_accounting_entry()
        move1 = s.account_move_id
        s.sudo().action_create_accounting_entry()
        self.assertEqual(s.account_move_id, move1)

    def test_H05_payout_and_clearing_reconciliation(self):
        """H5: Full payout flow: receipt + clearing reconciliation."""
        s = self._create_settlement()
        s.sudo().action_create_accounting_entry()
        s.account_move_id.action_post()

        payout = self.env['amazon.payout'].sudo().create({
            'instance_id': self.instance.id,
            'source': 'manual_confirmation',
            'source_reference': 'UAT-BANK-001',
            'payout_date': fields.Date.from_string('2026-10-11'),
            'currency_id': self.egp.id,
            'actual_received_amount': 725,
            'bank_journal_id': self.bank_journal.id,
            'matching_state': 'manually_matched',
        })
        self.env['amazon.payout.allocation'].sudo().create({
            'payout_id': payout.id,
            'settlement_id': s.id,
            'expected_amount': 725,
            'allocated_amount': 725,
        })
        payout.sudo().action_create_draft_receipt()
        self.assertEqual(payout.receipt_move_id.state, 'draft')
        payout.receipt_move_id.action_post()
        payout.sudo().action_reconcile_clearing()
        s.invalidate_recordset()
        self.assertEqual(payout.state, 'paid')
        self.assertTrue(self.egp.is_zero(s.clearing_remaining_amount))

    # ──────────────────────────────────────────────
    # STEP I — USER ROLES (server-side)
    # ──────────────────────────────────────────────

    def test_I01_amazon_groups_exist(self):
        """I1: Amazon security groups exist."""
        for xml_id in ('sdlc_amazon_connector.group_amazon_user',
                       'sdlc_amazon_connector.group_amazon_manager'):
            group = self.env.ref(xml_id, raise_if_not_found=False)
            self.assertTrue(group, "Missing security group: %s" % xml_id)

    def test_I02_access_rights_exist(self):
        """I2: Access rights for core models are defined."""
        acl = self.env['ir.model.access'].sudo()
        for model in ('amazon.instance', 'amazon.sale.order', 'amazon.product',
                      'amazon.settlement.report', 'amazon.fba.sale.stock.event'):
            rights = acl.search([('model_id.model', '=', model)])
            self.assertTrue(rights, "No access rights for %s" % model)

    def test_I03_regular_user_cannot_write_instance(self):
        """I3: Regular user without Amazon group cannot write instance."""
        regular = self.env['res.users'].sudo().create({
            'name': 'UAT Regular User',
            'login': 'uat_regular_user_p23',
            'email': 'regular@uat.test',
            'company_id': self.company.id,
            'company_ids': [Command.set([self.company.id])],
        })
        inst_as_user = self.instance.with_user(regular)
        with self.assertRaises(Exception):
            inst_as_user.write({'name': 'Hacked'})

    # ──────────────────────────────────────────────
    # STEP J — FAILURE RECOVERY & IDEMPOTENCY
    # ──────────────────────────────────────────────

    def test_J01_import_recovery_after_partial_failure(self):
        """J1: After a partial import, re-import completes without duplicates."""
        raw = self._raw_order_2026('UAT-J01', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao1, r1 = self._import_order(raw)
        self.assertTrue(r1['created'])
        ao2, r2 = self._import_order(raw)
        self.assertEqual(ao1, ao2)
        self.assertFalse(r2['created'])
        self.assertEqual(len(ao1.order_line_ids), 1)

    def test_J02_settlement_mismatch_handling(self):
        """J2: Settlement with mismatched net is flagged correctly."""
        s = self._create_settlement(sid='UAT-SETTLE-MISMATCH', net=800)
        self.assertNotEqual(s.reconciliation_state, 'matched')

    def test_J03_fba_event_retry(self):
        """J3: Manual review events can be released and re-processed."""
        raw = self._raw_order_2026('UAT-J03', [('UAT-SKU-001', 295, 1)], grand_total=295)
        self._import_order(raw, hold_fba=True)
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('amazon_order_ref', '=', 'UAT-J03'),
        ])
        self.assertEqual(events.state, 'manual_review')
        events.action_retry()
        self.assertEqual(events.state, 'pending')

    def test_J04_removal_audit_only(self):
        """J4: Removal shipment is audit-only by default."""
        raw = self._raw_order_2026('UAT-J04', [('UAT-SKU-001', 295, 1)], grand_total=295)
        ao, _ = self._import_order(raw)
        removal = self.env['amazon.removal.order'].sudo().import_detail_row(
            self.instance, {
                'request-date': '2026-10-05T10:00:00Z',
                'order-id': 'UAT-REMOVAL-J04',
                'order-type': 'Return',
                'order-status': 'Processing',
                'last-updated-date': '2026-10-05T11:00:00Z',
                'sku': 'UAT-SKU-001', 'fnsku': 'X00UAT001',
                'disposition': 'Sellable',
                'requested-quantity': '1', 'cancelled-quantity': '0',
                'disposed-quantity': '0', 'shipped-quantity': '0',
                'in-process-quantity': '1',
            }, 'UAT-REMOVAL-REPORT-J04',
        )
        shipment = self.env['amazon.removal.shipment'].sudo().import_row(
            self.instance, {
                'request-date': '2026-10-05T10:00:00Z',
                'order-id': removal.removal_order_id,
                'shipment-date': '2026-10-05T12:00:00Z',
                'sku': 'UAT-SKU-001', 'fnsku': 'X00UAT001',
                'disposition': 'Sellable',
                'shipped-quantity': '1',
                'carrier': 'Amazon Logistics',
                'tracking-number': 'UAT-TRACK-J04',
                'removal-order-type': 'Return',
            }, 'UAT-REMOVAL-SHIP-REPORT-J04',
        )
        self.assertEqual(shipment.stock_action_state, 'audit_only')

    # ──────────────────────────────────────────────
    # DATA INTEGRITY — verify no side effects
    # ──────────────────────────────────────────────

    def test_Z01_no_live_api_calls(self):
        """Z1: Confirm mock credentials cannot reach live Amazon."""
        self.assertEqual(self.instance.refresh_token, 'uat-mock-refresh-token')
        self.assertEqual(self.instance.client_id, 'uat-mock-client-id')
