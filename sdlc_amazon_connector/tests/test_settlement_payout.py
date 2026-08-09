from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI, REPORT_SETTLEMENT


HEADERS = [
    'settlement-id', 'settlement-start-date', 'settlement-end-date',
    'deposit-date', 'total-amount', 'currency', 'transaction-type',
    'order-id', 'merchant-order-id', 'adjustment-id', 'shipment-id',
    'marketplace-name', 'amount-type', 'amount-description', 'amount',
    'fulfillment-id', 'posted-date', 'posted-date-time', 'order-item-code',
    'merchant-order-item-id', 'merchant-adjustment-item-id', 'sku',
    'quantity-purchased', 'promotion-id',
]


@tagged('post_install', '-at_install', 'amazon_settlement_payout')
class TestAmazonSettlementPayout(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Settlement Test Company',
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Egypt Settlement Test',
            'company_id': self.company.id,
            'seller_id': 'SETTLEMENT-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
        })
        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Settlement Product', 'default_code': 'SETTLE-SKU',
            'type': 'consu', 'is_storable': True,
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon Settlement Product',
            'instance_id': self.instance.id,
            'sku': 'SETTLE-SKU', 'asin': 'B0SETTLE01',
            'odoo_product_id': self.product.id,
        })
        self.metadata = {
            'reportId': 'REPORT-V2-001',
            'reportDocumentId': 'DOCUMENT-V2-001',
            'reportType': REPORT_SETTLEMENT,
            'processingStatus': 'DONE',
            'createdTime': '2026-08-05T12:00:00Z',
            'processingStartTime': '2026-08-05T12:01:00Z',
            'processingEndTime': '2026-08-05T12:02:00Z',
            'marketplaceIds': ['ARBP9OOSHTCHU'],
        }

    def _row(self, amount='100.00', total='100.00', **updates):
        row = {
            'settlement-id': 'SETTLEMENT-001',
            'settlement-start-date': '2026-08-01T00:00:00Z',
            'settlement-end-date': '2026-08-04T23:59:59Z',
            'deposit-date': '2026-08-05T00:00:00Z',
            'total-amount': total, 'currency': 'EGP',
            'transaction-type': 'Order', 'order-id': '',
            'merchant-order-id': '', 'adjustment-id': '', 'shipment-id': '',
            'marketplace-name': 'Amazon.eg', 'amount-type': 'ItemPrice',
            'amount-description': 'Principal', 'amount': amount,
            'fulfillment-id': 'AFN', 'posted-date': '2026-08-02',
            'posted-date-time': '2026-08-02T10:00:00Z',
            'order-item-code': 'ITEM-001', 'merchant-order-item-id': '',
            'merchant-adjustment-item-id': '', 'sku': 'SETTLE-SKU',
            'quantity-purchased': '1', 'promotion-id': '',
        }
        row.update(updates)
        return row

    def _raw(self, rows, extra_headers=None):
        headers = HEADERS + list(extra_headers or [])
        return '\ufeff' + '\t'.join(headers) + '\n' + '\n'.join(
            '\t'.join(str(row.get(header, '')) for header in headers)
            for row in rows
        )

    def _import(self, rows, instance=None, metadata=None, extra_headers=None):
        result = self.env['amazon.settlement.report'].sudo().import_flat_file(
            instance or self.instance,
            self._raw(rows, extra_headers=extra_headers),
            metadata or self.metadata,
        )
        return result, result['settlements']

    def test_a_one_settlement_imports(self):
        result, settlement = self._import([self._row()])
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(settlement), 1)
        self.assertEqual(settlement.settlement_id, 'SETTLEMENT-001')
        self.assertEqual(settlement.reported_net_amount, 100)
        self.assertEqual(settlement.currency_code, 'EGP')
        self.assertEqual(settlement.deposit_date, fields.Datetime.from_string('2026-08-05 00:00:00'))

    def test_b_idempotency_and_instance_isolation(self):
        first_result, first = self._import([self._row()])
        second_result, second = self._import([self._row(amount='125', total='125')])
        self.assertEqual(first, second)
        self.assertEqual(len(second.line_ids.filtered('active')), 1)
        self.assertEqual(second.line_ids.amount, 125)
        self.assertEqual(second.reported_net_amount, 125)
        self.assertEqual(first_result['processed'], second_result['processed'])

        # A refreshed document can temporarily drop a row. If it later
        # reappears, the inactive deterministic identity is reactivated.
        self._import([self._row(amount='130', total='130', **{
            'order-item-code': 'ITEM-REPLACEMENT',
        })])
        _result, restored = self._import([self._row(amount='140', total='140')])
        self.assertEqual(len(restored.line_ids), 1)
        self.assertEqual(restored.line_ids.amount, 140)
        self.assertEqual(self.env['amazon.settlement.report.line'].sudo().with_context(
            active_test=False,
        ).search_count([('report_id', '=', restored.id)]), 2)

        other_company = self.env['res.company'].sudo().create({'name': 'Other Seller'})
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'Other Amazon Seller', 'company_id': other_company.id,
            'seller_id': 'OTHER-SELLER', 'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu', 'refresh_token': 'mock-refresh',
            'client_id': 'mock-client', 'client_secret': 'mock-secret',
        })
        _result, other = self._import([self._row()], instance=other_instance)
        self.assertNotEqual(first, other)
        self.assertEqual(self.env['amazon.settlement.report'].sudo().search_count([
            ('settlement_id', '=', 'SETTLEMENT-001'),
        ]), 2)

    def test_c_sale_line_and_order_product_linking(self):
        order = self.env['amazon.sale.order'].sudo().create({
            'instance_id': self.instance.id, 'amazon_order_ref': 'ORDER-001',
        })
        _result, settlement = self._import([
            self._row(**{'order-id': order.amazon_order_ref}),
        ])
        line = settlement.line_ids
        self.assertEqual(line.normalized_category, 'sale')
        self.assertEqual(line.amazon_order_record_id, order)
        self.assertEqual(line.order_link_state, 'linked')
        self.assertEqual(line.amazon_product_id, self.amazon_product)
        self.assertEqual(line.odoo_product_id, self.product)

    def test_d_amazon_fee_lines_remain_detailed(self):
        _result, settlement = self._import([
            self._row(amount='-15', total='-20', **{
                'amount-type': 'AmazonFees', 'amount-description': 'Commission',
                'order-item-code': 'FEE-1',
            }),
            self._row(amount='-5', total='-20', **{
                'amount-type': 'AmazonFees',
                'amount-description': 'FBAFulfillmentFee',
                'order-item-code': 'FEE-2',
            }),
        ])
        self.assertEqual(set(settlement.line_ids.mapped('normalized_category')), {
            'amazon_fee', 'fba_fee',
        })
        self.assertEqual(settlement.amazon_fee_amount, -20)
        self.assertEqual(set(settlement.line_ids.mapped('amount_description')), {
            'Commission', 'FBAFulfillmentFee',
        })

    def test_e_financial_refund_is_independent(self):
        _result, settlement = self._import([self._row(amount='-40', total='-40', **{
            'transaction-type': 'Refund', 'amount-type': 'ItemPrice',
            'amount-description': 'Principal',
        })])
        line = settlement.line_ids
        self.assertEqual(line.normalized_category, 'refund')
        self.assertEqual(line.amount, -40)
        self.assertFalse(line.return_line_id)

    def test_f_reimbursement_line_links_existing_reimbursement(self):
        reimbursement = self.env['amazon.fba.reimbursement'].sudo().import_row(
            self.instance, {
                'approval-date': '2026-08-02T10:00:00Z',
                'reimbursement-id': 'REIMB-SETTLEMENT-1', 'case-id': '',
                'amazon-order-id': '', 'reason': 'Lost_Warehouse',
                'sku': 'SETTLE-SKU', 'fnsku': 'FNSKU-SETTLE',
                'asin': 'B0SETTLE01', 'product-name': 'Settlement Product',
                'condition': 'SELLABLE', 'currency-unit': 'EGP',
                'amount-per-unit': '50', 'amount-total': '50',
                'quantity-reimbursed-cash': '1',
                'quantity-reimbursed-inventory': '0',
                'quantity-reimbursed-total': '1',
                'original-reimbursement-id': '', 'original-reimbursement-type': '',
            }, 'REIMBURSEMENT-REPORT',
        )
        _result, settlement = self._import([self._row(amount='50', total='50', **{
            'transaction-type': 'Adjustment',
            'amount-type': 'Other', 'amount-description': 'Reimbursement',
            'adjustment-id': reimbursement.reimbursement_id,
        })])
        self.assertEqual(settlement.line_ids.normalized_category, 'reimbursement')
        self.assertEqual(settlement.line_ids.reimbursement_id, reimbursement)
        self.assertEqual(reimbursement.linked_settlement_id, settlement)

    def test_g_adjustment_is_not_forced_to_fee_or_reimbursement(self):
        _result, settlement = self._import([self._row(amount='12', total='12', **{
            'transaction-type': 'Adjustment',
            'amount-type': 'Adjustment', 'amount-description': 'ReserveAdjustment',
        })])
        self.assertEqual(settlement.line_ids.normalized_category, 'adjustment')
        self.assertEqual(settlement.adjustment_amount, 12)

    def test_h_positive_and_negative_amounts_are_preserved(self):
        rows = [
            self._row(amount='100', total='65', **{'order-item-code': 'SALE'}),
            self._row(amount='-15', total='65', **{
                'amount-type': 'AmazonFees', 'amount-description': 'Commission',
                'order-item-code': 'FEE',
            }),
            self._row(amount='-20', total='65', **{
                'transaction-type': 'Refund', 'order-item-code': 'REFUND',
            }),
        ]
        _result, settlement = self._import(rows)
        self.assertEqual(sorted(settlement.line_ids.mapped('amount')), [-20, -15, 100])
        self.assertEqual(settlement.calculated_net_amount, 65)

    def test_i_equal_reported_and_calculated_total_is_matched(self):
        _result, settlement = self._import([self._row()])
        self.assertEqual(settlement.calculated_net_amount, 100)
        self.assertEqual(settlement.reconciliation_difference, 0)
        self.assertEqual(settlement.reconciliation_state, 'matched')

    def test_j_different_total_is_mismatch_without_balancing_line(self):
        _result, settlement = self._import([self._row(amount='80', total='100')])
        self.assertEqual(settlement.calculated_net_amount, 80)
        self.assertEqual(settlement.reconciliation_difference, 20)
        self.assertEqual(settlement.reconciliation_state, 'mismatch')
        self.assertEqual(len(settlement.line_ids), 1)

    def test_k_currency_rounding_and_mixed_currency_protection(self):
        _result, settlement = self._import([self._row()])
        settlement.line_ids.amount = 100.004
        settlement._recompute_reconciliation()
        self.assertEqual(settlement.reconciliation_state, 'matched')

        _result, mixed = self._import([
            self._row(amount='50', total='100', **{'order-item-code': 'EGP'}),
            self._row(amount='50', total='100', currency='USD', **{
                'order-item-code': 'USD',
            }),
        ])
        self.assertTrue(mixed.currency_mismatch)
        self.assertEqual(mixed.reconciliation_state, 'incomplete')

    def test_l_malformed_row_keeps_valid_data_but_is_incomplete(self):
        valid = self._row(amount='100', total='100', **{'order-item-code': 'VALID'})
        malformed = self._row(amount='not-a-number', total='100', **{
            'order-item-code': 'MALFORMED',
        })
        result, settlement = self._import([valid, malformed])
        self.assertEqual(len(settlement.line_ids), 1)
        self.assertGreater(result['failed'], 0)
        self.assertEqual(settlement.reconciliation_state, 'incomplete')
        self.assertIn('MALFORMED', malformed['order-item-code'])
        self.assertIn('invalid amount', settlement.parsing_error_log)

    def test_m_unknown_amount_description_is_retained(self):
        _result, settlement = self._import([self._row(**{
            'transaction-type': 'NovelTransaction',
            'amount-type': 'NovelAmountType',
            'amount-description': 'FutureAmazonValue',
        })], extra_headers=['future-column'])
        line = settlement.line_ids
        self.assertEqual(line.normalized_category, 'unknown')
        self.assertEqual(line.amount_description, 'FutureAmazonValue')
        self.assertIn('future-column', line.raw_report_row)

    def test_n_missing_order_is_retained(self):
        _result, settlement = self._import([self._row(**{
            'order-id': 'ORDER-NOT-IMPORTED',
        })])
        line = settlement.line_ids
        self.assertTrue(line.exists())
        self.assertEqual(line.order_link_state, 'order_not_found')
        self.assertIn('ORDER NOT FOUND', line.matching_note)

    def test_o_import_and_reconciliation_create_no_account_move(self):
        before = self.env['account.move'].sudo().search_count([])
        _result, settlement = self._import([self._row()])
        settlement.action_reconcile()
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)

    def test_p_async_discovery_download_and_retry(self):
        raw = self._raw([self._row()])
        report = dict(self.metadata)
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'settlements',
        })
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(
                AmazonAPI, 'get_settlement_reports_list', return_value=[report],
            ) as discover,
        ):
            job._run_one_turn()
        self.assertEqual(job.stage, 'download')
        self.assertEqual(discover.call_args.kwargs['processing_statuses'], 'DONE')
        self.assertEqual(discover.call_args.args[1], 'token')
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_report_document', return_value={
                'url': 'https://example.test/settlement-v2',
            }),
            patch.object(AmazonAPI, 'download_report_document', return_value=raw),
        ):
            job._run_one_turn()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_processed, 1)
        self.assertTrue(self.instance.last_settlement_sync_at)

        retry_job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'settlements',
        })
        retry_job._fail_or_retry(Exception('HTTP 429 throttled'))
        self.assertEqual(retry_job.state, 'pending')
        self.assertGreater(retry_job.next_run_at, fields.Datetime.now())

        role_job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'settlements',
        })
        role_job._fail_or_retry(Exception('HTTP 403 Forbidden'))
        self.assertEqual(role_job.state, 'failed')
        self.assertEqual(role_job.last_error_code, 'SETTLEMENT_ROLE_MISSING')
        self.assertIn('Finance and Accounting', role_job.last_error_message)
