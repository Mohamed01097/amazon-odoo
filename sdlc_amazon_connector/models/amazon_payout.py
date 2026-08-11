from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


PAYOUT_STATES = [
    ('pending', 'Pending'),
    ('matched', 'Matched'),
    ('draft_receipt', 'Draft Receipt'),
    ('partially_paid', 'Partially Paid'),
    ('paid', 'Paid / Reconciled'),
    ('mismatch', 'Mismatch'),
    ('ambiguous', 'Ambiguous'),
    ('error', 'Error'),
]


class AmazonPayout(models.Model):
    _name = 'amazon.payout'
    _description = 'Amazon Bank Payout'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'payout_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(default='New', required=True, readonly=True, copy=False)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='restrict', index=True,
        check_company=True, tracking=True,
    )
    company_id = fields.Many2one(
        related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    source = fields.Selection([
        ('manual_confirmation', 'Manual Confirmation'),
        ('bank_transaction', 'Existing Odoo Bank Transaction'),
    ], required=True, default='manual_confirmation', tracking=True, index=True)
    source_reference = fields.Char(
        tracking=True, copy=False,
        help='Bank reference or manual evidence reference. It is not an Amazon API confirmation.',
    )
    external_payout_id = fields.Char(
        string='External Payout / Disbursement ID', copy=False, index=True,
        help='Populate only when an authoritative external source provides this identifier.',
    )
    payout_date = fields.Date(required=True, tracking=True, index=True)
    currency_id = fields.Many2one(
        'res.currency', required=True, ondelete='restrict', tracking=True,
    )
    expected_payout_amount = fields.Monetary(
        compute='_compute_amounts', currency_field='currency_id',
    )
    actual_received_amount = fields.Monetary(
        currency_field='currency_id', tracking=True,
    )
    allocated_received_amount = fields.Monetary(
        compute='_compute_amounts', currency_field='currency_id',
    )
    difference_amount = fields.Monetary(
        compute='_compute_amounts', currency_field='currency_id',
    )
    unallocated_amount = fields.Monetary(
        compute='_compute_amounts', currency_field='currency_id',
    )
    state = fields.Selection(PAYOUT_STATES, compute='_compute_state', string='Payout State')
    matching_state = fields.Selection([
        ('unmatched', 'Unmatched'), ('matched', 'Matched'),
        ('ambiguous', 'Ambiguous'), ('manually_matched', 'Manually Matched'),
    ], default='unmatched', required=True, tracking=True, index=True)
    matching_explanation = fields.Text(tracking=True)
    error_message = fields.Text(readonly=True)
    notes = fields.Text()

    bank_journal_id = fields.Many2one(
        'account.journal', string='Payout Bank Journal', check_company=True,
        ondelete='restrict', domain="[('company_id', '=', company_id), ('type', '=', 'bank')]",
        tracking=True,
    )
    bank_statement_line_id = fields.Many2one(
        'account.bank.statement.line', string='Existing Bank Transaction',
        ondelete='restrict', check_company=True, copy=False, tracking=True,
        domain="[('company_id', '=', company_id), ('journal_id.type', '=', 'bank')]",
    )
    receipt_move_id = fields.Many2one(
        'account.move', string='Bank Receipt Entry', ondelete='restrict',
        check_company=True, readonly=True, copy=False, tracking=True,
    )
    receipt_move_state = fields.Selection(
        related='receipt_move_id.state', string='Bank Receipt State', readonly=True,
    )
    allocation_ids = fields.One2many(
        'amazon.payout.allocation', 'payout_id', string='Settlement Allocations',
        copy=False,
    )
    settlement_ids = fields.Many2many(
        'amazon.settlement.report', compute='_compute_settlement_ids',
        string='Settlements', readonly=True,
    )
    settlement_count = fields.Integer(compute='_compute_settlement_ids')
    confirmed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    confirmed_at = fields.Datetime(readonly=True, copy=False)
    reconciled_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    reconciled_at = fields.Datetime(readonly=True, copy=False)

    _external_payout_unique = models.Constraint(
        'UNIQUE(instance_id, external_payout_id)',
        'This external payout/disbursement ID already exists for the Amazon instance.',
    )
    _bank_transaction_unique = models.Constraint(
        'UNIQUE(bank_statement_line_id)',
        'This Odoo bank transaction is already linked to an Amazon payout.',
    )
    _receipt_move_unique = models.Constraint(
        'UNIQUE(receipt_move_id)',
        'This bank receipt entry is already linked to an Amazon payout.',
    )
    _source_reference_unique = models.Constraint(
        'UNIQUE(instance_id, source, source_reference)',
        'This payout evidence reference already exists for the Amazon instance.',
    )
    _actual_non_negative = models.Constraint(
        'CHECK(actual_received_amount >= 0)',
        'Actual payout receipt amount cannot be negative.',
    )

    @api.model
    def _check_payout_access(self):
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
                'Only an Accounting user who is also an Amazon Manager can manage Amazon payouts.'
            ))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_payout_access()
        for vals in vals_list:
            bank_transaction_id = vals.get('bank_statement_line_id')
            if bank_transaction_id and self.sudo().search_count([
                ('bank_statement_line_id', '=', bank_transaction_id),
            ]):
                raise ValidationError(_(
                    'This Odoo bank transaction is already linked to an Amazon payout.'
                ))
        records = super().create(vals_list)
        for payout in records:
            if payout.name == 'New':
                payout.name = self.env['ir.sequence'].next_by_code('amazon.payout') or 'New'
        return records

    def write(self, vals):
        self._check_payout_access()
        protected = {
            'instance_id', 'source', 'currency_id', 'actual_received_amount',
            'bank_journal_id', 'bank_statement_line_id', 'payout_date',
            'source_reference', 'external_payout_id',
        }
        if protected.intersection(vals) and any(payout.receipt_move_id for payout in self):
            raise UserError(_(
                'Payout evidence and amounts cannot be changed after a bank receipt is linked.'
            ))
        return super().write(vals)

    def unlink(self):
        self._check_payout_access()
        if any(payout.receipt_move_id for payout in self):
            raise UserError(_('A payout linked to a bank receipt cannot be deleted.'))
        return super().unlink()

    @api.depends('allocation_ids.settlement_id')
    def _compute_settlement_ids(self):
        for payout in self:
            payout.settlement_ids = payout.allocation_ids.settlement_id
            payout.settlement_count = len(payout.settlement_ids)

    @api.depends(
        'actual_received_amount', 'allocation_ids.expected_amount',
        'allocation_ids.allocated_amount',
    )
    def _compute_amounts(self):
        for payout in self:
            payout.expected_payout_amount = sum(payout.allocation_ids.mapped('expected_amount'))
            payout.allocated_received_amount = sum(payout.allocation_ids.mapped('allocated_amount'))
            payout.difference_amount = (
                payout.actual_received_amount - payout.expected_payout_amount
            )
            payout.unallocated_amount = (
                payout.actual_received_amount - payout.allocated_received_amount
            )

    @api.depends(
        'matching_state', 'receipt_move_id', 'receipt_move_id.state',
        'actual_received_amount', 'expected_payout_amount',
        'allocation_ids.reconciled_amount', 'allocation_ids.allocated_amount',
    )
    def _compute_state(self):
        for payout in self:
            if payout.matching_state == 'ambiguous':
                payout.state = 'ambiguous'
            elif not payout.allocation_ids:
                payout.state = 'pending'
            elif payout.receipt_move_id and payout.receipt_move_id.state == 'draft':
                payout.state = 'draft_receipt'
            elif not payout.receipt_move_id:
                payout.state = (
                    'matched' if payout.matching_state in ('matched', 'manually_matched')
                    else 'pending'
                )
            elif payout.receipt_move_id.state != 'posted':
                payout.state = 'error'
            else:
                comparison = payout.currency_id.compare_amounts(
                    payout.actual_received_amount, payout.expected_payout_amount,
                )
                fully_reconciled = all(
                    payout.currency_id.compare_amounts(
                        allocation.reconciled_amount, allocation.allocated_amount,
                    ) >= 0
                    for allocation in payout.allocation_ids
                )
                if comparison > 0:
                    payout.state = 'mismatch'
                elif comparison < 0:
                    payout.state = 'partially_paid'
                elif fully_reconciled:
                    payout.state = 'paid'
                else:
                    payout.state = 'matched'

    @api.onchange('actual_received_amount')
    def _onchange_actual_received_amount(self):
        for payout in self:
            if len(payout.allocation_ids) == 1 and not payout.receipt_move_id:
                payout.allocation_ids.allocated_amount = payout.actual_received_amount

    @api.constrains('instance_id', 'currency_id', 'bank_journal_id', 'bank_statement_line_id')
    def _check_company_and_currency(self):
        for payout in self:
            if payout.bank_journal_id and (
                payout.bank_journal_id.company_id != payout.company_id
                or payout.bank_journal_id.type != 'bank'
            ):
                raise ValidationError(_(
                    'The payout bank journal must be a bank journal of the Amazon instance company.'
                ))
            if payout.bank_statement_line_id and (
                payout.bank_statement_line_id.company_id != payout.company_id
                or payout.bank_statement_line_id.journal_id.type != 'bank'
            ):
                raise ValidationError(_(
                    'The selected bank transaction must belong to a bank journal of the payout company.'
                ))
            for allocation in payout.allocation_ids:
                if allocation.settlement_id.instance_id != payout.instance_id:
                    raise ValidationError(_(
                        'Every payout settlement must belong to the exact Amazon instance.'
                    ))
                if allocation.settlement_id.currency_id != payout.currency_id:
                    raise ValidationError(_('Settlement and payout currencies must match.'))

    def _validate_allocations(self):
        self.ensure_one()
        if not self.allocation_ids:
            raise UserError(_('Add at least one settlement allocation.'))
        if self.actual_received_amount <= 0:
            raise UserError(_('Actual received amount must be greater than zero.'))
        if self.currency_id.compare_amounts(
            self.allocated_received_amount, self.actual_received_amount,
        ) != 0:
            raise UserError(_(
                'Settlement allocations must equal the actual receipt. Actual: '
                '%(actual)s; allocated: %(allocated)s; difference: %(difference)s.',
                actual=self.actual_received_amount,
                allocated=self.allocated_received_amount,
                difference=self.unallocated_amount,
            ))
        if self.currency_id != self.company_id.currency_id:
            raise UserError(_(
                'Cross-currency Amazon payout clearing is not enabled. Use matching '
                'settlement, bank, and company currency without inventing an exchange rate.'
            ))
        clearing = self.instance_id.amazon_clearing_account_id
        if not clearing or self.company_id not in clearing.company_ids:
            raise UserError(_('Configure the company Amazon Clearing Account first.'))
        if not clearing.reconcile:
            raise UserError(_('Amazon Clearing Account must allow reconciliation.'))
        for allocation in self.allocation_ids:
            settlement = allocation.settlement_id
            if settlement.company_id != self.company_id or settlement.instance_id != self.instance_id:
                raise UserError(_('Payout allocation crosses company or Amazon instance.'))
            if settlement.currency_id != self.currency_id:
                raise UserError(_('Settlement and payout currencies must match.'))
            if not settlement.account_move_id or settlement.account_move_id.state != 'posted':
                raise UserError(_(
                    'Settlement %(settlement)s accounting entry must be posted first.',
                    settlement=settlement.settlement_id,
                ))
            settlement._ensure_settlement_clearing_line()
            if not settlement.clearing_move_line_id:
                raise UserError(_(
                    'Settlement %(settlement)s has no unique linked Amazon Clearing line.',
                    settlement=settlement.settlement_id,
                ))
            open_amount = settlement._settlement_clearing_residual()
            if settlement.currency_id.is_zero(open_amount):
                raise UserError(_(
                    'Settlement %(settlement)s Amazon Clearing is already fully reconciled.',
                    settlement=settlement.settlement_id,
                ))

            # A first authoritative receipt can legitimately reveal an
            # overpayment. Once another receipt is already linked, however,
            # an allocation beyond the open clearing amount is more likely a
            # duplicate than new payout evidence and must be reviewed first.
            other_linked_allocations = settlement.payout_allocation_ids.filtered(
                lambda other: (
                    other != allocation
                    and other.payout_id.receipt_move_id
                    and other.payout_id.receipt_move_id.state != 'cancel'
                )
            )
            if other_linked_allocations and self.currency_id.compare_amounts(
                allocation.allocated_amount, open_amount,
            ) > 0:
                raise UserError(_(
                    'Allocation for settlement %(settlement)s exceeds its open Amazon '
                    'Clearing amount by %(difference)s %(currency)s. Existing payout '
                    'evidence prevents treating this as a new overpayment.',
                    settlement=settlement.settlement_id,
                    difference=allocation.allocated_amount - open_amount,
                    currency=self.currency_id.name,
                ))

    def action_create_draft_receipt(self):
        self.ensure_one()
        self._check_payout_access()
        if self.source != 'manual_confirmation':
            raise UserError(_('Draft receipts are only created for manual confirmation evidence.'))
        if self.receipt_move_id:
            return self.action_view_receipt()
        if not (self.source_reference or '').strip():
            raise UserError(_(
                'Enter the actual bank receipt reference before creating the draft receipt.'
            ))
        self._validate_allocations()
        journal = self.bank_journal_id or self.instance_id.amazon_payout_bank_journal_id
        if not journal or journal.company_id != self.company_id or journal.type != 'bank':
            raise UserError(_('Configure a bank journal for Amazon payouts.'))
        journal_currency = journal.currency_id or journal.company_id.currency_id
        if journal_currency != self.currency_id:
            raise UserError(_('Bank journal and payout currencies must match.'))
        if not journal.default_account_id or journal.default_account_id.account_type != 'asset_cash':
            raise UserError(_('The payout bank journal requires a bank/cash default account.'))

        allocation_commands = []
        for allocation in self.allocation_ids:
            allocation_commands.append(Command.create({
                'name': _('Amazon Payout %(payout)s | Settlement %(settlement)s | Allocation %(allocation)s',
                          payout=self.name, settlement=allocation.settlement_id.settlement_id,
                          allocation=allocation.id),
                'account_id': self.instance_id.amazon_clearing_account_id.id,
                'debit': 0.0,
                'credit': allocation.allocated_amount,
            }))
        move = self.env['account.move'].with_company(self.company_id).create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'company_id': self.company_id.id,
            'date': self.payout_date,
            'ref': _('Amazon Payout %(payout)s | %(reference)s',
                     payout=self.name, reference=self.source_reference),
            'amazon_instance_id': self.instance_id.id,
            'line_ids': [
                Command.create({
                    'name': _('Amazon Payout %(payout)s | Bank Receipt', payout=self.name),
                    'account_id': journal.default_account_id.id,
                    'debit': self.actual_received_amount,
                    'credit': 0.0,
                }),
                *allocation_commands,
            ],
        })
        if move.state != 'draft':
            raise UserError(_('Connector-created payout receipt must remain draft.'))
        self.with_context(allow_payout_evidence_write=True).write({
            'bank_journal_id': journal.id,
            'receipt_move_id': move.id,
            'matching_state': 'manually_matched',
            'matching_explanation': _(
                'Manual receipt evidence recorded by %(user)s; accounting entry awaits review and posting.',
                user=self.env.user.display_name,
            ),
            'confirmed_by_id': self.env.user.id,
            'confirmed_at': fields.Datetime.now(),
        })
        for allocation in self.allocation_ids:
            marker = '| Allocation %s' % allocation.id
            receipt_line = move.line_ids.filtered(
                lambda line: (
                    line.account_id == self.instance_id.amazon_clearing_account_id
                    and marker in (line.name or '')
                )
            )
            if len(receipt_line) != 1:
                raise UserError(_('Could not identify the exact payout clearing allocation line.'))
            allocation.receipt_clearing_move_line_id = receipt_line
        return self.action_view_receipt()

    def action_confirm_bank_transaction(self):
        self.ensure_one()
        self._check_payout_access()
        if self.source != 'bank_transaction' or not self.bank_statement_line_id:
            raise UserError(_('Select an existing Odoo bank transaction first.'))
        if self.receipt_move_id:
            return self.action_view_receipt()
        bank_line = self.bank_statement_line_id
        if bank_line.move_id.state != 'posted':
            raise UserError(_('The existing Odoo bank transaction must be posted.'))
        if bank_line.foreign_currency_id:
            raise UserError(_('Cross-currency bank transactions require manual accounting review.'))
        if bank_line.currency_id != self.currency_id:
            raise UserError(_('Bank transaction and payout currencies must match.'))
        if bank_line.amount <= 0:
            raise UserError(_('The selected bank transaction is not a positive bank receipt.'))
        if self.actual_received_amount and self.currency_id.compare_amounts(
            self.actual_received_amount, bank_line.amount,
        ) != 0:
            raise UserError(_('Actual amount differs from the selected bank transaction.'))
        self.actual_received_amount = bank_line.amount
        if len(self.allocation_ids) == 1:
            self.allocation_ids.allocated_amount = bank_line.amount
        self._validate_allocations()
        clearing_lines = bank_line.move_id.line_ids.filtered(
            lambda line: line.account_id == self.instance_id.amazon_clearing_account_id
        )
        if len(clearing_lines) != 1:
            raise UserError(_(
                'The existing bank transaction must contain exactly one counterpart '
                'line on the configured Amazon Clearing Account. Reconcile/classify '
                'the bank transaction in standard Odoo first.'
            ))
        clearing_line = clearing_lines
        clearing_amount = abs(
            clearing_line.amount_currency
            if clearing_line.currency_id == self.currency_id
            else clearing_line.balance
        )
        if self.currency_id.compare_amounts(
            clearing_amount, self.actual_received_amount,
        ) != 0:
            raise UserError(_(
                'The existing bank transaction Amazon Clearing counterpart is '
                '%(clearing)s %(currency)s, but the bank receipt is %(actual)s. '
                'Classify the bank transaction exactly without an automatic write-off.',
                clearing=clearing_amount, actual=self.actual_received_amount,
                currency=self.currency_id.name,
            ))
        allowed_settlement_lines = self.allocation_ids.mapped(
            'settlement_id.clearing_move_line_id'
        )
        already_matched_lines = (
            clearing_line.matched_debit_ids.debit_move_id
            | clearing_line.matched_debit_ids.credit_move_id
            | clearing_line.matched_credit_ids.debit_move_id
            | clearing_line.matched_credit_ids.credit_move_id
        ) - clearing_line
        if already_matched_lines - allowed_settlement_lines:
            raise UserError(_(
                'The selected bank transaction Amazon Clearing line is already '
                'reconciled against unrelated journal items.'
            ))
        if len(self.allocation_ids) > 1:
            for allocation in self.allocation_ids:
                open_amount = allocation.settlement_id._settlement_clearing_residual()
                if self.currency_id.compare_amounts(allocation.allocated_amount, open_amount) != 0:
                    raise UserError(_(
                        'A single existing bank clearing line can cover multiple settlements '
                        'only when every settlement is fully allocated.'
                    ))
        self.with_context(allow_payout_evidence_write=True).write({
            'bank_journal_id': bank_line.journal_id.id,
            'receipt_move_id': bank_line.move_id.id,
            'source_reference': bank_line.payment_ref or self.source_reference or self.name,
            'payout_date': bank_line.date,
            'matching_state': 'manually_matched',
            'matching_explanation': _(
                'Existing Odoo bank transaction %(transaction)s selected by %(user)s. No bank entry was created.',
                transaction=bank_line.display_name, user=self.env.user.display_name,
            ),
            'confirmed_by_id': self.env.user.id,
            'confirmed_at': fields.Datetime.now(),
        })
        self.allocation_ids.receipt_clearing_move_line_id = clearing_lines
        return self.action_view_receipt()

    def action_reconcile_clearing(self):
        self.ensure_one()
        self._check_payout_access()
        if self.reconciled_at:
            return True
        self._validate_allocations()
        if not self.receipt_move_id or self.receipt_move_id.state != 'posted':
            raise UserError(_('Post the bank receipt transaction before clearing reconciliation.'))
        if self.receipt_move_id.company_id != self.company_id:
            raise UserError(_('Bank receipt and payout companies differ.'))
        clearing = self.instance_id.amazon_clearing_account_id
        if self.source == 'bank_transaction' and len(self.allocation_ids) > 1:
            settlement_lines = self.allocation_ids.mapped(
                'settlement_id.clearing_move_line_id'
            ).filtered(lambda line: not line.reconciled)
            receipt_line = self.allocation_ids[:1].receipt_clearing_move_line_id
            if settlement_lines and receipt_line and not receipt_line.reconciled:
                (settlement_lines | receipt_line).with_context(
                    no_exchange_difference=True,
                ).reconcile()
        else:
            for allocation in self.allocation_ids:
                settlement_line = allocation.settlement_id.clearing_move_line_id
                receipt_line = allocation.receipt_clearing_move_line_id
                if not receipt_line or receipt_line.move_id != self.receipt_move_id:
                    raise UserError(_('The exact payout clearing line is missing.'))
                if settlement_line.account_id != clearing or receipt_line.account_id != clearing:
                    raise UserError(_('Both lines must use the configured Amazon Clearing Account.'))
                if settlement_line.company_id != self.company_id or receipt_line.company_id != self.company_id:
                    raise UserError(_('Clearing lines must belong to the payout company.'))
                if not settlement_line.reconciled and not receipt_line.reconciled:
                    (settlement_line | receipt_line).with_context(
                        no_exchange_difference=True,
                    ).reconcile()
        self.write({
            'reconciled_by_id': self.env.user.id,
            'reconciled_at': fields.Datetime.now(),
            'matching_explanation': (self.matching_explanation or '') + '\n' + _(
                'Amazon Clearing reconciled with standard Odoo reconciliation by %(user)s.',
                user=self.env.user.display_name,
            ),
        })
        self.invalidate_recordset()
        self.allocation_ids.invalidate_recordset()
        return True

    def action_view_receipt(self):
        self.ensure_one()
        if not self.receipt_move_id:
            raise UserError(_('No bank receipt transaction is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Amazon Payout Bank Receipt'),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.receipt_move_id.id,
        }

    def action_view_bank_transaction(self):
        self.ensure_one()
        if not self.bank_statement_line_id:
            raise UserError(_('No existing bank transaction is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Odoo Bank Transaction'),
            'res_model': 'account.bank.statement.line',
            'view_mode': 'form',
            'res_id': self.bank_statement_line_id.id,
        }

    def action_view_settlements(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Amazon Settlements'),
            'res_model': 'amazon.settlement.report',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.settlement_ids.ids)],
        }
        if len(self.settlement_ids) == 1:
            action.update({'view_mode': 'form', 'res_id': self.settlement_ids.id})
        return action


class AmazonPayoutAllocation(models.Model):
    _name = 'amazon.payout.allocation'
    _description = 'Amazon Payout Settlement Allocation'
    _order = 'id'
    _check_company_auto = True

    payout_id = fields.Many2one(
        'amazon.payout', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(
        related='payout_id.instance_id', store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        related='payout_id.company_id', store=True, readonly=True, index=True,
    )
    settlement_id = fields.Many2one(
        'amazon.settlement.report', required=True, ondelete='restrict',
        index=True, check_company=True,
    )
    currency_id = fields.Many2one(
        related='payout_id.currency_id', store=True, readonly=True,
    )
    expected_amount = fields.Monetary(
        currency_field='currency_id', required=True,
        help='Expected settlement clearing amount when this allocation was created.',
    )
    allocated_amount = fields.Monetary(
        currency_field='currency_id', required=True,
        help='Actual receipt amount allocated to this settlement.',
    )
    settlement_clearing_move_line_id = fields.Many2one(
        related='settlement_id.clearing_move_line_id', store=True, readonly=True,
    )
    receipt_clearing_move_line_id = fields.Many2one(
        'account.move.line', string='Receipt Clearing Line', readonly=True,
        copy=False, ondelete='restrict', check_company=True,
    )
    reconciled_amount = fields.Monetary(
        compute='_compute_reconciled_amount', currency_field='currency_id',
    )
    remaining_amount = fields.Monetary(
        compute='_compute_reconciled_amount', currency_field='currency_id',
    )

    _payout_settlement_unique = models.Constraint(
        'UNIQUE(payout_id, settlement_id)',
        'A settlement can only appear once in the same payout.',
    )
    _expected_positive = models.Constraint(
        'CHECK(expected_amount > 0)', 'Expected payout allocation must be positive.',
    )
    _allocated_positive = models.Constraint(
        'CHECK(allocated_amount > 0)', 'Actual payout allocation must be positive.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        self.env['amazon.payout']._check_payout_access()
        for vals in vals_list:
            settlement = self.env['amazon.settlement.report'].browse(vals.get('settlement_id'))
            if settlement:
                vals.setdefault('expected_amount', settlement.payout_remaining_amount)
                vals.setdefault('allocated_amount', vals.get('expected_amount'))
        return super().create(vals_list)

    def write(self, vals):
        self.env['amazon.payout']._check_payout_access()
        if {'settlement_id', 'expected_amount', 'allocated_amount'}.intersection(vals):
            if any(allocation.payout_id.receipt_move_id for allocation in self):
                raise UserError(_('Payout allocations cannot change after receipt creation.'))
        return super().write(vals)

    def unlink(self):
        self.env['amazon.payout']._check_payout_access()
        if any(allocation.payout_id.receipt_move_id for allocation in self):
            raise UserError(_('Payout allocations cannot be removed after receipt creation.'))
        return super().unlink()

    @api.onchange('settlement_id')
    def _onchange_settlement_id(self):
        for allocation in self:
            if allocation.settlement_id:
                allocation.expected_amount = allocation.settlement_id.payout_remaining_amount
                allocation.allocated_amount = allocation.expected_amount

    @api.depends(
        'allocated_amount', 'settlement_clearing_move_line_id.matched_credit_ids.amount',
        'settlement_clearing_move_line_id.matched_debit_ids.amount',
        'receipt_clearing_move_line_id.matched_credit_ids.amount',
        'receipt_clearing_move_line_id.matched_debit_ids.amount',
    )
    def _compute_reconciled_amount(self):
        for allocation in self:
            settlement_line = allocation.settlement_clearing_move_line_id
            receipt_line = allocation.receipt_clearing_move_line_id
            partials = (
                settlement_line.matched_credit_ids
                | settlement_line.matched_debit_ids
                | receipt_line.matched_credit_ids
                | receipt_line.matched_debit_ids
            ).filtered(lambda partial: {
                partial.debit_move_id.id, partial.credit_move_id.id,
            } == {settlement_line.id, receipt_line.id}) if settlement_line and receipt_line else self.env['account.partial.reconcile']
            allocation.reconciled_amount = sum(partials.mapped('amount'))
            allocation.remaining_amount = max(
                allocation.allocated_amount - allocation.reconciled_amount, 0.0,
            )

    @api.constrains('payout_id', 'settlement_id', 'receipt_clearing_move_line_id')
    def _check_allocation_scope(self):
        for allocation in self:
            if allocation.settlement_id.instance_id != allocation.instance_id:
                raise ValidationError(_('Payout allocation must remain in the exact Amazon instance.'))
            if allocation.settlement_id.currency_id != allocation.currency_id:
                raise ValidationError(_('Payout allocation currency must match the settlement.'))
            if allocation.receipt_clearing_move_line_id and (
                allocation.receipt_clearing_move_line_id.company_id != allocation.company_id
                or allocation.receipt_clearing_move_line_id.account_id
                != allocation.instance_id.amazon_clearing_account_id
            ):
                raise ValidationError(_('Receipt allocation line must use company Amazon Clearing.'))


class AmazonSettlementPayout(models.Model):
    _inherit = 'amazon.settlement.report'

    clearing_move_line_id = fields.Many2one(
        'account.move.line', string='Settlement Clearing Line', readonly=True,
        copy=False, ondelete='restrict', check_company=True,
    )
    payout_allocation_ids = fields.One2many(
        'amazon.payout.allocation', 'settlement_id', string='Payout Allocations',
        readonly=True,
    )
    payout_ids = fields.Many2many(
        'amazon.payout', compute='_compute_payout_status', string='Payouts', readonly=True,
    )
    payout_count = fields.Integer(compute='_compute_payout_status')
    expected_payout_amount = fields.Monetary(
        compute='_compute_payout_status', currency_field='currency_id',
    )
    received_payout_amount = fields.Monetary(
        compute='_compute_payout_status', currency_field='currency_id',
    )
    payout_remaining_amount = fields.Monetary(
        compute='_compute_payout_status', currency_field='currency_id',
    )
    payout_difference_amount = fields.Monetary(
        compute='_compute_payout_status', currency_field='currency_id',
    )
    clearing_remaining_amount = fields.Monetary(
        compute='_compute_payout_status', currency_field='currency_id',
    )
    payout_state = fields.Selection(
        PAYOUT_STATES, compute='_compute_payout_status', string='Payout State',
    )

    @api.depends(
        'reported_net_amount', 'account_move_id.state',
        'clearing_move_line_id.amount_residual',
        'clearing_move_line_id.amount_residual_currency',
        'payout_allocation_ids.allocated_amount',
        'payout_allocation_ids.payout_id.receipt_move_id.state',
        'payout_allocation_ids.payout_id.state',
    )
    def _compute_payout_status(self):
        for settlement in self:
            allocations = settlement.payout_allocation_ids
            payouts = allocations.payout_id
            posted_allocations = allocations.filtered(
                lambda allocation: allocation.payout_id.receipt_move_id.state == 'posted'
            )
            received = sum(posted_allocations.mapped('allocated_amount'))
            settlement.payout_ids = payouts
            settlement.payout_count = len(payouts)
            settlement.expected_payout_amount = settlement.reported_net_amount
            settlement.received_payout_amount = received
            settlement.payout_remaining_amount = settlement.reported_net_amount - received
            settlement.payout_difference_amount = received - settlement.reported_net_amount
            settlement.clearing_remaining_amount = settlement._settlement_clearing_residual()
            if not payouts:
                settlement.payout_state = 'pending'
            elif any(payout.state == 'ambiguous' for payout in payouts):
                settlement.payout_state = 'ambiguous'
            elif any(payout.state == 'error' for payout in payouts):
                settlement.payout_state = 'error'
            elif any(payout.receipt_move_id.state == 'draft' for payout in payouts):
                settlement.payout_state = 'draft_receipt'
            elif not posted_allocations:
                settlement.payout_state = 'matched'
            else:
                comparison = settlement.currency_id.compare_amounts(
                    received, settlement.reported_net_amount,
                )
                if comparison > 0:
                    settlement.payout_state = 'mismatch'
                elif comparison < 0:
                    settlement.payout_state = 'partially_paid'
                elif settlement.currency_id.is_zero(settlement.clearing_remaining_amount):
                    settlement.payout_state = 'paid'
                else:
                    settlement.payout_state = 'matched'

    def _ensure_settlement_clearing_line(self):
        for settlement in self:
            if settlement.clearing_move_line_id:
                continue
            if not settlement.account_move_id or not settlement.instance_id.amazon_clearing_account_id:
                continue
            lines = settlement.account_move_id.line_ids.filtered(
                lambda line: line.account_id == settlement.instance_id.amazon_clearing_account_id
            )
            if len(lines) == 1:
                settlement.clearing_move_line_id = lines
        return self.mapped('clearing_move_line_id')

    def _settlement_clearing_residual(self):
        self.ensure_one()
        line = self.clearing_move_line_id
        if not line and self.account_move_id and self.instance_id.amazon_clearing_account_id:
            candidates = self.account_move_id.line_ids.filtered(
                lambda candidate: (
                    candidate.account_id == self.instance_id.amazon_clearing_account_id
                )
            )
            line = candidates if len(candidates) == 1 else self.env['account.move.line']
        if not line:
            return 0.0
        if line.currency_id == self.currency_id:
            return abs(line.amount_residual_currency)
        return abs(line.amount_residual)

    def action_create_accounting_entry(self):
        result = super().action_create_accounting_entry()
        self._ensure_settlement_clearing_line()
        return result

    def action_register_payout(self):
        self.ensure_one()
        self.env['amazon.payout']._check_payout_access()
        if not self.account_move_id or self.account_move_id.state != 'posted':
            raise UserError(_('Post the settlement accounting entry before registering payout evidence.'))
        if self.currency_id.compare_amounts(self.payout_remaining_amount, 0.0) <= 0:
            raise UserError(_('This settlement has no positive payout amount remaining.'))
        payout = self.env['amazon.payout'].create({
            'instance_id': self.instance_id.id,
            'source': 'manual_confirmation',
            'payout_date': (
                self.deposit_date.date() if self.deposit_date
                else self.settlement_end_date.date()
            ),
            'currency_id': self.currency_id.id,
            'actual_received_amount': self.payout_remaining_amount,
            'bank_journal_id': self.instance_id.amazon_payout_bank_journal_id.id,
            'matching_state': 'manually_matched',
            'matching_explanation': _(
                'Settlement selected explicitly by %(user)s. Deposit date is not treated as bank receipt evidence.',
                user=self.env.user.display_name,
            ),
        })
        self.env['amazon.payout.allocation'].create({
            'payout_id': payout.id,
            'settlement_id': self.id,
            'expected_amount': self.payout_remaining_amount,
            'allocated_amount': self.payout_remaining_amount,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Register Amazon Payout'),
            'res_model': 'amazon.payout',
            'view_mode': 'form',
            'res_id': payout.id,
        }

    def action_view_payouts(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Amazon Payouts'),
            'res_model': 'amazon.payout',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.payout_ids.ids)],
        }
        if len(self.payout_ids) == 1:
            action.update({'view_mode': 'form', 'res_id': self.payout_ids.id})
        return action

    @api.constrains('clearing_move_line_id', 'account_move_id')
    def _check_settlement_clearing_line(self):
        for settlement in self:
            if settlement.clearing_move_line_id and (
                settlement.clearing_move_line_id.move_id != settlement.account_move_id
                or settlement.clearing_move_line_id.account_id
                != settlement.instance_id.amazon_clearing_account_id
            ):
                raise ValidationError(_(
                    'Settlement clearing line must be the configured Amazon Clearing line of its accounting entry.'
                ))
