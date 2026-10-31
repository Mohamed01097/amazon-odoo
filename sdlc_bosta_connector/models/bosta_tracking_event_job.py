"""Durable queue applying Bosta status callbacks to Odoo deliveries.

The webhook controller only verifies and stores; everything below runs on cron
so Bosta is never kept waiting and a failure cannot lose a callback.

Bosta does not send a webhook on shipment creation, and a callback can arrive
before this side has finished recording the booking, so an event whose
delivery cannot be matched yet is deferred rather than discarded.
"""

import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from . import bosta_state

_logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 5
RETRY_BACKOFF_BASE_MINUTES = 3
UNMATCHED_RETRY_MINUTES = 10


class BostaTrackingEventJob(models.Model):
    _name = 'bosta.tracking.event.job'
    _description = 'Bosta Tracking Event Job'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one(
        'bosta.instance', string='Bosta Account', required=True, index=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)

    state = fields.Selection([
        ('draft', 'Queued'),
        ('done', 'Applied'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)

    payload_json = fields.Text(
        required=True, readonly=True,
        help="Raw verified Bosta callback, stored verbatim so it can be "
             "re-applied after a mapping correction.",
    )

    tracking_number = fields.Char(index=True, readonly=True)
    bosta_delivery_id = fields.Char(index=True, readonly=True)
    bosta_state_code = fields.Integer(readonly=True)
    bosta_state_label = fields.Char(readonly=True)
    event_timestamp = fields.Char(readonly=True)

    picking_id = fields.Many2one('stock.picking', readonly=True, index=True)
    return_picking_id = fields.Many2one(
        'stock.picking', string='Return Delivery', readonly=True,
        help="Reverse picking created when Bosta reported the parcel coming back.",
    )

    next_run_at = fields.Datetime(index=True)
    retry_count = fields.Integer(default=0)
    last_error_message = fields.Text(readonly=True)
    sync_log_id = fields.Many2one('bosta.sync.log', readonly=True)

    # One row per (delivery, state, timestamp): a redelivered callback is a
    # no-op instead of applying the same transition twice.
    _event_unique = models.Constraint(
        'UNIQUE (bosta_delivery_id, bosta_state_code, event_timestamp)',
        'This Bosta status event has already been recorded.',
    )

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def cron_process_tracking_event_jobs(self, limit=50):
        """Apply queued status events."""
        now = fields.Datetime.now()
        # The claim below is raw SQL, so pending ORM writes must reach the
        # database first or a just-queued event would be invisible to it.
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id
              FROM bosta_tracking_event_job
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
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                _logger.exception("Bosta tracking event job %s failed", job.id)
                job._mark_failed(str(exc))
        return True

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process_job(self):
        self.ensure_one()
        picking = self._resolve_picking()
        if not picking:
            self._defer(
                _("No delivery matches this Bosta shipment yet."),
                minutes=UNMATCHED_RETRY_MINUTES,
            )
            return

        shipment_state = bosta_state.shipment_state_for(self.bosta_state_code)
        if not shipment_state:
            # An undocumented code must not be mapped onto a nearby state.
            self.sudo().write({
                'state': 'ignored',
                'picking_id': picking.id,
                'next_run_at': False,
                'last_error_message': _(
                    "Unrecognised Bosta state code %s; recorded without "
                    "changing the delivery.", self.bosta_state_code,
                ),
            })
            picking.message_post(body=_(
                "Bosta reported an unrecognised status code %(code)s. The "
                "delivery was left unchanged for review.",
                code=self.bosta_state_code,
            ))
            return

        vals = {'bosta_shipment_state': shipment_state}
        if shipment_state == 'delivered':
            vals['bosta_delivered_at'] = fields.Datetime.now()
        picking.sudo().write(vals)

        picking.message_post(body=_(
            "Bosta status: %(label)s (code %(code)s).",
            label=self.bosta_state_label or bosta_state.label_for(self.bosta_state_code),
            code=self.bosta_state_code,
        ))

        return_picking = False
        if bosta_state.is_return(self.bosta_state_code):
            return_picking = self._create_return_picking(picking)

        self.sudo().write({
            'state': 'done',
            'picking_id': picking.id,
            'return_picking_id': return_picking.id if return_picking else False,
            'next_run_at': False,
            'last_error_message': False,
        })

    def _resolve_picking(self):
        """Find the delivery this callback refers to.

        Matching prefers Bosta's own identifiers and only then falls back to
        the reference this module sent, so a customer-edited reference cannot
        redirect a status onto the wrong delivery.
        """
        self.ensure_one()
        Picking = self.env['stock.picking'].sudo()

        if self.bosta_delivery_id:
            picking = Picking.search([
                ('bosta_delivery_id', '=', self.bosta_delivery_id),
            ], limit=1)
            if picking:
                return picking
        if self.tracking_number:
            picking = Picking.search([
                ('carrier_tracking_ref', '=', self.tracking_number),
            ], limit=1)
            if picking:
                return picking

        reference = self._payload().get('businessReference')
        if reference:
            picking = Picking.search([
                ('name', '=', reference),
                ('carrier_id.delivery_type', '=', 'bosta'),
            ], limit=1)
            if picking:
                return picking
        return Picking.browse()

    def _create_return_picking(self, picking):
        """Create the reverse picking for a parcel coming back.

        Uses Odoo's own return wizard, which confirms and reserves the reverse
        picking but does **not** validate it. The parcel has not physically
        arrived yet, so receiving it stays a warehouse decision.
        """
        self.ensure_one()
        if picking.state != 'done':
            return False
        if picking.bosta_return_picking_id:
            return picking.bosta_return_picking_id

        wizard = self.env['stock.return.picking'].sudo().with_context(
            active_model='stock.picking', active_id=picking.id,
        ).create({'picking_id': picking.id})

        # The wizard computes its lines with quantity 0 because the UI expects
        # a human to type them. A refused parcel comes back in full, so mirror
        # what actually went out.
        for line in wizard.product_return_moves:
            line.quantity = line.move_id.quantity
        returnable = wizard.product_return_moves.filtered(lambda l: l.quantity > 0)
        if not returnable:
            _logger.warning(
                "Bosta reported picking %s as returned but it has no returnable "
                "quantity; no reverse picking was created.", picking.name,
            )
            return False
        (wizard.product_return_moves - returnable).unlink()

        new_picking = wizard._create_return()

        picking.sudo().write({
            'bosta_return_picking_id': new_picking.id,
            'bosta_returned_at': fields.Datetime.now(),
        })
        new_picking.message_post(body=_(
            "Created automatically because Bosta reported shipment %(tracking)s "
            "as %(label)s. Validate it once the parcel is physically received.",
            tracking=self.tracking_number or '-',
            label=self.bosta_state_label or bosta_state.label_for(self.bosta_state_code),
        ))
        picking.message_post(body=_(
            "Bosta returned this shipment. Reverse delivery %(name)s was "
            "created and is awaiting receipt.", name=new_picking.name,
        ))
        return new_picking

    def _payload(self):
        self.ensure_one()
        try:
            data = json.loads(self.payload_json or '{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _defer(self, reason, minutes=15):
        self.ensure_one()
        retry_count = self.retry_count + 1
        vals = {'retry_count': retry_count, 'last_error_message': reason}
        if retry_count >= MAX_RETRY_COUNT:
            # Stop retrying, but keep the payload: it can be replayed by hand.
            vals.update(state='failed', next_run_at=False)
        else:
            vals['next_run_at'] = fields.Datetime.now() + timedelta(minutes=minutes)
        self.sudo().write(vals)

    def _mark_failed(self, error_message):
        self.ensure_one()
        retry_count = self.retry_count + 1
        vals = {'retry_count': retry_count, 'last_error_message': error_message}
        if retry_count >= MAX_RETRY_COUNT:
            vals.update(state='failed', next_run_at=False)
        else:
            delay = RETRY_BACKOFF_BASE_MINUTES ** min(retry_count, 4)
            vals['next_run_at'] = fields.Datetime.now() + timedelta(minutes=delay)
        self.sudo().write(vals)

    def action_retry(self):
        for job in self:
            if job.state not in ('failed', 'ignored'):
                continue
            job.sudo().write({
                'state': 'draft',
                'retry_count': 0,
                'next_run_at': False,
                'last_error_message': False,
            })
        return True

    # ------------------------------------------------------------------
    # Polling fallback
    # ------------------------------------------------------------------

    @api.model
    def cron_reconcile_active_shipments(self, limit=25, stale_hours=12):
        """Poll Bosta for shipments that have gone quiet.

        The webhook is the primary mechanism; this exists so a missed or
        misconfigured callback cannot leave a shipment stuck forever.
        """
        from .bosta_api import BostaAPI, BostaError

        cutoff = fields.Datetime.now() - timedelta(hours=stale_hours)
        pickings = self.env['stock.picking'].sudo().search([
            ('carrier_id.delivery_type', '=', 'bosta'),
            ('carrier_tracking_ref', '!=', False),
            ('bosta_shipment_state', 'in', ('booked', 'in_transit', 'exception')),
            ('write_date', '<=', cutoff),
        ], limit=limit)
        if not pickings:
            return False

        for picking in pickings:
            instance = picking.carrier_id.bosta_instance_id
            if not instance:
                continue
            api = BostaAPI(instance._get_api_base_url(), instance._get_api_key())
            log = self.env['bosta.sync.log'].log_start(
                instance, 'tracking_poll',
                request_data={'trackingNumber': picking.carrier_tracking_ref},
            )
            try:
                with self.env.cr.savepoint():
                    result = api.get_delivery_tracking(picking.carrier_tracking_ref)
                    log.log_success(
                        _("Polled %s.", picking.carrier_tracking_ref),
                        response_data=result,
                    )
                    self._queue_from_poll(instance, picking, result)
            except BostaError as exc:
                log.log_fail(str(exc), http_status=exc.status_code,
                             retryable=exc.retryable, response_data=exc.payload)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Bosta poll failed for %s", picking.name)
                log.log_fail(str(exc))
        return True

    @api.model
    def _queue_from_poll(self, instance, picking, result):
        """Turn a polled state into an event row, reusing the webhook path."""
        data = result.get('data') if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return False
        state = data.get('state')
        code = state.get('code') if isinstance(state, dict) else state
        if code is None:
            return False

        payload = {
            '_id': picking.bosta_delivery_id or data.get('_id'),
            'trackingNumber': picking.carrier_tracking_ref,
            'state': code,
            'timeStamp': data.get('timeStamp') or 'poll',
            'businessReference': picking.name,
        }
        return self.sudo().queue_event(instance, payload, source='poll')

    # ------------------------------------------------------------------
    # Entry point shared by the controller and the poller
    # ------------------------------------------------------------------

    @api.model
    def queue_event(self, instance, payload, source='webhook'):
        """Store one verified status event, ignoring exact duplicates."""
        state_raw = payload.get('state')
        try:
            state_code = int(state_raw)
        except (TypeError, ValueError):
            state_code = 0

        delivery_id = payload.get('_id') or payload.get('deliveryId') or ''
        timestamp = str(payload.get('timeStamp') or payload.get('timestamp') or '')

        existing = self.sudo().search([
            ('bosta_delivery_id', '=', delivery_id or False),
            ('bosta_state_code', '=', state_code),
            ('event_timestamp', '=', timestamp),
        ], limit=1)
        if existing:
            return existing

        try:
            with self.env.cr.savepoint():
                return self.sudo().create({
                    'instance_id': instance.id,
                    'payload_json': json.dumps(payload),
                    'tracking_number': payload.get('trackingNumber') or False,
                    'bosta_delivery_id': delivery_id or False,
                    'bosta_state_code': state_code,
                    'bosta_state_label': bosta_state.label_for(state_code),
                    'event_timestamp': timestamp or False,
                })
        except Exception:
            # The unique constraint fired: a concurrent delivery of the same
            # event won, which is the desired idempotent outcome.
            duplicate = self.sudo().search([
                ('bosta_delivery_id', '=', delivery_id or False),
                ('bosta_state_code', '=', state_code),
                ('event_timestamp', '=', timestamp),
            ], limit=1)
            if duplicate:
                return duplicate
            raise
