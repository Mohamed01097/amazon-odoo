import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonInboundShipment(models.Model):
    _name = 'amazon.inbound.shipment'
    _description = 'Amazon Inbound Shipment'
    _order = 'create_date desc'

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    shipment_id = fields.Char('Amazon Shipment ID', index=True)
    shipment_name = fields.Char('Shipment Name')
    destination_fulfillment_center = fields.Char('Destination FC')
    label_prep_type = fields.Selection([
        ('NO_LABEL', 'No Label'),
        ('SELLER_LABEL', 'Seller Label'),
        ('AMAZON_LABEL', 'Amazon Label'),
    ], string='Label Prep', default='SELLER_LABEL')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('planning', 'Planning'),
        ('submitted', 'Submitted'),
        ('shipped', 'Shipped'),
        ('in_transit', 'In Transit'),
        ('receiving', 'Receiving'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft')

    # Carrier
    carrier_type = fields.Selection([
        ('partnered', 'Amazon Partnered'),
        ('non_partnered', 'Non-Partnered'),
    ], string='Carrier Type', default='non_partnered')
    carrier_name = fields.Char('Carrier Name')
    tracking_id = fields.Char('Tracking ID')
    pro_number = fields.Char('PRO Number')

    # Dates
    ship_date = fields.Date('Ship Date')
    estimated_arrival = fields.Date('Estimated Arrival')

    # Lines
    line_ids = fields.One2many('amazon.inbound.shipment.line', 'shipment_id', string='Items')
    line_count = fields.Integer(compute='_compute_line_count')

    # Odoo link
    picking_id = fields.Many2one('stock.picking', string='Delivery Order')

    _sql_constraints = [
        ('unique_shipment', 'unique(shipment_id, instance_id)', 'Shipment ID must be unique per instance.'),
    ]

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.inbound.shipment') or 'New'
        return super().create(vals_list)

    def action_create_shipment_plan(self):
        """Create a shipment plan on Amazon."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError("Shipment plan can only be created from draft state.")
        if not self.line_ids:
            raise UserError("Add items before creating a shipment plan.")
        self.instance_id._create_inbound_shipment_plan(self)

    def action_submit_shipment(self):
        """Submit the shipment to Amazon."""
        self.ensure_one()
        if self.state not in ('draft', 'planning'):
            raise UserError("Shipment must be in draft or planning state.")
        self.instance_id._submit_inbound_shipment(self)

    def action_mark_shipped(self):
        """Mark shipment as shipped and update tracking."""
        self.ensure_one()
        if not self.tracking_id and not self.pro_number:
            raise UserError("Please enter tracking information.")
        self.instance_id._update_inbound_shipment_tracking(self)

    def action_check_status(self):
        """Refresh shipment status from Amazon."""
        self.ensure_one()
        self.instance_id._check_inbound_shipment_status(self)

    def action_import_by_shipment_id(self):
        """Import an existing Amazon inbound shipment by ID."""
        self.ensure_one()
        if not self.shipment_id:
            raise UserError("Enter the Amazon Shipment ID first.")
        self.instance_id._import_inbound_shipment(self)

    def action_cancel(self):
        self.ensure_one()
        if self.state in ('closed', 'cancelled'):
            raise UserError("Cannot cancel a %s shipment." % self.state)
        self.state = 'cancelled'

    def action_get_labels(self):
        """Download package/pallet labels from Amazon."""
        self.ensure_one()
        if not self.shipment_id:
            raise UserError("Shipment must be submitted to Amazon first.")
        self.instance_id._get_shipment_labels(self)


class AmazonInboundShipmentLine(models.Model):
    _name = 'amazon.inbound.shipment.line'
    _description = 'Amazon Inbound Shipment Line'

    shipment_id = fields.Many2one('amazon.inbound.shipment', string='Shipment', required=True, ondelete='cascade')
    amazon_product_id = fields.Many2one('amazon.product', string='Amazon Product')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    sku = fields.Char('SKU', required=True)
    fnsku = fields.Char('FNSKU')
    quantity_shipped = fields.Float('Qty Shipped')
    quantity_received = fields.Float('Qty Received')
    quantity_in_case = fields.Float('Qty Per Case')
    quantity_discrepancy = fields.Float('Discrepancy', compute='_compute_discrepancy', store=True)

    @api.depends('quantity_shipped', 'quantity_received')
    def _compute_discrepancy(self):
        for line in self:
            line.quantity_discrepancy = line.quantity_shipped - line.quantity_received
