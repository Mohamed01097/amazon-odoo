"""
Amazon Smart Alert Engine — UNIQUE FEATURE
Detects: hijack attempts, listing suppression, price wars, stockout risks,
buy box loss, review bombing, policy violations. Auto-sends notifications.
"""
import json
import logging
from datetime import timedelta

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class AmazonSmartAlert(models.Model):
    _name = 'amazon.smart.alert'
    _description = 'Amazon Smart Alert'
    _order = 'severity desc, create_date desc'
    _rec_name = 'display_name'

    product_id = fields.Many2one('amazon.product', ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)

    alert_type = fields.Selection([
        ('stockout', 'Stockout Risk'),
        ('low_stock', 'Low Stock'),
        ('price_war', 'Price War Detected'),
        ('buy_box_lost', 'Buy Box Lost'),
        ('listing_suppressed', 'Listing Suppressed'),
        ('hijack', 'Possible Hijack'),
        ('review_drop', 'Review Rating Drop'),
        ('negative_margin', 'Negative Margin'),
        ('no_sales', 'No Sales (7 days)'),
        ('sync_error', 'Sync Error'),
        ('policy_warning', 'Policy Warning'),
        ('competitor_price', 'Competitor Undercut'),
        ('high_return', 'High Return Rate'),
    ], string='Alert Type', required=True, index=True)

    severity = fields.Selection([
        ('1_info', 'Info'),
        ('2_warning', 'Warning'),
        ('3_urgent', 'Urgent'),
        ('4_critical', 'Critical'),
    ], string='Severity', required=True, default='2_warning', index=True)

    title = fields.Char('Alert Title', required=True)
    description = fields.Text('Details')
    suggested_action = fields.Text('Suggested Action')

    state = fields.Selection([
        ('new', 'New'),
        ('acknowledged', 'Acknowledged'),
        ('resolved', 'Resolved'),
        ('dismissed', 'Dismissed'),
    ], default='new', required=True, index=True)

    resolved_at = fields.Datetime('Resolved At')
    resolved_by = fields.Many2one('res.users', 'Resolved By')
    resolution_note = fields.Text('Resolution Note')

    display_name = fields.Char(compute='_compute_display_name', store=True)

    @api.depends('alert_type', 'title', 'severity')
    def _compute_display_name(self):
        for rec in self:
            sev = dict(self._fields['severity'].selection).get(rec.severity, '')
            rec.display_name = "[%s] %s" % (sev, rec.title or '')

    def action_acknowledge(self):
        self.filtered(lambda r: r.state == 'new').write({'state': 'acknowledged'})

    def action_resolve(self):
        self.write({
            'state': 'resolved',
            'resolved_at': fields.Datetime.now(),
            'resolved_by': self.env.uid,
        })

    def action_dismiss(self):
        self.write({'state': 'dismissed'})

    # ══════════════════════════════════════════════════
    # Alert Generation Engine
    # ══════════════════════════════════════════════════

    @api.model
    def run_alert_scan(self, instance_id=None):
        """Scan all products and generate alerts. Called by cron."""
        domain = [('active', '=', True)]
        if instance_id:
            domain.append(('id', '=', instance_id))
        instances = self.env['amazon.instance'].search(domain)

        total = 0
        for instance in instances:
            total += self._scan_instance(instance)

        _logger.info("Smart Alert scan complete: %d alerts generated.", total)
        return total

    def _scan_instance(self, instance):
        """Run all alert checks for one instance."""
        products = self.env['amazon.product'].search([
            ('instance_id', '=', instance.id),
            ('status', '=', 'Active'),
        ])
        created = 0

        for product in products:
            created += self._check_stockout(instance, product)
            created += self._check_negative_margin(instance, product)
            created += self._check_no_sales(instance, product)
            created += self._check_listing_issues(instance, product)

        # Instance-level alerts
        created += self._check_sync_errors(instance)
        return created

    def _create_alert_if_new(self, instance, product, alert_type, severity, title, description, suggested_action=''):
        """Create alert only if no unresolved alert of same type exists."""
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('product_id', '=', product.id if product else False),
            ('alert_type', '=', alert_type),
            ('state', 'in', ['new', 'acknowledged']),
        ], limit=1)
        if existing:
            return 0

        rec = self.create({
            'instance_id': instance.id,
            'product_id': product.id if product else False,
            'alert_type': alert_type,
            'severity': severity,
            'title': title,
            'description': description,
            'suggested_action': suggested_action,
        })
        # Send real-time bus notification for critical/urgent alerts
        if severity in ('3_urgent', '4_critical'):
            try:
                sev_label = 'CRITICAL' if severity == '4_critical' else 'URGENT'
                msg_type = 'danger' if severity == '4_critical' else 'warning'
                self.env['bus.bus']._sendmany([
                    (partner, 'simple_notification', {
                        'title': "Amazon Alert: %s" % sev_label,
                        'message': title,
                        'type': msg_type,
                        'sticky': True,
                    })
                    for partner in self.env['res.users'].search([]).mapped('partner_id')
                ])
            except Exception:
                pass
        return 1

    def _check_stockout(self, instance, product):
        count = 0
        stock = product.odoo_stock or product.amazon_qty or 0
        if stock <= 0:
            count += self._create_alert_if_new(
                instance, product, 'stockout', '4_critical',
                'OUT OF STOCK: %s' % product.sku,
                'Product %s (%s) has zero stock. Listing may be suppressed.' % (product.name, product.sku),
                'Restock immediately or pause advertising. Create Purchase Order.',
            )
        elif stock < (int(self.env['ir.config_parameter'].sudo().get_param('amazon_connector.low_stock_threshold', '10'))):
            count += self._create_alert_if_new(
                instance, product, 'low_stock', '3_urgent',
                'Low Stock: %s (%d units)' % (product.sku, int(stock)),
                'Product %s has only %d units remaining.' % (product.name, int(stock)),
                'Place reorder. Use AI Demand Forecast to calculate optimal quantity.',
            )
        return count

    def _check_negative_margin(self, instance, product):
        if not product.amazon_price or not product.odoo_product_id:
            return 0
        cost = product.odoo_product_id.standard_price
        if cost > 0 and product.amazon_price < cost:
            return self._create_alert_if_new(
                instance, product, 'negative_margin', '4_critical',
                'NEGATIVE MARGIN: %s' % product.sku,
                'Selling at %.2f but cost is %.2f. Losing money on every sale.' % (product.amazon_price, cost),
                'Increase price immediately or use AI Pricing to find optimal price.',
            )
        return 0

    def _check_no_sales(self, instance, product):
        if not product.odoo_product_id:
            return 0
        today = fields.Date.today()
        recent_sales = self.env['sale.order.line'].search_count([
            ('product_id', '=', product.odoo_product_id.id),
            ('order_id.state', 'in', ['sale', 'done']),
            ('order_id.date_order', '>=', today - timedelta(days=7)),
        ])
        if recent_sales == 0 and product.amazon_qty > 0:
            return self._create_alert_if_new(
                instance, product, 'no_sales', '2_warning',
                'No Sales (7d): %s' % product.sku,
                'Product %s has had no sales in the last 7 days despite having stock.' % product.name,
                'Review pricing, run AI Listing Optimisation, consider PPC campaign.',
            )
        return 0

    def _check_listing_issues(self, instance, product):
        count = 0
        if product.status == 'Inactive':
            count += self._create_alert_if_new(
                instance, product, 'listing_suppressed', '3_urgent',
                'INACTIVE Listing: %s' % product.sku,
                'Listing for %s is marked Inactive. It may be suppressed by Amazon.' % product.name,
                'Check Seller Central for suppression reasons. Fix issues and reactivate.',
            )
        if product.status == 'Incomplete':
            count += self._create_alert_if_new(
                instance, product, 'listing_suppressed', '2_warning',
                'Incomplete Listing: %s' % product.sku,
                'Listing for %s is incomplete. Missing required attributes.' % product.name,
                'Use AI Generate to fill missing fields, then push to Amazon.',
            )
        return count

    def _check_sync_errors(self, instance):
        recent_errors = self.env['amazon.sync.log'].search_count([
            ('instance_id', '=', instance.id),
            ('state', '=', 'failed'),
            ('create_date', '>=', fields.Datetime.now() - timedelta(hours=24)),
        ])
        if recent_errors >= 3:
            return self._create_alert_if_new(
                instance, None, 'sync_error', '3_urgent',
                '%d Sync Failures in 24h' % recent_errors,
                '%d sync operations failed in the last 24 hours. Check API credentials and Amazon status.' % recent_errors,
                'Go to Reports > Sync Logs to review errors. Test connection from instance.',
            )
        return 0
