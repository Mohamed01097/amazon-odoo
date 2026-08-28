{
    'name': 'Odoo Amazon Connector',
    'version': '19.0.10.1.0',
    'summary': 'Amazon SP-API Connector for Odoo with Orders, Products, FBA, Settlements, Inventory, Pricing, Returns, and AI Automation',
    'description': """
        Complete Amazon-Odoo connector with Amazon SP-API integration,
        order import, product sync, inventory sync, price sync, FBA management,
        settlement reconciliation, returns, refunds, and AI-powered automation.
        Full bidirectional Amazon-Odoo connector with AI-powered automation:

        CORE AMAZON FEATURES:
        - Order import (FBM + FBA) with auto SO creation & shipment tracking
        - Product catalog sync with CSV/Excel bulk mapping
        - Bidirectional stock sync (Odoo ↔ Amazon)
        - Bidirectional price sync (Odoo ↔ Amazon)
        - Settlement report processing & reconciliation
        - FBA inventory management (live stock, adjustments, shipment reports)
        - Inbound shipment management (v2024 API)
        - Removal order processing
        - Return & refund management with credit notes
        - Multi-Channel Fulfillment (MCF)
        - VCS tax report processing
        - Seller rating reports
        - Invoice upload to Amazon
        - Full automation with configurable scheduled actions

        AI-POWERED FEATURES (10 Features | 4 Providers | 3 Categories):

        Category 1 - Content & Listing Optimization (3 features):
          1. AI Listing Generator     — SEO-optimized title, bullets, description, keywords (model: amazon.ai.listing)
          2. Product Type Detection   — Smart Amazon product type classification, prevents error 4000003 (model: amazon.product)
          3. Review Analysis          — Sentiment scoring, positive/negative themes, improvement suggestions (model: amazon.review.analysis)

        Category 2 - Pricing & Profitability (3 features):
          4. AI Price Optimizer       — Competitor analysis, Buy Box strategy, 5 pricing modes (model: amazon.ai.pricing)
          5. Profit Calculator        — Per-unit & 30-day profit, ROI, margin analysis with AI insights (model: amazon.profit.calculator)
          6. Competitor Monitor       — Track competitor prices & market positioning (model: amazon.competitor.monitor)

        Category 3 - Forecasting & Intelligence (4 features):
          7. Demand Forecasting       — 30/60/90-day predictions, reorder points, stockout risk (model: amazon.demand.forecast)
          8. Product Health Score     — Overall 0-100 score (Grade A-F) combining 5 health dimensions (model: amazon.product.health)
          9. Smart Alert Engine      — 13 alert types: hijack, price war, suppression, stockout, etc. (model: amazon.smart.alert)
         10. AI Chat Assistant       — Floating widget with context-aware responses & dashboard stats (model: amazon.ai.chat)

        AI PROVIDERS SUPPORTED (4):
          - Groq (LLaMA 3.3 70B)       — Default, free tier available
          - OpenAI (GPT-4 / GPT-4o)    — Premium, best accuracy
          - Anthropic Claude (Sonnet 4) — Premium, best reasoning
          - Google Gemini (2.0 Flash)   — Value, fast & affordable

        TECHNICAL:
        - API retry with exponential backoff (429, 5xx)
        - Comprehensive sync logging for all operations
        - Login With Amazon (LWA) token authentication
        - Rate-limit aware request handling
        - 25+ marketplace support (NA, EU, Far East)
    """,
    'author': 'SDLC Corp',
    'maintainer': 'SDLC Corp',
    'website': 'https://sdlccorp.com/products/amazon-odoo-connector/',
    'support': 'sales@sdlccorp.com',
    'category': 'Sales',
    'license': 'OPL-1',
    'depends': [
        'sale_management',
        'stock',
        'contacts',
        'account',
        'mail',
        'purchase',
        'delivery',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/dashboard_view.xml',
        'views/instance_view.xml',
        'views/product_view.xml',
        'views/sale_order_view.xml',
        'views/order_import_job_view.xml',
        'views/order_status_sync_job_view.xml',
        'views/settlement_view.xml',
        'views/payout_view.xml',
        'views/return_view.xml',
        'views/removal_order_view.xml',
        'views/phase7_view.xml',
        'views/inbound_shipment_view.xml',
        'views/inventory_reconciliation_view.xml',
        'views/fba_inventory_view.xml',
        'views/rating_view.xml',
        'views/vcs_view.xml',
        'views/outbound_order_view.xml',
        'views/sync_log_view.xml',
        'views/delivery_view.xml',
        'views/ai_tools_view.xml',
        'views/ai_advanced_view.xml',
        'views/health_alerts_view.xml',
        'views/operations_view.xml',
        'views/wizard_view.xml',
        'views/user_guidance_view.xml',
        'views/menu.xml',
        'data/cron.xml',
        'data/sequence.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sdlc_amazon_connector/static/src/js/amazon_dashboard.js',
            'sdlc_amazon_connector/static/src/xml/amazon_dashboard.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
"images":["static/description/banner.gif"],
}
