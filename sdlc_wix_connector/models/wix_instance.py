from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class WixInstance(models.Model):
    _name = 'wix.instance'
    _description = 'Wix Store Connection'
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )

    app_id = fields.Char(
        'Wix App ID',
        help="Identifier of the Wix custom app that sends the webhooks.",
    )
    site_id = fields.Char(
        'Wix Site ID',
        help="The Wix site identifier. Only needed for outbound Wix REST API "
             "calls, not for inbound webhook processing.",
    )
    api_key = fields.Char(
        'Wix API Key',
        help="Credential for any outbound call to the Wix API. Not required "
             "for inbound webhook processing.",
    )
    webhook_public_key = fields.Text(
        'Webhook Public Key (PEM)',
        help="RSA public key of the Wix app, copied from the Wix app dashboard "
             "under 'View ID & keys'. Wix signs every webhook as an RS256 JWT; "
             "this key is what proves a delivery really came from Wix. Inbound "
             "webhooks are rejected while this is empty.",
    )

    order_import_enabled = fields.Boolean(
        'Enable Order Import',
        default=True,
        help="Explicit gate for processing inbound Wix order webhooks for this "
             "connection. Disabling this stops new sale orders from being "
             "created without disabling the module.",
    )
    order_status_sync_enabled = fields.Boolean(
        'Apply Order Status Updates',
        default=True,
        help="When enabled, Wix 'updated' and 'canceled' events refresh the "
             "status fields on the linked sale order. Disabling this keeps the "
             "events stored for audit without touching the order.",
    )
    order_cancel_policy = fields.Selection([
        ('flag_only', 'Flag for review only'),
        ('cancel_when_safe', 'Cancel the sale order when safe'),
    ], string='On Wix Cancellation', default='flag_only', required=True,
        help="What to do when Wix reports an order as cancelled. "
             "'Flag for review only' never changes the order, it just marks it. "
             "'Cancel when safe' additionally cancels the Odoo order, but only "
             "when it has no validated delivery and no posted invoice.",
    )

    default_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Default Warehouse',
        check_company=True,
        domain="[('active', '=', True), ('company_id', '=', company_id)]",
        help="Warehouse used for sale orders created from this Wix connection "
             "when no other warehouse can be determined. Required before order "
             "import is enabled.",
    )
    default_pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Default Pricelist',
        check_company=True,
        domain="[('company_id', 'in', [company_id, False])]",
    )
    default_fiscal_position_id = fields.Many2one(
        'account.fiscal.position',
        string='Default Fiscal Position',
        check_company=True,
        domain="[('company_id', 'in', [company_id, False])]",
    )
    default_sales_team_id = fields.Many2one(
        'crm.team',
        string='Default Sales Team',
        check_company=True,
        domain="[('company_id', 'in', [company_id, False])]",
    )

    # --- Order financial mapping ---
    sales_tax_id = fields.Many2one(
        'account.tax', string='Sales Tax',
        domain="[('company_id', '=', company_id), ('type_tax_use', '=', 'sale'),"
               " ('active', '=', True)]",
        help="Tax applied to imported Wix order lines. Leave empty to carry "
             "Wix's prices with no tax, which keeps the Odoo total equal to "
             "the Wix total. Set a price-included tax only once an accountant "
             "has confirmed it matches how Wix prices are quoted.",
    )
    shipping_product_id = fields.Many2one(
        'product.product', string='Shipping Product',
        domain="['|', ('company_id', '=', company_id), ('company_id', '=', False)]",
        help="Service product used for the shipping Wix charged the customer. "
             "Resolved or created automatically when left empty.",
    )
    discount_product_id = fields.Many2one(
        'product.product', string='Discount Product',
        domain="['|', ('company_id', '=', company_id), ('company_id', '=', False)]",
        help="Service product used for Wix order-level discounts, added as a "
             "negative line. Resolved or created automatically when empty.",
    )
    fee_product_id = fields.Many2one(
        'product.product', string='Additional Fee Product',
        domain="['|', ('company_id', '=', company_id), ('company_id', '=', False)]",
        help="Service product used for Wix additional fees. Resolved or "
             "created automatically when left empty.",
    )
    total_mismatch_policy = fields.Selection([
        ('flag', 'Import and flag for review'),
        ('fail', 'Refuse the import'),
    ], string='On Total Mismatch', default='flag', required=True,
        help="What to do when the Odoo order total does not match the total "
             "Wix reported. A mismatch means the money in Odoo is wrong, so "
             "it is never passed over silently.",
    )
    total_mismatch_tolerance = fields.Float(
        'Mismatch Tolerance', default=0.02,
        help="Absolute difference tolerated between the Odoo and Wix totals, "
             "to absorb rounding.",
    )

    def _get_service_product(self, config_field, default_code, name):
        """Return, or idempotently create, a company-safe service product."""
        self.ensure_one()
        configured = self[config_field]
        if configured:
            return configured
        company = self.company_id or self.env.company
        Product = self.env['product.product'].sudo()
        product = Product.search([
            ('default_code', '=', default_code),
            ('company_id', 'in', (company.id, False)),
        ], limit=1)
        if not product:
            product = Product.create({
                'name': name,
                'default_code': default_code,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'list_price': 0.0,
                'taxes_id': [(6, 0, [])],
            })
        return product

    def _get_shipping_product(self):
        return self._get_service_product(
            'shipping_product_id', 'WIX-SHIPPING', 'Wix Shipping')

    def _get_discount_product(self):
        return self._get_service_product(
            'discount_product_id', 'WIX-DISCOUNT', 'Wix Discount')

    def _get_fee_product(self):
        return self._get_service_product(
            'fee_product_id', 'WIX-FEE', 'Wix Additional Fee')

    @api.constrains('company_id', 'sales_tax_id')
    def _check_sales_tax_company(self):
        for instance in self:
            tax = instance.sales_tax_id
            if tax and tax.company_id != instance.company_id:
                raise ValidationError(_(
                    "The Sales Tax must belong to the Wix connection company."
                ))

    @api.constrains('order_import_enabled', 'default_warehouse_id')
    def _check_order_import_requires_warehouse(self):
        for instance in self:
            if instance.order_import_enabled and not instance.default_warehouse_id:
                raise ValidationError(_(
                    "Set a Default Warehouse before enabling order import for %s.",
                    instance.name,
                ))

    @api.constrains('company_id', 'default_warehouse_id')
    def _check_default_warehouse_company(self):
        for instance in self:
            if (
                instance.default_warehouse_id
                and instance.default_warehouse_id.company_id != instance.company_id
            ):
                raise ValidationError(_(
                    "The Default Warehouse must belong to the Wix connection company."
                ))
