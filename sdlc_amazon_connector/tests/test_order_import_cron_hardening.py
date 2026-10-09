from datetime import datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI

EG = 'ARBP9OOSHTCHU'
HOLD = 'HELD_FOR_CONTROLLED_IMPORT'


def _make_fake_get_orders(pool):
    """Return a paginating stand-in for AmazonAPI.get_orders over `pool`.

    Continuation is encoded in the token ('TOK-<offset>'), mirroring Amazon's
    opaque paginationToken, so continuation resumes the exact result set.
    """
    def fake(instance, access_token, created_after=None, created_before=None,
             last_updated_after=None, last_updated_before=None, order_statuses=None,
             fulfillment_channels=None, next_token=None, max_results_per_page=None,
             included_data=None):
        start = int(next_token.split('-', 1)[1]) if next_token else 0
        size = max_results_per_page or 10
        page = pool[start:start + size]
        end = start + len(page)
        nt = ('TOK-%d' % end) if end < len(pool) else False
        return {'payload': {'Orders': list(page), 'NextToken': nt}}
    return fake


@tagged('post_install', '-at_install')
class TestOrderImportCronHardening(TransactionCase):
    """Phase 10B: automatic FBA hold, hard per-job cap (loss-free), enable gate."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        wh = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        base_sale = self.env['account.tax'].search(
            [('type_tax_use', '=', 'sale'), ('company_id', '=', self.company.id)], limit=1)
        self.vat14 = base_sale.copy({
            'name': 'EG VAT 14% incl (10B)', 'amount': 14.0,
            'price_include_override': 'tax_included', 'active': True})
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': '10B Instance', 'company_id': self.company.id,
            'marketplace_id': EG, 'region': 'eu',
            'fba_warehouse_id': wh.id, 'fbm_warehouse_id': wh.id,
            'amazon_sales_tax_id': self.vat14.id,
            'order_import_enabled': False,
            'order_import_hold_fba_stock': True,
            'order_import_max_orders_per_job': 10,
        })
        self.product = self.env['product.product'].sudo().create({
            'name': '10B Towel', 'type': 'consu', 'is_storable': True})
        self.env['amazon.product'].sudo().create({
            'name': '10B Towel', 'instance_id': self.instance.id,
            'sku': 'MAP-SKU', 'odoo_product_id': self.product.id})
        self.SE = self.env['amazon.fba.sale.stock.event']
        self.Job = self.env['amazon.order.import.job']

    # ---- builders ----
    def _order(self, oid, sku='MAP-SKU', channel='AFN', qty=1, unit=100.0, shipped=None):
        shipped = qty if shipped is None else shipped
        return {
            'AmazonOrderId': oid, 'PurchaseDate': '2026-08-01T10:00:00Z',
            'LastUpdateDate': '2026-08-01T11:00:00Z', 'OrderStatus': 'Shipped',
            'FulfillmentChannel': channel, 'OrderType': 'StandardOrder',
            'SalesChannel': 'Amazon.eg', 'IsPrime': False, 'IsBusinessOrder': False,
            'OrderTotal': {'Amount': unit * qty, 'CurrencyCode': 'EGP'},
            'ShipServiceLevel': 'STD', 'ShippingAddress': {},
            'OrderItems': [{
                'OrderItemId': 'IT-' + oid, 'SellerSKU': sku, 'ASIN': 'B0', 'Title': 'x',
                'QuantityOrdered': qty, 'QuantityShipped': shipped,
                'ItemPrice': {'Amount': unit * qty, 'CurrencyCode': 'EGP'},
                'ShippingPrice': {}, 'ItemTax': {}, 'PromotionDiscount': {}}]}

    def _new_job(self, **vals):
        base = dict(
            instance_id=self.instance.id,
            date_from=datetime(2026, 8, 1, 0, 0, 0),
            date_to=datetime(2026, 8, 2, 0, 0, 0),
            effective_date_to=datetime(2026, 8, 2, 0, 0, 0),
            amazon_request_before='2026-08-02T00:00:00Z',
            batch_size=10, hold_fba_stock=True, max_orders=0)
        base.update(vals)
        return self.Job.sudo().create(base)

    def _drive(self, job, pool):
        with (
            patch.object(type(self.instance), '_auto_fix_region', return_value=True),
            patch.object(type(self.instance), '_check_required_fields', return_value=True),
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='tok'),
            patch.object(AmazonAPI, 'get_orders', side_effect=_make_fake_get_orders(pool)),
        ):
            for _ in range(60):
                if job.state not in ('draft', 'running'):
                    break
                job._process_next_batch()
        return job

    def _staged(self, oid):
        return self.env['amazon.sale.order'].sudo().search(
            [('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', oid)])

    # 1
    def test_01_cron_noop_when_disabled(self):
        self.instance.order_import_enabled = False
        before = self.Job.search_count([('instance_id', '=', self.instance.id)])
        with patch.object(AmazonAPI, 'get_orders', side_effect=AssertionError('no fetch')):
            self.env['amazon.instance'].cron_import_orders()
        self.assertEqual(self.Job.search_count([('instance_id', '=', self.instance.id)]), before)

    # 2
    def test_02_cron_queues_when_enabled(self):
        self.instance.order_import_enabled = True
        with (patch.object(type(self.instance), '_auto_fix_region', return_value=True),
              patch.object(type(self.instance), '_check_required_fields', return_value=True)):
            self.env['amazon.instance'].cron_import_orders()
        jobs = self.Job.search([('instance_id', '=', self.instance.id), ('state', 'in', ('draft', 'running'))])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs.hold_fba_stock)       # snapshot default True
        self.assertEqual(jobs.max_orders, 10)

    # 14
    def test_14_fbm_cron_respects_gate(self):
        self.instance.order_import_enabled = False
        before = self.Job.search_count([('instance_id', '=', self.instance.id)])
        self.env['amazon.instance'].cron_import_fbm_orders()
        self.assertEqual(self.Job.search_count([('instance_id', '=', self.instance.id)]), before)

    # 3
    def test_03_auto_afn_import_creates_held_event(self):
        job = self._new_job(hold_fba_stock=True, max_orders=0)
        self._drive(job, [self._order('AX-1', channel='AFN')])
        ev = self.SE.search([('instance_id', '=', self.instance.id), ('amazon_order_ref', '=', 'AX-1')])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)
        self.assertFalse(ev.picking_ids)

    # 4
    def test_04_held_auto_event_survives_status_reupsert(self):
        job = self._new_job(hold_fba_stock=True)
        self._drive(job, [self._order('AX-2', channel='AFN')])
        order = self._staged('AX-2')
        # status-sync-style re-upsert WITHOUT hold context
        self.Job.sudo().new({'instance_id': self.instance.id})._upsert_order_items(
            order, AmazonAPI._normalize_order_2026({
                'orderId': 'AX-2', 'fulfillment': {'fulfilledBy': 'AMAZON', 'fulfillmentStatus': 'SHIPPED'},
                'orderItems': [{'orderItemId': 'IT-AX-2', 'quantityOrdered': 1,
                                'product': {'sellerSku': 'MAP-SKU',
                                            'price': {'unitPrice': {'amount': '100.0', 'currencyCode': 'EGP'}}},
                                'fulfillment': {'quantityFulfilled': 1}}]})['OrderItems'])
        ev = self.SE.search([('amazon_order_ref', '=', 'AX-2'), ('instance_id', '=', self.instance.id)])
        self.assertEqual(ev.state, 'manual_review')
        self.assertEqual(ev.last_error_code, HOLD)

    # 5
    def test_05_processor_skips_held_auto_event(self):
        job = self._new_job(hold_fba_stock=True)
        self._drive(job, [self._order('AX-3', channel='AFN')])
        ev = self.SE.search([('amazon_order_ref', '=', 'AX-3'), ('instance_id', '=', self.instance.id)])
        self.assertFalse(ev._process_one())
        ev.invalidate_recordset()
        self.assertEqual(ev.state, 'manual_review')
        self.assertFalse(ev.picking_ids)

    # 6
    def test_06_retry_releases_auto_event(self):
        job = self._new_job(hold_fba_stock=True)
        self._drive(job, [self._order('AX-4', channel='AFN')])
        ev = self.SE.search([('amazon_order_ref', '=', 'AX-4'), ('instance_id', '=', self.instance.id)])
        ev.action_retry(); ev.invalidate_recordset()
        self.assertEqual(ev.state, 'pending')
        self.assertFalse(ev.last_error_code)
        self.assertFalse(ev._is_controlled_import_hold())

    # 7
    def test_07_cap_10_with_more_available(self):
        pool = [self._order('C7-%02d' % i, sku='UNMAPPED-SKU', channel='MFN') for i in range(25)]
        job = self._new_job(max_orders=10)
        self._drive(job, pool)
        self.assertEqual(job.state, 'capped')
        self.assertTrue(job.limit_reached)
        self.assertEqual(job.total_found, 10)
        staged = self.env['amazon.sale.order'].search_count([('instance_id', '=', self.instance.id),
                                                             ('amazon_order_ref', 'like', 'C7-%')])
        self.assertEqual(staged, 10)

    # 8
    def test_08_pagination_cannot_bypass_cap(self):
        # 25 available, batch 10, cap 10 -> exactly 10, never 20/30.
        pool = [self._order('C8-%02d' % i, sku='UNMAPPED-SKU', channel='MFN') for i in range(25)]
        job = self._new_job(max_orders=10, batch_size=10)
        self._drive(job, pool)
        self.assertEqual(job.total_found, 10)

    # 9
    def test_09_non_page_multiple_cap(self):
        # cap 15, pages of 10 -> exactly 15 (10 + 5), never 20.
        pool = [self._order('C9-%02d' % i, sku='UNMAPPED-SKU', channel='MFN') for i in range(25)]
        job = self._new_job(max_orders=15, batch_size=10)
        self._drive(job, pool)
        self.assertEqual(job.state, 'capped')
        self.assertEqual(job.total_found, 15)

    # 10
    def test_10_cap_does_not_advance_cursor(self):
        self.instance.last_order_sync = datetime(2026, 7, 1, 0, 0, 0)
        pool = [self._order('C10-%02d' % i, sku='UNMAPPED-SKU', channel='MFN') for i in range(25)]
        job = self._new_job(max_orders=10)
        self._drive(job, pool)
        self.assertEqual(job.state, 'capped')
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.last_order_sync, datetime(2026, 7, 1, 0, 0, 0))

    # 11
    def test_11_continuation_imports_remainder_no_gaps_no_dupes(self):
        self.instance.last_order_sync = datetime(2026, 7, 1, 0, 0, 0)
        pool = [self._order('C11-%02d' % i, sku='UNMAPPED-SKU', channel='MFN') for i in range(25)]
        job1 = self._new_job(max_orders=10)
        self._drive(job1, pool)
        self.assertEqual(job1.state, 'capped')
        # continuation job via _queue_order_import_job (detects capped job)
        with (patch.object(type(self.instance), '_auto_fix_region', return_value=True),
              patch.object(type(self.instance), '_check_required_fields', return_value=True)):
            job2, created, _w = self.instance._queue_order_import_job()
        self.assertTrue(created)
        self.assertEqual(job2.continuation_of_id, job1)
        self.assertEqual(job2.next_token, job1.next_token)
        self._drive(job2, pool)
        job3 = job2
        # a third continuation drains the last 5
        if job2.state == 'capped':
            with (patch.object(type(self.instance), '_auto_fix_region', return_value=True),
                  patch.object(type(self.instance), '_check_required_fields', return_value=True)):
                job3, _c, _w = self.instance._queue_order_import_job()
            self._drive(job3, pool)
        staged = self.env['amazon.sale.order'].search([('instance_id', '=', self.instance.id),
                                                       ('amazon_order_ref', 'like', 'C11-%')])
        self.assertEqual(len(staged), 25)                        # all 25, no gaps
        self.assertEqual(len(set(staged.mapped('amazon_order_ref'))), 25)   # no dupes
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.last_order_sync, datetime(2026, 8, 2, 0, 0, 0))  # advanced only at end

    # 12
    def test_12_idempotent_repeated_order(self):
        pool = [self._order('C12-1', sku='UNMAPPED-SKU', channel='MFN')]
        self._drive(self._new_job(max_orders=0), pool)
        self._drive(self._new_job(max_orders=0), pool)
        self.assertEqual(len(self._staged('C12-1')), 1)

    # 13
    def test_13_fbm_import_is_draft_only(self):
        job = self._new_job(hold_fba_stock=True)
        self._drive(job, [self._order('FBM-1', channel='MFN')])
        order = self._staged('FBM-1')
        so = order.sale_order_id
        self.assertTrue(so)
        self.assertEqual(so.state, 'draft')
        self.assertFalse(so.picking_ids)
        self.assertFalse(so.invoice_ids)
        self.assertFalse(self.SE.search([('amazon_order_ref', '=', 'FBM-1'),
                                         ('instance_id', '=', self.instance.id)]))

    # 15
    def test_15_vat_shipping_parity_in_cron_path(self):
        job = self._new_job(hold_fba_stock=True)
        self._drive(job, [self._order('VP-1', channel='AFN', qty=1, unit=100.0)])
        so = self._staged('VP-1').sale_order_id
        self.assertEqual(round(so.amount_total, 2), 100.00)
        self.assertEqual(round(so.amount_untaxed, 2), 87.72)
        self.assertEqual(round(so.amount_tax, 2), 12.28)
        self.assertEqual(so.order_line[:1].tax_ids, self.vat14)
