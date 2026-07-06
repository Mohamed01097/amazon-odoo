"""
Amazon Product Health Score — UNIQUE FEATURE
Combines all data points into a single 0-100 health score per product.
No competitor has this.
"""
import json
import logging
from datetime import timedelta

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class AmazonProductHealth(models.Model):
    _name = 'amazon.product.health'
    _description = 'Amazon Product Health Score'
    _order = 'health_score asc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    # ── Overall Score ──
    health_score = fields.Integer('Health Score (0-100)', default=0)
    health_grade = fields.Selection([
        ('A', 'A — Excellent'),
        ('B', 'B — Good'),
        ('C', 'C — Needs Attention'),
        ('D', 'D — At Risk'),
        ('F', 'F — Critical'),
    ], string='Grade', compute='_compute_grade', store=True)

    # ── Component Scores ──
    listing_score = fields.Integer('Listing Quality (0-100)')
    pricing_score = fields.Integer('Pricing Health (0-100)')
    stock_score = fields.Integer('Inventory Health (0-100)')
    sales_score = fields.Integer('Sales Velocity (0-100)')
    review_score = fields.Integer('Review Score (0-100)')

    # ── Issues Detected ──
    issues = fields.Text('Issues (JSON)')
    issue_count = fields.Integer('Issue Count', compute='_compute_issue_count', store=True)
    recommendations = fields.Text('AI Recommendations (JSON)')

    # ── Key Metrics ──
    days_of_stock = fields.Integer('Days of Stock')
    sales_last_7d = fields.Float('Sales (7d)')
    sales_last_30d = fields.Float('Sales (30d)')
    conversion_estimate = fields.Float('Estimated CVR %')
    buy_box_eligible = fields.Boolean('Buy Box Eligible')

    # ── Alerts ──
    has_critical_alert = fields.Boolean('Critical Alert', compute='_compute_alerts', store=True)
    alert_summary = fields.Char('Alert Summary', compute='_compute_alerts', store=True)

    generated_at = fields.Datetime('Last Updated', default=fields.Datetime.now)

    @api.depends('health_score')
    def _compute_grade(self):
        for rec in self:
            s = rec.health_score
            if s >= 85:
                rec.health_grade = 'A'
            elif s >= 70:
                rec.health_grade = 'B'
            elif s >= 50:
                rec.health_grade = 'C'
            elif s >= 30:
                rec.health_grade = 'D'
            else:
                rec.health_grade = 'F'

    @api.depends('issues')
    def _compute_issue_count(self):
        for rec in self:
            try:
                rec.issue_count = len(json.loads(rec.issues or '[]'))
            except (json.JSONDecodeError, TypeError):
                rec.issue_count = 0

    @api.depends('health_score', 'stock_score', 'issue_count')
    def _compute_alerts(self):
        for rec in self:
            alerts = []
            if rec.health_score < 30:
                alerts.append('CRITICAL: Health score below 30')
            if rec.stock_score < 20:
                alerts.append('STOCKOUT RISK')
            if rec.issue_count > 5:
                alerts.append('%d issues detected' % rec.issue_count)
            rec.has_critical_alert = rec.health_score < 30 or rec.stock_score < 20
            rec.alert_summary = ' | '.join(alerts) if alerts else ''

    def action_calculate_health(self):
        """Calculate comprehensive health score from all data points."""
        self.ensure_one()
        product = self.product_id
        issues = []
        recs = []

        # ── 1. LISTING QUALITY (0-100) ──
        listing = 0
        if product.name and len(product.name) >= 50:
            listing += 20
        elif product.name:
            listing += 10
            issues.append('Title too short (<%d chars)' % len(product.name))

        if product.bullet_point_ids and len(product.bullet_point_ids) >= 5:
            listing += 20
        elif product.bullet_point_ids:
            listing += len(product.bullet_point_ids) * 4
            issues.append('Only %d bullet points (need 5)' % len(product.bullet_point_ids))
        else:
            issues.append('No bullet points')

        if product.description and len(product.description) >= 100:
            listing += 15
        elif not product.description:
            issues.append('No description')

        if product.image_ids and len(product.image_ids) >= 5:
            listing += 20
        elif product.image_ids:
            listing += len(product.image_ids) * 4
            issues.append('Only %d images (need 5+)' % len(product.image_ids))
        elif product.image_url or product.product_image:
            listing += 10
            issues.append('Only 1 image')
        else:
            issues.append('No images')

        if product.search_terms:
            listing += 10
        else:
            issues.append('No backend keywords')

        if product.brand:
            listing += 10
        else:
            issues.append('No brand')

        if product.barcode or product.no_barcode:
            listing += 5
        else:
            issues.append('No barcode/GTIN')

        # ── 2. PRICING HEALTH (0-100) ──
        pricing = 50  # Baseline
        if product.amazon_price and product.amazon_price > 0:
            pricing += 20
            if product.odoo_product_id and product.odoo_product_id.standard_price > 0:
                margin = (product.amazon_price - product.odoo_product_id.standard_price) / product.amazon_price * 100
                if margin >= 30:
                    pricing += 30
                    recs.append('Healthy margin (%.0f%%)' % margin)
                elif margin >= 15:
                    pricing += 20
                elif margin >= 0:
                    pricing += 10
                    issues.append('Low margin (%.0f%%)' % margin)
                else:
                    issues.append('NEGATIVE margin (%.0f%%)' % margin)
                    pricing -= 30
        else:
            pricing = 10
            issues.append('No price set')

        # ── 3. INVENTORY HEALTH (0-100) ──
        stock = product.odoo_stock or product.amazon_qty or 0
        stock_score = 0
        if stock > 50:
            stock_score = 100
        elif stock > 20:
            stock_score = 80
        elif stock > 10:
            stock_score = 60
            issues.append('Low stock (%d units)' % int(stock))
        elif stock > 0:
            stock_score = 30
            issues.append('Very low stock (%d units)' % int(stock))
        else:
            stock_score = 0
            issues.append('OUT OF STOCK')

        # Days of stock estimate
        dos = 999
        sales_7d = 0
        sales_30d = 0
        if product.odoo_product_id:
            today = fields.Date.today()
            sales_7d = sum(self.env['sale.order.line'].search([
                ('product_id', '=', product.odoo_product_id.id),
                ('order_id.state', 'in', ['sale', 'done']),
                ('order_id.date_order', '>=', today - timedelta(days=7)),
            ]).mapped('product_uom_qty'))
            sales_30d = sum(self.env['sale.order.line'].search([
                ('product_id', '=', product.odoo_product_id.id),
                ('order_id.state', 'in', ['sale', 'done']),
                ('order_id.date_order', '>=', today - timedelta(days=30)),
            ]).mapped('product_uom_qty'))
            daily_avg = sales_30d / 30.0 if sales_30d else 0
            if daily_avg > 0 and stock > 0:
                dos = int(stock / daily_avg)
                if dos < 7:
                    issues.append('Only %d days of stock remaining!' % dos)

        # ── 4. SALES VELOCITY (0-100) ──
        sales_velocity = 0
        if sales_30d > 100:
            sales_velocity = 100
        elif sales_30d > 50:
            sales_velocity = 80
        elif sales_30d > 20:
            sales_velocity = 60
        elif sales_30d > 5:
            sales_velocity = 40
        elif sales_30d > 0:
            sales_velocity = 20
        else:
            sales_velocity = 0
            if product.status == 'Active':
                issues.append('No sales in 30 days')

        # ── 5. REVIEW SCORE (0-100) ──
        review_sc = 50  # Neutral baseline
        latest_review = self.env['amazon.review.analysis'].search([
            ('product_id', '=', product.id),
        ], limit=1, order='generated_at desc')
        if latest_review:
            review_sc = int(latest_review.sentiment_score * 100)
        else:
            recs.append('Run AI Review Analysis for insights')

        # ── CALCULATE OVERALL SCORE ──
        weights = {
            'listing': 0.25,
            'pricing': 0.20,
            'stock': 0.25,
            'sales': 0.20,
            'review': 0.10,
        }
        overall = int(
            listing * weights['listing'] +
            pricing * weights['pricing'] +
            stock_score * weights['stock'] +
            sales_velocity * weights['sales'] +
            review_sc * weights['review']
        )

        # Generate AI recommendations
        if overall < 50:
            recs.insert(0, 'URGENT: Product needs immediate attention')
        if listing < 60:
            recs.append('Improve listing: add more images, bullet points, and keywords')
        if stock_score < 40:
            recs.append('Restock immediately — risk of stockout')
        if sales_velocity < 30 and product.status == 'Active':
            recs.append('Consider running PPC campaign or lowering price to boost sales')

        self.write({
            'health_score': max(0, min(100, overall)),
            'listing_score': max(0, min(100, listing)),
            'pricing_score': max(0, min(100, pricing)),
            'stock_score': max(0, min(100, stock_score)),
            'sales_score': max(0, min(100, sales_velocity)),
            'review_score': max(0, min(100, review_sc)),
            'days_of_stock': dos,
            'sales_last_7d': sales_7d,
            'sales_last_30d': sales_30d,
            'issues': json.dumps(issues, ensure_ascii=False),
            'recommendations': json.dumps(recs, ensure_ascii=False),
            'generated_at': fields.Datetime.now(),
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Health Score: %d/100 (%s)" % (self.health_score, self.health_grade),
                "message": "%d issues found" % len(issues),
                "type": "success" if overall >= 70 else ("warning" if overall >= 40 else "danger"),
                "sticky": overall < 40,
            },
        }

    @api.model
    def calculate_all_health_scores(self, instance_id):
        """Calculate health scores for all active products in an instance."""
        products = self.env['amazon.product'].search([
            ('instance_id', '=', instance_id),
            ('status', '=', 'Active'),
        ])
        for product in products:
            existing = self.search([('product_id', '=', product.id)], limit=1)
            if not existing:
                existing = self.create({'product_id': product.id})
            try:
                existing.action_calculate_health()
            except Exception as exc:
                _logger.warning("Health calc failed for %s: %s", product.sku, exc)
        return len(products)
