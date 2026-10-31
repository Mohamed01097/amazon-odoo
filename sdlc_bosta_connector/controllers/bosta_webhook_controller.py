"""Public endpoint receiving Bosta shipment-status callbacks.

Bosta posts a JSON body on every status change (never on creation) and echoes
back whichever custom header was registered with the shipment, which is what
authenticates the call - there is no signature scheme. See
https://docs.bosta.co/docs/how-to/get-delivery-status-via-webhook/.

The controller verifies, stores, and returns. Applying the status to a
delivery happens in ``bosta.tracking.event.job`` on cron.
"""

import hmac
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class BostaWebhookController(http.Controller):

    @http.route(
        '/bosta/webhook/status',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def bosta_status_webhook(self, **kwargs):
        payload = request.get_json_data() if request.httprequest.data else None
        if not isinstance(payload, dict):
            _logger.warning("Rejected Bosta webhook: body is not a JSON object.")
            return self._respond(400, 'invalid_payload')

        instance = self._authenticate()
        if instance is None:
            _logger.warning(
                "Rejected Bosta webhook: no account matched the request's auth header."
            )
            return self._respond(401, 'unauthorized')

        log = request.env['bosta.sync.log'].sudo().log_start(
            instance, 'webhook_received', request_data=payload,
        )
        try:
            job = request.env['bosta.tracking.event.job'].sudo().queue_event(
                instance, payload,
            )
            log.log_success(f"Queued tracking event job {job.id}.")
            return self._respond(200, 'queued')
        except Exception:  # noqa: BLE001 - never leak internals to the caller
            _logger.exception("Failed to queue a verified Bosta webhook.")
            log.log_fail("Failed to queue the verified webhook.")
            # 500 lets Bosta retry; the event was verified but not stored.
            return self._respond(500, 'queue_failed')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _authenticate(self):
        """Return the account whose configured header matches this request.

        An account with no secret configured is never matched: that would make
        the endpoint accept anonymous callbacks.
        """
        instances = request.env['bosta.instance'].sudo().search([('active', '=', True)])
        for instance in instances:
            expected = (instance.webhook_secret or '').strip()
            if not expected:
                continue
            header_name = (instance.webhook_auth_header_name or 'Authorization').strip()
            received = request.httprequest.headers.get(header_name) or ''
            # Constant-time compare so a wrong secret cannot be guessed by timing.
            if hmac.compare_digest(received.strip(), expected):
                return instance
        return None

    def _respond(self, status, result):
        return request.make_json_response({'result': result}, status=status)
