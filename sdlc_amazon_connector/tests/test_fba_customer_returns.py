import json
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install', 'amazon_fba_returns')
class TestFBACustomerReturns(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].sudo().create({'name': 'FBA Returns Company'})
        cls.partner = cls.env['res.partner'].sudo().create({
            'name': 'FBA Returns Customer', 'company_id': cls.company.id,
        })
        cls.instance = cls.env['amazon.instance'].sudo().create({
            'name': 'FBA Returns Egypt',
            'company_id': cls.company.id,
            'seller_id': 'RETURNS-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
        })
        cls.product = cls.env['product.product'].sudo().with_company(cls.company).create({
            'name': 'FBA Return Product',
            'default_code': 'RET-SKU',
            'type': 'consu',
            'is_storable': True,
            'company_id': cls.company.id,
        })
        cls.amazon_product = cls.env['amazon.product'].sudo().create({
            'name': 'FBA Return Amazon Product',
            'instance_id': cls.instance.id,
            'sku': 'RET-SKU',
            'asin': 'B0RETURNS01',
            'odoo_product_id': cls.product.id,
        })
        cls.sale_order = cls.env['sale.order'].sudo().with_company(cls.company).create({
            'partner_id': cls.partner.id,
            'company_id': cls.company.id,
        })
        cls.sale_line = cls.env['sale.order.line'].sudo().create({
            'order_id': cls.sale_order.id,
            'product_id': cls.product.id,
            'product_uom_qty': 3,
        })
        cls.amazon_order = cls.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': 'RET-ORDER-1',
            'instance_id': cls.instance.id,
            'sale_order_id': cls.sale_order.id,
            'fulfillment_channel': 'AFN',
        })
        cls.amazon_order_line = cls.env['amazon.sale.order.line'].sudo().create({
            'order_id': cls.amazon_order.id,
            'amazon_order_item_id': 'RET-ITEM-1',
            'amazon_product_id': cls.amazon_product.id,
            'odoo_product_id': cls.product.id,
            'sku': 'RET-SKU',
            'asin': 'B0RETURNS01',
            'quantity': 3,
        })

    def setUp(self):
        super().setUp()
        self.report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'downloaded',
            'amazon_report_id': 'RET-REPORT-%s' % self.id(),
        })

    def _row(self, **overrides):
        row = {
            'return-date': '2026-08-08T10:00:00Z',
            'order-id': 'RET-ORDER-1',
            'sku': 'RET-SKU',
            'asin': 'B0RETURNS01',
            'fnsku': 'RET-FNSKU',
            'product-name': 'FBA Return Product',
            'quantity': '1',
            'fulfillment-center-id': 'CAI1',
            'detailed-disposition': 'SELLABLE',
            'reason': 'CUSTOMER_RETURN',
            'status': 'Unit returned to inventory',
            'license-plate-number': 'LPN-RET-1',
            'customer-comments': '',
        }
        row.update(overrides)
        return row

    def _import(self, row=None):
        event = self.env['amazon.return.report.line'].sudo().import_row(
            self.report, row or self._row(),
        )
        event._classify_and_apply()
        return event

    def test_a_normal_return_imports_and_links(self):
        before_account_moves = self.env['account.move'].sudo().search_count([])
        event = self._import()
        self.assertEqual(event.quantity, 1)
        self.assertEqual(event.amazon_product_id, self.amazon_product)
        self.assertEqual(event.odoo_product_id, self.product)
        self.assertEqual(event.order_id, self.amazon_order)
        self.assertEqual(event.amazon_order_line_id, self.amazon_order_line)
        self.assertEqual(event.linked_sale_order_id, self.sale_order)
        self.assertEqual(event.linked_sale_order_line_id, self.sale_line)
        self.assertEqual(event.marketplace_id, 'ARBP9OOSHTCHU')
        self.assertFalse(event.amazon_order_item_id)
        self.assertEqual(event.return_reason, 'CUSTOMER_RETURN')
        self.assertEqual(event.detailed_disposition, 'SELLABLE')
        self.assertEqual(event.stock_action_state, 'audit_only')
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before_account_moves)

    def test_b_same_return_is_idempotent(self):
        first = self._import()
        second_report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'downloaded',
            'amazon_report_id': 'RET-REPORT-OVERLAP',
        })
        second = self.env['amazon.return.report.line'].sudo().import_row(second_report, self._row())
        self.assertEqual(first, second)
        self.assertEqual(self.env['amazon.return.report.line'].search_count([
            ('instance_id', '=', self.instance.id), ('event_key', '=', first.event_key),
        ]), 1)

    def test_c_partial_order_return_does_not_change_order_quantity(self):
        event = self._import(self._row(quantity='1'))
        self.assertEqual(event.quantity, 1)
        self.assertEqual(event.amazon_order_line_id.quantity, 3)
        self.assertEqual(event.linked_sale_order_line_id.product_uom_qty, 3)

    def test_d_multiple_events_for_one_order_item_remain_distinct(self):
        first = self._import(self._row(**{'license-plate-number': 'LPN-EVENT-1'}))
        second = self._import(self._row(
            **{
                'return-date': '2026-08-09T10:00:00Z',
                'quantity': '2',
                'license-plate-number': 'LPN-EVENT-2',
            }
        ))
        self.assertNotEqual(first.event_key, second.event_key)
        self.assertEqual(second.quantity, 2)
        self.assertEqual((first | second).mapped('amazon_order_line_id'), self.amazon_order_line)

    def test_e_unmapped_sku_is_retained_without_product_creation(self):
        before_products = self.env['product.product'].sudo().search_count([])
        event = self._import(self._row(
            sku='UNKNOWN-RETURN-SKU', asin='B0UNKNOWNRET', fnsku='UNKNOWN-FNSKU',
            **{'license-plate-number': 'LPN-UNMAPPED'},
        ))
        self.assertTrue(event)
        self.assertEqual(event.product_mapping_status, 'unmapped')
        self.assertFalse(event.amazon_product_id)
        self.assertFalse(event.odoo_product_id)
        self.assertTrue(event.manual_review_required)
        self.assertIn('UNMAPPED RETURN ITEM', event.review_reason)
        self.assertEqual(self.env['product.product'].sudo().search_count([]), before_products)

    def test_f_order_not_found_is_retained(self):
        event = self._import(self._row(
            **{'order-id': 'RET-ORDER-NOT-IMPORTED', 'license-plate-number': 'LPN-NO-ORDER'}
        ))
        self.assertFalse(event.order_id)
        self.assertEqual(event.order_link_status, 'order_not_found')
        self.assertIn('ORDER NOT FOUND', event.review_reason)

    def test_g_sellable_disposition_never_increases_stock(self):
        before_moves = self.env['stock.move'].sudo().search_count([])
        first = self._import(self._row(**{'license-plate-number': 'LPN-SELLABLE'}))
        second = self._import(self._row(**{'license-plate-number': 'LPN-SELLABLE'}))
        self.assertEqual(first, second)
        self.assertEqual(first.operational_disposition, 'sellable')
        self.assertFalse(first.linked_stock_move_id)
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), before_moves)

    def test_h_unsellable_disposition_never_increases_sellable_stock(self):
        before_moves = self.env['stock.move'].sudo().search_count([])
        event = self._import(self._row(
            **{
                'detailed-disposition': 'CUSTOMER_DAMAGED',
                'license-plate-number': 'LPN-UNSELLABLE',
            }
        ))
        self.assertEqual(event.operational_disposition, 'unsellable')
        self.assertFalse(event.linked_stock_move_id)
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), before_moves)

    def test_i_empty_report_completes_without_return_rows(self):
        raw = '\t'.join([
            'return-date', 'order-id', 'sku', 'asin', 'fnsku', 'quantity',
            'detailed-disposition', 'reason', 'status',
        ]) + '\n'
        self.assertEqual(self.env['amazon.phase7.job']._parse_rows(raw), [])
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id,
            'operation_type': 'customer_returns',
            'source_model': self.report._name,
            'source_id': self.report.id,
            'stage': 'process',
            'state': 'pending',
            'report_kind': 'returns',
            'raw_document': '[]',
        })
        job._run_one_turn()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_found, 0)
        self.assertEqual(self.report.state, 'processed')

    def test_j_malformed_row_does_not_rollback_valid_row(self):
        rows = [
            self._row(**{'license-plate-number': 'LPN-VALID'}),
            self._row(quantity='', **{'license-plate-number': 'LPN-BAD'}),
        ]
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id,
            'operation_type': 'customer_returns',
            'source_model': self.report._name,
            'source_id': self.report.id,
            'stage': 'process',
            'state': 'pending',
            'report_kind': 'returns',
            'raw_document': json.dumps(rows),
            'total_found': 2,
        })
        job._run_one_turn()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_processed, 1)
        self.assertEqual(job.total_failed, 1)
        self.assertIn('missing quantity', job.row_error_log)
        self.assertEqual(self.report.line_count, 1)
        self.assertFalse(self.instance.last_fba_return_sync_at)

    def test_k_http_429_is_retried_without_stock(self):
        before_moves = self.env['stock.move'].sudo().search_count([])
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id,
            'operation_type': 'customer_returns',
        })
        before = fields.Datetime.now()
        job._fail_or_retry(Exception('HTTP 429 throttled; Retry-After 120'))
        self.assertEqual(job.state, 'pending')
        self.assertGreater(job.next_run_at, before)
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), before_moves)

    def test_l_report_processing_is_retried_in_background(self):
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id,
            'operation_type': 'customer_returns',
            'source_model': self.report._name,
            'source_id': self.report.id,
            'stage': 'poll',
            'state': 'pending',
            'amazon_report_id': 'RET-REPORT-POLL',
        })
        self.assertEqual(
            job._report_configuration()[0],
            'GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA',
        )
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_report', return_value={
                'processingStatus': 'IN_PROGRESS',
                '_amazon_request_id': 'REQ-RETURNS-1',
            }),
        ):
            job._run_one_turn()
        self.assertEqual(job.state, 'waiting_amazon')
        self.assertEqual(job.last_error_code, 'IN_PROGRESS')
        self.assertEqual(job.amazon_request_id, 'REQ-RETURNS-1')
        self.assertGreater(job.next_run_at, fields.Datetime.now())

    def test_m_instances_are_isolated(self):
        other_company = self.env['res.company'].sudo().create({'name': 'FBA Returns Other Company'})
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'FBA Returns Other',
            'company_id': other_company.id,
            'seller_id': 'RETURNS-SELLER-OTHER',
            'marketplace_id': 'A1PA6795UKMFR9',
        })
        other_report = self.env['amazon.return.report'].sudo().create({
            'instance_id': other_instance.id,
            'state': 'downloaded',
            'amazon_report_id': 'RET-REPORT-OTHER',
        })
        first = self._import()
        second = self.env['amazon.return.report.line'].sudo().import_row(other_report, self._row())
        self.assertNotEqual(first.event_key, second.event_key)
        self.assertEqual(first.instance_id, self.instance)
        self.assertEqual(second.instance_id, other_instance)
        self.assertFalse(second.amazon_product_id)
        self.assertFalse(second.order_id)

    def test_n_return_sync_never_calls_stock_quant_or_picking_create(self):
        Quant = type(self.env['stock.quant'])
        Picking = type(self.env['stock.picking'])
        with (
            patch.object(Quant, 'create', autospec=True, side_effect=AssertionError('direct quant create')) as quant_create,
            patch.object(Quant, 'write', autospec=True, side_effect=AssertionError('direct quant write')) as quant_write,
            patch.object(Picking, 'create', autospec=True, side_effect=AssertionError('return picking create')) as picking_create,
        ):
            event = self._import(self._row(**{'license-plate-number': 'LPN-NO-STOCK'}))
        self.assertFalse(event.linked_stock_move_id)
        quant_create.assert_not_called()
        quant_write.assert_not_called()
        picking_create.assert_not_called()
