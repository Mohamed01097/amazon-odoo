"""Durable queue that books a Bosta shipment for a validated delivery.

Odoo's own convention is to call the carrier synchronously while the picking is
being validated (``stock_delivery`` does this from
``_send_confirmation_email``). This module deliberately does not: a Bosta
outage would then block warehouse staff from validating stock, and a request
that timed out after Bosta had already created the shipment would leave no
record of it.

Instead ``stock.picking._action_done`` enqueues a job here and the cron books
the shipment with bounded retries. The booking is guarded by a row lock so two
cron workers can never create two shipments for the same picking.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .bosta_api import (
    DELIVERY_TYPE_SEND,
    BostaAPI,
    BostaError,
)

_logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 5
RETRY_BACKOFF_BASE_MINUTES = 3


class BostaShipmentJob(models.Model):
    _name = 'bosta.shipment.job'
    _description = 'Bosta Shipment Booking Job'
    _order = 'create_date desc, id desc'

    picking_id = fields.Many2one(
        'stock.picking', string='Delivery', required=True, index=True,
        ondelete='cascade',
    )
    instance_id = fields.Many2one(
        'bosta.instance', string='Bosta Account', required=True, index=True,
        ondelete='restrict',
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)

    state = fields.Selection([
        ('draft', 'Queued'),
        ('done', 'Booked'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)

    bosta_delivery_id = fields.Char('Bosta Delivery ID', readonly=True, index=True)
    tracking_number = fields.Char('Tracking Number', readonly=True, index=True)

    request_payload_json = fields.Text(readonly=True)
    response_payload_json = fields.Text(
        readonly=True,
        help="Raw Bosta response, stored verbatim so a booking can be audited "
             "or re-read after a schema correction.",
    )

    next_run_at = fields.Datetime(index=True)
    retry_count = fields.Integer(default=0)
    last_error_message = fields.Text(readonly=True)
    sync_log_id = fields.Many2one('bosta.sync.log', readonly=True)

    _picking_unique = models.Constraint(
        'UNIQUE (picking_id)',
        'A Bosta shipment job already exists for this delivery.',
    )

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def cron_process_shipment_jobs(self, limit=10):
        """Book queued shipments.

        ``FOR UPDATE SKIP LOCKED`` lets several workers drain the queue without
        two of them claiming the same job. The cron transaction is the batch
        transaction; no explicit commit is used.
        """
        now = fields.Datetime.now()
        # The claim below is raw SQL, so pending ORM writes must reach the
        # database first or a just-queued job would be invisible to it.
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id
              FROM bosta_shipment_job
             WHERE state = 'draft'
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, create_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
        """, [now, limit])
        job_ids = [row[0] for row in self.env.cr.fetchall()]
        if not job_ids:
            return False

        for job in self.browse(job_ids):
            try:
                with self.env.cr.savepoint():
                    job._process_job()
            except BostaError as exc:
                _logger.warning("Bosta shipment job %s failed: %s", job.id, exc)
                job._mark_failed(str(exc), retryable=exc.retryable)
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                _logger.exception("Bosta shipment job %s failed", job.id)
                job._mark_failed(str(exc), retryable=False)
        return True

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------

    def _process_job(self):
        self.ensure_one()
        picking = self.picking_id
        instance = self.instance_id

        # Never book twice for the same picking, even if a previous attempt
        # crashed after Bosta had accepted the request.
        if self.tracking_number or picking.carrier_tracking_ref:
            self._mark_done(
                self.bosta_delivery_id,
                self.tracking_number or picking.carrier_tracking_ref,
            )
            return

        if picking.state != 'done':
            self._defer(_("The delivery is no longer validated."))
            return

        # Booking creates a real shipment that a courier will collect, so it
        # stays behind an explicit per-account gate.
        if not instance.shipment_booking_enabled:
            self._defer(_(
                "Shipment booking is disabled for Bosta account %s.", instance.name,
            ))
            return

        payload = self._build_payload()
        log = self.env['bosta.sync.log'].log_start(
            instance, 'create_shipment', request_data=payload, source=self,
        )
        self.sudo().write({
            'sync_log_id': log.id,
            'request_payload_json': self.env['bosta.sync.log']._serialize(payload),
        })

        api = BostaAPI(instance._get_api_base_url(), instance._get_api_key())
        try:
            data = api.create_delivery(payload)
        except BostaError as exc:
            log.log_fail(
                str(exc), http_status=exc.status_code,
                retryable=exc.retryable, response_data=exc.payload,
            )
            raise

        tracking_number = data.get('trackingNumber')
        delivery_id = data.get('_id')
        self.sudo().write({
            'response_payload_json': self.env['bosta.sync.log']._serialize(data),
        })
        self._mark_done(delivery_id, tracking_number)
        log.log_success(
            _("Booked Bosta shipment %s.", tracking_number), response_data=data,
        )

    def _build_payload(self):
        """Assemble the create-delivery request for this picking."""
        self.ensure_one()
        picking = self.picking_id
        instance = self.instance_id
        partner = picking.partner_id

        if not partner:
            raise BostaError("The delivery has no customer address.")
        # Odoo 19 merged `mobile` into `phone` on res.partner.
        phone = (partner.phone or '').strip()
        if not phone:
            raise BostaError(
                f"Customer {partner.display_name} has no phone number, which "
                f"Bosta requires for a shipment."
            )

        first_name, _sep, last_name = (partner.name or '').partition(' ')
        payload = {
            'type': DELIVERY_TYPE_SEND,
            'businessReference': picking.name,
            'receiver': {
                'firstName': first_name or partner.name or _("Customer"),
                'lastName': last_name or first_name or '-',
                'phone': phone,
            },
            'dropOffAddress': self._build_drop_off_address(),
            'specs': BostaAPI.build_specs(
                items_count=self._count_items(),
                description=picking.name,
                weight=picking.shipping_weight or picking.weight,
            ),
            'cod': self._compute_cod(),
        }
        if partner.email:
            payload['receiver']['email'] = partner.email
        if picking.note:
            payload['notes'] = str(picking.note)[:500]

        # Registering the webhook per order keeps status reporting working
        # without relying on a dashboard setting being present.
        webhook_url = instance._get_webhook_url()
        if webhook_url:
            payload['webhookUrl'] = webhook_url
            headers = instance._get_webhook_headers()
            if headers:
                payload['webhookCustomHeaders'] = headers
        return payload

    def _build_drop_off_address(self):
        """Map the Odoo delivery address onto Bosta's address fields.

        Bosta needs a city from its own catalogue, not free text, so the
        address is resolved against the cached catalogue. An unresolved city
        fails the booking with an actionable message: sending a guessed city
        code would ship the parcel to the wrong governorate.
        """
        self.ensure_one()
        partner = self.picking_id.partner_id
        street = (partner.street or '').strip()
        if not street:
            raise BostaError(
                f"Customer {partner.display_name} has no street address."
            )

        city, zone, district = partner._resolve_bosta_location(self.instance_id)
        if not city:
            raise BostaError(
                f"The city on {partner.display_name}'s address "
                f"({partner.city or 'empty'}) does not match any city in "
                f"Bosta's catalogue. Set the Bosta City on the contact, or "
                f"refresh the catalogue on the Bosta account."
            )
        if not city.code:
            raise BostaError(
                f"Bosta city {city.name} has no code cached, so it cannot be "
                f"sent. Refresh the Bosta address catalogue."
            )

        address = {'firstLine': street, 'city': city.code}
        if partner.street2:
            address['secondLine'] = partner.street2.strip()
        if zone:
            address['zone'] = zone.name
        if district:
            address['district'] = district.name
        if partner.zip:
            address['postalCode'] = partner.zip.strip()
        return address

    def _count_items(self):
        self.ensure_one()
        return int(sum(self.picking_id.move_ids.mapped('quantity')) or 1)

    def _compute_cod(self):
        """Amount Bosta must collect from the customer on delivery.

        Only unpaid amounts are collected: an order already settled online must
        never be charged again at the door.
        """
        self.ensure_one()
        if not self.instance_id.default_cod_enabled:
            return 0
        order = self.picking_id.sale_id
        if not order:
            return 0
        residual = sum(
            invoice.amount_residual
            for invoice in order.invoice_ids
            if invoice.state == 'posted' and invoice.move_type == 'out_invoice'
        )
        if order.invoice_ids and not residual:
            return 0
        return round(residual or order.amount_total, 2)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _mark_done(self, delivery_id, tracking_number):
        self.ensure_one()
        self.sudo().write({
            'state': 'done',
            'bosta_delivery_id': delivery_id or False,
            'tracking_number': tracking_number or False,
            'next_run_at': False,
            'last_error_message': False,
        })
        # Mirror onto the picking: carrier_tracking_ref is the field core Odoo
        # and the portal read for tracking.
        self.picking_id.sudo().write({
            'carrier_tracking_ref': tracking_number or False,
            'bosta_delivery_id': delivery_id or False,
            'bosta_shipment_state': 'booked',
        })

    def _defer(self, reason):
        """Retry later without consuming a retry attempt."""
        self.ensure_one()
        self.sudo().write({
            'next_run_at': fields.Datetime.now() + timedelta(minutes=15),
            'last_error_message': reason,
        })

    def _mark_failed(self, error_message, retryable=True):
        """Record a failure, backing off until the retry cap is reached."""
        self.ensure_one()
        retry_count = self.retry_count + 1
        vals = {'retry_count': retry_count, 'last_error_message': error_message}

        if not retryable or retry_count >= MAX_RETRY_COUNT:
            vals.update(state='failed', next_run_at=False)
        else:
            delay = RETRY_BACKOFF_BASE_MINUTES ** min(retry_count, 4)
            vals['next_run_at'] = fields.Datetime.now() + timedelta(minutes=delay)
        self.sudo().write(vals)

        if vals.get('state') == 'failed':
            # Make the failure visible where the warehouse works, rather than
            # leaving a delivery that silently has no shipment.
            self.picking_id.sudo().write({'bosta_shipment_state': 'failed'})
            self.picking_id.message_post(body=_(
                "Booking a Bosta shipment failed and will not be retried "
                "automatically: %s", error_message,
            ))

    def action_retry(self):
        """Re-queue a failed job for another attempt."""
        for job in self:
            if job.state != 'failed':
                continue
            job.sudo().write({
                'state': 'draft',
                'retry_count': 0,
                'next_run_at': False,
                'last_error_message': False,
            })
            job.picking_id.sudo().write({'bosta_shipment_state': 'queued'})
        return True
