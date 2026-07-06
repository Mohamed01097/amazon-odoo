import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonAIPricing(models.Model):
    _name = 'amazon.ai.pricing'
    _description = 'Amazon AI Pricing Suggestion'
    _order = 'generated_at desc'
    _rec_name = 'display_name'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    # Prices
    current_price = fields.Float('Current Price')
    cost_price = fields.Float('Cost Price')
    suggested_price = fields.Float('Suggested Price')
    min_price = fields.Float('Min Price')
    max_price = fields.Float('Max Price')
    buy_box_price = fields.Float('Buy Box Price')

    # AI Analysis
    confidence_score = fields.Float('Confidence', digits=(3, 2))
    reasoning = fields.Text('AI Reasoning')
    price_strategy = fields.Selection([
        ('competitive', 'Competitive'),
        ('penetration', 'Penetration'),
        ('premium', 'Premium'),
        ('value', 'Value'),
        ('undercut', 'Undercut'),
    ], string='Strategy')
    competitor_data = fields.Text('Competitor Data (JSON)')

    # Metadata
    generated_at = fields.Datetime('Generated At', default=fields.Datetime.now)
    status = fields.Selection([
        ('pending', 'Pending Review'),
        ('applied', 'Applied'),
        ('rejected', 'Rejected'),
    ], default='pending', required=True, index=True)
    applied_at = fields.Datetime('Applied At')

    # Computed
    profit_margin = fields.Float('Margin %', compute='_compute_margin', store=True)
    price_change_pct = fields.Float('Change %', compute='_compute_change', store=True)
    display_name = fields.Char(compute='_compute_display_name', store=True)

    @api.depends('product_id', 'suggested_price', 'status')
    def _compute_display_name(self):
        for rec in self:
            sku = rec.product_id.sku or '?'
            rec.display_name = "[%s] %s — %.2f" % (rec.status or '?', sku, rec.suggested_price or 0)

    @api.depends('suggested_price', 'cost_price')
    def _compute_margin(self):
        for rec in self:
            if rec.suggested_price and rec.cost_price:
                rec.profit_margin = ((rec.suggested_price - rec.cost_price) / rec.suggested_price) * 100
            else:
                rec.profit_margin = 0

    @api.depends('suggested_price', 'current_price')
    def _compute_change(self):
        for rec in self:
            if rec.current_price and rec.suggested_price:
                rec.price_change_pct = ((rec.suggested_price - rec.current_price) / rec.current_price) * 100
            else:
                rec.price_change_pct = 0

    def action_apply_price(self):
        """Apply the AI suggested price to the Amazon product and push to Amazon."""
        self.ensure_one()
        if self.status == 'applied':
            raise UserError("Price already applied.")

        self.product_id.amazon_price = self.suggested_price
        self.status = 'applied'
        self.applied_at = fields.Datetime.now()

        # Push to Amazon
        try:
            self.product_id.action_update_in_amazon()
        except Exception as exc:
            _logger.warning("Failed to push price to Amazon for %s: %s", self.product_id.sku, exc)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Price Applied",
                "message": "Price %.2f applied to %s" % (self.suggested_price, self.product_id.sku),
                "type": "success",
            },
        }

    def action_reject_price(self):
        self.ensure_one()
        self.status = 'rejected'


class AmazonAIPricingWizard(models.TransientModel):
    _name = 'amazon.ai.pricing.wizard'
    _description = 'Bulk AI Reprice Wizard'

    instance_id = fields.Many2one('amazon.instance', required=True)
    suggestion_ids = fields.Many2many('amazon.ai.pricing', string='Suggestions')
    product_count = fields.Integer('Products to Analyse', compute='_compute_product_count')

    @api.depends('instance_id')
    def _compute_product_count(self):
        for rec in self:
            rec.product_count = self.env['amazon.product'].search_count([
                ('instance_id', '=', rec.instance_id.id),
                ('status', '=', 'Active'),
                ('amazon_price', '>', 0),
            ]) if rec.instance_id else 0

    def action_generate_suggestions(self):
        """Generate AI pricing suggestions for all active products."""
        self.ensure_one()
        products = self.env['amazon.product'].search([
            ('instance_id', '=', self.instance_id.id),
            ('status', '=', 'Active'),
            ('amazon_price', '>', 0),
        ])
        if not products:
            raise UserError("No active products with prices found.")

        from ..services.ai_service import AmazonAIService
        instance = self.instance_id
        if not instance.ai_api_key:
            raise UserError("AI API Key is not configured.")

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model
        currency = instance.default_currency_id.name if instance.default_currency_id else 'INR'

        created = []
        for product in products:
            cost = product.odoo_product_id.standard_price if product.odoo_product_id else 0
            try:
                result = AmazonAIService.optimize_price(
                    provider, api_key, model,
                    product_name=product.name,
                    category=product.product_type or '',
                    current_price=product.amazon_price,
                    cost_price=cost,
                    currency=currency,
                )
                rec = self.env['amazon.ai.pricing'].create({
                    'product_id': product.id,
                    'current_price': product.amazon_price,
                    'cost_price': cost,
                    'suggested_price': result.get('suggested_price', 0),
                    'min_price': result.get('min_price', 0),
                    'max_price': result.get('max_price', 0),
                    'confidence_score': result.get('confidence', 0),
                    'reasoning': result.get('reasoning', ''),
                    'price_strategy': result.get('strategy', 'competitive'),
                })
                created.append(rec.id)
            except Exception as exc:
                _logger.warning("AI pricing failed for %s: %s", product.sku, exc)

        return {
            'type': 'ir.actions.act_window',
            'name': 'AI Pricing Suggestions',
            'res_model': 'amazon.ai.pricing',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created)],
        }
