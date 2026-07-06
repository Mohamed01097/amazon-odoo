import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonOutboundOrder(models.Model):
    _name = 'amazon.outbound.order'
    _description = 'Amazon Multi-Channel Fulfillment Order'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    sale_order_id = fields.Many2one('sale.order', string='Odoo Sale Order')
    fulfillment_order_id = fields.Char('Amazon Fulfillment Order ID')
    displayable_order_id = fields.Char('Displayable Order ID')
    displayable_comment = fields.Text('Order Comment')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft')

    shipping_speed = fields.Selection([
        ('Standard', 'Standard'),
        ('Expedited', 'Expedited'),
        ('Priority', 'Priority'),
    ], string='Shipping Speed', default='Standard')

    # Destination
    dest_name = fields.Char('Recipient Name')
    dest_address_line1 = fields.Char('Address Line 1')
    dest_address_line2 = fields.Char('Address Line 2')
    dest_city = fields.Char('City')
    dest_state = fields.Char('State')
    dest_postal_code = fields.Char('Postal Code')
    dest_country_code = fields.Char('Country Code')

    # Tracking
    carrier_name = fields.Char('Carrier')
    tracking_number = fields.Char('Tracking Number')
    estimated_arrival_date = fields.Datetime('Est. Arrival')

    line_ids = fields.One2many('amazon.outbound.order.line', 'order_id', string='Items')
    line_count = fields.Integer(compute='_compute_line_count')

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.outbound.order') or 'New'
        return super().create(vals_list)

    def action_submit_to_amazon(self):
        """Submit MCF outbound order to Amazon."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError("Only draft orders can be submitted.")
        if not self.line_ids:
            raise UserError("Add items to the order first.")
        if not self.dest_name or not self.dest_address_line1:
            raise UserError("Destination address is required.")
        self.instance_id._submit_outbound_order(self)

    def action_check_status(self):
        """Check fulfillment status on Amazon."""
        self.ensure_one()
        if not self.fulfillment_order_id:
            raise UserError("Order not yet submitted to Amazon.")
        self.instance_id._check_outbound_order_status(self)

    def action_cancel(self):
        """Cancel the MCF order."""
        self.ensure_one()
        if self.state in ('delivered', 'cancelled'):
            raise UserError("Cannot cancel a %s order." % self.state)
        self.instance_id._cancel_outbound_order(self)

    def action_create_from_sale_order(self):
        """Populate MCF order lines from linked Odoo sale order."""
        self.ensure_one()
        if not self.sale_order_id:
            raise UserError("Link a sale order first.")

        so = self.sale_order_id
        self.dest_name = so.partner_shipping_id.name or so.partner_id.name
        self.dest_address_line1 = so.partner_shipping_id.street or so.partner_id.street or ''
        self.dest_address_line2 = so.partner_shipping_id.street2 or so.partner_id.street2 or ''
        self.dest_city = so.partner_shipping_id.city or so.partner_id.city or ''
        self.dest_state = (so.partner_shipping_id.state_id.code or so.partner_id.state_id.code or '')
        self.dest_postal_code = so.partner_shipping_id.zip or so.partner_id.zip or ''
        self.dest_country_code = (so.partner_shipping_id.country_id.code or so.partner_id.country_id.code or '')
        self.displayable_order_id = so.name

        lines = []
        for sol in so.order_line:
            if not sol.product_id:
                continue
            amazon_prod = self.env['amazon.product'].search([
                ('odoo_product_id', '=', sol.product_id.id),
                ('instance_id', '=', self.instance_id.id),
            ], limit=1)
            lines.append((0, 0, {
                'sku': amazon_prod.sku if amazon_prod else sol.product_id.default_code or '',
                'quantity': sol.product_uom_qty,
                'odoo_product_id': sol.product_id.id,
            }))
        self.line_ids = [(5, 0, 0)] + lines


class AmazonOutboundOrderLine(models.Model):
    _name = 'amazon.outbound.order.line'
    _description = 'Amazon MCF Order Line'

    order_id = fields.Many2one('amazon.outbound.order', string='Order', required=True, ondelete='cascade')
    sku = fields.Char('SKU', required=True)
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    quantity = fields.Float('Quantity', default=1)
