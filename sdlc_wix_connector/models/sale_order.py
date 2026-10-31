"""Wix traceability on the Odoo sale order.

These fields record what Wix reports; they never drive accounting. Payment and
invoicing stay on Odoo's standard flow, so a Wix payment status is evidence to
look at, not an instruction to post anything.
"""

from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wix_instance_id = fields.Many2one(
        'wix.instance', string='Wix Connection', readonly=True, copy=False, index=True,
    )
    wix_order_id = fields.Char(
        'Wix Order ID', readonly=True, copy=False, index=True,
    )
    wix_order_number = fields.Char('Wix Order Number', readonly=True, copy=False)

    # Raw Wix enums, stored as text: Wix may add values, and an unknown value
    # must be visible rather than rejected or mapped onto a nearby one.
    wix_order_status = fields.Char('Wix Order Status', readonly=True, copy=False)
    wix_payment_status = fields.Char('Wix Payment Status', readonly=True, copy=False)
    wix_fulfillment_status = fields.Char(
        'Wix Fulfillment Status', readonly=True, copy=False,
    )
    wix_last_event_at = fields.Datetime('Last Wix Update', readonly=True, copy=False)
    wix_review_needed = fields.Boolean(
        'Needs Wix Review', readonly=True, copy=False, index=True,
        help="Set when Wix reported something this connector will not apply "
             "automatically, such as a cancellation of an order that is "
             "already delivered or invoiced.",
    )
