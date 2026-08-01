import json
import logging
import random
from datetime import timedelta

import requests
from psycopg2 import IntegrityError

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from .amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)


ERROR_CATEGORIES = [
    ('transient', 'Transient'),
    ('configuration', 'Configuration'),
    ('authorization', 'Authorization'),
    ('validation', 'Validation'),
    ('data', 'Data'),
    ('rate_limit', 'Rate Limit'),
    ('amazon_service', 'Amazon Service'),
    ('unknown', 'Unknown'),
]

SOURCE_MODELS = {
    'amazon.order.import.job',
    'amazon.order.status.sync.job',
    'amazon.inbound.operation.job',
    'amazon.inventory.reconciliation.run',
}

INBOUND_READ_OPERATIONS = {
    'refresh_packing_options',
    'refresh_placement_options',
    'refresh_shipment_status',
    'sync_receiving',
}


class AmazonOperationControl(models.Model):
    """Retry/manual-review metadata layered over existing durable jobs.

    Business job data stays in its original model.  This record only stores the
    operational decision and links its API-attempt history in ``amazon.sync.log``.
    """

    _name = 'amazon.operation.control'
    _description = 'Amazon Operation Control'
    _order = 'severity desc, latest_failure_at desc, id desc'
    _check_company_auto = True

    source_model = fields.Char(required=True, index=True, readonly=True)
    source_id = fields.Integer(required=True, index=True, readonly=True)
    source_name = fields.Char(readonly=True)
    job_type = fields.Char(required=True, index=True, readonly=True)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, ondelete='cascade', index=True,
        readonly=True,
    )
    state = fields.Selection([
        ('active', 'Active'),
        ('retry_pending', 'Waiting Retry'),
        ('manual_review', 'Manual Review'),
        ('cancelled', 'Retry Cancelled'),
        ('exhausted', 'Retries Exhausted'),
        ('resolved', 'Resolved'),
    ], required=True, default='active', index=True)
    severity = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('critical', 'Critical'),
    ], required=True, default='warning', index=True)
    priority = fields.Integer(default=50, index=True)
    responsible_user_id = fields.Many2one(
        'res.users', default=lambda self: self.env.user, index=True,
    )
    error_category = fields.Selection(ERROR_CATEGORIES, index=True, readonly=True)
    retry_safe = fields.Boolean(index=True, readonly=True)
    retry_count = fields.Integer(default=0, readonly=True)
    attempt_count = fields.Integer(default=0, readonly=True)
    max_retries = fields.Integer(default=5, readonly=True)
    next_retry_at = fields.Datetime(index=True, readonly=True)
    first_failure_at = fields.Datetime(index=True, readonly=True)
    latest_failure_at = fields.Datetime(index=True, readonly=True)
    latest_attempt_at = fields.Datetime(index=True, readonly=True)
    last_activity_at = fields.Datetime(index=True, readonly=True)
    endpoint = fields.Char(index=True, readonly=True)
    http_method = fields.Char(readonly=True)
    last_http_status = fields.Integer(index=True, readonly=True)
    last_error_code = fields.Char(index=True, readonly=True)
    last_error_message = fields.Text(readonly=True)
    last_amazon_request_id = fields.Char(index=True, readonly=True)
    waiting_reason = fields.Selection([
        ('actively_running', 'Actively Running'),
        ('waiting_amazon', 'Waiting for Amazon'),
        ('waiting_rate_limit', 'Waiting for Rate Limit'),
        ('abandoned', 'Abandoned'),
        ('unclear', 'Unclear'),
    ], index=True, readonly=True)
    recommended_action = fields.Text(readonly=True)
    manual_review_note = fields.Text()
    sync_log_id = fields.Many2one('amazon.sync.log', ondelete='set null', readonly=True)
    attempt_log_ids = fields.One2many(
        'amazon.sync.log', 'operation_control_id', string='Attempt History',
        readonly=True,
    )

    _unique_source = models.Constraint(
        'UNIQUE (source_model, source_id)',
        'Only one operational control record can exist for a source job.',
    )
    _valid_retry_values = models.Constraint(
        'CHECK (retry_count >= 0 AND attempt_count >= 0 AND max_retries BETWEEN 1 AND 100)',
        'Operational retry values are invalid.',
    )

    @api.model
    def classify_error(self, http_status=0, error_code='', message=''):
        """Classify failures centrally without treating unknown errors as safe."""
        try:
            status = int(http_status or 0)
        except (TypeError, ValueError):
            status = 0
        code = str(error_code or '').strip()
        text = ('%s %s' % (code, message or '')).lower()
        if status == 429 or 'http429' in text or 'quotaexceeded' in text or 'throttl' in text:
            return {'category': 'rate_limit', 'transient': True, 'retry_safe': True}
        if status in (401, 403) or any(token in text for token in (
            'unauthorized', 'forbidden', 'accessdenied', 'invalid_grant',
            'revoked authorization', 'role is not authorized',
        )):
            return {'category': 'authorization', 'transient': False, 'retry_safe': False}
        if status >= 500 or any(token in text for token in (
            'internalfailure', 'serviceunavailable', 'amazon service', 'http 50',
        )):
            return {'category': 'amazon_service', 'transient': True, 'retry_safe': True}
        if any(token in text for token in (
            'connection reset', 'connection aborted', 'connection error', 'timeout',
            'timed out', 'temporary failure', 'name resolution', 'network error',
        )):
            return {'category': 'transient', 'transient': True, 'retry_safe': True}
        if any(token in text for token in (
            'in_progress', 'in progress', 'not_started', 'not started',
            'operation is pending', 'still processing',
        )):
            return {'category': 'transient', 'transient': True, 'retry_safe': True}
        if any(token in text for token in (
            'missing configuration', 'not configured',
            'missing required fields', 'configure ',
        )):
            return {'category': 'configuration', 'transient': False, 'retry_safe': False}
        if status == 400 or any(token in text for token in (
            'invalidinput', 'invalid input', 'invalid address', 'malformed',
            'invalid workflow state', 'validationerror', 'validation error',
        )):
            return {'category': 'validation', 'transient': False, 'retry_safe': False}
        if any(token in text for token in (
            'invalid sku', 'unknown sku', 'sku mapping', 'could not be mapped',
            'missing sku', 'duplicate sku', 'missing product mapping',
        )):
            return {'category': 'data', 'transient': False, 'retry_safe': False}
        return {'category': 'unknown', 'transient': False, 'retry_safe': False}

    @api.model
    def _job_type(self, source):
        if source._name == 'amazon.order.import.job':
            return 'order_import'
        if source._name == 'amazon.order.status.sync.job':
            return 'order_status_sync'
        if source._name == 'amazon.inventory.reconciliation.run':
            return 'inventory_reconciliation'
        if source._name == 'amazon.inbound.operation.job':
            return source.operation_type or 'inbound_operation'
        return source._name

    @api.model
    def _instance_for_source(self, source):
        instance = source.instance_id
        if not instance:
            raise ValidationError(_("The Amazon job has no instance."))
        return instance

    @api.model
    def get_or_create_for_source(self, source_model, source_id):
        if source_model not in SOURCE_MODELS or not source_id:
            return self.browse()
        control = self.search([
            ('source_model', '=', source_model),
            ('source_id', '=', int(source_id)),
        ], limit=1)
        if control:
            return control
        source = self.env[source_model].sudo().browse(int(source_id)).exists()
        if not source:
            return self.browse()
        instance = self._instance_for_source(source)
        try:
            with self.env.cr.savepoint():
                return self.create({
                    'source_model': source_model,
                    'source_id': source.id,
                    'source_name': source.display_name,
                    'job_type': self._job_type(source),
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'max_retries': instance.maximum_automatic_retries or 5,
                    'responsible_user_id': (
                        getattr(source, 'responsible_user_id', False).id
                        or source.create_uid.id or self.env.uid
                    ),
                    'last_activity_at': getattr(source, 'last_activity_at', False) or source.write_date,
                })
        except IntegrityError:
            # A competing worker may have inserted the unique source overlay.
            # The savepoint keeps that benign race from aborting the job's
            # surrounding business transaction.
            control = self.search([
                ('source_model', '=', source_model),
                ('source_id', '=', int(source_id)),
            ], limit=1)
            if control:
                return control
            raise

    def _source_record(self):
        self.ensure_one()
        if self.source_model not in SOURCE_MODELS:
            return self.env['amazon.instance'].browse()
        return self.env[self.source_model].browse(self.source_id).exists()

    def _source_retry_safe(self, source):
        if source._name in {
            'amazon.order.import.job',
            'amazon.order.status.sync.job',
            'amazon.inventory.reconciliation.run',
        }:
            return True
        if source._name == 'amazon.inbound.operation.job':
            return bool(
                source.operation_type in INBOUND_READ_OPERATIONS
                or source.operation_id
            )
        return False

    @api.model
    def _source_error(self, source):
        message = (
            getattr(source, 'last_error_message', False)
            or getattr(source, 'last_error', False)
            or getattr(source, 'error_message', False)
            or _('The Amazon job failed without a structured error.')
        )
        code = getattr(source, 'last_error_code', False) or ''
        if source._name == 'amazon.inbound.operation.job' and source.inbound_shipment_id:
            shipment = source.inbound_shipment_id
            code = code or getattr(shipment, 'shipment_error_code', False) or ''
            message = message or getattr(shipment, 'shipment_error_message', False) or ''
        return str(code or ''), str(message or '')

    @api.model
    def _recommended_action(self, category, retry_safe):
        actions = {
            'rate_limit': _('Wait until the displayed retry time; reduce or stagger calls if throttling repeats.'),
            'amazon_service': _('Retry after backoff. If unrelated operations also fail, check the official SP-API Health Dashboard.'),
            'transient': _('Verify network connectivity and retry the same resumable job.'),
            'authorization': _('Re-authorize the selling partner application and verify the required SP-API role.'),
            'configuration': _('Correct the Amazon instance configuration before retrying.'),
            'validation': _('Correct the source payload or workflow state; do not retry unchanged data.'),
            'data': _('Correct the SKU/product mapping or source data before retrying.'),
            'unknown': _('Review the sanitized request, response, and Amazon request ID before deciding whether to retry.'),
        }
        if not retry_safe and category in ('unknown', 'validation', 'data', 'configuration', 'authorization'):
            return actions[category]
        return actions.get(category, actions['unknown'])

    @api.model
    def record_source_failure(self, source, partial=False):
        control = self.sudo().get_or_create_for_source(source._name, source.id)
        if not control:
            return control
        code, message = self._source_error(source)
        classification = self.classify_error(
            http_status=control.last_http_status,
            error_code=code,
            message=message,
        )
        retry_safe = bool(classification['retry_safe'] and control._source_retry_safe(source))
        category = classification['category']
        if partial:
            retry_safe = False
            category = 'data' if category == 'unknown' else category
        now = fields.Datetime.now()
        delay = control.instance_id.retry_backoff_base_seconds or 60
        delay *= 2 ** min(control.retry_count, 8)
        # A long Retry-After is deliberately deferred to a later worker turn.
        # The linked API attempt is the latest response for this source job.
        if control.sync_log_id and control.sync_log_id.is_throttled:
            delay = max(delay, control.sync_log_id.retry_after_seconds or 0.0)
        delay += random.uniform(0, max(1.0, min(delay * 0.25, 30.0)))
        next_retry = now + timedelta(seconds=min(delay, 86400)) if retry_safe else False
        exhausted = control.retry_count >= control.max_retries
        if exhausted:
            next_retry = False
        severity = 'critical' if category == 'authorization' or exhausted else (
            'error' if not retry_safe else 'warning'
        )
        state = 'exhausted' if exhausted else ('retry_pending' if retry_safe else 'manual_review')
        values = {
            'source_name': source.display_name,
            'job_type': self._job_type(source),
            'state': state,
            'severity': severity,
            'error_category': category,
            'retry_safe': retry_safe,
            'next_retry_at': next_retry,
            'first_failure_at': control.first_failure_at or now,
            'latest_failure_at': now,
            'last_activity_at': getattr(source, 'last_activity_at', False) or source.write_date,
            'last_error_code': code or control.last_error_code,
            'last_error_message': message,
            'recommended_action': self._recommended_action(category, retry_safe),
            'max_retries': control.instance_id.maximum_automatic_retries or control.max_retries,
            'waiting_reason': 'waiting_rate_limit' if category == 'rate_limit' else False,
        }
        control.write(values)
        attempt = self.env['amazon.sync.log'].sudo().with_context(
            skip_amazon_operation_tracking=True,
        ).create({
            'instance_id': control.instance_id.id,
            'operation': 'job_failure',
            'state': 'failed',
            'started_at': now,
            'finished_at': now,
            'summary': _('%s failed (%s).', control.job_type, category),
            'error_message': message[:5000],
            'res_model': source._name,
            'res_id': source.id,
            'source_model': source._name,
            'source_id': source.id,
            'operation_control_id': control.id,
            'responsible_user_id': control.responsible_user_id.id,
            'attempt_number': control.attempt_count + 1,
            'http_status': control.last_http_status,
            'amazon_request_id': control.last_amazon_request_id,
            'amazon_error_code': code or False,
            'amazon_error_message': message,
            'error_category': category,
            'transient_error': classification['transient'],
            'retry_safe': retry_safe,
        })
        control.write({
            'attempt_count': control.attempt_count + 1,
            'latest_attempt_at': now,
            'sync_log_id': attempt.id,
        })
        return control

    def mark_source_resolved(self):
        for control in self:
            control.write({
                'state': 'resolved',
                'severity': 'info',
                'next_retry_at': False,
                'waiting_reason': False,
                'retry_safe': False,
            })

    def _check_retry_access(self, administrative=False):
        group = (
            'sdlc_amazon_connector.group_amazon_technical_admin'
            if administrative else 'sdlc_amazon_connector.group_amazon_manager'
        )
        if not self.env.user.has_group(group):
            raise AccessError(_("You are not allowed to retry Amazon operations."))

    def _queue_source(self, administrative=False):
        self.ensure_one()
        self._check_retry_access(administrative=administrative)
        self.env.cr.execute(
            'SELECT id FROM amazon_operation_control WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset()
        source = self._source_record()
        if not source:
            raise UserError(_("The source job no longer exists."))
        active_states = {
            'amazon.order.import.job': ('draft', 'running'),
            'amazon.order.status.sync.job': ('draft', 'pending', 'running'),
            'amazon.inbound.operation.job': ('pending', 'in_progress'),
            'amazon.inventory.reconciliation.run': ('queued', 'running'),
        }
        if source.state in active_states[source._name] and self.state != 'retry_pending':
            raise UserError(_("This source job is already queued or running."))
        if not administrative and (not self.retry_safe or self.error_category not in (
            'transient', 'rate_limit', 'amazon_service',
        )):
            raise UserError(_("This is a permanent or unclassified failure. Correct it and use an administrative retry only after review."))
        if self.retry_count >= self.max_retries and not administrative:
            raise UserError(_("The maximum automatic retry count has been reached."))

        now = fields.Datetime.now()
        values = {'finished_at': False}
        if source._name == 'amazon.order.import.job':
            values.update(state='draft', next_run_at=now)
        elif source._name == 'amazon.order.status.sync.job':
            values.update(state='pending', next_run_at=now)
        elif source._name == 'amazon.inbound.operation.job':
            values.update(state='pending', next_run_at=now)
        else:
            values.update(state='queued', next_run_at=now)
        source.sudo().with_context(skip_amazon_operation_tracking=True).write(values)
        retry_count = self.retry_count + 1
        self.write({
            'state': 'active',
            'retry_count': retry_count,
            'next_retry_at': False,
            'waiting_reason': False,
            'last_activity_at': now,
        })
        self.env['amazon.sync.log'].sudo().create({
            'instance_id': self.instance_id.id,
            'operation': 'job_retry',
            'state': 'success',
            'started_at': now,
            'finished_at': now,
            'summary': _('Retry %s/%s queued for %s.', retry_count, self.max_retries, self.source_name),
            'res_model': self.source_model,
            'res_id': self.source_id,
            'source_model': self.source_model,
            'source_id': self.source_id,
            'operation_control_id': self.id,
            'responsible_user_id': self.responsible_user_id.id,
            'attempt_number': self.attempt_count + 1,
        })
        return True

    def action_retry(self):
        for control in self:
            control._queue_source(administrative=False)
        return True

    def action_administrative_retry(self):
        for control in self:
            control._queue_source(administrative=True)
        return True

    def action_cancel_pending_retry(self):
        self._check_retry_access()
        eligible = self.filtered(lambda record: record.state == 'retry_pending')
        if len(eligible) != len(self):
            raise UserError(_("Only pending retries can be cancelled."))
        eligible.write({'state': 'cancelled', 'next_retry_at': False})
        return True

    def action_mark_manual_review(self):
        self._check_retry_access()
        self.write({'state': 'manual_review', 'next_retry_at': False})
        return True

    @api.model
    def action_retry_all_eligible(self):
        controls = self.search([
            ('state', '=', 'retry_pending'),
            ('retry_safe', '=', True),
            ('error_category', 'in', ('transient', 'rate_limit', 'amazon_service')),
        ]).filtered(lambda control: control.retry_count < control.max_retries)
        for control in controls:
            control._queue_source(administrative=False)
        return True

    def action_open_source(self):
        self.ensure_one()
        source = self._source_record()
        if not source:
            raise UserError(_("The source job no longer exists."))
        return {
            'type': 'ir.actions.act_window',
            'name': self.source_name,
            'res_model': self.source_model,
            'view_mode': 'form',
            'res_id': self.source_id,
            'target': 'current',
        }

    def action_open_sync_log(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Amazon Attempt History'),
            'res_model': 'amazon.sync.log',
            'view_mode': 'list,form',
            'domain': [('operation_control_id', '=', self.id)],
            'context': {'create': False},
        }

    @api.model
    def sync_existing_failure_controls(self, limit=1000):
        """Backfill failures created before the operational overlay existed.

        Normal state transitions are tracked by ``write`` hooks. This bounded
        scan makes upgrades safe and ensures legacy failed/partial durable jobs
        become visible in Retry Center without creating replacement jobs.
        """
        states_by_model = {
            'amazon.order.import.job': ('failed', 'partial'),
            'amazon.order.status.sync.job': ('failed', 'partial'),
            'amazon.inbound.operation.job': ('failed',),
            'amazon.inventory.reconciliation.run': ('failed',),
        }
        created = 0
        remaining = max(int(limit or 0), 0)
        for model_name, failure_states in states_by_model.items():
            if not remaining:
                break
            sources = self.env[model_name].sudo().search([
                ('state', 'in', failure_states),
            ], order='write_date desc, id desc', limit=remaining)
            for source in sources:
                control = self.sudo().get_or_create_for_source(model_name, source.id)
                if control and not control.first_failure_at:
                    self.sudo().record_source_failure(
                        source, partial=source.state == 'partial',
                    )
                    created += 1
            remaining -= len(sources)
        return created

    @api.model
    def cron_dispatch_operational_retries(self):
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_operation_control
             WHERE state = 'retry_pending'
               AND retry_safe IS TRUE
               AND error_category IN ('transient', 'rate_limit', 'amazon_service')
               AND retry_count < max_retries
               AND (next_retry_at IS NULL OR next_retry_at <= %s)
             ORDER BY priority, COALESCE(next_retry_at, latest_failure_at), id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        """, [now])
        row = self.env.cr.fetchone()
        if not row:
            return False
        control = self.browse(row[0]).sudo()
        # Cron is an administrative actor, but still obeys the strict automatic
        # category and retry-safe filters in the SQL query above.
        control.with_user(self.env.ref('base.user_root'))._queue_source(administrative=True)
        return True

    def _source_row_is_locked(self, source):
        table = source._table
        if source._name not in SOURCE_MODELS:
            return False
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    'SELECT id FROM "%s" WHERE id = %%s FOR UPDATE NOWAIT' % table,
                    [source.id],
                )
            return False
        except Exception:
            return True

    @api.model
    def cron_detect_stuck_jobs(self):
        now = fields.Datetime.now()
        monitors = self.env['amazon.operation.job.monitor'].sudo().search([
            ('state', '=', 'running'),
        ], order='last_activity_at asc', limit=200)
        classified = 0
        for monitor in monitors:
            threshold = monitor.instance_id.stuck_job_threshold_minutes or 60
            if not monitor.last_activity_at or monitor.last_activity_at > now - timedelta(minutes=threshold):
                continue
            control = self.sudo().get_or_create_for_source(
                monitor.source_model, monitor.source_id,
            )
            source = control._source_record()
            if not source:
                continue
            next_run = getattr(source, 'next_run_at', False)
            if next_run and next_run > now:
                reason = (
                    'waiting_rate_limit'
                    if control.error_category == 'rate_limit'
                    else 'waiting_amazon'
                )
                control.write({'waiting_reason': reason, 'last_activity_at': monitor.last_activity_at})
                classified += 1
                continue
            if control._source_row_is_locked(source):
                control.write({'waiting_reason': 'actively_running'})
                classified += 1
                continue
            safe = control._source_retry_safe(source)
            control.write({
                'waiting_reason': 'abandoned' if safe else 'unclear',
                'state': 'retry_pending' if safe else 'manual_review',
                'severity': 'error',
                'error_category': 'transient' if safe else 'unknown',
                'retry_safe': safe,
                'next_retry_at': now if safe else False,
                'latest_failure_at': control.latest_failure_at or now,
                'first_failure_at': control.first_failure_at or now,
                'last_error_code': control.last_error_code or 'STUCK_JOB',
                'last_error_message': control.last_error_message or _(
                    'The job has no recent activity and no worker currently owns its row lock.'
                ),
                'recommended_action': _(
                    'Resume the same durable job; do not create a replacement Amazon operation.'
                ) if safe else _(
                    'Review the worker logs and source state before taking administrative action.'
                ),
            })
            classified += 1
        return classified

    @api.model
    def cron_cleanup_operational_records(self):
        now = fields.Datetime.now()
        total = 0
        for instance in self.env['amazon.instance'].sudo().search([]):
            total += self.env['amazon.sync.log'].sudo().with_context(
                active_test=False,
            ).search_count([
                ('instance_id', '=', instance.id),
                ('create_date', '<', now - timedelta(days=instance.log_retention_days or 90)),
                ('state', '=', 'success'),
                ('operation_control_id', '=', False),
                ('operation', '=', 'api_request'),
            ])
            self.env['amazon.sync.log'].sudo().cleanup_old_logs(
                days=instance.log_retention_days or 90,
                instance=instance,
            )
            stale_controls = self.sudo().search([
                ('instance_id', '=', instance.id),
                ('state', '=', 'resolved'),
                ('write_date', '<', now - timedelta(days=instance.job_retention_days or 365)),
                ('attempt_log_ids', '=', False),
            ])
            total += len(stale_controls)
            stale_controls.unlink()
        return total


class AmazonOperationJobMonitor(models.Model):
    _name = 'amazon.operation.job.monitor'
    _description = 'Amazon Unified Job Monitor'
    _auto = False
    _rec_name = 'source_name'
    _order = 'priority, last_activity_at desc, id desc'

    source_model = fields.Char(readonly=True, index=True)
    source_id = fields.Integer(readonly=True, index=True)
    source_name = fields.Char(readonly=True)
    instance_id = fields.Many2one('amazon.instance', readonly=True, index=True)
    company_id = fields.Many2one('res.company', readonly=True, index=True)
    job_type = fields.Char(readonly=True, index=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('partial', 'Partial'),
        ('failed', 'Failed'),
        ('retry_pending', 'Waiting Retry'),
        ('manual_review', 'Manual Review'),
        ('cancelled', 'Retry Cancelled'),
        ('exhausted', 'Retries Exhausted'),
    ], readonly=True, index=True)
    priority = fields.Integer(readonly=True, index=True)
    progress = fields.Float(readonly=True)
    records_processed = fields.Integer(readonly=True)
    records_failed = fields.Integer(readonly=True)
    retry_count = fields.Integer(readonly=True)
    max_retries = fields.Integer(readonly=True)
    next_retry_at = fields.Datetime(readonly=True, index=True)
    started_at = fields.Datetime(readonly=True, index=True)
    last_activity_at = fields.Datetime(readonly=True, index=True)
    finished_at = fields.Datetime(readonly=True, index=True)
    duration_seconds = fields.Float(readonly=True)
    last_error_code = fields.Char(readonly=True)
    last_error_message = fields.Text(readonly=True)
    amazon_request_id = fields.Char(readonly=True)
    responsible_user_id = fields.Many2one('res.users', readonly=True, index=True)
    error_category = fields.Selection(ERROR_CATEGORIES, readonly=True, index=True)
    retry_safe = fields.Boolean(readonly=True, index=True)
    waiting_reason = fields.Selection([
        ('actively_running', 'Actively Running'),
        ('waiting_amazon', 'Waiting for Amazon'),
        ('waiting_rate_limit', 'Waiting for Rate Limit'),
        ('abandoned', 'Abandoned'),
        ('unclear', 'Unclear'),
    ], readonly=True, index=True)
    severity = fields.Selection([
        ('info', 'Info'), ('warning', 'Warning'), ('error', 'Error'), ('critical', 'Critical'),
    ], readonly=True, index=True)
    control_id = fields.Many2one('amazon.operation.control', readonly=True, index=True)

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None, *,
                active_test=True, bypass_access=False):
        # This SQL view depends on four models that the ORM cannot infer from
        # field metadata. Flush their deferred writes before querying the view.
        for model_name in SOURCE_MODELS:
            self.env[model_name].flush_model()
        self.env['amazon.operation.control'].flush_model()
        return super()._search(
            domain, offset=offset, limit=limit, order=order,
            active_test=active_test, bypass_access=bypass_access,
        )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW amazon_operation_job_monitor AS (
                SELECT j.id * 10 + 1 AS id,
                       'amazon.order.import.job'::varchar AS source_model,
                       j.id AS source_id,
                       ('Order Import #' || j.id)::varchar AS source_name,
                       j.instance_id, i.company_id,
                       'order_import'::varchar AS job_type,
                       CASE WHEN c.state IN ('retry_pending','manual_review','cancelled','exhausted') THEN c.state
                            WHEN j.state = 'draft' THEN 'pending' ELSE j.state END::varchar AS state,
                       COALESCE(c.priority, 50) AS priority,
                       CASE WHEN j.state = 'done' THEN 100.0
                            WHEN j.total_found > 0 THEN LEAST(100.0, j.total_processed * 100.0 / j.total_found)
                            WHEN j.state = 'running' THEN 10.0 ELSE 0.0 END AS progress,
                       j.total_processed AS records_processed,
                       j.total_failed AS records_failed,
                       COALESCE(c.retry_count, 0) AS retry_count,
                       COALESCE(c.max_retries, i.maximum_automatic_retries, 5) AS max_retries,
                       COALESCE(c.next_retry_at, j.next_run_at) AS next_retry_at,
                       j.started_at,
                       COALESCE(j.last_activity_at, j.write_date, j.create_date) AS last_activity_at,
                       j.finished_at,
                       COALESCE(j.duration_seconds, 0.0) AS duration_seconds,
                       COALESCE(c.last_error_code, j.last_error_code) AS last_error_code,
                       COALESCE(c.last_error_message, j.last_error_message, j.error_message) AS last_error_message,
                       COALESCE(c.last_amazon_request_id, j.amazon_request_id) AS amazon_request_id,
                       COALESCE(c.responsible_user_id, j.responsible_user_id, j.create_uid) AS responsible_user_id,
                       c.error_category, COALESCE(c.retry_safe, FALSE) AS retry_safe,
                       c.waiting_reason, COALESCE(c.severity, 'info')::varchar AS severity,
                       c.id AS control_id
                  FROM amazon_order_import_job j
                  JOIN amazon_instance i ON i.id = j.instance_id
             LEFT JOIN amazon_operation_control c
                    ON c.source_model = 'amazon.order.import.job' AND c.source_id = j.id
                UNION ALL
                SELECT j.id * 10 + 2, 'amazon.order.status.sync.job', j.id,
                       ('Order Status #' || j.id)::varchar, j.instance_id, i.company_id,
                       'order_status_sync',
                       CASE WHEN c.state IN ('retry_pending','manual_review','cancelled','exhausted') THEN c.state
                            WHEN j.state IN ('draft','pending') THEN 'pending' ELSE j.state END,
                       COALESCE(c.priority, 50),
                       CASE WHEN j.state = 'done' THEN 100.0
                            WHEN j.total_fetched > 0 THEN LEAST(100.0, j.total_processed * 100.0 / j.total_fetched)
                            WHEN j.state = 'running' THEN 10.0 ELSE 0.0 END,
                       j.total_processed, j.total_failed,
                       COALESCE(c.retry_count, j.retry_count, 0),
                       COALESCE(c.max_retries, i.maximum_automatic_retries, 5),
                       COALESCE(c.next_retry_at, j.next_run_at), j.started_at,
                       COALESCE(j.last_activity_at, j.write_date, j.create_date), j.finished_at,
                       COALESCE(j.duration_seconds, 0.0),
                       COALESCE(c.last_error_code, j.last_error_code),
                       COALESCE(c.last_error_message, j.last_error_message, j.error_message),
                       COALESCE(c.last_amazon_request_id, j.amazon_request_id),
                       COALESCE(c.responsible_user_id, j.responsible_user_id, j.create_uid),
                       c.error_category, COALESCE(c.retry_safe, FALSE), c.waiting_reason,
                       COALESCE(c.severity, 'info'), c.id
                  FROM amazon_order_status_sync_job j
                  JOIN amazon_instance i ON i.id = j.instance_id
             LEFT JOIN amazon_operation_control c
                    ON c.source_model = 'amazon.order.status.sync.job' AND c.source_id = j.id
                UNION ALL
                SELECT j.id * 10 + 3, 'amazon.inbound.operation.job', j.id,
                       (COALESCE(s.name, 'Inbound') || ' / ' || j.operation_type)::varchar,
                       j.instance_id, j.company_id, j.operation_type,
                       CASE WHEN c.state IN ('retry_pending','manual_review','cancelled','exhausted') THEN c.state
                            WHEN j.state = 'pending' THEN 'pending'
                            WHEN j.state = 'in_progress' THEN 'running'
                            WHEN j.state = 'done' THEN 'done' ELSE 'failed' END,
                       COALESCE(c.priority, 50),
                       CASE WHEN j.state = 'done' THEN 100.0 WHEN j.state = 'in_progress' THEN 50.0 ELSE 0.0 END,
                       0, CASE WHEN j.state = 'failed' THEN 1 ELSE 0 END,
                       COALESCE(c.retry_count, j.retry_count, 0),
                       COALESCE(c.max_retries, j.max_retries, i.maximum_automatic_retries, 5),
                       COALESCE(c.next_retry_at, j.next_run_at), j.started_at,
                       COALESCE(j.last_activity_at, j.write_date, j.create_date), j.finished_at,
                       CASE WHEN j.started_at IS NULL THEN 0.0
                            ELSE EXTRACT(EPOCH FROM (COALESCE(j.finished_at, now()) - j.started_at)) END,
                       COALESCE(c.last_error_code, NULL),
                       COALESCE(c.last_error_message, j.last_error),
                       COALESCE(c.last_amazon_request_id, j.amazon_request_id),
                       COALESCE(c.responsible_user_id, j.responsible_user_id, j.create_uid),
                       c.error_category, COALESCE(c.retry_safe, FALSE), c.waiting_reason,
                       COALESCE(c.severity, 'info'), c.id
                  FROM amazon_inbound_operation_job j
                  JOIN amazon_instance i ON i.id = j.instance_id
                  JOIN amazon_inbound_shipment s ON s.id = j.inbound_shipment_id
             LEFT JOIN amazon_operation_control c
                    ON c.source_model = 'amazon.inbound.operation.job' AND c.source_id = j.id
                UNION ALL
                SELECT j.id * 10 + 4, 'amazon.inventory.reconciliation.run', j.id,
                       j.name::varchar, j.instance_id, j.company_id, 'inventory_reconciliation',
                       CASE WHEN c.state IN ('retry_pending','manual_review','cancelled','exhausted') THEN c.state
                            WHEN j.state = 'queued' THEN 'pending'
                            WHEN j.state = 'running' THEN 'running'
                            WHEN j.state = 'completed' THEN 'done' ELSE 'failed' END,
                       COALESCE(c.priority, 50),
                       CASE WHEN j.state = 'completed' THEN 100.0 WHEN j.state = 'running' THEN 50.0 ELSE 0.0 END,
                       j.products_checked, j.mismatch_count,
                       COALESCE(c.retry_count, j.retry_count, 0),
                       COALESCE(c.max_retries, j.max_retries, i.maximum_automatic_retries, 5),
                       COALESCE(c.next_retry_at, j.next_run_at), j.started_at,
                       COALESCE(j.last_activity_at, j.write_date, j.create_date), j.finished_at,
                       CASE WHEN j.started_at IS NULL THEN 0.0
                            ELSE EXTRACT(EPOCH FROM (COALESCE(j.finished_at, now()) - j.started_at)) END,
                       COALESCE(c.last_error_code, NULL),
                       COALESCE(c.last_error_message, j.last_error),
                       COALESCE(c.last_amazon_request_id, j.amazon_request_id),
                       COALESCE(c.responsible_user_id, j.responsible_user_id, j.create_uid),
                       c.error_category, COALESCE(c.retry_safe, FALSE), c.waiting_reason,
                       COALESCE(c.severity, 'info'), c.id
                  FROM amazon_inventory_reconciliation_run j
                  JOIN amazon_instance i ON i.id = j.instance_id
             LEFT JOIN amazon_operation_control c
                    ON c.source_model = 'amazon.inventory.reconciliation.run' AND c.source_id = j.id
            )
        """)

    def _controls(self):
        controls = self.env['amazon.operation.control']
        for monitor in self:
            controls |= monitor.control_id or controls.get_or_create_for_source(
                monitor.source_model, monitor.source_id,
            )
        return controls

    def action_retry_selected(self):
        return self._controls().action_retry()

    @api.model
    def action_retry_all_eligible(self):
        controls = self.env['amazon.operation.control'].search([
            ('state', 'in', ('retry_pending', 'exhausted')),
            ('retry_safe', '=', True),
            ('error_category', 'in', ('transient', 'rate_limit', 'amazon_service')),
        ])
        # The SQL-style dynamic retry_count < max_retries condition is enforced
        # again by _queue_source; this search only narrows the candidate set.
        controls = controls.filtered(lambda control: control.retry_count < control.max_retries)
        return controls.action_retry() if controls else True

    def action_cancel_pending_retry(self):
        return self._controls().action_cancel_pending_retry()

    def action_mark_manual_review(self):
        return self._controls().action_mark_manual_review()

    def action_open_source(self):
        self.ensure_one()
        return (self.control_id or self._controls()).action_open_source()

    def action_open_sync_log(self):
        self.ensure_one()
        return (self.control_id or self._controls()).action_open_sync_log()


class AmazonAPIOperationMetric(models.Model):
    """Stored 24-hour API aggregates refreshed by the dashboard cron."""

    _name = 'amazon.api.operation.metric'
    _description = 'Amazon API Operation Metric'
    _order = 'throttle_rate_24h desc, throttle_count_24h desc, operation_name'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    operation_name = fields.Char(required=True, readonly=True, index=True)
    request_count_24h = fields.Integer(readonly=True)
    error_count_24h = fields.Integer(readonly=True)
    throttle_count_1h = fields.Integer(readonly=True)
    throttle_count_24h = fields.Integer(readonly=True)
    throttle_rate_24h = fields.Float(readonly=True, aggregator='avg')
    average_retry_delay = fields.Float(readonly=True, aggregator='avg')
    last_throttled_at = fields.Datetime(readonly=True, index=True)
    repeated_throttling = fields.Boolean(readonly=True, index=True)
    generated_at = fields.Datetime(readonly=True, index=True)

    _unique_instance_operation = models.Constraint(
        'UNIQUE (instance_id, operation_name)',
        'Only one current API metric can exist per instance and operation.',
    )

    @api.model
    def refresh_metrics(self, instances=None):
        instances = instances or self.env['amazon.instance'].sudo().search([
            ('active', '=', True),
        ])
        instances = instances.sudo().exists()
        if not instances:
            return 0
        now = fields.Datetime.now()
        one_hour = now - timedelta(hours=1)
        one_day = now - timedelta(hours=24)
        self.env['amazon.sync.log'].flush_model([
            'instance_id', 'operation', 'operation_name', 'endpoint', 'state',
            'is_throttled', 'retry_after_seconds', 'create_date', 'finished_at',
        ])
        self.env.cr.execute("""
            SELECT instance_id,
                   COALESCE(NULLIF(operation_name, ''), NULLIF(endpoint, ''), 'unknown') AS operation_key,
                   COUNT(*) AS request_count_24h,
                   COUNT(*) FILTER (WHERE state = 'failed') AS error_count_24h,
                   COUNT(*) FILTER (
                       WHERE is_throttled IS TRUE AND create_date >= %s
                   ) AS throttle_count_1h,
                   COUNT(*) FILTER (WHERE is_throttled IS TRUE) AS throttle_count_24h,
                   CASE WHEN COUNT(*) = 0 THEN 0.0
                        ELSE 100.0 * COUNT(*) FILTER (WHERE is_throttled IS TRUE) / COUNT(*)
                    END AS throttle_rate_24h,
                   COALESCE(AVG(NULLIF(retry_after_seconds, 0.0)) FILTER (
                       WHERE is_throttled IS TRUE
                   ), 0.0) AS average_retry_delay,
                   MAX(COALESCE(finished_at, create_date)) FILTER (
                       WHERE is_throttled IS TRUE
                   ) AS last_throttled_at
              FROM amazon_sync_log
             WHERE operation = 'api_request'
               AND create_date >= %s
               AND instance_id = ANY(%s)
             GROUP BY instance_id, operation_key
        """, [one_hour, one_day, instances.ids])
        rows = self.env.cr.fetchall()
        existing = self.sudo().search([('instance_id', 'in', instances.ids)])
        by_key = {(metric.instance_id.id, metric.operation_name): metric for metric in existing}
        seen = set()
        for row in rows:
            key = (row[0], row[1])
            seen.add(key)
            values = {
                'instance_id': row[0],
                'operation_name': row[1],
                'request_count_24h': row[2],
                'error_count_24h': row[3],
                'throttle_count_1h': row[4],
                'throttle_count_24h': row[5],
                'throttle_rate_24h': row[6],
                'average_retry_delay': row[7],
                'last_throttled_at': row[8],
                'repeated_throttling': row[4] >= 3,
                'generated_at': now,
            }
            metric = by_key.get(key)
            if metric:
                metric.write(values)
            else:
                self.sudo().create(values)
        existing.filtered(
            lambda metric: (metric.instance_id.id, metric.operation_name) not in seen
        ).unlink()
        return len(rows)


class AmazonOperationsDashboard(models.Model):
    _name = 'amazon.operations.dashboard'
    _description = 'Amazon Operations Dashboard Snapshot'
    _order = 'connection_health, instance_id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    connection_health = fields.Selection(
        related='instance_id.connection_health', store=True, readonly=True, index=True,
    )
    health_score = fields.Integer(related='instance_id.health_score', readonly=True)
    health_summary = fields.Text(related='instance_id.health_summary', readonly=True)
    running_jobs = fields.Integer(readonly=True)
    failed_jobs = fields.Integer(readonly=True)
    waiting_retry = fields.Integer(readonly=True)
    orders_imported_today = fields.Integer(readonly=True)
    inbound_in_progress = fields.Integer(readonly=True)
    inbound_blocked = fields.Integer(readonly=True)
    inventory_mismatches = fields.Integer(readonly=True)
    critical_inventory_differences = fields.Integer(readonly=True)
    api_errors_24h = fields.Integer(readonly=True)
    rate_limits_1h = fields.Integer(readonly=True)
    rate_limits_24h = fields.Integer(readonly=True)
    average_retry_delay = fields.Float(readonly=True)
    last_successful_sync_at = fields.Datetime(readonly=True)
    generated_at = fields.Datetime(readonly=True, index=True)
    api_operation_metric_ids = fields.One2many(
        related='instance_id.api_operation_metric_ids', readonly=True,
    )

    _unique_instance = models.Constraint(
        'UNIQUE (instance_id)',
        'Only one operations dashboard snapshot can exist per Amazon instance.',
    )

    def _refresh_snapshot(self):
        now = fields.Datetime.now()
        self.env['amazon.operation.control'].sudo().sync_existing_failure_controls()
        for dashboard in self:
            instance = dashboard.instance_id
            jobs = self.env['amazon.operation.job.monitor'].sudo()
            job_domain = [('instance_id', '=', instance.id)]
            api_logs = self.env['amazon.sync.log'].sudo()
            metric_model = self.env['amazon.api.operation.metric'].sudo()
            metric_model.refresh_metrics(instance)
            metrics = metric_model.search([('instance_id', '=', instance.id)])
            last_success = api_logs.search([
                ('instance_id', '=', instance.id),
                ('state', '=', 'success'),
                ('operation', '=', 'api_request'),
            ], order='finished_at desc, id desc', limit=1)
            inbound_states = [
                'plan_created', 'packing_generated', 'packing_confirmed',
                'placement_generated', 'placement_confirmed', 'picking_created',
                'ready_to_ship', 'shipment_confirmed', 'waiting_receiving',
                'partially_received',
            ]
            throttle_count = sum(metrics.mapped('throttle_count_24h'))
            weighted_retry_delay = sum(
                metric.average_retry_delay * metric.throttle_count_24h
                for metric in metrics
            )
            dashboard.write({
                'running_jobs': jobs.search_count(job_domain + [('state', '=', 'running')]),
                'failed_jobs': jobs.search_count(job_domain + [
                    ('state', 'in', ('failed', 'exhausted', 'manual_review')),
                ]),
                'waiting_retry': jobs.search_count(job_domain + [('state', '=', 'retry_pending')]),
                'orders_imported_today': self.env['amazon.sale.order'].sudo().search_count([
                    ('instance_id', '=', instance.id),
                    ('create_date', '>=', now.replace(hour=0, minute=0, second=0, microsecond=0)),
                ]),
                'inbound_in_progress': self.env['amazon.inbound.shipment'].sudo().search_count([
                    ('instance_id', '=', instance.id), ('state', 'in', inbound_states),
                ]),
                'inbound_blocked': self.env['amazon.operation.control'].sudo().search_count([
                    ('instance_id', '=', instance.id),
                    ('source_model', '=', 'amazon.inbound.operation.job'),
                    ('state', 'in', ('manual_review', 'exhausted')),
                ]),
                'inventory_mismatches': self.env['amazon.inventory.reconciliation'].sudo().search_count([
                    ('instance_id', '=', instance.id), ('status', '!=', 'matched'),
                ]),
                'critical_inventory_differences': self.env['amazon.inventory.reconciliation'].sudo().search_count([
                    ('instance_id', '=', instance.id), ('severity', '=', 'critical'),
                    ('status', 'not in', ('matched', 'ignored', 'applied')),
                ]),
                'api_errors_24h': sum(metrics.mapped('error_count_24h')),
                'rate_limits_1h': sum(metrics.mapped('throttle_count_1h')),
                'rate_limits_24h': throttle_count,
                'average_retry_delay': (
                    weighted_retry_delay / throttle_count if throttle_count else 0.0
                ),
                'last_successful_sync_at': last_success.finished_at or last_success.create_date,
                'generated_at': now,
            })
        return True

    @api.model
    def cron_refresh_operations_dashboard(self):
        dashboards = self.sudo()
        for instance in self.env['amazon.instance'].sudo().search([('active', '=', True)]):
            dashboard = self.search([('instance_id', '=', instance.id)], limit=1)
            dashboards |= dashboard or self.create({'instance_id': instance.id})
        dashboards._refresh_snapshot()
        return len(dashboards)

    def action_refresh(self):
        self._refresh_snapshot()
        return True

    def action_run_health_check(self):
        self.ensure_one()
        result = self.instance_id.action_run_health_check()
        self._refresh_snapshot()
        return result

    def action_open_amazon_health_dashboard(self):
        self.ensure_one()
        return self.instance_id.action_open_amazon_health_dashboard()

    def _job_action(self, domain, name):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': name,
            'res_model': 'amazon.operation.job.monitor', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.instance_id.id)] + domain,
            'context': {'create': False},
        }

    def action_open_running_jobs(self):
        return self._job_action([('state', '=', 'running')], _('Running Amazon Jobs'))

    def action_open_failed_jobs(self):
        return self._job_action([('state', 'in', ('failed', 'exhausted', 'manual_review'))], _('Failed Amazon Jobs'))

    def action_open_waiting_retry(self):
        return self._job_action([('state', '=', 'retry_pending')], _('Amazon Retry Center'))

    def action_open_inbound_blocked(self):
        return self._job_action([
            ('source_model', '=', 'amazon.inbound.operation.job'),
            ('state', 'in', ('failed', 'exhausted', 'manual_review')),
        ], _('Blocked Inbound Shipments'))

    def action_open_inventory_differences(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Inventory Differences'),
            'res_model': 'amazon.inventory.reconciliation', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.instance_id.id), ('status', '!=', 'matched')],
        }

    def action_open_api_errors(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Amazon API Errors'),
            'res_model': 'amazon.sync.log', 'view_mode': 'list,form',
            'domain': [
                ('instance_id', '=', self.instance_id.id),
                ('operation', '=', 'api_request'), ('state', '=', 'failed'),
                ('create_date', '>=', fields.Datetime.now() - timedelta(hours=24)),
            ],
        }


class AmazonInstanceOperations(models.Model):
    _inherit = 'amazon.instance'

    # Enforce credential secrecy at ORM/RPC level, not only in form views.
    refresh_token = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    client_id = fields.Char(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    client_secret = fields.Char(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    aws_access_key = fields.Char(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    aws_secret_key = fields.Char(groups='sdlc_amazon_connector.group_amazon_technical_admin')

    connection_health = fields.Selection([
        ('healthy', 'Healthy'), ('warning', 'Warning'),
        ('error', 'Error'), ('unknown', 'Unknown'),
    ], default='unknown', readonly=True, index=True)
    last_health_check_at = fields.Datetime(readonly=True, index=True)
    last_successful_api_call_at = fields.Datetime(readonly=True, index=True)
    last_failed_api_call_at = fields.Datetime(readonly=True, index=True)
    consecutive_failure_count = fields.Integer(default=0, readonly=True)
    last_amazon_error_code = fields.Char(readonly=True)
    last_amazon_error_message = fields.Text(readonly=True)
    last_amazon_request_id = fields.Char(readonly=True, index=True)
    token_health = fields.Selection([
        ('healthy', 'Healthy'), ('warning', 'Warning'),
        ('error', 'Error'), ('unknown', 'Unknown'),
    ], default='unknown', readonly=True)
    authorization_health = fields.Selection([
        ('healthy', 'Healthy'), ('warning', 'Warning'),
        ('error', 'Error'), ('unknown', 'Unknown'),
    ], default='unknown', readonly=True)
    rate_limit_health = fields.Selection([
        ('healthy', 'Healthy'), ('warning', 'Warning'),
        ('error', 'Error'), ('unknown', 'Unknown'),
    ], default='unknown', readonly=True)
    last_rate_limit_at = fields.Datetime(readonly=True, index=True)
    health_summary = fields.Text(readonly=True)
    health_score = fields.Integer(default=0, readonly=True)
    external_failure_kind = fields.Selection([
        ('none', 'None'),
        ('local_connector', 'Local Connector Error'),
        ('configuration', 'Local Configuration Error'),
        ('authorization', 'Authorization Error'),
        ('possible_amazon_incident', 'Possible Amazon Service Incident'),
        ('unknown_external', 'Unknown External Failure'),
    ], default='none', readonly=True, index=True)

    enable_health_monitoring = fields.Boolean(default=True)
    health_check_interval_minutes = fields.Integer(default=60)
    enable_operational_alerts = fields.Boolean(default=True)
    consecutive_failure_alert_threshold = fields.Integer(default=5)
    stuck_job_threshold_minutes = fields.Integer(default=60)
    maximum_automatic_retries = fields.Integer(default=5)
    retry_backoff_base_seconds = fields.Integer(default=60)
    inventory_mismatch_alert_threshold = fields.Float(default=10.0)
    receiving_discrepancy_alert_threshold = fields.Float(default=5.0)
    job_retention_days = fields.Integer(default=365)
    log_retention_days = fields.Integer(default=90)
    operations_dashboard_id = fields.Many2one(
        'amazon.operations.dashboard', compute='_compute_operations_dashboard',
    )
    api_operation_metric_ids = fields.One2many(
        'amazon.api.operation.metric', 'instance_id', readonly=True,
    )

    @api.constrains(
        'health_check_interval_minutes', 'consecutive_failure_alert_threshold',
        'stuck_job_threshold_minutes', 'maximum_automatic_retries',
        'retry_backoff_base_seconds', 'inventory_mismatch_alert_threshold',
        'receiving_discrepancy_alert_threshold', 'job_retention_days',
        'log_retention_days',
    )
    def _check_operational_settings(self):
        for instance in self:
            positive = {
                _('Health Check Interval'): instance.health_check_interval_minutes,
                _('Failure Alert Threshold'): instance.consecutive_failure_alert_threshold,
                _('Stuck Job Threshold'): instance.stuck_job_threshold_minutes,
                _('Maximum Automatic Retries'): instance.maximum_automatic_retries,
                _('Retry Backoff Base'): instance.retry_backoff_base_seconds,
                _('Job Retention Days'): instance.job_retention_days,
                _('Log Retention Days'): instance.log_retention_days,
            }
            invalid = [name for name, value in positive.items() if value < 1]
            if invalid:
                raise ValidationError(_("Operational settings must be positive: %s", ', '.join(invalid)))
            if instance.maximum_automatic_retries > 100:
                raise ValidationError(_("Maximum Automatic Retries cannot exceed 100."))
            if instance.inventory_mismatch_alert_threshold < 0 or instance.receiving_discrepancy_alert_threshold < 0:
                raise ValidationError(_("Operational quantity thresholds cannot be negative."))
            if instance.log_retention_days < 30:
                raise ValidationError(_("Amazon API logs must be retained for at least 30 days."))

    def _compute_operations_dashboard(self):
        dashboards = self.env['amazon.operations.dashboard'].sudo().search([
            ('instance_id', 'in', self.ids),
        ])
        mapped = {record.instance_id.id: record for record in dashboards}
        for instance in self:
            instance.operations_dashboard_id = mapped.get(instance.id)

    def _health_values(self):
        self.ensure_one()
        score = 0
        score += {'healthy': 25, 'warning': 12}.get(self.token_health, 0)
        score += {'healthy': 35, 'warning': 17}.get(self.authorization_health, 0)
        score += {'healthy': 20, 'warning': 10}.get(self.rate_limit_health, 0)
        if self.last_successful_api_call_at:
            score += 20
        if self.external_failure_kind == 'possible_amazon_incident':
            summary = _(
                'Authentication is valid, but multiple Amazon operations returned 5xx responses. Check the official SP-API Health Dashboard.'
            )
            health = 'warning'
        elif self.authorization_health == 'error' or self.token_health == 'error':
            summary = _('Amazon authorization is invalid or revoked. Re-authorize the application.')
            health = 'error'
        elif self.consecutive_failure_count >= self.consecutive_failure_alert_threshold:
            summary = _('%s consecutive Amazon API calls failed.', self.consecutive_failure_count)
            health = 'error'
        elif self.rate_limit_health == 'warning':
            summary = _('Amazon access is available, but recent requests were throttled. Queued jobs will honor backoff.')
            health = 'warning'
        elif self.authorization_health == 'healthy' and self.token_health == 'healthy':
            summary = _('Amazon token and lightweight authorized API call are healthy.')
            health = 'healthy'
        else:
            summary = _('Amazon connection health has not been verified yet.')
            health = 'unknown'
        return {'health_score': min(score, 100), 'health_summary': summary, 'connection_health': health}

    def _record_api_outcome(self, log):
        for instance in self:
            now = log.finished_at or fields.Datetime.now()
            endpoint = log.endpoint or ''
            is_token = '/auth/o2/token' in endpoint
            values = {'last_amazon_request_id': log.amazon_request_id or instance.last_amazon_request_id}
            if log.state == 'success':
                values.update({
                    'last_successful_api_call_at': now,
                    'consecutive_failure_count': 0,
                    'last_amazon_error_code': False,
                    'last_amazon_error_message': False,
                    'external_failure_kind': 'none',
                })
                if is_token:
                    values['token_health'] = 'healthy'
                else:
                    values['authorization_health'] = 'healthy'
                if not instance.last_rate_limit_at or instance.last_rate_limit_at < now - timedelta(hours=24):
                    values['rate_limit_health'] = 'healthy'
            else:
                values.update({
                    'last_failed_api_call_at': now,
                    'consecutive_failure_count': instance.consecutive_failure_count + 1,
                    'last_amazon_error_code': log.amazon_error_code or ('HTTP%s' % log.http_status if log.http_status else 'NETWORK'),
                    'last_amazon_error_message': (log.amazon_error_message or log.error_message or '')[:2000],
                })
                if log.error_category == 'authorization':
                    values['token_health' if is_token else 'authorization_health'] = 'error'
                    values['external_failure_kind'] = 'authorization'
                elif log.error_category == 'configuration':
                    values['external_failure_kind'] = 'configuration'
                elif log.error_category == 'amazon_service':
                    recent = self.env['amazon.sync.log'].sudo().search([
                        ('instance_id', '=', instance.id),
                        ('error_category', '=', 'amazon_service'),
                        ('create_date', '>=', now - timedelta(hours=1)),
                    ])
                    operations = set(recent.mapped('operation_name'))
                    values['external_failure_kind'] = (
                        'possible_amazon_incident'
                        if len(operations) >= 2 and instance.authorization_health == 'healthy'
                        else 'unknown_external'
                    )
                else:
                    values['external_failure_kind'] = 'local_connector' if not log.http_status else 'unknown_external'
            if log.is_throttled:
                values.update({
                    'last_rate_limit_at': now,
                    'rate_limit_health': 'warning',
                })
            instance.with_context(skip_health_refresh=True).write(values)
            instance.write(instance._health_values())
        return True

    def _record_health_configuration_failure(self, message):
        self.ensure_one()
        self.write({
            'last_health_check_at': fields.Datetime.now(),
            'connection_health': 'error',
            'external_failure_kind': 'configuration',
            'health_score': 0,
            'health_summary': str(message)[:2000],
            'last_amazon_error_code': 'CONFIGURATION',
            'last_amazon_error_message': str(message)[:2000],
        })

    def _run_health_check(self):
        self.ensure_one()
        now = fields.Datetime.now()
        self.write({'last_health_check_at': now})
        try:
            self._check_required_fields()
        except Exception as exc:
            self._record_health_configuration_failure(exc)
            return False
        api = AmazonAPI()
        scoped = self.with_context(
            amazon_source_model='amazon.instance',
            amazon_source_id=self.id,
            amazon_operation='health_access_token',
        )
        try:
            access_token = api.get_access_token(scoped)
            scoped = scoped.with_context(amazon_operation='getMarketplaceParticipations')
            response = api.get_marketplace_participations(scoped, access_token)
            payload = response.get('payload') if isinstance(response, dict) else None
            if not isinstance(payload, list):
                raise ValidationError(_("Amazon Sellers API returned an invalid marketplace participation payload."))
            self.write({
                'last_health_check_at': fields.Datetime.now(),
                'token_health': 'healthy',
                'authorization_health': 'healthy',
                'external_failure_kind': 'none',
            })
            self.write(self._health_values())
            return True
        except Exception as exc:
            _logger.warning('Amazon health check failed for instance %s: %s', self.id, exc)
            # The API logger already records HTTP failures.  Non-HTTP validation
            # failures are persisted here without exposing credentials.
            if not isinstance(exc, requests.exceptions.RequestException):
                self.write({
                    'last_failed_api_call_at': fields.Datetime.now(),
                    'consecutive_failure_count': self.consecutive_failure_count + 1,
                    'last_amazon_error_code': 'HEALTH_CHECK_FAILED',
                    'last_amazon_error_message': str(exc)[:2000],
                    'external_failure_kind': 'local_connector',
                })
                self.write(self._health_values())
            return False

    def action_run_health_check(self):
        if not self.env.user.has_group('sdlc_amazon_connector.group_amazon_technical_admin'):
            raise AccessError(_("Only an Amazon Technical Administrator can run a health check."))
        self.ensure_one()
        healthy = self._run_health_check()
        return self._notify(
            _('Amazon Health Check'), self.health_summary,
            'success' if healthy else 'danger', sticky=not healthy,
        )

    @api.model
    def cron_run_health_checks(self):
        now = fields.Datetime.now()
        instances = self.sudo().search([
            ('active', '=', True), ('enable_health_monitoring', '=', True),
        ], order='last_health_check_at asc, id asc')
        checked = 0
        for instance in instances:
            interval = timedelta(minutes=instance.health_check_interval_minutes or 60)
            if instance.last_health_check_at and instance.last_health_check_at > now - interval:
                continue
            instance._run_health_check()
            checked += 1
        return checked

    def action_open_operations_dashboard(self):
        self.ensure_one()
        dashboard = self.env['amazon.operations.dashboard'].sudo().search([
            ('instance_id', '=', self.id),
        ], limit=1)
        if not dashboard:
            dashboard = self.env['amazon.operations.dashboard'].sudo().create({
                'instance_id': self.id,
            })
            dashboard._refresh_snapshot()
        return {
            'type': 'ir.actions.act_window', 'name': _('Amazon Operations'),
            'res_model': 'amazon.operations.dashboard', 'view_mode': 'form',
            'res_id': dashboard.id,
        }

    def action_open_amazon_health_dashboard(self):
        return {
            'type': 'ir.actions.act_url',
            'url': 'https://sellercentral.amazon.com/sp-api-status',
            'target': 'new',
        }


def _tracking_values(record, values):
    values = dict(values)
    if any(key in values for key in (
        'state', 'next_run_at', 'next_token', 'last_error', 'last_error_message',
        'error_message', 'retry_count', 'total_processed', 'total_failed',
    )):
        values.setdefault('last_activity_at', fields.Datetime.now())
    if values.get('state') in ('running', 'in_progress') and not record.started_at:
        values.setdefault('started_at', fields.Datetime.now())
    if values.get('state') in ('done', 'completed', 'failed', 'partial'):
        values.setdefault('finished_at', fields.Datetime.now())
    return values


def _after_job_write(records, old_states):
    if records.env.context.get('skip_amazon_operation_tracking'):
        return
    controls = records.env['amazon.operation.control'].sudo()
    for record in records:
        old_state = old_states.get(record.id)
        if record.state == 'failed' and old_state != 'failed':
            controls.record_source_failure(record)
        elif record.state == 'partial' and old_state != 'partial':
            controls.record_source_failure(record, partial=True)
        elif record.state in ('done', 'completed') and old_state not in ('done', 'completed'):
            control = controls.search([
                ('source_model', '=', record._name), ('source_id', '=', record.id),
            ], limit=1)
            if control:
                control.mark_source_resolved()


class AmazonOrderImportJobOperations(models.Model):
    _inherit = 'amazon.order.import.job'

    last_activity_at = fields.Datetime(default=fields.Datetime.now, index=True)
    responsible_user_id = fields.Many2one('res.users', default=lambda self: self.env.user, index=True)
    amazon_request_id = fields.Char(index=True, copy=False)
    started_at = fields.Datetime(index=True)
    finished_at = fields.Datetime(index=True)

    def write(self, values):
        old_states = {record.id: record.state for record in self}
        values = _tracking_values(self[:1], values) if self else values
        result = super().write(values)
        _after_job_write(self, old_states)
        return result


class AmazonOrderStatusSyncJobOperations(models.Model):
    _inherit = 'amazon.order.status.sync.job'

    last_activity_at = fields.Datetime(default=fields.Datetime.now, index=True)
    responsible_user_id = fields.Many2one('res.users', default=lambda self: self.env.user, index=True)
    amazon_request_id = fields.Char(index=True, copy=False)
    started_at = fields.Datetime(index=True)
    finished_at = fields.Datetime(index=True)

    def write(self, values):
        old_states = {record.id: record.state for record in self}
        values = _tracking_values(self[:1], values) if self else values
        result = super().write(values)
        _after_job_write(self, old_states)
        return result


class AmazonInboundOperationJobOperations(models.Model):
    _inherit = 'amazon.inbound.operation.job'

    last_activity_at = fields.Datetime(default=fields.Datetime.now, index=True)
    responsible_user_id = fields.Many2one('res.users', default=lambda self: self.env.user, index=True)
    started_at = fields.Datetime(index=True)
    finished_at = fields.Datetime(index=True)

    def write(self, values):
        old_states = {record.id: record.state for record in self}
        values = _tracking_values(self[:1], values) if self else values
        result = super().write(values)
        _after_job_write(self, old_states)
        return result


class AmazonInventoryReconciliationRunOperations(models.Model):
    _inherit = 'amazon.inventory.reconciliation.run'

    last_activity_at = fields.Datetime(default=fields.Datetime.now, index=True)
    responsible_user_id = fields.Many2one('res.users', default=lambda self: self.env.user, index=True)
    amazon_request_id = fields.Char(index=True, copy=False)
    started_at = fields.Datetime(index=True)
    finished_at = fields.Datetime(index=True)

    def write(self, values):
        old_states = {record.id: record.state for record in self}
        values = _tracking_values(self[:1], values) if self else values
        result = super().write(values)
        _after_job_write(self, old_states)
        return result


class AmazonSyncLogOperations(models.Model):
    _inherit = 'amazon.sync.log'

    request_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    response_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')

    operation = fields.Selection(selection_add=[
        ('job_failure', 'Job Failure'),
        ('job_retry', 'Job Retry'),
        ('health_check', 'Health Check'),
    ], ondelete={
        'job_failure': 'cascade',
        'job_retry': 'cascade',
        'health_check': 'cascade',
    })


class AmazonSmartAlertOperations(models.Model):
    _inherit = 'amazon.smart.alert'

    alert_type = fields.Selection(selection_add=[
        ('authorization_revoked', 'Authorization Revoked'),
        ('consecutive_api_failures', 'Consecutive API Failures'),
        ('stuck_job', 'Stuck Job'),
        ('inbound_blocked', 'Inbound Shipment Blocked'),
        ('receiving_discrepancy', 'Receiving Discrepancy'),
        ('critical_inventory_mismatch', 'Critical Inventory Mismatch'),
        ('repeated_throttling', 'Repeated HTTP 429'),
        ('retry_exhausted', 'Automatic Retries Exhausted'),
        ('possible_amazon_incident', 'Possible Amazon Service Incident'),
    ], ondelete={
        'authorization_revoked': 'cascade',
        'consecutive_api_failures': 'cascade',
        'stuck_job': 'cascade',
        'inbound_blocked': 'cascade',
        'receiving_discrepancy': 'cascade',
        'critical_inventory_mismatch': 'cascade',
        'repeated_throttling': 'cascade',
        'retry_exhausted': 'cascade',
        'possible_amazon_incident': 'cascade',
    })
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    issue_key = fields.Char(index=True, copy=False)
    is_operational = fields.Boolean(default=False, index=True)
    source_model = fields.Char(index=True)
    source_id = fields.Integer(index=True)
    amazon_request_id = fields.Char(index=True)
    error_category = fields.Selection(ERROR_CATEGORIES, index=True)

    _unique_issue_key = models.Constraint(
        'UNIQUE (issue_key)',
        'An operational issue can have only one alert lifecycle.',
    )

    @api.model
    def _upsert_operational_alert(self, instance, issue_key, alert_type, severity,
                                  title, description, suggested_action,
                                  source_model=False, source_id=0,
                                  request_id=False, category=False):
        alert = self.sudo().search([('issue_key', '=', issue_key)], limit=1)
        values = {
            'instance_id': instance.id,
            'issue_key': issue_key,
            'alert_type': alert_type,
            'severity': severity,
            'title': title,
            'description': description,
            'suggested_action': suggested_action,
            'is_operational': True,
            'source_model': source_model or False,
            'source_id': source_id or 0,
            'amazon_request_id': request_id or False,
            'error_category': category or False,
            'state': 'new',
            'resolved_at': False,
            'resolved_by': False,
        }
        created_or_reopened = not alert or alert.state in ('resolved', 'dismissed')
        if alert:
            alert.write(values)
        else:
            alert = self.sudo().create(values)
        if created_or_reopened and instance.enable_operational_alerts:
            alert.message_post(body=description)
            if severity in ('3_urgent', '4_critical'):
                managers = self.env.ref(
                    'sdlc_amazon_connector.group_amazon_manager'
                ).user_ids.filtered(lambda user: (
                    user.active and instance.company_id in user.company_ids
                ))
                if managers:
                    alert.activity_schedule(
                        'mail.mail_activity_data_todo',
                        user_id=managers[0].id,
                        summary=title,
                        note=suggested_action,
                    )
        return alert

    @api.model
    def cron_evaluate_operational_alerts(self):
        now = fields.Datetime.now()
        active_keys = set()
        for instance in self.env['amazon.instance'].sudo().search([
            ('active', '=', True), ('enable_operational_alerts', '=', True),
        ]):
            def upsert(key, *args, **kwargs):
                active_keys.add(key)
                return self._upsert_operational_alert(instance, key, *args, **kwargs)

            if instance.authorization_health == 'error' or instance.token_health == 'error':
                upsert(
                    'authorization:%s' % instance.id,
                    'authorization_revoked', '4_critical',
                    _('Amazon authorization failed: %s', instance.name),
                    instance.last_amazon_error_message or _('Amazon rejected the connector authorization.'),
                    _('Re-authorize the Amazon application and verify the required application roles.'),
                    request_id=instance.last_amazon_request_id,
                    category='authorization',
                )
            if instance.consecutive_failure_count >= instance.consecutive_failure_alert_threshold:
                upsert(
                    'consecutive_failures:%s' % instance.id,
                    'consecutive_api_failures', '3_urgent',
                    _('%s consecutive Amazon API failures', instance.consecutive_failure_count),
                    instance.health_summary,
                    _('Open the Retry Center, inspect the newest request ID, and check authorization or Amazon service health.'),
                    request_id=instance.last_amazon_request_id,
                    category='unknown',
                )
            throttles = self.env['amazon.sync.log'].sudo().search_count([
                ('instance_id', '=', instance.id), ('is_throttled', '=', True),
                ('create_date', '>=', now - timedelta(hours=1)),
            ])
            if throttles >= 3:
                upsert(
                    'throttling:%s' % instance.id,
                    'repeated_throttling', '3_urgent',
                    _('Repeated Amazon throttling: %s', instance.name),
                    _('%s HTTP 429 responses occurred in the last hour.', throttles),
                    _('Let queued jobs honor Retry-After and reduce or stagger calls for the affected operations.'),
                    category='rate_limit',
                )
            if instance.external_failure_kind == 'possible_amazon_incident':
                upsert(
                    'amazon_incident:%s' % instance.id,
                    'possible_amazon_incident', '3_urgent',
                    _('Possible Amazon SP-API incident'), instance.health_summary,
                    _('Check the official SP-API Health Dashboard; do not change valid local credentials.'),
                    request_id=instance.last_amazon_request_id,
                    category='amazon_service',
                )
            controls = self.env['amazon.operation.control'].sudo().search([
                ('instance_id', '=', instance.id),
                ('state', 'in', ('manual_review', 'exhausted')),
            ])
            for control in controls:
                if control.waiting_reason in ('abandoned', 'unclear'):
                    upsert(
                        'stuck:%s:%s' % (control.source_model, control.source_id),
                        'stuck_job', '3_urgent',
                        _('Stuck Amazon job: %s', control.source_name),
                        control.last_error_message or _('The job has no recent activity.'),
                        control.recommended_action,
                        source_model=control.source_model, source_id=control.source_id,
                        request_id=control.last_amazon_request_id,
                        category=control.error_category,
                    )
                if control.state == 'exhausted':
                    upsert(
                        'retries:%s:%s' % (control.source_model, control.source_id),
                        'retry_exhausted', '4_critical',
                        _('Amazon retries exhausted: %s', control.source_name),
                        control.last_error_message,
                        control.recommended_action,
                        source_model=control.source_model, source_id=control.source_id,
                        request_id=control.last_amazon_request_id,
                        category=control.error_category,
                    )
                if control.source_model == 'amazon.inbound.operation.job':
                    upsert(
                        'inbound:%s' % control.source_id,
                        'inbound_blocked', '3_urgent',
                        _('Inbound operation blocked: %s', control.source_name),
                        control.last_error_message,
                        control.recommended_action,
                        source_model=control.source_model, source_id=control.source_id,
                        request_id=control.last_amazon_request_id,
                        category=control.error_category,
                    )
            critical_lines = self.env['amazon.inventory.reconciliation'].sudo().search([
                ('instance_id', '=', instance.id),
                ('severity', '=', 'critical'),
                ('status', 'not in', ('matched', 'ignored', 'applied')),
            ])
            for line in critical_lines:
                maximum = max(abs(line.difference_sellable), abs(line.difference_reserved),
                              abs(line.difference_unsellable), abs(line.difference_inbound))
                if maximum >= instance.inventory_mismatch_alert_threshold:
                    upsert(
                        'inventory:%s' % line.id,
                        'critical_inventory_mismatch', '4_critical',
                        _('Critical FBA inventory mismatch: %s', line.sku),
                        _('Largest absolute difference is %s units.', maximum),
                        _('Open the reconciliation line and review its suggested action; inventory is not adjusted automatically by monitoring.'),
                        source_model=line._name, source_id=line.id,
                        category='data',
                    )
            discrepancies = self.env['amazon.fba.inventory.discrepancy'].sudo().search([
                ('instance_id', '=', instance.id), ('status', '=', 'open'),
                ('quantity', '>=', instance.receiving_discrepancy_alert_threshold),
            ]) if 'instance_id' in self.env['amazon.fba.inventory.discrepancy']._fields else self.env['amazon.fba.inventory.discrepancy']
            for discrepancy in discrepancies:
                upsert(
                    'receiving:%s' % discrepancy.id,
                    'receiving_discrepancy', '3_urgent',
                    _('Amazon receiving discrepancy'),
                    discrepancy.notes or discrepancy.display_name,
                    _('Review the shipment discrepancy; do not silently reduce transit inventory.'),
                    source_model=discrepancy._name, source_id=discrepancy.id,
                    category='data',
                )

        unresolved = self.sudo().search([
            ('is_operational', '=', True), ('state', 'in', ('new', 'acknowledged')),
        ])
        resolved = unresolved.filtered(lambda alert: alert.issue_key not in active_keys)
        if resolved:
            resolved.write({
                'state': 'resolved', 'resolved_at': now,
                'resolution_note': _('The monitoring condition cleared automatically.'),
            })
            resolved.activity_ids.action_done()
        return len(active_keys)
