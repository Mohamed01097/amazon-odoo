import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonVCSTaxReport(models.Model):
    _name = 'amazon.vcs.tax.report'
    _description = 'Amazon VCS Tax Report'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
    ], string='Status', default='draft')
    report_date = fields.Date('Report Date', default=fields.Date.today)
    line_ids = fields.One2many('amazon.vcs.tax.report.line', 'report_id', string='Lines')
    line_count = fields.Integer(compute='_compute_line_count')

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.vcs.tax.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Download VCS tax report from Amazon."""
        self.ensure_one()
        self.instance_id._download_vcs_report(self)

    def action_process_report(self):
        """Process VCS report and store Amazon invoice numbers on Odoo invoices."""
        self.ensure_one()
        if self.state != 'downloaded':
            raise UserError("Download the report first.")

        processed = 0
        for line in self.line_ids:
            if not line.amazon_order_id or not line.amazon_invoice_number:
                continue

            # Find matching Amazon order
            amazon_order = self.env['amazon.sale.order'].search([
                ('amazon_order_ref', '=', line.amazon_order_id),
                ('instance_id', '=', self.instance_id.id),
            ], limit=1)
            if amazon_order:
                amazon_order.amazon_invoice_number = line.amazon_invoice_number

                # Store on Odoo invoice if exists
                if amazon_order.invoice_id:
                    line.invoice_id = amazon_order.invoice_id.id
                    if amazon_order.invoice_id.state in ('posted', 'paid'):
                        amazon_order.invoice_id.amazon_invoice_number = line.amazon_invoice_number
                        processed += 1

        self.state = 'processed'
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "VCS Report Processed",
                "message": "%d invoice(s) updated with Amazon invoice numbers." % processed,
                "type": "success",
                "sticky": False,
            },
        }


class AmazonVCSTaxReportLine(models.Model):
    _name = 'amazon.vcs.tax.report.line'
    _description = 'Amazon VCS Tax Report Line'

    report_id = fields.Many2one('amazon.vcs.tax.report', string='Report', required=True, ondelete='cascade')
    amazon_order_id = fields.Char('Amazon Order ID')
    invoice_id = fields.Many2one('account.move', string='Odoo Invoice')
    amazon_invoice_number = fields.Char('Amazon Invoice Number')
    vat_number = fields.Char('VAT Number')
    vat_amount = fields.Float('VAT Amount')
    invoice_amount = fields.Float('Invoice Amount')
    currency_code = fields.Char('Currency')
