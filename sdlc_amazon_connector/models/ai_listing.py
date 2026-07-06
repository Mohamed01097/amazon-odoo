import json
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AmazonAIListing(models.Model):
    _name = 'amazon.ai.listing'
    _description = 'Amazon AI Listing Optimisation'
    _order = 'generated_at desc'
    _rec_name = 'product_id'

    product_id = fields.Many2one('amazon.product', required=True, ondelete='cascade', index=True)
    instance_id = fields.Many2one('amazon.instance', related='product_id.instance_id', store=True)

    # Original
    original_title = fields.Char('Original Title')
    original_description = fields.Text('Original Description')

    # Optimised
    optimised_title = fields.Char('Optimised Title')
    optimised_description = fields.Text('Optimised Description')
    bullet_points = fields.Text('Bullet Points (JSON)', help='JSON array of 5 bullet points')
    backend_keywords = fields.Text('Backend Keywords')
    search_terms = fields.Text('Search Terms')
    a_plus_suggestions = fields.Text('A+ Content Ideas')

    # Score
    seo_score = fields.Integer('SEO Score (0-100)', default=0)

    # Metadata
    generated_at = fields.Datetime('Generated At', default=fields.Datetime.now)
    applied = fields.Boolean('Applied to Amazon', default=False)
    applied_at = fields.Datetime('Applied At')

    def _get_bullet_points_list(self):
        """Return bullet points as a Python list."""
        if self.bullet_points:
            try:
                return json.loads(self.bullet_points)
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def action_optimise_with_ai(self):
        """Generate optimised listing content using AI."""
        self.ensure_one()
        product = self.product_id
        instance = product.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError("AI API Key is not configured.")

        from ..services.ai_service import AmazonAIService

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # Gather product data
        features = ''
        if product.odoo_product_id:
            attrs = product.odoo_product_id.product_template_attribute_value_ids
            if attrs:
                features = ', '.join(attrs.mapped('name'))

        prompt = """You are an Amazon SEO expert. Optimise this product listing for Amazon A9/A10 algorithm.

Product: {name}
Brand: {brand}
Category/Type: {ptype}
Current Title: {title}
Current Description: {desc}
Key Features: {features}
Price: {price}
Material: {material}
Color: {color}
Size: {size}

Return ONLY valid JSON:
{{
  "optimised_title": "max 200 chars, front-load top keywords, include brand + key attributes",
  "optimised_description": "compelling, SEO-rich product description (2-3 paragraphs)",
  "bullet_points": ["benefit 1 (max 255 chars, start with CAPITAL benefit word)", "benefit 2", "benefit 3", "benefit 4", "benefit 5"],
  "backend_keywords": "comma-separated, no brand, no ASIN, no repetition, max 250 bytes",
  "search_terms": "5 relevant long-tail search phrases separated by commas",
  "a_plus_content_idea": "suggestion for A+ content layout and messaging",
  "seo_score": 85
}}""".format(
            name=product.name or '',
            brand=product.brand or '',
            ptype=product.product_type or '',
            title=product.name or '',
            desc=product.description or '',
            features=features or 'N/A',
            price=product.amazon_price or 0,
            material=product.material or '',
            color=product.color or '',
            size=product.size or '',
        )

        try:
            result = AmazonAIService._call_and_parse(provider, api_key, model, prompt)
        except Exception as exc:
            raise UserError("AI listing optimisation failed: %s" % exc) from exc

        self.write({
            'original_title': product.name,
            'original_description': product.description,
            'optimised_title': result.get('optimised_title', ''),
            'optimised_description': result.get('optimised_description', ''),
            'bullet_points': json.dumps(result.get('bullet_points', []), ensure_ascii=False),
            'backend_keywords': result.get('backend_keywords', ''),
            'search_terms': result.get('search_terms', ''),
            'a_plus_suggestions': result.get('a_plus_content_idea', ''),
            'seo_score': result.get('seo_score', 0),
            'generated_at': fields.Datetime.now(),
            'applied': False,
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Listing Optimised",
                "message": "SEO Score: %d/100 — Review and apply." % self.seo_score,
                "type": "success",
                "sticky": False,
            },
        }

    def action_apply_to_amazon(self):
        """Push optimised listing content to Amazon."""
        self.ensure_one()
        if self.applied:
            raise UserError("Already applied.")
        if not self.optimised_title:
            raise UserError("No optimised content. Run AI optimisation first.")

        product = self.product_id

        # Update product fields
        updates = {}
        if self.optimised_title:
            updates['name'] = self.optimised_title
        if self.optimised_description:
            updates['description'] = self.optimised_description
        if self.backend_keywords:
            updates['search_terms'] = self.backend_keywords

        if updates:
            product.write(updates)

        # Update bullet points
        bp_list = self._get_bullet_points_list()
        if bp_list:
            product.bullet_point_ids.unlink()
            for i, bp in enumerate(bp_list[:10]):
                if bp and isinstance(bp, str):
                    self.env['amazon.product.bullet.point'].create({
                        'product_id': product.id,
                        'name': bp.strip(),
                        'sequence': (i + 1) * 10,
                    })

        # Push to Amazon
        try:
            product.action_update_in_amazon()
        except Exception as exc:
            _logger.warning("Failed to push listing to Amazon: %s", exc)

        self.applied = True
        self.applied_at = fields.Datetime.now()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Listing Applied",
                "message": "Optimised listing pushed to Amazon for %s" % product.sku,
                "type": "success",
            },
        }

    def action_generate_new(self):
        """Create a new AI listing record and run optimisation for a product."""
        product_id = self.env.context.get('default_product_id') or (self.product_id.id if self.product_id else False)
        if not product_id:
            raise UserError("No product selected.")

        rec = self.create({
            'product_id': product_id,
        })
        return rec.action_optimise_with_ai()
