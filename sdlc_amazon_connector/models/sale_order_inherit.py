import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class SaleOrderInherit(models.Model):
    _inherit = 'sale.order'

    amazon_order_id = fields.Many2one('amazon.sale.order', string='Amazon Order', copy=False)
    is_amazon_order = fields.Boolean('Is Amazon Order', default=False)
    amazon_instance_id = fields.Many2one('amazon.instance', string='Amazon Instance')
    amazon_order_ref = fields.Char(
        'Amazon Order ID',
        related='amazon_order_id.amazon_order_ref',
        store=True,
        readonly=True,
    )
    amazon_status = fields.Char(
        'Amazon Status',
        index=True,
        copy=False,
        help="Raw Amazon Orders API status. This is separate from Odoo's native Sale Order state.",
    )
    previous_amazon_status = fields.Char('Previous Amazon Status', copy=False)
    amazon_status_last_synced_at = fields.Datetime('Amazon Status Last Synced At', copy=False)
    amazon_last_update_date = fields.Datetime('Amazon Last Update Date', copy=False)
    amazon_status_sync_error = fields.Text('Amazon Status Sync Error', copy=False)
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

    def action_refresh_amazon_status(self):
        """Refresh the linked Amazon status without changing Amazon itself."""
        self.ensure_one()
        if not self.amazon_order_id:
            from odoo.exceptions import UserError
            raise UserError("This Sale Order is not linked to an Amazon order.")
        return self.amazon_order_id.action_refresh_status_from_amazon()


class SaleOrderLineInherit(models.Model):
    _inherit = 'sale.order.line'

    def _action_launch_stock_rule(self, *, previous_product_uom_qty=False):
        """Amazon owns AFN fulfillment; its durable item event owns stock."""
        afn_lines = self.filtered(lambda line: (
            line.order_id.is_amazon_order
            and line.order_id.amazon_fulfillment_channel == 'AFN'
        ))
        other_lines = self - afn_lines
        if other_lines:
            return super(SaleOrderLineInherit, other_lines)._action_launch_stock_rule(
                previous_product_uom_qty=previous_product_uom_qty,
            )
        return True
