"""Dashboard controller — KPI + chart data with instance & date filtering."""
import logging
from datetime import date, datetime, timedelta

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class AmazonDashboardController(http.Controller):

    @http.route('/amazon/dashboard/instances', type='jsonrpc', auth='user')
    def get_instances(self):
        instances = request.env['amazon.instance'].search([('active', '=', True)])
        return {'instances': [{'id': i.id, 'name': i.name} for i in instances]}

    @http.route('/amazon/dashboard/data', type='jsonrpc', auth='user')
    def dashboard_data(self, instance_id=None, date_range='30', custom_from=None, custom_to=None):
        env = request.env
        today = date.today()

        if date_range == 'custom' and custom_from and custom_to:
            start_date = custom_from
            end_date = custom_to
            days = (datetime.strptime(custom_to, '%Y-%m-%d').date() - datetime.strptime(custom_from, '%Y-%m-%d').date()).days
        else:
            days = int(date_range or 30)
            start_date = (today - timedelta(days=days)).isoformat()
            end_date = (today + timedelta(days=1)).isoformat()

        od = []
        pd2 = []
        if instance_id:
            od = [('instance_id', '=', int(instance_id))]
            pd2 = [('instance_id', '=', int(instance_id))]

        Order = env['amazon.sale.order']
        Product = env['amazon.product']

        period_orders = Order.search(od + [('purchase_date', '>=', start_date), ('purchase_date', '<=', end_date)])
        orders_today = Order.search_count(od + [('purchase_date', '>=', today.isoformat())])
        total_orders = Order.search_count(od)

        revenue_period = sum(period_orders.mapped('order_total'))
        avg_order = revenue_period / len(period_orders) if period_orders else 0

        pending_orders = Order.search_count(od + [('order_status', 'in', ['Pending', 'Unshipped'])])
        shipped_orders = Order.search_count(od + [('order_status', '=', 'Shipped')])
        cancelled_orders = Order.search_count(od + [('order_status', '=', 'Canceled')])

        total_products = Product.search_count(pd2 + [('status', '=', 'Active')])
        low_stock = Product.search_count(pd2 + [('amazon_qty', '<', 10), ('status', '=', 'Active')])
        out_of_stock = Product.search_count(pd2 + [('amazon_qty', '<=', 0), ('status', '=', 'Active')])

        active_alerts = env['amazon.smart.alert'].search_count([('state', 'in', ['new', 'acknowledged'])])
        critical_alerts = env['amazon.smart.alert'].search_count([('state', 'in', ['new', 'acknowledged']), ('severity', '=', '4_critical')])

        partners = period_orders.mapped('partner_id')
        total_customers = len(set(partners.ids))

        fbm_count = Order.search_count(od + [('fulfillment_channel', '=', 'MFN')])
        fba_count = Order.search_count(od + [('fulfillment_channel', '=', 'AFN')])

        avg_health = 0
        healths = env['amazon.product.health'].search(pd2)
        if healths:
            avg_health = round(sum(healths.mapped('health_score')) / len(healths))

        daily_sales = []
        for i in range(min(days, 90)):
            d = today - timedelta(days=days - 1 - i)
            day_orders = Order.search(od + [
                ('purchase_date', '>=', d.isoformat()),
                ('purchase_date', '<', (d + timedelta(days=1)).isoformat()),
            ])
            daily_sales.append({'date': d.isoformat(), 'orders': len(day_orders), 'revenue': sum(day_orders.mapped('order_total'))})

        status_dist = {}
        for s in ['Pending', 'Unshipped', 'Shipped', 'Canceled']:
            c = Order.search_count(od + [('order_status', '=', s), ('purchase_date', '>=', start_date)])
            if c:
                status_dist[s] = c

        product_sales = {}
        for order in period_orders:
            for line in order.order_line_ids:
                name = line.title or line.sku or 'Unknown'
                product_sales[name] = product_sales.get(name, 0) + line.quantity
        top_products = [{'name': p[0][:25], 'qty': p[1]} for p in sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:10]]

        weeks = min(days // 7 + 1, 12)
        weekly_revenue = []
        for w in range(weeks):
            ws = today - timedelta(weeks=weeks - 1 - w, days=today.weekday())
            we = ws + timedelta(days=7)
            wk_orders = Order.search(od + [('purchase_date', '>=', ws.isoformat()), ('purchase_date', '<', we.isoformat())])
            weekly_revenue.append({'week': ws.strftime('%d/%m'), 'revenue': sum(wk_orders.mapped('order_total')), 'orders': len(wk_orders)})

        recent_syncs = []
        SyncLog = env['amazon.sync.log']
        sync_domain = []
        if instance_id:
            sync_domain = [('instance_id', '=', int(instance_id))]
        for log in SyncLog.search(sync_domain, limit=10, order='create_date desc'):
            recent_syncs.append({
                'operation': dict(SyncLog._fields['operation'].selection).get(log.operation, log.operation),
                'state': log.state,
                'summary': (log.summary or '')[:60],
                'date': log.create_date.strftime('%Y-%m-%d %H:%M') if log.create_date else '',
            })

        return {
            'kpis': {
                'orders_today': orders_today, 'orders_period': len(period_orders),
                'total_orders': total_orders, 'revenue_period': round(revenue_period, 2),
                'avg_order_value': round(avg_order, 2),
                'pending_orders': pending_orders, 'shipped_orders': shipped_orders,
                'cancelled_orders': cancelled_orders,
                'total_products': total_products, 'low_stock': low_stock, 'out_of_stock': out_of_stock,
                'total_customers': total_customers, 'active_alerts': active_alerts,
                'critical_alerts': critical_alerts, 'avg_health': avg_health,
                'fbm_count': fbm_count, 'fba_count': fba_count,
            },
            'charts': {
                'daily_sales': daily_sales, 'status_distribution': status_dist,
                'top_products': top_products, 'weekly_revenue': weekly_revenue,
            },
            'recent_syncs': recent_syncs,
        }

    @http.route('/amazon/dashboard/ai-insights', type='jsonrpc', auth='user')
    def ai_insights(self, instance_id=None):
        env = request.env
        if instance_id:
            instance = env['amazon.instance'].browse(int(instance_id)).exists()
        else:
            instance = env['amazon.instance'].search([('active', '=', True)], limit=1)
        if not instance or not instance.ai_api_key:
            return {'insights': 'AI not configured. Go to Configuration > Instances.'}

        data = self.dashboard_data(instance_id=instance.id)
        k = data['kpis']

        from odoo.addons.sdlc_amazon_connector.services.ai_service import AmazonAIService
        prompt = (
            "Amazon seller analyst. Data:\n"
            "Orders: %d (today %d) | Revenue: %s | Avg: %s\n"
            "Pending: %d | Shipped: %d | Cancelled: %d\n"
            "Products: %d | Low Stock: %d | OOS: %d | Health: %d/100\n"
            "Alerts: %d critical | FBM: %d FBA: %d | Customers: %d\n"
            "Give: 1) Summary 2) 3 Actions 3) Growth Tip 4) Risk"
        ) % (k['orders_period'], k['orders_today'], k['revenue_period'], k['avg_order_value'],
             k['pending_orders'], k['shipped_orders'], k['cancelled_orders'],
             k['total_products'], k['low_stock'], k['out_of_stock'], k['avg_health'],
             k['critical_alerts'], k['fbm_count'], k['fba_count'], k['total_customers'])

        try:
            r = AmazonAIService._call_provider(instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model, prompt)
            return {'insights': r}
        except Exception as e:
            return {'insights': 'AI Error: %s' % str(e)[:200]}

    @http.route('/amazon/dashboard/optimize-store', type='jsonrpc', auth='user')
    def optimize_store(self, instance_id=None):
        env = request.env
        if instance_id:
            inst = env['amazon.instance'].browse(int(instance_id)).exists()
        else:
            inst = env['amazon.instance'].search([('active', '=', True)], limit=1)
        if not inst:
            return {'error': 'No instance.', 'actions': []}
        actions = []
        try:
            c = env['amazon.smart.alert'].run_alert_scan(inst.id)
            actions.append('Alerts: %d found' % c)
        except Exception as e:
            actions.append('Alerts failed: %s' % str(e)[:50])
        try:
            c = env['amazon.product.health'].calculate_all_health_scores(inst.id)
            actions.append('Health: %d products scored' % c)
        except Exception as e:
            actions.append('Health failed: %s' % str(e)[:50])
        return {'actions': actions, 'message': 'Optimization complete!'}
