import logging
import random
from datetime import timedelta

import requests

from odoo import api, fields, models

from .amazon_api import AmazonAPI, amazon_safe_before_dt, amazon_safe_before_iso, amazon_to_utc_naive

_logger = logging.getLogger(__name__)

MAX_INVALID_BEFORE_RETRIES = 3


def _datetime_to_spapi(value):
    value = amazon_to_utc_naive(value)
    if not value:
        return False
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


class AmazonOrderStatusSyncJob(models.Model):
    _name = 'amazon.order.status.sync.job'
    _description = 'Amazon Order Status Sync Job'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('partial', 'Partial'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)
    fulfillment_channel = fields.Selection([
        ('MFN', 'FBM'),
        ('AFN', 'FBA'),
    ], string='Fulfillment Channel', help="Leave empty to synchronize both FBM and FBA order statuses.")

    date_from = fields.Datetime(index=True)
    date_to = fields.Datetime(index=True)
    effective_date_to = fields.Datetime(
        string='Effective Safe Date To',
        help="Actual UTC LastUpdatedBefore used after applying the 3-minute Amazon safety margin.",
    )
    amazon_request_before = fields.Char(
        string='Amazon Request Before UTC',
        help="Final UTC ISO-8601 timestamp sent to Amazon as LastUpdatedBefore.",
    )
    upper_bound_adjusted = fields.Boolean('Upper Bound Adjusted')
    next_token = fields.Text()
    next_run_at = fields.Datetime(index=True)
    batch_size = fields.Integer(default=10)

    total_fetched = fields.Integer(default=0)
    total_processed = fields.Integer(default=0)
    total_changed = fields.Integer(default=0)
    total_unchanged = fields.Integer(default=0)
    total_missing = fields.Integer(default=0)
    total_failed = fields.Integer(default=0)
    total_rate_limited = fields.Integer(default=0)
    retry_count = fields.Integer(default=0)
    date_bound_retry_count = fields.Integer(default=0)

    last_processed_amazon_order_id = fields.Char()
    last_error_code = fields.Char()
    last_error_message = fields.Text()
    error_message = fields.Text()
    sync_log_id = fields.Many2one('amazon.sync.log', ondelete='set null')
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    duration_seconds = fields.Float(compute='_compute_duration', store=True)

    _batch_size_range = models.Constraint(
        'CHECK (batch_size IS NULL OR (batch_size >= 1 AND batch_size <= 100))',
        'Status sync batch size must be between 1 and 100.',
    )

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for job in self:
            end = job.finished_at or fields.Datetime.now()
            job.duration_seconds = (end - job.started_at).total_seconds() if job.started_at else 0.0

    @api.model
    def _get_amazon_safe_before_dt(self, requested_before=None):
        return amazon_safe_before_dt(requested_before)

    @api.model
    def _get_amazon_safe_before(self, requested_before=None):
        return amazon_safe_before_iso(requested_before)

    @api.model
    def _create_for_instance(self, instance, fulfillment_channel=False, date_from=None, date_to=None):
        instance.ensure_one()
        safe_date_to = self._get_amazon_safe_before_dt(date_to)
        requested_to = amazon_to_utc_naive(date_to)
        if not date_from:
            date_from = self._default_status_date_from(instance, safe_date_to)
        vals = {
            'instance_id': instance.id,
            'fulfillment_channel': fulfillment_channel or False,
            'date_from': date_from,
            'date_to': date_to or safe_date_to,
            'effective_date_to': safe_date_to,
            'amazon_request_before': _datetime_to_spapi(safe_date_to),
            'upper_bound_adjusted': bool(requested_to and requested_to > safe_date_to),
            'batch_size': instance.status_sync_batch_size or 10,
        }
        return self.create(vals)

    @api.model
    def _default_status_date_from(self, instance, safe_date_to):
        overlap = instance.status_sync_lookback_minutes if instance.status_sync_lookback_minutes is not None else 10
        if instance.last_status_sync_at:
            return instance.last_status_sync_at - timedelta(minutes=overlap)

        self.env.cr.execute("""
            SELECT COALESCE(amazon_last_update_date, last_update_date, purchase_date)
              FROM amazon_sale_order
             WHERE instance_id = %s
               AND COALESCE(amazon_last_update_date, last_update_date, purchase_date) IS NOT NULL
             ORDER BY COALESCE(amazon_last_update_date, last_update_date, purchase_date), id
             LIMIT 1
        """, [instance.id])
        row = self.env.cr.fetchone()
        if row and row[0]:
            return row[0] - timedelta(minutes=overlap)
        return safe_date_to - timedelta(days=1)

    @api.model
    def cron_sync_order_statuses(self):
        """Create due jobs for enabled instances, then process one job batch."""
        if self.cron_process_status_sync_jobs():
            return True

        now = fields.Datetime.now()
        instances = self.env['amazon.instance'].search([
            ('order_status_sync_enabled', '=', True),
        ], order='last_status_sync_at asc, id asc')
        for instance in instances:
            if not self.env['amazon.sale.order'].search_count([('instance_id', '=', instance.id)]):
                continue
            active_job = self.search([
                ('instance_id', '=', instance.id),
                ('state', 'in', ('draft', 'pending', 'running')),
            ], limit=1)
            if active_job:
                continue
            interval = instance.order_status_sync_interval or 15
            if instance.last_status_sync_at and instance.last_status_sync_at > now - timedelta(minutes=interval):
                continue
            self._create_for_instance(instance)
            break

        return self.cron_process_status_sync_jobs()

    @api.model
    def cron_process_status_sync_jobs(self):
        """Process one queued/running status-sync job batch with row locking."""
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_order_status_sync_job
             WHERE state IN ('draft', 'pending', 'running')
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, create_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        """, [now])
        row = self.env.cr.fetchone()
        if not row:
            return False
        job = self.browse(row[0]).with_context(
            amazon_source_model=self._name,
            amazon_source_id=row[0],
            amazon_operation='order_status_sync',
        )
        job._process_next_batch()
        return True

    def _ensure_log(self):
        self.ensure_one()
        if self.sync_log_id:
            return self.sync_log_id
        self.sync_log_id = self.env['amazon.sync.log'].log_start(
            self.instance_id,
            'order_status_update',
            request_data={
                'job_id': self.id,
                'date_from': self.date_from,
                'date_to': self.date_to,
                'effective_date_to': self.effective_date_to,
                'amazon_request_before': self.amazon_request_before,
                'batch_size': self.batch_size,
                'fulfillment_channel': self.fulfillment_channel or 'all',
            },
            res_model=self._name,
            res_id=self.id,
        ).id
        return self.sync_log_id

    def _process_next_batch(self):
        self.ensure_one()
        if self.state in ('done', 'partial', 'failed'):
            return False

        now = fields.Datetime.now()
        vals = {'state': 'running', 'next_run_at': False}
        if not self.started_at:
            vals['started_at'] = now
        self.write(vals)

        log = self._ensure_log()
        instance = self.instance_id.with_context(
            amazon_source_model=self._name,
            amazon_source_id=self.id,
            amazon_operation='order_status_sync',
        )
        api = AmazonAPI()

        try:
            instance._auto_fix_region()
            instance._check_required_fields()
            access_token = instance._get_access_token_or_raise()

            last_updated_after = _datetime_to_spapi(self.date_from)
            last_updated_before = _datetime_to_spapi(self.effective_date_to)
            if not self.next_token:
                effective_to = self._get_amazon_safe_before_dt(self.date_to)
                requested_to = amazon_to_utc_naive(self.date_to)
                before_vals = {
                    'effective_date_to': effective_to,
                    'amazon_request_before': _datetime_to_spapi(effective_to),
                    'upper_bound_adjusted': bool(requested_to and requested_to > effective_to),
                }
                changed_vals = self._changed_values(self, before_vals)
                if changed_vals:
                    self.write(changed_vals)

                date_from = amazon_to_utc_naive(self.date_from)
                if date_from and date_from >= effective_to:
                    self._defer_for_unsafe_window(date_from, effective_to)
                    return False
                last_updated_after = _datetime_to_spapi(date_from)
                last_updated_before = _datetime_to_spapi(effective_to)

            data = api.get_orders(
                instance,
                access_token,
                last_updated_after=last_updated_after,
                last_updated_before=last_updated_before,
                fulfillment_channels=self.fulfillment_channel or None,
                next_token=self.next_token or None,
                max_results_per_page=self.batch_size or 10,
                included_data=('FULFILLMENT',),
            )
        except requests.exceptions.HTTPError as exc:
            if self._is_rate_limit_error(exc):
                self._defer_for_rate_limit(exc, scope='get_orders')
                return False
            if self._is_unsafe_before_invalid_input(exc):
                self._handle_invalid_before(exc)
                return False
            self._mark_failed(AmazonAPI.format_exception(exc))
            return False
        except Exception as exc:
            self._mark_failed(str(exc))
            return False

        payload = data.get('payload', {}) or {}
        orders = payload.get('Orders', []) or []
        next_token = payload.get('NextToken') or False

        batch_processed = batch_changed = batch_unchanged = batch_missing = batch_failed = 0
        if not orders and not next_token:
            self._mark_complete()
            return True

        for order_data in orders:
            amazon_order_id = order_data.get('AmazonOrderId') or 'UNKNOWN'
            try:
                with self.env.cr.savepoint():
                    result = self._process_one_order(order_data)
            except Exception as exc:
                batch_failed += 1
                self._append_error("Order %s failed during status sync: %s" % (amazon_order_id, exc))
                _logger.warning("Amazon status sync job %s failed for order %s: %s", self.id, amazon_order_id, exc)
                continue

            batch_processed += 1
            batch_changed += 1 if result.get('changed') else 0
            batch_unchanged += 1 if result.get('unchanged') else 0
            batch_missing += 1 if result.get('missing') else 0
            self.last_processed_amazon_order_id = amazon_order_id

        self.write({
            'total_fetched': self.total_fetched + len(orders),
            'total_processed': self.total_processed + batch_processed,
            'total_changed': self.total_changed + batch_changed,
            'total_unchanged': self.total_unchanged + batch_unchanged,
            'total_missing': self.total_missing + batch_missing,
            'total_failed': self.total_failed + batch_failed,
            'next_token': next_token,
            'last_error_code': False,
            'last_error_message': False,
        })

        if next_token:
            log.write({'summary': self._summary()})
            return True

        self._mark_complete()
        return True

    def _process_one_order(self, order_data):
        amazon_order_id = order_data.get('AmazonOrderId')
        if not amazon_order_id:
            raise ValueError("AmazonOrderId is missing in order status payload.")

        order_rec = self.env['amazon.sale.order'].search([
            ('instance_id', '=', self.instance_id.id),
            ('amazon_order_ref', '=', amazon_order_id),
        ], limit=1)
        if not order_rec:
            self._append_error("Amazon order %s returned by status sync but is missing locally." % amazon_order_id)
            return {'missing': True}

        result = order_rec._sync_amazon_status_from_payload(
            order_data,
            source='status_sync_job_%s' % self.id,
            create_chatter=True,
            apply_workflow=True,
        )
        return {
            'changed': bool(result.get('changed')),
            'unchanged': not result.get('changed'),
            'missing': False,
        }

    def _changed_values(self, record, vals):
        changed = {}
        for field_name, value in vals.items():
            field = record._fields.get(field_name)
            if not field:
                continue
            current = record[field_name]
            if field.type == 'many2one':
                current = current.id
            if current != value:
                changed[field_name] = value
        return changed

    def _is_rate_limit_error(self, exc):
        response = getattr(exc, 'response', None)
        return bool(response is not None and response.status_code == 429)

    def _retry_after_seconds(self, exc):
        response = getattr(exc, 'response', None)
        if response is not None:
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                try:
                    return max(1, int(float(retry_after)))
                except (TypeError, ValueError):
                    pass
        return 60

    def _amazon_error_code_message(self, exc):
        response = getattr(exc, 'response', None)
        if response is None:
            return '', str(exc)
        try:
            data = response.json()
        except ValueError:
            return '', response.text or str(exc)
        errors = data.get('errors') or []
        if errors:
            first_error = errors[0] or {}
            return first_error.get('code') or '', first_error.get('message') or ''
        return data.get('code') or '', data.get('message') or response.text or str(exc)

    def _is_unsafe_before_invalid_input(self, exc):
        response = getattr(exc, 'response', None)
        if response is None or response.status_code != 400:
            return False
        code, message = self._amazon_error_code_message(exc)
        message_lower = (message or '').lower()
        return (
            code == 'InvalidInput'
            and ('createdbefore' in message_lower or 'lastupdatedbefore' in message_lower)
            and (
                '2 minutes' in message_lower
                or 'current time' in message_lower
                or 'systematic delay' in message_lower
                or 'data retrieval' in message_lower
            )
        )

    def _handle_invalid_before(self, exc):
        code, message = self._amazon_error_code_message(exc)
        correction = self._get_amazon_safe_before_dt()
        retry_count = self.date_bound_retry_count + 1
        diagnostic = AmazonAPI.format_exception(exc)
        self.write({
            'effective_date_to': correction,
            'amazon_request_before': _datetime_to_spapi(correction),
            'upper_bound_adjusted': True,
            'date_bound_retry_count': retry_count,
            'last_error_code': code or 'InvalidInput',
            'last_error_message': message or diagnostic,
        })
        if retry_count > MAX_INVALID_BEFORE_RETRIES:
            self._mark_failed(diagnostic)
            return
        retry_at = fields.Datetime.now() + timedelta(minutes=1)
        self.write({
            'state': 'pending',
            'next_run_at': retry_at,
            'error_message': self._merge_error(
                "Amazon rejected LastUpdatedBefore as too recent. "
                "Adjusted request upper bound to %s and will retry. Retry %d/%d. Amazon message: %s"
                % (self.amazon_request_before, retry_count, MAX_INVALID_BEFORE_RETRIES, message or diagnostic)
            ),
        })
        if self.sync_log_id:
            self.sync_log_id.write({'summary': self._summary(), 'error_message': self.error_message})
        _logger.warning(
            "Adjusted Amazon status sync job %s upper bound to %s after InvalidInput retry %d/%d.",
            self.id, self.amazon_request_before, retry_count, MAX_INVALID_BEFORE_RETRIES,
        )

    def _defer_for_unsafe_window(self, date_from, safe_date_to):
        message = (
            "No safe Amazon order status window is available yet. "
            "The end date must be at least 3 minutes before the current UTC time. "
            "date_from=%s safe_date_to=%s"
        ) % (_datetime_to_spapi(date_from), _datetime_to_spapi(safe_date_to))
        self.write({
            'state': 'pending',
            'next_run_at': fields.Datetime.now() + timedelta(minutes=1),
            'effective_date_to': safe_date_to,
            'amazon_request_before': _datetime_to_spapi(safe_date_to),
            'upper_bound_adjusted': True,
            'last_error_code': 'UnsafeAmazonWindow',
            'last_error_message': message,
            'error_message': self._merge_error(message),
        })
        if self.sync_log_id:
            self.sync_log_id.write({'summary': self._summary(), 'error_message': self.error_message})
        _logger.info("Deferred Amazon status sync job %s: %s", self.id, message)

    def _defer_for_rate_limit(self, exc, scope=''):
        retry_after = self._retry_after_seconds(exc)
        retry_after += random.randint(0, min(15, retry_after))
        message = str(exc)
        prefix = "Amazon rate limit during %s" % scope if scope else "Amazon rate limit"
        self.write({
            'state': 'pending',
            'next_run_at': fields.Datetime.now() + timedelta(seconds=retry_after),
            'total_rate_limited': self.total_rate_limited + 1,
            'retry_count': self.retry_count + 1,
            'last_error_code': 'HTTP429',
            'last_error_message': message,
            'error_message': self._merge_error("%s. Retrying after %ss.\n%s" % (prefix, retry_after, message)),
        })
        if self.sync_log_id:
            self.sync_log_id.write({'summary': self._summary(), 'error_message': self.error_message})
        _logger.warning("%s. Status sync job %s deferred for %s seconds.", prefix, self.id, retry_after)

    def _append_error(self, message):
        self.error_message = self._merge_error(message)

    def _merge_error(self, message):
        existing = self.error_message or ''
        merged = "%s\n%s" % (existing, message) if existing else str(message)
        return merged[-5000:]

    def _mark_complete(self):
        # Missing local orders are logged and counted, but they are not a job
        # failure: getOrders can return account orders outside the local import
        # scope. Only real per-order processing failures make the job partial.
        state = 'partial' if self.total_failed else 'done'
        self.write({
            'state': state,
            'next_token': False,
            'next_run_at': False,
            'finished_at': fields.Datetime.now(),
        })
        if state == 'done' and (self.effective_date_to or self.date_to):
            self.instance_id.last_status_sync_at = self.effective_date_to or self.date_to
        summary = self._summary()
        if self.sync_log_id:
            if state == 'partial':
                self.sync_log_id.log_partial(
                    summary=summary,
                    records_processed=self.total_processed,
                    records_updated=self.total_changed,
                    records_failed=self.total_failed + self.total_missing,
                    error_message=self.error_message or '',
                )
            else:
                self.sync_log_id.log_success(
                    summary=summary,
                    records_processed=self.total_processed,
                    records_updated=self.total_changed,
                )

    def _mark_failed(self, message):
        self.write({
            'state': 'failed',
            'finished_at': fields.Datetime.now(),
            'last_error_code': self.last_error_code or 'Error',
            'last_error_message': str(message)[:5000],
            'error_message': self._merge_error(message),
        })
        if self.sync_log_id:
            self.sync_log_id.log_fail(self.error_message or message)
        _logger.error("Amazon status sync job %s failed: %s", self.id, message)

    def _summary(self):
        return (
            "orders fetched=%d | processed=%d | changed=%d | unchanged=%d | missing=%d | "
            "failed=%d | last order=%s | next token=%s | request before=%s | "
            "upper adjusted=%s | rate-limit retries=%d | date-bound retries=%d | duration=%.1fs"
        ) % (
            self.total_fetched,
            self.total_processed,
            self.total_changed,
            self.total_unchanged,
            self.total_missing,
            self.total_failed,
            self.last_processed_amazon_order_id or 'N/A',
            'yes' if self.next_token else 'no',
            self.amazon_request_before or 'N/A',
            'yes' if self.upper_bound_adjusted else 'no',
            self.total_rate_limited,
            self.date_bound_retry_count,
            self.duration_seconds,
        )
