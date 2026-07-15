
import logging
import requests
from datetime import datetime, timedelta, timezone

from odoo import models, fields
from odoo.exceptions import UserError

from .amazon_api import (
    AmazonAPI, FEED_JSON_LISTINGS, FEED_POST_PRODUCT_PRICING, FEED_POST_INVENTORY,
    FEED_ORDER_FULFILLMENT, FEED_INVOICE_UPLOAD,
)

_logger = logging.getLogger(__name__)

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
    fba_warehouse_id = fields.Many2one('stock.warehouse', string='FBA Warehouse')
    fbm_warehouse_id = fields.Many2one('stock.warehouse', string='FBM Warehouse')

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
        required = {
            'refresh_token': 'Refresh Token',
            'client_id': 'Client ID',
            'client_secret': 'Client Secret',
            'seller_id': 'Seller ID',
            'marketplace_id': 'Marketplace ID',
        }
        if extra:
            required.update(extra)
        missing = [label for f, label in required.items() if not (self[f] or '').strip()]
        if missing:
            raise UserError("Missing required fields: %s" % ", ".join(missing))

    def _get_access_token_or_raise(self):
        self.ensure_one()
        api = AmazonAPI()
        try:
            access_token = api.get_access_token(self)
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
        self._auto_fix_region()
        sanitized = {}
        for f in ("seller_id", "marketplace_id", "refresh_token", "client_id", "client_secret", "aws_access_key", "aws_secret_key"):
            value = self[f]
            if isinstance(value, str):
                stripped = value.strip()
                if stripped != value:
                    sanitized[f] = stripped
        if sanitized:
            self.write(sanitized)

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
        """Manual Amazon → Odoo stock reconciliation."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        try:
            rows = self._api_call_safe(
                api.fetch_fba_inventory_report, self, access_token,
                error_msg="Failed to fetch FBA inventory from Amazon"
            )
        except UserError as exc:
            msg = str(exc.args[0] if exc.args else exc)
            if 'FATAL' in msg:
                return self._notify("Pull Stock", "No FBA inventory found on Amazon. This account may have no FBA listings, or the SP-API app is missing the FBA Inventory role.", 'warning')
            if 'CANCELLED' in msg:
                return self._notify("Pull Stock", "No FBA inventory data available. Amazon has no active FBA inventory for this account.", 'warning')
            raise
        updated = 0
        for row in rows:
            sku = (row.get('sku') or row.get('seller-sku') or '').strip()
            if not sku:
                continue
            qty = float(row.get('afn-fulfillable-quantity') or row.get('quantity') or 0)

            amz_prod = self.env['amazon.product'].search([
                ('sku', '=', sku), ('instance_id', '=', self.id)
            ], limit=1)
            if amz_prod:
                amz_prod.amazon_qty = qty
                # Update Odoo stock quant if mapped and warehouse set
                if amz_prod.odoo_product_id and self.fba_warehouse_id:
                    location = self.fba_warehouse_id.lot_stock_id
                    self.env['stock.quant'].with_context(inventory_mode=True).sudo().create({
                        'product_id': amz_prod.odoo_product_id.id,
                        'location_id': location.id,
                        'inventory_quantity': qty,
                    })
                updated += 1

        self.last_stock_sync = fields.Datetime.now()
        return self._notify(
            "Stock Pull",
            "%d product stock level(s) pulled from Amazon. This is Amazon → Odoo reconciliation; "
            "do not use it when Odoo is stock master unless you intentionally want to reconcile." % updated,
            'warning',
            True,
        )

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
            results.append("Orders imported")
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

    def action_import_orders(self):
        """Import FBM + FBA orders from Amazon."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        log = self._log_start('order_import')

        created_after = self.last_order_sync or (datetime.now(timezone.utc) - timedelta(days=30))
        if isinstance(created_after, datetime):
            created_after = created_after.strftime('%Y-%m-%dT%H:%M:%SZ')

        imported = 0
        next_token = None
        while True:
            data = self._api_call_safe(
                api.get_orders, self, access_token,
                created_after=created_after, next_token=next_token,
                error_msg="Failed to fetch orders"
            )
            orders = data.get('payload', {}).get('Orders', [])
            for order_data in orders:
                amazon_order_id = order_data.get('AmazonOrderId')
                if not amazon_order_id:
                    continue

                existing = self.env['amazon.sale.order'].search([
                    ('amazon_order_ref', '=', amazon_order_id),
                    ('instance_id', '=', self.id),
                ], limit=1)

                amount = order_data.get('OrderTotal', {})
                vals = {
                    'amazon_order_ref': amazon_order_id,
                    'instance_id': self.id,
                    'order_status': order_data.get('OrderStatus', 'Pending'),
                    'fulfillment_channel': order_data.get('FulfillmentChannel', 'MFN'),
                    'order_type': order_data.get('OrderType', 'StandardOrder'),
                    'purchase_date': order_data.get('PurchaseDate'),
                    'last_update_date': order_data.get('LastUpdateDate'),
                    'sales_channel': order_data.get('SalesChannel', ''),
                    'is_prime': order_data.get('IsPrime', False),
                    'is_business_order': order_data.get('IsBusinessOrder', False),
                    'order_total': float(amount.get('Amount', 0)) if amount else 0,
                    'ship_service_level': order_data.get('ShipServiceLevel', ''),
                }

                # Currency
                currency_code = amount.get('CurrencyCode') if amount else None
                if currency_code:
                    currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
                    if currency:
                        vals['currency_id'] = currency.id

                # Shipping address
                ship_addr = order_data.get('ShippingAddress', {})
                if ship_addr:
                    vals.update({
                        'shipping_address_name': ship_addr.get('Name', ''),
                        'shipping_address_line1': ship_addr.get('AddressLine1', ''),
                        'shipping_address_line2': ship_addr.get('AddressLine2', ''),
                        'shipping_city': ship_addr.get('City', ''),
                        'shipping_state': ship_addr.get('StateOrRegion', ''),
                        'shipping_postal_code': ship_addr.get('PostalCode', ''),
                        'shipping_country_code': ship_addr.get('CountryCode', ''),
                    })

                if existing:
                    existing.write(vals)
                    order_rec = existing
                else:
                    order_rec = self.env['amazon.sale.order'].create(vals)
                    imported += 1

                # Fetch order items
                try:
                    items_data = api.get_order_items(self, access_token, amazon_order_id)
                    order_items = items_data.get('payload', {}).get('OrderItems', [])
                    for item in order_items:
                        item_id = item.get('OrderItemId')
                        existing_line = self.env['amazon.sale.order.line'].search([
                            ('order_id', '=', order_rec.id),
                            ('amazon_order_item_id', '=', item_id),
                        ], limit=1)
                        price_info = item.get('ItemPrice', {})
                        tax_info = item.get('ItemTax', {})
                        shipping_info = item.get('ShippingPrice', {})
                        promo_info = item.get('PromotionDiscount', {})

                        line_vals = {
                            'order_id': order_rec.id,
                            'amazon_order_item_id': item_id,
                            'sku': item.get('SellerSKU', ''),
                            'asin': item.get('ASIN', ''),
                            'title': item.get('Title', ''),
                            'quantity': item.get('QuantityOrdered', 1),
                            'item_price': float(price_info.get('Amount', 0)) if price_info else 0,
                            'item_tax': float(tax_info.get('Amount', 0)) if tax_info else 0,
                            'shipping_price': float(shipping_info.get('Amount', 0)) if shipping_info else 0,
                            'promotion_discount': float(promo_info.get('Amount', 0)) if promo_info else 0,
                        }

                        # Auto-map product
                        if line_vals['sku']:
                            amz_prod = self.env['amazon.product'].search([
                                ('sku', '=', line_vals['sku']), ('instance_id', '=', self.id)
                            ], limit=1)
                            if amz_prod:
                                line_vals['amazon_product_id'] = amz_prod.id
                                if amz_prod.odoo_product_id:
                                    line_vals['odoo_product_id'] = amz_prod.odoo_product_id.id

                        if existing_line:
                            existing_line.write(line_vals)
                        else:
                            self.env['amazon.sale.order.line'].create(line_vals)
                except Exception as exc:
                    _logger.warning("Failed to fetch items for order %s: %s", amazon_order_id, exc)

                # Auto-create Odoo sale.order if not exists
                if not order_rec.sale_order_id and order_rec.order_line_ids:
                    try:
                        order_rec.action_create_sale_order()
                        _logger.info("Auto-created Odoo SO for Amazon order %s", amazon_order_id)
                    except Exception as exc:
                        _logger.warning("Failed to auto-create SO for %s: %s", amazon_order_id, exc)

            next_token = data.get('payload', {}).get('NextToken')
            if not next_token:
                break

        self.last_order_sync = fields.Datetime.now()
        log.log_success(
            summary="%d new order(s) imported." % imported,
            records_processed=imported, records_created=imported,
        )
        return self._notify("Order Import", "%d new order(s) imported from Amazon." % imported)

    def action_import_fbm_orders(self):
        """Import only FBM (Merchant Fulfilled) orders."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        created_after = self.last_order_sync or (datetime.now(timezone.utc) - timedelta(days=30))
        if isinstance(created_after, datetime):
            created_after = created_after.strftime('%Y-%m-%dT%H:%M:%SZ')

        data = self._api_call_safe(
            api.get_orders, self, access_token,
            created_after=created_after, fulfillment_channels='MFN',
            error_msg="Failed to fetch FBM orders"
        )
        return self._process_order_import(data, access_token, api, 'FBM')

    def action_import_fba_orders(self):
        """Import only FBA (Amazon Fulfilled) orders."""
        self.ensure_one()
        self._auto_fix_region()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        created_after = self.last_order_sync or (datetime.now(timezone.utc) - timedelta(days=30))
        if isinstance(created_after, datetime):
            created_after = created_after.strftime('%Y-%m-%dT%H:%M:%SZ')

        data = self._api_call_safe(
            api.get_orders, self, access_token,
            created_after=created_after, fulfillment_channels='AFN',
            error_msg="Failed to fetch FBA orders"
        )
        return self._process_order_import(data, access_token, api, 'FBA')

    def _process_order_import(self, data, access_token, api, label):
        """Helper to process order import response."""
        orders = data.get('payload', {}).get('Orders', [])
        imported = 0
        for order_data in orders:
            amazon_order_id = order_data.get('AmazonOrderId')
            if not amazon_order_id:
                continue
            existing = self.env['amazon.sale.order'].search([
                ('amazon_order_ref', '=', amazon_order_id), ('instance_id', '=', self.id)
            ], limit=1)
            if existing:
                continue
            amount = order_data.get('OrderTotal', {})
            vals = {
                'amazon_order_ref': amazon_order_id,
                'instance_id': self.id,
                'order_status': order_data.get('OrderStatus', 'Pending'),
                'fulfillment_channel': order_data.get('FulfillmentChannel', 'MFN'),
                'purchase_date': order_data.get('PurchaseDate'),
                'order_total': float(amount.get('Amount', 0)) if amount else 0,
                'sales_channel': order_data.get('SalesChannel', ''),
            }
            self.env['amazon.sale.order'].create(vals)
            imported += 1
        self.last_order_sync = fields.Datetime.now()
        return self._notify("%s Order Import" % label, "%d new %s order(s) imported." % (imported, label))

    # ══════════════════════════════════════════════════
    # Order Status & Cancellation Check
    # ══════════════════════════════════════════════════

    def action_check_canceled_orders(self):
        """Check for canceled orders on Amazon and update status in Odoo."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        orders = self.env['amazon.sale.order'].search([
            ('instance_id', '=', self.id),
            ('order_status', 'not in', ['Canceled', 'Shipped']),
        ])
        canceled = 0
        for order in orders:
            try:
                data = api.get_order(self, access_token, order.amazon_order_ref)
                status = data.get('payload', {}).get('OrderStatus')
                if status and order.order_status != status:
                    order.order_status = status
                    if status == 'Canceled':
                        canceled += 1
                        if order.sale_order_id and order.sale_order_id.state not in ('cancel', 'done'):
                            order.sale_order_id.action_cancel()
            except Exception as exc:
                _logger.warning("Failed to check order %s: %s", order.amazon_order_ref, exc)

        return self._notify("Order Status Check", "%d order(s) found canceled." % canceled)

    def action_update_fbm_order_status(self):
        """Update status for all pending FBM orders."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        orders = self.env['amazon.sale.order'].search([
            ('instance_id', '=', self.id),
            ('fulfillment_channel', '=', 'MFN'),
            ('order_status', 'in', ['Pending', 'Unshipped', 'PartiallyShipped']),
        ])
        updated = 0
        for order in orders:
            try:
                data = api.get_order(self, access_token, order.amazon_order_ref)
                new_status = data.get('payload', {}).get('OrderStatus')
                if new_status and new_status != order.order_status:
                    order.order_status = new_status
                    updated += 1
            except Exception as exc:
                _logger.warning("Failed to update order %s: %s", order.amazon_order_ref, exc)

        return self._notify("FBM Status Update", "%d order(s) updated." % updated)

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
        amazon_order.ship_date = fields.Datetime.now()

    def _cancel_amazon_order(self, amazon_order):
        """Mark order as canceled (Amazon doesn't have a cancel API for sellers - cancellation is tracked)."""
        amazon_order.order_status = 'Canceled'
        if amazon_order.sale_order_id and amazon_order.sale_order_id.state not in ('cancel', 'done'):
            amazon_order.sale_order_id.action_cancel()

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
        """Fetch the list of settlement reports available on Amazon and create
        a draft ``amazon.settlement.report`` row for any that aren't already
        in the DB. Does NOT download the report bodies — that is per-report
        via ``action_download_report``.
        """
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        reports_list = self._api_call_safe(
            api.get_settlement_reports_list, self, access_token,
            error_msg="Failed to fetch settlement reports list",
        )
        total = len(reports_list)
        created = 0
        skipped_existing = 0
        skipped_invalid = 0

        for rpt in reports_list:
            report_id = rpt.get('reportId')
            if not report_id:
                skipped_invalid += 1
                continue

            existing = self.env['amazon.settlement.report'].search([
                ('settlement_id', '=', report_id),
                ('instance_id', '=', self.id),
            ], limit=1)
            if existing:
                skipped_existing += 1
                continue

            # SP-API returns ISO 8601 timestamps; persist only the date portion
            # for ``start_date``/``end_date`` (Date fields on the model).
            data_start = (rpt.get('dataStartTime') or '')[:10] or False
            data_end = (rpt.get('dataEndTime') or '')[:10] or False

            # SP-API uses 'YYYY-MM-DDTHH:MM:SS[+Z]'. Odoo Datetime accepts the
            # 'YYYY-MM-DD HH:MM:SS' form, so we strip the 'T' and trim any
            # timezone suffix. None / empty becomes False.
            def _to_dt(iso):
                if not iso:
                    return False
                # Take first 19 chars 'YYYY-MM-DDTHH:MM:SS' and replace 'T' with space.
                cleaned = iso[:19].replace('T', ' ')
                return cleaned if len(cleaned) == 19 else False

            # marketplaceIds may be a list of strings; join for the Char field.
            mp_ids = rpt.get('marketplaceIds') or []
            marketplace_value = ','.join(mp_ids) if isinstance(mp_ids, list) else (mp_ids or '')

            self.env['amazon.settlement.report'].create({
                'instance_id': self.id,
                'settlement_id': report_id,
                'report_document_id': rpt.get('reportDocumentId') or '',
                'start_date': data_start,
                'end_date': data_end,
                'state': 'draft',
                # Amazon-side metadata — captured verbatim, surfaced in UI for audit.
                'report_type': rpt.get('reportType') or '',
                'processing_status': rpt.get('processingStatus') or '',
                'created_time': _to_dt(rpt.get('createdTime')),
                'processing_start_time': _to_dt(rpt.get('processingStartTime')),
                'processing_end_time': _to_dt(rpt.get('processingEndTime')),
                'marketplace_ids': marketplace_value,
            })
            created += 1

        # Message intentionally distinguishes the three buckets so the user
        # can self-diagnose: if total=0, the API returned nothing (auth/role/
        # marketplace issue on Amazon's side); if skipped_existing dominates,
        # the import is working and they already have these reports.
        msg = (
            "Amazon returned %d settlement report(s); "
            "%d new added, %d already imported, %d skipped (no reportId)."
            % (total, created, skipped_existing, skipped_invalid)
        )
        return self._notify("Settlement Reports", msg)

    def _download_settlement_report(self, report_rec):
        """Download and parse a specific settlement report."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        if not report_rec.report_document_id:
            raise UserError("No report document ID. The report may still be processing.")

        doc_data = self._api_call_safe(
            api.get_report_document, self, access_token, report_rec.report_document_id,
            error_msg="Failed to get report document"
        )
        download_url = doc_data.get('url')
        if not download_url:
            raise UserError("No download URL for settlement report.")

        import csv as csv_mod
        import io as io_mod

        raw_text = api.download_report_document(
            download_url,
            compression=doc_data.get('compressionAlgorithm'),
            encryption=doc_data.get('encryptionDetails'),
            instance=self,
        )
        # Same defensive parsing as fetch_report_rows in amazon_api.py: strip
        # BOM, normalise line endings, and disable CSV quoting. Without this a
        # stray \r in an unquoted field raises _csv.Error.
        raw_text = raw_text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
        reader = csv_mod.DictReader(
            io_mod.StringIO(raw_text), delimiter='\t', quoting=csv_mod.QUOTE_NONE,
        )

        report_rec.line_ids.unlink()
        for row in reader:
            self.env['amazon.settlement.report.line'].create({
                'report_id': report_rec.id,
                'order_id_ref': row.get('order-id', ''),
                'order_item_id': row.get('order-item-code', ''),
                'transaction_type': row.get('transaction-type', ''),
                'amount_type': row.get('amount-type', ''),
                'amount_description': row.get('amount-description', ''),
                'amount': float(row.get('amount', 0) or 0),
                'posted_date': row.get('posted-date', ''),
            })

        report_rec.state = 'downloaded'

    # ══════════════════════════════════════════════════
    # Return Reports
    # ══════════════════════════════════════════════════

    def _download_return_report(self, report_rec):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        rows = self._api_call_safe(
            api.fetch_fba_returns_report, self, access_token,
            error_msg="Failed to fetch FBA returns report"
        )
        report_rec.line_ids.unlink()
        for row in rows:
            self.env['amazon.return.report.line'].create({
                'report_id': report_rec.id,
                'amazon_order_id': row.get('order-id', ''),
                'sku': row.get('sku', ''),
                'asin': row.get('asin', ''),
                'fnsku': row.get('fnsku', ''),
                'product_name': row.get('product-name', ''),
                'quantity': int(row.get('quantity', 1) or 1),
                'fulfillment_center_id': row.get('fulfillment-center-id', ''),
                'return_date': row.get('return-date', ''),
                'return_reason': row.get('reason', ''),
                'status': row.get('status', ''),
            })
        report_rec.state = 'downloaded'

    # ══════════════════════════════════════════════════
    # FBA Inventory Reports
    # ══════════════════════════════════════════════════

    def _download_fba_inventory_report(self, report_rec):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        if report_rec.report_type == 'live_stock':
            rows = self._api_call_safe(api.fetch_fba_inventory_report, self, access_token, error_msg="Failed to fetch FBA inventory")
        elif report_rec.report_type == 'adjustment':
            rows = self._api_call_safe(api.fetch_fba_inventory_adjustment_report, self, access_token, error_msg="Failed to fetch adjustment report")
        elif report_rec.report_type == 'fba_shipment':
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
        _logger.info("Removal order submission to Amazon: %s (via Feeds API)", removal_order.name)
        removal_order.state = 'submitted'

    def _check_removal_order_status(self, removal_order):
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()
        rows = self._api_call_safe(api.fetch_removal_report, self, access_token, error_msg="Failed to fetch removal report")
        for row in rows:
            rid = row.get('order-id', '')
            if rid == removal_order.removal_order_id:
                status = row.get('order-status', '')
                if status.lower() == 'completed':
                    removal_order.state = 'completed'
                elif status.lower() in ('pending', 'processing'):
                    removal_order.state = 'processing'

    def action_import_removal_orders(self):
        """Import removal orders from Amazon report."""
        self.ensure_one()
        self._check_required_fields()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        try:
            rows = self._api_call_safe(api.fetch_removal_report, self, access_token, error_msg="Failed to fetch removal report")
        except UserError as exc:
            msg = str(exc.args[0] if exc.args else exc)
            if 'CANCELLED' in msg:
                return self._notify("Removal Orders", "No removal order data available. This account has no FBA removal orders.", 'warning')
            raise
        imported = 0
        for row in rows:
            rid = row.get('order-id', '')
            if not rid:
                continue
            existing = self.env['amazon.removal.order'].search([
                ('removal_order_id', '=', rid), ('instance_id', '=', self.id)
            ], limit=1)
            if existing:
                continue
            order_type = 'Disposal' if 'disposal' in (row.get('order-type', '')).lower() else 'Return'
            rec = self.env['amazon.removal.order'].create({
                'instance_id': self.id,
                'removal_order_id': rid,
                'order_type': order_type,
                'state': 'completed' if row.get('order-status', '').lower() == 'completed' else 'processing',
            })
            sku = row.get('sku', '')
            if sku:
                self.env['amazon.removal.order.line'].create({
                    'order_id': rec.id,
                    'sku': sku,
                    'fnsku': row.get('fnsku', ''),
                    'requested_quantity': float(row.get('requested-quantity', 0) or 0),
                    'shipped_quantity': float(row.get('shipped-quantity', 0) or 0),
                    'cancelled_quantity': float(row.get('cancelled-quantity', 0) or 0),
                })
            imported += 1
        return self._notify("Removal Orders", "%d removal order(s) imported." % imported)

    # ══════════════════════════════════════════════════
    # Inbound Shipments
    # ══════════════════════════════════════════════════

    def _create_inbound_shipment_plan(self, shipment):
        """Create inbound shipment plan on Amazon."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        items = []
        for line in shipment.line_ids:
            items.append({
                "msku": line.sku,
                "prepOwner": "SELLER",
                "quantity": int(line.quantity_shipped),
            })

        body = {
            "destinationMarketplaces": [self.marketplace_id],
            "items": items,
            "sourceAddress": {
                "name": self.name,
                "countryCode": "IN",
            },
        }

        result = self._api_call_safe(
            api.create_inbound_plan, self, access_token, body,
            error_msg="Failed to create inbound plan"
        )
        plan_id = result.get('inboundPlanId', '')
        if plan_id:
            shipment.shipment_id = plan_id
        shipment.state = 'planning'

    def _submit_inbound_shipment(self, shipment):
        """Submit inbound shipment to Amazon."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        if not shipment.shipment_id:
            raise UserError("Create a shipment plan first.")

        # Get shipment details from the plan
        result = self._api_call_safe(
            api.get_inbound_plan, self, access_token, shipment.shipment_id,
            error_msg="Failed to get inbound plan"
        )
        shipment.state = 'submitted'

    def _update_inbound_shipment_tracking(self, shipment):
        """Update tracking info and mark as shipped."""
        shipment.state = 'shipped'
        shipment.ship_date = fields.Date.today()

    def _check_inbound_shipment_status(self, shipment):
        """Check shipment status from Amazon."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        if not shipment.shipment_id:
            raise UserError("No shipment ID to check.")

        result = self._api_call_safe(
            api.get_inbound_plan, self, access_token, shipment.shipment_id,
            error_msg="Failed to check shipment status"
        )
        status = result.get('status', '')
        status_map = {
            'ACTIVE': 'submitted', 'SHIPPED': 'shipped', 'IN_TRANSIT': 'in_transit',
            'RECEIVING': 'receiving', 'CLOSED': 'closed', 'CANCELLED': 'cancelled',
        }
        if status in status_map:
            shipment.state = status_map[status]

        # Update line received quantities if available
        try:
            items_data = api.get_shipment_items(self, access_token, shipment.shipment_id, shipment.shipment_id)
            for item in items_data.get('items', []):
                sku = item.get('msku', '')
                line = shipment.line_ids.filtered(lambda l: l.sku == sku)
                if line:
                    line[0].quantity_received = item.get('quantityReceived', 0)
        except Exception as exc:
            _logger.warning("Failed to fetch shipment items: %s", exc)

    def _import_inbound_shipment(self, shipment):
        """Import existing shipment from Amazon by ID."""
        self.ensure_one()
        access_token = self._get_access_token_or_raise()
        api = AmazonAPI()

        result = self._api_call_safe(
            api.get_inbound_plan, self, access_token, shipment.shipment_id,
            error_msg="Failed to import shipment"
        )
        if result.get('sourceAddress', {}).get('name'):
            shipment.shipment_name = result['sourceAddress']['name']

        # Import items
        try:
            items_data = api.get_shipment_items(self, access_token, shipment.shipment_id, shipment.shipment_id)
            for item in items_data.get('items', []):
                sku = item.get('msku', '')
                if not sku:
                    continue
                existing_line = shipment.line_ids.filtered(lambda l: l.sku == sku)
                vals = {
                    'shipment_id': shipment.id,
                    'sku': sku,
                    'fnsku': item.get('fnsku', ''),
                    'quantity_shipped': item.get('quantity', 0),
                    'quantity_received': item.get('quantityReceived', 0),
                }
                amz_prod = self.env['amazon.product'].search([
                    ('sku', '=', sku), ('instance_id', '=', self.id)
                ], limit=1)
                if amz_prod:
                    vals['amazon_product_id'] = amz_prod.id
                    if amz_prod.odoo_product_id:
                        vals['odoo_product_id'] = amz_prod.odoo_product_id.id
                if existing_line:
                    existing_line[0].write(vals)
                else:
                    self.env['amazon.inbound.shipment.line'].create(vals)
        except Exception as exc:
            _logger.warning("Failed to import shipment items: %s", exc)

        shipment.state = 'submitted'

    def _get_shipment_labels(self, shipment):
        """Fetch labels — placeholder, labels require specific API not yet available."""
        _logger.info("Label fetching for shipment %s — not yet implemented in SP-API v2024", shipment.shipment_id)
        raise UserError("Label download is not yet supported via SP-API. Download labels from Amazon Seller Central.")

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
                    _logger.info("[AutoSync] Orders synced for %s", inst.name)
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
            if inst._should_run('settlement_sync_interval', 'last_order_sync'):
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
            except Exception as exc:
                _logger.error("Cron order import failed for %s: %s", inst.display_name, exc)

    def cron_import_fbm_orders(self):
        for inst in self.env['amazon.instance'].search([]):
            try:
                inst.action_import_fbm_orders()
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
        for inst in self.env['amazon.instance'].search([]):
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
        for inst in self.env['amazon.instance'].search([]):
            _logger.warning(
                "Cron stock pull skipped for %s. Amazon → Odoo stock reconciliation is manual only.",
                inst.display_name,
            )

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
