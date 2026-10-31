{
    'name': 'Bosta Connector',
    'version': '19.0.2.0.0',
    'summary': 'Bosta last-mile delivery carrier for Odoo',
    'description': """
        Bosta Connector
        ===============
        Registers Bosta (Egyptian last-mile courier) as an Odoo delivery
        method, so it can be selected on any sale order through the standard
        shipping-method flow.

        This module is independent of any sales channel: it works for orders
        entered manually, imported from Wix, or created by any other module.

        Shipment creation against the live Bosta API and inbound tracking
        webhooks are delivered in later phases; this phase registers the
        carrier and its configuration only.
    """,
    'author': 'SDLC Corp',
    'maintainer': 'SDLC Corp',
    'category': 'Inventory/Delivery',
    'license': 'OPL-1',
    # stock_delivery is what defines the send_shipping / get_tracking_link /
    # cancel_shipment provider contract and the carrier_tracking_ref field.
    # Depending on bare `delivery` + `stock` would not expose them.
    'depends': [
        'stock_delivery',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/bosta_location_views.xml',
        'views/bosta_instance_views.xml',
        'views/bosta_shipment_job_views.xml',
        'views/bosta_tracking_event_job_views.xml',
        'views/delivery_carrier_views.xml',
        'views/stock_picking_views.xml',
        'views/menu.xml',
        'data/cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
