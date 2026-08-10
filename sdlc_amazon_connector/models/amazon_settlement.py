import csv
import hashlib
import io
import json
import logging
import re
from collections import defaultdict
from datetime import timezone
from decimal import Decimal, InvalidOperation

from dateutil import parser as date_parser

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_logger = logging.getLogger(__name__)


SETTLEMENT_HEADER_FIELDS = (
    'settlement-id', 'settlement-start-date', 'settlement-end-date',
    'deposit-date', 'total-amount', 'currency',
)
SETTLEMENT_LINE_DIMENSIONS = (
    'transaction-type', 'order-id', 'merchant-order-id', 'adjustment-id',
    'shipment-id', 'marketplace-name', 'amount-type', 'amount-description',
    'fulfillment-id', 'posted-date', 'posted-date-time', 'order-item-code',
    'merchant-order-item-id', 'merchant-adjustment-item-id', 'sku',
    'promotion-id',
)
ACCOUNT_FIELD_BY_CATEGORY = {
    'sale': 'amazon_sales_account_id',
    'amazon_fee': 'amazon_fee_account_id',
    'fba_fee': 'amazon_fba_fee_account_id',
    'refund': 'amazon_refund_account_id',
    'promotion': 'amazon_promotion_account_id',
    'reimbursement': 'amazon_reimbursement_account_id',
    'adjustment': 'amazon_adjustment_account_id',
    'tax': 'amazon_tax_account_id',
    'shipping': 'amazon_shipping_account_id',
    'other_credit': 'amazon_other_credit_account_id',
    'other_debit': 'amazon_other_debit_account_id',
}
INVOICE_FINANCIAL_CATEGORIES = {'sale', 'refund', 'promotion', 'tax', 'shipping'}


class AmazonSettlementReport(models.Model):
    _name = 'amazon.settlement.report'
    _description = 'Amazon Settlement Report'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'settlement_end_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default='New', readonly=True, copy=False)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    marketplace_id = fields.Char(
        related='instance_id.marketplace_id', store=True, readonly=True, index=True,
    )
    marketplace_ids = fields.Char(
        string='Amazon Marketplace IDs', readonly=True,
        help='Marketplace IDs returned in the Reports API metadata.',
    )
    settlement_id = fields.Char('Settlement ID', index=True, readonly=True, tracking=True)
    settlement_start_date = fields.Datetime(readonly=True, index=True)
    settlement_end_date = fields.Datetime(readonly=True, index=True)
    deposit_date = fields.Datetime(readonly=True, index=True)
    # Legacy date fields are retained for existing integrations and views.
    start_date = fields.Date('Start Date', readonly=True)
    end_date = fields.Date('End Date', readonly=True)

    currency_id = fields.Many2one('res.currency', readonly=True, ondelete='restrict')
    currency_code = fields.Char(readonly=True, index=True)
    reported_net_amount = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help='Exact total-amount reported by Amazon. It is never derived locally.',
    )
    total_amount = fields.Monetary(
        related='reported_net_amount', currency_field='currency_id',
        store=True, readonly=True, string='Amazon Reported Payout',
    )
    calculated_net_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    reconciliation_difference = fields.Monetary(
        compute='_compute_reconciliation_difference', store=True,
        currency_field='currency_id',
    )

    gross_sales_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    refund_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    amazon_fee_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    reimbursement_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    promotion_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    adjustment_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    tax_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    other_credit_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )
    other_debit_amount = fields.Monetary(
        compute='_compute_financial_totals', store=True, currency_field='currency_id',
    )

    state = fields.Selection([
        ('draft', 'Draft'), ('downloaded', 'Downloaded'),
        ('processed', 'Processed'), ('reconciled', 'Reconciled'),
        ('imported', 'Imported'), ('incomplete', 'Incomplete'), ('error', 'Error'),
    ], default='draft', required=True, readonly=True, index=True, tracking=True)
    reconciliation_state = fields.Selection([
        ('pending', 'Pending'), ('matched', 'Matched'), ('mismatch', 'Mismatch'),
        ('incomplete', 'Incomplete'), ('error', 'Error'),
        ('manual_review', 'Manual Review'),
    ], default='pending', required=True, readonly=True, index=True, tracking=True)
    currency_mismatch = fields.Boolean(readonly=True, index=True)
    parsing_error_count = fields.Integer(readonly=True)
    parsing_error_log = fields.Text(readonly=True)

    report_id = fields.Char('Amazon Report ID', readonly=True, copy=False, index=True)
    report_document_id = fields.Char(readonly=True, copy=False, index=True)
    report_type = fields.Char(readonly=True)
    processing_status = fields.Char('Amazon Status', readonly=True)
    created_time = fields.Datetime(readonly=True)
    processing_start_time = fields.Datetime(readonly=True)
    processing_end_time = fields.Datetime(readonly=True)
    imported_at = fields.Datetime(readonly=True)
    last_synced_at = fields.Datetime(readonly=True)
    raw_header_source = fields.Text(
        readonly=True, groups='sdlc_amazon_connector.group_amazon_technical_admin',
    )

    line_ids = fields.One2many(
        'amazon.settlement.report.line', 'report_id', string='Financial Lines',
        readonly=True,
    )
    line_count = fields.Integer(compute='_compute_link_counts')
    order_count = fields.Integer(compute='_compute_link_counts')
    reimbursement_count = fields.Integer(compute='_compute_link_counts')
    sync_log_count = fields.Integer(compute='_compute_link_counts')

    account_move_id = fields.Many2one(
        'account.move', string='Accounting Entry', readonly=True, copy=False,
        ondelete='restrict', check_company=True, tracking=True,
    )
    accounting_state = fields.Selection([
        ('not_ready', 'Not Ready'), ('ready', 'Ready'),
        ('draft_entry', 'Draft Entry'), ('posted', 'Posted'), ('error', 'Error'),
    ], compute='_compute_accounting_status', string='Accounting State')
    accounting_date = fields.Date(compute='_compute_accounting_status')
    accounting_mapping_errors = fields.Text(
        compute='_compute_accounting_status', string='Accounting Mapping Errors',
        groups='account.group_account_user,account.group_account_manager',
    )

    # Legacy links are retained read-only for upgrade compatibility.
    reimbursement_invoice_ids = fields.Many2many(
        'account.move', string='Legacy Reimbursement Invoices', readonly=True,
    )

    _settlement_unique = models.Constraint(
        'UNIQUE(instance_id, settlement_id)',
        'Settlement ID must be unique per Amazon instance.',
    )
    _account_move_unique = models.Constraint(
        'UNIQUE(account_move_id)',
        'An accounting entry can only belong to one Amazon settlement.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'amazon.settlement.report'
                ) or 'New'
        return super().create(vals_list)

    @api.depends('line_ids.amount', 'line_ids.normalized_category', 'line_ids.active')
    def _compute_financial_totals(self):
        for settlement in self:
            lines = settlement.line_ids.filtered('active')
            settlement.calculated_net_amount = sum(lines.mapped('amount'))
            settlement.gross_sales_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'sale').mapped('amount')
            )
            settlement.refund_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'refund').mapped('amount')
            )
            settlement.amazon_fee_amount = sum(
                lines.filtered(lambda line: line.normalized_category in ('amazon_fee', 'fba_fee')).mapped('amount')
            )
            settlement.reimbursement_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'reimbursement').mapped('amount')
            )
            settlement.promotion_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'promotion').mapped('amount')
            )
            settlement.adjustment_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'adjustment').mapped('amount')
            )
            settlement.tax_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'tax').mapped('amount')
            )
            settlement.other_credit_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'other_credit').mapped('amount')
            )
            settlement.other_debit_amount = sum(
                lines.filtered(lambda line: line.normalized_category == 'other_debit').mapped('amount')
            )

    @api.depends('reported_net_amount', 'calculated_net_amount')
    def _compute_reconciliation_difference(self):
        for settlement in self:
            settlement.reconciliation_difference = (
                settlement.reported_net_amount - settlement.calculated_net_amount
            )

    def _compute_link_counts(self):
        sync_logs = self.env['amazon.sync.log'].sudo()
        for settlement in self:
            lines = settlement.line_ids.filtered('active')
            settlement.line_count = len(lines)
            settlement.order_count = len(lines.mapped('amazon_order_record_id'))
            settlement.reimbursement_count = len(lines.mapped('reimbursement_id'))
            settlement.sync_log_count = sync_logs.search_count([
                ('source_model', '=', settlement._name),
                ('source_id', '=', settlement.id),
            ])

    @api.depends(
        'account_move_id', 'account_move_id.state', 'deposit_date',
        'settlement_end_date', 'state', 'reconciliation_state',
        'parsing_error_count', 'currency_mismatch', 'currency_id',
        'line_ids.active', 'line_ids.normalized_category', 'line_ids.currency_id',
        'line_ids.order_link_state', 'line_ids.reimbursement_id.financial_state',
        'instance_id.settlement_journal_id',
        'instance_id.amazon_clearing_account_id',
        'instance_id.amazon_sales_account_id',
        'instance_id.amazon_refund_account_id',
        'instance_id.amazon_fee_account_id',
        'instance_id.amazon_fba_fee_account_id',
        'instance_id.amazon_reimbursement_account_id',
        'instance_id.amazon_promotion_account_id',
        'instance_id.amazon_adjustment_account_id',
        'instance_id.amazon_shipping_account_id',
        'instance_id.amazon_tax_account_id',
        'instance_id.amazon_other_credit_account_id',
        'instance_id.amazon_other_debit_account_id',
    )
    def _compute_accounting_status(self):
        for settlement in self:
            settlement.accounting_date = (
                settlement.account_move_id.date
                if settlement.account_move_id
                else (
                    settlement.deposit_date.date() if settlement.deposit_date
                    else settlement.settlement_end_date.date()
                    if settlement.settlement_end_date else False
                )
            )
            if settlement.account_move_id:
                settlement.accounting_state = (
                    'posted' if settlement.account_move_id.state == 'posted' else 'draft_entry'
                )
                settlement.accounting_mapping_errors = False
                continue
            errors = settlement.sudo()._accounting_validation_errors()
            settlement.accounting_mapping_errors = '\n'.join(errors) or False
            if settlement.reconciliation_state != 'matched':
                settlement.accounting_state = 'not_ready'
            else:
                settlement.accounting_state = 'error' if errors else 'ready'

    def _open_records(self, title, model_name, records):
        self.ensure_one()
        records = records.exists()
        action = {
            'type': 'ir.actions.act_window', 'name': title,
            'res_model': model_name, 'view_mode': 'list,form',
            'domain': [('id', 'in', records.ids)],
        }
        if len(records) == 1:
            action.update({'view_mode': 'form', 'res_id': records.id})
        return action

    def action_view_orders(self):
        return self._open_records(
            _('Amazon Orders'), 'amazon.sale.order',
            self.line_ids.mapped('amazon_order_record_id'),
        )

    def action_view_reimbursements(self):
        return self._open_records(
            _('FBA Reimbursements'), 'amazon.fba.reimbursement',
            self.line_ids.mapped('reimbursement_id'),
        )

    def action_view_sync_logs(self):
        records = self.env['amazon.sync.log'].search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ])
        return self._open_records(_('Sync Logs'), records._name, records)

    def action_view_accounting_entry(self):
        self.ensure_one()
        if not self.account_move_id:
            raise UserError(_('No accounting entry has been created for this settlement.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Amazon Settlement Accounting Entry'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.account_move_id.id,
        }

    @api.model
    def _check_accounting_creation_access(self):
        if self.env.su:
            return
        is_accountant = (
            self.env.user.has_group('account.group_account_user')
            or self.env.user.has_group('account.group_account_manager')
        )
        is_amazon_manager = self.env.user.has_group(
            'sdlc_amazon_connector.group_amazon_manager'
        )
        if not (is_accountant and is_amazon_manager):
            raise AccessError(_(
                'Only an Accounting user who is also an Amazon Manager can create '
                'settlement entries.'
            ))

    def _invoice_receivable_target(self, line):
        """Return (account, partner, error) for already-booked invoice value."""
        self.ensure_one()
        if line.normalized_category not in INVOICE_FINANCIAL_CATEGORIES:
            return self.env['account.account'], self.env['res.partner'], False

        documents = self.env['account.move']
        if line.amazon_order_record_id.invoice_id:
            documents |= line.amazon_order_record_id.invoice_id
        if line.sale_order_id:
            documents |= line.sale_order_id.invoice_ids
        if line.return_line_id.credit_note_id:
            documents |= line.return_line_id.credit_note_id
        documents = documents.exists().filtered(lambda move: move.state != 'cancel')

        refund_transaction = (
            line.normalized_category == 'refund'
            or 'refund' in (line.amazon_transaction_type_raw or '').lower()
        )
        expected_type = 'out_refund' if refund_transaction else 'out_invoice'
        relevant = documents.filtered(lambda move: move.move_type == expected_type)
        if relevant.filtered(lambda move: move.state == 'draft'):
            return self.env['account.account'], self.env['res.partner'], _(
                'Settlement line %(line)s has a draft customer document. Review it before accounting.',
                line=line.display_name,
            )
        posted = relevant.filtered(lambda move: move.state == 'posted')
        if not posted:
            return self.env['account.account'], self.env['res.partner'], False
        receivable_lines = posted.line_ids.filtered(
            lambda move_line: move_line.account_id.account_type == 'asset_receivable'
        )
        accounts = receivable_lines.account_id
        partners = receivable_lines.partner_id
        if len(accounts) != 1 or len(partners) > 1:
            return self.env['account.account'], self.env['res.partner'], _(
                'Settlement line %(line)s has ambiguous posted invoice receivable data.',
                line=line.display_name,
            )
        if not accounts:
            return self.env['account.account'], self.env['res.partner'], _(
                'Settlement line %(line)s links a posted customer document without a receivable line.',
                line=line.display_name,
            )
        return accounts, partners[:1], False

    def _accounting_validation_errors(self):
        self.ensure_one()
        errors = []
        instance = self.instance_id
        company = self.company_id
        lines = self.line_ids.filtered('active')
        if self.state != 'reconciled' or self.reconciliation_state != 'matched':
            errors.append(_('The settlement must be complete and payout-matched.'))
        if self.parsing_error_count or self.currency_mismatch or not self.currency_id:
            errors.append(_('The settlement contains import or currency errors.'))
        if not lines:
            errors.append(_('The settlement has no active financial lines.'))
        if not self.deposit_date and not self.settlement_end_date:
            errors.append(_('The settlement has no deposit date or settlement end date.'))

        journal = instance.settlement_journal_id
        if not journal:
            errors.append(_('Settlement Journal is not configured.'))
        elif journal.company_id != company or journal.type != 'general':
            errors.append(_('Settlement Journal must be a general journal of the instance company.'))

        clearing = instance.amazon_clearing_account_id
        if not clearing:
            errors.append(_('Amazon Clearing Account is not configured.'))
        elif company not in clearing.company_ids:
            errors.append(_('Amazon Clearing Account does not belong to the instance company.'))
        elif clearing.account_type == 'asset_cash':
            errors.append(_('Amazon Clearing Account cannot be a bank or cash account.'))

        for line in lines:
            if line.currency_id != self.currency_id or line.currency_code != self.currency_code:
                errors.append(_(
                    'Settlement line %(line)s has a currency inconsistent with the settlement.',
                    line=line.display_name,
                ))
                continue
            if line.normalized_category == 'unknown':
                errors.append(_(
                    'Unknown Amazon category on %(description)s; configure/classify it before accounting.',
                    description=line.amount_description or line.display_name,
                ))
                continue
            if (
                line.normalized_category in ('sale', 'refund')
                and line.amazon_order_id and line.order_link_state != 'linked'
            ):
                errors.append(_(
                    '%(category)s line for Amazon order %(order)s is not safely linked; invoice double-booking cannot be excluded.',
                    category=line.normalized_category.title(), order=line.amazon_order_id,
                ))
                continue
            invoice_account, _partner, invoice_error = self._invoice_receivable_target(line)
            if invoice_error:
                errors.append(invoice_error)
                continue
            account = invoice_account or instance[ACCOUNT_FIELD_BY_CATEGORY.get(
                line.normalized_category, 'amazon_suspense_account_id'
            )]
            if not account:
                errors.append(_(
                    'No account mapping is configured for category %(category)s.',
                    category=line.normalized_category,
                ))
            elif company not in account.company_ids:
                errors.append(_(
                    'Mapped account %(account)s is outside the instance company.',
                    account=account.display_name,
                ))
            elif account.account_type == 'asset_cash':
                errors.append(_(
                    'Mapped account %(account)s is a bank/cash account; settlement posting only uses Amazon Clearing.',
                    account=account.display_name,
                ))
            if (
                line.reimbursement_id
                and line.reimbursement_id.financial_state == 'posted_later'
            ):
                errors.append(_(
                    'Reimbursement %(reimbursement)s is already marked as financially posted.',
                    reimbursement=line.reimbursement_id.reimbursement_id,
                ))
        return list(dict.fromkeys(errors))

    def _accounting_move_line_values(self, line, move_date):
        self.ensure_one()
        instance = self.instance_id
        company_currency = self.company_id.currency_id
        invoice_account, partner, invoice_error = self._invoice_receivable_target(line)
        if invoice_error:
            raise UserError(invoice_error)
        account = invoice_account or instance[ACCOUNT_FIELD_BY_CATEGORY[line.normalized_category]]
        # Amazon positive values are credits to the financial component and a
        # debit to clearing; negative values reverse that direction.
        balance = self.currency_id._convert(
            -line.amount, company_currency, self.company_id, move_date,
        )
        values = {
            'name': _('Amazon Settlement %(settlement)s | %(category)s | %(description)s',
                      settlement=self.settlement_id,
                      category=line.normalized_category.replace('_', ' ').title(),
                      description=line.amount_description or line.transaction_type or line.line_key[:12]),
            'account_id': account.id,
            'partner_id': partner.id or False,
            'debit': max(balance, 0.0),
            'credit': max(-balance, 0.0),
        }
        if self.currency_id != company_currency:
            values.update({
                'currency_id': self.currency_id.id,
                'amount_currency': -line.amount,
            })
        return values

    def action_create_accounting_entry(self):
        self.ensure_one()
        self._check_accounting_creation_access()
        # Serialize creation for this settlement. Combined with the unique
        # account_move_id constraint this prevents double clicks and workers.
        self.flush_recordset(['account_move_id'])
        self.env.cr.execute(
            'SELECT account_move_id FROM amazon_settlement_report WHERE id = %s FOR UPDATE',
            [self.id],
        )
        existing_move_id = self.env.cr.fetchone()[0]
        if existing_move_id:
            self.invalidate_recordset(['account_move_id'])
            return self.action_view_accounting_entry()

        errors = self._accounting_validation_errors()
        if errors:
            raise UserError(_('Accounting entry creation is blocked:\n- %s', '\n- '.join(errors)))

        move_date = (
            self.deposit_date.date() if self.deposit_date
            else self.settlement_end_date.date()
        )
        component_values = [
            self._accounting_move_line_values(line, move_date)
            for line in self.line_ids.filtered('active')
        ]
        company_currency = self.company_id.currency_id
        component_balance = sum(
            values['debit'] - values['credit'] for values in component_values
        )
        clearing_balance = -component_balance
        clearing_values = {
            'name': _('Amazon Settlement %(settlement)s | Amazon Clearing',
                      settlement=self.settlement_id),
            'account_id': self.instance_id.amazon_clearing_account_id.id,
            'debit': max(clearing_balance, 0.0),
            'credit': max(-clearing_balance, 0.0),
        }
        if self.currency_id != company_currency:
            clearing_values.update({
                'currency_id': self.currency_id.id,
                'amount_currency': self.reported_net_amount,
            })

        total_debit = sum(values['debit'] for values in component_values) + clearing_values['debit']
        total_credit = sum(values['credit'] for values in component_values) + clearing_values['credit']
        if company_currency.compare_amounts(total_debit, total_credit) != 0:
            raise UserError(_(
                'Settlement entry is not balanced. Debit: %(debit)s; Credit: %(credit)s.',
                debit=total_debit, credit=total_credit,
            ))
        amazon_total = sum(self.line_ids.filtered('active').mapped('amount'))
        if self.currency_id.compare_amounts(amazon_total, self.reported_net_amount) != 0:
            raise UserError(_(
                'Amazon Clearing does not match the payout. Reported: %(reported)s; computed: %(computed)s; difference: %(difference)s.',
                reported=self.reported_net_amount, computed=amazon_total,
                difference=self.reported_net_amount - amazon_total,
            ))
        clearing_currency_impact = (
            self.reported_net_amount
            if self.currency_id != company_currency else clearing_balance
        )
        if self.currency_id.compare_amounts(
            clearing_currency_impact, self.reported_net_amount
        ) != 0:
            raise UserError(_(
                'Amazon Clearing impact differs from the payout by %(difference)s %(currency)s.',
                difference=self.reported_net_amount - clearing_currency_impact,
                currency=self.currency_code,
            ))

        move = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'entry',
            'journal_id': self.instance_id.settlement_journal_id.id,
            'company_id': self.company_id.id,
            'date': move_date,
            'ref': _('Amazon Settlement %s', self.settlement_id),
            'amazon_instance_id': self.instance_id.id,
            'line_ids': [
                *(Command.create(values) for values in component_values),
                Command.create(clearing_values),
            ],
        })
        if move.state != 'draft':
            raise UserError(_('Settlement accounting entry was not created in draft.'))
        self.account_move_id = move
        return self.action_view_accounting_entry()

    @api.constrains('account_move_id', 'company_id')
    def _check_account_move_company(self):
        for settlement in self:
            if (
                settlement.account_move_id
                and settlement.account_move_id.company_id != settlement.company_id
            ):
                raise ValidationError(_(
                    'The settlement accounting entry must belong to the settlement company.'
                ))

    def action_download_report(self):
        """Compatibility button: enqueue discovery/download; never block the UI."""
        self.ensure_one()
        self.env['amazon.phase7.job'].enqueue(self.instance_id, 'settlements')
        return self.instance_id._notify(
            _('Settlement Reports'), _('Settlement import was queued.'),
        )

    def action_process_report(self):
        """Relink and recalculate existing imported financial data only."""
        for settlement in self:
            settlement.line_ids._resolve_links()
            settlement._recompute_reconciliation()
        return True

    def action_reconcile(self):
        """Recalculate payout reconciliation without accounting side effects."""
        self.action_process_report()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Settlement Reconciliation'),
                'message': _('The Amazon-reported payout was compared with signed financial lines.'),
                'type': 'success', 'sticky': False,
            },
        }

    def _recompute_reconciliation(self):
        for settlement in self:
            # Recompute now so a just-imported report is reconciled in the
            # same transaction, without depending on a later cache flush.
            settlement._compute_financial_totals()
            settlement._compute_reconciliation_difference()
            if (
                settlement.parsing_error_count or settlement.currency_mismatch
                or not settlement.currency_id or not settlement.line_ids.filtered('active')
            ):
                settlement.write({
                    'state': 'incomplete', 'reconciliation_state': 'incomplete',
                })
                continue
            matched = settlement.currency_id.compare_amounts(
                settlement.reported_net_amount,
                settlement.calculated_net_amount,
            ) == 0
            settlement.write({
                'state': 'reconciled' if matched else 'processed',
                'reconciliation_state': 'matched' if matched else 'mismatch',
            })
        return True

    @api.model
    def _normalize_row(self, row):
        normalized = {
            str(key or '').strip().lstrip('\ufeff').lower().replace('_', '-').replace(' ', '-'): (
                value.strip() if isinstance(value, str) else value
            )
            for key, value in (row or {}).items()
            if key and key != '_extra_fields'
        }
        if row.get('_extra_fields'):
            normalized['_extra_fields'] = row['_extra_fields']
        return normalized

    @api.model
    def _flat_file_rows(self, raw_text):
        text = (raw_text or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        if not text.strip():
            return []
        lines = text.split('\n')
        header_index = False
        required = {'settlement-id', 'amount-type', 'amount-description', 'amount'}
        for index, line in enumerate(lines):
            headers = {
                cell.strip().lstrip('\ufeff').lower().replace('_', '-').replace(' ', '-')
                for cell in line.split('\t')
            }
            if required.issubset(headers):
                header_index = index
                break
        if header_index is False:
            raise ValidationError(_(
                'Unsupported settlement schema: the V2 settlement headers were not found.'
            ))
        reader = csv.DictReader(
            io.StringIO('\n'.join(lines[header_index:])), delimiter='\t',
            quoting=csv.QUOTE_NONE, restkey='_extra_fields', restval='',
        )
        return [
            self._normalize_row(row)
            for row in reader
            if any(value not in (None, '') for value in row.values())
        ]

    @api.model
    def _number(self, value, field_label='amount', optional=False):
        if value in (None, ''):
            if optional:
                return 0.0
            raise ValidationError(_('Amazon omitted required %s.', field_label))
        if isinstance(value, (int, float, Decimal)):
            return float(value)
        text = str(value).strip().replace('\u00a0', '').replace(' ', '').replace("'", '')
        negative = text.startswith('(') and text.endswith(')')
        if negative:
            text = text[1:-1]
        if not re.fullmatch(r'[+-]?[0-9.,]+', text or ''):
            raise ValidationError(_('Amazon returned invalid %s: %s', field_label, value))
        comma = text.rfind(',')
        dot = text.rfind('.')
        if comma >= 0 and dot >= 0:
            decimal_separator = ',' if comma > dot else '.'
            thousands_separator = '.' if decimal_separator == ',' else ','
            text = text.replace(thousands_separator, '')
            text = text.replace(decimal_separator, '.')
        elif comma >= 0:
            parts = text.split(',')
            if len(parts[-1]) in (1, 2):
                text = ''.join(parts[:-1]) + '.' + parts[-1]
            else:
                text = ''.join(parts)
        elif dot >= 0:
            parts = text.split('.')
            if len(parts[-1]) in (1, 2):
                text = ''.join(parts[:-1]) + '.' + parts[-1]
            else:
                text = ''.join(parts)
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValidationError(_('Amazon returned invalid %s: %s', field_label, value)) from exc
        return float(-result if negative else result)

    @api.model
    def _datetime(self, value):
        if not value:
            return False
        try:
            result = date_parser.parse(str(value))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_('Amazon returned an invalid settlement date: %s', value)) from exc
        if result.tzinfo:
            result = result.astimezone(timezone.utc).replace(tzinfo=None)
        return result.replace(microsecond=0)

    @api.model
    def _required_datetime(self, value):
        if value in (None, ''):
            raise ValidationError(_('Amazon omitted a required settlement date.'))
        return self._datetime(value)

    @api.model
    def _currency(self, code):
        return self.env['res.currency'].with_context(active_test=False).search([
            ('name', '=', str(code or '').strip()),
        ], limit=1)

    @api.model
    def _metadata_datetime(self, value):
        return self._datetime(value) if value else False

    @api.model
    def import_flat_file(self, instance, raw_text, report_metadata):
        """Upsert V2 settlements and their signed financial lines.

        The report body, not the Reports API reportId, owns settlement-id and
        total-amount. Valid rows are retained when another row is malformed,
        while reconciliation is forced to INCOMPLETE.
        """
        rows = self._flat_file_rows(raw_text)
        if not rows:
            return {'settlements': self.browse(), 'processed': 0, 'failed': 0}

        carry = {}
        grouped = defaultdict(list)
        unassigned_errors = []
        for row_number, row in enumerate(rows, start=2):
            for field_name in SETTLEMENT_HEADER_FIELDS:
                if row.get(field_name) not in (None, ''):
                    carry[field_name] = row[field_name]
            merged = dict(row)
            for field_name in SETTLEMENT_HEADER_FIELDS:
                if merged.get(field_name) in (None, '') and carry.get(field_name) not in (None, ''):
                    merged[field_name] = carry[field_name]
            settlement_id = str(merged.get('settlement-id') or '').strip()
            if not settlement_id:
                unassigned_errors.append(_('Row %s has no settlement-id.', row_number))
                continue
            grouped[settlement_id].append((row_number, merged))

        if not grouped:
            raise ValidationError(_(
                'The settlement document contains data rows but no settlement-id.'
            ))

        imported = self.browse()
        total_processed = total_failed = total_issue_count = 0
        sync_token = '%s:%s' % (
            report_metadata.get('reportId') or 'report', fields.Datetime.now(),
        )
        for settlement_id, settlement_rows in grouped.items():
            first_row = settlement_rows[0][1]
            errors = list(unassigned_errors)
            currency_code = str(first_row.get('currency') or '').strip()
            currency = self._currency(currency_code)
            if not currency_code or not currency:
                errors.append(_('Unknown or missing settlement currency: %s', currency_code or _('empty')))

            def safely(parse_method, value, label, default):
                try:
                    return parse_method(value)
                except ValidationError as exc:
                    errors.append(_('%s: %s', label, str(exc)))
                    return default

            reported_total = safely(
                lambda value: self._number(value, 'total-amount'),
                first_row.get('total-amount'), 'total-amount', 0.0,
            )
            start_datetime = safely(
                self._required_datetime, first_row.get('settlement-start-date'),
                'settlement-start-date', False,
            )
            end_datetime = safely(
                self._required_datetime, first_row.get('settlement-end-date'),
                'settlement-end-date', False,
            )
            deposit_datetime = safely(
                self._required_datetime, first_row.get('deposit-date'), 'deposit-date', False,
            )

            settlement = self.search([
                ('instance_id', '=', instance.id), ('settlement_id', '=', settlement_id),
            ], limit=1)
            if not settlement and report_metadata.get('reportId'):
                legacy = self.search([
                    ('instance_id', '=', instance.id),
                    ('settlement_id', '=', report_metadata['reportId']),
                ], limit=2)
                settlement = legacy if len(legacy) == 1 else self.browse()

            marketplace_ids = report_metadata.get('marketplaceIds') or []
            marketplace_value = (
                ','.join(marketplace_ids)
                if isinstance(marketplace_ids, list) else str(marketplace_ids or '')
            )
            now = fields.Datetime.now()
            header_values = {
                'instance_id': instance.id,
                'settlement_id': settlement_id,
                'settlement_start_date': start_datetime,
                'settlement_end_date': end_datetime,
                'deposit_date': deposit_datetime,
                'start_date': start_datetime.date() if start_datetime else False,
                'end_date': end_datetime.date() if end_datetime else False,
                'currency_id': currency.id or False,
                'currency_code': currency_code,
                'reported_net_amount': reported_total,
                'report_id': report_metadata.get('reportId') or '',
                'report_document_id': report_metadata.get('reportDocumentId') or '',
                'report_type': report_metadata.get('reportType') or '',
                'processing_status': report_metadata.get('processingStatus') or '',
                'created_time': self._metadata_datetime(report_metadata.get('createdTime')),
                'processing_start_time': self._metadata_datetime(
                    report_metadata.get('processingStartTime')
                ),
                'processing_end_time': self._metadata_datetime(
                    report_metadata.get('processingEndTime')
                ),
                'marketplace_ids': marketplace_value,
                'last_synced_at': now,
                'raw_header_source': json.dumps(first_row, default=str, sort_keys=True)[:20000],
                'state': 'imported', 'reconciliation_state': 'pending',
            }
            if settlement:
                settlement.write(header_values)
            else:
                settlement = self.create({
                    **header_values, 'imported_at': now,
                })

            dimension_counts = defaultdict(int)
            seen_keys = set()
            for row_number, row in settlement_rows:
                try:
                    with self.env.cr.savepoint():
                        if row.get('_extra_fields'):
                            errors.append(_(
                                'Row %s has unexpected trailing columns.', row_number,
                            ))
                        amount = self._number(row.get('amount'), 'amount')
                        row_currency = str(row.get('currency') or currency_code).strip()
                        if row_currency != currency_code:
                            errors.append(_(
                                'Row currency %s differs from settlement currency %s.',
                                row_currency or _('empty'), currency_code or _('empty'),
                            ))
                        row_currency_record = self._currency(row_currency)
                        if not row_currency_record:
                            errors.append(_('Unknown row currency: %s', row_currency or _('empty')))
                        if row.get('total-amount') not in (None, ''):
                            row_total = self._number(row.get('total-amount'), 'total-amount')
                            if currency and currency.compare_amounts(row_total, reported_total) != 0:
                                errors.append(_(
                                    'Row %s reports a different settlement total: %s.',
                                    row_number, row.get('total-amount'),
                                ))
                        dimensions = [str(row.get(name) or '').strip() for name in SETTLEMENT_LINE_DIMENSIONS]
                        dimension_digest = hashlib.sha256(
                            '|'.join(dimensions).encode()
                        ).hexdigest()
                        occurrence = dimension_counts[dimension_digest]
                        dimension_counts[dimension_digest] += 1
                        line_key = hashlib.sha256(
                            ('%s|%s|%s|%s' % (
                                instance.id, settlement_id, dimension_digest, occurrence,
                            )).encode()
                        ).hexdigest()
                        settlement._upsert_line(
                            row, line_key, amount, row_currency_record, row_currency,
                            report_metadata, sync_token,
                        )
                        seen_keys.add(line_key)
                        total_processed += 1
                except Exception as exc:
                    total_failed += 1
                    errors.append(_('Row %s: %s', row_number, str(exc)[:1000]))
                    _logger.exception(
                        'Settlement row %s failed for %s', row_number, settlement_id,
                    )

            if not seen_keys:
                errors.append(_('No valid financial lines were imported.'))
            if not errors:
                settlement.line_ids.filtered(
                    lambda line: line.line_key not in seen_keys
                ).write({'active': False})
            settlement.write({
                'parsing_error_count': len(errors),
                'parsing_error_log': '\n'.join(errors)[-10000:] or False,
                'currency_mismatch': any('currency' in str(error).lower() for error in errors),
            })
            settlement._recompute_reconciliation()
            imported |= settlement
            total_issue_count += len(errors)

        return {
            'settlements': imported,
            'processed': total_processed,
            'failed': total_failed + total_issue_count,
        }

    def _upsert_line(self, row, line_key, amount, currency, currency_code,
                     report_metadata, sync_token):
        self.ensure_one()
        # Include inactive stale lines so an Amazon refresh can reactivate the
        # same deterministic identity instead of violating uniqueness.
        line_model = self.env['amazon.settlement.report.line'].with_context(
            active_test=False,
        )
        line = line_model.search([
            ('report_id', '=', self.id), ('line_key', '=', line_key),
        ], limit=1)
        if not line:
            legacy = line_model.search([
                ('report_id', '=', self.id),
                ('order_id_ref', '=', row.get('order-id') or ''),
                ('transaction_type', '=', row.get('transaction-type') or ''),
                ('amount_type', '=', row.get('amount-type') or ''),
                ('amount_description', '=', row.get('amount-description') or ''),
                ('order_item_id', '=', row.get('order-item-code') or ''),
            ], limit=2)
            line = legacy if len(legacy) == 1 else line_model.browse()
        quantity = self._number(
            row.get('quantity-purchased'), 'quantity-purchased', optional=True,
        )
        category, fee_category = line_model._classify(
            row.get('transaction-type'), row.get('amount-type'),
            row.get('amount-description'), row.get('promotion-id'), amount,
        )
        posted_date = self._datetime(row.get('posted-date'))
        posted_datetime = self._datetime(
            row.get('posted-date-time') or row.get('posted-date')
        )
        vals = {
            'report_id': self.id, 'line_key': line_key, 'active': True,
            'amazon_order_id': row.get('order-id') or '',
            'order_id_ref': row.get('order-id') or '',
            'merchant_order_id': row.get('merchant-order-id') or '',
            'adjustment_id': row.get('adjustment-id') or '',
            'shipment_id': row.get('shipment-id') or '',
            'marketplace_name': row.get('marketplace-name') or '',
            'transaction_type': row.get('transaction-type') or '',
            'amazon_transaction_type_raw': row.get('transaction-type') or '',
            'amount_type': row.get('amount-type') or '',
            'amazon_amount_type_raw': row.get('amount-type') or '',
            'amount_description': row.get('amount-description') or '',
            'amazon_amount_description_raw': row.get('amount-description') or '',
            'amount': amount,
            'currency_id': currency.id or False,
            'currency_code': currency_code,
            'fulfillment_id': row.get('fulfillment-id') or '',
            'posted_date': posted_date,
            'posted_date_time': posted_datetime,
            'order_item_id': row.get('order-item-code') or '',
            'merchant_order_item_id': row.get('merchant-order-item-id') or '',
            'merchant_adjustment_item_id': row.get('merchant-adjustment-item-id') or '',
            'sku': row.get('sku') or '',
            'quantity_purchased': quantity,
            'promotion_id': row.get('promotion-id') or '',
            'normalized_category': category,
            'fee_category': fee_category,
            'source_report_id': report_metadata.get('reportId') or '',
            'source_document_id': report_metadata.get('reportDocumentId') or '',
            'last_seen_sync_token': sync_token,
            'raw_report_row': json.dumps(row, default=str, sort_keys=True)[:20000],
        }
        vals.update(line_model._order_and_product_values(self.instance_id, row))
        if line:
            line.write(vals)
        else:
            line = line_model.create(vals)
        line._resolve_links()
        return line


class AmazonSettlementReportLine(models.Model):
    _name = 'amazon.settlement.report.line'
    _description = 'Amazon Settlement Financial Line'
    _order = 'posted_date_time, id'
    _check_company_auto = True

    report_id = fields.Many2one(
        'amazon.settlement.report', required=True, ondelete='cascade',
        index=True, check_company=True,
    )
    instance_id = fields.Many2one(
        related='report_id.instance_id', store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        related='report_id.company_id', store=True, readonly=True, index=True,
    )
    line_key = fields.Char(readonly=True, copy=False, index=True)
    active = fields.Boolean(default=True, index=True)

    amazon_order_id = fields.Char(index=True)
    order_id_ref = fields.Char('Amazon Order ID', index=True)
    merchant_order_id = fields.Char(index=True)
    adjustment_id = fields.Char(index=True)
    shipment_id = fields.Char(index=True)
    marketplace_name = fields.Char(index=True)
    transaction_type = fields.Char(index=True)
    amazon_transaction_type_raw = fields.Char(readonly=True, index=True)
    amount_type = fields.Char(index=True)
    amazon_amount_type_raw = fields.Char(readonly=True, index=True)
    amount_description = fields.Char('Amount Description', index=True)
    amazon_amount_description_raw = fields.Char(readonly=True, index=True)
    amount = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', ondelete='restrict')
    currency_code = fields.Char(index=True)
    fulfillment_id = fields.Char(index=True)
    posted_date = fields.Datetime(index=True)
    posted_date_time = fields.Datetime(index=True)
    order_item_id = fields.Char('Amazon Order Item Code', index=True)
    merchant_order_item_id = fields.Char(index=True)
    merchant_adjustment_item_id = fields.Char(index=True)
    sku = fields.Char(index=True)
    quantity_purchased = fields.Float()
    promotion_id = fields.Char(index=True)

    normalized_category = fields.Selection([
        ('sale', 'Sale'), ('amazon_fee', 'Amazon Fee'), ('fba_fee', 'FBA Fee'),
        ('refund', 'Refund'), ('promotion', 'Promotion'),
        ('reimbursement', 'Reimbursement'), ('adjustment', 'Adjustment'),
        ('tax', 'Tax'), ('shipping', 'Shipping'),
        ('other_credit', 'Other Credit'), ('other_debit', 'Other Debit'),
        ('unknown', 'Unknown'),
    ], default='unknown', required=True, index=True)
    fee_category = fields.Selection([
        ('referral_commission', 'Referral / Commission'),
        ('fba_fulfillment', 'FBA Fulfillment Fee'),
        ('storage', 'Storage Fee'),
        ('shipping_service', 'Shipping / Service Fee'),
        ('refund_administration', 'Refund Administration Fee'),
        ('other_amazon_fee', 'Other Amazon Fee'),
    ], index=True)

    amazon_order_record_id = fields.Many2one(
        'amazon.sale.order', string='Amazon Order', ondelete='set null',
        index=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order', string='Odoo Sale Order', ondelete='set null',
        index=True, check_company=True,
    )
    order_link_state = fields.Selection([
        ('not_applicable', 'No Order Reported'), ('linked', 'Order Linked'),
        ('order_not_found', 'Order Not Found'), ('ambiguous', 'Ambiguous Order'),
    ], default='not_applicable', required=True, index=True)
    amazon_product_id = fields.Many2one('amazon.product', ondelete='set null', index=True)
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    return_line_id = fields.Many2one(
        'amazon.return.report.line', string='Linked Financial Refund Return',
        ondelete='set null', index=True, check_company=True,
    )
    reimbursement_id = fields.Many2one(
        'amazon.fba.reimbursement', string='Linked FBA Reimbursement',
        ondelete='set null', index=True, check_company=True,
    )
    matching_note = fields.Text(readonly=True)

    # Upgrade-only accounting pointer. Settlement import never populates it.
    invoice_id = fields.Many2one('account.move', string='Legacy Invoice', readonly=True)
    source_report_id = fields.Char(readonly=True, index=True)
    source_document_id = fields.Char(readonly=True, index=True)
    last_seen_sync_token = fields.Char(readonly=True, index=True)
    raw_report_row = fields.Text(
        readonly=True, groups='sdlc_amazon_connector.group_amazon_technical_admin',
    )

    _line_unique = models.Constraint(
        'UNIQUE(report_id, line_key)',
        'This settlement financial line was already imported.',
    )

    @api.model
    def _classify(self, transaction_type, amount_type, description,
                  promotion_id, amount):
        transaction = str(transaction_type or '').strip().lower()
        amount_group = str(amount_type or '').strip().lower()
        detail = str(description or '').strip().lower()
        text = ' '.join((transaction, amount_group, detail))

        if 'reimburse' in text:
            return 'reimbursement', False
        if promotion_id or 'promotion' in amount_group or 'promotion' in detail:
            return 'promotion', False
        if 'adjust' in transaction or 'adjust' in amount_group:
            return 'adjustment', False
        fee_evidence = (
            'fee' in amount_group or 'fee' in detail
            or any(token in detail for token in ('commission', 'referral'))
        )
        if fee_evidence:
            if any(token in detail for token in ('commission', 'referral')):
                fee_category = 'referral_commission'
            elif 'storage' in detail:
                fee_category = 'storage'
            elif 'refund' in detail:
                fee_category = 'refund_administration'
            elif any(token in detail for token in ('fba', 'fulfillment')):
                fee_category = 'fba_fulfillment'
            elif any(token in detail for token in ('shipping', 'service')):
                fee_category = 'shipping_service'
            else:
                fee_category = 'other_amazon_fee'
            category = 'fba_fee' if fee_category in ('fba_fulfillment', 'storage') else 'amazon_fee'
            return category, fee_category
        if 'refund' in transaction:
            return 'refund', False
        if 'tax' in amount_group or 'tax' in detail:
            return 'tax', False
        if any(token in amount_group for token in ('itemprice', 'item price', 'principal')):
            return 'sale', False
        if 'shipping' in amount_group or 'shipping' in detail:
            return 'shipping', False
        if 'other' in transaction or 'other' in amount_group:
            return ('other_credit' if amount >= 0 else 'other_debit'), False
        return 'unknown', False

    @api.model
    def _order_and_product_values(self, instance, row):
        order_id = str(row.get('order-id') or '').strip()
        order = self.env['amazon.sale.order']
        order_state = 'not_applicable'
        if order_id:
            candidates = order.search([
                ('instance_id', '=', instance.id), ('amazon_order_ref', '=', order_id),
            ], limit=2)
            if len(candidates) == 1:
                order = candidates
                order_state = 'linked'
            elif candidates:
                order_state = 'ambiguous'
            else:
                order_state = 'order_not_found'
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            instance, row.get('sku'), False, False,
        )
        return {
            'amazon_order_record_id': order.id or False,
            'sale_order_id': order.sale_order_id.id if order.sale_order_id else False,
            'order_link_state': order_state,
            **product_values,
        }

    def _resolve_links(self):
        for line in self:
            notes = []
            if line.order_link_state == 'order_not_found':
                notes.append(_('ORDER NOT FOUND: %s', line.amazon_order_id))

            return_line = self.env['amazon.return.report.line']
            if line.normalized_category == 'refund' and line.amazon_order_id:
                candidates = return_line.search([
                    ('instance_id', '=', line.instance_id.id),
                    ('amazon_order_id', '=', line.amazon_order_id),
                ], limit=20)
                if line.sku:
                    candidates = candidates.filtered(lambda item: item.sku == line.sku)
                if line.order_item_id:
                    candidates = candidates.filtered(
                        lambda item: item.amazon_order_item_id == line.order_item_id
                    )
                if len(candidates) == 1:
                    return_line = candidates
                    notes.append(_('Refund linked by Amazon order and item identity.'))
                elif len(candidates) > 1:
                    notes.append(_('Refund return match is ambiguous.'))

            reimbursement = self.env['amazon.fba.reimbursement']
            if line.normalized_category == 'reimbursement':
                identifiers = list(dict.fromkeys(filter(None, (
                    line.adjustment_id, line.merchant_adjustment_item_id,
                ))))
                if identifiers:
                    candidates = reimbursement.search([
                        ('instance_id', '=', line.instance_id.id),
                        '|', ('reimbursement_id', 'in', identifiers),
                        ('original_reimbursement_id', 'in', identifiers),
                    ], limit=20)
                    if line.sku:
                        exact = candidates.filtered(lambda item: item.sku == line.sku)
                        candidates = exact or candidates
                    if len(candidates) == 1:
                        reimbursement = candidates
                        notes.append(_('Reimbursement linked by explicit adjustment identifier.'))
                    elif len(candidates) > 1:
                        notes.append(_('Reimbursement match is ambiguous.'))
                else:
                    notes.append(_('Reimbursement has no explicit adjustment identifier.'))

            line.write({
                'return_line_id': return_line.id or False,
                'reimbursement_id': reimbursement.id or False,
                'matching_note': '\n'.join(notes) or False,
            })
            if reimbursement and not reimbursement.linked_settlement_id:
                reimbursement.linked_settlement_id = line.report_id
        return True

    @api.constrains(
        'amazon_order_record_id', 'sale_order_id', 'return_line_id', 'reimbursement_id',
    )
    def _check_instance_links(self):
        for line in self:
            for record in (line.amazon_order_record_id, line.return_line_id, line.reimbursement_id):
                if record and record.instance_id != line.instance_id:
                    raise ValidationError(_('A settlement line cannot link across Amazon instances.'))
            if line.sale_order_id and line.sale_order_id.company_id != line.company_id:
                raise ValidationError(_('A settlement line cannot link across companies.'))
