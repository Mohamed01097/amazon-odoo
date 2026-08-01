import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonFBAInventoryReport(models.Model):
    _name = 'amazon.fba.inventory.report'
    _description = 'Amazon FBA Inventory Report'
    _order = 'report_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    report_type = fields.Selection([
        ('live_stock', 'FBA Live Stock'),
        ('adjustment', 'Inventory Adjustment'),
        ('fba_shipment', 'FBA Shipment'),
    ], string='Report Type', required=True, default='live_stock')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
    ], string='Status', default='draft')
    report_date = fields.Date('Report Date', default=fields.Date.today)
    line_ids = fields.One2many('amazon.fba.inventory.report.line', 'report_id', string='Lines')
    line_count = fields.Integer(compute='_compute_line_count')

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.fba.inventory.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Download FBA inventory report from Amazon."""
        self.ensure_one()
        self.instance_id._download_fba_inventory_report(self)

    def action_process_report(self):
        """Process inventory report and adjust Odoo stock."""
        self.ensure_one()
        if self.state != 'downloaded':
            raise UserError("Download the report first.")

        if self.report_type == 'live_stock':
            self._process_live_stock()
        elif self.report_type == 'adjustment':
            self._process_adjustment()
        elif self.report_type == 'fba_shipment':
            self._process_fba_shipment()

        self.state = 'processed'
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Report Processed",
                "message": "%d line(s) processed." % len(self.line_ids),
                "type": "success",
                "sticky": False,
            },
        }

    def _process_live_stock(self):
        """Update amazon.product quantities from live stock report."""
        for line in self.line_ids:
            amazon_prod = self.env['amazon.product'].search([
                ('sku', '=', line.sku),
                ('instance_id', '=', self.instance_id.id),
            ], limit=1)
            if amazon_prod:
                amazon_prod.amazon_qty = line.quantity

    def _process_adjustment(self):
        """Block the retired direct-quant adjustment path."""
        raise UserError(
            "Legacy report adjustments are disabled. Run an Inventory Audit and "
            "apply a supported stock transfer, or use Odoo's standard Inventory "
            "Adjustment workflow after manual review."
        )

    def _process_fba_shipment(self):
        """Process FBA shipment report: create orders and deliveries."""
        for line in self.line_ids:
            if not line.amazon_order_id:
                continue
            amazon_order = self.env['amazon.sale.order'].search([
                ('amazon_order_ref', '=', line.amazon_order_id),
                ('instance_id', '=', self.instance_id.id),
            ], limit=1)
            if amazon_order and amazon_order.order_status != 'Shipped':
                amazon_order.order_status = 'Shipped'


class AmazonFBAInventoryReportLine(models.Model):
    _name = 'amazon.fba.inventory.report.line'
    _description = 'Amazon FBA Inventory Report Line'

    report_id = fields.Many2one('amazon.fba.inventory.report', string='Report', required=True, ondelete='cascade')
    sku = fields.Char('SKU')
    fnsku = fields.Char('FNSKU')
    asin = fields.Char('ASIN')
    product_name = fields.Char('Product Name')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    condition = fields.Char('Condition')
    disposition = fields.Char('Disposition')
    quantity = fields.Float('Quantity')
    fulfillment_center_id = fields.Char('Fulfillment Center')
    reason = fields.Char('Reason')
    adjustment_date = fields.Datetime('Date')
    amazon_order_id = fields.Char('Amazon Order ID')
