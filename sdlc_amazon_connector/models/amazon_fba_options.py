from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


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
    discount_amount = fields.Float(readonly=True, copy=False, digits=(16, 2))
    discount_currency = fields.Char(readonly=True, copy=False, size=3)
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
    packing_group_ids = fields.One2many(
        'amazon.fba.packing.group', 'packing_option_id', string='Amazon Packing Groups',
    )

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


class AmazonFbaPackingGroup(models.Model):
    _name = 'amazon.fba.packing.group'
    _description = 'Amazon FBA Packing Group'
    _rec_name = 'amazon_packing_group_id'
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
    amazon_packing_group_id = fields.Char(required=True, copy=False, index=True)
    item_ids = fields.One2many(
        'amazon.fba.packing.group.item', 'packing_group_id', string='Items',
    )
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_group = models.Constraint(
        'UNIQUE (packing_option_id, amazon_packing_group_id)',
        'A packing group can only occur once on a packing option.',
    )


class AmazonFbaPackingGroupItem(models.Model):
    _name = 'amazon.fba.packing.group.item'
    _description = 'Amazon FBA Packing Group Item'
    _order = 'msku, id'
    _check_company_auto = True

    packing_group_id = fields.Many2one(
        'amazon.fba.packing.group', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', related='packing_group_id.inbound_shipment_id',
        store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='packing_group_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='packing_group_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one('amazon.product', ondelete='restrict', index=True)
    msku = fields.Char(required=True, index=True)
    asin = fields.Char()
    fnsku = fields.Char()
    quantity = fields.Integer(required=True)
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_group_msku = models.Constraint(
        'UNIQUE (packing_group_id, msku)',
        'An MSKU can only occur once in a packing group.',
    )
    _positive_quantity = models.Constraint(
        'CHECK (quantity > 0 AND quantity <= 500000)',
        'Packing group item quantity must be from 1 to 500000.',
    )

    @api.constrains('amazon_product_id', 'instance_id', 'msku')
    def _check_product_mapping(self):
        for item in self.filtered('amazon_product_id'):
            if item.amazon_product_id.instance_id != item.instance_id:
                raise ValidationError(_("The packing group product must belong to the same instance."))
            if (item.amazon_product_id.sku or '').strip() != (item.msku or '').strip():
                raise ValidationError(_("The packing group MSKU must match the Amazon Product SKU."))


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


class AmazonFbaBulkBoxWizard(models.TransientModel):
    _name = 'amazon.fba.bulk.box.wizard'
    _description = 'Amazon FBA Bulk Box Creation'

    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, readonly=True, ondelete='cascade',
    )
    packing_option_id = fields.Many2one(
        'amazon.fba.packing.option', required=True, readonly=True, ondelete='cascade',
    )
    packing_group_id = fields.Many2one(
        'amazon.fba.packing.group', required=True,
        domain="[('packing_option_id', '=', packing_option_id)]",
    )
    packing_group_item_id = fields.Many2one(
        'amazon.fba.packing.group.item', required=True,
        domain="[('packing_group_id', '=', packing_group_id)]",
    )
    amazon_product_id = fields.Many2one(
        'amazon.product', related='packing_group_item_id.amazon_product_id', readonly=True,
    )
    msku = fields.Char(related='packing_group_item_id.msku', readonly=True)
    box_count = fields.Integer(required=True, default=1)
    units_per_box = fields.Integer(required=True)
    length = fields.Float(required=True, digits=(16, 4))
    width = fields.Float(required=True, digits=(16, 4))
    height = fields.Float(required=True, digits=(16, 4))
    dimension_unit = fields.Selection([
        ('CM', 'Centimeters'),
        ('IN', 'Inches'),
    ], required=True, default='CM')
    weight = fields.Float(required=True, digits=(16, 4))
    weight_unit = fields.Selection([
        ('KG', 'Kilograms'),
        ('LB', 'Pounds'),
    ], required=True, default='KG')
    replace_existing = fields.Boolean(
        string='Replace Existing Boxes',
        help="Remove existing boxes on the selected packing option before creating this batch.",
    )

    @api.constrains('box_count', 'units_per_box', 'length', 'width', 'height', 'weight')
    def _check_positive_values(self):
        for wizard in self:
            if wizard.box_count <= 0:
                raise ValidationError(_("Number of boxes must be greater than zero."))
            if wizard.units_per_box <= 0:
                raise ValidationError(_("Units per box must be greater than zero."))
            if (
                wizard.length <= 0
                or wizard.width <= 0
                or wizard.height <= 0
                or wizard.weight <= 0
            ):
                raise ValidationError(_("Box dimensions and weight must be greater than zero."))

    @api.onchange('packing_group_id')
    def _onchange_packing_group_id(self):
        if self.packing_group_item_id.packing_group_id != self.packing_group_id:
            self.packing_group_item_id = False

    def _validate_generation_scope(self):
        self.ensure_one()
        shipment = self.inbound_shipment_id
        option = self.packing_option_id
        group = self.packing_group_id
        item = self.packing_group_item_id
        selected = shipment.packing_option_ids.filtered('selected')
        if option.inbound_shipment_id != shipment:
            raise UserError(_("The packing option does not belong to this inbound shipment."))
        if len(selected) != 1 or selected != option:
            raise UserError(_("Bulk boxes can only be created for the selected packing option."))
        if shipment.state != 'packing_confirmed' or shipment.packing_confirmation_status != 'success':
            raise UserError(_("Confirm the Amazon packing option before creating box information."))
        if option.status != 'ACCEPTED':
            raise UserError(_("The selected packing option must have Amazon status ACCEPTED."))
        if group.packing_option_id != option:
            raise UserError(_("The packing group does not belong to the selected packing option."))
        if item.packing_group_id != group:
            raise UserError(_("The selected item does not belong to the packing group."))
        if not item.amazon_product_id:
            raise UserError(_("The packing group item must be mapped to an Amazon Product before boxes can be created."))

    def _matching_existing_boxes(self):
        self.ensure_one()
        group_id = (self.packing_group_id.amazon_packing_group_id or '').strip()
        msku = (self.packing_group_item_id.msku or '').strip()
        return self.packing_option_id.box_ids.filtered(lambda box: (
            (box.amazon_packing_group_id or '').strip() == group_id
            and box.length == self.length
            and box.width == self.width
            and box.height == self.height
            and box.dimension_unit == self.dimension_unit
            and box.weight == self.weight
            and box.weight_unit == self.weight_unit
            and len(box.line_ids) == 1
            and (box.line_ids.msku or '').strip() == msku
            and box.line_ids.quantity == self.units_per_box
        ))

    def _existing_quantity_for_item(self):
        self.ensure_one()
        if self.replace_existing:
            return 0
        group_id = (self.packing_group_id.amazon_packing_group_id or '').strip()
        msku = (self.packing_group_item_id.msku or '').strip()
        quantity = 0
        for box in self.packing_option_id.box_ids.filtered(
            lambda candidate: (candidate.amazon_packing_group_id or '').strip() == group_id
        ):
            quantity += sum(
                line.quantity
                for line in box.line_ids
                if (line.msku or '').strip() == msku
            )
        return quantity

    def _next_box_number(self):
        self.ensure_one()
        max_number = 0
        for box_id in self.packing_option_id.box_ids.mapped('amazon_box_id'):
            value = (box_id or '').strip()
            if value.startswith('BOX-') and value[4:].isdigit():
                max_number = max(max_number, int(value[4:]))
        return max_number + 1

    def action_generate_boxes(self):
        self.ensure_one()
        self._validate_generation_scope()
        self.inbound_shipment_id._check_inbound_manager_access()
        self.inbound_shipment_id._lock_phase3_workflow()
        if not self.replace_existing and len(self._matching_existing_boxes()) >= self.box_count:
            raise UserError(_(
                "A matching batch of boxes already exists. Change the carton values, "
                "or use Replace Existing Boxes to regenerate deliberately."
            ))
        expected_quantity = self.packing_group_item_id.quantity
        requested_quantity = self.box_count * self.units_per_box
        existing_quantity = self._existing_quantity_for_item()
        if existing_quantity + requested_quantity > expected_quantity:
            raise UserError(_(
                "This batch would exceed Amazon's expected quantity for %(msku)s. "
                "Expected %(expected)s; existing boxes contain %(existing)s; this batch adds %(requested)s.",
                msku=self.msku,
                expected=expected_quantity,
                existing=existing_quantity,
                requested=requested_quantity,
            ))
        if self.replace_existing:
            self.packing_option_id.box_ids.unlink()
        next_number = self._next_box_number()
        group_id = (self.packing_group_id.amazon_packing_group_id or '').strip()
        values = []
        for offset in range(self.box_count):
            values.append({
                'packing_option_id': self.packing_option_id.id,
                'amazon_box_id': 'BOX-%04d' % (next_number + offset),
                'amazon_packing_group_id': group_id,
                'length': self.length,
                'width': self.width,
                'height': self.height,
                'dimension_unit': self.dimension_unit,
                'weight': self.weight,
                'weight_unit': self.weight_unit,
                'line_ids': [(0, 0, {
                    'amazon_product_id': self.amazon_product_id.id,
                    'msku': self.msku,
                    'quantity': self.units_per_box,
                })],
            })
        self.env['amazon.fba.box'].create(values)
        return self.inbound_shipment_id.instance_id._notify(
            _("Bulk Boxes"),
            _("%(count)s box(es) were created for packing group %(group)s.",
              count=self.box_count, group=group_id),
        )


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
    discount = fields.Float(readonly=True, copy=False, digits=(16, 2))
    discount_currency = fields.Char(readonly=True, copy=False, size=3)
    selected = fields.Boolean(index=True, copy=False)
    expiration_date = fields.Datetime(readonly=True, copy=False)
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    physical_shipment_ids = fields.One2many(
        'amazon.fba.physical.shipment', 'placement_option_id',
        string='Amazon Physical Shipments',
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


class AmazonFbaPhysicalShipment(models.Model):
    _name = 'amazon.fba.physical.shipment'
    _description = 'Amazon FBA Physical Shipment'
    _rec_name = 'amazon_shipment_id'
    _order = 'amazon_shipment_id, id'
    _check_company_auto = True

    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    placement_option_id = fields.Many2one(
        'amazon.fba.placement.option', required=True, ondelete='cascade', index=True,
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
    amazon_shipment_id = fields.Char(required=True, copy=False, index=True)
    shipment_confirmation_id = fields.Char(copy=False, index=True)
    amazon_reference_id = fields.Char(copy=False)
    name = fields.Char(copy=False)
    status = fields.Char(copy=False, index=True)
    destination_fc = fields.Char(copy=False)
    line_ids = fields.One2many(
        'amazon.fba.physical.shipment.line', 'physical_shipment_id', string='Items',
    )
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_plan_shipment = models.Constraint(
        'UNIQUE (inbound_shipment_id, amazon_shipment_id)',
        'An Amazon shipment ID can only occur once on an inbound plan.',
    )

    @api.constrains('inbound_shipment_id', 'placement_option_id')
    def _check_placement_plan(self):
        for shipment in self:
            if shipment.placement_option_id.inbound_shipment_id != shipment.inbound_shipment_id:
                raise ValidationError(_("The physical shipment placement option must belong to the same inbound plan."))


class AmazonFbaPhysicalShipmentLine(models.Model):
    _name = 'amazon.fba.physical.shipment.line'
    _description = 'Amazon FBA Physical Shipment Item'
    _order = 'msku, id'
    _check_company_auto = True

    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', related='physical_shipment_id.inbound_shipment_id',
        store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='physical_shipment_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='physical_shipment_id.company_id',
        store=True, readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one('amazon.product', ondelete='restrict', index=True)
    msku = fields.Char(required=True, index=True)
    asin = fields.Char()
    fnsku = fields.Char()
    quantity = fields.Integer(required=True)
    raw_response = fields.Text(
        readonly=True, copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_shipment_msku = models.Constraint(
        'UNIQUE (physical_shipment_id, msku)',
        'An MSKU can only occur once in a physical Amazon shipment.',
    )
    _positive_quantity = models.Constraint(
        'CHECK (quantity > 0 AND quantity <= 500000)',
        'Physical shipment item quantity must be from 1 to 500000.',
    )

    @api.constrains('amazon_product_id', 'instance_id', 'msku')
    def _check_product_mapping(self):
        for item in self.filtered('amazon_product_id'):
            if item.amazon_product_id.instance_id != item.instance_id:
                raise ValidationError(_("The physical shipment product must belong to the same instance."))
            if (item.amazon_product_id.sku or '').strip() != (item.msku or '').strip():
                raise ValidationError(_("The physical shipment MSKU must match the Amazon Product SKU."))
