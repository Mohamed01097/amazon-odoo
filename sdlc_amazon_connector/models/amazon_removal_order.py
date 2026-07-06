import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonRemovalOrder(models.Model):
    _name = 'amazon.removal.order'
    _description = 'Amazon Removal Order'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    removal_order_id = fields.Char('Amazon Removal Order ID')
    order_type = fields.Selection([
        ('Return', 'Return to Seller'),
        ('Disposal', 'Disposal'),
    ], string='Order Type', default='Return', required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft')
    ship_to_address = fields.Text('Ship To Address')
    line_ids = fields.One2many('amazon.removal.order.line', 'order_id', string='Lines')
    line_count = fields.Integer(compute='_compute_line_count')

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.removal.order') or 'New'
        return super().create(vals_list)

    def action_submit_to_amazon(self):
        """Submit removal order to Amazon."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError("Only draft removal orders can be submitted.")
        if not self.line_ids:
            raise UserError("Add at least one product line.")
        self.instance_id._submit_removal_order(self)

    def action_check_status(self):
        """Check removal order status on Amazon."""
        self.ensure_one()
        self.instance_id._check_removal_order_status(self)

    def action_cancel(self):
        """Cancel this removal order."""
        self.ensure_one()
        if self.state in ('completed', 'cancelled'):
            raise UserError("Cannot cancel a %s order." % self.state)
        self.state = 'cancelled'


class AmazonRemovalOrderLine(models.Model):
    _name = 'amazon.removal.order.line'
    _description = 'Amazon Removal Order Line'

    order_id = fields.Many2one('amazon.removal.order', string='Removal Order', required=True, ondelete='cascade')
    amazon_product_id = fields.Many2one('amazon.product', string='Amazon Product')
    sku = fields.Char('SKU')
    fnsku = fields.Char('FNSKU')
    disposition = fields.Selection([
        ('Sellable', 'Sellable'),
        ('Unsellable', 'Unsellable'),
    ], string='Disposition', default='Sellable')
    requested_quantity = fields.Float('Requested Qty')
    shipped_quantity = fields.Float('Shipped Qty')
    cancelled_quantity = fields.Float('Cancelled Qty')
