import logging
import random
import time
from datetime import datetime, timedelta

import requests

from odoo import models, fields, api

from .amazon_api import AmazonAPI, amazon_safe_before_dt, amazon_safe_before_iso, amazon_to_utc_naive
from .amazon_instance import _amazon_datetime_to_odoo

_logger = logging.getLogger(__name__)

EGYPT_MARKETPLACE_ID = 'ARBP9OOSHTCHU'
MAX_INVALID_BEFORE_RETRIES = 3


class AmazonRateLimitDeferred(Exception):
    """Raised when Amazon throttling should pause the current job batch."""

    def __init__(self, message, retry_after_seconds=60):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _datetime_to_spapi(value):
    value = amazon_to_utc_naive(value)
    if not value:
        return False
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def _float_amount(value, default=0.0):
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


class AmazonOrderImportJob(models.Model):
    _name = 'amazon.order.import.job'
    _description = 'Amazon Order Import Job'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('partial', 'Partial'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)

    fulfillment_channel = fields.Selection([
        ('MFN', 'FBM'),
        ('AFN', 'FBA'),
    ], string='Fulfillment Channel', help="Leave empty to import both FBM and FBA orders.")
    date_from = fields.Datetime(index=True)
    date_to = fields.Datetime(index=True)
    effective_date_to = fields.Datetime(
        string='Effective Safe Date To',
        help="Actual UTC upper bound used for Amazon CreatedBefore/LastUpdatedBefore after applying the 3-minute safety margin.",
    )
    amazon_request_before = fields.Char(
        string='Amazon Request Before UTC',
        help="Final UTC ISO-8601 timestamp sent to Amazon as CreatedBefore or LastUpdatedBefore.",
    )
    upper_bound_adjusted = fields.Boolean(
        string='Upper Bound Adjusted',
        help="Enabled when the requested Date To was later than Amazon's safe data-availability window.",
    )
    next_token = fields.Text()
    next_run_at = fields.Datetime(index=True)
    batch_size = fields.Integer(default=10)

    total_found = fields.Integer(default=0)
    total_processed = fields.Integer(default=0)
    total_created = fields.Integer(default=0)
    total_updated = fields.Integer(default=0)
    total_skipped = fields.Integer(default=0)
    total_failed = fields.Integer(default=0)
    total_sale_orders_created = fields.Integer(default=0)
    total_sale_orders_skipped = fields.Integer(default=0)
    total_rate_limited = fields.Integer(default=0)
    date_bound_retry_count = fields.Integer(default=0)

    last_processed_order_id = fields.Char()
    last_error_code = fields.Char()
    last_error_message = fields.Text()
    error_message = fields.Text()
    sync_log_id = fields.Many2one('amazon.sync.log', ondelete='set null')
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    duration_seconds = fields.Float(compute='_compute_duration', store=True)

    _batch_size_range = models.Constraint(
        'CHECK (batch_size IS NULL OR (batch_size >= 1 AND batch_size <= 100))',
        'Batch size must be between 1 and 100.',
    )

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for job in self:
            end = job.finished_at or fields.Datetime.now()
            job.duration_seconds = (end - job.started_at).total_seconds() if job.started_at else 0.0

    @api.model
    def cron_process_order_import_jobs(self):
        """Process one queued/running job batch.

        The row lock prevents multiple cron workers from processing the same job.
        The cron transaction is the batch transaction; no explicit commit is used.
        """
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_order_import_job
             WHERE state IN ('draft', 'running')
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
            amazon_operation='order_import',
        )
        job._process_next_batch()
        return True

    @api.model
    def _get_amazon_safe_before_dt(self, requested_before=None):
        """Return Amazon-safe UTC-naive upper bound with a 3-minute safety delay."""
        return amazon_safe_before_dt(requested_before)

    @api.model
    def _get_amazon_safe_before(self, requested_before=None):
        """Return Amazon-safe upper bound formatted as YYYY-MM-DDTHH:MM:SSZ."""
        return amazon_safe_before_iso(requested_before)

    def _ensure_log(self):
        self.ensure_one()
        if self.sync_log_id:
            return self.sync_log_id
        self.sync_log_id = self.env['amazon.sync.log'].log_start(
            self.instance_id,
            'order_import',
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
            amazon_operation='order_import',
        )
        api = AmazonAPI()

        try:
            instance._auto_fix_region()
            instance._check_required_fields()
            access_token = instance._get_access_token_or_raise()

            created_after = _datetime_to_spapi(self.date_from)
            created_before = False
            if not self.next_token:
                created_before_dt = self._get_amazon_safe_before_dt(self.date_to)
                created_before = _datetime_to_spapi(created_before_dt)
                requested_to = amazon_to_utc_naive(self.date_to)
                update_before_vals = {
                    'effective_date_to': created_before_dt,
                    'amazon_request_before': created_before,
                    'upper_bound_adjusted': bool(requested_to and requested_to > created_before_dt),
                }
                if self._changed_values(self, update_before_vals):
                    self.write(update_before_vals)
                date_from = amazon_to_utc_naive(self.date_from)
                if date_from and date_from >= created_before_dt:
                    self._defer_for_unsafe_window(date_from, created_before_dt)
                    return False

            data = api.get_orders(
                instance,
                access_token,
                created_after=created_after,
                created_before=created_before,
                fulfillment_channels=self.fulfillment_channel or None,
                next_token=self.next_token or None,
                max_results_per_page=self.batch_size or 10,
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

        batch_created = batch_updated = batch_skipped = batch_failed = 0
        batch_processed = batch_sale_orders_created = batch_sale_orders_skipped = 0
        rate_limited = False

        if not orders and not next_token:
            self._mark_complete()
            return True

        for order_data in orders:
            amazon_order_id = order_data.get('AmazonOrderId') or 'UNKNOWN'
            try:
                with self.env.cr.savepoint():
                    result = self._import_one_order(api, access_token, order_data)
            except AmazonRateLimitDeferred as exc:
                rate_limited = True
                self._defer_for_rate_limit(exc, scope='get_order_items', amazon_order_id=amazon_order_id)
                break
            except Exception as exc:
                batch_failed += 1
                self._append_error("Order %s failed: %s" % (amazon_order_id, exc))
                _logger.warning("Amazon order %s failed during import job %s: %s", amazon_order_id, self.id, exc)
                continue

            batch_processed += 1
            batch_created += 1 if result.get('created') else 0
            batch_updated += 1 if result.get('updated') else 0
            batch_skipped += 1 if result.get('skipped') else 0
            batch_sale_orders_created += 1 if result.get('sale_order_created') else 0
            batch_sale_orders_skipped += 1 if result.get('sale_order_skipped') else 0
            self.last_processed_order_id = result.get('amazon_order_id') or amazon_order_id

        update_vals = {
            'total_processed': self.total_processed + batch_processed,
            'total_created': self.total_created + batch_created,
            'total_updated': self.total_updated + batch_updated,
            'total_skipped': self.total_skipped + batch_skipped,
            'total_failed': self.total_failed + batch_failed,
            'total_sale_orders_created': self.total_sale_orders_created + batch_sale_orders_created,
            'total_sale_orders_skipped': self.total_sale_orders_skipped + batch_sale_orders_skipped,
        }

        if not rate_limited:
            update_vals.update({
                'total_found': self.total_found + len(orders),
                'next_token': next_token,
            })
        self.write(update_vals)

        if rate_limited:
            log.write({'summary': self._summary()})
            return False

        if next_token:
            log.write({'summary': self._summary()})
            return True

        self._mark_complete()
        return True

    def _import_one_order(self, api, access_token, order_data):
        self.ensure_one()
        instance = self.instance_id
        amazon_order_id = order_data.get('AmazonOrderId')
        if not amazon_order_id:
            raise ValueError("AmazonOrderId is missing in order payload.")

        order_model = self.env['amazon.sale.order']
        order_rec = order_model.search([
            ('amazon_order_ref', '=', amazon_order_id),
            ('instance_id', '=', instance.id),
        ], limit=1)

        vals = self._prepare_order_vals(order_data)
        created = updated = skipped = False
        if order_rec:
            changed_vals = self._changed_values(order_rec, vals)
            if changed_vals:
                order_rec.write(changed_vals)
                updated = True
            else:
                skipped = True
        else:
            order_rec = order_model.create(vals)
            created = True

        should_fetch_items = created or not order_rec.order_line_ids
        if should_fetch_items:
            time.sleep(random.uniform(0.15, 0.45))
            try:
                items_data = api.get_order_items(instance, access_token, amazon_order_id)
            except requests.exceptions.HTTPError as exc:
                if self._is_rate_limit_error(exc):
                    raise AmazonRateLimitDeferred(
                        AmazonAPI.format_exception(exc),
                        retry_after_seconds=self._retry_after_seconds(exc),
                    ) from exc
                raise
            self._upsert_order_items(order_rec, items_data.get('payload', {}).get('OrderItems', []) or [])

        sale_order_created = sale_order_skipped = False
        if not order_rec.sale_order_id and order_rec.order_line_ids:
            missing_lines = order_rec.order_line_ids.filtered(lambda line: not line.odoo_product_id)
            if missing_lines:
                missing_refs = ", ".join(
                    (line.sku or line.asin or line.amazon_order_item_id or 'Unknown item')
                    for line in missing_lines[:10]
                )
                self._append_error(
                    "Order %s skipped for Odoo SO creation: missing product mapping for %s"
                    % (amazon_order_id, missing_refs)
                )
                skipped = True
                sale_order_skipped = True
            else:
                order_rec.action_create_sale_order()
                sale_order_created = True
                _logger.info("Auto-created Odoo SO for Amazon order %s", amazon_order_id)

        order_rec._sync_amazon_status_from_payload(
            order_data,
            source='order_import',
            create_chatter=False,
            apply_workflow=False,
        )

        return {
            'amazon_order_id': amazon_order_id,
            'created': created,
            'updated': updated,
            'skipped': skipped,
            'sale_order_created': sale_order_created,
            'sale_order_skipped': sale_order_skipped,
        }

    def _prepare_order_vals(self, order_data):
        instance = self.instance_id
        amount = order_data.get('OrderTotal', {}) or {}
        amazon_status = order_data.get('OrderStatus')
        legacy_status = self.env['amazon.sale.order']._amazon_status_to_order_status(amazon_status)
        amazon_last_update = _amazon_datetime_to_odoo(order_data.get('LastUpdateDate'))
        vals = {
            'amazon_order_ref': order_data.get('AmazonOrderId'),
            'instance_id': instance.id,
            'order_status': legacy_status or False,
            'amazon_status': amazon_status,
            'fulfillment_channel': self._selection_value('amazon.sale.order', 'fulfillment_channel', order_data.get('FulfillmentChannel'), 'MFN'),
            'order_type': self._selection_value('amazon.sale.order', 'order_type', order_data.get('OrderType'), 'StandardOrder'),
            'purchase_date': _amazon_datetime_to_odoo(order_data.get('PurchaseDate')),
            'last_update_date': amazon_last_update,
            'amazon_last_update_date': amazon_last_update,
            'status_last_synced_at': fields.Datetime.now(),
            'sales_channel': order_data.get('SalesChannel', ''),
            'is_prime': order_data.get('IsPrime', False),
            'is_business_order': order_data.get('IsBusinessOrder', False),
            'order_total': _float_amount(amount.get('Amount')) if amount else 0.0,
            'ship_service_level': order_data.get('ShipServiceLevel', ''),
        }

        currency = self._resolve_currency(amount)
        if currency:
            vals['currency_id'] = currency.id

        ship_addr = order_data.get('ShippingAddress', {}) or {}
        if ship_addr:
            vals.update({
                'shipping_address_name': ship_addr.get('Name', ''),
                'shipping_address_line1': ship_addr.get('AddressLine1', ''),
                'shipping_address_line2': ship_addr.get('AddressLine2', ''),
                'shipping_city': ship_addr.get('City', ''),
                'shipping_state': ship_addr.get('StateOrRegion', ''),
                'shipping_postal_code': ship_addr.get('PostalCode', ''),
                'shipping_country_code': ship_addr.get('CountryCode', ''),
            })
        return vals

    def _upsert_order_items(self, order_rec, order_items):
        line_model = self.env['amazon.sale.order.line']
        for item in order_items:
            item_id = item.get('OrderItemId')
            if not item_id:
                raise ValueError("Order item without OrderItemId for Amazon order %s" % order_rec.amazon_order_ref)

            price_info = item.get('ItemPrice', {}) or {}
            tax_info = item.get('ItemTax', {}) or {}
            shipping_info = item.get('ShippingPrice', {}) or {}
            promo_info = item.get('PromotionDiscount', {}) or {}
            line_vals = {
                'order_id': order_rec.id,
                'amazon_order_item_id': item_id,
                'sku': item.get('SellerSKU', ''),
                'asin': item.get('ASIN', ''),
                'title': item.get('Title', ''),
                'quantity': item.get('QuantityOrdered', 1),
                'item_price': _float_amount(price_info.get('Amount')) if price_info else 0.0,
                'item_tax': _float_amount(tax_info.get('Amount')) if tax_info else 0.0,
                'shipping_price': _float_amount(shipping_info.get('Amount')) if shipping_info else 0.0,
                'promotion_discount': _float_amount(promo_info.get('Amount')) if promo_info else 0.0,
            }

            if line_vals['sku']:
                amazon_product = self.env['amazon.product'].search([
                    ('sku', '=', line_vals['sku']),
                    ('instance_id', '=', self.instance_id.id),
                ], limit=1)
                if amazon_product:
                    line_vals['amazon_product_id'] = amazon_product.id
                    if amazon_product.odoo_product_id:
                        line_vals['odoo_product_id'] = amazon_product.odoo_product_id.id

            existing_line = line_model.search([
                ('order_id', '=', order_rec.id),
                ('amazon_order_item_id', '=', item_id),
            ], limit=1)
            if existing_line:
                existing_line.write(self._changed_values(existing_line, line_vals))
            else:
                line_model.create(line_vals)

    def _resolve_currency(self, amount):
        currency_code = (amount or {}).get('CurrencyCode')
        if not currency_code and self.instance_id.marketplace_id == EGYPT_MARKETPLACE_ID:
            currency_code = 'EGP'
        if currency_code:
            currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
            if currency:
                return currency
        return self.instance_id._get_currency()

    def _selection_value(self, model_name, field_name, value, default):
        selection = dict(self.env[model_name]._fields[field_name].selection)
        if value in selection:
            return value
        if value:
            _logger.warning("Unsupported Amazon %s value %s; using %s.", field_name, value, default)
        return default

    def _changed_values(self, record, vals):
        changed = {}
        for field_name, value in vals.items():
            field = record._fields.get(field_name)
            if not field:
                continue
            current = record[field_name]
            if field.type == 'many2one':
                current = current.id
            if isinstance(current, datetime) and isinstance(value, datetime):
                if current.replace(microsecond=0) != value.replace(microsecond=0):
                    changed[field_name] = value
            elif current != value:
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
            'state': 'running',
            'next_run_at': retry_at,
            'error_message': self._merge_error(
                "Amazon rejected the order upper bound as too recent. "
                "Adjusted request upper bound to %s and will retry on the next cron run. "
                "Retry %d/%d. Amazon message: %s"
                % (
                    self.amazon_request_before,
                    retry_count,
                    MAX_INVALID_BEFORE_RETRIES,
                    message or diagnostic,
                )
            ),
        })
        if self.sync_log_id:
            self.sync_log_id.write({
                'summary': self._summary(),
                'error_message': self.error_message,
            })
        _logger.warning(
            "Adjusted Amazon order import job %s upper bound to %s after InvalidInput retry %d/%d.",
            self.id, self.amazon_request_before, retry_count, MAX_INVALID_BEFORE_RETRIES,
        )

    def _defer_for_unsafe_window(self, date_from, safe_date_to):
        message = (
            "No safe Amazon order window is available yet. "
            "The end date must be at least 3 minutes before the current UTC time. "
            "date_from=%s safe_date_to=%s"
        ) % (_datetime_to_spapi(date_from), _datetime_to_spapi(safe_date_to))
        self.write({
            'state': 'running',
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
        _logger.info("Deferred Amazon order import job %s: %s", self.id, message)

    def _defer_for_rate_limit(self, exc, scope='', amazon_order_id=None):
        retry_after = getattr(exc, 'retry_after_seconds', None) or self._retry_after_seconds(exc)
        retry_after += random.randint(0, min(15, retry_after))
        message = str(exc)
        prefix = "Amazon rate limit during %s" % scope if scope else "Amazon rate limit"
        if amazon_order_id:
            prefix += " for order %s" % amazon_order_id
        self.write({
            'state': 'running',
            'next_run_at': fields.Datetime.now() + timedelta(seconds=retry_after),
            'total_rate_limited': self.total_rate_limited + 1,
            'last_error_code': 'HTTP429',
            'last_error_message': message,
            'error_message': self._merge_error("%s. Retrying after %ss.\n%s" % (prefix, retry_after, message)),
        })
        if self.sync_log_id:
            self.sync_log_id.write({'summary': self._summary(), 'error_message': self.error_message})
        _logger.warning("%s. Job %s deferred for %s seconds.", prefix, self.id, retry_after)

    def _append_error(self, message):
        self.error_message = self._merge_error(message)

    def _merge_error(self, message):
        existing = self.error_message or ''
        merged = "%s\n%s" % (existing, message) if existing else str(message)
        return merged[-5000:]

    def _mark_complete(self):
        state = 'partial' if self.total_failed or self.total_sale_orders_skipped else 'done'
        self.write({
            'state': state,
            'next_token': False,
            'next_run_at': False,
            'finished_at': fields.Datetime.now(),
        })
        if self.effective_date_to or self.date_to:
            self.instance_id.last_order_sync = self.effective_date_to or self.date_to
        summary = self._summary()
        if self.sync_log_id:
            if state == 'partial':
                self.sync_log_id.log_partial(
                    summary=summary,
                    records_processed=self.total_processed,
                    records_created=self.total_created,
                    records_updated=self.total_updated,
                    records_failed=self.total_failed,
                    error_message=self.error_message or '',
                )
            else:
                self.sync_log_id.log_success(
                    summary=summary,
                    records_processed=self.total_processed,
                    records_created=self.total_created,
                    records_updated=self.total_updated,
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
        _logger.error("Amazon order import job %s failed: %s", self.id, message)

    def _summary(self):
        return (
            "orders fetched=%d | processed=%d | created=%d | updated=%d | "
            "duplicates/skipped=%d | failed=%d | sale orders created=%d | sale orders skipped=%d | "
            "last order=%s | next token=%s | request before=%s | upper adjusted=%s | "
            "rate-limit retries=%d | date-bound retries=%d | duration=%.1fs"
        ) % (
            self.total_found,
            self.total_processed,
            self.total_created,
            self.total_updated,
            self.total_skipped,
            self.total_failed,
            self.total_sale_orders_created,
            self.total_sale_orders_skipped,
            self.last_processed_order_id or 'N/A',
            'yes' if self.next_token else 'no',
            self.amazon_request_before or 'N/A',
            'yes' if self.upper_bound_adjusted else 'no',
            self.total_rate_limited,
            self.date_bound_retry_count,
            self.duration_seconds,
        )
