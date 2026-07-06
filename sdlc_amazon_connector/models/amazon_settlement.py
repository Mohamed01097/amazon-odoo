import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonSettlementReport(models.Model):
    _name = 'amazon.settlement.report'
    _description = 'Amazon Settlement Report'
    _order = 'end_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    settlement_id = fields.Char('Settlement ID', index=True)
    report_document_id = fields.Char('Report Document ID')
    start_date = fields.Date('Start Date')
    end_date = fields.Date('End Date')
    # Computed from line_ids.amount so it populates after Download (no need to
    # click Process just to see the total). `store=True` keeps it queryable
    # and sortable in list views.
    total_amount = fields.Float('Total Amount', compute='_compute_total_amount', store=True)
    currency_id = fields.Many2one('res.currency', string='Currency')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
        ('reconciled', 'Reconciled'),
    ], string='Status', default='draft')
    line_ids = fields.One2many('amazon.settlement.report.line', 'report_id', string='Lines')
    line_count = fields.Integer(compute='_compute_line_count')

    # Amazon-side report metadata. Captured verbatim from getReports response
    # so nothing Amazon returns is silently dropped — surfaced in the UI for
    # client visibility/audit.
    report_type = fields.Char('Report Type', readonly=True,
                              help='Amazon reportType, e.g. GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE.')
    processing_status = fields.Char('Amazon Status', readonly=True,
                                    help="Amazon's processingStatus: IN_QUEUE / IN_PROGRESS / DONE / CANCELLED / FATAL.")
    created_time = fields.Datetime('Created Time', readonly=True,
                                   help='Amazon createdTime — when Amazon began generating the report.')
    processing_start_time = fields.Datetime('Processing Start', readonly=True)
    processing_end_time = fields.Datetime('Processing End', readonly=True)
    marketplace_ids = fields.Char('Marketplace IDs', readonly=True,
                                  help='Comma-separated Amazon marketplace IDs the report covers. '
                                       'For most sellers this is the same value on every row.')

    reimbursement_invoice_ids = fields.Many2many('account.move', string='Reimbursement Invoices')

    _sql_constraints = [
        ('unique_settlement', 'unique(settlement_id, instance_id)', 'Settlement ID must be unique per instance.'),
    ]

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('line_ids.amount')
    def _compute_total_amount(self):
        for rec in self:
            rec.total_amount = sum(rec.line_ids.mapped('amount'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.settlement.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Download settlement report from Amazon."""
        self.ensure_one()
        self.instance_id._download_settlement_report(self)

    def action_process_report(self):
        """Process downloaded settlement data — link lines to Odoo orders/invoices."""
        self.ensure_one()
        if self.state not in ('downloaded',):
            raise UserError("Report must be in 'Downloaded' state to process.")

        # total_amount is now a stored compute on line_ids.amount; no manual sum needed.
        for line in self.line_ids:
            # Try to match with existing Odoo sale orders.
            if line.order_id_ref and not line.sale_order_id:
                amazon_order = self.env['amazon.sale.order'].search([
                    ('amazon_order_ref', '=', line.order_id_ref),
                    ('instance_id', '=', self.instance_id.id),
                ], limit=1)
                if amazon_order and amazon_order.sale_order_id:
                    line.sale_order_id = amazon_order.sale_order_id.id
                if amazon_order and amazon_order.invoice_id:
                    line.invoice_id = amazon_order.invoice_id.id

        self.state = 'processed'

    def action_reconcile(self):
        """Reconcile settlement lines against invoices and orders."""
        self.ensure_one()
        if self.state != 'processed':
            raise UserError("Process the report before reconciliation.")

        reconciled = 0
        reimbursement_lines = []
        for line in self.line_ids:
            if line.invoice_id and line.invoice_id.payment_state != 'paid':
                reconciled += 1
            if line.transaction_type == 'other-transaction' and 'Reimbursement' in (line.amount_type or ''):
                reimbursement_lines.append(line)

        # Generate reimbursement invoices if any
        if reimbursement_lines:
            self._create_reimbursement_invoices(reimbursement_lines)

        self.state = 'reconciled'
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Settlement Reconciled",
                "message": "%d line(s) reconciled." % reconciled,
                "type": "success",
                "sticky": False,
            },
        }

    def _create_reimbursement_invoices(self, lines):
        """Create vendor bills for Amazon reimbursements."""
        amazon_partner = self.env['res.partner'].search([('name', '=', 'Amazon')], limit=1)
        if not amazon_partner:
            amazon_partner = self.env['res.partner'].create({'name': 'Amazon', 'supplier_rank': 1})

        invoice_lines = []
        for line in lines:
            invoice_lines.append((0, 0, {
                'name': '%s - %s' % (line.amount_type or '', line.amount_description or ''),
                'price_unit': abs(line.amount),
                'quantity': 1,
            }))

        if invoice_lines:
            invoice = self.env['account.move'].create({
                'move_type': 'in_refund',
                'partner_id': amazon_partner.id,
                'invoice_line_ids': invoice_lines,
                'ref': 'Amazon Reimbursement - %s' % self.settlement_id,
            })
            self.reimbursement_invoice_ids = [(4, invoice.id)]


class AmazonSettlementReportLine(models.Model):
    _name = 'amazon.settlement.report.line'
    _description = 'Amazon Settlement Report Line'

    report_id = fields.Many2one('amazon.settlement.report', string='Report', required=True, ondelete='cascade')
    order_id_ref = fields.Char('Amazon Order ID')
    order_item_id = fields.Char('Order Item ID')
    transaction_type = fields.Char('Transaction Type')
    amount_type = fields.Char('Amount Type')
    amount_description = fields.Char('Description')
    amount = fields.Float('Amount')
    fulfillment_id = fields.Char('Fulfillment ID')
    posted_date = fields.Datetime('Posted Date')
    sale_order_id = fields.Many2one('sale.order', string='Odoo Sale Order')
    invoice_id = fields.Many2one('account.move', string='Invoice')
