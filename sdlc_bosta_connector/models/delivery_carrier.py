"""Bosta as an Odoo delivery provider.

Odoo dispatches provider behaviour by convention: a carrier whose
``delivery_type`` is ``bosta`` gets its rate from ``bosta_rate_shipment`` and,
when shipping is requested, from ``bosta_send_shipping`` (see
``stock_delivery/models/delivery_carrier.py``).

This phase implements rating only. ``integration_level`` therefore defaults to
``'rate'`` for Bosta carriers, which stops Odoo from calling
``send_to_shipper()`` automatically while shipment creation does not exist yet.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(
        selection_add=[('bosta', 'Bosta')],
        ondelete={'bosta': 'set default'},
    )

    bosta_instance_id = fields.Many2one(
        'bosta.instance',
        string='Bosta Account',
        check_company=True,
        domain="[('active', '=', True), ('company_id', 'in', [company_id, False])]",
        help="Bosta connection used for shipments booked through this method.",
    )
    bosta_flat_rate = fields.Float(
        'Bosta Flat Rate',
        default=0.0,
        help="Shipping price charged for this method. Bosta's own rate-card "
             "API is not integrated yet, so the price is taken from here.",
    )
    bosta_tracking_url_template = fields.Char(
        'Tracking URL Template',
        default='https://bosta.co/tracking-shipment?tracking_number={tracking_number}',
        help="Customer-facing tracking URL. '{tracking_number}' is replaced "
             "with the shipment's tracking reference.",
    )

    @api.constrains('delivery_type', 'bosta_instance_id')
    def _check_bosta_instance_required(self):
        for carrier in self:
            if carrier.delivery_type == 'bosta' and not carrier.bosta_instance_id:
                raise ValidationError(_(
                    "Select a Bosta Account for delivery method %s.", carrier.name,
                ))

    @api.constrains('delivery_type', 'integration_level')
    def _check_bosta_integration_level(self):
        """Keep Bosta carriers on 'rate' so booking stays asynchronous.

        At ``rate_and_ship`` Odoo calls ``send_to_shipper()`` inline while the
        picking is being validated. This module books through
        ``bosta.shipment.job`` instead, so a Bosta outage cannot block stock
        validation and a failed call is retried without re-validating.
        """
        for carrier in self:
            if carrier.delivery_type == 'bosta' and carrier.integration_level != 'rate':
                raise ValidationError(_(
                    "Bosta delivery methods must keep Integration Level on "
                    "'Get Rate'. Shipments are booked asynchronously after the "
                    "delivery is validated, not during validation."
                ))

    @api.onchange('delivery_type')
    def _onchange_delivery_type_bosta(self):
        if self.delivery_type == 'bosta':
            self.integration_level = 'rate'

    # ------------------------------------------------------------------
    # Provider contract
    # ------------------------------------------------------------------

    def bosta_rate_shipment(self, order):
        """Return the configured flat rate for this Bosta method."""
        self.ensure_one()
        if not self.bosta_instance_id:
            return {
                'success': False,
                'price': 0.0,
                'error_message': _("No Bosta account is configured on this delivery method."),
                'warning_message': False,
            }
        return {
            'success': True,
            'price': self.bosta_flat_rate,
            'error_message': False,
            'warning_message': False,
        }

    def bosta_get_tracking_link(self, picking):
        """Return the customer-facing tracking URL for a booked shipment."""
        self.ensure_one()
        tracking_number = picking.carrier_tracking_ref
        if not tracking_number or not self.bosta_tracking_url_template:
            return None
        return self.bosta_tracking_url_template.replace(
            '{tracking_number}', tracking_number,
        )

    def bosta_send_shipping(self, pickings):
        """Book shipments synchronously, for the standard carrier contract.

        The automatic path is asynchronous (``bosta.shipment.job``); this
        satisfies callers that invoke the documented provider contract
        directly, such as a manual "book now" action.
        """
        self.ensure_one()
        Job = self.env['bosta.shipment.job']
        result = []
        for picking in pickings:
            job = Job.sudo().search([('picking_id', '=', picking.id)], limit=1)
            if not job:
                job = Job.sudo().create({
                    'picking_id': picking.id,
                    'instance_id': self.bosta_instance_id.id,
                })
            job._process_job()
            if not job.tracking_number:
                raise UserError(_(
                    "Bosta did not return a tracking number for %(picking)s: "
                    "%(error)s",
                    picking=picking.name,
                    error=job.last_error_message or _("unknown error"),
                ))
            result.append({
                'exact_price': self.bosta_flat_rate,
                'tracking_number': job.tracking_number,
            })
        return result

    def bosta_cancel_shipment(self, pickings):
        """Cancelling a booked Bosta shipment is not implemented.

        Raising keeps it visible: silently succeeding would leave a cancelled
        Odoo delivery with a live courier collection still scheduled.
        """
        raise UserError(_(
            "Cancelling a Bosta shipment from Odoo is not implemented. Cancel "
            "the shipment in the Bosta dashboard, then cancel the delivery here."
        ))
