
import logging
import requests
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .amazon_api import (
    AmazonAPI, FEED_JSON_LISTINGS, FEED_POST_PRODUCT_PRICING, FEED_POST_INVENTORY,
    FEED_ORDER_FULFILLMENT, FEED_INVOICE_UPLOAD,
)

_logger = logging.getLogger(__name__)


FBA_LOCATION_DEFINITIONS = {
    'transit': {
        'field': 'fba_transit_location_id',
        'label': 'FBA Transit Location',
        'name': 'Amazon FBA Transit',
        'usage': 'transit',
    },
    'received': {
        'field': 'fba_received_location_id',
        'label': 'FBA Received / Staging Location',
        'name': 'Amazon FBA Received / Staging',
        'usage': 'internal',
    },
    'sellable': {
        'field': 'fba_sellable_location_id',
        'label': 'FBA Sellable Location',
        'name': 'Amazon FBA Sellable',
        'usage': 'internal',
    },
    'reserved': {
        'field': 'fba_reserved_location_id',
        'label': 'FBA Reserved Location',
        'name': 'Amazon FBA Reserved',
        'usage': 'internal',
    },
    'unsellable': {
        'field': 'fba_unsellable_location_id',
        'label': 'FBA Unsellable Location',
        'name': 'Amazon FBA Unsellable',
        'usage': 'internal',
    },
    'return_source': {
        'field': 'fba_return_source_location_id',
        'label': 'FBA Customer Return Source',
        'name': 'Amazon FBA Customer Returns',
        'usage': 'customer',
    },
    'sold_customer': {
        'field': 'fba_sold_customer_location_id',
        'label': 'FBA Sold / Customers Location',
        'name': 'Amazon FBA Sold / Customers',
        'usage': 'customer',
    },
    'removal_transit': {
        'field': 'fba_removal_transit_location_id',
        'label': 'FBA Removal Transit Location',
        'name': 'Amazon FBA Removal Transit',
        'usage': 'transit',
    },
    'disposal': {
        'field': 'fba_disposal_location_id',
        'label': 'FBA Disposal / Inventory Loss Location',
        'name': 'Amazon FBA Disposal / Inventory Loss',
        'usage': 'inventory',
    },
}

FBA_CONFIGURATION_FIELDS = {
    'fba_warehouse_id',
    'fba_source_location_id',
    'fba_ship_from_partner_id',
    'fba_removal_return_partner_id',
    'fba_sale_stock_cutover_at',
    *(definition['field'] for definition in FBA_LOCATION_DEFINITIONS.values()),
}
SETTLEMENT_ACCOUNTING_FIELDS = {
    'settlement_accounting_strategy', 'settlement_accounting_cutoff_date',
    'settlement_journal_id', 'amazon_payout_bank_journal_id',
    'amazon_clearing_account_id',
    'amazon_sales_account_id', 'amazon_refund_account_id',
    'amazon_fee_account_id', 'amazon_fba_fee_account_id',
    'amazon_reimbursement_account_id', 'amazon_promotion_account_id',
    'amazon_adjustment_account_id', 'amazon_shipping_account_id',
    'amazon_tax_account_id', 'amazon_other_credit_account_id',
    'amazon_other_debit_account_id', 'amazon_suspense_account_id',
}


def _amazon_datetime_to_odoo(value):
    """Convert Amazon SP-API ISO datetimes to Odoo naive UTC datetimes."""
    if not value:
        return False

    if isinstance(value, datetime):
        date_value = value
    elif isinstance(value, str):
        normalized_value = value.strip()
        if not normalized_value:
            return False
        if normalized_value.endswith('Z'):
            normalized_value = normalized_value[:-1] + '+00:00'
        try:
            date_value = datetime.fromisoformat(normalized_value)
        except ValueError:
            _logger.warning("Invalid Amazon datetime value: %s", value)
            return False
    else:
        _logger.warning("Unsupported Amazon datetime value %r of type %s", value, type(value).__name__)
        return False

    if date_value.tzinfo:
        date_value = date_value.astimezone(timezone.utc).replace(tzinfo=None)
    return date_value


# Amazon Marketplace ID → SP-API region
MARKETPLACE_REGION = {
    # North America
    'ATVPDKIKX0DER': 'na',   # US
    'A2EUQ1WTGCTBG2': 'na',  # Canada
    'A1AM78C64UM0Y8': 'na',  # Mexico
    'A2Q3Y263D00KWC': 'na',  # Brazil
    # Europe
    'A1F83G8C2ARO7P': 'eu',  # UK
    'A13V1IB3VIYZZH': 'eu',  # France
    'A1PA6795UKMFR9': 'eu',  # Germany
    'APJ6JRA9NG5V4': 'eu',   # Italy
    'A1RKKUPIHCS9HS': 'eu',  # Spain
    'A1805IZSGTT6HS': 'eu',  # Netherlands
    'A33AVAJ2PDY3EV': 'eu',  # Turkey
    'A2VIGQ35RCS4UG': 'eu',  # UAE
    'A21TJRUUN4KGV': 'eu',   # India
    'A17E79C6D8DWNP': 'eu',  # Saudi Arabia
    'ARBP9OOSHTCHU': 'eu',   # Egypt
    'A1C3SOZRARQ6R3': 'eu',  # Poland
    'A2NODRKZP88ZB9': 'eu',  # Sweden
    'AE08WJ6YKNBMC': 'eu',   # South Africa
    'AMEN7PMS3EDWL': 'eu',   # Belgium
    # Far East
    'A1VC38T7YXB528': 'fe',  # Japan
    'A39IBJ37TRP1C6': 'fe',  # Australia
    'A19VAU5U5O7RUS': 'fe',  # Singapore
}


class AmazonInstance(models.Model):
    _name = 'amazon.instance'
    _description = 'Amazon Instance'
    _check_company_auto = True

    name = fields.Char(required=True)
    seller_id = fields.Char('Seller ID')
    marketplace_id = fields.Char('Marketplace ID')
    refresh_token = fields.Text('Refresh Token')
    client_id = fields.Char('Client ID')
    client_secret = fields.Char('Client Secret')
    aws_access_key = fields.Char('AWS Access Key')
    aws_secret_key = fields.Char('AWS Secret Key')
    region = fields.Selection([
        ('eu', 'Europe'),
        ('na', 'North America'),
        ('fe', 'Far East'),
    ], default='eu')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        help="Company used for currency fallback and products created from Amazon listings.",
    )

    # Fulfillment program
    fulfillment_program = fields.Selection([
        ('none', 'None'),
        ('pan_eu', 'Pan-European (PAN EU)'),
        ('efn', 'European Fulfillment Network (EFN)'),
        ('mci', 'Multi-Country Inventory (MCI)'),
        ('cfn', 'Central Fulfillment Network (CFN)'),
    ], string='Fulfillment Program', default='none')

    # Warehouses
    fba_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='FBA Warehouse',
        check_company=True,
        domain="[('active', '=', True), ('company_id', '=', company_id)]",
        help="Warehouse whose Stock location contains the Amazon FBA inventory locations.",
    )
    fbm_warehouse_id = fields.Many2one('stock.warehouse', string='FBM Warehouse')
    fba_source_location_id = fields.Many2one(
        'stock.location',
        string='FBA Source Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Client warehouse stock location from which inventory will later be sent to Amazon.",
    )
    fba_ship_from_partner_id = fields.Many2one(
        'res.partner',
        string='FBA Ship-From Address',
        check_company=True,
        domain="[('active', '=', True), ('company_id', 'in', [company_id, False])]",
        help="Physical source address sent to Amazon when creating an FBA inbound plan.",
    )
    fba_transit_location_id = fields.Many2one(
        'stock.location',
        string='FBA Transit Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'transit'), ('company_id', '=', company_id)]",
        help="Company-owned inventory sent to Amazon but not received by Amazon yet.",
    )
    fba_received_location_id = fields.Many2one(
        'stock.location',
        string='FBA Received / Staging Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        help=(
            "Company-owned inventory Amazon has physically received, before a separate "
            "inventory reconciliation assigns Sellable, Reserved, or Unsellable disposition."
        ),
    )
    fba_sellable_location_id = fields.Many2one(
        'stock.location',
        string='FBA Sellable Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        help="Company-owned inventory Amazon has received and can sell.",
    )
    fba_reserved_location_id = fields.Many2one(
        'stock.location',
        string='FBA Reserved Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        help="Company-owned inventory Amazon has reserved.",
    )
    fba_unsellable_location_id = fields.Many2one(
        'stock.location',
        string='FBA Unsellable Location',
        check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        help="Company-owned inventory Amazon holds but cannot sell.",
    )
    fba_return_source_location_id = fields.Many2one(
        'stock.location', string='FBA Customer Return Source', check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'customer'), ('company_id', '=', company_id)]",
        help="Legacy virtual location retained for compatibility. Current FBA return imports never create stock moves.",
    )
    fba_sold_customer_location_id = fields.Many2one(
        'stock.location', string='FBA Sold / Customers Location', check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'customer'), ('company_id', '=', company_id)]",
        help=(
            "Auditable customer-usage destination for quantities Amazon has cumulatively fulfilled. "
            "It removes company-owned stock from FBA Sellable without touching WH/Stock."
        ),
    )
    fba_removal_transit_location_id = fields.Many2one(
        'stock.location', string='FBA Removal Transit Location', check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'transit'), ('company_id', '=', company_id)]",
        help="Inventory dispatched by Amazon but not physically received in the customer warehouse.",
    )
    fba_disposal_location_id = fields.Many2one(
        'stock.location', string='FBA Disposal / Inventory Loss Location', check_company=True,
        domain="[('active', '=', True), ('usage', '=', 'inventory'), ('company_id', '=', company_id)]",
    )
    fba_removal_return_partner_id = fields.Many2one(
        'res.partner', string='FBA Removal Return Address', check_company=True,
        domain="[('active', '=', True), ('company_id', 'in', [company_id, False])]",
        help="Dedicated destination for return-to-address removals. It is never inferred from Ship-From.",
    )
    fba_sale_stock_cutover_at = fields.Datetime(
        string='FBA Sale Stock Cutover',
        help=(
            "Amazon fulfilled orders whose Amazon Purchase Date is before this timestamp "
            "are considered already represented in the opening FBA inventory baseline. "
            "They must not generate FBA Sellable stock depletion in Odoo. Changing this "
            "after live FBA sale stock processing is dangerous and is blocked when "
            "post-cutover event-owned stock movement already exists."
        ),
    )
    return_stock_policy = fields.Selection([
        ('informational', 'Informational Only'),
        ('event_moves', 'Legacy Event Moves (Disabled)'),
        ('audit_only', 'Reconcile through Inventory Audit Only'),
    ], default='audit_only', required=True,
        help="FBA customer-return rows never modify stock. Inventory disposition is applied only through reviewed FBA Inventory Reconciliation.")
    adjustment_stock_policy = fields.Selection([
        ('informational', 'Informational Only'),
        ('event_moves', 'Create Moves from Trusted Events'),
        ('audit_only', 'Reconcile through Inventory Audit Only'),
    ], default='informational', required=True)
    last_fba_return_sync_at = fields.Datetime(readonly=True)
    last_fba_removal_sync_at = fields.Datetime(readonly=True)
    last_fba_adjustment_sync_at = fields.Datetime(readonly=True)
    last_fba_reimbursement_sync_at = fields.Datetime(readonly=True)
    last_settlement_sync_at = fields.Datetime(readonly=True)

    # Settlement accounting. These mappings are intentionally instance- and
    # company-specific; settlement entries never post directly to a bank.
    settlement_accounting_strategy = fields.Selection([
        ('settlement_based', 'Settlement-Based'),
        ('invoice_aware', 'Invoice-Aware'),
    ], string='Settlement Accounting Strategy', required=True,
       default='settlement_based',
       help=(
           'Settlement-Based recognizes financial components from the matched Amazon '
           'settlement. Invoice-Aware uses only safely linked posted customer documents '
           'for sales, refunds, shipping, promotions and tax. Select and validate one '
           'strategy before financial go-live; the connector never switches per line.'
       ))
    settlement_accounting_cutoff_date = fields.Date(
        string='Settlement Accounting Cut-Off',
        help=(
            'Only settlements whose Amazon deposit date is on or after this date may '
            'create connector accounting entries. Earlier accounting remains the '
            'legacy responsibility and may still be imported as read-only evidence.'
        ),
    )
    settlement_journal_id = fields.Many2one(
        'account.journal', string='Settlement Journal', check_company=True,
        ondelete='restrict', domain="[('company_id', '=', company_id), ('type', '=', 'general')]",
    )
    amazon_payout_bank_journal_id = fields.Many2one(
        'account.journal', string='Amazon Payout Bank Journal', check_company=True,
        ondelete='restrict', domain="[('company_id', '=', company_id), ('type', '=', 'bank')]",
        help=(
            'Bank journal used only when an accounting user confirms an actual Amazon '
            'receipt manually. Settlement deposit dates never create bank entries.'
        ),
    )
    amazon_clearing_account_id = fields.Many2one(
        'account.account', string='Amazon Clearing Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_sales_account_id = fields.Many2one(
        'account.account', string='Amazon Sales Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_refund_account_id = fields.Many2one(
        'account.account', string='Amazon Refund Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_fee_account_id = fields.Many2one(
        'account.account', string='Amazon Fee Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_fba_fee_account_id = fields.Many2one(
        'account.account', string='Amazon FBA Fee Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_reimbursement_account_id = fields.Many2one(
        'account.account', string='Amazon Reimbursement Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_promotion_account_id = fields.Many2one(
        'account.account', string='Amazon Promotion Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_adjustment_account_id = fields.Many2one(
        'account.account', string='Amazon Adjustment Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_shipping_account_id = fields.Many2one(
        'account.account', string='Amazon Shipping Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_tax_account_id = fields.Many2one(
        'account.account', string='Amazon Tax Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_other_credit_account_id = fields.Many2one(
        'account.account', string='Amazon Other Credit Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_other_debit_account_id = fields.Many2one(
        'account.account', string='Amazon Other Debit Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
    )
    amazon_suspense_account_id = fields.Many2one(
        'account.account', string='Amazon Suspense Account', check_company=True,
        ondelete='restrict', domain="[('company_ids', 'in', [company_id])]",
        help='Reserved for an explicit future review workflow. Unknown settlement categories block draft creation and are never silently posted here.',
    )

    # Defaults
    default_currency_id = fields.Many2one('res.currency', string='Default Currency')

    # AI Configuration
    ai_provider = fields.Selection([
        ('groq', 'Groq'),
        ('openai', 'OpenAI (GPT)'),
        ('anthropic', 'Anthropic (Claude)'),
        ('gemini', 'Google (Gemini)'),
    ], string='AI Provider', default='groq')
    ai_api_key = fields.Char('AI API Key')
    ai_model = fields.Char(
        'AI Model', default='llama-3.3-70b-versatile',
        help="Groq: llama-3.3-70b-versatile, mixtral-8x7b-32768\n"
             "OpenAI: gpt-4o-mini\nClaude: claude-sonnet-4-20250514\nGemini: gemini-2.0-flash",
    )
    ai_connection_status = fields.Char('AI Status', readonly=True)

    # Counts
    product_ids = fields.One2many('amazon.product', 'instance_id', string='Amazon Products')
    product_count = fields.Integer(compute='_compute_counts', string='Products')
    order_count = fields.Integer(compute='_compute_counts', string='Orders')
    settlement_count = fields.Integer(compute='_compute_counts', string='Settlements')

    # Sync timestamps
    last_order_sync = fields.Datetime('Last Order Sync')
    last_stock_sync = fields.Datetime('Last Stock Sync')
    last_product_sync = fields.Datetime('Last Product Sync')

    # Order import controls
    initial_order_import_from = fields.Datetime(
        'Initial Order Import From',
        help="Optional start datetime for the next order import job. Leave empty for normal incremental sync.",
    )
    initial_order_import_to = fields.Datetime(
        'Initial Order Import To',
        help="Optional end datetime for the next order import job. Use this for safe initial one-day imports.",
    )
    order_import_batch_size = fields.Integer(
        'Import Batch Size',
        default=10,
        help="Maximum Amazon orders processed by one background cron transaction.",
    )
    order_import_overlap_minutes = fields.Integer(
        'Order Import Overlap Minutes',
        default=10,
        help="Normal incremental sync starts this many minutes before Last Order Sync. Idempotency prevents duplicates.",
    )

    # Order status sync controls
    order_status_sync_enabled = fields.Boolean(
        'Enable Automatic Status Sync',
        default=True,
        help="When enabled, the status-sync cron creates resumable jobs for this instance.",
    )
    order_status_sync_interval = fields.Integer(
        'Status Sync Interval',
        default=15,
        help="Minimum minutes between automatic Amazon order status synchronization jobs.",
    )
    last_status_sync_at = fields.Datetime('Last Status Sync At', readonly=True)
    status_sync_lookback_minutes = fields.Integer(
        'Status Sync Lookback Minutes',
        default=10,
        help="Overlap applied to Last Status Sync At so Amazon updates are not missed.",
    )
    status_sync_batch_size = fields.Integer(
        'Status Sync Batch Size',
        default=10,
        help="Maximum Amazon orders processed by one status-sync cron transaction.",
    )
    auto_confirm_sale_order_on_unshipped = fields.Boolean(
        'Auto Confirm Sale Order on Unshipped',
        default=False,
    )
    auto_confirm_sale_order_on_shipped = fields.Boolean(
        'Auto Confirm Sale Order on Shipped',
        default=False,
    )
    auto_cancel_draft_sale_order_on_amazon_cancellation = fields.Boolean(
        'Auto Cancel Draft Sale Order on Amazon Cancellation',
        default=False,
    )
    auto_cancel_confirmed_sale_order_when_safe = fields.Boolean(
        'Auto Cancel Confirmed Sale Order When Safe',
        default=False,
        help="Only applies when no completed delivery, posted invoice, or paid invoice exists.",
    )
    auto_validate_delivery_on_shipped = fields.Boolean(
        'Auto Validate Delivery on Shipped',
        default=False,
        help="Disabled by default. Current implementation never validates without exact shipped quantity verification.",
    )
    auto_create_invoice_on_shipped = fields.Boolean(
        'Auto Create Invoice on Shipped',
        default=False,
    )
    auto_post_invoice = fields.Boolean(
        'Auto Post Invoice',
        default=False,
    )
    create_activity_on_status_conflict = fields.Boolean(
        'Create Activity on Status Conflict',
        default=True,
    )

    _order_import_batch_size_range = models.Constraint(
        'CHECK (order_import_batch_size IS NULL OR (order_import_batch_size >= 1 AND order_import_batch_size <= 100))',
        'Order import batch size must be between 1 and 100.',
    )
    _order_import_overlap_non_negative = models.Constraint(
        'CHECK (order_import_overlap_minutes IS NULL OR order_import_overlap_minutes >= 0)',
        'Order import overlap minutes cannot be negative.',
    )
    _status_sync_batch_size_range = models.Constraint(
        'CHECK (status_sync_batch_size IS NULL OR (status_sync_batch_size >= 1 AND status_sync_batch_size <= 100))',
        'Status sync batch size must be between 1 and 100.',
    )
    _status_sync_interval_positive = models.Constraint(
        'CHECK (order_status_sync_interval IS NULL OR order_status_sync_interval >= 1)',
        'Status sync interval must be at least 1 minute.',
    )

    @api.constrains(
        'company_id', 'settlement_journal_id', 'amazon_payout_bank_journal_id',
        'amazon_clearing_account_id',
        'amazon_sales_account_id', 'amazon_refund_account_id',
        'amazon_fee_account_id', 'amazon_fba_fee_account_id',
        'amazon_reimbursement_account_id', 'amazon_promotion_account_id',
        'amazon_adjustment_account_id', 'amazon_shipping_account_id',
        'amazon_tax_account_id', 'amazon_other_credit_account_id',
        'amazon_other_debit_account_id', 'amazon_suspense_account_id',
    )
    def _check_settlement_accounting_company(self):
        account_fields = (
            'amazon_clearing_account_id', 'amazon_sales_account_id',
            'amazon_refund_account_id', 'amazon_fee_account_id',
            'amazon_fba_fee_account_id', 'amazon_reimbursement_account_id',
            'amazon_promotion_account_id', 'amazon_adjustment_account_id',
            'amazon_shipping_account_id', 'amazon_tax_account_id',
            'amazon_other_credit_account_id', 'amazon_other_debit_account_id',
            'amazon_suspense_account_id',
        )
        for instance in self:
            if (
                instance.settlement_journal_id
                and instance.settlement_journal_id.company_id != instance.company_id
            ):
                raise ValidationError(_(
                    'The settlement journal must belong to the Amazon instance company.'
                ))
            if instance.amazon_payout_bank_journal_id and (
                instance.amazon_payout_bank_journal_id.company_id != instance.company_id
                or instance.amazon_payout_bank_journal_id.type != 'bank'
            ):
                raise ValidationError(_(
                    'The Amazon payout journal must be a bank journal of the Amazon instance company.'
                ))
            for field_name in account_fields:
                account = instance[field_name]
                if account and instance.company_id not in account.company_ids:
                    raise ValidationError(_(
                        '%(account)s is not available to company %(company)s.',
                        account=account.display_name, company=instance.company_id.display_name,
                    ))
    _status_sync_lookback_non_negative = models.Constraint(
        'CHECK (status_sync_lookback_minutes IS NULL OR status_sync_lookback_minutes >= 0)',
        'Status sync lookback minutes cannot be negative.',
    )

    @api.model
    def _check_amazon_manager_access(self):
        if (
            self.env.su
            or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager')
            or self.env.user.has_group('base.group_system')
        ):
            return
        raise AccessError(_(
            "Only an Amazon Connector Manager or Technical Administrator can run "
            "Amazon synchronization operations."
        ))

    @api.model
    def _check_fba_configuration_access(self):
        if (
            self.env.su
            or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager')
            or self.env.user.has_group('stock.group_stock_manager')
        ):
            return
        raise AccessError(_(
            "Only an Amazon Connector Manager or Inventory Administrator can change "
            "the FBA stock structure configuration."
        ))

    @api.model
    def _check_settlement_accounting_access(self):
        if self.env.su:
            return
        is_accountant = (
            self.env.user.has_group('account.group_account_user')
            or self.env.user.has_group('account.group_account_manager')
        )
        is_amazon_manager = self.env.user.has_group(
            'sdlc_amazon_connector.group_amazon_manager'
        )
        if not (is_accountant and is_amazon_manager):
            raise AccessError(_(
                'Only an Accounting user who is also an Amazon Manager can change '
                'settlement accounting mappings.'
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if any(FBA_CONFIGURATION_FIELDS.intersection(vals) for vals in vals_list):
            self._check_fba_configuration_access()
        if any(SETTLEMENT_ACCOUNTING_FIELDS.intersection(vals) for vals in vals_list):
            self._check_settlement_accounting_access()
        return super().create(vals_list)

    def write(self, vals):
        if FBA_CONFIGURATION_FIELDS.intersection(vals):
            self._check_fba_configuration_access()
        if SETTLEMENT_ACCOUNTING_FIELDS.intersection(vals):
            self._check_settlement_accounting_access()
        if 'fba_sale_stock_cutover_at' in vals:
            new_cutover = (
                fields.Datetime.to_datetime(vals['fba_sale_stock_cutover_at'])
                if vals.get('fba_sale_stock_cutover_at')
                else False
            )
            for instance in self:
                old_cutover = instance.fba_sale_stock_cutover_at
                if (old_cutover or False) == (new_cutover or False):
                    continue
                if old_cutover and instance._has_processed_fba_sale_stock_at_or_after(old_cutover):
                    raise UserError(_(
                        "FBA Sale Stock Cutover cannot change after live post-cutover "
                        "FBA sale stock movement exists. Use a controlled repair or "
                        "migration process."
                    ))
                if new_cutover and instance._has_processed_fba_sale_stock_at_or_after(new_cutover):
                    raise UserError(_(
                        "FBA Sale Stock Cutover cannot be set to %(cutover)s because "
                        "event-owned FBA sale stock movement already exists for orders "
                        "on or after that timestamp, or for orders without a Purchase Date.",
                        cutover=new_cutover,
                    ))
        protected_financial_fields = {
            'settlement_accounting_strategy', 'settlement_accounting_cutoff_date',
        }
        if protected_financial_fields.intersection(vals):
            for instance in self:
                new_strategy = vals.get(
                    'settlement_accounting_strategy', instance.settlement_accounting_strategy,
                )
                new_cutoff = (
                    fields.Date.to_date(vals['settlement_accounting_cutoff_date'])
                    if 'settlement_accounting_cutoff_date' in vals
                    and vals['settlement_accounting_cutoff_date']
                    else (
                        False
                        if 'settlement_accounting_cutoff_date' in vals
                        else instance.settlement_accounting_cutoff_date
                    )
                )
                if (
                    new_strategy == instance.settlement_accounting_strategy
                    and new_cutoff == instance.settlement_accounting_cutoff_date
                ):
                    continue
                if self.env['amazon.settlement.report'].sudo().search_count([
                    ('instance_id', '=', instance.id), ('account_move_id', '!=', False),
                ]):
                    raise UserError(_(
                        'Settlement accounting strategy and cut-off cannot change after a '
                        'settlement accounting entry exists. Use a controlled migration.'
                    ))
        return super().write(vals)

    def _has_processed_fba_sale_stock_at_or_after(self, cutover_at):
        """Return whether a cutover edit would reclassify processed live stock."""
        self.ensure_one()
        domain = [
            ('instance_id', '=', self.id),
            ('state', '!=', 'historical'),
            ('processed_fulfilled_qty', '>', 0),
            ('picking_ids.state', '=', 'done'),
        ]
        if cutover_at:
            domain.extend([
                '|',
                ('order_id.purchase_date', '=', False),
                ('order_id.purchase_date', '>=', cutover_at),
            ])
        return bool(self.env['amazon.fba.sale.stock.event'].sudo().search_count(domain))

    @api.constrains(
        'company_id',
        'fba_warehouse_id',
        'fba_source_location_id',
        'fba_ship_from_partner_id',
        'fba_transit_location_id',
        'fba_received_location_id',
        'fba_sellable_location_id',
        'fba_reserved_location_id',
        'fba_unsellable_location_id',
        'fba_return_source_location_id',
        'fba_sold_customer_location_id',
        'fba_removal_transit_location_id',
        'fba_disposal_location_id',
        'fba_removal_return_partner_id',
    )
    def _check_fba_stock_configuration(self):
        for instance in self:
            company = instance.company_id
            warehouse = instance.fba_warehouse_id
            ship_from_partner = instance.fba_ship_from_partner_id
            if ship_from_partner:
                if not ship_from_partner.active:
                    raise ValidationError(_("The FBA Ship-From Address must be active."))
                if ship_from_partner.company_id and ship_from_partner.company_id != company:
                    raise ValidationError(_(
                        "The FBA Ship-From Address must belong to the Amazon instance "
                        "company or be a shared contact."
                    ))
            removal_partner = instance.fba_removal_return_partner_id
            if removal_partner:
                if not removal_partner.active:
                    raise ValidationError(_("The FBA Removal Return Address must be active."))
                if removal_partner.company_id and removal_partner.company_id != company:
                    raise ValidationError(_(
                        "The FBA Removal Return Address must belong to the instance company or be shared."
                    ))
            if warehouse:
                if not company or warehouse.company_id != company:
                    raise ValidationError(_(
                        "The FBA warehouse must belong to the Amazon instance company."
                    ))
                if not warehouse.active:
                    raise ValidationError(_("The FBA warehouse must be active."))

            configured_locations = {
                'source': instance.fba_source_location_id,
                **{
                    role: instance[definition['field']]
                    for role, definition in FBA_LOCATION_DEFINITIONS.items()
                },
            }
            used_location_ids = {}
            for role, location in configured_locations.items():
                if not location:
                    continue
                label = (
                    _("FBA Source Location")
                    if role == 'source'
                    else _(FBA_LOCATION_DEFINITIONS[role]['label'])
                )
                if not location.active:
                    raise ValidationError(_("%s must be active.", label))

                expected_usage = (
                    'internal'
                    if role == 'source'
                    else FBA_LOCATION_DEFINITIONS[role]['usage']
                )
                if location.usage != expected_usage:
                    raise ValidationError(_(
                        "%s must have location type %s.", label, expected_usage
                    ))

                if role == 'source':
                    if location.company_id and location.company_id != company:
                        raise ValidationError(_(
                            "The FBA Source Location must belong to the Amazon instance company "
                            "or be a shared location."
                        ))
                    if location.amazon_fba_location_type:
                        raise ValidationError(_(
                            "The FBA Source Location cannot also be a connector-managed FBA location."
                        ))
                elif not company or location.company_id != company:
                    raise ValidationError(_(
                        "%s must belong to the Amazon instance company.", label
                    ))

                previous_role = used_location_ids.get(location.id)
                if previous_role:
                    raise ValidationError(_(
                        "The same stock location cannot be assigned to both %s and %s.",
                        previous_role,
                        label,
                    ))
                used_location_ids[location.id] = label

                if role in {'received', 'sellable', 'reserved', 'unsellable'}:
                    stock_location = warehouse.lot_stock_id if warehouse else False
                    if (
                        not stock_location
                        or location == stock_location
                        or not location._child_of(stock_location)
                    ):
                        raise ValidationError(_(
                            "%s must be below the configured FBA warehouse Stock location.", label
                        ))
                elif (
                    role in {'transit', 'return_source', 'removal_transit', 'disposal'}
                    and warehouse
                    and location._child_of(warehouse.lot_stock_id)
                ):
                    raise ValidationError(_(
                        "%s cannot be inside the FBA warehouse Stock hierarchy.", label
                    ))

                if role != 'source' and (
                    location.amazon_instance_id
                    and location.amazon_instance_id != instance
                ):
                    raise ValidationError(_(
                        "%s is managed by another Amazon instance.", label
                    ))
                if role != 'source' and (
                    location.amazon_fba_location_type
                    and location.amazon_fba_location_type != role
                ):
                    raise ValidationError(_(
                        "%s is marked for a different FBA role.", label
                    ))

    def _validate_fba_setup_location(self, location, role):
        """Validate one setup candidate without changing stock."""
        self.ensure_one()
        definition = FBA_LOCATION_DEFINITIONS[role]
        label = _(definition['label'])
        if not location.active:
            raise UserError(_("%s must be active.", label))
        if location.usage != definition['usage']:
            raise UserError(_(
                "%s must have location type %s.", label, definition['usage']
            ))
        if location.company_id != self.company_id:
            raise UserError(_("%s must belong to the Amazon instance company.", label))
        if role in {'received', 'sellable', 'reserved', 'unsellable'}:
            stock_location = self.fba_warehouse_id.lot_stock_id
            if location == stock_location or not location._child_of(stock_location):
                raise UserError(_(
                    "%s must be below the configured FBA warehouse Stock location.", label
                ))
        elif self.fba_warehouse_id and location._child_of(self.fba_warehouse_id.lot_stock_id):
            raise UserError(_(
                "The FBA Transit Location cannot be inside the FBA warehouse Stock hierarchy."
            ))
        if location.amazon_instance_id and location.amazon_instance_id != self:
            raise UserError(_("%s is managed by another Amazon instance.", label))
        if (
            location.amazon_fba_location_type
            and location.amazon_fba_location_type != role
        ):
            raise UserError(_("%s is marked for a different FBA role.", label))

    def _claim_fba_location(self, location, role):
        """Attach the stable connector marker. Return whether it was repaired."""
        self.ensure_one()
        self._validate_fba_setup_location(location, role)
        if (
            location.amazon_instance_id == self
            and location.amazon_fba_location_type == role
        ):
            return False

        other_location = self.env['stock.location'].sudo().with_context(active_test=False).search([
            ('id', '!=', location.id),
            ('amazon_instance_id', '=', self.id),
            ('amazon_fba_location_type', '=', role),
        ], limit=1)
        if other_location:
            raise UserError(_(
                "%s is already represented by %s. Resolve the duplicate configuration first.",
                _(FBA_LOCATION_DEFINITIONS[role]['label']),
                other_location.display_name,
            ))
        if location.amazon_instance_id or location.amazon_fba_location_type:
            raise UserError(_(
                "%s already has incompatible Amazon FBA ownership metadata.",
                location.display_name,
            ))
        location.sudo().write({
            'amazon_instance_id': self.id,
            'amazon_fba_location_type': role,
        })
        return True

    def action_create_fba_stock_structure(self):
        """Create or repair only the locations required by the future FBA flow."""
        self.ensure_one()
        self._check_fba_configuration_access()
        if not self.company_id:
            raise UserError(_("Select an Instance Company before creating the FBA stock structure."))
        if not self.fba_warehouse_id:
            raise UserError(_("Select an FBA Warehouse before creating the FBA stock structure."))
        if self.fba_warehouse_id.company_id != self.company_id:
            raise UserError(_("The FBA Warehouse must belong to the Amazon instance company."))
        if not self.fba_warehouse_id.active:
            raise UserError(_("The FBA Warehouse must be active."))

        created = []
        reused = []
        linked = []
        skipped = []

        source_location = self.fba_source_location_id
        if not source_location:
            source_warehouse = self.fbm_warehouse_id
            candidate = source_warehouse.lot_stock_id if source_warehouse else False
            if (
                source_warehouse
                and source_warehouse.active
                and source_warehouse.company_id == self.company_id
                and candidate.active
                and candidate.usage == 'internal'
                and candidate.company_id == self.company_id
            ):
                source_location = candidate
                self.sudo().write({'fba_source_location_id': source_location.id})
                linked.append(_("FBA Source Location"))
            else:
                raise UserError(_(
                    "Select the FBA Source Location. No safe source location could be determined "
                    "from an already configured source warehouse."
                ))
        else:
            if not source_location.active or source_location.usage != 'internal':
                raise UserError(_("The FBA Source Location must be an active internal location."))
            if source_location.company_id and source_location.company_id != self.company_id:
                raise UserError(_(
                    "The FBA Source Location must belong to the Amazon instance company "
                    "or be a shared location."
                ))
            skipped.append(_("FBA Source Location was already configured"))

        Location = self.env['stock.location'].sudo().with_context(active_test=False)
        stock_location = self.fba_warehouse_id.lot_stock_id
        for role, definition in FBA_LOCATION_DEFINITIONS.items():
            field_name = definition['field']
            label = _(definition['label'])
            parent_location = stock_location if definition['usage'] == 'internal' else False
            location = self[field_name].sudo()

            if location:
                self._validate_fba_setup_location(location, role)
                marker_repaired = self._claim_fba_location(location, role)
                reused.append(label)
                if marker_repaired:
                    linked.append(_("%s marker", label))
                continue

            location = Location.search([
                ('amazon_instance_id', '=', self.id),
                ('amazon_fba_location_type', '=', role),
            ], limit=1)
            if location:
                self._validate_fba_setup_location(location, role)
                reused.append(label)
            else:
                structural_domain = [
                    ('name', '=', definition['name']),
                    ('company_id', '=', self.company_id.id),
                    ('usage', '=', definition['usage']),
                    ('active', '=', True),
                    ('location_id', '=', parent_location.id if parent_location else False),
                ]
                candidates = Location.search(structural_domain)
                if len(candidates) > 1:
                    raise UserError(_(
                        "Multiple locations match %s. Resolve the duplicates before running setup.",
                        label,
                    ))
                if candidates:
                    location = candidates
                    self._claim_fba_location(location, role)
                    reused.append(label)
                    linked.append(_("%s marker", label))
                else:
                    location = Location.create({
                        'name': definition['name'],
                        'usage': definition['usage'],
                        'company_id': self.company_id.id,
                        'location_id': parent_location.id if parent_location else False,
                        'active': True,
                        'amazon_instance_id': self.id,
                        'amazon_fba_location_type': role,
                    })
                    created.append(label)

            self.sudo().write({field_name: location.id})
            linked.append(label)

        self._check_fba_stock_configuration()
        if not linked and not created:
            skipped.append(_("No configuration changes were needed"))

        lines = [
            _("Created: %s", ", ".join(created) if created else _("None")),
            _("Reused: %s", ", ".join(reused) if reused else _("None")),
            _("Linked/repaired: %s", ", ".join(linked) if linked else _("None")),
            _("Skipped: %s", ", ".join(skipped) if skipped else _("None")),
        ]
        _logger.info(
            "FBA stock structure configured for Amazon instance %s (id=%s): "
            "transit=%s, received=%s, sellable=%s, reserved=%s, unsellable=%s",
            self.name,
            self.id,
            self.fba_transit_location_id.display_name,
            self.fba_received_location_id.display_name,
            self.fba_sellable_location_id.display_name,
            self.fba_reserved_location_id.display_name,
            self.fba_unsellable_location_id.display_name,
        )
        return self._notify(
            _("FBA Stock Structure"),
            "\n".join(lines),
        )

    # ── Auto Sync Scheduler ──
    auto_sync_enabled = fields.Boolean('Enable Auto Sync', default=False)

    INTERVAL_SELECTION = [
        ('disabled', 'Disabled'),
        ('15min', 'Every 15 Minutes'),
        ('30min', 'Every 30 Minutes'),
        ('hourly', 'Every Hour'),
        ('2hours', 'Every 2 Hours'),
        ('4hours', 'Every 4 Hours'),
        ('6hours', 'Every 6 Hours'),
        ('12hours', 'Every 12 Hours'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ]

    order_sync_interval = fields.Selection(INTERVAL_SELECTION, string='Order Sync', default='30min')
    product_sync_interval = fields.Selection(INTERVAL_SELECTION, string='Product Sync', default='daily')
    stock_push_interval = fields.Selection(INTERVAL_SELECTION, string='Stock Push (Odoo→Amazon)', default='2hours')
    stock_pull_interval = fields.Selection(INTERVAL_SELECTION, string='Stock Pull (Amazon→Odoo)', default='disabled')
    price_push_interval = fields.Selection(INTERVAL_SELECTION, string='Price Push', default='4hours')
    price_pull_interval = fields.Selection(INTERVAL_SELECTION, string='Price Pull', default='disabled')
    settlement_sync_interval = fields.Selection(INTERVAL_SELECTION, string='Settlement Reports', default='daily')
    alert_scan_interval = fields.Selection(INTERVAL_SELECTION, string='Smart Alert Scan', default='4hours')

    # AI Auto Schedules
    ai_pricing_interval = fields.Selection(INTERVAL_SELECTION, string='AI Pricing Suggestions', default='weekly')
    ai_listing_interval = fields.Selection(INTERVAL_SELECTION, string='AI Listing Optimisation', default='weekly')
    ai_forecast_interval = fields.Selection(INTERVAL_SELECTION, string='AI Demand Forecast', default='weekly')
    ai_review_interval = fields.Selection(INTERVAL_SELECTION, string='AI Review Analysis', default='weekly')
    ai_health_interval = fields.Selection(INTERVAL_SELECTION, string='Product Health Scores', default='daily')

    # Tracking last AI runs
    last_ai_pricing_run = fields.Datetime('Last AI Pricing Run')
    last_ai_listing_run = fields.Datetime('Last AI Listing Run')
    last_ai_forecast_run = fields.Datetime('Last AI Forecast Run')
    last_ai_review_run = fields.Datetime('Last AI Review Run')
    last_ai_health_run = fields.Datetime('Last AI Health Run')

    # Sync logs
    sync_log_count = fields.Integer(compute='_compute_sync_log_count', string='Sync Logs')

    def _compute_counts(self):
        for rec in self:
            rec.product_count = self.env['amazon.product'].search_count([('instance_id', '=', rec.id)])
            rec.order_count = self.env['amazon.sale.order'].search_count([('instance_id', '=', rec.id)])
            rec.settlement_count = self.env['amazon.settlement.report'].search_count([('instance_id', '=', rec.id)])

    def _compute_sync_log_count(self):
        for rec in self:
            rec.sync_log_count = self.env['amazon.sync.log'].search_count([('instance_id', '=', rec.id)])

    def action_view_sync_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sync Logs',
            'res_model': 'amazon.sync.log',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }

    def _log_start(self, operation, **kwargs):
        """Shortcut to create a sync log entry for this instance."""
        return self.env['amazon.sync.log'].log_start(self, operation, **kwargs)

    def _auto_fix_region(self):
        for rec in self:
            marketplace = (rec.marketplace_id or '').strip()
            if marketplace and marketplace in MARKETPLACE_REGION:
                correct_region = MARKETPLACE_REGION[marketplace]
                if rec.region != correct_region:
                    _logger.info("Auto-correcting region: %s → %s", rec.region, correct_region)
                    rec.region = correct_region

    def _check_required_fields(self, extra=None):
        """Validate that required API fields are present."""
        self.ensure_one()
        secure_instance = self.sudo()
        required = {
            'refresh_token': 'Refresh Token',
            'client_id': 'Client ID',
            'client_secret': 'Client Secret',
            'seller_id': 'Seller ID',
            'marketplace_id': 'Marketplace ID',
        }
        if extra:
            required.update(extra)
        missing = [
            label for field_name, label in required.items()
            if not (secure_instance[field_name] or '').strip()
        ]
        if missing:
            raise UserError("Missing required fields: %s" % ", ".join(missing))

    def _get_access_token_or_raise(self):
        self.ensure_one()
        self._check_amazon_manager_access()
        api = AmazonAPI()
        try:
            access_token = api.get_access_token(self.sudo())
        except requests.exceptions.ConnectionError as exc:
            raise UserError("Unable to reach Amazon OAuth endpoint.") from exc
        except requests.exceptions.Timeout as exc:
            raise UserError("Amazon OAuth request timed out.") from exc
        except requests.exceptions.HTTPError as exc:
            raise UserError("Amazon authentication failed:\n\n%s" % AmazonAPI.format_exception(exc)) from exc
        except requests.exceptions.RequestException as exc:
            raise UserError("Amazon authentication failed:\n\n%s" % AmazonAPI.format_exception(exc)) from exc
        except Exception as exc:
            raise UserError("Connection failed: %s" % exc) from exc
        if not access_token:
            raise UserError("Authentication failed: no access token returned.")
        return access_token

    def _api_call_safe(self, func, *args, error_msg="Amazon API error", **kwargs):
        """Wrap an API call with standard error handling."""
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as exc:
            diagnostic = getattr(exc, 'amazon_diagnostic', None) or AmazonAPI.format_exception(exc)
            _logger.warning("%s:\n%s", error_msg, diagnostic)
            raise UserError("%s:\n\n%s" % (error_msg, diagnostic)) from exc
        except requests.exceptions.RequestException as exc:
            diagnostic = AmazonAPI.format_exception(exc)
            _logger.warning("%s:\n%s", error_msg, diagnostic)
            raise UserError("%s:\n\n%s" % (error_msg, diagnostic)) from exc

    def _notify(self, title, message, msg_type='success', sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": msg_type, "sticky": sticky},
        }

    def _get_currency(self):
        self.ensure_one()
        return self.default_currency_id or self.company_id.currency_id or self.env.company.currency_id

    def _get_currency_code(self):
        self.ensure_one()
        currency = self._get_currency()
        return currency.name if currency else ''

    # ══════════════════════════════════════════════════
    # Test Connection
    # ══════════════════════════════════════════════════

    def action_test_connection(self):
        self.ensure_one()
        self._check_amazon_manager_access()
        self._auto_fix_region()
        secure_instance = self.sudo()
        sanitized = {}
        for f in ("seller_id", "marketplace_id", "refresh_token", "client_id", "client_secret", "aws_access_key", "aws_secret_key"):
            value = secure_instance[f]
            if isinstance(value, str):
                stripped = value.strip()
                if stripped != value:
                    sanitized[f] = stripped
        if sanitized:
            secure_instance.write(sanitized)

        self._check_required_fields()
        self._get_access_token_or_raise()
        return self._notify("Amazon Connection", "Connection successful.")

    def action_test_ai_connection(self):
        """Test AI provider connection with a simple prompt."""
        self.ensure_one()
        if not self.ai_api_key:
            raise UserError("AI API Key is not configured.")

        from ..services.ai_service import AmazonAIService

        provider = self.ai_provider or 'groq'
        api_key = self.ai_api_key
        ai_model = self.ai_model
        _logger.info("Testing AI connection — provider: %s, model: %s", provider, ai_model or 'default')

        try:
            response = AmazonAIService._call_provider(
                provider, api_key, ai_model,
                prompt='Reply with exactly: {"status":"ok","provider":"%s"}' % provider,
                temperature=0, max_tokens=50,
            )
            self.ai_connection_status = 'Connected — %s (%s)' % (provider, ai_model or 'default')
            return self._notify(
                "AI Connection Successful",
                "Provider: %s\nModel: %s\nResponse: %s" % (provider, ai_model or 'default', response[:100]),
                msg_type='success',
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else '?'
            body = exc.response.text[:300] if exc.response is not None else str(exc)
            self.ai_connection_status = 'FAILED — HTTP %s' % status
            raise UserError(
                "AI Connection Failed (HTTP %s):\n\n%s\n\n"
                "Check your API key and model name." % (status, body)
            ) from exc
        except Exception as exc:
            self.ai_connection_status = 'FAILED — %s' % str(exc)[:100]
            raise UserError("AI Connection Failed: %s" % exc) from exc

    # ══════════════════════════════════════════════════
    # Product Sync
    # ══════════════════════════════════════════════════

    @staticmethod
    def _clean_report_value(value):
        if value in (None, False):
            return ''
        if isinstance(value, (list, tuple)):
            value = ' '.join(str(v) for v in value if v not in (None, False))
        return str(value).replace('\r', ' ').replace('\n', ' ').strip()

    def _get_report_value(self, row, *keys):
        for key in keys:
            if key in row and row.get(key) not in (None, False, ''):
                return self._clean_report_value(row.get(key))
        return ''

    def _float_from_report(self, value, default=0.0):
        cleaned = self._clean_report_value(value)
        if not cleaned:
            return default
        try:
            return float(cleaned.replace(',', ''))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _amazon_fulfillment_to_channel(value):
        fulfillment = (value or '').strip().upper()
        return 'AFN' if fulfillment in (
            'AMAZON_NA', 'AMAZON_EU', 'AMAZON_FE', 'AMAZON_IN', 'AMAZON', 'AFN',
        ) else 'MFN'

    @staticmethod
    def _amazon_status_from_report(value):
        status_raw = (value or '').strip().lower()
        if status_raw in ('inactive', 'closed'):
            return 'Inactive'
        if status_raw == 'incomplete':
            return 'Incomplete'
        return 'Active'

    def action_sync_products(self):
        """Import products from Amazon using Merchant Listings Report."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        log = self._log_start('product_import')
        try:
            rows = self._api_call_safe(
                api.fetch_merchant_listings_report, self, access_token,
                error_msg="Failed to fetch products from Amazon"
            )
        except Exception as exc:
            log.log_fail(str(exc))
            raise

        created = updated = mapped = odoo_created = skipped = 0
        debug_rows = []
        Product = self.env['product.product'].with_context(active_test=False)
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                skipped += 1
                debug_rows.append({'row': index, 'reason': 'malformed-row'})
                continue
            if row.get('_extra_fields') and len(debug_rows) < 20:
                debug_rows.append({
                    'row': index,
                    'reason': 'extra-fields-in-report-row',
                    'extra_fields': row.get('_extra_fields'),
                })

            sku = self._get_report_value(row, 'seller-sku', 'sku', 'SKU')
            if not sku:
                skipped += 1
                debug_rows.append({'row': index, 'reason': 'missing-sku'})
                continue

            asin = self._get_report_value(row, 'asin1', 'asin')
            product_name = self._get_report_value(row, 'item-name', 'name', 'product-name') or sku
            price = self._float_from_report(self._get_report_value(row, 'price', 'item-price'))
            qty = self._float_from_report(self._get_report_value(row, 'quantity', 'qty'))
            description = self._get_report_value(row, 'item-description', 'description')
            image_url = self._get_report_value(row, 'image-url', 'image_url')
            fulfillment = self._get_report_value(row, 'fulfillment-channel', 'fulfillment_channel')
            status_raw = self._get_report_value(row, 'status', 'item-is-marketplace')

            # Merchant Listings reports use DEFAULT for merchant-fulfilled listings.
            fc = self._amazon_fulfillment_to_channel(fulfillment)
            status = self._amazon_status_from_report(status_raw)

            existing = self.env['amazon.product'].search([('sku', '=', sku), ('instance_id', '=', self.id)], limit=1)
            vals = {
                'name': product_name, 'asin': asin, 'sku': sku,
                'amazon_price': price, 'amazon_qty': qty,
                'description': description, 'image_url': image_url,
                'fulfillment_channel': fc, 'status': status,
                'instance_id': self.id, 'last_sync_date': fields.Datetime.now(),
            }
            if existing:
                existing.write(vals)
                updated += 1
                row_action = 'updated'
            else:
                existing = self.env['amazon.product'].create(vals)
                created += 1
                row_action = 'created'

            if not existing.odoo_product_id:
                odoo_products = Product.search([('default_code', '=', sku)])
                if len(odoo_products) == 1:
                    existing.odoo_product_id = odoo_products.id
                    mapped += 1
                    odoo_action = 'linked-existing'
                elif len(odoo_products) > 1:
                    skipped += 1
                    odoo_action = 'skipped-duplicate-default-code'
                    debug_rows.append({
                        'row': index,
                        'sku': sku,
                        'reason': 'multiple-odoo-products-with-same-internal-reference',
                    })
                else:
                    try:
                        with self.env.cr.savepoint():
                            odoo_product = existing._create_odoo_product_from_amazon()
                            existing.odoo_product_id = odoo_product.id
                        odoo_created += 1
                        odoo_action = 'created-missing'
                    except Exception as exc:
                        skipped += 1
                        odoo_action = 'odoo-create-failed'
                        debug_rows.append({
                            'row': index,
                            'sku': sku,
                            'reason': 'odoo-product-create-failed',
                            'error': str(exc),
                        })
            else:
                odoo_action = 'already-linked'

            if len(debug_rows) < 20:
                debug_rows.append({
                    'row': index,
                    'sku': sku,
                    'action': row_action,
                    'mapped': bool(existing.odoo_product_id),
                    'odoo_action': odoo_action,
                    'fulfillment_channel': fc,
                })

        self.last_product_sync = fields.Datetime.now()
        total = created + updated
        summary = (
            "%d row(s) read; %d Amazon product(s) synced (%d created, %d updated), "
            "%d linked to existing Odoo products, %d Odoo product(s) created, %d skipped."
            % (len(rows), total, created, updated, mapped, odoo_created, skipped)
        )
        response_data = {
            'rows_read': len(rows),
            'created': created,
            'updated': updated,
            'mapped': mapped,
            'odoo_created': odoo_created,
            'skipped': skipped,
            'debug_rows': debug_rows[:20],
        }
        if skipped:
            log.log_partial(
                summary=summary,
                records_processed=len(rows),
                records_created=created,
                records_updated=updated,
                records_failed=skipped,
                error_message=str(debug_rows[:20]),
            )
            log.response_data = str(response_data)[:5000]
        else:
            log.log_success(
                summary=summary,
                records_processed=len(rows),
                records_created=created,
                records_updated=updated,
                response_data=response_data,
            )
        _logger.info("[Amazon Product Sync] %s", summary)
        return self._notify("Product Sync", summary, 'warning' if skipped else 'success', bool(skipped))

    def _export_product_to_amazon(self, amazon_product):
        """Export product using the product's full _build_amazon_attributes()."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()
        if not amazon_product.sku:
            raise UserError("Cannot export product without SKU.")
        if not amazon_product.product_type:
            raise UserError("Product Type is required for Amazon export.")

        # Use the product's own attribute builder (sends ALL fields)
        attrs = amazon_product._build_amazon_attributes()
        body = {
            "productType": amazon_product.product_type,
            "requirements": "LISTING",
            "attributes": attrs,
        }

        result = self._api_call_safe(api.put_listings_item, self, access_token, amazon_product.sku, body, error_msg="Failed to export product")
        amazon_product.last_sync_date = fields.Datetime.now()
        status = result.get('status', 'UNKNOWN')
        if result.get('errors'):
            details = []
            for error in result['errors'][:5]:
                code = error.get('code', '')
                message = error.get('message', '') or error.get('details', '')
                details.append("%s: %s" % (code, message) if code else message)
            raise UserError(
                "Amazon rejected listing export: %s"
                % ("; ".join(details) or "No error details returned.")
            )
        if status == 'INVALID':
            issues = []
            for issue in result.get('issues', [])[:5]:
                code = issue.get('code', '')
                message = issue.get('message', '')
                issues.append("%s: %s" % (code, message) if code else message)
            raise UserError(
                "Amazon rejected listing export: %s"
                % ("; ".join(issues) or "No issue details returned.")
            )
        if status == 'ACCEPTED' and result.get('asin'):
            amazon_product.asin = result['asin']
        return self._notify("Export to Amazon", "Product exported. Status: %s" % status,
                            'success' if status == 'ACCEPTED' else 'warning')

    def action_export_all_products(self):
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('odoo_product_id', '!=', False), ('sku', '!=', False),
        ])
        if not products:
            return self._notify("Export Products", "No mapped products with SKU found to export. Go to Catalog > Import / Map Products first.", 'warning')
        errors, exported = [], 0
        for prod in products:
            try:
                self._export_product_to_amazon(prod)
                exported += 1
            except UserError as exc:
                errors.append("%s: %s" % (prod.sku, exc.args[0] if exc.args else exc))
        msg = "%d product(s) exported." % exported
        if errors:
            msg += " Errors: " + "; ".join(errors[:5])
        return self._notify("Bulk Export", msg, 'warning' if errors else 'success', bool(errors))

    def action_open_product_setup_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Amazon Product Initial Setup',
            'res_model': 'amazon.product.setup.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_instance_id': self.id},
        }

    def action_link_products_by_sku(self):
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id),
            ('odoo_product_id', '=', False),
        ])
        if not products:
            return self._notify("Product Mapping", "No unmapped Amazon products found for this instance.", 'warning')
        return products._setup_odoo_products(create_missing=False)

    def action_create_missing_odoo_products(self):
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id),
            ('odoo_product_id', '=', False),
        ])
        if not products:
            return self._notify("Create Odoo Products", "No unmapped Amazon products found for this instance.", 'warning')
        return products._setup_odoo_products(create_missing=True)

    # ══════════════════════════════════════════════════
    # Price Update (Odoo → Amazon)
    # ══════════════════════════════════════════════════

    def action_update_prices(self):
        """Push prices from Odoo products to Amazon via Feeds API."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('odoo_product_id', '!=', False), ('sku', '!=', False),
        ])
        if not products:
            return self._notify("Push Prices", "No mapped products to update prices for. Go to Catalog > Import / Map Products first.", 'warning')

        currency = self._get_currency_code()
        items = [
            {
                'sku': p.sku,
                'price': p.odoo_product_id.list_price,
                'currency': currency,
                'product_type': p.product_type or 'PRODUCT',
            }
            for p in products if p.odoo_product_id.list_price
        ]
        if not items:
            return self._notify("Push Prices", "Mapped products have no prices set in Odoo.", 'warning')

        content = api.build_price_json_feed(self, items)
        result = self._api_call_safe(
            api.submit_feed, self, access_token, FEED_JSON_LISTINGS, content,
            content_type='application/json; charset=UTF-8',
            error_msg="Price update feed failed",
        )
        feed_id = result.get('feedId', 'N/A')
        return self._notify("Price Update", "Price feed submitted (Feed ID: %s). %d product(s)." % (feed_id, len(items)))

    # ══════════════════════════════════════════════════
    # Stock Sync (Odoo → Amazon)
    # ══════════════════════════════════════════════════

    def _get_stock_qty_for_amazon_export(self, product):
        self.ensure_one()
        if self.fbm_warehouse_id and self.fbm_warehouse_id.lot_stock_id:
            product = product.with_context(
                warehouse=self.fbm_warehouse_id.id,
                location=self.fbm_warehouse_id.lot_stock_id.id,
            )
        return max(0, int(product.qty_available))

    def action_export_stock(self):
        """Push stock levels from Odoo to Amazon via Feeds API."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('odoo_product_id', '!=', False),
            ('sku', '!=', False), ('fulfillment_channel', '=', 'MFN'),
        ])
        afn_mapped = self.env['amazon.product'].search_count([
            ('instance_id', '=', self.id), ('odoo_product_id', '!=', False),
            ('sku', '!=', False), ('fulfillment_channel', '=', 'AFN'),
        ])
        if not products:
            if afn_mapped:
                return self._notify(
                    "Export Stock",
                    "All %d mapped product(s) are set to Fulfilled by Amazon (FBA/AFN). "
                    "Export Stock only applies to Merchant Fulfilled (MFN) products. "
                    "No MFN inventory feed was sent." % afn_mapped,
                    'warning',
                )
            return self._notify("Export Stock", "No mapped MFN products found. Map your Odoo products first via Catalog > Import / Map Products.", 'warning')

        items = []
        for p in products:
            qty = self._get_stock_qty_for_amazon_export(p.odoo_product_id)
            items.append({
                'sku': p.sku,
                'quantity': qty,
                'product_type': p.product_type or 'PRODUCT',
            })

        content = api.build_inventory_json_feed(self, items)
        result = self._api_call_safe(
            api.submit_feed, self, access_token, FEED_JSON_LISTINGS, content,
            content_type='application/json; charset=UTF-8',
            error_msg="Stock export feed failed",
        )
        feed_id = result.get('feedId', 'N/A')
        self.last_stock_sync = fields.Datetime.now()
        msg = "Inventory feed submitted (Feed ID: %s). %d MFN product(s)." % (feed_id, len(items))
        if afn_mapped:
            msg += " Skipped %d FBA/AFN product(s)." % afn_mapped
        return self._notify("Stock Export", msg, 'warning' if afn_mapped else 'success', bool(afn_mapped))

    # ══════════════════════════════════════════════════
    # Stock Pull (Amazon → Odoo)
    # ══════════════════════════════════════════════════

    def action_pull_stock(self):
        """Compatibility alias for the supported FBA Inventory API audit."""
        return self.action_run_inventory_audit()

    # ══════════════════════════════════════════════════
    # Price Pull (Amazon → Odoo)
    # ══════════════════════════════════════════════════

    def action_pull_prices(self):
        """Manual Amazon → Odoo price reconciliation."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('sku', '!=', False),
        ])
        updated = 0
        for prod in products:
            try:
                data = api.get_listings_item(self, access_token, prod.sku)
                # Extract price from offers or attributes
                a = data.get('attributes', {})
                price_list = a.get('purchasable_offer', [])
                if price_list:
                    try:
                        price = float(price_list[0]['our_price'][0]['schedule'][0]['value_with_tax'])
                        prod.amazon_price = price
                        # Sync to Odoo product if mapped
                        if prod.odoo_product_id:
                            prod.odoo_product_id.list_price = price
                        updated += 1
                    except (KeyError, IndexError, TypeError):
                        pass
                # Update quantity from fulfillment
                fa = data.get('fulfillmentAvailability', [])
                if fa:
                    prod.amazon_qty = fa[0].get('quantity', prod.amazon_qty)
            except Exception as exc:
                _logger.warning("Failed to pull price for %s: %s", prod.sku, exc)

        return self._notify(
            "Price Pull",
            "%d product price(s) pulled from Amazon. This is Amazon → Odoo reconciliation; "
            "do not use it when Odoo is price master unless you intentionally want to reconcile." % updated,
            'warning',
            True,
        )

    # ══════════════════════════════════════════════════
    # Full Bidirectional Sync (All at once)
    # ══════════════════════════════════════════════════

    def action_full_sync(self):
        """Run full bidirectional sync: pull products, pull orders, push stock, push prices."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        existing_unmapped = self.env['amazon.product'].search_count([
            ('instance_id', '=', self.id),
            ('sku', '!=', False),
            ('odoo_product_id', '=', False),
        ])
        if existing_unmapped:
            raise UserError(
                "Full Sync blocked: %d Amazon product(s) are not linked to Odoo products. "
                "Skipped order import, stock export, price update, and cancellation checks. "
                "Run Link Existing Products by SKU or Create Missing Odoo Products first."
                % existing_unmapped
            )

        log = self._log_start('full_sync')
        results = []

        # 1. Pull products from Amazon
        product_sync_failed = False
        try:
            self.action_sync_products()
            results.append("Products synced")
        except Exception as exc:
            product_sync_failed = True
            results.append("Product sync failed: %s" % exc)
        if product_sync_failed:
            results.extend([
                "Order import skipped: product sync failed",
                "Stock export skipped: product sync failed",
                "Price update skipped: product sync failed",
                "Cancellation check skipped: product sync failed",
            ])
            summary = " | ".join(results)
            log.log_partial(summary=summary, records_failed=1)
            return self._notify("Full Sync Protected", summary, 'warning', True)

        unmapped_after_product_sync = self.env['amazon.product'].search_count([
            ('instance_id', '=', self.id),
            ('sku', '!=', False),
            ('odoo_product_id', '=', False),
        ])
        if unmapped_after_product_sync:
            results.extend([
                "Order import skipped: %d unmapped product(s)" % unmapped_after_product_sync,
                "Stock export skipped: %d unmapped product(s)" % unmapped_after_product_sync,
                "Price update skipped: %d unmapped product(s)" % unmapped_after_product_sync,
                "Cancellation check skipped: %d unmapped product(s)" % unmapped_after_product_sync,
            ])
            summary = " | ".join(results)
            log.log_partial(summary=summary, records_failed=unmapped_after_product_sync)
            return self._notify("Full Sync Protected", summary, 'warning', True)

        # 2. Pull orders
        try:
            self.action_import_orders()
            results.append("Order import job queued")
        except Exception as exc:
            results.append("Order import failed: %s" % exc)

        # 3. Push stock to Amazon
        try:
            self.action_export_stock()
            results.append("Stock exported")
        except Exception as exc:
            results.append("Stock export failed: %s" % exc)

        # 4. Push prices to Amazon
        try:
            self.action_update_prices()
            results.append("Prices updated")
        except Exception as exc:
            results.append("Price update failed: %s" % exc)

        # 5. Check canceled orders
        try:
            self.action_check_canceled_orders()
            results.append("Cancellations checked")
        except Exception as exc:
            results.append("Cancel check failed: %s" % exc)

        failed = sum(1 for r in results if 'failed' in r.lower())
        if failed:
            log.log_partial(summary=" | ".join(results), records_failed=failed)
        else:
            log.log_success(summary=" | ".join(results))
        return self._notify("Full Sync Complete", " | ".join(results))

    # ══════════════════════════════════════════════════
    # Order Import (Amazon → Odoo)
    # ══════════════════════════════════════════════════

    def _get_order_import_window(self):
        """Return a bounded order import window for a new async job."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.initial_order_import_from:
            date_from = self.initial_order_import_from
        elif self.last_order_sync:
            overlap = self.order_import_overlap_minutes if self.order_import_overlap_minutes is not None else 10
            date_from = self.last_order_sync - timedelta(minutes=overlap)
        else:
            date_from = now - timedelta(days=30)

        date_to = self.initial_order_import_to or now
        if date_from and date_to and date_from >= date_to:
            raise UserError("Initial Order Import From must be earlier than Initial Order Import To.")
        return date_from, date_to

    def _queue_order_import_job(self, fulfillment_channel=False):
        """Create or reuse a resumable background order import job."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()

        active_job = self.env['amazon.order.import.job'].search([
            ('instance_id', '=', self.id),
            ('state', 'in', ('draft', 'running')),
        ], limit=1)
        if active_job:
            return active_job, False, False

        date_from, date_to = self._get_order_import_window()
        batch_size = self.order_import_batch_size or 10
        job_model = self.env['amazon.order.import.job']
        effective_date_to = job_model._get_amazon_safe_before_dt(date_to)
        amazon_request_before = job_model._get_amazon_safe_before(date_to)

        unmapped_count = self.env['amazon.product'].search_count([
            ('instance_id', '=', self.id),
            ('sku', '!=', False),
            ('odoo_product_id', '=', False),
        ])
        mapping_warning = bool(unmapped_count)

        job = self.env['amazon.order.import.job'].create({
            'instance_id': self.id,
            'date_from': date_from,
            'date_to': date_to,
            'effective_date_to': effective_date_to,
            'amazon_request_before': amazon_request_before,
            'upper_bound_adjusted': bool(date_to and date_to > effective_date_to),
            'batch_size': batch_size,
            'fulfillment_channel': fulfillment_channel or False,
            'error_message': (
                "%d existing Amazon product(s) are not linked to Odoo products. "
                "Affected Sale Orders will be skipped until products are mapped."
            ) % unmapped_count if unmapped_count else False,
        })
        log = self._log_start(
            'order_import',
            request_data={
                'job_id': job.id,
                'date_from': date_from,
                'date_to': date_to,
                'effective_date_to': effective_date_to,
                'amazon_request_before': amazon_request_before,
                'batch_size': batch_size,
                'fulfillment_channel': fulfillment_channel or 'all',
            },
            res_model='amazon.order.import.job',
            res_id=job.id,
        )
        job.sync_log_id = log.id

        cron = self.env.ref('sdlc_amazon_connector.cron_amazon_process_order_import_jobs', raise_if_not_found=False)
        if cron and not cron.active:
            cron.active = True
        return job, True, mapping_warning

    def action_import_orders(self):
        """Start a resumable background import job for FBM + FBA orders."""
        job, created, mapping_warning = self._queue_order_import_job()
        if not created:
            return self._notify(
                "Order import already running",
                "Job #%s is already queued/running for this instance." % job.id,
                'warning',
                True,
            )
        message = "Order import started. Job #%s will process up to %d order(s) per cron run." % (
            job.id, job.batch_size,
        )
        if mapping_warning:
            message += " Some existing Amazon products are not mapped; affected Sale Orders will be skipped and logged."
        return self._notify("Order Import", message, 'warning' if mapping_warning else 'success', mapping_warning)

    def action_import_fbm_orders(self):
        """Start a resumable background import job for FBM orders."""
        job, created, mapping_warning = self._queue_order_import_job('MFN')
        if not created:
            return self._notify(
                "FBM order import already running",
                "Job #%s is already queued/running for this instance." % job.id,
                'warning',
                True,
            )
        message = "FBM order import started. Job #%s will process up to %d order(s) per cron run." % (
            job.id, job.batch_size,
        )
        if mapping_warning:
            message += " Some existing Amazon products are not mapped; affected Sale Orders will be skipped and logged."
        return self._notify("FBM Order Import", message, 'warning' if mapping_warning else 'success', mapping_warning)

    def action_import_fba_orders(self):
        """Start a resumable background import job for FBA orders."""
        job, created, mapping_warning = self._queue_order_import_job('AFN')
        if not created:
            return self._notify(
                "FBA order import already running",
                "Job #%s is already queued/running for this instance." % job.id,
                'warning',
                True,
            )
        message = "FBA order import started. Job #%s will process up to %d order(s) per cron run." % (
            job.id, job.batch_size,
        )
        if mapping_warning:
            message += " Some existing Amazon products are not mapped; affected Sale Orders will be skipped and logged."
        return self._notify("FBA Order Import", message, 'warning' if mapping_warning else 'success', mapping_warning)

    def _process_order_import(self, data, access_token, api, label):
        """Deprecated compatibility wrapper; order imports are asynchronous now."""
        _logger.warning("Deprecated synchronous order import helper called; queuing %s import job instead.", label)
        if label == 'FBM':
            return self.action_import_fbm_orders()
        if label == 'FBA':
            return self.action_import_fba_orders()
        return self.action_import_orders()

    # ══════════════════════════════════════════════════
    # Order Status & Cancellation Check
    # ══════════════════════════════════════════════════

    def _queue_order_status_sync_job(self, fulfillment_channel=False):
        """Create or reuse a resumable Amazon → Odoo order status sync job."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        job_model = self.env['amazon.order.status.sync.job']
        active_job = job_model.search([
            ('instance_id', '=', self.id),
            ('state', 'in', ('draft', 'pending', 'running')),
            ('fulfillment_channel', '=', fulfillment_channel or False),
        ], limit=1)
        if active_job:
            return active_job, False

        job = job_model._create_for_instance(self, fulfillment_channel=fulfillment_channel or False)
        cron = self.env.ref('sdlc_amazon_connector.cron_amazon_sync_order_statuses', raise_if_not_found=False)
        if cron and not cron.active:
            cron.active = True
        return job, True

    def action_sync_order_statuses(self):
        """Queue a background Amazon → Odoo order status sync job."""
        job, created = self._queue_order_status_sync_job()
        if not created:
            return self._notify(
                "Order status sync already running",
                "Job #%s is already queued/running for this instance." % job.id,
                'warning',
                True,
            )
        return self._notify(
            "Order Status Sync",
            "Order status sync started. Job #%s will process up to %d order(s) per cron run." % (
                job.id, job.batch_size,
            ),
            'success',
        )

    def action_check_canceled_orders(self):
        """Compatibility action: queue the safe status-sync job instead of canceling inline."""
        return self.action_sync_order_statuses()

    def action_update_fbm_order_status(self):
        """Queue a safe FBM-only Amazon → Odoo status sync job."""
        self.ensure_one()
        job, created = self._queue_order_status_sync_job('MFN')
        if not created:
            return self._notify(
                "FBM status sync already running",
                "Job #%s is already queued/running for this instance." % job.id,
                'warning',
                True,
            )
        return self._notify(
            "FBM Status Sync",
            "FBM status sync started. Job #%s will process up to %d order(s) per cron run." % (
                job.id, job.batch_size,
            ),
            'success',
        )

    # ══════════════════════════════════════════════════
    # Shipment Confirmation (Odoo → Amazon)
    # ══════════════════════════════════════════════════

    def _confirm_order_shipment(self, amazon_order):
        """Send shipment confirmation feed to Amazon."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        items = [{
            'order_id': amazon_order.amazon_order_ref,
            'carrier': amazon_order.carrier_name or 'Other',
            'tracking': amazon_order.tracking_number or '',
            'ship_date': (amazon_order.ship_date or fields.Datetime.now()).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }]
        # Add individual items if available
        for line in amazon_order.order_line_ids:
            if line.amazon_order_item_id:
                items[0]['order_item_id'] = line.amazon_order_item_id
                items[0]['quantity'] = int(line.quantity)

        xml = api.build_order_fulfillment_feed_xml(items)
        self._api_call_safe(api.submit_feed, self, access_token, FEED_ORDER_FULFILLMENT, xml,
                            error_msg="Failed to submit shipment confirmation")
        amazon_order.order_status = 'Shipped'
        amazon_order.amazon_status = 'Shipped'
        amazon_order.status_last_synced_at = fields.Datetime.now()
        amazon_order.ship_date = fields.Datetime.now()

    def _cancel_amazon_order(self, amazon_order):
        """Mark local Amazon order as canceled using safe configured Odoo workflow rules."""
        amazon_order._sync_amazon_status_from_payload(
            {
                'AmazonOrderId': amazon_order.amazon_order_ref,
                'OrderStatus': 'Canceled',
                'LastUpdateDate': fields.Datetime.now(),
                'FulfillmentChannel': amazon_order.fulfillment_channel,
            },
            source='manual_local_cancel',
            create_chatter=True,
            apply_workflow=True,
        )

    # ══════════════════════════════════════════════════
    # Invoice Upload (Odoo → Amazon)
    # ══════════════════════════════════════════════════

    def _upload_invoice_to_amazon(self, amazon_order):
        """Upload invoice for VCS program."""
        self.ensure_one()
        if not amazon_order.invoice_id:
            raise UserError("No invoice linked to this order.")
        self._upload_invoice_to_amazon_by_move(amazon_order.invoice_id)

    def _upload_invoice_to_amazon_by_move(self, invoice):
        """Upload an account.move invoice PDF to Amazon via Feeds API."""
        self.ensure_one()
        import base64

        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        # Generate PDF from Odoo report engine
        report = self.env.ref('account.account_invoices')
        pdf_content, _content_type = self.env['ir.actions.report']._render_qweb_pdf(
            report, res_ids=[invoice.id],
        )

        if not pdf_content:
            raise UserError("Failed to generate invoice PDF for %s" % invoice.name)

        _logger.info("Generated PDF for invoice %s (%d bytes)", invoice.name, len(pdf_content))

        # Upload via Feeds API
        try:
            # Create feed document with PDF content type
            doc = api.create_feed_document(self, access_token, content_type='application/pdf')
            doc_id = doc.get('feedDocumentId')
            upload_url = doc.get('url')

            if not doc_id or not upload_url:
                raise UserError("Amazon did not return a feed document upload URL.")

            # Upload the PDF to Amazon's pre-signed S3 URL.
            api.upload_feed_document(upload_url, pdf_content, content_type='application/pdf', instance=self)

            # Submit the feed referencing the document.
            feed_result = api.create_feed(self, access_token, FEED_INVOICE_UPLOAD, doc_id)
            feed_id = feed_result.get('feedId', '')

            _logger.info("Invoice %s uploaded to Amazon — feed ID: %s", invoice.name, feed_id)

            invoice.amazon_invoice_uploaded = True
            invoice.amazon_invoice_number = feed_id

        except Exception as exc:
            _logger.error("Invoice upload failed for %s: %s", invoice.name, exc)
            raise UserError("Invoice upload to Amazon failed: %s" % exc) from exc

    # ══════════════════════════════════════════════════
    # Settlement Reports
    # ══════════════════════════════════════════════════

    def action_import_settlement_reports(self):
        """Queue discovery/import of Amazon-generated V2 settlement reports."""
        self.ensure_one()
        self._check_required_fields()
        job = self.env['amazon.phase7.job'].enqueue(self, 'settlements')
        return self._notify(
            _("Settlement Reports"),
            _("V2 settlement discovery and import was queued as job %s.", job.name),
        )

    def _download_settlement_report(self, report_rec):
        """Compatibility entry point: settlement downloads are always asynchronous."""
        self.ensure_one()
        if report_rec.instance_id != self:
            raise UserError(_("The settlement belongs to another Amazon instance."))
        return report_rec.action_download_report()

    # ══════════════════════════════════════════════════
    # Return Reports
    # ══════════════════════════════════════════════════

    def _download_return_report(self, report_rec):
        """Compatibility entry point: always queue the durable Reports API job."""
        self.ensure_one()
        if report_rec.instance_id != self:
            raise UserError(_("The return import belongs to another Amazon instance."))
        return report_rec.action_download_report()

    # ══════════════════════════════════════════════════
    # FBA Inventory Reports
    # ══════════════════════════════════════════════════

    def _download_fba_inventory_report(self, report_rec):
        self.ensure_one()
        if report_rec.report_type in ('live_stock', 'adjustment'):
            raise UserError(
                "Legacy FBA inventory reports are not used for reconciliation. "
                "Run an Inventory Audit, which uses the supported FBA Inventory API v1."
            )
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        if report_rec.report_type == 'fba_shipment':
            rows = self._api_call_safe(api.fetch_fba_shipment_report, self, access_token, error_msg="Failed to fetch FBA shipment report")
        else:
            raise UserError("Unknown report type: %s" % report_rec.report_type)

        report_rec.line_ids.unlink()
        for row in rows:
            vals = {
                'report_id': report_rec.id,
                'sku': row.get('sku') or row.get('seller-sku', ''),
                'fnsku': row.get('fnsku', ''),
                'asin': row.get('asin', ''),
                'product_name': row.get('product-name') or row.get('item-name', ''),
                'condition': row.get('condition', ''),
                'quantity': float(row.get('quantity') or row.get('afn-fulfillable-quantity') or 0),
                'fulfillment_center_id': row.get('fulfillment-center-id', ''),
                'amazon_order_id': row.get('amazon-order-id', ''),
            }
            # Try to auto-map Odoo product
            if vals['sku']:
                amz_prod = self.env['amazon.product'].search([
                    ('sku', '=', vals['sku']), ('instance_id', '=', self.id)
                ], limit=1)
                if amz_prod and amz_prod.odoo_product_id:
                    vals['odoo_product_id'] = amz_prod.odoo_product_id.id
            self.env['amazon.fba.inventory.report.line'].create(vals)

        report_rec.state = 'downloaded'

    # ══════════════════════════════════════════════════
    # Rating Report
    # ══════════════════════════════════════════════════

    def _download_rating_report(self, report_rec):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        rows = self._api_call_safe(
            api.fetch_seller_feedback_report, self, access_token,
            error_msg="Failed to fetch seller feedback report"
        )
        report_rec.line_ids.unlink()
        for row in rows:
            self.env['amazon.rating.report.line'].create({
                'report_id': report_rec.id,
                'amazon_order_id': row.get('order-id', ''),
                'rating': int(row.get('rating', 0) or 0),
                'feedback': row.get('comments', ''),
                'date': row.get('date', ''),
                'rater_email': row.get('rater-email', ''),
            })
        report_rec.state = 'downloaded'

    # ══════════════════════════════════════════════════
    # VCS Tax Report
    # ══════════════════════════════════════════════════

    def _download_vcs_report(self, report_rec):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        rows = self._api_call_safe(
            api.fetch_vcs_tax_report, self, access_token,
            error_msg="Failed to fetch VCS tax report"
        )
        report_rec.line_ids.unlink()
        for row in rows:
            self.env['amazon.vcs.tax.report.line'].create({
                'report_id': report_rec.id,
                'amazon_order_id': row.get('order-id') or row.get('amazon-order-id', ''),
                'amazon_invoice_number': row.get('invoice-number') or row.get('vat-invoice-number', ''),
                'vat_number': row.get('vat-number', ''),
                'vat_amount': float(row.get('tax-amount') or row.get('vat-amount') or 0),
                'invoice_amount': float(row.get('invoice-amount') or row.get('total-amount') or 0),
                'currency_code': row.get('currency', ''),
            })
        report_rec.state = 'downloaded'

    # ══════════════════════════════════════════════════
    # Removal Orders
    # ══════════════════════════════════════════════════

    def _submit_removal_order(self, removal_order):
        """Backward-compatible entry point: queue the real feed workflow."""
        removal_order.ensure_one()
        removal_order._validate_submission()
        job = self.env['amazon.phase7.job'].enqueue(
            self, 'removal_submit', source=removal_order,
        )
        removal_order.state = 'queued'
        return job

    def _check_removal_order_status(self, removal_order):
        """Backward-compatible entry point: queue both official detail reports."""
        self.ensure_one()
        return self.env['amazon.phase7.job'].enqueue(
            self, 'removal_status', source=removal_order,
        )

    def action_import_removal_orders(self):
        """Queue authoritative removal-order and shipment-detail reports."""
        self.ensure_one()
        self._check_required_fields()
        date_from, date_to = self._phase7_window('last_fba_removal_sync_at')
        job = self.env['amazon.phase7.job'].enqueue(
            self, 'removal_status', date_from=date_from, date_to=date_to,
        )
        return self._notify(_("Removal Orders"), _("Import job %s was queued.", job.display_name))

    # ══════════════════════════════════════════════════
    # Inbound Shipments
    # ══════════════════════════════════════════════════

    def _create_inbound_shipment_plan(self, shipment, payload=None):
        """Start createInboundPlan and persist both asynchronous identifiers."""
        self.ensure_one()
        shipment.ensure_one()
        shipment._check_inbound_manager_access()
        if shipment.instance_id != self:
            raise UserError(_("The inbound shipment belongs to another Amazon instance."))
        payload = payload or shipment._prepare_create_inbound_plan_payload()

        try:
            access_token = self._get_access_token_or_raise()
            result = self._api_call_safe(
                AmazonAPI().create_inbound_plan,
                self,
                access_token,
                payload,
                error_msg=_("Failed to create inbound plan"),
            )
        except UserError as exc:
            # A notification is returned instead of raising after the external call so
            # the diagnostic remains durable and cannot be lost to an RPC rollback.
            cause = exc.__cause__
            response = getattr(cause, 'response', None)
            status_code = response.status_code if response is not None else None
            error_code = 'CREATE_REQUEST_FAILED'
            retry_after_at = False
            request_id = AmazonAPI._amazon_request_id(response)
            if isinstance(cause, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                error_code = 'CREATE_OUTCOME_UNKNOWN'
            elif status_code and status_code >= 500:
                error_code = 'CREATE_OUTCOME_UNKNOWN'
            elif status_code == 429:
                error_code = 'CREATE_RATE_LIMITED'
                try:
                    retry_after_seconds = max(float(response.headers.get('Retry-After') or 0), 0)
                except (TypeError, ValueError):
                    retry_after_seconds = 0
                if retry_after_seconds:
                    retry_after_at = fields.Datetime.now() + timedelta(seconds=retry_after_seconds)
            shipment.sudo().write({
                'create_operation_status': 'failed',
                'create_operation_error_code': error_code,
                'create_operation_error_message': str(exc),
                'create_operation_request_id': request_id or False,
                'create_retry_after_at': retry_after_at,
                'state': 'failed',
            })
            return self._notify(
                _("Inbound Plan Creation"), str(exc), 'danger', sticky=True,
            )

        if not shipment._apply_create_inbound_plan_response(result):
            return self._notify(
                _("Inbound Plan Creation"),
                shipment.create_operation_error_message,
                'danger',
                sticky=True,
            )
        return self._notify(
            _("Inbound Plan Creation"),
            _(
                "Inbound plan creation started.\nAmazon Operation ID: %s",
                shipment.create_operation_id,
            ),
            'success',
        )

    def _submit_inbound_shipment(self, shipment):
        """Compatibility guard for the later packing/placement phase."""
        raise UserError(_("This action belongs to a later FBA workflow phase."))

    def _update_inbound_shipment_tracking(self, shipment):
        """Compatibility guard for the later transportation/tracking phase."""
        raise UserError(_("This action belongs to a later FBA workflow phase."))

    def _check_inbound_shipment_status(self, shipment):
        """Backward-compatible entry point for Phase 2 operation polling."""
        self.ensure_one()
        return shipment.action_check_create_operation_status()

    def _import_inbound_shipment(self, shipment):
        """Compatibility guard for the later inbound-plan import phase."""
        raise UserError(_("This action belongs to a later FBA workflow phase."))

    def _get_shipment_labels(self, shipment):
        """Compatibility guard for the later labels phase."""
        raise UserError(_("This action belongs to a later FBA workflow phase."))

    # ══════════════════════════════════════════════════
    # MCF Outbound Orders
    # ══════════════════════════════════════════════════

    def _submit_outbound_order(self, outbound):
        """Submit MCF fulfillment order to Amazon."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        items = []
        for line in outbound.line_ids:
            items.append({
                "sellerSku": line.sku,
                "sellerFulfillmentOrderItemId": "%s-%s" % (outbound.name, line.id),
                "quantity": int(line.quantity),
            })

        body = {
            "sellerFulfillmentOrderId": outbound.name,
            "displayableOrderId": outbound.displayable_order_id or outbound.name,
            "displayableOrderDate": fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
            "displayableOrderComment": outbound.displayable_comment or "Order from Odoo",
            "shippingSpeedCategory": outbound.shipping_speed or "Standard",
            "destinationAddress": {
                "name": outbound.dest_name or '',
                "addressLine1": outbound.dest_address_line1 or '',
                "addressLine2": outbound.dest_address_line2 or '',
                "city": outbound.dest_city or '',
                "stateOrRegion": outbound.dest_state or '',
                "postalCode": outbound.dest_postal_code or '',
                "countryCode": outbound.dest_country_code or '',
            },
            "items": items,
        }

        result = self._api_call_safe(
            api.create_fulfillment_order, self, access_token, body,
            error_msg="Failed to create MCF order"
        )
        outbound.fulfillment_order_id = outbound.name
        outbound.state = 'submitted'

    def _check_outbound_order_status(self, outbound):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()
        data = self._api_call_safe(
            api.get_fulfillment_order, self, access_token, outbound.fulfillment_order_id,
            error_msg="Failed to check MCF order status"
        )
        payload = data.get('payload', {}).get('fulfillmentOrder', {})
        status = payload.get('fulfillmentOrderStatus', '')
        status_map = {
            'RECEIVED': 'submitted', 'PLANNING': 'processing', 'PROCESSING': 'processing',
            'COMPLETE': 'shipped', 'COMPLETE_PARTIALLED': 'shipped',
            'UNFULFILLABLE': 'cancelled', 'CANCELLED': 'cancelled',
        }
        outbound.state = status_map.get(status, outbound.state)
        tracking = payload.get('fulfillmentShipment', {})
        if tracking:
            outbound.tracking_number = tracking.get('trackingNumber', outbound.tracking_number)

    def _cancel_outbound_order(self, outbound):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()
        self._api_call_safe(
            api.cancel_fulfillment_order, self, access_token, outbound.fulfillment_order_id,
            error_msg="Failed to cancel MCF order"
        )
        outbound.state = 'cancelled'

    # ══════════════════════════════════════════════════
    # Smart Buttons / Navigation
    # ══════════════════════════════════════════════════

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Amazon Products',
            'res_model': 'amazon.product', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }

    def action_view_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Amazon Orders',
            'res_model': 'amazon.sale.order', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }

    def action_view_settlements(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': 'Settlement Reports',
            'res_model': 'amazon.settlement.report', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id},
        }

    # ══════════════════════════════════════════════════
    # Auto Sync Scheduler
    # ══════════════════════════════════════════════════

    INTERVAL_MINUTES = {
        'disabled': 0, '15min': 15, '30min': 30, 'hourly': 60,
        '2hours': 120, '4hours': 240, '6hours': 360, '12hours': 720,
        'daily': 1440, 'weekly': 10080, 'monthly': 43200,
    }

    def _should_run(self, interval_field, last_sync_field):
        """Check if a sync job should run based on interval and last run time."""
        interval = getattr(self, interval_field, 'disabled')
        if interval == 'disabled' or not self.auto_sync_enabled:
            return False
        minutes = self.INTERVAL_MINUTES.get(interval, 0)
        if not minutes:
            return False
        last_run = getattr(self, last_sync_field, None)
        if not last_run:
            return True  # Never run before
        from datetime import timedelta
        return fields.Datetime.now() >= last_run + timedelta(minutes=minutes)

    def action_apply_sync_schedule(self):
        """Apply the auto-sync schedule — enables/disables the master cron job."""
        self.ensure_one()
        cron = self.env.ref('sdlc_amazon_connector.cron_amazon_master_scheduler', raise_if_not_found=False)
        if cron:
            # Enable master cron if any instance has auto_sync enabled
            any_enabled = self.env['amazon.instance'].search_count([('auto_sync_enabled', '=', True)])
            cron.active = any_enabled > 0
        return self._notify(
            "Sync Schedule",
            "Auto-sync %s for %s." % ("enabled" if self.auto_sync_enabled else "disabled", self.name),
        )

    @staticmethod
    def _get_all_active_instances(env):
        return env['amazon.instance'].search([('auto_sync_enabled', '=', True)])

    def cron_master_scheduler(self):
        """Master cron — runs every 15 min and dispatches individual sync jobs based on schedule."""
        instances = self.env['amazon.instance'].search([('auto_sync_enabled', '=', True)])
        for inst in instances:
            # Order sync
            if inst._should_run('order_sync_interval', 'last_order_sync'):
                try:
                    inst.action_import_orders()
                    _logger.info("[AutoSync] Order import job queued for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Order sync failed for %s: %s", inst.name, exc)

            # Product sync
            if inst._should_run('product_sync_interval', 'last_product_sync'):
                try:
                    inst.action_sync_products()
                    _logger.info("[AutoSync] Products synced for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Product sync failed for %s: %s", inst.name, exc)

            # Stock push (Odoo → Amazon)
            if inst._should_run('stock_push_interval', 'last_stock_sync'):
                try:
                    inst.action_export_stock()
                    _logger.info("[AutoSync] Stock pushed for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Stock push failed for %s: %s", inst.name, exc)

            # Stock pull (Amazon → Odoo)
            if inst._should_run('stock_pull_interval', 'last_stock_sync'):
                _logger.warning(
                    "[AutoSync] Stock pull skipped for %s. Amazon → Odoo stock reconciliation is manual only.",
                    inst.name,
                )

            # Price push
            if inst._should_run('price_push_interval', 'last_stock_sync'):
                try:
                    inst.action_update_prices()
                    _logger.info("[AutoSync] Prices pushed for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Price push failed for %s: %s", inst.name, exc)

            # Price pull
            if inst._should_run('price_pull_interval', 'last_stock_sync'):
                _logger.warning(
                    "[AutoSync] Price pull skipped for %s. Amazon → Odoo price reconciliation is manual only.",
                    inst.name,
                )

            # Settlement reports
            if inst._should_run('settlement_sync_interval', 'last_settlement_sync_at'):
                try:
                    inst.action_import_settlement_reports()
                    _logger.info("[AutoSync] Settlements synced for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Settlement sync failed for %s: %s", inst.name, exc)

            # Check canceled orders (runs with order sync)
            if inst._should_run('order_sync_interval', 'last_order_sync'):
                try:
                    inst.action_check_canceled_orders()
                except Exception as exc:
                    _logger.error("[AutoSync] Cancel check failed for %s: %s", inst.name, exc)

            # Smart Alert scan
            if inst._should_run('alert_scan_interval', 'last_ai_health_run'):
                try:
                    self.env['amazon.smart.alert'].run_alert_scan(inst.id)
                    _logger.info("[AutoSync] Alert scan done for %s", inst.name)
                except Exception as exc:
                    _logger.error("[AutoSync] Alert scan failed for %s: %s", inst.name, exc)

            # ── AI Features (only if AI key configured) ──
            if inst.ai_api_key:
                # AI Pricing
                if inst._should_run('ai_pricing_interval', 'last_ai_pricing_run'):
                    try:
                        inst._run_ai_pricing()
                        inst.last_ai_pricing_run = fields.Datetime.now()
                        _logger.info("[AutoSync] AI Pricing done for %s", inst.name)
                    except Exception as exc:
                        _logger.error("[AutoSync] AI Pricing failed for %s: %s", inst.name, exc)

                # AI Listing Optimisation
                if inst._should_run('ai_listing_interval', 'last_ai_listing_run'):
                    try:
                        inst._run_ai_listing()
                        inst.last_ai_listing_run = fields.Datetime.now()
                        _logger.info("[AutoSync] AI Listing done for %s", inst.name)
                    except Exception as exc:
                        _logger.error("[AutoSync] AI Listing failed for %s: %s", inst.name, exc)

                # AI Demand Forecast
                if inst._should_run('ai_forecast_interval', 'last_ai_forecast_run'):
                    try:
                        inst._run_ai_forecast()
                        inst.last_ai_forecast_run = fields.Datetime.now()
                        _logger.info("[AutoSync] AI Forecast done for %s", inst.name)
                    except Exception as exc:
                        _logger.error("[AutoSync] AI Forecast failed for %s: %s", inst.name, exc)

                # AI Review Analysis
                if inst._should_run('ai_review_interval', 'last_ai_review_run'):
                    try:
                        inst._run_ai_reviews()
                        inst.last_ai_review_run = fields.Datetime.now()
                        _logger.info("[AutoSync] AI Reviews done for %s", inst.name)
                    except Exception as exc:
                        _logger.error("[AutoSync] AI Reviews failed for %s: %s", inst.name, exc)

                # Product Health Scores
                if inst._should_run('ai_health_interval', 'last_ai_health_run'):
                    try:
                        self.env['amazon.product.health'].calculate_all_health_scores(inst.id)
                        inst.last_ai_health_run = fields.Datetime.now()
                        _logger.info("[AutoSync] Health scores done for %s", inst.name)
                    except Exception as exc:
                        _logger.error("[AutoSync] Health scores failed for %s: %s", inst.name, exc)

    # ── AI Auto-Run Methods ──

    def _run_ai_pricing(self):
        """Generate AI pricing suggestions for all active products."""
        self.ensure_one()
        from ..services.ai_service import AmazonAIService
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('status', '=', 'Active'), ('amazon_price', '>', 0),
        ])
        log = self._log_start('ai_price')
        created = 0
        for product in products[:50]:  # Limit to 50 per run to avoid timeout
            cost = product.odoo_product_id.standard_price if product.odoo_product_id else 0
            try:
                result = AmazonAIService.optimize_price(
                    self.ai_provider or 'groq', self.ai_api_key, self.ai_model,
                    product_name=product.name, category=product.product_type or '',
                    current_price=product.amazon_price, cost_price=cost,
                    currency=self._get_currency_code(),
                )
                self.env['amazon.ai.pricing'].create({
                    'product_id': product.id, 'current_price': product.amazon_price,
                    'cost_price': cost, 'suggested_price': result.get('suggested_price', 0),
                    'min_price': result.get('min_price', 0), 'max_price': result.get('max_price', 0),
                    'confidence_score': result.get('confidence', 0),
                    'reasoning': result.get('reasoning', ''),
                    'price_strategy': result.get('strategy', 'competitive'),
                })
                created += 1
            except Exception as exc:
                _logger.warning("AI pricing failed for %s: %s", product.sku, exc)
        log.log_success(summary="AI generated %d pricing suggestions" % created, records_created=created)

    def _run_ai_listing(self):
        """Generate AI listing optimisations for products without recent optimisation."""
        self.ensure_one()
        from ..services.ai_service import AmazonAIService
        # Find products not recently optimised
        already = self.env['amazon.ai.listing'].search([('instance_id', '=', self.id)]).mapped('product_id.id')
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('status', '=', 'Active'), ('id', 'not in', already),
        ], limit=20)
        log = self._log_start('ai_generate')
        created = 0
        for product in products:
            try:
                rec = self.env['amazon.ai.listing'].create({'product_id': product.id})
                rec.action_optimise_with_ai()
                created += 1
            except Exception as exc:
                _logger.warning("AI listing failed for %s: %s", product.sku, exc)
        log.log_success(summary="AI optimised %d listings" % created, records_created=created)

    def _run_ai_forecast(self):
        """Generate AI demand forecasts for active products."""
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('status', '=', 'Active'), ('odoo_product_id', '!=', False),
        ], limit=30)
        log = self._log_start('ai_inventory')
        created = 0
        for product in products:
            try:
                existing = self.env['amazon.demand.forecast'].search([('product_id', '=', product.id)], limit=1)
                if not existing:
                    existing = self.env['amazon.demand.forecast'].create({'product_id': product.id})
                existing.action_generate_forecast()
                created += 1
            except Exception as exc:
                _logger.warning("AI forecast failed for %s: %s", product.sku, exc)
        log.log_success(summary="AI generated %d demand forecasts" % created, records_created=created)

    def _run_ai_reviews(self):
        """Generate AI review analysis for active products."""
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.id), ('status', '=', 'Active'),
        ], limit=20)
        log = self._log_start('ai_generate')
        created = 0
        for product in products:
            try:
                existing = self.env['amazon.review.analysis'].search([('product_id', '=', product.id)], limit=1)
                if not existing:
                    existing = self.env['amazon.review.analysis'].create({'product_id': product.id})
                existing.action_analyse_reviews()
                created += 1
            except Exception as exc:
                _logger.warning("AI review failed for %s: %s", product.sku, exc)
        log.log_success(summary="AI analysed %d product reviews" % created, records_created=created)

    # ── Legacy cron methods (kept for backward compatibility) ──
    def cron_import_orders(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_import_orders()
                _logger.info("Cron queued order import job for %s", inst.display_name)
            except Exception as exc:
                _logger.error("Cron order import failed for %s: %s", inst.display_name, exc)

    def cron_import_fbm_orders(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_import_fbm_orders()
                _logger.info("Cron queued FBM order import job for %s", inst.display_name)
            except Exception as exc:
                _logger.error("Cron FBM order import failed for %s: %s", inst.display_name, exc)

    def cron_export_stock(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_export_stock()
            except Exception as exc:
                _logger.error("Cron stock export failed for %s: %s", inst.display_name, exc)

    def cron_update_prices(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_update_prices()
            except Exception as exc:
                _logger.error("Cron price update failed for %s: %s", inst.display_name, exc)

    def cron_sync_products(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_sync_products()
            except Exception as exc:
                _logger.error("Cron product sync failed for %s: %s", inst.display_name, exc)

    def cron_check_canceled_orders(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_check_canceled_orders()
            except Exception as exc:
                _logger.error("Cron cancel check failed for %s: %s", inst.display_name, exc)

    def cron_update_fbm_order_status(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_update_fbm_order_status()
            except Exception as exc:
                _logger.error("Cron FBM status update failed for %s: %s", inst.display_name, exc)

    def cron_import_settlement_reports(self):
        for inst in self.env['amazon.instance'].search([('active', '=', True)]):
            try:
                inst.action_import_settlement_reports()
            except Exception as exc:
                _logger.error("Cron settlement import failed for %s: %s", inst.display_name, exc)

    def cron_import_removal_orders(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_import_removal_orders()
            except Exception as exc:
                _logger.error("Cron removal import failed for %s: %s", inst.display_name, exc)

    def cron_pull_stock(self):
        """Compatibility scheduler: enqueue supported inventory audits."""
        return self.env['amazon.inventory.reconciliation.run'].cron_enqueue_inventory_audits()

    def cron_pull_prices(self):
        for inst in self.env['amazon.instance'].search([]):
            _logger.warning(
                "Cron price pull skipped for %s. Amazon → Odoo price reconciliation is manual only.",
                inst.display_name,
            )

    def cron_full_sync(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_full_sync()
            except Exception as exc:
                _logger.error("Cron full sync failed for %s: %s", inst.display_name, exc)
