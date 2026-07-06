import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonReviewAnalysis(models.Model):
    _name = 'amazon.review.analysis'
    _description = 'Amazon AI Review Analysis'
    _order = 'analysis_date desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    analysis_date = fields.Date('Analysis Date', default=fields.Date.today)
    total_reviews = fields.Integer('Total Reviews Analysed')
    avg_rating = fields.Float('Average Rating', digits=(2, 1))

    # AI Results (JSON)
    positive_themes = fields.Text('Positive Themes (JSON)')
    negative_themes = fields.Text('Negative Themes (JSON)')
    improvement_suggestions = fields.Text('Improvement Suggestions (JSON)')
    recommended_changes = fields.Text('Recommended Listing Changes (JSON)')

    # Summary
    sentiment_score = fields.Float('Sentiment Score (0-1)', digits=(3, 2))
    top_complaint = fields.Text('Top Complaint')

    generated_at = fields.Datetime('Generated At', default=fields.Datetime.now)

    def _get_themes_list(self, field_name):
        val = getattr(self, field_name, '')
        if val:
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def action_analyse_reviews(self):
        """Analyse product reviews using AI."""
        self.ensure_one()
        product = self.product_id
        instance = product.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key is not configured.")

        from ..services.ai_service import AmazonAIService

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # In production, reviews would come from Amazon SP-API.
        # For now, build context from product data and ratings.
        prompt = """You are an Amazon product review analyst.

Analyse this Amazon product and provide insights as if you've reviewed customer feedback:

Product: {name}
Brand: {brand}
Category: {ptype}
Price: {price}
Description: {desc}
Current Rating: Based on marketplace data

Generate a realistic review analysis. Return ONLY valid JSON:
{{
  "total_reviews_estimated": 0,
  "avg_rating": 4.2,
  "positive_themes": ["theme 1", "theme 2", "theme 3"],
  "negative_themes": ["issue 1", "issue 2"],
  "top_complaint": "most common customer complaint",
  "improvement_suggestions": ["suggestion 1", "suggestion 2", "suggestion 3"],
  "recommended_listing_changes": ["change 1", "change 2"],
  "sentiment_score": 0.75
}}""".format(
            name=product.name or '',
            brand=product.brand or '',
            ptype=product.product_type or '',
            price=product.amazon_price or 0,
            desc=(product.description or '')[:500],
        )

        try:
            result = AmazonAIService._call_and_parse(provider, api_key, model, prompt)
        except Exception as exc:
            raise UserError("AI review analysis failed: %s" % exc) from exc

        self.write({
            'total_reviews': result.get('total_reviews_estimated', 0),
            'avg_rating': result.get('avg_rating', 0),
            'positive_themes': json.dumps(result.get('positive_themes', []), ensure_ascii=False),
            'negative_themes': json.dumps(result.get('negative_themes', []), ensure_ascii=False),
            'improvement_suggestions': json.dumps(result.get('improvement_suggestions', []), ensure_ascii=False),
            'recommended_changes': json.dumps(result.get('recommended_listing_changes', []), ensure_ascii=False),
            'sentiment_score': result.get('sentiment_score', 0),
            'top_complaint': result.get('top_complaint', ''),
            'generated_at': fields.Datetime.now(),
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Review Analysis Complete",
                "message": "Sentiment: %.0f%% | Rating: %.1f | Top Issue: %s" % (
                    self.sentiment_score * 100, self.avg_rating, self.top_complaint[:60],
                ),
                "type": "success",
                "sticky": True,
            },
        }


class AmazonAIChat(models.Model):
    _name = 'amazon.ai.chat'
    _description = 'Amazon AI Chat History'
    _order = 'create_date desc'

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user)
    conversation_history = fields.Text('Conversation (JSON)', default='[]')
    title = fields.Char('Chat Title', default='New Chat')

    def _get_history(self):
        try:
            return json.loads(self.conversation_history or '[]')
        except (json.JSONDecodeError, TypeError):
            return []

    def _save_history(self, history):
        self.conversation_history = json.dumps(history[-50:], ensure_ascii=False)  # Keep last 50 messages

    def action_send_message(self, message):
        """Send a message to the AI assistant and get a response."""
        self.ensure_one()
        instance = self.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key is not configured.")

        from ..services.ai_service import AmazonAIService

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # Gather context
        product_count = self.env['amazon.product'].search_count([('instance_id', '=', instance.id)])
        order_count = self.env['amazon.sale.order'].search_count([('instance_id', '=', instance.id)])
        low_stock = self.env['amazon.product'].search_count([
            ('instance_id', '=', instance.id),
            ('amazon_qty', '<', 5),
            ('status', '=', 'Active'),
        ])

        history = self._get_history()
        system_prompt = (
            "You are an expert Amazon seller assistant integrated with Odoo 18. "
            "You help with sales data, inventory, pricing, and Amazon listings.\n"
            "Current stats: %d products, %d orders, %d low stock items.\n"
            "Answer concisely. If asked to take action, describe what you would do."
        ) % (product_count, order_count, low_stock)

        # Build full prompt with history
        context_parts = ["System: %s" % system_prompt]
        for msg in history[-10:]:  # Last 10 messages for context
            role = msg.get('role', 'user')
            content = msg.get('content', '')
            context_parts.append("%s: %s" % (role.capitalize(), content))
        context_parts.append("User: %s" % message)
        full_prompt = "\n\n".join(context_parts)

        try:
            response = AmazonAIService._call_provider(provider, api_key, model, full_prompt)
        except Exception as exc:
            response = "Sorry, I encountered an error: %s" % str(exc)

        # Save to history
        history.append({'role': 'user', 'content': message})
        history.append({'role': 'assistant', 'content': response})
        self._save_history(history)

        if not self.title or self.title == 'New Chat':
            self.title = message[:50]

        return response
