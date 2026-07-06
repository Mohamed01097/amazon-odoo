import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class SaleOrderInherit(models.Model):
    _inherit = 'sale.order'

    amazon_order_id = fields.Many2one('amazon.sale.order', string='Amazon Order', copy=False)
    is_amazon_order = fields.Boolean('Is Amazon Order', default=False)
    amazon_instance_id = fields.Many2one('amazon.instance', string='Amazon Instance')
    amazon_fulfillment_channel = fields.Selection([
        ('MFN', 'FBM'),
        ('AFN', 'FBA'),
    ], string='Amazon Fulfillment')

    def action_confirm(self):
        """Override to tag outgoing pickings with Amazon info after confirmation."""
        res = super().action_confirm()
        for order in self:
            if order.is_amazon_order and order.amazon_instance_id:
                order._tag_amazon_pickings()
        return res

    def _tag_amazon_pickings(self):
        """Tag all delivery pickings with Amazon info for auto-tracking export."""
        self.ensure_one()
        if not self.is_amazon_order:
            return
        pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and not p.is_amazon_delivery
        )
        for picking in pickings:
            picking.write({
                'is_amazon_delivery': True,
                'amazon_instance_id': self.amazon_instance_id.id,
                'amazon_order_ref': self.client_order_ref or (self.amazon_order_id.amazon_order_ref if self.amazon_order_id else ''),
            })
            _logger.info(
                "Tagged picking %s as Amazon delivery for order %s",
                picking.name, self.client_order_ref,
            )
