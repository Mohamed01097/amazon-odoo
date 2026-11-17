"""Enqueue a Bosta booking when a Bosta-carrier delivery is validated."""

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    bosta_delivery_id = fields.Char('Bosta Delivery ID', readonly=True, copy=False)
    bosta_shipment_state = fields.Selection([
        ('queued', 'Queued'),
        ('booked', 'Booked'),
        ('in_transit', 'In Transit'),
        ('delivered', 'Delivered'),
        ('returned', 'Returned'),
        ('canceled', 'Canceled at Bosta'),
        ('exception', 'Exception'),
        ('failed', 'Booking Failed'),
    ], string='Bosta Shipment', readonly=True, copy=False, index=True)
    bosta_shipment_job_id = fields.Many2one(
        'bosta.shipment.job', string='Bosta Job', readonly=True, copy=False,
    )
    bosta_delivered_at = fields.Datetime(readonly=True, copy=False)
    bosta_returned_at = fields.Datetime(readonly=True, copy=False)
    bosta_return_picking_id = fields.Many2one(
        'stock.picking', string='Bosta Return Delivery', readonly=True, copy=False,
        help="Reverse picking created when Bosta reported the parcel as returned.",
    )

    def _action_done(self):
        """Queue a Bosta booking for each newly validated Bosta delivery.

        The API is never called here: the booking runs in
        ``bosta.shipment.job`` so a Bosta outage cannot block stock validation
        and a failed call can be retried without re-validating the picking.
        """
        res = super()._action_done()
        for picking in self:
            try:
                with self.env.cr.savepoint():
                    picking._enqueue_bosta_shipment(require_automatic=True)
            except Exception:  # noqa: BLE001 - must never break validation
                _logger.exception(
                    "Could not queue a Bosta shipment for picking %s", picking.name,
                )
        return res

    def _enqueue_bosta_shipment(self, require_automatic=False):
        self.ensure_one()
        if not self._should_book_bosta_shipment(require_automatic=require_automatic):
            return False

        job = self.env['bosta.shipment.job'].sudo().create({
            'picking_id': self.id,
            'instance_id': self.carrier_id.bosta_instance_id.id,
        })
        self.sudo().write({
            'bosta_shipment_job_id': job.id,
            'bosta_shipment_state': 'queued',
        })
        return job

    def _should_book_bosta_shipment(self, require_automatic=False):
        """Only outgoing Bosta deliveries that are not already booked.

        ``require_automatic`` additionally requires the Bosta account's
        dispatch mode to be "automatic": it is set by ``_action_done`` so a
        "manual" account is never queued without a user picking the delivery,
        and left off for the explicit "Send to Bosta" bulk action.
        """
        self.ensure_one()
        if self.state != 'done':
            return False
        if self.picking_type_id.code != 'outgoing':
            return False
        carrier = self.carrier_id
        if not carrier or carrier.delivery_type != 'bosta':
            return False
        instance = carrier.bosta_instance_id
        if not instance:
            return False
        if require_automatic and instance.dispatch_mode != 'automatic':
            return False
        if self.carrier_tracking_ref or self.bosta_delivery_id:
            return False
        if self.bosta_shipment_state in ('queued', 'booked'):
            return False
        if self.env['bosta.shipment.job'].sudo().search_count([
            ('picking_id', '=', self.id),
        ]):
            return False
        return True

    def action_send_to_bosta(self):
        """Bulk action: queue a Bosta booking for the selected deliveries.

        Used on "manual" dispatch accounts, where ``_action_done`` does not
        queue anything on its own. Each picking is checked independently so
        one ineligible row in the selection does not block the rest.
        """
        queued = self.browse()
        skipped = self.browse()
        for picking in self:
            job = picking._enqueue_bosta_shipment()
            if job:
                queued |= picking
            else:
                skipped |= picking

        if not queued:
            raise UserError(_(
                "None of the selected deliveries are eligible for Bosta "
                "booking (already booked, not a validated outgoing Bosta "
                "delivery, or missing a Bosta account)."
            ))

        message = _("Queued %(count)s deliveries for Bosta booking.",
                     count=len(queued))
        if skipped:
            message += ' ' + _(
                "%(count)s were skipped (already booked or not eligible): %(names)s.",
                count=len(skipped), names=', '.join(skipped.mapped('name')),
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Bosta Dispatch"),
                'message': message,
                'type': 'success' if not skipped else 'warning',
            },
        }

    def action_retry_bosta_shipment(self):
        """Re-queue a failed booking from the delivery form."""
        for picking in self:
            job = picking.bosta_shipment_job_id
            if job:
                job.action_retry()
            else:
                picking._enqueue_bosta_shipment()
        return True

    def action_open_bosta_tracking(self):
        self.ensure_one()
        url = self.carrier_id.get_tracking_link(self)
        if not url:
            return False
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}
