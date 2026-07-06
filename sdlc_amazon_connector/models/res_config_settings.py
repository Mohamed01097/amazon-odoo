from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Amazon AI Settings (stored in ir.config_parameter)
    amazon_ai_enabled = fields.Boolean(
        'Enable AI Features',
        config_parameter='amazon_connector.ai_enabled',
        default=True,
    )
    amazon_ai_auto_pricing = fields.Boolean(
        'Auto AI Pricing',
        config_parameter='amazon_connector.ai_auto_pricing',
        help='Automatically generate AI pricing suggestions weekly.',
    )
    amazon_ai_auto_listing = fields.Boolean(
        'Auto AI Listing Optimisation',
        config_parameter='amazon_connector.ai_auto_listing',
        help='Automatically optimise new listings with AI.',
    )
    amazon_ai_auto_forecast = fields.Boolean(
        'Auto AI Demand Forecast',
        config_parameter='amazon_connector.ai_auto_forecast',
        help='Automatically run demand forecasts weekly.',
    )
    amazon_ai_review_analysis = fields.Boolean(
        'AI Review Analysis',
        config_parameter='amazon_connector.ai_review_analysis',
    )
    amazon_low_stock_threshold = fields.Integer(
        'Low Stock Alert Threshold',
        config_parameter='amazon_connector.low_stock_threshold',
        default=10,
    )
