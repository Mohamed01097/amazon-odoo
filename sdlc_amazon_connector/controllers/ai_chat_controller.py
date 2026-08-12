"""
AI Chat REST Controller — enables the floating chat widget and external API access.
POST /amazon/ai/chat → send message, get AI response with full Odoo context.
"""
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class AmazonAIChatController(http.Controller):

    @http.route('/amazon/ai/chat', type='jsonrpc', auth='user', methods=['POST'])
    def ai_chat(self, message='', config_id=None, chat_id=None):
        """
        Send a message to the Amazon AI assistant.

        :param message: User message string
        :param config_id: amazon.instance ID (optional — uses first active if not given)
        :param chat_id: amazon.ai.chat ID (optional — creates new if not given)
        :return: dict with response, chat_id, suggestions
        """
        if not message or not message.strip():
            return {'error': 'Message is required.'}

        env = request.env

        # Find or create instance
        if config_id:
            instance = env['amazon.instance'].browse(config_id).exists()
        else:
            instance = env['amazon.instance'].search([('active', '=', True)], limit=1)

        if not instance:
            return {'error': 'No Amazon instance configured.'}
        if not instance.ai_api_key:
            return {'error': 'AI API Key not configured on instance.'}

        # Find or create chat
        ChatModel = env['amazon.ai.chat']
        if chat_id:
            chat = ChatModel.browse(chat_id).exists()
            if not chat:
                chat = ChatModel.create({'instance_id': instance.id})
        else:
            chat = ChatModel.create({'instance_id': instance.id})

        # Get response
        try:
            response = chat.action_send_message(message)
        except Exception as exc:
            _logger.error("AI Chat error: %s", exc)
            return {'error': str(exc), 'chat_id': chat.id}

        # Generate quick action suggestions based on response
        suggestions = []
        lower_resp = (response or '').lower()
        if 'stock' in lower_resp or 'inventory' in lower_resp:
            suggestions.append({'label': 'Check Inventory', 'action': 'stock'})
        if 'price' in lower_resp or 'pricing' in lower_resp:
            suggestions.append({'label': 'AI Pricing', 'action': 'pricing'})
        if 'order' in lower_resp:
            suggestions.append({'label': 'View Orders', 'action': 'orders'})

        return {
            'response': response,
            'chat_id': chat.id,
            'suggestions': suggestions,
        }

    @http.route('/amazon/ai/chat/history', type='jsonrpc', auth='user', methods=['POST'])
    def ai_chat_history(self, chat_id=None):
        """Get chat history for an existing conversation."""
        if not chat_id:
            return {'error': 'chat_id required.'}

        chat = request.env['amazon.ai.chat'].browse(chat_id).exists()
        if not chat:
            return {'error': 'Chat not found.'}

        return {
            'chat_id': chat.id,
            'title': chat.title,
            'history': chat._get_history(),
        }

    @http.route('/amazon/ai/dashboard-stats', type='jsonrpc', auth='user', methods=['POST'])
    def dashboard_stats(self, instance_id=None):
        """Get real-time dashboard stats for the Amazon connector."""
        env = request.env
        domain = []
        if instance_id:
            domain = [('instance_id', '=', instance_id)]

        from datetime import date, timedelta
        today = date.today()

        # Orders
        order_model = env['amazon.sale.order']
        total_orders = order_model.search_count(domain)
        orders_today = order_model.search_count(domain + [('purchase_date', '>=', today.isoformat())])
        orders_week = order_model.search_count(domain + [('purchase_date', '>=', (today - timedelta(days=7)).isoformat())])

        # Revenue
        orders_month = order_model.search(domain + [('purchase_date', '>=', (today - timedelta(days=30)).isoformat())])
        revenue_month = sum(orders_month.mapped('order_total'))

        # Products
        prod_domain = [('instance_id.active', '=', True)] if not instance_id else [('instance_id', '=', instance_id)]
        total_products = env['amazon.product'].search_count(prod_domain + [('status', '=', 'Active')])
        low_stock = env['amazon.product'].search_count(prod_domain + [('amazon_qty', '<', 10), ('status', '=', 'Active')])

        # Alerts
        alert_domain = [('state', 'in', ['new', 'acknowledged'])]
        if instance_id:
            alert_domain.append(('instance_id', '=', instance_id))
        critical_alerts = env['amazon.smart.alert'].search_count(alert_domain + [('severity', '=', '4_critical')])
        total_alerts = env['amazon.smart.alert'].search_count(alert_domain)

        # AI
        pending_pricing = env['amazon.ai.pricing'].search_count([('status', '=', 'pending')])

        # Health
        avg_health = 0
        healths = env['amazon.product.health'].search(prod_domain)
        if healths:
            avg_health = sum(healths.mapped('health_score')) / len(healths)

        return {
            'total_orders': total_orders,
            'orders_today': orders_today,
            'orders_week': orders_week,
            'revenue_month': revenue_month,
            'total_products': total_products,
            'low_stock': low_stock,
            'critical_alerts': critical_alerts,
            'total_alerts': total_alerts,
            'pending_pricing': pending_pricing,
            'avg_health_score': round(avg_health),
        }
