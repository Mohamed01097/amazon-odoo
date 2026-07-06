"""
Centralized AI Service for Amazon Connector.

Supports OpenAI, Anthropic (Claude), and Google Gemini.
Provides:
  1. generate_listing()       - SEO optimized title, bullets, description
  2. detect_product_type()    - Smart Amazon productType detection
  3. optimize_price()         - AI pricing suggestion
  4. predict_inventory()      - Demand prediction & reorder suggestion
  5. generate_customer_reply() - Auto-reply for buyer messages
  6. analyze_and_fix_error()  - AI error analysis + fix suggestion
"""

import json
import logging

import requests

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────

LISTING_PROMPT = """You are an Amazon Seller Central product listing expert. Generate a COMPLETE listing filling EVERY field.

Product: {product_name}
Category: {category}
{extra_context}

Return ONLY valid JSON. Fill ALL fields with realistic values. Do NOT skip any field.

{{
  "name": "SEO title max 200 chars, front-load keywords, include brand + key attributes",
  "brand": "brand name",
  "product_type": "UPPER_SNAKE_CASE e.g. SHOES, SHIRT, LAPTOP",
  "description": "2-3 paragraph compelling product description with use cases",
  "bullet_points": ["BENEFIT 1 - detail", "BENEFIT 2 - detail", "BENEFIT 3", "BENEFIT 4", "BENEFIT 5"],
  "search_terms": "backend keywords comma-separated max 250 chars, no brand, include synonyms",
  "manufacturer": "manufacturer name",
  "model_name": "model name",
  "model_number": "model/part number e.g. MX-2024",
  "color": "specific color e.g. Midnight Black",
  "color_map": "standard color: Red, Blue, Black, White, Green, Brown, Grey, etc.",
  "size": "size value e.g. 9, M, L, 42",
  "material": "primary material e.g. Genuine Leather, Cotton, Polyester",
  "target_gender": "Male or Female or Unisex",
  "age_range": "Adult",
  "item_type_name": "specific type e.g. Running Shoes, T-Shirt, Laptop",
  "country_of_origin": "IN",
  "occasion": "usage occasion e.g. Casual, Sports, Party, Office",
  "special_features": "comma-separated: Breathable, Lightweight, Anti-Slip",
  "lifestyle": "e.g. Casual, Active, Ethnic",
  "pattern": "e.g. Solid, Striped, Printed",
  "style": "e.g. Casual, Formal, Ethnic",
  "seasons": "e.g. All Season, Summer, Winter",
  "specific_uses": "e.g. Running, Daily Wear, Office",
  "subject_character": "",
  "theme": "",
  "condition_type": "new_new",

  "amazon_price": 1999.0,
  "mrp": 2999.0,
  "sale_price": 1799.0,
  "amazon_qty": 100,
  "handling_time": 2,

  "item_weight": 0.8,
  "item_length": 32.0,
  "item_width": 22.0,
  "item_height": 12.0,
  "package_weight": 1.0,
  "package_length": 35.0,
  "package_width": 25.0,
  "package_height": 15.0,

  "manufacturer_contact": "Brand Name Pvt Ltd, Industrial Area, City, State, India - 110001, Ph: +91-11-12345678",
  "importer_contact": "Importer Name Pvt Ltd, Trade Center, City, State, India - 110001, Ph: +91-11-87654321",
  "packer_contact": "Packer Name Pvt Ltd, Warehouse Zone, City, State, India - 110001, Ph: +91-11-11223344",
  "part_number": "part number",
  "external_info_entity": "HSN",
  "external_info_value": "6-8 digit HSN code for this product",

  "water_resistance_level": "Not Water Resistant or Water Resistant or Waterproof",
  "lining_description": "lining material if applicable",
  "closure_type": "closure type e.g. Lace-Up, Slip-On, Zip, Button",
  "embellishment_feature": "if applicable",
  "sport_type": "if applicable e.g. Running, Cricket",
  "unit_count": 1,
  "unit_count_type": "Count or Pair or Set",

  {category_fields}
}}

Rules:
- CRITICAL AMAZON PRICING: MRP must be GREATER than amazon_price. amazon_price must be GREATER than sale_price. Example: mrp=2999, amazon_price=1999, sale_price=1799. NEVER set amazon_price >= mrp.
- ALL prices in {currency} with realistic market values.
- HSN code must be a valid 6-8 digit code for this product category (India).
- Importer/Packer/Manufacturer contact MUST be realistic Indian business addresses with full format: "Company Name, Address, City, State, India - PIN, Ph: +91-XX-XXXXXXXX"
- Dimensions in centimeters, weight in kilograms.
- Barcode: generate a realistic 13-digit EAN number.
- Fill EVERY field with a realistic value. No empty strings for important fields.
- For footwear: footwear_size_system must be "EU" or "UK" or "US". footwear_size must be a number like "8" or "42".
- Return ONLY the JSON object."""

# Category-specific field templates injected into the prompt
CATEGORY_FIELDS = {
    'footwear': """
  "footwear_size_system": "EU or US or UK",
  "footwear_age_group": "Adult or Big Kid or Little Kid",
  "footwear_gender": "Men or Women or Unisex",
  "footwear_size_class": "Regular or Wide or Narrow",
  "footwear_width": "Medium or Wide or Narrow",
  "footwear_size": "size number e.g. 8, 42",
  "sole_material": "sole material e.g. Rubber, EVA",
  "toe_style": "toe style e.g. Round Toe, Pointed",
  "heel_height": 2.5,
  "heel_height_unit": "inches or centimeters",
  "heel_type": "heel type e.g. Flat, Block, Stiletto",
  "shoe_type": "shoe type e.g. Running, Casual, Formal",
  "closure_type": "closure e.g. Lace-Up, Slip-On, Velcro",
  "height_map": "shaft height e.g. Ankle, Mid-Calf, Knee-High",
  "lining_description": "lining material e.g. Mesh, Fabric",
  "water_resistance_level": "Not Water Resistant or Water Resistant or Waterproof",
  "embellishment_feature": "embellishment if any"
""",
    'clothing': """
  "style": "style e.g. Casual, Formal, Ethnic, Streetwear",
  "pattern": "pattern e.g. Solid, Striped, Checkered, Floral",
  "closure_type": "closure e.g. Button, Zip, Pull-On",
  "seasons": "seasons e.g. Summer, Winter, All Season",
  "item_type_name": "garment type e.g. T-Shirt, Kurta, Jeans, Shirt",
  "fit_type": "REQUIRED fit e.g. Regular Fit, Slim Fit, Loose Fit",
  "department": "REQUIRED department e.g. Men, Women, Boys, Girls",
  "number_of_items": 1,
  "shirt_size": "REQUIRED size e.g. S, M, L, XL, XXL, 38, 40, 42",
  "care_instructions": "REQUIRED care e.g. Machine Wash, Hand Wash Only",
  "special_size_type": "REQUIRED e.g. Standard, Big & Tall, Petite",
  "sleeve_type": "REQUIRED sleeve e.g. Half Sleeve, Full Sleeve, Short Sleeve, Sleeveless",
  "unit_count_type": "Unit"
""",
    'electronics': """
  "special_features": "key features comma-separated e.g. Bluetooth 5.0, Fast Charging, IP67",
  "part_number": "manufacturer part number",
  "item_type_name": "device type e.g. Smartphone, Laptop, TWS Earbuds",
  "water_resistance_level": "IP rating if applicable",
  "unit_count": 1,
  "unit_count_type": "Count"
""",
    'beauty': """
  "item_type_name": "product type e.g. Face Cream, Shampoo, Lipstick",
  "special_features": "key claims e.g. Paraben-Free, Organic, SPF 50",
  "unit_count": 100,
  "unit_count_type": "Milliliter or Gram or Count",
  "specific_uses": "skin/hair concern e.g. Anti-Aging, Moisturizing, Dandruff",
  "lifestyle": "brand positioning e.g. Natural, Luxury, Ayurvedic"
""",
    'home': """
  "item_type_name": "item type e.g. Bedsheet, Pan, Lamp, Curtain",
  "style": "decor style e.g. Modern, Traditional, Minimalist",
  "pattern": "design e.g. Solid, Floral, Geometric",
  "special_features": "key features e.g. Non-Stick, Machine Washable, BPA-Free",
  "specific_uses": "room/use e.g. Bedroom, Kitchen, Bathroom",
  "unit_count": 1,
  "unit_count_type": "Set or Count or Piece"
""",
    'sports': """
  "sport_type": "sport e.g. Cricket, Football, Yoga, Running",
  "item_type_name": "equipment type e.g. Bat, Ball, Mat, Gloves",
  "special_features": "features e.g. Anti-Slip, Breathable, Shock-Absorbing",
  "specific_uses": "training type e.g. Training, Competition, Recreation",
  "water_resistance_level": "water resistance if applicable"
""",
    'jewelry': """
  "item_type_name": "jewelry type e.g. Ring, Necklace, Bracelet, Watch",
  "style": "design style e.g. Classic, Contemporary, Bohemian",
  "occasion": "wearing occasion e.g. Wedding, Daily Wear, Party",
  "special_features": "features e.g. Hypoallergenic, Adjustable, Tarnish-Free",
  "embellishment_feature": "stone/detail e.g. Diamond, Pearl, Cubic Zirconia",
  "closure_type": "clasp type e.g. Lobster Claw, Spring Ring, Hook"
""",
}

# Default for general/food/books/toys/automotive
CATEGORY_FIELDS_DEFAULT = """
  "unit_count": 1,
  "unit_count_type": "Count",
  "sport_type": "sport if applicable or empty string",
  "embellishment_feature": "embellishment if applicable or empty string",
  "closure_type": "closure if applicable or empty string"
"""

PRODUCT_TYPE_PROMPT = """You are an Amazon Seller Central expert.

Given this product info, determine the EXACT valid Amazon productType.

Product: {product_name}
Category: {category}
{extra_context}

Amazon uses specific productType values in UPPER_SNAKE_CASE for the Listings API.
Common examples: SHOES, SHIRT, PANTS, HANDBAG, WATCH, LAPTOP, PHONE, HOME_BED_AND_BATH,
KITCHEN, TOY, BEAUTY, GROCERY, LUGGAGE, BACKPACK, SPORTING_GOODS, etc.

Return ONLY valid JSON:
{{
  "product_type": "EXACT_PRODUCT_TYPE",
  "confidence": 0.95,
  "alternatives": ["ALT_TYPE_1", "ALT_TYPE_2"],
  "reasoning": "brief explanation of why this type was chosen"
}}"""

PRICE_OPTIMIZATION_PROMPT = """You are an Amazon pricing strategist.

Product: {product_name}
Category: {category}
Current Price: {current_price} {currency}
Cost Price: {cost_price} {currency}
Competitor Prices: {competitor_prices}
Current BSR (Best Seller Rank): {bsr}
{extra_context}

Analyze and suggest optimal pricing. Return ONLY valid JSON:
{{
  "suggested_price": 0.0,
  "min_price": 0.0,
  "max_price": 0.0,
  "confidence": 0.85,
  "strategy": "penetration/competitive/premium/value",
  "reasoning": "detailed explanation",
  "expected_margin_percent": 0.0,
  "price_elasticity_note": "how sensitive is demand to price changes",
  "recommendations": ["recommendation 1", "recommendation 2"]
}}"""

INVENTORY_PREDICTION_PROMPT = """You are a supply chain and inventory analyst.

Product: {product_name}
Current Stock: {current_stock}
Average Daily Sales (last 30 days): {avg_daily_sales}
Lead Time (days): {lead_time}
Sales History (weekly, last 12 weeks): {sales_history}
Seasonality Notes: {seasonality}
{extra_context}

Predict demand and suggest reorder quantities. Return ONLY valid JSON:
{{
  "predicted_daily_demand": 0.0,
  "days_of_stock_remaining": 0,
  "reorder_point": 0,
  "suggested_reorder_qty": 0,
  "safety_stock": 0,
  "stockout_risk": "low/medium/high",
  "reasoning": "explanation of prediction",
  "demand_trend": "increasing/stable/decreasing",
  "seasonal_factors": "any seasonal notes",
  "recommendations": ["rec 1", "rec 2"]
}}"""

CUSTOMER_REPLY_PROMPT = """You are a professional Amazon seller customer service representative.

Buyer Message: {buyer_message}
Product: {product_name}
Order Status: {order_status}
{extra_context}

Generate a professional, helpful reply that:
- Addresses the buyer's concern directly
- Is polite and empathetic
- Follows Amazon communication guidelines (no external links, no personal info)
- Offers a resolution
- Keeps under 2000 characters

Return ONLY valid JSON:
{{
  "reply": "the full reply text",
  "sentiment": "positive/neutral/negative/urgent",
  "category": "shipping/product_inquiry/return/complaint/other",
  "suggested_actions": ["action 1", "action 2"],
  "escalation_needed": false
}}"""

ERROR_FIX_PROMPT = """You are an Amazon SP-API integration expert and debugging specialist.

The following Amazon API call failed:

API Endpoint: {endpoint}
HTTP Method: {method}
HTTP Status: {status_code}
Error Response: {error_body}

Request Body (relevant fields):
{request_summary}

Product Info: {product_info}

Analyze this error and provide a fix. Return ONLY valid JSON:
{{
  "error_type": "validation/auth/throttle/server/unknown",
  "root_cause": "clear explanation of what went wrong",
  "fix_description": "what needs to change to fix this",
  "field_fixes": {{
    "field_name": "corrected_value"
  }},
  "retry_recommended": true,
  "retry_after_seconds": 0,
  "confidence": 0.9,
  "amazon_error_code": "the Amazon error code if present",
  "documentation_hint": "relevant Amazon docs section"
}}

Common Amazon Listings API errors:
- 4000003: Invalid productType
- 4000001: Missing required attribute
- 4000037: Invalid attribute value
- 8541: Brand not in Amazon Brand Registry
- 8572: GTIN already used by another seller
- 99010: Throttled (rate limit exceeded)"""


class AmazonAIService:
    """Stateless AI service — call class methods directly."""

    # ──────────────────────────────────────────────
    # Core API call
    # ──────────────────────────────────────────────

    # Provider → (base_url, default_model)
    OPENAI_COMPATIBLE = {
        'groq': ('https://api.groq.com/openai/v1/chat/completions', 'llama-3.3-70b-versatile'),
        'openai': ('https://api.openai.com/v1/chat/completions', 'gpt-4o-mini'),
    }

    @staticmethod
    def _call_provider(provider, api_key, model, prompt, temperature=0.3, max_tokens=2000):
        """Call AI provider and return the raw text response."""

        # ── Groq / OpenAI (same API format) ──
        if provider in ('groq', 'openai'):
            urls = {
                'groq': 'https://api.groq.com/openai/v1/chat/completions',
                'openai': 'https://api.openai.com/v1/chat/completions',
            }
            defaults = {
                'groq': 'llama-3.3-70b-versatile',
                'openai': 'gpt-4o-mini',
            }
            model = model or defaults.get(provider, 'llama-3.3-70b-versatile')
            url = urls.get(provider, urls['groq'])

            resp = requests.post(
                url,
                headers={'Authorization': 'Bearer %s' % api_key, 'Content-Type': 'application/json'},
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'temperature': temperature,
                    'max_tokens': max_tokens,
                },
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content']

        # ── Anthropic Claude ──
        elif provider == 'anthropic':
            model = model or 'claude-sonnet-4-20250514'
            resp = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'Content-Type': 'application/json',
                },
                json={
                    'model': model,
                    'max_tokens': max_tokens,
                    'messages': [{'role': 'user', 'content': prompt}],
                },
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()['content'][0]['text']

        # ── Google Gemini ──
        elif provider == 'gemini':
            model = model or 'gemini-2.0-flash'
            url = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s' % (model, api_key)
            resp = requests.post(
                url,
                headers={'Content-Type': 'application/json'},
                json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'temperature': temperature, 'maxOutputTokens': max_tokens},
                },
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()['candidates'][0]['content']['parts'][0]['text']

        else:
            raise ValueError("Unsupported AI provider: %s. Supported: groq, openai, anthropic, gemini" % provider)

    @staticmethod
    def _parse_json(text):
        """Extract JSON from AI response — handles markdown fences, extra text, etc."""
        if not text:
            return {}
        text = text.strip()

        # Remove markdown code fences
        if '```' in text:
            # Find content between first ``` and last ```
            parts = text.split('```')
            for part in parts[1:]:  # Skip text before first fence
                candidate = part.strip()
                # Remove language tag like "json"
                if candidate.lower().startswith('json'):
                    candidate = candidate[4:].strip()
                if candidate.startswith('{'):
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        continue

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Find first { ... last } (extract JSON object from surrounding text)
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace:last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        # Last resort: log and raise
        _logger.error("Failed to parse JSON from AI response: %s", text[:500])
        raise json.JSONDecodeError("No valid JSON found in AI response", text[:200], 0)

    @classmethod
    def _call_and_parse(cls, provider, api_key, model, prompt, **kwargs):
        """Call provider, parse JSON, return dict."""
        raw = cls._call_provider(provider, api_key, model, prompt, **kwargs)
        return cls._parse_json(raw)

    # ──────────────────────────────────────────────
    # 1. Product Listing Generator
    # ──────────────────────────────────────────────

    @classmethod
    def generate_listing(cls, provider, api_key, model, product_name,
                         brand='', product_type='', barcode='', currency='INR',
                         category_group='general'):
        """Generate SEO-optimized Amazon listing content with category-specific fields."""
        extra_lines = []
        if brand:
            extra_lines.append("Brand: %s" % brand)
        if product_type:
            extra_lines.append("Product Type: %s" % product_type)
        if barcode:
            extra_lines.append("Barcode: %s" % barcode)
        extra_context = "\n".join(extra_lines) if extra_lines else "No additional context."

        # Get category-specific fields for the prompt
        cat_fields = CATEGORY_FIELDS.get(category_group, CATEGORY_FIELDS_DEFAULT)

        prompt = LISTING_PROMPT.format(
            product_name=product_name,
            category=category_group or 'general',
            extra_context=extra_context,
            currency=currency,
            category_fields=cat_fields,
        )
        return cls._call_and_parse(provider, api_key, model, prompt)

    # ──────────────────────────────────────────────
    # 2. Smart Product Type Detection
    # ──────────────────────────────────────────────

    @classmethod
    def detect_product_type(cls, provider, api_key, model, product_name,
                            category='', brand='', description=''):
        """Detect the correct Amazon productType to avoid error 4000003."""
        extra_lines = []
        if brand:
            extra_lines.append("Brand: %s" % brand)
        if description:
            extra_lines.append("Description: %s" % description[:300])
        extra_context = "\n".join(extra_lines) if extra_lines else ""

        prompt = PRODUCT_TYPE_PROMPT.format(
            product_name=product_name,
            category=category or 'Unknown',
            extra_context=extra_context,
        )
        return cls._call_and_parse(provider, api_key, model, prompt)

    # ──────────────────────────────────────────────
    # 3. AI Price Optimization
    # ──────────────────────────────────────────────

    @classmethod
    def optimize_price(cls, provider, api_key, model, product_name,
                       category='', current_price=0, cost_price=0,
                       competitor_prices='', bsr='N/A', currency='INR'):
        """Suggest optimal pricing based on market analysis."""
        extra_lines = []
        if not competitor_prices:
            competitor_prices = 'Not available'
        extra_context = "\n".join(extra_lines) if extra_lines else ""

        prompt = PRICE_OPTIMIZATION_PROMPT.format(
            product_name=product_name,
            category=category or 'General',
            current_price=current_price,
            cost_price=cost_price,
            competitor_prices=competitor_prices,
            bsr=bsr,
            currency=currency,
            extra_context=extra_context,
        )
        return cls._call_and_parse(provider, api_key, model, prompt)

    # ──────────────────────────────────────────────
    # 4. AI Inventory Prediction
    # ──────────────────────────────────────────────

    @classmethod
    def predict_inventory(cls, provider, api_key, model, product_name,
                          current_stock=0, avg_daily_sales=0, lead_time=7,
                          sales_history='', seasonality=''):
        """Predict demand and suggest reorder quantities."""
        prompt = INVENTORY_PREDICTION_PROMPT.format(
            product_name=product_name,
            current_stock=current_stock,
            avg_daily_sales=avg_daily_sales,
            lead_time=lead_time,
            sales_history=sales_history or 'Not available',
            seasonality=seasonality or 'None noted',
            extra_context='',
        )
        return cls._call_and_parse(provider, api_key, model, prompt)

    # ──────────────────────────────────────────────
    # 5. Customer Support Auto-Reply
    # ──────────────────────────────────────────────

    @classmethod
    def generate_customer_reply(cls, provider, api_key, model,
                                buyer_message, product_name='',
                                order_status='', seller_name=''):
        """Generate professional reply for buyer messages."""
        extra_lines = []
        if seller_name:
            extra_lines.append("Seller Name: %s" % seller_name)
        extra_context = "\n".join(extra_lines) if extra_lines else ""

        prompt = CUSTOMER_REPLY_PROMPT.format(
            buyer_message=buyer_message,
            product_name=product_name or 'N/A',
            order_status=order_status or 'Unknown',
            extra_context=extra_context,
        )
        return cls._call_and_parse(provider, api_key, model, prompt)

    # ──────────────────────────────────────────────
    # 6. AI Error Analyzer & Fixer
    # ──────────────────────────────────────────────

    @classmethod
    def analyze_and_fix_error(cls, provider, api_key, model,
                              endpoint='', method='', status_code='',
                              error_body='', request_summary='',
                              product_info=''):
        """Analyze Amazon API error and suggest fixes."""
        prompt = ERROR_FIX_PROMPT.format(
            endpoint=endpoint or 'N/A',
            method=method or 'N/A',
            status_code=status_code or 'N/A',
            error_body=str(error_body)[:2000] if error_body else 'N/A',
            request_summary=str(request_summary)[:1500] if request_summary else 'N/A',
            product_info=str(product_info)[:500] if product_info else 'N/A',
        )
        return cls._call_and_parse(provider, api_key, model, prompt, temperature=0.2)
