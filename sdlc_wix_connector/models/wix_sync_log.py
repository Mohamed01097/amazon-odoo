import json
import logging

from odoo import api, fields, models

from .wix_api import sanitize_for_log

_logger = logging.getLogger(__name__)

MAX_STORED_PAYLOAD_CHARS = 100000


class WixSyncLog(models.Model):
    _name = 'wix.sync.log'
    _description = 'Wix Synchronization Log'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one('wix.instance', string='Connection', index=True, ondelete='cascade')
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)
    operation = fields.Selection([
        ('order_webhook_received', 'Order Webhook Received'),
        ('order_import', 'Order Import'),
    ], required=True, index=True)
    state = fields.Selection([
        ('started', 'Started'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], default='started', required=True, index=True)

    summary = fields.Char()
    error_message = fields.Text()
    error_category = fields.Selection([
        ('signature', 'Signature Verification'),
        ('payload', 'Malformed Payload'),
        ('mapping', 'Missing Mapping'),
        ('configuration', 'Configuration'),
        ('unexpected', 'Unexpected Error'),
    ])
    request_data = fields.Text(help="Redacted inbound payload kept for audit.")

    source_model = fields.Char(index=True)
    source_id = fields.Integer(index=True)

    @api.model
    def _serialize_payload(self, payload):
        if payload is None:
            return False
        try:
            text = json.dumps(sanitize_for_log(payload), indent=2, default=str)
        except (TypeError, ValueError):
            text = str(payload)
        if len(text) > MAX_STORED_PAYLOAD_CHARS:
            text = text[:MAX_STORED_PAYLOAD_CHARS] + '\n...truncated...'
        return text

    @api.model
    def log_start(self, instance, operation, request_data=None, source=None):
        vals = {
            'instance_id': instance.id if instance else False,
            'operation': operation,
            'state': 'started',
            'request_data': self._serialize_payload(request_data),
        }
        if source is not None:
            vals.update(source_model=source._name, source_id=source.id)
        return self.sudo().create(vals)

    def log_success(self, summary=None):
        self.ensure_one()
        self.sudo().write({'state': 'success', 'summary': summary})
        return self

    def log_fail(self, error_message, error_category='unexpected', summary=None):
        self.ensure_one()
        self.sudo().write({
            'state': 'failed',
            'error_message': error_message,
            'error_category': error_category,
            'summary': summary,
        })
        return self

    @api.model
    def cron_cleanup_old_logs(self, days=30):
        """Remove successful logs older than ``days``.

        Failures are kept indefinitely for audit; only noise from routine,
        already-succeeded webhook deliveries and imports is pruned, so this
        can never delete evidence needed to investigate a problem.
        """
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.search([
            ('create_date', '<', cutoff),
            ('state', '=', 'success'),
        ])
        count = len(old)
        old.unlink()
        _logger.info("Cleaned up %d Wix sync logs older than %d days.", count, days)
        return count
