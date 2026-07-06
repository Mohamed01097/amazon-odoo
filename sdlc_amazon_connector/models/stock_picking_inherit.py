import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class StockPickingInherit(models.Model):
    _inherit = 'stock.picking'

    amazon_instance_id = fields.Many2one('amazon.instance', string='Amazon Instance')
    amazon_order_ref = fields.Char('Amazon Order ID')
    is_amazon_delivery = fields.Boolean('Amazon Delivery', default=False)
    amazon_tracking_exported = fields.Boolean('Tracking Exported to Amazon', default=False)

    def action_export_tracking_to_amazon(self):
        """Send tracking info to Amazon for FBM orders."""
        self.ensure_one()
        from odoo.exceptions import UserError
        if not self.amazon_instance_id or not self.amazon_order_ref:
            raise UserError("This is not an Amazon delivery.")
        if not self.carrier_tracking_ref:
            raise UserError("No tracking number set on this delivery.")

        amazon_order = self.env['amazon.sale.order'].search([
            ('amazon_order_ref', '=', self.amazon_order_ref),
            ('instance_id', '=', self.amazon_instance_id.id),
        ], limit=1)
        if not amazon_order:
            raise UserError("Amazon order not found: %s" % self.amazon_order_ref)

        amazon_order.tracking_number = self.carrier_tracking_ref
        amazon_order.carrier_name = self.carrier_id.name if self.carrier_id else ''
        self.amazon_instance_id._confirm_order_shipment(amazon_order)
        self.amazon_tracking_exported = True

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Tracking Exported",
                "message": "Tracking info sent to Amazon for order %s" % self.amazon_order_ref,
                "type": "success",
                "sticky": False,
            },
        }

    def button_validate(self):
        """Override to auto-push tracking to Amazon and send delivery email."""
        res = super().button_validate()

        # After successful validation, handle Amazon-linked deliveries
        for picking in self:
            if picking.state != 'done':
                continue

            # Auto-link Amazon info from SO if not already linked
            if not picking.is_amazon_delivery and picking.sale_id and picking.sale_id.is_amazon_order:
                picking.write({
                    'is_amazon_delivery': True,
                    'amazon_instance_id': picking.sale_id.amazon_instance_id.id,
                    'amazon_order_ref': picking.sale_id.client_order_ref,
                })

            if not picking.is_amazon_delivery or not picking.amazon_order_ref:
                continue

            # Auto-push tracking to Amazon for FBM orders
            if (picking.carrier_tracking_ref
                    and not picking.amazon_tracking_exported
                    and picking.amazon_instance_id):
                try:
                    amazon_order = self.env['amazon.sale.order'].search([
                        ('amazon_order_ref', '=', picking.amazon_order_ref),
                        ('instance_id', '=', picking.amazon_instance_id.id),
                    ], limit=1)
                    if amazon_order and amazon_order.fulfillment_channel == 'MFN':
                        amazon_order.tracking_number = picking.carrier_tracking_ref
                        amazon_order.carrier_name = picking.carrier_id.name if picking.carrier_id else ''
                        picking.amazon_instance_id._confirm_order_shipment(amazon_order)
                        picking.amazon_tracking_exported = True
                        _logger.info(
                            "Auto-exported tracking %s to Amazon for order %s",
                            picking.carrier_tracking_ref, picking.amazon_order_ref,
                        )
                except Exception as exc:
                    _logger.warning(
                        "Failed to auto-export tracking for Amazon order %s: %s",
                        picking.amazon_order_ref, exc,
                    )

            # Send delivery confirmation email to customer
            picking._send_amazon_delivery_email()

        return res

    def _send_amazon_delivery_email(self):
        """Send delivery confirmation email to the customer."""
        self.ensure_one()
        if not self.is_amazon_delivery:
            return

        partner = self.partner_id or (self.sale_id and self.sale_id.partner_id)
        if not partner or not partner.email:
            _logger.info(
                "No email for Amazon order %s — skipping delivery notification.",
                self.amazon_order_ref,
            )
            return

        tracking_info = ''
        if self.carrier_tracking_ref:
            carrier_name = self.carrier_id.name if self.carrier_id else 'the carrier'
            tracking_info = (
                "<p><strong>Tracking Number:</strong> %s</p>"
                "<p><strong>Carrier:</strong> %s</p>"
            ) % (self.carrier_tracking_ref, carrier_name)

        # Build product list from move lines
        product_lines = []
        for move in self.move_ids.filtered(lambda m: m.state == 'done'):
            product_lines.append(
                "<li>%s — Qty: %s</li>" % (move.product_id.display_name, int(move.quantity))
            )
        products_html = "<ul>%s</ul>" % "".join(product_lines) if product_lines else ""

        subject = "Your order %s has been delivered!" % (self.amazon_order_ref or self.origin or self.name)
        body_html = """
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #232f3e;">Order Delivered</h2>
            <p>Dear %s,</p>
            <p>Great news! Your order <strong>%s</strong> has been shipped and is on its way.</p>
            %s
            %s
            <p>If you have any questions about your order, please don't hesitate to reach out.</p>
            <p>Thank you for your purchase!</p>
            <hr style="border: 1px solid #eee;"/>
            <p style="color: #888; font-size: 12px;">
                This is an automated notification. Please do not reply directly to this email.
            </p>
        </div>
        """ % (
            partner.name or 'Valued Customer',
            self.amazon_order_ref or self.origin or self.name,
            tracking_info,
            "<h3>Items Shipped:</h3>%s" % products_html if products_html else "",
        )

        try:
            mail_values = {
                'subject': subject,
                'body_html': body_html,
                'email_from': self.env.company.email or self.env.user.email,
                'email_to': partner.email,
                'auto_delete': True,
            }
            mail = self.env['mail.mail'].sudo().create(mail_values)
            mail.send()
            _logger.info(
                "Delivery confirmation email sent to %s for order %s",
                partner.email, self.amazon_order_ref,
            )
        except Exception as exc:
            _logger.warning(
                "Failed to send delivery email for order %s: %s",
                self.amazon_order_ref, exc,
            )
