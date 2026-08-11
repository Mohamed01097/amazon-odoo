import inspect

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_payout import AmazonPayout


@tagged('post_install', '-at_install', 'amazon_payout_clearing')
class TestAmazonPayoutClearing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.currency = self.env['res.currency'].sudo().with_context(
            active_test=False,
        ).search([('name', '=', 'EGP')], limit=1)
        self.currency.active = True
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Payout Clearing Test Company',
            'currency_id': self.currency.id,
        })
        self.clearing_account = self.env['account.account'].sudo().with_company(
            self.company,
        ).create({
            'code': '730001', 'name': 'Amazon Clearing',
            'account_type': 'asset_current', 'reconcile': True,
            'company_ids': [Command.set([self.company.id])],
        })
        self.sales_account = self.env['account.account'].sudo().with_company(
            self.company,
        ).create({
            'code': '730002', 'name': 'Amazon Sales', 'account_type': 'income',
            'company_ids': [Command.set([self.company.id])],
        })
        self.bank_account = self.env['account.account'].sudo().with_company(
            self.company,
        ).create({
            'code': '730003', 'name': 'Amazon Payout Bank',
            'account_type': 'asset_cash',
            'company_ids': [Command.set([self.company.id])],
        })
        self.settlement_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'Amazon Settlement Test', 'code': 'AZST1',
            'type': 'general', 'company_id': self.company.id,
        })
        self.bank_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'Amazon Payout Bank Test', 'code': 'AZBK1',
            'type': 'bank', 'company_id': self.company.id,
            'default_account_id': self.bank_account.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Egypt Payout Test',
            'company_id': self.company.id,
            'seller_id': 'PAYOUT-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'settlement_journal_id': self.settlement_journal.id,
            'amazon_payout_bank_journal_id': self.bank_journal.id,
            'amazon_clearing_account_id': self.clearing_account.id,
            'amazon_sales_account_id': self.sales_account.id,
        })

    def _settlement(self, amount=78000, posted=True, suffix='001'):
        settlement = self.env['amazon.settlement.report'].sudo().create({
            'instance_id': self.instance.id,
            'settlement_id': 'PAYOUT-SETTLEMENT-%s' % suffix,
            'settlement_start_date': fields.Datetime.from_string('2026-08-01 00:00:00'),
            'settlement_end_date': fields.Datetime.from_string('2026-08-04 23:59:59'),
            'deposit_date': fields.Datetime.from_string('2026-08-05 00:00:00'),
            'currency_id': self.currency.id,
            'currency_code': self.currency.name,
            'reported_net_amount': amount,
            'state': 'imported',
            'reconciliation_state': 'pending',
        })
        self.env['amazon.settlement.report.line'].sudo().create({
            'report_id': settlement.id,
            'line_key': 'PAYOUT-LINE-%s' % suffix,
            'normalized_category': 'sale',
            'amount': amount,
            'currency_id': self.currency.id,
            'currency_code': self.currency.name,
            'amount_description': 'Principal',
            'transaction_type': 'Order',
            'amazon_transaction_type_raw': 'Order',
            'order_link_state': 'not_applicable',
        })
        settlement._recompute_reconciliation()
        settlement.action_create_accounting_entry()
        if posted:
            settlement.account_move_id.action_post()
        settlement._ensure_settlement_clearing_line()
        return settlement

    def _payout(self, settlement, actual, reference=None, source='manual_confirmation'):
        reference = reference or 'BANK-%s-%s' % (settlement.settlement_id, actual)
        payout = self.env['amazon.payout'].sudo().create({
            'instance_id': self.instance.id,
            'source': source,
            'source_reference': reference if source == 'manual_confirmation' else False,
            'payout_date': fields.Date.from_string('2026-08-06'),
            'currency_id': self.currency.id,
            'actual_received_amount': actual,
            'bank_journal_id': self.bank_journal.id,
            'matching_state': 'manually_matched',
        })
        allocation = self.env['amazon.payout.allocation'].sudo().create({
            'payout_id': payout.id,
            'settlement_id': settlement.id,
            'expected_amount': settlement.payout_remaining_amount,
            'allocated_amount': actual,
        })
        return payout, allocation

    def _create_posted_receipt_move(self, amount, reference='TEST RECEIPT'):
        move = self.env['account.move'].sudo().with_company(self.company).create({
            'move_type': 'entry',
            'journal_id': self.bank_journal.id,
            'company_id': self.company.id,
            'date': fields.Date.from_string('2026-08-06'),
            'ref': reference,
            'line_ids': [
                Command.create({
                    'name': reference, 'account_id': self.bank_account.id,
                    'debit': amount, 'credit': 0,
                }),
                Command.create({
                    'name': reference, 'account_id': self.clearing_account.id,
                    'debit': 0, 'credit': amount,
                }),
            ],
        })
        move.action_post()
        return move

    def _complete_manual_payout(self, settlement, actual, reference=None):
        payout, allocation = self._payout(settlement, actual, reference=reference)
        payout.action_create_draft_receipt()
        self.assertEqual(payout.receipt_move_id.state, 'draft')
        payout.receipt_move_id.action_post()
        payout.action_reconcile_clearing()
        return payout, allocation

    def test_a_exact_receipt_is_matched_and_clearing_is_zero(self):
        settlement = self._settlement()
        payout, _allocation = self._complete_manual_payout(settlement, 78000)
        self.assertEqual(payout.state, 'paid')
        self.assertEqual(settlement.payout_state, 'paid')
        self.assertTrue(self.currency.is_zero(settlement.clearing_remaining_amount))

    def test_b_underpayment_leaves_500_open(self):
        settlement = self._settlement()
        payout, _allocation = self._complete_manual_payout(settlement, 77500)
        self.assertEqual(payout.state, 'partially_paid')
        self.assertEqual(settlement.clearing_remaining_amount, 500)
        self.assertEqual(settlement.payout_remaining_amount, 500)
        self.assertEqual(payout.difference_amount, -500)

    def test_c_overpayment_is_mismatch_without_fabricated_income(self):
        settlement = self._settlement()
        payout, allocation = self._complete_manual_payout(settlement, 78500)
        self.assertEqual(payout.state, 'mismatch')
        self.assertEqual(payout.difference_amount, 500)
        self.assertEqual(allocation.receipt_clearing_move_line_id.amount_residual, -500)
        self.assertTrue(self.currency.is_zero(settlement.clearing_remaining_amount))

    def test_d_draft_settlement_accounting_blocks_reconciliation(self):
        settlement = self._settlement(posted=False)
        payout, allocation = self._payout(settlement, 78000)
        receipt = self._create_posted_receipt_move(78000)
        payout.write({'receipt_move_id': receipt.id})
        allocation.receipt_clearing_move_line_id = receipt.line_ids.filtered(
            lambda line: line.account_id == self.clearing_account
        )
        with self.assertRaisesRegex(UserError, 'must be posted first'):
            payout.action_reconcile_clearing()

    def test_e_existing_bank_transaction_is_reused_without_duplicate_move(self):
        settlement = self._settlement()
        bank_line = self.env['account.bank.statement.line'].sudo().with_company(
            self.company,
        ).create({
            'journal_id': self.bank_journal.id,
            'date': fields.Date.from_string('2026-08-06'),
            'payment_ref': 'AMAZON BANK CREDIT 001',
            'amount': 78000,
            'counterpart_account_id': self.clearing_account.id,
        })
        payout, _allocation = self._payout(
            settlement, 78000, reference='UNUSED', source='bank_transaction',
        )
        payout.bank_statement_line_id = bank_line
        before = self.env['account.move'].sudo().search_count([])
        payout.action_confirm_bank_transaction()
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)
        self.assertEqual(payout.receipt_move_id, bank_line.move_id)
        self.assertEqual(payout.source_reference, bank_line.payment_ref)

    def test_f_manual_confirmation_creates_draft_only(self):
        settlement = self._settlement()
        payout, _allocation = self._payout(settlement, 78000)
        payout.action_create_draft_receipt()
        self.assertEqual(payout.receipt_move_id.state, 'draft')
        bank_line = payout.receipt_move_id.line_ids.filtered(
            lambda line: line.account_id == self.bank_account
        )
        self.assertEqual(bank_line.debit, 78000)

    def test_g_same_payout_processed_twice_reuses_receipt_and_reconciliation(self):
        settlement = self._settlement()
        payout, _allocation = self._payout(settlement, 78000)
        payout.action_create_draft_receipt()
        receipt = payout.receipt_move_id
        before = self.env['account.move'].sudo().search_count([])
        payout.action_create_draft_receipt()
        self.assertEqual(payout.receipt_move_id, receipt)
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)
        receipt.action_post()
        payout.action_reconcile_clearing()
        partial_count = self.env['account.partial.reconcile'].sudo().search_count([])
        payout.action_reconcile_clearing()
        self.assertEqual(
            self.env['account.partial.reconcile'].sudo().search_count([]), partial_count,
        )

    def test_h_same_bank_transaction_cannot_be_allocated_twice(self):
        settlement = self._settlement()
        bank_line = self.env['account.bank.statement.line'].sudo().with_company(
            self.company,
        ).create({
            'journal_id': self.bank_journal.id,
            'date': fields.Date.from_string('2026-08-06'),
            'payment_ref': 'UNIQUE BANK CREDIT',
            'amount': 78000,
            'counterpart_account_id': self.clearing_account.id,
        })
        first, _allocation = self._payout(
            settlement, 78000, reference='FIRST', source='bank_transaction',
        )
        first.bank_statement_line_id = bank_line
        first.action_confirm_bank_transaction()
        with self.assertRaises(ValidationError):
            self.env['amazon.payout'].sudo().create({
                'instance_id': self.instance.id,
                'source': 'bank_transaction',
                'source_reference': 'SECOND',
                'payout_date': fields.Date.from_string('2026-08-06'),
                'currency_id': self.currency.id,
                'actual_received_amount': 78000,
                'bank_statement_line_id': bank_line.id,
            })

    def test_i_partial_payout_is_supported(self):
        settlement = self._settlement()
        payout, allocation = self._complete_manual_payout(settlement, 50000)
        self.assertEqual(payout.state, 'partially_paid')
        self.assertEqual(allocation.reconciled_amount, 50000)
        self.assertEqual(settlement.clearing_remaining_amount, 28000)

    def test_j_currency_mismatch_is_rejected(self):
        settlement = self._settlement()
        usd = self.env['res.currency'].sudo().with_context(active_test=False).search([
            ('name', '=', 'USD'),
        ], limit=1)
        usd.active = True
        payout = self.env['amazon.payout'].sudo().create({
            'instance_id': self.instance.id,
            'source': 'manual_confirmation',
            'source_reference': 'USD RECEIPT',
            'payout_date': fields.Date.from_string('2026-08-06'),
            'currency_id': usd.id,
            'actual_received_amount': 78000,
        })
        with self.assertRaisesRegex(ValidationError, 'currency must match'):
            self.env['amazon.payout.allocation'].sudo().create({
                'payout_id': payout.id,
                'settlement_id': settlement.id,
                'expected_amount': 78000,
                'allocated_amount': 78000,
            })

    def test_k_cross_company_bank_journal_is_rejected(self):
        other_company = self.env['res.company'].sudo().create({
            'name': 'Other Payout Company', 'currency_id': self.currency.id,
        })
        other_bank = self.env['account.account'].sudo().with_company(other_company).create({
            'code': '740001', 'name': 'Other Bank', 'account_type': 'asset_cash',
            'company_ids': [Command.set([other_company.id])],
        })
        other_journal = self.env['account.journal'].sudo().with_company(
            other_company,
        ).create({
            'name': 'Other Bank Journal', 'code': 'OTHBK', 'type': 'bank',
            'company_id': other_company.id, 'default_account_id': other_bank.id,
        })
        with self.assertRaises(UserError):
            self.instance.amazon_payout_bank_journal_id = other_journal

    def test_l_standard_odoo_reconciliation_creates_partial_record(self):
        settlement = self._settlement()
        payout, allocation = self._complete_manual_payout(settlement, 78000)
        partials = self.env['account.partial.reconcile'].sudo().search([
            ('debit_move_id', '=', settlement.clearing_move_line_id.id),
            ('credit_move_id', '=', allocation.receipt_clearing_move_line_id.id),
        ])
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials.amount, 78000)
        self.assertTrue(payout.receipt_move_id.state == 'posted')

    def test_m_connector_reconciliation_has_no_direct_sql_or_residual_write(self):
        source = inspect.getsource(AmazonPayout.action_reconcile_clearing)
        self.assertIn('.reconcile()', source)
        self.assertNotIn('.execute(', source)
        self.assertNotIn('amount_residual =', source)
        self.assertNotIn('reconciled =', source)

    def test_n_partial_reconciliation_creates_no_writeoff_move(self):
        settlement = self._settlement()
        payout, _allocation = self._payout(settlement, 77500)
        payout.action_create_draft_receipt()
        payout.receipt_move_id.action_post()
        before = self.env['account.move'].sudo().search_count([])
        payout.action_reconcile_clearing()
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)
        self.assertEqual(settlement.clearing_remaining_amount, 500)

    def test_o_clearing_reaches_zero_only_after_full_multiple_receipts(self):
        settlement = self._settlement()
        self._complete_manual_payout(settlement, 50000, reference='PARTIAL ONE')
        self.assertEqual(settlement.clearing_remaining_amount, 28000)
        self.assertEqual(settlement.payout_state, 'partially_paid')
        self._complete_manual_payout(settlement, 28000, reference='PARTIAL TWO')
        self.assertTrue(self.currency.is_zero(settlement.clearing_remaining_amount))
        self.assertEqual(settlement.received_payout_amount, 78000)
        self.assertEqual(settlement.payout_state, 'paid')
