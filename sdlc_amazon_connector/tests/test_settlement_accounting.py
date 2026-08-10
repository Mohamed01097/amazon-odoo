from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'amazon_settlement_accounting')
class TestAmazonSettlementAccounting(TransactionCase):

    def setUp(self):
        super().setUp()
        self.currency = self.env['res.currency'].sudo().with_context(active_test=False).search([
            ('name', '=', 'EGP'),
        ], limit=1)
        self.currency.active = True
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Settlement Accounting Test Company',
            'currency_id': self.currency.id,
        })
        self.accounts = {}
        account_specs = {
            'clearing': ('710001', 'Amazon Clearing', 'asset_current'),
            'sales': ('710002', 'Amazon Sales', 'income'),
            'refund': ('710003', 'Amazon Refunds', 'income'),
            'fee': ('710004', 'Amazon Fees', 'expense'),
            'fba_fee': ('710005', 'Amazon FBA Fees', 'expense'),
            'reimbursement': ('710006', 'Amazon Reimbursements', 'income_other'),
            'promotion': ('710007', 'Amazon Promotions', 'expense'),
            'adjustment': ('710008', 'Amazon Adjustments', 'expense_other'),
            'shipping': ('710009', 'Amazon Shipping', 'income'),
            'tax': ('710010', 'Amazon Tax', 'liability_current'),
            'other_credit': ('710011', 'Amazon Other Credits', 'income_other'),
            'other_debit': ('710012', 'Amazon Other Debits', 'expense_other'),
            'suspense': ('710013', 'Amazon Suspense', 'asset_current'),
            'receivable': ('710014', 'Amazon Customer Receivable', 'asset_receivable'),
        }
        for key, (code, name, account_type) in account_specs.items():
            self.accounts[key] = self.env['account.account'].sudo().with_company(
                self.company
            ).create({
                'code': code,
                'name': name,
                'account_type': account_type,
                'company_ids': [Command.set([self.company.id])],
            })
        self.journal = self.env['account.journal'].sudo().with_company(self.company).create({
            'name': 'Amazon Settlements', 'code': 'AMZST',
            'type': 'general', 'company_id': self.company.id,
        })
        self.sales_journal = self.env['account.journal'].sudo().with_company(
            self.company
        ).create({
            'name': 'Amazon Customer Invoices', 'code': 'AMZSI',
            'type': 'sale', 'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Egypt Accounting Test',
            'company_id': self.company.id,
            'seller_id': 'ACCOUNTING-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'settlement_journal_id': self.journal.id,
            'amazon_clearing_account_id': self.accounts['clearing'].id,
            'amazon_sales_account_id': self.accounts['sales'].id,
            'amazon_refund_account_id': self.accounts['refund'].id,
            'amazon_fee_account_id': self.accounts['fee'].id,
            'amazon_fba_fee_account_id': self.accounts['fba_fee'].id,
            'amazon_reimbursement_account_id': self.accounts['reimbursement'].id,
            'amazon_promotion_account_id': self.accounts['promotion'].id,
            'amazon_adjustment_account_id': self.accounts['adjustment'].id,
            'amazon_shipping_account_id': self.accounts['shipping'].id,
            'amazon_tax_account_id': self.accounts['tax'].id,
            'amazon_other_credit_account_id': self.accounts['other_credit'].id,
            'amazon_other_debit_account_id': self.accounts['other_debit'].id,
            'amazon_suspense_account_id': self.accounts['suspense'].id,
        })

    def _settlement(self, lines, reported=None, settlement_id='ACCOUNTING-SETTLEMENT-001'):
        reported = sum(line_data[1] for line_data in lines) if reported is None else reported
        settlement = self.env['amazon.settlement.report'].sudo().create({
            'instance_id': self.instance.id,
            'settlement_id': settlement_id,
            'settlement_start_date': fields.Datetime.from_string('2026-08-01 00:00:00'),
            'settlement_end_date': fields.Datetime.from_string('2026-08-04 23:59:59'),
            'deposit_date': fields.Datetime.from_string('2026-08-05 00:00:00'),
            'currency_id': self.currency.id,
            'currency_code': self.currency.name,
            'reported_net_amount': reported,
            'state': 'imported',
            'reconciliation_state': 'pending',
        })
        for index, line_data in enumerate(lines, start=1):
            if len(line_data) == 2:
                category, amount = line_data
                extra = {}
            else:
                category, amount, extra = line_data
            self.env['amazon.settlement.report.line'].sudo().create({
                'report_id': settlement.id,
                'line_key': '%s-%s' % (settlement_id, index),
                'normalized_category': category,
                'amount': amount,
                'currency_id': self.currency.id,
                'currency_code': self.currency.name,
                'amount_description': extra.get('amount_description', category),
                'transaction_type': extra.get('transaction_type', 'Order'),
                'amazon_transaction_type_raw': extra.get('transaction_type', 'Order'),
                'amazon_order_id': extra.get('amazon_order_id', ''),
                'order_link_state': extra.get('order_link_state', 'not_applicable'),
                'amazon_order_record_id': extra.get('amazon_order_record_id', False),
                'sale_order_id': extra.get('sale_order_id', False),
                'return_line_id': extra.get('return_line_id', False),
                'reimbursement_id': extra.get('reimbursement_id', False),
            })
        settlement._recompute_reconciliation()
        return settlement

    def _create_entry(self, settlement):
        settlement.sudo().action_create_accounting_entry()
        return settlement.account_move_id

    def test_a_matched_settlement_creates_one_draft_move(self):
        settlement = self._settlement([('sale', 100)])
        move = self._create_entry(settlement)
        self.assertTrue(move)
        self.assertEqual(move.state, 'draft')
        self.assertEqual(move.date, fields.Date.from_string('2026-08-05'))
        self.assertEqual(move.ref, 'Amazon Settlement ACCOUNTING-SETTLEMENT-001')

    def test_b_second_click_reuses_one_move(self):
        settlement = self._settlement([('sale', 100)])
        first = self._create_entry(settlement)
        settlement.sudo().action_create_accounting_entry()
        self.assertEqual(settlement.account_move_id, first)
        self.assertEqual(self.env['account.move'].sudo().search_count([
            ('ref', '=', move_ref := 'Amazon Settlement ACCOUNTING-SETTLEMENT-001'),
        ]), 1, move_ref)

    def test_c_mismatch_blocks_creation(self):
        settlement = self._settlement([('sale', 80)], reported=100)
        with self.assertRaisesRegex(UserError, 'complete and payout-matched'):
            self._create_entry(settlement)
        self.assertFalse(settlement.account_move_id)

    def test_d_missing_relevant_mapping_blocks_creation(self):
        settlement = self._settlement([('amazon_fee', -10)])
        self.instance.amazon_fee_account_id = False
        with self.assertRaisesRegex(UserError, 'No account mapping.*amazon_fee'):
            self._create_entry(settlement)

    def test_e_unknown_category_blocks_creation(self):
        settlement = self._settlement([('unknown', 10)])
        with self.assertRaisesRegex(UserError, 'Unknown Amazon category'):
            self._create_entry(settlement)

    def test_f_move_is_balanced(self):
        settlement = self._settlement([
            ('sale', 100), ('amazon_fee', -15), ('refund', -20),
            ('reimbursement', 5),
        ])
        move = self._create_entry(settlement)
        self.assertEqual(
            self.company.currency_id.compare_amounts(
                sum(move.line_ids.mapped('debit')), sum(move.line_ids.mapped('credit')),
            ),
            0,
        )

    def test_g_clearing_impact_matches_payout(self):
        settlement = self._settlement([('sale', 100), ('amazon_fee', -15)])
        move = self._create_entry(settlement)
        clearing_lines = move.line_ids.filtered(
            lambda line: line.account_id == self.accounts['clearing']
        )
        self.assertEqual(len(clearing_lines), 1)
        self.assertEqual(clearing_lines.debit - clearing_lines.credit, 85)
        self.assertEqual(settlement.reported_net_amount, 85)

    def test_h_fee_goes_to_fee_account(self):
        move = self._create_entry(self._settlement([('amazon_fee', -10)]))
        fee_line = move.line_ids.filtered(lambda line: line.account_id == self.accounts['fee'])
        self.assertEqual(fee_line.debit, 10)
        self.assertEqual(fee_line.credit, 0)

    def test_i_refund_goes_to_refund_account(self):
        move = self._create_entry(self._settlement([('refund', -20)]))
        refund_line = move.line_ids.filtered(
            lambda line: line.account_id == self.accounts['refund']
        )
        self.assertEqual(refund_line.debit, 20)

    def test_j_reimbursement_goes_to_reimbursement_account(self):
        move = self._create_entry(self._settlement([('reimbursement', 30)]))
        line = move.line_ids.filtered(
            lambda move_line: move_line.account_id == self.accounts['reimbursement']
        )
        self.assertEqual(line.credit, 30)

    def test_k_no_bank_account_is_touched(self):
        move = self._create_entry(self._settlement([
            ('sale', 100), ('amazon_fee', -15), ('refund', -20),
        ]))
        self.assertFalse(move.line_ids.account_id.filtered(
            lambda account: account.account_type == 'asset_cash'
        ))

    def test_l_move_remains_draft(self):
        move = self._create_entry(self._settlement([('sale', 100)]))
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.name and move.name != '/')

    def test_m_cross_company_mapping_is_rejected(self):
        other_company = self.env['res.company'].sudo().create({
            'name': 'Other Accounting Company', 'currency_id': self.currency.id,
        })
        other_account = self.env['account.account'].sudo().with_company(other_company).create({
            'code': '720001', 'name': 'Other Company Fee', 'account_type': 'expense',
            'company_ids': [Command.set([other_company.id])],
        })
        with self.assertRaisesRegex(ValidationError, 'not available to company'):
            self.instance.amazon_fee_account_id = other_account

    def test_n_posted_invoice_revenue_is_not_double_booked(self):
        partner = self.env['res.partner'].sudo().create({'name': 'Amazon Buyer'})
        partner.with_company(self.company).property_account_receivable_id = self.accounts['receivable']
        invoice = self.env['account.move'].sudo().with_company(self.company).create({
            'move_type': 'out_invoice',
            'journal_id': self.sales_journal.id,
            'company_id': self.company.id,
            'partner_id': partner.id,
            'invoice_date': fields.Date.from_string('2026-08-02'),
            'invoice_line_ids': [Command.create({
                'name': 'Amazon sale already invoiced',
                'account_id': self.accounts['sales'].id,
                'quantity': 1,
                'price_unit': 100,
            })],
        })
        invoice.action_post()
        amazon_order = self.env['amazon.sale.order'].sudo().create({
            'instance_id': self.instance.id,
            'amazon_order_ref': 'ORDER-WITH-POSTED-INVOICE',
            'invoice_id': invoice.id,
        })
        settlement = self._settlement([('sale', 100, {
            'amazon_order_id': amazon_order.amazon_order_ref,
            'order_link_state': 'linked',
            'amazon_order_record_id': amazon_order.id,
        })])
        move = self._create_entry(settlement)
        self.assertFalse(move.line_ids.filtered(
            lambda line: line.account_id == self.accounts['sales']
        ))
        receivable_line = move.line_ids.filtered(
            lambda line: line.account_id == self.accounts['receivable']
        )
        self.assertEqual(receivable_line.credit, 100)
        self.assertEqual(invoice.state, 'posted')
