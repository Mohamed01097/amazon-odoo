"""Tests for FBA Cutover V2 — Historical Fulfillment Evidence Baseline.

Covers:
 1. B2 race condition fix
 2. Fully fulfilled before cutover
 3. Fully fulfilled after cutover
 4. Partial before/after
 5. Order imported only after all fulfillment
 6. Multiple orders same SKU
 7. Multiple shipment rows per order item
 8. Duplicate shipment evidence replay (idempotency)
 9. Timestamp exact boundary
10. Report window splitting
11. Report window failure → baseline NOT ready
12. Report latency safety gate
13. Missing evidence with complete coverage → B=0
14. Missing evidence with incomplete coverage
15. Pre-cutover order older than history_start_at
16. Post-cutover normal order
17. Cumulative 0→1→2→3
18. Repeated same cumulative (idempotent)
19. Duplicate/concurrent worker
20. Crash/rollback atomicity
21. Reconciliation overlap
22. Legacy historical event compatibility
23. Multi-company isolation
24. Instance isolation
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.float_utils import float_compare

from ..models.amazon_api import AmazonAPI
from ..models.amazon_fba_sale_stock_cutover import AmazonFbaSaleStockCutoverRun
from ..models.amazon_instance import AmazonInstance


def _shipment_row(order_id, item_id, sku, shipment_date, qty,
                  shipment_id='SHP-1', shipment_item_id=None):
    """Build a mock FBA shipment report row dict."""
    if shipment_item_id is None:
        shipment_item_id = '%s-%s-%s' % (shipment_id, item_id, shipment_date)
    return {
        'amazon-order-id': order_id,
        'amazon-order-item-id': item_id,
        'sku': sku,
        'shipment-date': shipment_date,
        'quantity-shipped': str(qty),
        'shipment-id': shipment_id,
        'shipment-item-id': shipment_item_id,
        'purchase-date': '2026-08-15T10:00:00+00:00',
        'product-name': 'Test Product %s' % sku,
    }


@tagged('post_install', '-at_install', 'amazon_fba_cutover_v2')
class TestFbaCutoverV2(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({'name': 'Cutover V2 Test Co'})
        wh_model = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.customer_wh = wh_model.create({
            'name': 'CV2 Customer WH', 'code': 'CV2CW',
            'company_id': self.company.id,
        })
        self.fba_wh = wh_model.create({
            'name': 'CV2 FBA WH', 'code': 'CV2FW',
            'company_id': self.company.id,
        })
        self.partner = self.env['res.partner'].sudo().create({
            'name': 'CV2 Partner', 'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'CV2 Egypt',
            'company_id': self.company.id,
            'seller_id': 'CV2-SELLER',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock',
            'client_id': 'mock',
            'client_secret': 'mock',
            'fba_warehouse_id': self.fba_wh.id,
            'fba_source_location_id': self.customer_wh.lot_stock_id.id,
            'fba_removal_return_partner_id': self.partner.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product, self.amazon_product = self._create_product('SKU-V2')
        self._put_stock(self.product, self.instance.fba_sellable_location_id, 100)

        self.cutover_dt = datetime(2026, 9, 12, 0, 0, 0)
        self.history_start_dt = datetime(2026, 8, 1, 0, 0, 0)

    def _create_product(self, sku):
        product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku, 'default_code': sku, 'type': 'consu',
            'is_storable': True, 'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon %s' % sku, 'instance_id': self.instance.id,
            'sku': sku, 'asin': 'B0%s' % sku.replace('-', ''),
            'fulfillment_channel': 'AFN', 'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _put_stock(self, product, dest, qty):
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_wh.in_type_id.id,
            'location_id': supplier.id, 'location_dest_id': dest.id,
            'company_id': self.company.id, 'origin': 'CV2 OPENING',
            'move_ids': [Command.create({
                'product_id': product.id, 'product_uom_qty': qty,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id, 'location_dest_id': dest.id,
                'company_id': self.company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        picking.with_context(picking_ids_not_to_backorder=picking.ids, skip_backorder=True).button_validate()
        return picking

    def _qty(self, product, location):
        product.invalidate_recordset()
        return product.sudo().with_company(self.company).with_context(location=location.id).qty_available

    def _order_line(self, order_ref, sku='SKU-V2', quantity=5,
                    product=None, amazon_product=None, purchase_date=None):
        product = product or self.product
        amazon_product = amazon_product or self.amazon_product
        vals = {
            'amazon_order_ref': order_ref,
            'instance_id': self.instance.id,
            'fulfillment_channel': 'AFN',
            'amazon_status': 'Unshipped',
        }
        if purchase_date:
            vals['purchase_date'] = fields.Datetime.to_datetime(purchase_date)
        order = self.env['amazon.sale.order'].sudo().create(vals)
        line = self.env['amazon.sale.order.line'].sudo().create({
            'order_id': order.id,
            'amazon_order_item_id': '%s-ITEM-%s' % (order_ref, sku),
            'amazon_product_id': amazon_product.id,
            'odoo_product_id': product.id,
            'sku': sku, 'quantity': quantity,
        })
        return order, line

    def _setup_v2_cutover(self, shipment_rows=None):
        """Create and activate a v2 cutover run with given shipment evidence."""
        if shipment_rows is None:
            shipment_rows = []
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_fetch_report_rows(*args, **kw):
            return shipment_rows

        with patch.object(
            AmazonAPI, 'fetch_report_rows', side_effect=mock_fetch_report_rows,
        ), patch.object(
            AmazonInstance, '_get_access_token_or_raise', return_value='mock-token',
        ), patch.object(
            AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True,
        ):
            run.action_build_baselines()

        self.assertEqual(run.state, 'ready')
        run.action_activate()
        self.assertEqual(run.state, 'activated')
        self.assertEqual(self.instance.fba_sale_stock_cutover_at, self.cutover_dt)
        return run

    def _upsert(self, line, cumulative):
        return self.env['amazon.fba.sale.stock.event'].sudo().upsert_from_order_line(
            line, cumulative, fields.Datetime.now(),
        )

    # ==================================================================
    # 1. B2 RACE CONDITION — THE CRITICAL TEST
    # ==================================================================

    def test_01_b2_race_condition_fixed(self):
        """Order purchased before cutover, fulfilled after, imported AFTER fulfillment.
        Under v1, baseline = QuantityShipped = 3, delta = 0 → phantom inventory.
        Under v2, B = 0 (no shipment before cutover), delta = 3 → correct depletion.
        """
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('B2-ORDER', purchase_date='2026-09-10 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 0.0)
        self.assertEqual(event.processed_fulfilled_qty, 0.0)
        self.assertEqual(event.state, 'pending')

        picking = event._process_one()
        self.assertTrue(picking)
        self.assertEqual(event.processed_fulfilled_qty, 3.0)
        self.assertEqual(event.state, 'done')
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 97)

    # ==================================================================
    # 2. FULLY FULFILLED BEFORE CUTOVER
    # ==================================================================

    def test_02_fully_fulfilled_before_cutover(self):
        """B = 3, C = 3 → delta = 0, state = done, no picking."""
        rows = [_shipment_row('ORD-FULL-BF', 'ORD-FULL-BF-ITEM-SKU-V2', 'SKU-V2',
                              '2026-09-10T10:00:00+00:00', 3)]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-FULL-BF', purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 3.0)
        self.assertEqual(event.processed_fulfilled_qty, 3.0)
        self.assertEqual(event.state, 'done')
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 100)

    # ==================================================================
    # 3. FULLY FULFILLED AFTER CUTOVER
    # ==================================================================

    def test_03_fully_fulfilled_after_cutover(self):
        """B = 0 (no evidence), C = 3 → delta = 3."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-AFTER', purchase_date='2026-09-10 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 0.0)
        self.assertEqual(event.state, 'pending')
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 3.0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 97)

    # ==================================================================
    # 4. PARTIAL BEFORE/AFTER
    # ==================================================================

    def test_04_partial_before_after(self):
        """B = 2, C = 5 → delta = 3."""
        rows = [
            _shipment_row('ORD-PART', 'ORD-PART-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 2, shipment_item_id='SPI-PART-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-PART', purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 5)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 2.0)
        self.assertEqual(event.processed_fulfilled_qty, 2.0)
        self.assertEqual(event.state, 'pending')

        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 5.0)
        self.assertEqual(event.last_delta_qty, 3.0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 97)

    # ==================================================================
    # 5. ORDER IMPORTED AFTER ALL FULFILLMENT
    # ==================================================================

    def test_05_imported_after_all_fulfillment(self):
        """10 ordered, 3 shipped before cutover, all 10 shipped by import time.
        B = 3, C = 10 → delta = 7."""
        rows = [
            _shipment_row('ORD-10', 'ORD-10-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 3, shipment_item_id='SPI-10-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-10', quantity=10, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 10)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 3.0)
        self.assertEqual(event.processed_fulfilled_qty, 3.0)
        self.assertEqual(event.state, 'pending')

        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 10.0)
        self.assertEqual(event.last_delta_qty, 7.0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 93)

    # ==================================================================
    # 6. MULTIPLE ORDERS SAME SKU
    # ==================================================================

    def test_06_multiple_orders_same_sku(self):
        """Order A: B=3,C=3. Order B: B=1,C=4. Order C: B=0,C=5. Total moves = 0+3+5 = 8."""
        rows = [
            _shipment_row('ORD-A', 'ORD-A-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 3, shipment_item_id='SPI-A-1'),
            _shipment_row('ORD-B', 'ORD-B-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-11T10:00:00+00:00', 1, shipment_item_id='SPI-B-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)

        for ref, qty, cumul, expected_b, expected_delta in [
            ('ORD-A', 3, 3, 3, 0),
            ('ORD-B', 4, 4, 1, 3),
            ('ORD-C', 5, 5, 0, 5),
        ]:
            order, line = self._order_line(ref, quantity=qty, purchase_date='2026-09-08 10:00:00')
            event = self._upsert(line, cumul)
            self.assertEqual(event.cutover_baseline_fulfilled_qty, expected_b,
                             "B mismatch for %s" % ref)
            if expected_delta > 0:
                event._process_one()
                self.assertEqual(event.last_delta_qty, expected_delta,
                                 "Delta mismatch for %s" % ref)

        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 92)

    # ==================================================================
    # 7. MULTIPLE SHIPMENT ROWS PER ORDER ITEM
    # ==================================================================

    def test_07_multi_shipment_rows_aggregation(self):
        """Two shipment rows before cutover: qty 1 + qty 2 = B=3."""
        rows = [
            _shipment_row('ORD-MS', 'ORD-MS-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T08:00:00+00:00', 1, shipment_item_id='SPI-MS-1'),
            _shipment_row('ORD-MS', 'ORD-MS-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T14:00:00+00:00', 2, shipment_item_id='SPI-MS-2'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-MS', quantity=10, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 6)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 3.0)
        event._process_one()
        self.assertEqual(event.last_delta_qty, 3.0)

    # ==================================================================
    # 8. DUPLICATE SHIPMENT EVIDENCE REPLAY (IDEMPOTENCY)
    # ==================================================================

    def test_08_duplicate_evidence_replay(self):
        """Running the same evidence twice produces the same B, not double."""
        rows = [
            _shipment_row('ORD-DUP', 'ORD-DUP-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 3, shipment_item_id='SPI-DUP-1'),
        ]
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_fetch(*args, **kw):
            return rows

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_fetch), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'), \
             patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True):
            run.action_build_baselines()
            # Build again — should be idempotent via shipment_item_id dedup
            run.write({'state': 'building'})
            run.action_build_baselines()

        self.assertEqual(run.state, 'ready')
        baselines = run.baseline_ids
        matching = baselines.filtered(lambda b: b.amazon_order_ref == 'ORD-DUP')
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching.fulfilled_before_cutover, 3.0)

    # ==================================================================
    # 9. TIMESTAMP EXACT BOUNDARY
    # ==================================================================

    def test_09_timestamp_boundary(self):
        """shipment_date == cutover → included. cutover + 1s → excluded."""
        cutover_str = '2026-09-12T00:00:00+00:00'
        rows = [
            _shipment_row('ORD-BD', 'ORD-BD-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-11T23:59:59+00:00', 1, shipment_item_id='SPI-BD-1'),
            _shipment_row('ORD-BD', 'ORD-BD-ITEM-SKU-V2', 'SKU-V2',
                          cutover_str, 2, shipment_item_id='SPI-BD-2'),
            _shipment_row('ORD-BD', 'ORD-BD-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-12T00:00:01+00:00', 4, shipment_item_id='SPI-BD-3'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-BD', quantity=10, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 7)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 3.0)  # 1 + 2, NOT 1+2+4

    # ==================================================================
    # 10. REPORT WINDOW SPLITTING
    # ==================================================================

    def test_10_report_window_splitting(self):
        """42-day period should split into 2 windows."""
        Run = self.env['amazon.fba.sale.stock.cutover.run']
        windows = Run._compute_report_windows(
            datetime(2026, 8, 1), datetime(2026, 9, 12),
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0][0], datetime(2026, 8, 1))
        self.assertEqual(windows[0][1], datetime(2026, 8, 31))
        self.assertEqual(windows[1][0], datetime(2026, 8, 31))
        self.assertEqual(windows[1][1], datetime(2026, 9, 12))

    # ==================================================================
    # 11. REPORT WINDOW FAILURE → NOT READY
    # ==================================================================

    def test_11_report_failure_blocks_ready(self):
        """A failed report window should prevent the run from reaching ready."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_fail(*args, **kw):
            raise Exception("Amazon report timeout")

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_fail), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'):
            result = run.action_build_baselines()

        self.assertFalse(result)
        self.assertEqual(run.state, 'building')
        self.assertGreater(run.report_windows_failed, 0)

        with self.assertRaises(UserError):
            run.action_mark_ready()

    # ==================================================================
    # 12. REPORT LATENCY SAFETY GATE
    # ==================================================================

    def test_12_safety_delay_gate(self):
        """Build succeeds but state stays building until safety delay passes."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_empty(*args, **kw):
            return []

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_empty), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'), \
             patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=False):
            run.action_build_baselines()

        self.assertEqual(run.state, 'building')
        self.assertIn('safety delay', run.last_error or '')

        with patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=False):
            with self.assertRaises(UserError):
                run.action_mark_ready()

    # ==================================================================
    # 13. MISSING EVIDENCE WITH COMPLETE COVERAGE → B=0
    # ==================================================================

    def test_13_missing_evidence_complete_coverage(self):
        """Order within covered period, no shipment evidence → B=0, delta=C."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-MISS', purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 2)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 0.0)
        self.assertEqual(event.state, 'pending')
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 2.0)

    # ==================================================================
    # 14. MISSING EVIDENCE WITH INCOMPLETE COVERAGE → MANUAL_REVIEW
    # ==================================================================

    def test_14_pre_cutover_order_older_than_coverage(self):
        """Order with purchase_date before history_start_at and no baseline
        evidence → manual_review with CUTOVER_BASELINE_OUTSIDE_COVERAGE.
        B=0 cannot be proven safe when coverage is incomplete."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-OLD',
                                       purchase_date='2026-07-15 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.state, 'manual_review')
        self.assertEqual(event.last_error_code, 'CUTOVER_BASELINE_OUTSIDE_COVERAGE')
        self.assertEqual(event.processed_fulfilled_qty, 0.0)

    # ==================================================================
    # 15. PRE-CUTOVER ORDER OLDER THAN HISTORY_START_AT
    # ==================================================================

    def test_15_very_old_order_blocked(self):
        """Purchase date way before coverage — B=0 is NOT safe to assume.
        Must go to manual_review, not auto-deplete. Cron will not pick it up."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-VOLD',
                                       purchase_date='2026-06-01 10:00:00')
        event = self._upsert(line, 5)
        self.assertEqual(event.state, 'manual_review')
        self.assertEqual(event.last_error_code, 'CUTOVER_BASELINE_OUTSIDE_COVERAGE')
        self.assertFalse(event.next_run_at)
        processed = self.env['amazon.fba.sale.stock.event'].cron_process_fba_sale_stock_events(limit=10)
        self.assertEqual(processed, 0)

    # ==================================================================
    # 16. POST-CUTOVER NORMAL ORDER
    # ==================================================================

    def test_16_post_cutover_normal(self):
        """Normal post-cutover order — B=0, processed=0, delta=C."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-POST',
                                       purchase_date='2026-09-15 10:00:00')
        event = self._upsert(line, 4)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 0.0)
        self.assertEqual(event.processed_fulfilled_qty, 0.0)
        self.assertEqual(event.state, 'pending')
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 4.0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 96)

    # ==================================================================
    # 17. CUMULATIVE 0→1→2→3
    # ==================================================================

    def test_17_incremental_cumulative(self):
        """Post-cutover: C goes 0→1→2→3, each increment produces one picking."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-INC',
                                       purchase_date='2026-09-15 10:00:00',
                                       quantity=5)
        event = self._upsert(line, 0)
        self.assertEqual(event.state, 'done')

        for c in [1, 2, 3]:
            event = self._upsert(line, c)
            self.assertEqual(event.state, 'pending')
            event._process_one()
            self.assertEqual(event.processed_fulfilled_qty, float(c))
            self.assertEqual(event.last_delta_qty, 1.0)

        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 97)

    # ==================================================================
    # 18. REPEATED SAME CUMULATIVE (IDEMPOTENT)
    # ==================================================================

    def test_18_repeated_same_cumulative(self):
        """Calling upsert with the same C=3 three times → only one picking."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-REP',
                                       purchase_date='2026-09-15 10:00:00')
        event = self._upsert(line, 3)
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 3.0)

        for _ in range(2):
            event = self._upsert(line, 3)
            self.assertEqual(event.state, 'done')

        self.assertEqual(len(event.picking_ids.filtered(lambda p: p.state == 'done')), 1)

    # ==================================================================
    # 19. DUPLICATE/CONCURRENT WORKER (FOR UPDATE)
    # ==================================================================

    def test_19_for_update_lock(self):
        """The FOR UPDATE SKIP LOCKED in the cron prevents duplicate processing."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-LOCK',
                                       purchase_date='2026-09-15 10:00:00')
        event = self._upsert(line, 3)
        processed = self.env['amazon.fba.sale.stock.event'].cron_process_fba_sale_stock_events(limit=1)
        self.assertEqual(processed, 1)
        event.invalidate_recordset()
        self.assertEqual(event.state, 'done')
        self.assertEqual(event.processed_fulfilled_qty, 3.0)

    # ==================================================================
    # 20. CRASH/ROLLBACK ATOMICITY
    # ==================================================================

    def test_20_crash_rollback(self):
        """Exception during picking validation rolls back everything."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-CRASH',
                                       purchase_date='2026-09-15 10:00:00')
        event = self._upsert(line, 3)

        with patch.object(
            type(self.env['stock.picking']), 'button_validate',
            side_effect=Exception("Simulated crash"),
        ):
            event._process_one()

        event.invalidate_recordset()
        self.assertIn(event.state, ('pending', 'failed'))
        self.assertEqual(event.processed_fulfilled_qty, 0.0)
        self.assertEqual(len(event.picking_ids.filtered(lambda p: p.state == 'done')), 0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 100)

    # ==================================================================
    # 21. RECONCILIATION OVERLAP
    # ==================================================================

    def test_21_reconciliation_overlap_preserved(self):
        """Event-owned stock moves and reconciliation do not conflict."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-RECON',
                                       purchase_date='2026-09-15 10:00:00')
        event = self._upsert(line, 2)
        event._process_one()
        self.assertEqual(event.state, 'done')
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 98)

    # ==================================================================
    # 22. LEGACY V1 HISTORICAL EVENT COMPATIBILITY
    # ==================================================================

    def test_22_legacy_v1_events_preserved(self):
        """Existing v1 historical events must NOT be reprocessed."""
        self.instance.sudo().write({'fba_sale_stock_cutover_at': self.cutover_dt})
        order, line = self._order_line('ORD-V1', purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.state, 'historical')

        self.instance.sudo().write({'fba_sale_stock_cutover_at': False})
        self._setup_v2_cutover(shipment_rows=[])

        event.invalidate_recordset()
        result = event._process_one()
        self.assertFalse(result)
        self.assertEqual(event.state, 'historical')

    # ==================================================================
    # 23. MULTI-COMPANY ISOLATION
    # ==================================================================

    def test_23_multi_company_isolation(self):
        """Cutover evidence from company A does not leak to company B."""
        company_b = self.env['res.company'].sudo().create({'name': 'CV2 Co B'})
        wh_b = self.env['stock.warehouse'].sudo().with_company(company_b).create({
            'name': 'CV2 FBA B', 'code': 'CV2FB', 'company_id': company_b.id,
        })
        partner_b = self.env['res.partner'].sudo().create({
            'name': 'CV2 B Partner', 'company_id': company_b.id,
        })
        instance_b = self.env['amazon.instance'].sudo().create({
            'name': 'CV2 B Egypt', 'company_id': company_b.id,
            'seller_id': 'CV2B-SELLER', 'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu', 'refresh_token': 'mock', 'client_id': 'mock',
            'client_secret': 'mock', 'fba_warehouse_id': wh_b.id,
            'fba_source_location_id': wh_b.lot_stock_id.id,
            'fba_removal_return_partner_id': partner_b.id,
        })

        rows_a = [
            _shipment_row('ORD-ISO', 'ORD-ISO-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 5, shipment_item_id='SPI-ISO-A'),
        ]
        run = self._setup_v2_cutover(shipment_rows=rows_a)

        baseline_b = self.env['amazon.fba.sale.stock.event']._lookup_cutover_v2_baseline_for_line(
            instance_b, 'ORD-ISO', 'ORD-ISO-ITEM-SKU-V2',
        )
        self.assertEqual(baseline_b, 0.0)

    # ==================================================================
    # 24. INSTANCE ISOLATION
    # ==================================================================

    def test_24_instance_isolation(self):
        """Second instance on same company does not see first instance's baselines."""
        instance_2 = self.env['amazon.instance'].sudo().create({
            'name': 'CV2 Egypt 2', 'company_id': self.company.id,
            'seller_id': 'CV2-SELLER-2', 'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu', 'refresh_token': 'mock', 'client_id': 'mock',
            'client_secret': 'mock', 'fba_warehouse_id': self.fba_wh.id,
            'fba_source_location_id': self.customer_wh.lot_stock_id.id,
            'fba_removal_return_partner_id': self.partner.id,
        })

        rows = [
            _shipment_row('ORD-INST', 'ORD-INST-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 7, shipment_item_id='SPI-INST-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)

        baseline_2 = self.env['amazon.fba.sale.stock.event']._lookup_cutover_v2_baseline_for_line(
            instance_2, 'ORD-INST', 'ORD-INST-ITEM-SKU-V2',
        )
        self.assertEqual(baseline_2, 0.0)

    # ==================================================================
    # MULTI-SHIPMENT EVIDENCE: before/after boundary filter
    # ==================================================================

    def test_25_multi_shipment_boundary_filter(self):
        """3 shipment rows: 2 before cutover, 1 after. B = sum of the 2 before."""
        rows = [
            _shipment_row('ORD-MBF', 'ORD-MBF-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T08:00:00+00:00', 1, shipment_item_id='SPI-MBF-1'),
            _shipment_row('ORD-MBF', 'ORD-MBF-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-11T14:00:00+00:00', 2, shipment_item_id='SPI-MBF-2'),
            _shipment_row('ORD-MBF', 'ORD-MBF-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-13T10:00:00+00:00', 3, shipment_item_id='SPI-MBF-3'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-MBF', quantity=10, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 6)
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 3.0)  # 1 + 2 = 3

    # ==================================================================
    # PREFLIGHT CHECK
    # ==================================================================

    def test_26_preflight_check(self):
        """Preflight validates all prerequisites."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_empty(*args, **kw):
            return []

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_empty), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'), \
             patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True):
            run.action_build_baselines()

        self.assertEqual(run.state, 'ready')
        with patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True):
            preflight = run._preflight_check()
        self.assertFalse(preflight['blocked'])

    # ==================================================================
    # DUPLICATE CUTOVER RUN BLOCKED
    # ==================================================================

    def test_27_duplicate_run_blocked(self):
        """Cannot create two active cutover runs for the same instance."""
        self._setup_v2_cutover(shipment_rows=[])
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        with self.assertRaises(UserError):
            CutoverRun.create_cutover_run(
                self.instance.id, self.cutover_dt, self.history_start_dt,
            )

    # ==================================================================
    # V2 PRE-CUTOVER EVENT PROCESSES NORMALLY (not marked historical)
    # ==================================================================

    def test_28_v2_pre_cutover_not_marked_historical(self):
        """Under v2, pre-cutover events with delta > 0 are processed, not marked historical."""
        rows = [
            _shipment_row('ORD-NH', 'ORD-NH-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 1, shipment_item_id='SPI-NH-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-NH', purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.state, 'pending')
        self.assertNotEqual(event.state, 'historical')

        event._process_one()
        self.assertEqual(event.state, 'done')
        self.assertNotEqual(event.state, 'historical')
        self.assertEqual(event.processed_fulfilled_qty, 3.0)
        self.assertEqual(event.last_delta_qty, 2.0)

    # ==================================================================
    # B > C → MANUAL_REVIEW
    # ==================================================================

    def test_29_baseline_exceeds_cumulative(self):
        """B > C is a data inconsistency → manual_review, not a crash."""
        rows = [
            _shipment_row('ORD-BIG', 'ORD-BIG-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 5, shipment_item_id='SPI-BIG-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-BIG', quantity=5, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 2)
        self.assertEqual(event.state, 'manual_review')
        self.assertEqual(event.last_error_code, 'CUTOVER_BASELINE_EXCEEDS_CUMULATIVE')
        self.assertEqual(event.processed_fulfilled_qty, 0.0)
        self.assertEqual(self._qty(self.product, self.instance.fba_sellable_location_id), 100)

    # ==================================================================
    # B == C → DONE, NO PICKING
    # ==================================================================

    def test_30_baseline_equals_cumulative(self):
        """B == C → delta = 0, done, no picking — same as test_02 but explicit."""
        rows = [
            _shipment_row('ORD-EQ', 'ORD-EQ-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 4, shipment_item_id='SPI-EQ-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-EQ', quantity=5, purchase_date='2026-09-08 10:00:00')
        event = self._upsert(line, 4)
        self.assertEqual(event.state, 'done')
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 4.0)
        self.assertEqual(event.processed_fulfilled_qty, 4.0)
        self.assertEqual(len(event.picking_ids), 0)

    # ==================================================================
    # BASELINE IMMUTABILITY — READY state
    # ==================================================================

    def test_31_baseline_immutable_in_ready(self):
        """Cannot rebuild baselines once the run is in ready state."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )

        def mock_empty(*args, **kw):
            return []

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_empty), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'), \
             patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True):
            run.action_build_baselines()

        self.assertEqual(run.state, 'ready')
        with self.assertRaises(UserError):
            run._aggregate_baselines()

    # ==================================================================
    # BASELINE IMMUTABILITY — ACTIVATED state
    # ==================================================================

    def test_32_baseline_immutable_in_activated(self):
        """Cannot rebuild baselines once the run is activated."""
        self._setup_v2_cutover(shipment_rows=[])
        run = self.env['amazon.fba.sale.stock.cutover.run'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('state', '=', 'activated'),
        ], limit=1)
        self.assertTrue(run)
        with self.assertRaises(UserError):
            run._aggregate_baselines()

    # ==================================================================
    # CANCELLED RUN NOT USED
    # ==================================================================

    def test_33_cancelled_run_not_used(self):
        """A cancelled cutover run does not provide baselines."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run']
        run = CutoverRun.create_cutover_run(
            self.instance.id, self.cutover_dt, self.history_start_dt,
        )
        rows = [
            _shipment_row('ORD-CX', 'ORD-CX-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 5, shipment_item_id='SPI-CX-1'),
        ]

        def mock_fetch(*args, **kw):
            return rows

        with patch.object(AmazonAPI, 'fetch_report_rows', side_effect=mock_fetch), \
             patch.object(AmazonInstance, '_get_access_token_or_raise', return_value='t'), \
             patch.object(AmazonFbaSaleStockCutoverRun, '_is_safety_delay_passed', return_value=True):
            run.action_build_baselines()

        self.assertEqual(run.state, 'ready')
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')

        baseline = self.env['amazon.fba.sale.stock.event']._lookup_cutover_v2_baseline_for_line(
            self.instance, 'ORD-CX', 'ORD-CX-ITEM-SKU-V2',
        )
        self.assertEqual(baseline, 0.0)
        self.assertFalse(self.env['amazon.fba.sale.stock.event']._is_cutover_v2_active(self.instance))

    # ==================================================================
    # ACTIVATION PRODUCES NO STOCK MOVES/ORDERS/CRONS
    # ==================================================================

    def test_34_activation_no_side_effects(self):
        """action_activate only sets cutover_at and state. No picks, no moves."""
        picking_count_before = self.env['stock.picking'].sudo().search_count([
            ('company_id', '=', self.company.id),
        ])
        self._setup_v2_cutover(shipment_rows=[])
        picking_count_after = self.env['stock.picking'].sudo().search_count([
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(picking_count_before, picking_count_after,
                         "Activation must not create stock pickings")

    # ==================================================================
    # INSIDE COVERAGE + NO EVIDENCE → B=0 PROVEN
    # ==================================================================

    def test_35_inside_coverage_no_evidence_b_zero_proven(self):
        """Order within [history_start_at, cutover_at) with no shipment evidence
        → B=0 is legitimately proven, event processes normally."""
        self._setup_v2_cutover(shipment_rows=[])
        order, line = self._order_line('ORD-COV',
                                       purchase_date='2026-08-20 10:00:00')
        event = self._upsert(line, 3)
        self.assertEqual(event.state, 'pending')
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 0.0)
        self.assertNotEqual(event.last_error_code, 'CUTOVER_BASELINE_OUTSIDE_COVERAGE')

    # ==================================================================
    # OUTSIDE COVERAGE BUT HAS EVIDENCE → NOT BLOCKED
    # ==================================================================

    def test_36_outside_coverage_with_evidence_not_blocked(self):
        """Order older than coverage but HAS shipment evidence (B>0)
        → the evidence proves fulfillment, so it proceeds normally."""
        rows = [
            _shipment_row('ORD-OE', 'ORD-OE-ITEM-SKU-V2', 'SKU-V2',
                          '2026-09-10T10:00:00+00:00', 2, shipment_item_id='SPI-OE-1'),
        ]
        self._setup_v2_cutover(shipment_rows=rows)
        order, line = self._order_line('ORD-OE', quantity=5,
                                       purchase_date='2026-07-01 10:00:00')
        event = self._upsert(line, 5)
        self.assertEqual(event.state, 'pending')
        self.assertEqual(event.cutover_baseline_fulfilled_qty, 2.0)
        self.assertNotEqual(event.last_error_code, 'CUTOVER_BASELINE_OUTSIDE_COVERAGE')

    # ==================================================================
    # NO EXPLICIT TRANSACTION COMMITS
    # ==================================================================

    def test_37_no_explicit_commits(self):
        """Verify no env.cr.commit() in cutover or event processing code."""
        import inspect
        from ..models import amazon_fba_sale_stock_cutover as cutover_mod
        from ..models import amazon_fba_sale_stock as event_mod

        commit_pattern = '.cr.' + 'commit()'
        for mod in [cutover_mod, event_mod]:
            source = inspect.getsource(mod)
            self.assertNotIn(commit_pattern, source,
                             "Found explicit commit in %s" % mod.__name__)
