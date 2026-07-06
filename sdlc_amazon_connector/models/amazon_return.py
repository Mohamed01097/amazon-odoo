import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonReturnReport(models.Model):
    _name = 'amazon.return.report'
    _description = 'Amazon Return Report'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
    ], string='Status', default='draft')
    line_ids = fields.One2many('amazon.return.report.line', 'report_id', string='Return Lines')
    line_count = fields.Integer(compute='_compute_line_count')
    report_date = fields.Date('Report Date', default=fields.Date.today)

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.return.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Download FBA return report from Amazon."""
        self.ensure_one()
        self.instance_id._download_return_report(self)

    def action_process_returns(self):
        """Process return lines: validate incoming shipments, create credit notes."""
        self.ensure_one()
        if self.state != 'downloaded':
            raise UserError("Download the report first.")

        processed = 0
        for line in self.line_ids:
            if line.state == 'processed':
                continue

            # Find matching Amazon order
            amazon_order = self.env['amazon.sale.order'].search([
                ('amazon_order_ref', '=', line.amazon_order_id),
                ('instance_id', '=', self.instance_id.id),
            ], limit=1)
            if amazon_order:
                line.order_id = amazon_order.id

                # Create credit note if invoice exists
                if amazon_order.invoice_id and not line.credit_note_id:
                    credit_note = self._create_credit_note(amazon_order, line)
                    if credit_note:
                        line.credit_note_id = credit_note.id

            line.state = 'processed'
            processed += 1

        self.state = 'processed'
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Returns Processed",
                "message": "%d return(s) processed." % processed,
                "type": "success",
                "sticky": False,
            },
        }

    def _create_credit_note(self, amazon_order, return_line):
        """Create a credit note for a return."""
        invoice = amazon_order.invoice_id
        if not invoice or invoice.state == 'cancel':
            return False

        move_reversal = self.env['account.move.reversal'].with_context(
            active_model='account.move',
            active_ids=[invoice.id],
        ).create({
            'reason': 'Amazon Return: %s' % return_line.return_reason,
            'journal_id': invoice.journal_id.id,
        })
        reversal = move_reversal.refund_moves()
        if reversal and reversal.get('res_id'):
            return self.env['account.move'].browse(reversal['res_id'])
        return False


class AmazonReturnReportLine(models.Model):
    _name = 'amazon.return.report.line'
    _description = 'Amazon Return Report Line'

    report_id = fields.Many2one('amazon.return.report', string='Report', required=True, ondelete='cascade')
    amazon_order_id = fields.Char('Amazon Order ID')
    order_id = fields.Many2one('amazon.sale.order', string='Amazon Order')
    sku = fields.Char('SKU')
    asin = fields.Char('ASIN')
    fnsku = fields.Char('FNSKU')
    product_name = fields.Char('Product Name')
    quantity = fields.Integer('Quantity', default=1)
    fulfillment_center_id = fields.Char('Fulfillment Center')
    return_date = fields.Datetime('Return Date')
    return_reason = fields.Char('Return Reason')
    status = fields.Char('Amazon Status')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processed', 'Processed'),
    ], default='pending')
    credit_note_id = fields.Many2one('account.move', string='Credit Note')
