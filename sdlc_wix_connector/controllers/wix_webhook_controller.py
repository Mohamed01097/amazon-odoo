"""Public endpoint receiving Wix order webhooks.

The controller does the minimum amount of work that must happen inline:
verify the signature, then persist the payload as a queued job and return 200.
All order creation happens later in ``wix.order.import.job``, driven by cron,
so Wix never waits on Odoo business logic and a transient failure cannot cause
a webhook to be lost.
"""

import json
import logging

from odoo import http
from odoo.http import request

from ..models.wix_api import WixSignatureError, extract_event_payload, verify_wix_jwt

_logger = logging.getLogger(__name__)

# Wix order events this endpoint accepts. Anything else is acknowledged but
# not queued, so adding a subscription in the Wix dashboard cannot silently
# create unexpected sale orders.
SUPPORTED_EVENT_SLUGS = frozenset({'created', 'updated', 'canceled'})
SUPPORTED_ENTITY_FQDN = 'wix.ecom.v1.order'


class WixWebhookController(http.Controller):

    @http.route(
        '/wix/webhook/order',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def wix_order_webhook(self, **kwargs):
        """Accept one Wix order webhook delivery."""
        raw_body = request.httprequest.get_data(as_text=True)

        # The connection is resolved by trying each active connection's key.
        # Wix does not include an unauthenticated site identifier we could
        # trust before verification, so the key that validates the signature
        # is what identifies the sender.
        instance, claims = self._verify_and_resolve_instance(raw_body)
        if instance is None:
            _logger.warning(
                "Rejected Wix webhook: no active connection's public key validated the signature."
            )
            return self._respond(401, 'invalid_signature')

        log = request.env['wix.sync.log'].sudo().log_start(
            instance, 'order_webhook_received', request_data=claims,
        )

        try:
            event = extract_event_payload(claims)
            entity_fqdn = event.get('entityFqdn') or (event.get('data') or {}).get('entityFqdn')
            slug = event.get('slug') or (event.get('data') or {}).get('slug')

            if entity_fqdn and entity_fqdn != SUPPORTED_ENTITY_FQDN:
                log.log_success(f"Ignored unsupported entity {entity_fqdn}.")
                return self._respond(200, 'ignored')
            if slug and slug not in SUPPORTED_EVENT_SLUGS:
                log.log_success(f"Ignored unsupported event slug {slug}.")
                return self._respond(200, 'ignored')

            order_id, event_id = self._extract_identifiers(event)
            if not order_id:
                log.log_fail("Webhook contained no order id.", error_category='payload')
                return self._respond(200, 'ignored_no_order_id')

            queued = self._queue_job(
                instance, claims, order_id, event_id, slug or 'created',
            )
            log.log_success(
                f"Queued {slug} job {queued.id} for Wix order {order_id}."
                if queued
                else f"Wix event for order {order_id} was already queued."
            )
            return self._respond(200, 'queued')
        except Exception:  # noqa: BLE001 - never leak internals to the caller
            _logger.exception("Failed to queue a verified Wix webhook.")
            log.log_fail("Failed to queue the verified webhook.", error_category='unexpected')
            # 500 asks Wix to retry delivery; the payload was verified but not stored.
            return self._respond(500, 'queue_failed')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _verify_and_resolve_instance(self, raw_body):
        """Return the ``(instance, claims)`` whose key signed this payload."""
        instances = request.env['wix.instance'].sudo().search([('active', '=', True)])
        for instance in instances:
            if not instance.webhook_public_key:
                continue
            try:
                claims = verify_wix_jwt(raw_body, instance.webhook_public_key)
            except WixSignatureError:
                continue
            return instance, claims
        return None, None

    def _extract_identifiers(self, event):
        """Return the order id and the per-event id, across all envelope shapes."""
        data = event.get('data') or {}
        entity = request.env['wix.order.import.job']._extract_entity(event)
        order_id = (
            event.get('entityId')
            or data.get('entityId')
            or entity.get('id')
        )
        event_id = event.get('id') or data.get('id')
        return order_id, event_id

    def _queue_job(self, instance, claims, order_id, event_id, slug):
        """Create the job, tolerating a concurrent duplicate delivery.

        Deduplication is on Wix's event id, which Wix documents as the value
        to deduplicate on. An order legitimately produces several events, so
        keying on the order instead would drop every update after the first.
        """
        Job = request.env['wix.order.import.job'].sudo()
        dedupe_domain = [('instance_id', '=', instance.id)]
        if event_id:
            dedupe_domain.append(('wix_event_id', '=', event_id))
        else:
            # Without an event id the only safe guard is the order plus the
            # event type, so a redelivery cannot be told apart from a genuine
            # repeat of the same transition.
            dedupe_domain += [
                ('wix_order_id', '=', order_id), ('event_slug', '=', slug),
            ]
        if Job.search(dedupe_domain, limit=1):
            return False

        try:
            with request.env.cr.savepoint():
                return Job.create({
                    'instance_id': instance.id,
                    'payload_json': json.dumps(claims),
                    'wix_order_id': order_id,
                    'wix_event_id': event_id,
                    'event_slug': slug,
                })
        except Exception:
            # The unique constraint fired: another worker queued the same
            # event first, which is the desired idempotent outcome.
            if Job.search_count(dedupe_domain):
                return False
            raise

    def _respond(self, status, result):
        return request.make_json_response({'result': result}, status=status)
