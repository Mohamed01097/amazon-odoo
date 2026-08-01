import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class AmazonSyncLog(models.Model):
    _name = 'amazon.sync.log'
    _description = 'Amazon Sync Log'
    _order = 'create_date desc'
    _rec_name = 'display_name'

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)
    operation = fields.Selection([
        # Products
        ('product_import', 'Product Import'),
        ('product_export', 'Product Export'),
        ('product_create', 'Product Create'),
        ('product_update', 'Product Update'),
        ('product_mapping', 'Product Mapping'),
        ('product_pull', 'Product Pull'),
        # Orders
        ('order_import', 'Order Import'),
        ('order_status_update', 'Order Status Update'),
        ('order_cancel', 'Order Cancel'),
        # Stock
        ('stock_export', 'Stock Export'),
        ('stock_pull', 'Stock Pull'),
        ('inventory_reconciliation', 'Inventory Reconciliation'),
        # Prices
        ('price_export', 'Price Export'),
        ('price_pull', 'Price Pull'),
        # Shipping
        ('shipment_confirm', 'Shipment Confirm'),
        ('tracking_export', 'Tracking Export'),
        # Reports
        ('settlement_import', 'Settlement Import'),
        ('return_import', 'Return Import'),
        ('removal_import', 'Removal Import'),
        ('fba_inventory_import', 'FBA Inventory Import'),
        ('rating_import', 'Rating Import'),
        ('vcs_import', 'VCS Import'),
        # FBA
        ('inbound_shipment', 'Inbound Shipment'),
        ('outbound_order', 'Outbound Order'),
        # AI
        ('ai_generate', 'AI Generate'),
        ('ai_detect_type', 'AI Detect Type'),
        ('ai_price', 'AI Price Optimization'),
        ('ai_inventory', 'AI Inventory Prediction'),
        ('ai_reply', 'AI Customer Reply'),
        ('ai_error_fix', 'AI Error Fix'),
        # Raw Amazon API exchange
        ('api_request', 'Amazon API Request'),
        # Full Sync
        ('full_sync', 'Full Sync'),
    ], required=True, index=True)

    state = fields.Selection([
        ('started', 'Started'),
        ('success', 'Success'),
        ('partial', 'Partial Success'),
        ('failed', 'Failed'),
    ], default='started', required=True, index=True)

    # Counters
    records_processed = fields.Integer(default=0)
    records_created = fields.Integer(default=0)
    records_updated = fields.Integer(default=0)
    records_failed = fields.Integer(default=0)

    # Details
    summary = fields.Text('Summary')
    error_message = fields.Text('Error Details')
    request_data = fields.Text('Request Data')
    response_data = fields.Text('Response Data')

    # Normalized operational telemetry.  The raw JSON fields above remain the
    # audit source; these indexed columns make monitoring cheap and predictable.
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    http_method = fields.Char(index=True, readonly=True)
    endpoint = fields.Char(index=True, readonly=True)
    operation_name = fields.Char(index=True, readonly=True)
    http_status = fields.Integer(index=True, readonly=True)
    amazon_request_id = fields.Char(index=True, readonly=True)
    amazon_error_code = fields.Char(index=True, readonly=True)
    amazon_error_message = fields.Text(readonly=True)
    error_category = fields.Selection([
        ('transient', 'Transient'),
        ('configuration', 'Configuration'),
        ('authorization', 'Authorization'),
        ('validation', 'Validation'),
        ('data', 'Data'),
        ('rate_limit', 'Rate Limit'),
        ('amazon_service', 'Amazon Service'),
        ('unknown', 'Unknown'),
    ], index=True, readonly=True)
    transient_error = fields.Boolean(index=True, readonly=True)
    retry_safe = fields.Boolean(index=True, readonly=True)
    rate_limit = fields.Float(readonly=True)
    retry_after_seconds = fields.Float(readonly=True)
    is_throttled = fields.Boolean(index=True, readonly=True)
    source_model = fields.Char(index=True, readonly=True)
    source_id = fields.Integer(index=True, readonly=True)
    operation_control_id = fields.Many2one(
        'amazon.operation.control', ondelete='set null', index=True,
        readonly=True,
    )
    responsible_user_id = fields.Many2one(
        'res.users', default=lambda self: self.env.user,
        readonly=True, index=True,
    )
    attempt_number = fields.Integer(default=1, readonly=True)

    # Timing
    started_at = fields.Datetime(default=fields.Datetime.now)
    finished_at = fields.Datetime()
    duration_seconds = fields.Float(compute='_compute_duration', store=True)

    # Links
    res_model = fields.Char('Related Model')
    res_id = fields.Integer('Related Record ID')

    display_name = fields.Char(compute='_compute_display_name', store=True)

    @api.depends('operation', 'state', 'create_date')
    def _compute_display_name(self):
        for rec in self:
            op_label = dict(self._fields['operation'].selection).get(rec.operation, rec.operation or '')
            ts = rec.create_date.strftime('%Y-%m-%d %H:%M') if rec.create_date else ''
            rec.display_name = "[%s] %s — %s" % (rec.state or 'started', op_label, ts)

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                delta = rec.finished_at - rec.started_at
                rec.duration_seconds = delta.total_seconds()
            else:
                rec.duration_seconds = 0.0

    # ──────────────────────────────────────────────
    # Convenience: start / finish / fail
    # ──────────────────────────────────────────────

    @api.model
    def _serialize_payload(self, payload, limit=None):
        if payload in (None, False):
            return False
        payload = self._sanitize_payload(payload)
        if isinstance(payload, (dict, list)):
            value = json.dumps(payload, default=str, indent=2)
        else:
            value = str(payload)
        return value[:limit] if limit else value

    @api.model
    def _sanitize_payload(self, payload):
        """Redact credentials even when callers bypass :class:`AmazonAPI`."""
        sensitive = {
            'access_token', 'authorization', 'client_secret', 'lwa_access_token',
            'password', 'refresh_token', 'secret', 'signature', 'token',
            'x-amz-access-token', 'x-amz-security-token', 'aws_access_key',
            'aws_secret_key',
        }
        if isinstance(payload, dict):
            clean = {}
            for key, value in payload.items():
                normalized = str(key or '').lower()
                is_sensitive = any(part in normalized for part in sensitive)
                clean[key] = '***REDACTED***' if is_sensitive else self._sanitize_payload(value)
            return clean
        if isinstance(payload, (list, tuple)):
            return [self._sanitize_payload(value) for value in payload]
        return payload

    @api.model
    def _response_header(self, response_data, name):
        headers = (response_data or {}).get('headers') or {}
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return value
        return False

    @api.model
    def _float_header(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @api.model
    def log_start(self, instance, operation, request_data=None, res_model=None, res_id=None):
        """Create a log entry when an operation starts."""
        vals = {
            'instance_id': instance.id if hasattr(instance, 'id') else instance,
            'operation': operation,
            'state': 'started',
            'started_at': fields.Datetime.now(),
        }
        if request_data:
            vals['request_data'] = self._serialize_payload(request_data, limit=5000)
        if res_model:
            vals['res_model'] = res_model
        if res_id:
            vals['res_id'] = res_id
        return self.create(vals)

    @api.model
    def log_api_request(self, instance, request_data=None, response_data=None,
                        error_message='', duration_seconds=0.0):
        """Persist one raw Amazon HTTP exchange without sending UI popups."""
        request_data = request_data or {}
        response_data = response_data or {}
        status_code = response_data.get('status_code')
        try:
            status_code = int(status_code) if status_code not in (None, False, '') else 0
        except (TypeError, ValueError):
            status_code = 0
        failed = bool(error_message) or (status_code and int(status_code) >= 400)
        finished_at = fields.Datetime.now()
        started_at = finished_at - timedelta(seconds=duration_seconds or 0.0)
        method = request_data.get('method') or ''
        endpoint = request_data.get('endpoint') or ''
        endpoint_path = urlsplit(endpoint).path or endpoint
        response_json = response_data.get('response_json')
        error = {}
        try:
            from .amazon_api import AmazonAPI
            error = AmazonAPI._extract_amazon_error(response_json)
        except Exception:
            error = {}
        error_code = error.get('code') or ''
        error_text = error.get('message') or str(error_message or '')
        classification = self.env['amazon.operation.control'].classify_error(
            http_status=status_code,
            error_code=error_code,
            message=error_text,
        )
        source_model = self.env.context.get('amazon_source_model') or False
        source_id = self.env.context.get('amazon_source_id') or 0
        control = self.env['amazon.operation.control']
        responsible_user = self.env.user
        if source_model and source_id:
            control = control.sudo().get_or_create_for_source(source_model, source_id)
            source = self.env[source_model].sudo().browse(int(source_id)).exists() \
                if source_model in self.env else self.env['res.users']
            if source and 'responsible_user_id' in source._fields and source.responsible_user_id:
                responsible_user = source.responsible_user_id
            elif source and source.create_uid:
                responsible_user = source.create_uid
        attempt_number = (control.attempt_count + 1) if control else 1
        retry_after = self._float_header(self._response_header(response_data, 'Retry-After'))
        rate_limit = self._float_header(
            self._response_header(response_data, 'x-amzn-RateLimit-Limit')
        )
        request_id = response_data.get('amazon_request_id') or self._response_header(
            response_data, 'x-amzn-RequestId'
        )
        summary = "%s %s -> %s in %.3fs" % (
            method,
            endpoint,
            "HTTP %s" % status_code if status_code else "no response",
            duration_seconds or 0.0,
        )
        log = self.create({
            'instance_id': instance.id if hasattr(instance, 'id') else instance,
            'operation': 'api_request',
            'state': 'failed' if failed else 'success',
            'started_at': started_at,
            'finished_at': finished_at,
            'summary': summary,
            'request_data': self._serialize_payload(request_data),
            'response_data': self._serialize_payload(response_data),
            'error_message': str(error_message) if error_message else False,
            'http_method': method.upper() or False,
            'endpoint': endpoint_path or False,
            'operation_name': self.env.context.get('amazon_operation') or endpoint_path.rsplit('/', 1)[-1] or False,
            'http_status': status_code or 0,
            'amazon_request_id': request_id or False,
            'amazon_error_code': error_code or False,
            'amazon_error_message': error_text or False,
            'error_category': classification['category'],
            'transient_error': classification['transient'],
            'retry_safe': classification['retry_safe'],
            'rate_limit': rate_limit,
            'retry_after_seconds': retry_after,
            'is_throttled': status_code == 429,
            'source_model': source_model,
            'source_id': source_id,
            'operation_control_id': control.id if control else False,
            'responsible_user_id': responsible_user.id,
            'attempt_number': attempt_number,
        })
        if control:
            control.sudo().write({
                'attempt_count': attempt_number,
                'endpoint': endpoint_path or control.endpoint,
                'http_method': method.upper() or control.http_method,
                'last_amazon_request_id': request_id or control.last_amazon_request_id,
                'last_http_status': status_code or control.last_http_status,
                'latest_attempt_at': finished_at,
                'sync_log_id': log.id,
            })
            source = control._source_record()
            if source and 'amazon_request_id' in source._fields and request_id:
                source.sudo().with_context(skip_amazon_operation_tracking=True).write({
                    'amazon_request_id': request_id,
                })
        instance_record = instance if hasattr(instance, 'id') else self.env['amazon.instance'].browse(instance)
        if instance_record and hasattr(instance_record, '_record_api_outcome'):
            instance_record.sudo()._record_api_outcome(log)
        return log

    def _send_bus_notification(self, title, message, msg_type='success'):
        """Send a real-time bus notification to all users viewing Amazon module."""
        try:
            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'title': title,
                    'message': message,
                    'type': msg_type,
                    'sticky': msg_type == 'danger',
                },
            )
        except Exception:
            pass  # Bus notification is non-critical

    def log_success(self, summary='', records_processed=0, records_created=0,
                    records_updated=0, response_data=None):
        """Mark operation as successful + send notification."""
        vals = {
            'state': 'success',
            'finished_at': fields.Datetime.now(),
            'summary': summary,
            'records_processed': records_processed,
            'records_created': records_created,
            'records_updated': records_updated,
        }
        if response_data:
            vals['response_data'] = self._serialize_payload(response_data, limit=5000)
        self.write(vals)
        # Send popup notification
        op_label = dict(self._fields['operation'].selection).get(self.operation, self.operation or '')
        self._send_bus_notification(
            "Amazon: %s" % op_label,
            summary or "Completed successfully. %d processed, %d created, %d updated." % (
                records_processed, records_created, records_updated,
            ),
            'success',
        )

    def log_partial(self, summary='', records_processed=0, records_created=0,
                    records_updated=0, records_failed=0, error_message=''):
        """Mark operation as partially successful + send notification."""
        self.write({
            'state': 'partial',
            'finished_at': fields.Datetime.now(),
            'summary': summary,
            'records_processed': records_processed,
            'records_created': records_created,
            'records_updated': records_updated,
            'records_failed': records_failed,
            'error_message': error_message,
        })
        op_label = dict(self._fields['operation'].selection).get(self.operation, self.operation or '')
        self._send_bus_notification(
            "Amazon: %s (Partial)" % op_label,
            summary or "%d processed, %d failed." % (records_processed, records_failed),
            'warning',
        )

    def log_fail(self, error_message='', response_data=None):
        """Mark operation as failed + send notification."""
        vals = {
            'state': 'failed',
            'finished_at': fields.Datetime.now(),
            'error_message': str(error_message)[:5000],
        }
        if response_data:
            vals['response_data'] = self._serialize_payload(response_data, limit=5000)
        self.write(vals)
        op_label = dict(self._fields['operation'].selection).get(self.operation, self.operation or '')
        self._send_bus_notification(
            "Amazon: %s FAILED" % op_label,
            str(error_message)[:200],
            'danger',
        )

    # ──────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────

    @api.model
    def cleanup_old_logs(self, days=30, instance=None):
        """Remove logs older than N days."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        # Keep failures, partial results, linked attempts and audit-critical logs.
        domain = [
            ('create_date', '<', cutoff),
            ('state', '=', 'success'),
            ('operation_control_id', '=', False),
            ('operation', '=', 'api_request'),
        ]
        if instance:
            domain.append(('instance_id', '=', instance.id if hasattr(instance, 'id') else instance))
        old = self.search(domain)
        count = len(old)
        old.unlink()
        _logger.info("Cleaned up %d sync logs older than %d days.", count, days)
        return count
