from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockLocation(models.Model):
    _inherit = 'stock.location'

    amazon_fba_location_type = fields.Selection(
        [
            ('transit', 'FBA Transit'),
            ('received', 'FBA Received / Staging'),
            ('sellable', 'FBA Sellable'),
            ('reserved', 'FBA Reserved'),
            ('unsellable', 'FBA Unsellable'),
            ('return_source', 'FBA Customer Return Source'),
            ('removal_transit', 'FBA Removal Transit'),
            ('disposal', 'FBA Disposal / Inventory Loss'),
        ],
        string='Amazon FBA Location Type',
        copy=False,
        index=True,
        help="Stable connector marker used to identify the location's FBA inventory role.",
    )
    amazon_instance_id = fields.Many2one(
        'amazon.instance',
        string='Amazon Instance',
        check_company=True,
        copy=False,
        index=True,
        ondelete='restrict',
        help="Amazon instance that owns this connector-managed FBA location.",
    )

    _amazon_fba_instance_role_unique = models.Constraint(
        'UNIQUE (amazon_instance_id, amazon_fba_location_type)',
        'An Amazon instance can have only one connector-managed location for each FBA role.',
    )

    @api.constrains(
        'amazon_fba_location_type',
        'amazon_instance_id',
        'usage',
        'company_id',
        'location_id',
        'active',
    )
    def _check_amazon_fba_location_configuration(self):
        expected_usage = {
            'transit': 'transit',
            'received': 'internal',
            'sellable': 'internal',
            'reserved': 'internal',
            'unsellable': 'internal',
            'return_source': 'customer',
            'removal_transit': 'transit',
            'disposal': 'inventory',
        }
        for location in self:
            role = location.amazon_fba_location_type
            instance = location.amazon_instance_id
            if bool(role) != bool(instance):
                raise ValidationError(_(
                    "Amazon FBA Location Type and Amazon Instance must be configured together."
                ))
            if not role:
                continue
            if not location.active:
                raise ValidationError(_("A connector-managed FBA location must be active."))
            if not instance.company_id or location.company_id != instance.company_id:
                raise ValidationError(_(
                    "A connector-managed FBA location must belong to its Amazon instance company."
                ))
            if location.usage != expected_usage[role]:
                raise ValidationError(_(
                    "The location type does not match the selected Amazon FBA role."
                ))

            warehouse = instance.fba_warehouse_id
            if role in {'received', 'sellable', 'reserved', 'unsellable'}:
                stock_location = warehouse.lot_stock_id if warehouse else False
                if (
                    not stock_location
                    or location == stock_location
                    or not location._child_of(stock_location)
                ):
                    raise ValidationError(_(
                        "Received/Staging, Sellable, Reserved, and Unsellable FBA locations must be below "
                        "the configured FBA warehouse Stock location."
                    ))
            elif warehouse and location._child_of(warehouse.lot_stock_id):
                raise ValidationError(_(
                    "Amazon FBA virtual/transit locations cannot be inside the FBA warehouse Stock hierarchy."
                ))
