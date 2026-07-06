"""
AI Profit Calculator — Real-time profit/loss per product including Amazon fees.
AI Competitor Monitor — Track competitor prices and get undercut alerts.
AI Keyword Tracker — SEO keyword suggestions and ranking insights.
AI Return Analyzer — Pattern detection in returns, suggest product improvements.
AI Sales Insights — Weekly/monthly trend analysis with AI commentary.
"""
import json
import logging
from datetime import timedelta

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
# 1. AI Profit Calculator
# ══════════════════════════════════════════════════

class AmazonProfitCalculator(models.Model):
    _name = 'amazon.profit.calculator'
    _description = 'Amazon Profit Calculator'
    _order = 'net_profit desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    # Revenue
    selling_price = fields.Float('Selling Price')
    units_sold_30d = fields.Integer('Units Sold (30d)')
    gross_revenue = fields.Float('Gross Revenue (30d)', compute='_compute_profit', store=True)

    # Costs
    cost_price = fields.Float('Cost Price (per unit)')
    amazon_referral_fee_pct = fields.Float('Referral Fee %', default=15.0)
    amazon_referral_fee = fields.Float('Referral Fee', compute='_compute_profit', store=True)
    fba_fee = fields.Float('FBA/Shipping Fee (per unit)', default=50.0)
    gst_pct = fields.Float('GST %', default=18.0)
    packaging_cost = fields.Float('Packaging Cost (per unit)', default=10.0)
    other_costs = fields.Float('Other Costs (per unit)')

    # Profit
    total_cost_per_unit = fields.Float('Total Cost/Unit', compute='_compute_profit', store=True)
    profit_per_unit = fields.Float('Profit/Unit', compute='_compute_profit', store=True)
    net_profit = fields.Float('Net Profit (30d)', compute='_compute_profit', store=True)
    profit_margin_pct = fields.Float('Margin %', compute='_compute_profit', store=True)
    roi_pct = fields.Float('ROI %', compute='_compute_profit', store=True)

    # AI
    ai_analysis = fields.Text('AI Profit Analysis')
    profitability = fields.Selection([
        ('highly_profitable', 'Highly Profitable'),
        ('profitable', 'Profitable'),
        ('break_even', 'Break Even'),
        ('loss_making', 'Loss Making'),
    ], string='Status', compute='_compute_profit', store=True)

    generated_at = fields.Datetime('Last Updated', default=fields.Datetime.now)

    @api.depends('selling_price', 'cost_price', 'units_sold_30d', 'amazon_referral_fee_pct',
                 'fba_fee', 'gst_pct', 'packaging_cost', 'other_costs')
    def _compute_profit(self):
        for rec in self:
            sp = rec.selling_price or 0
            cp = rec.cost_price or 0
            units = rec.units_sold_30d or 0

            ref_fee = sp * (rec.amazon_referral_fee_pct or 15) / 100
            gst = sp * (rec.gst_pct or 18) / 100
            total_cost = cp + ref_fee + (rec.fba_fee or 0) + gst + (rec.packaging_cost or 0) + (rec.other_costs or 0)

            profit = sp - total_cost
            margin = (profit / sp * 100) if sp else 0
            roi = (profit / cp * 100) if cp else 0

            rec.amazon_referral_fee = ref_fee
            rec.total_cost_per_unit = total_cost
            rec.profit_per_unit = profit
            rec.gross_revenue = sp * units
            rec.net_profit = profit * units
            rec.profit_margin_pct = margin
            rec.roi_pct = roi

            if margin >= 25:
                rec.profitability = 'highly_profitable'
            elif margin >= 10:
                rec.profitability = 'profitable'
            elif margin >= 0:
                rec.profitability = 'break_even'
            else:
                rec.profitability = 'loss_making'

    def action_calculate(self):
        """Auto-fill from product data and calculate."""
        self.ensure_one()
        product = self.product_id
        updates = {'selling_price': product.amazon_price or 0}
        if product.odoo_product_id:
            updates['cost_price'] = product.odoo_product_id.standard_price or 0
            # Count sales
            today = fields.Date.today()
            units = sum(self.env['sale.order.line'].search([
                ('product_id', '=', product.odoo_product_id.id),
                ('order_id.state', 'in', ['sale', 'done']),
                ('order_id.date_order', '>=', today - timedelta(days=30)),
            ]).mapped('product_uom_qty'))
            updates['units_sold_30d'] = int(units)
        updates['generated_at'] = fields.Datetime.now()
        self.write(updates)

    def action_ai_analyze_profit(self):
        """AI analysis of profitability with recommendations."""
        self.ensure_one()
        self.action_calculate()
        instance = self.product_id.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key not configured.")

        from ..services.ai_service import AmazonAIService
        prompt = """Analyze this Amazon product's profitability and give actionable recommendations.

Product: {name}
Selling Price: {sp} INR
Cost Price: {cp} INR
Amazon Referral Fee: {ref_fee} INR ({ref_pct}%)
FBA/Shipping Fee: {fba} INR
GST: {gst_pct}%
Profit Per Unit: {profit} INR
Margin: {margin}%
ROI: {roi}%
Units Sold (30d): {units}
Net Profit (30d): {net_profit} INR

Give a brief analysis (3-4 sentences) and 3 specific recommendations to improve profitability.
Return plain text, not JSON.""".format(
            name=self.product_id.name, sp=self.selling_price, cp=self.cost_price,
            ref_fee=self.amazon_referral_fee, ref_pct=self.amazon_referral_fee_pct,
            fba=self.fba_fee, gst_pct=self.gst_pct, profit=self.profit_per_unit,
            margin=round(self.profit_margin_pct, 1), roi=round(self.roi_pct, 1),
            units=self.units_sold_30d, net_profit=round(self.net_profit, 2),
        )
        try:
            result = AmazonAIService._call_provider(
                instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt,
            )
            self.ai_analysis = result
        except Exception as exc:
            raise UserError("AI analysis failed: %s" % exc) from exc
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Profit Analysis", "message": "AI analysis complete.", "type": "success"}}


# ══════════════════════════════════════════════════
# 2. AI Competitor Monitor
# ══════════════════════════════════════════════════

class AmazonCompetitorMonitor(models.Model):
    _name = 'amazon.competitor.monitor'
    _description = 'Amazon Competitor Price Monitor'
    _order = 'price_diff_pct desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    our_price = fields.Float('Our Price')
    buy_box_price = fields.Float('Buy Box Price')
    lowest_price = fields.Float('Lowest Price')
    highest_price = fields.Float('Highest Price')
    num_sellers = fields.Integer('Number of Sellers')

    price_diff = fields.Float('Price Difference', compute='_compute_diff', store=True)
    price_diff_pct = fields.Float('Diff %', compute='_compute_diff', store=True)
    price_position = fields.Selection([
        ('lowest', 'We Are Lowest'),
        ('competitive', 'Competitive'),
        ('above_avg', 'Above Average'),
        ('highest', 'We Are Highest'),
    ], string='Position', compute='_compute_diff', store=True)

    ai_strategy = fields.Text('AI Pricing Strategy')
    last_checked = fields.Datetime('Last Checked', default=fields.Datetime.now)

    @api.depends('our_price', 'buy_box_price', 'lowest_price', 'highest_price')
    def _compute_diff(self):
        for rec in self:
            bb = rec.buy_box_price or rec.lowest_price or 0
            our = rec.our_price or 0
            rec.price_diff = our - bb
            rec.price_diff_pct = ((our - bb) / bb * 100) if bb else 0
            if our <= rec.lowest_price and rec.lowest_price > 0:
                rec.price_position = 'lowest'
            elif rec.price_diff_pct <= 5:
                rec.price_position = 'competitive'
            elif rec.price_diff_pct <= 15:
                rec.price_position = 'above_avg'
            else:
                rec.price_position = 'highest'

    def action_fetch_competitor_data(self):
        """Fetch competitor pricing from Amazon API."""
        self.ensure_one()
        from .amazon_api import AmazonAPI
        product = self.product_id
        instance = product.instance_id
        if not product.asin:
            raise UserError("ASIN required to fetch competitor data.")
        api = AmazonAPI()
        access_token = instance._get_access_token_or_raise()
        try:
            data = api.get_competitive_pricing(instance, access_token, product.asin)
            prices = data.get('payload', [])
            if prices:
                product_data = prices[0].get('Product', {})
                comp_prices = product_data.get('CompetitivePricing', {}).get('CompetitivePrices', [])
                offers = product_data.get('NumberOfOfferListings', [])
                if comp_prices:
                    amounts = [float(cp.get('Price', {}).get('ListingPrice', {}).get('Amount', 0)) for cp in comp_prices]
                    amounts = [a for a in amounts if a > 0]
                    if amounts:
                        self.buy_box_price = min(amounts)
                        self.lowest_price = min(amounts)
                        self.highest_price = max(amounts)
                if offers:
                    total = sum(int(o.get('Count', 0)) for o in offers)
                    self.num_sellers = total
            self.our_price = product.amazon_price
            self.last_checked = fields.Datetime.now()
        except Exception as exc:
            _logger.warning("Competitor fetch failed for %s: %s", product.asin, exc)

    def action_ai_strategy(self):
        """Get AI recommendation for competitive pricing."""
        self.ensure_one()
        instance = self.product_id.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key not configured.")
        from ..services.ai_service import AmazonAIService
        prompt = """You are an Amazon pricing strategist. Analyze this competitive data and recommend a strategy.

Product: {name}
Our Price: {our} INR
Buy Box Price: {bb} INR
Lowest Competitor: {low} INR
Highest Competitor: {high} INR
Number of Sellers: {sellers}
Our Position: {pos}
Price Difference: {diff}%

Give a brief strategy (3-4 sentences) with a specific recommended price.
Return plain text.""".format(
            name=self.product_id.name, our=self.our_price, bb=self.buy_box_price,
            low=self.lowest_price, high=self.highest_price, sellers=self.num_sellers,
            pos=self.price_position, diff=round(self.price_diff_pct, 1),
        )
        try:
            self.ai_strategy = AmazonAIService._call_provider(
                instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt,
            )
        except Exception as exc:
            raise UserError("AI strategy failed: %s" % exc) from exc
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "AI Strategy", "message": "Strategy generated.", "type": "success"}}


# ══════════════════════════════════════════════════
# 3. AI Keyword Tracker
# ══════════════════════════════════════════════════

class AmazonKeywordTracker(models.Model):
    _name = 'amazon.keyword.tracker'
    _description = 'Amazon SEO Keyword Tracker'
    _order = 'relevance_score desc'
    _rec_name = 'keyword'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    keyword = fields.Char('Keyword', required=True)
    search_volume = fields.Selection([
        ('very_high', 'Very High'),
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
    ], string='Search Volume')
    relevance_score = fields.Integer('Relevance (0-100)')
    in_title = fields.Boolean('In Title')
    in_bullets = fields.Boolean('In Bullet Points')
    in_backend = fields.Boolean('In Backend Keywords')
    keyword_type = fields.Selection([
        ('primary', 'Primary'),
        ('secondary', 'Secondary'),
        ('long_tail', 'Long Tail'),
        ('competitor', 'Competitor'),
    ], string='Type', default='primary')

    generated_at = fields.Datetime('Generated', default=fields.Datetime.now)

    @api.model
    def action_generate_keywords(self, product_id):
        """Use AI to generate keyword suggestions for a product."""
        product = self.env['amazon.product'].browse(product_id)
        if not product.exists():
            raise UserError("Product not found.")
        instance = product.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key not configured.")

        from ..services.ai_service import AmazonAIService
        prompt = """You are an Amazon SEO keyword expert. Generate the best keywords for this product.

Product: {name}
Brand: {brand}
Category: {ptype}
Description: {desc}

Return ONLY valid JSON array of keywords:
[
  {{"keyword": "keyword phrase", "type": "primary/secondary/long_tail", "volume": "very_high/high/medium/low", "relevance": 95}},
  ...
]

Generate exactly 20 keywords: 5 primary, 5 secondary, 10 long-tail. Sort by relevance.""".format(
            name=product.name or '', brand=product.brand or '',
            ptype=product.product_type or '', desc=(product.description or '')[:300],
        )
        try:
            result = AmazonAIService._call_and_parse(
                instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt,
            )
        except Exception as exc:
            raise UserError("AI keyword generation failed: %s" % exc) from exc

        # Handle both list and dict responses
        keywords = result if isinstance(result, list) else result.get('keywords', [])

        # Delete old keywords for this product
        self.search([('product_id', '=', product.id)]).unlink()

        # Check which keywords are already in product content
        title = (product.name or '').lower()
        bullets = ' '.join([bp.name for bp in product.bullet_point_ids]).lower()
        backend = (product.search_terms or '').lower()

        created = 0
        for kw in keywords[:25]:
            if not isinstance(kw, dict):
                continue
            keyword = kw.get('keyword', '')
            if not keyword:
                continue
            kw_lower = keyword.lower()
            self.create({
                'product_id': product.id,
                'keyword': keyword,
                'keyword_type': kw.get('type', 'primary'),
                'search_volume': kw.get('volume', 'medium'),
                'relevance_score': kw.get('relevance', 50),
                'in_title': kw_lower in title,
                'in_bullets': kw_lower in bullets,
                'in_backend': kw_lower in backend,
            })
            created += 1

        return {
            'type': 'ir.actions.act_window',
            'name': 'Keywords for %s' % product.name[:30],
            'res_model': 'amazon.keyword.tracker',
            'view_mode': 'list',
            'domain': [('product_id', '=', product.id)],
        }


# ══════════════════════════════════════════════════
# 4. AI Return Analyzer
# ══════════════════════════════════════════════════

class AmazonReturnAnalyzer(models.Model):
    _name = 'amazon.return.analyzer'
    _description = 'Amazon Return Pattern Analyzer'
    _order = 'return_rate desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    total_orders = fields.Integer('Total Orders (90d)')
    total_returns = fields.Integer('Total Returns (90d)')
    return_rate = fields.Float('Return Rate %', compute='_compute_rate', store=True)

    top_reason = fields.Char('Top Return Reason')
    common_reasons = fields.Text('Common Reasons (JSON)')
    ai_analysis = fields.Text('AI Analysis & Recommendations')

    risk_level = fields.Selection([
        ('low', 'Low (<5%)'),
        ('medium', 'Medium (5-10%)'),
        ('high', 'High (10-20%)'),
        ('critical', 'Critical (>20%)'),
    ], string='Risk Level', compute='_compute_rate', store=True)

    generated_at = fields.Datetime('Last Updated', default=fields.Datetime.now)

    @api.depends('total_orders', 'total_returns')
    def _compute_rate(self):
        for rec in self:
            if rec.total_orders:
                rec.return_rate = (rec.total_returns / rec.total_orders) * 100
            else:
                rec.return_rate = 0
            rate = rec.return_rate
            if rate >= 20:
                rec.risk_level = 'critical'
            elif rate >= 10:
                rec.risk_level = 'high'
            elif rate >= 5:
                rec.risk_level = 'medium'
            else:
                rec.risk_level = 'low'

    def action_ai_analyze_returns(self):
        """AI analysis of return patterns with improvement suggestions."""
        self.ensure_one()
        instance = self.product_id.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key not configured.")
        from ..services.ai_service import AmazonAIService
        prompt = """Analyze this Amazon product's return data and suggest improvements.

Product: {name}
Category: {ptype}
Price: {price} INR
Total Orders (90d): {orders}
Total Returns (90d): {returns}
Return Rate: {rate}%
Top Return Reason: {reason}

Give:
1. Root cause analysis (2-3 sentences)
2. 3 specific actionable recommendations to reduce returns
3. Listing improvements that could set better expectations
Return plain text.""".format(
            name=self.product_id.name, ptype=self.product_id.product_type or '',
            price=self.product_id.amazon_price, orders=self.total_orders,
            returns=self.total_returns, rate=round(self.return_rate, 1),
            reason=self.top_reason or 'Not specified',
        )
        try:
            self.ai_analysis = AmazonAIService._call_provider(
                instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt,
            )
        except Exception as exc:
            raise UserError("AI analysis failed: %s" % exc) from exc
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Return Analysis", "message": "AI analysis complete.", "type": "success"}}


# ══════════════════════════════════════════════════
# 5. AI Sales Insights
# ══════════════════════════════════════════════════

class AmazonSalesInsights(models.Model):
    _name = 'amazon.sales.insights'
    _description = 'Amazon AI Sales Insights'
    _order = 'generated_at desc'

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade')
    period = fields.Selection([
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], required=True, default='weekly')

    # Metrics
    total_orders = fields.Integer('Total Orders')
    total_revenue = fields.Float('Total Revenue')
    avg_order_value = fields.Float('Avg Order Value')
    top_product_id = fields.Many2one('amazon.product', 'Top Selling Product')
    top_product_units = fields.Integer('Top Product Units')
    worst_product_id = fields.Many2one('amazon.product', 'Worst Selling Product')
    new_customers = fields.Integer('New Customers')
    repeat_customers = fields.Integer('Repeat Customers')

    # AI
    ai_summary = fields.Text('AI Summary & Insights')
    ai_recommendations = fields.Text('AI Recommendations')
    trend = fields.Selection([
        ('growing', 'Growing'),
        ('stable', 'Stable'),
        ('declining', 'Declining'),
    ], string='Trend')

    generated_at = fields.Datetime('Generated At', default=fields.Datetime.now)

    def action_generate_insights(self):
        """Calculate metrics and generate AI insights."""
        self.ensure_one()
        instance = self.instance_id
        today = fields.Date.today()
        days = 7 if self.period == 'weekly' else 30
        start_date = today - timedelta(days=days)

        orders = self.env['amazon.sale.order'].search([
            ('instance_id', '=', instance.id),
            ('purchase_date', '>=', start_date),
        ])
        self.total_orders = len(orders)
        self.total_revenue = sum(orders.mapped('order_total'))
        self.avg_order_value = self.total_revenue / self.total_orders if self.total_orders else 0

        # Top product
        product_sales = {}
        for order in orders:
            for line in order.order_line_ids:
                pid = line.amazon_product_id.id if line.amazon_product_id else None
                if pid:
                    product_sales[pid] = product_sales.get(pid, 0) + line.quantity
        if product_sales:
            top_pid = max(product_sales, key=product_sales.get)
            self.top_product_id = top_pid
            self.top_product_units = int(product_sales[top_pid])
            worst_pid = min(product_sales, key=product_sales.get)
            self.worst_product_id = worst_pid

        self.generated_at = fields.Datetime.now()

        # AI Analysis
        if instance.ai_api_key:
            from ..services.ai_service import AmazonAIService
            prompt = """Analyze this Amazon seller's {period} performance and give insights.

Period: Last {days} days
Total Orders: {orders}
Total Revenue: {revenue} INR
Avg Order Value: {aov} INR
Top Product: {top} ({top_units} units)

Give:
1. Performance summary (2-3 sentences)
2. Key trends observed
3. 3 specific recommendations for next {period}
Return plain text.""".format(
                period=self.period, days=days, orders=self.total_orders,
                revenue=round(self.total_revenue, 2), aov=round(self.avg_order_value, 2),
                top=self.top_product_id.name if self.top_product_id else 'N/A',
                top_units=self.top_product_units,
            )
            try:
                result = AmazonAIService._call_provider(
                    instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt,
                )
                self.ai_summary = result
            except Exception as exc:
                _logger.warning("AI insights failed: %s", exc)

        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Sales Insights", "message": "Report generated.", "type": "success"}}
