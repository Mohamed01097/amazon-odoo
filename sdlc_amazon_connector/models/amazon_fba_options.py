from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AmazonFbaPackingOption(models.Model):
    _name = 'amazon.fba.packing.option'
    _description = 'Amazon FBA Packing Option'
    _rec_name = 'option_name'
    _order = 'selected desc, expiration_date, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company', related='inbound_shipment_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_packing_option_id = fields.Char(required=True, copy=False, index=True)
    option_name = fields.Char(required=True)
    status = fields.Char(readonly=True, copy=False)
    expiration_date = fields.Datetime(readonly=True, copy=False)
    fee_amount = fields.Float(readonly=True, copy=False, digits=(16, 2))
    fee_currency = fields.Char(readonly=True, copy=False, size=3)
    selected = fields.Boolean(index=True, copy=False)
    amazon_packing_group_ids = fields.Text(
        readonly=True, copy=False,
        help="JSON list of packingGroupIds returned by Amazon. These IDs are needed by the later packing-information phase.",
    )
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    box_ids = fields.One2many('amazon.fba.box', 'packing_option_id', string='Packing Boxes')

    _unique_amazon_option = models.Constraint(
        'UNIQUE (inbound_shipment_id, amazon_packing_option_id)',
        'A packing option can only occur once on an inbound shipment.',
    )
    _single_selected_option = models.UniqueIndex(
        '(inbound_shipment_id) WHERE selected IS TRUE',
        'Only one packing option can be selected per inbound shipment.',
    )

    @api.constrains('instance_id', 'inbound_shipment_id')
    def _check_instance(self):
        for option in self:
            if option.instance_id != option.inbound_shipment_id.instance_id:
                raise ValidationError(_("The packing option instance must match the inbound shipment instance."))

    @api.constrains('selected', 'inbound_shipment_id')
    def _check_single_selected(self):
        for option in self.filtered('selected'):
            if self.search_count([
                ('inbound_shipment_id', '=', option.inbound_shipment_id.id),
                ('selected', '=', True),
            ]) > 1:
                raise ValidationError(_("Only one packing option can be selected per inbound shipment."))

    @api.model_create_multi
    def create(self, vals_list):
        selected_shipment_ids = [
            vals.get('inbound_shipment_id')
            for vals in vals_list
            if vals.get('selected') and vals.get('inbound_shipment_id')
        ]
        if (
            len(selected_shipment_ids) != len(set(selected_shipment_ids))
            or self.search_count([
                ('inbound_shipment_id', 'in', selected_shipment_ids),
                ('selected', '=', True),
            ])
        ):
            raise ValidationError(_("Only one packing option can be selected per inbound shipment."))
        return super().create(vals_list)

    def write(self, vals):
        if 'selected' in vals and not self.env.context.get('amazon_sync_option_selection'):
            if vals['selected'] and (
                len(self) != 1
                or self.search_count([
                    ('inbound_shipment_id', '=', self.inbound_shipment_id.id),
                    ('selected', '=', True),
                    ('id', 'not in', self.ids),
                ])
            ):
                raise ValidationError(_("Only one packing option can be selected per inbound shipment."))
            locked = self.filtered(
                lambda option: option.inbound_shipment_id.packing_confirmation_status
                in ('pending', 'in_progress', 'success')
            )
            if locked:
                raise ValidationError(_(
                    "Packing option selection cannot change after Amazon confirmation starts."
                ))
        return super().write(vals)


class AmazonFbaBox(models.Model):
    _name = 'amazon.fba.box'
    _description = 'Amazon FBA Packing Box'
    _order = 'id'
    _check_company_auto = True

    packing_option_id = fields.Many2one(
        'amazon.fba.packing.option', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', related='packing_option_id.inbound_shipment_id',
        store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='packing_option_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='packing_option_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_box_id = fields.Char(copy=False, index=True)
    amazon_packing_group_id = fields.Char(
        string='Amazon Packing Group ID', copy=False, index=True,
        help="Packing group to which this local box belongs. Amazon does not return boxes from listPackingOptions.",
    )
    length = fields.Float(required=True, digits=(16, 4))
    width = fields.Float(required=True, digits=(16, 4))
    height = fields.Float(required=True, digits=(16, 4))
    weight = fields.Float(required=True, digits=(16, 4))
    weight_unit = fields.Selection([
        ('KG', 'Kilograms'),
        ('LB', 'Pounds'),
    ], required=True, default='KG')
    dimension_unit = fields.Selection([
        ('CM', 'Centimeters'),
        ('IN', 'Inches'),
    ], required=True, default='CM')
    line_ids = fields.One2many('amazon.fba.box.line', 'box_id', string='Box Items')

    _unique_amazon_box = models.Constraint(
        'UNIQUE (packing_option_id, amazon_box_id)',
        'An Amazon box can only occur once on a packing option.',
    )
    _positive_measurements = models.Constraint(
        'CHECK (length > 0 AND width > 0 AND height > 0 AND weight > 0)',
        'Box dimensions and weight must be positive.',
    )


class AmazonFbaBoxLine(models.Model):
    _name = 'amazon.fba.box.line'
    _description = 'Amazon FBA Packing Box Item'
    _order = 'id'
    _check_company_auto = True

    box_id = fields.Many2one(
        'amazon.fba.box', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', related='box_id.inbound_shipment_id',
        store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='box_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='box_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one('amazon.product', required=True, ondelete='restrict')
    msku = fields.Char(string='MSKU', required=True)
    quantity = fields.Integer(required=True)

    _unique_box_msku = models.Constraint(
        'UNIQUE (box_id, msku)',
        'An MSKU can only occur once in a box.',
    )
    _positive_quantity = models.Constraint(
        'CHECK (quantity > 0 AND quantity <= 500000)',
        'Box item quantity must be from 1 to 500000.',
    )

    @api.constrains('amazon_product_id', 'box_id', 'msku')
    def _check_product_mapping(self):
        for line in self:
            product = line.amazon_product_id
            if product.instance_id != line.instance_id:
                raise ValidationError(_("The box item Amazon Product must belong to the same instance."))
            if not product.sku or product.sku.strip() != (line.msku or '').strip():
                raise ValidationError(_("The box item MSKU must match the mapped Amazon Product SKU."))

    @api.onchange('amazon_product_id')
    def _onchange_amazon_product_id(self):
        if self.amazon_product_id:
            self.msku = self.amazon_product_id.sku or False


class AmazonFbaPlacementOption(models.Model):
    _name = 'amazon.fba.placement.option'
    _description = 'Amazon FBA Placement Option'
    _rec_name = 'amazon_placement_option_id'
    _order = 'selected desc, expiration_date, id'
    _check_company_auto = True

    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='inbound_shipment_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='inbound_shipment_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_placement_option_id = fields.Char(required=True, copy=False, index=True)
    status = fields.Char(readonly=True, copy=False)
    destination_fc = fields.Char(
        string='Destination FC', readonly=True, copy=False,
        help="Populated only when later getShipment responses provide fulfillment-center destinations. listPlacementOptions returns shipment IDs, not FC codes.",
    )
    amazon_shipment_ids = fields.Text(
        string='Amazon Shipment IDs', readonly=True, copy=False,
        help="JSON list of shipmentIds returned by listPlacementOptions.",
    )
    fee = fields.Float(readonly=True, copy=False, digits=(16, 2))
    currency = fields.Char(readonly=True, copy=False, size=3)
    selected = fields.Boolean(index=True, copy=False)
    expiration_date = fields.Datetime(readonly=True, copy=False)
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_amazon_option = models.Constraint(
        'UNIQUE (inbound_shipment_id, amazon_placement_option_id)',
        'A placement option can only occur once on an inbound shipment.',
    )
    _single_selected_option = models.UniqueIndex(
        '(inbound_shipment_id) WHERE selected IS TRUE',
        'Only one placement option can be selected per inbound shipment.',
    )

    @api.constrains('selected', 'inbound_shipment_id')
    def _check_single_selected(self):
        for option in self.filtered('selected'):
            if self.search_count([
                ('inbound_shipment_id', '=', option.inbound_shipment_id.id),
                ('selected', '=', True),
            ]) > 1:
                raise ValidationError(_("Only one placement option can be selected per inbound shipment."))

    @api.model_create_multi
    def create(self, vals_list):
        selected_shipment_ids = [
            vals.get('inbound_shipment_id')
            for vals in vals_list
            if vals.get('selected') and vals.get('inbound_shipment_id')
        ]
        if (
            len(selected_shipment_ids) != len(set(selected_shipment_ids))
            or self.search_count([
                ('inbound_shipment_id', 'in', selected_shipment_ids),
                ('selected', '=', True),
            ])
        ):
            raise ValidationError(_("Only one placement option can be selected per inbound shipment."))
        return super().create(vals_list)

    def write(self, vals):
        if 'selected' in vals and not self.env.context.get('amazon_sync_option_selection'):
            if vals['selected'] and (
                len(self) != 1
                or self.search_count([
                    ('inbound_shipment_id', '=', self.inbound_shipment_id.id),
                    ('selected', '=', True),
                    ('id', 'not in', self.ids),
                ])
            ):
                raise ValidationError(_("Only one placement option can be selected per inbound shipment."))
            locked = self.filtered(
                lambda option: option.inbound_shipment_id.placement_confirmation_status
                in ('pending', 'in_progress', 'success')
            )
            if locked:
                raise ValidationError(_(
                    "Placement option selection cannot change after Amazon confirmation starts."
                ))
        return super().write(vals)
