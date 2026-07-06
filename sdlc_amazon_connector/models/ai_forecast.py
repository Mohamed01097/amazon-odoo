import json
import logging
from datetime import timedelta

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonDemandForecast(models.Model):
    _name = 'amazon.demand.forecast'
    _description = 'Amazon AI Demand Forecast'
    _order = 'generated_at desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    # Forecast data
    forecast_date = fields.Date('Forecast Date', default=fields.Date.today)
    forecast_30d = fields.Integer('Forecast 30 Days (units)')
    forecast_60d = fields.Integer('Forecast 60 Days (units)')
    forecast_90d = fields.Integer('Forecast 90 Days (units)')

    # Reorder
    reorder_point = fields.Integer('Reorder Point (units)')
    reorder_qty = fields.Integer('Suggested Reorder Qty')
    current_stock = fields.Float('Current Stock')
    days_of_stock = fields.Integer('Days of Stock', compute='_compute_days_of_stock', store=True)
    lead_time_days = fields.Integer('Lead Time (days)')

    # Risk
    stockout_risk = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string='Stockout Risk', default='low', index=True)

    # Analysis
    seasonality_notes = fields.Text('Seasonality Notes')
    reasoning = fields.Text('AI Reasoning')
    weekly_sales_data = fields.Text('Weekly Sales Data (JSON)')

    # Metadata
    generated_at = fields.Datetime('Generated At', default=fields.Datetime.now)

    # Purchase order
    purchase_order_id = fields.Many2one('purchase.order', 'Suggested PO', copy=False)

    @api.depends('current_stock', 'forecast_30d')
    def _compute_days_of_stock(self):
        for rec in self:
            if rec.forecast_30d and rec.forecast_30d > 0:
                daily = rec.forecast_30d / 30.0
                rec.days_of_stock = int(rec.current_stock / daily) if daily > 0 else 999
            else:
                rec.days_of_stock = 999

    def action_generate_forecast(self):
        """Generate AI demand forecast for this product."""
        self.ensure_one()
        product = self.product_id
        instance = product.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key is not configured.")

        from ..services.ai_service import AmazonAIService

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # Gather sales history (last 13 weeks)
        today = fields.Date.today()
        weekly_sales = []
        odoo_prod = product.odoo_product_id
        if odoo_prod:
            for week in range(13):
                start = today - timedelta(days=(week + 1) * 7)
                end = today - timedelta(days=week * 7)
                qty = sum(
                    self.env['sale.order.line'].search([
                        ('product_id', '=', odoo_prod.id),
                        ('order_id.state', 'in', ['sale', 'done']),
                        ('order_id.date_order', '>=', start),
                        ('order_id.date_order', '<', end),
                    ]).mapped('product_uom_qty')
                )
                weekly_sales.append(qty)
            weekly_sales.reverse()

        current_stock = product.odoo_stock or product.amazon_qty or 0

        # Get lead time from supplier
        lead_time = 7
        if odoo_prod:
            supplier = self.env['product.supplierinfo'].search([
                ('product_tmpl_id', '=', odoo_prod.product_tmpl_id.id),
            ], limit=1)
            if supplier:
                lead_time = supplier.delay or 7

        avg_price = product.amazon_price or 0

        try:
            result = AmazonAIService.predict_inventory(
                provider, api_key, model,
                product_name=product.name,
                current_stock=current_stock,
                avg_daily_sales=round(sum(weekly_sales) / 91, 2) if weekly_sales else 0,
                lead_time=lead_time,
                sales_history=', '.join(["%.0f" % s for s in weekly_sales]) if weekly_sales else 'N/A',
            )
        except Exception as exc:
            raise UserError("AI forecast failed: %s" % exc) from exc

        self.write({
            'forecast_30d': result.get('predicted_daily_demand', 0) * 30,
            'forecast_60d': result.get('predicted_daily_demand', 0) * 60,
            'forecast_90d': result.get('predicted_daily_demand', 0) * 90,
            'reorder_point': result.get('reorder_point', 0),
            'reorder_qty': result.get('suggested_reorder_qty', 0),
            'current_stock': current_stock,
            'lead_time_days': lead_time,
            'stockout_risk': result.get('stockout_risk', 'low'),
            'reasoning': result.get('reasoning', ''),
            'seasonality_notes': result.get('seasonal_factors', ''),
            'weekly_sales_data': json.dumps(weekly_sales) if weekly_sales else '[]',
            'generated_at': fields.Datetime.now(),
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Forecast Generated",
                "message": "30d: %d | 60d: %d | Risk: %s" % (
                    self.forecast_30d, self.forecast_60d, self.stockout_risk,
                ),
                "type": "warning" if self.stockout_risk == 'high' else "success",
                "sticky": self.stockout_risk == 'high',
            },
        }

    def action_create_purchase_order(self):
        """Create a draft PO with the suggested reorder quantity."""
        self.ensure_one()
        if self.purchase_order_id:
            raise UserError("PO already created: %s" % self.purchase_order_id.name)
        if not self.reorder_qty:
            raise UserError("No reorder quantity suggested.")

        product = self.product_id
        odoo_prod = product.odoo_product_id
        if not odoo_prod:
            raise UserError("No Odoo product linked. Map the product first.")

        # Find supplier
        supplier = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', '=', odoo_prod.product_tmpl_id.id),
        ], limit=1)
        if not supplier:
            raise UserError("No supplier configured for %s. Add a vendor first." % odoo_prod.display_name)

        po = self.env['purchase.order'].create({
            'partner_id': supplier.partner_id.id,
            'origin': 'AI Forecast — %s' % product.sku,
            'order_line': [(0, 0, {
                'product_id': odoo_prod.id,
                'product_qty': self.reorder_qty,
                'price_unit': supplier.price or odoo_prod.standard_price,
                'name': '[%s] %s — AI Reorder' % (product.sku, odoo_prod.name),
            })],
        })
        self.purchase_order_id = po.id

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': po.id,
            'view_mode': 'form',
        }
