{
    'name': 'Wix Connector',
    'version': '19.0.5.0.0',
    'summary': 'Import Wix storefront orders into Odoo as standard sale orders',
    'description': """
        Wix Connector
        =============
        Receives Wix order webhooks and creates confirmed Odoo sale orders from
        them. Order fulfillment, delivery, invoicing and payment follow Odoo's
        standard sale/stock/account flow unmodified. Shipping carrier assignment
        (e.g. Bosta) is done through Odoo's normal delivery-method selection on
        the sale order, and can be set automatically per connection via a
        configured default; this module has no dependency on any specific
        carrier module (Bosta or otherwise), only on core `delivery`.
    """,
    'author': 'SDLC Corp',
    'maintainer': 'SDLC Corp',
    'category': 'Sales',
    'license': 'OPL-1',
    'depends': [
        'sale_management',
        'stock',
        'delivery',
        'contacts',
        'mail',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/wix_instance_views.xml',
        'views/wix_order_import_job_views.xml',
        'views/wix_sync_log_views.xml',
        'views/sale_order_views.xml',
        'views/menu.xml',
        'data/cron.xml',
    ],
    'external_dependencies': {
        'python': ['cryptography'],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
