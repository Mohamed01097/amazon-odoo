import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonRatingReport(models.Model):
    _name = 'amazon.rating.report'
    _description = 'Amazon Seller Rating Report'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
    ], string='Status', default='draft')
    report_date = fields.Date('Report Date', default=fields.Date.today)
    line_ids = fields.One2many('amazon.rating.report.line', 'report_id', string='Ratings')
    line_count = fields.Integer(compute='_compute_line_count')

    # Summary
    average_rating = fields.Float('Average Rating', compute='_compute_averages', store=True)
    total_ratings = fields.Integer('Total Ratings', compute='_compute_averages', store=True)

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('line_ids', 'line_ids.rating')
    def _compute_averages(self):
        for rec in self:
            lines = rec.line_ids.filtered(lambda l: l.rating > 0)
            rec.total_ratings = len(lines)
            rec.average_rating = sum(l.rating for l in lines) / len(lines) if lines else 0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.rating.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Download seller feedback/rating report."""
        self.ensure_one()
        self.instance_id._download_rating_report(self)

    def action_process_report(self):
        """Process downloaded rating data."""
        self.ensure_one()
        if self.state != 'downloaded':
            raise UserError("Download the report first.")
        # Match with orders
        for line in self.line_ids:
            if line.amazon_order_id and not line.order_id:
                amazon_order = self.env['amazon.sale.order'].search([
                    ('amazon_order_ref', '=', line.amazon_order_id),
                    ('instance_id', '=', self.instance_id.id),
                ], limit=1)
                if amazon_order:
                    line.order_id = amazon_order.id
        self.state = 'processed'


class AmazonRatingReportLine(models.Model):
    _name = 'amazon.rating.report.line'
    _description = 'Amazon Seller Rating Line'

    report_id = fields.Many2one('amazon.rating.report', string='Report', required=True, ondelete='cascade')
    amazon_order_id = fields.Char('Amazon Order ID')
    order_id = fields.Many2one('amazon.sale.order', string='Amazon Order')
    rating = fields.Integer('Rating')
    feedback = fields.Text('Feedback')
    date = fields.Datetime('Date')
    rater_email = fields.Char('Customer Email')
    your_response = fields.Text('Your Response')
