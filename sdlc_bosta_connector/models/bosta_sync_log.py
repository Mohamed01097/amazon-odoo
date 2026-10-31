import json
import logging

from odoo import api, fields, models

from .bosta_api import BostaAPI

_logger = logging.getLogger(__name__)

MAX_STORED_PAYLOAD_CHARS = 100000


class BostaSyncLog(models.Model):
    _name = 'bosta.sync.log'
    _description = 'Bosta Synchronization Log'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one(
        'bosta.instance', string='Bosta Account', index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)
    operation = fields.Selection([
        ('create_shipment', 'Create Shipment'),
        ('tracking_poll', 'Tracking Poll'),
        ('webhook_received', 'Webhook Received'),
        ('catalogue_sync', 'Address Catalogue Sync'),
    ], required=True, index=True)
    state = fields.Selection([
        ('started', 'Started'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], default='started', required=True, index=True)

    summary = fields.Char()
    error_message = fields.Text()
    http_status = fields.Integer()
    retryable = fields.Boolean()

    request_data = fields.Text(help="Redacted request payload kept for audit.")
    response_data = fields.Text(help="Redacted response payload kept for audit.")

    source_model = fields.Char(index=True)
    source_id = fields.Integer(index=True)

    @api.model
    def _serialize(self, payload):
        if payload is None:
            return False
        try:
            text = json.dumps(BostaAPI.sanitize(payload), indent=2, default=str)
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
            'request_data': self._serialize(request_data),
        }
        if source is not None:
            vals.update(source_model=source._name, source_id=source.id)
        return self.sudo().create(vals)

    def log_success(self, summary=None, response_data=None):
        self.ensure_one()
        self.sudo().write({
            'state': 'success',
            'summary': summary,
            'response_data': self._serialize(response_data),
        })
        return self

    def log_fail(self, error_message, http_status=None, retryable=False,
                 response_data=None):
        self.ensure_one()
        self.sudo().write({
            'state': 'failed',
            'error_message': error_message,
            'http_status': http_status or 0,
            'retryable': retryable,
            'response_data': self._serialize(response_data),
        })
        return self

    @api.model
    def cron_cleanup_old_logs(self, days=30):
        """Remove successful logs older than ``days``.

        Failures are kept indefinitely for audit; only noise from routine,
        already-succeeded bookings, polls, webhooks and catalogue syncs is
        pruned, so this can never delete evidence needed to investigate a
        problem.
        """
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.search([
            ('create_date', '<', cutoff),
            ('state', '=', 'success'),
        ])
        count = len(old)
        old.unlink()
        _logger.info("Cleaned up %d Bosta sync logs older than %d days.", count, days)
        return count
