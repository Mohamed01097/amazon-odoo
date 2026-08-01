import json
import logging
from datetime import datetime, timedelta

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
        if isinstance(payload, (dict, list)):
            value = json.dumps(payload, default=str, indent=2)
        else:
            value = str(payload)
        return value[:limit] if limit else value

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
            vals['request_data'] = json.dumps(request_data, default=str)[:5000] if isinstance(request_data, (dict, list)) else str(request_data)[:5000]
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
        failed = bool(error_message) or (status_code and int(status_code) >= 400)
        finished_at = fields.Datetime.now()
        started_at = finished_at - timedelta(seconds=duration_seconds or 0.0)
        method = request_data.get('method') or ''
        endpoint = request_data.get('endpoint') or ''
        summary = "%s %s -> %s in %.3fs" % (
            method,
            endpoint,
            "HTTP %s" % status_code if status_code else "no response",
            duration_seconds or 0.0,
        )
        return self.create({
            'instance_id': instance.id if hasattr(instance, 'id') else instance,
            'operation': 'api_request',
            'state': 'failed' if failed else 'success',
            'started_at': started_at,
            'finished_at': finished_at,
            'summary': summary,
            'request_data': self._serialize_payload(request_data),
            'response_data': self._serialize_payload(response_data),
            'error_message': str(error_message) if error_message else False,
        })

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
            vals['response_data'] = json.dumps(response_data, default=str)[:5000] if isinstance(response_data, (dict, list)) else str(response_data)[:5000]
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
            vals['response_data'] = json.dumps(response_data, default=str)[:5000] if isinstance(response_data, (dict, list)) else str(response_data)[:5000]
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
    def cleanup_old_logs(self, days=30):
        """Remove logs older than N days."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.search([('create_date', '<', cutoff)])
        count = len(old)
        old.unlink()
        _logger.info("Cleaned up %d sync logs older than %d days.", count, days)
        return count
