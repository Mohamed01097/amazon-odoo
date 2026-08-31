import logging
from odoo import models, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMoveInherit(models.Model):
    _inherit = 'account.move'

    amazon_instance_id = fields.Many2one('amazon.instance', string='Amazon Instance')
    amazon_order_ref = fields.Char('Amazon Order ID')
    amazon_invoice_number = fields.Char('Amazon Invoice Number')
    is_amazon_invoice = fields.Boolean('Amazon Invoice', default=False)
    amazon_invoice_uploaded = fields.Boolean('Uploaded to Amazon', default=False)

    def action_post(self):
        """Keep reimbursement financial ownership auditable after normal Odoo posting."""
        result = super().action_post()
        settlements = self.env['amazon.settlement.report'].sudo().search([
            ('account_move_id', 'in', self.ids),
        ])
        for settlement in settlements:
            settlement.line_ids.mapped('reimbursement_id').sudo().write({
                'financial_state': 'posted_later',
            })
        return result

    def action_upload_to_amazon(self):
        """Upload this invoice PDF to Amazon."""
        self.ensure_one()
        if not self.amazon_instance_id or not self.amazon_order_ref:
            raise UserError("This is not an Amazon invoice.")
        if self.state != 'posted':
            raise UserError("Only posted invoices can be uploaded to Amazon.")
        self.amazon_instance_id._upload_invoice_to_amazon_by_move(self)
        self.amazon_invoice_uploaded = True
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Invoice Uploaded",
                "message": "Invoice uploaded to Amazon for order %s" % self.amazon_order_ref,
                "type": "success",
                "sticky": False,
            },
        }
