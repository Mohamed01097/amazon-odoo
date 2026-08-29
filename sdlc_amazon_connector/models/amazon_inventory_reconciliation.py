import json
import logging
import re
from datetime import timedelta

import requests

from odoo import _, Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero

from .amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)


class AmazonInventoryReconciliationRun(models.Model):
    _name = 'amazon.inventory.reconciliation.run'
    _description = 'Amazon Inventory Reconciliation Run'
    _order = 'run_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default='New', copy=False, readonly=True)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    run_date = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    trigger = fields.Selection([
        ('manual', 'Manual'),
        ('scheduled', 'Scheduled'),
    ], required=True, default='manual', readonly=True)
    mode = fields.Selection([
        ('manual', 'Manual Review'),
        ('automatic', 'Legacy Automatic (Disabled)'),
    ], required=True, default='manual', readonly=True,
       help=(
           "Inventory snapshots never change stock automatically. The legacy "
           "automatic value is retained only for historical records."
       ))
    state = fields.Selection([
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], required=True, default='queued', copy=False, index=True)
    retry_count = fields.Integer(default=0, copy=False, readonly=True)
    max_retries = fields.Integer(default=5, readonly=True)
    next_run_at = fields.Datetime(copy=False, readonly=True, index=True)
    last_error = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    amazon_request_ids = fields.Text(
        string='Amazon Request IDs',
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    raw_response = fields.Text(
        string='Raw Amazon Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    reconciliation_ids = fields.One2many(
        'amazon.inventory.reconciliation', 'run_id', string='Inventory Audit Lines',
        copy=False,
    )
    products_checked = fields.Integer(readonly=True)
    amazon_records_read = fields.Integer(readonly=True)
    page_count = fields.Integer(readonly=True)
    matched_count = fields.Integer(readonly=True)
    issue_count = fields.Integer(readonly=True)
    mismatch_count = fields.Integer(readonly=True)
    unmapped_count = fields.Integer(readonly=True)
    not_returned_count = fields.Integer(readonly=True)
    error_count = fields.Integer(readonly=True)
    critical_count = fields.Integer(readonly=True)
    pending_review_count = fields.Integer(readonly=True)
    snapshot_complete = fields.Boolean(readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    retry_after_seconds = fields.Float(readonly=True, copy=False)
    transfer_count = fields.Integer(compute='_compute_transfer_count')

    _valid_retry_limits = models.Constraint(
        'CHECK (retry_count >= 0 AND max_retries >= 1 AND max_retries <= 25)',
        'Inventory reconciliation retry limits are invalid.',
    )

    @api.depends('reconciliation_ids.applied_picking_id')
    def _compute_transfer_count(self):
        for run in self:
            run.transfer_count = len(run.reconciliation_ids.applied_picking_id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code(
                        'amazon.inventory.reconciliation.run'
                    ) or 'New'
                )
            # Reconciliation always starts in snapshot/manual-review mode.
            # Keep the legacy selection value readable, but never create a new
            # run that can apply stock automatically.
            vals['mode'] = 'manual'
        return super().create(vals_list)

    def _lock_run(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM amazon_inventory_reconciliation_run '
            'WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset()

    def _lock_instance(self):
        """Prevent simultaneous snapshots for the same seller instance."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)",
            ['amazon_fba_inventory_reconciliation', self.instance_id.id],
        )
        if not self.env.cr.fetchone()[0]:
            raise UserError(_(
                "Another FBA inventory reconciliation is already running for %s.",
                self.instance_id.display_name,
            ))

    @staticmethod
    def _json(value):
        return json.dumps(value, default=str, sort_keys=True, indent=2)

    @staticmethod
    def _amazon_quantity(value, field_name):
        if value in (None, False, ''):
            return 0.0
        if isinstance(value, bool):
            raise ValidationError(_("Amazon returned an invalid %s.", field_name))
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Amazon returned an invalid %s.", field_name)) from exc
        if number < 0:
            raise ValidationError(_("Amazon returned a negative %s.", field_name))
        return number

    def _aggregate_amazon_summaries(self, response):
        self.ensure_one()
        payload = response.get('payload') if isinstance(response, dict) else None
        summaries = payload.get('inventorySummaries') if isinstance(payload, dict) else None
        if not isinstance(summaries, list):
            raise ValidationError(_("Amazon did not return a valid inventorySummaries list."))
        aggregated = {}
        for summary in summaries:
            if not isinstance(summary, dict):
                raise ValidationError(_("Amazon returned an invalid inventory summary."))
            sku = str(summary.get('sellerSku') or '').strip()
            if not sku:
                raise ValidationError(_("Amazon returned an inventory summary without sellerSku."))
            details = summary.get('inventoryDetails')
            if not isinstance(details, dict):
                raise ValidationError(_(
                    "Amazon omitted inventoryDetails for SKU %s. The audit requires details=true.",
                    sku,
                ))
            reserved = details.get('reservedQuantity') or {}
            unfulfillable = details.get('unfulfillableQuantity') or {}
            if not isinstance(reserved, dict) or not isinstance(unfulfillable, dict):
                raise ValidationError(_("Amazon returned invalid inventory details for SKU %s.", sku))
            values = {
                'sellable': self._amazon_quantity(
                    details.get('fulfillableQuantity'), 'fulfillableQuantity',
                ),
                'reserved': self._amazon_quantity(
                    reserved.get('totalReservedQuantity'), 'totalReservedQuantity',
                ),
                'unsellable': self._amazon_quantity(
                    unfulfillable.get('totalUnfulfillableQuantity'),
                    'totalUnfulfillableQuantity',
                ),
                'inbound_working': self._amazon_quantity(
                    details.get('inboundWorkingQuantity'), 'inboundWorkingQuantity',
                ),
                'inbound_shipped': self._amazon_quantity(
                    details.get('inboundShippedQuantity'), 'inboundShippedQuantity',
                ),
                'inbound_receiving': self._amazon_quantity(
                    details.get('inboundReceivingQuantity'), 'inboundReceivingQuantity',
                ),
                'total': self._amazon_quantity(
                    summary.get('totalQuantity'), 'totalQuantity',
                ),
                'reserved_customer_orders': self._amazon_quantity(
                    reserved.get('pendingCustomerOrderQuantity'),
                    'pendingCustomerOrderQuantity',
                ),
                'reserved_fc_transfers': self._amazon_quantity(
                    reserved.get('pendingTransshipmentQuantity'),
                    'pendingTransshipmentQuantity',
                ),
                'reserved_fc_processing': self._amazon_quantity(
                    reserved.get('fcProcessingQuantity'), 'fcProcessingQuantity',
                ),
            }
            bucket = aggregated.setdefault(sku, {
                'sellable': 0.0,
                'reserved': 0.0,
                'unsellable': 0.0,
                'inbound_working': 0.0,
                'inbound_shipped': 0.0,
                'inbound_receiving': 0.0,
                'total': 0.0,
                'reserved_customer_orders': 0.0,
                'reserved_fc_transfers': 0.0,
                'reserved_fc_processing': 0.0,
                'asins': set(),
                'fnskus': set(),
                'conditions': set(),
                'last_updated_values': [],
                'raw': [],
            })
            for key, quantity in values.items():
                bucket[key] += quantity
            if summary.get('asin'):
                bucket['asins'].add(str(summary['asin']).strip())
            if summary.get('fnSku'):
                bucket['fnskus'].add(str(summary['fnSku']).strip())
            if summary.get('condition'):
                bucket['conditions'].add(str(summary['condition']).strip())
            if summary.get('lastUpdatedTime'):
                bucket['last_updated_values'].append(str(summary['lastUpdatedTime']).strip())
            bucket['raw'].append(summary)
        for bucket in aggregated.values():
            bucket['asin'] = ', '.join(sorted(bucket.pop('asins')))
            bucket['fnsku'] = ', '.join(sorted(bucket.pop('fnskus')))
            bucket['condition'] = ', '.join(sorted(bucket.pop('conditions')))
            bucket['amazon_last_updated_raw'] = max(
                bucket.pop('last_updated_values'), default='',
            )
        return aggregated

    def _validate_locations(self):
        self.ensure_one()
        instance = self.instance_id
        locations = {
            'received': instance.fba_received_location_id,
            'sellable': instance.fba_sellable_location_id,
            'reserved': instance.fba_reserved_location_id,
            'unsellable': instance.fba_unsellable_location_id,
            'transit': instance.fba_transit_location_id,
        }
        if any(not location for location in locations.values()):
            raise UserError(_(
                "Configure Amazon Received/Staging, Sellable, Reserved, Unsellable, "
                "and Transit locations first."
            ))
        expected_usage = {
            'received': 'internal',
            'sellable': 'internal',
            'reserved': 'internal',
            'unsellable': 'internal',
            'transit': 'transit',
        }
        if len({location.id for location in locations.values()}) != 5:
            raise UserError(_("The five Amazon inventory locations must be distinct."))
        for role, location in locations.items():
            if not location.active or location.usage != expected_usage[role]:
                raise UserError(_("The configured Amazon %s location is invalid.", role.title()))
            if location.company_id != self.company_id:
                raise UserError(_("Amazon inventory locations must belong to the audit company."))
        return locations

    def _odoo_quantities(self, product, locations):
        self.ensure_one()
        if product.company_id and product.company_id != self.company_id:
            raise ValidationError(_("Product %s belongs to another company.", product.display_name))
        product = product.with_company(self.company_id)
        return {
            role: product.with_context(location=location.id).qty_available
            for role, location in locations.items()
        }

    @staticmethod
    def _classification(amazon_values, odoo_values, rounding):
        differences = {
            key: amazon_values[key] - odoo_values[key]
            for key in ('sellable', 'reserved', 'unsellable', 'transit')
        }

        def zero(value):
            return float_is_zero(value, precision_rounding=rounding)

        if all(zero(value) for value in differences.values()):
            return differences, 'none', 'matched', 'normal'
        # getInventorySummaries is a point-in-time aggregate, not a disposition
        # event stream. Equal and opposite differences are therefore evidence
        # for review, never proof that a particular transfer occurred.
        disposition_difference = any(
            not zero(differences[key])
            for key in ('sellable', 'reserved', 'unsellable')
        )
        return (
            differences,
            'manual_review',
            'mismatch',
            'critical' if disposition_difference else 'warning',
        )

    def _prepare_lines(self, response):
        self.ensure_one()
        locations = self._validate_locations()
        amazon_by_sku = self._aggregate_amazon_summaries(response)
        amazon_products = self.env['amazon.product'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('sku', '!=', False),
        ])
        products_by_sku = {
            product.sku.strip(): product
            for product in amazon_products
            if product.sku and (
                product.fulfillment_channel == 'AFN'
                or product.sku.strip() in amazon_by_sku
            )
        }
        all_skus = sorted(set(amazon_by_sku) | set(products_by_sku))
        commands = []
        for sku in all_skus:
            amazon_returned = sku in amazon_by_sku
            summary = amazon_by_sku.get(sku, {})
            amazon_product = products_by_sku.get(sku)
            odoo_product = amazon_product.odoo_product_id if amazon_product else False
            amazon_values = {
                'sellable': summary.get('sellable', 0.0),
                'reserved': summary.get('reserved', 0.0),
                'unsellable': summary.get('unsellable', 0.0),
                # Odoo Transit represents physically shipped inventory. Amazon
                # inboundWorking is intentionally excluded because it is only
                # inventory the seller has notified Amazon about.
                'transit': (
                    summary.get('inbound_shipped', 0.0)
                    + summary.get('inbound_receiving', 0.0)
                ),
            }
            if not amazon_returned:
                # Amazon documents an unfiltered call as returning all summaries
                # with available details, but does not guarantee that every
                # mapped zero-quantity SKU appears. Absence is not zero.
                odoo_values = (
                    self._odoo_quantities(odoo_product, locations)
                    if odoo_product and odoo_product.is_storable
                    else dict.fromkeys(
                        ('received', 'sellable', 'reserved', 'unsellable', 'transit'),
                        0.0,
                    )
                )
                differences = dict.fromkeys(
                    ('sellable', 'reserved', 'unsellable', 'transit'), 0.0,
                )
                suggested = 'manual_review'
                status = 'not_returned'
                severity = 'warning'
            elif odoo_product and odoo_product.is_storable:
                odoo_values = self._odoo_quantities(odoo_product, locations)
                rounding = odoo_product.uom_id.rounding or 0.01
                differences, suggested, status, severity = self._classification(
                    amazon_values, odoo_values, rounding,
                )
            else:
                odoo_values = dict.fromkeys(
                    ('received', 'sellable', 'reserved', 'unsellable', 'transit'), 0.0,
                )
                differences = {
                    key: amazon_values[key] for key in amazon_values
                }
                suggested = 'manual_review'
                status = 'unmapped'
                severity = 'critical'
            pending_sale_qty = 0.0
            sale_overlap_state = 'none'
            overlap_message = False
            if odoo_product and amazon_returned:
                sale_events = self.env['amazon.fba.sale.stock.event'].sudo().search([
                    ('instance_id', '=', self.instance_id.id),
                    ('product_id', '=', odoo_product.id),
                    ('state', 'in', ('pending', 'processing', 'manual_review', 'failed')),
                ])
                pending_sale_qty = sum(max(
                    event.amazon_cumulative_fulfilled_qty - event.processed_fulfilled_qty,
                    0.0,
                ) for event in sale_events)
                amazon_disposition = sum(
                    amazon_values[key] for key in ('sellable', 'reserved', 'unsellable')
                )
                odoo_disposition = sum(
                    odoo_values[key] for key in ('sellable', 'reserved', 'unsellable')
                )
                if pending_sale_qty > 0:
                    sale_overlap_state = 'pending_event'
                    overlap_message = _(
                        "The snapshot overlaps %s units of durable, unprocessed Amazon FBA sale events. "
                        "Do not apply a snapshot transfer.", pending_sale_qty,
                    )
                elif amazon_disposition < odoo_disposition:
                    sale_overlap_state = 'snapshot_outflow'
                    overlap_message = _(
                        "Amazon's snapshot shows a net disposition outflow of %s units. Await/import the "
                        "authoritative order-item fulfillment event; the snapshot cannot consume stock.",
                        odoo_disposition - amazon_disposition,
                    )
            values = {
                'sku': sku,
                'amazon_returned': amazon_returned,
                'amazon_product_id': amazon_product.id if amazon_product else False,
                'odoo_product_id': odoo_product.id if odoo_product else False,
                'asin': summary.get('asin', ''),
                'fnsku': summary.get('fnsku', ''),
                'amazon_condition': summary.get('condition', ''),
                'amazon_last_updated_raw': summary.get('amazon_last_updated_raw', ''),
                'amazon_sellable': amazon_values['sellable'],
                'amazon_reserved': amazon_values['reserved'],
                'amazon_unsellable': amazon_values['unsellable'],
                'amazon_total': summary.get('total', 0.0),
                'amazon_reserved_customer_orders': summary.get(
                    'reserved_customer_orders', 0.0,
                ),
                'amazon_reserved_fc_transfers': summary.get(
                    'reserved_fc_transfers', 0.0,
                ),
                'amazon_reserved_fc_processing': summary.get(
                    'reserved_fc_processing', 0.0,
                ),
                'amazon_inbound': amazon_values['transit'],
                'amazon_inbound_working': summary.get('inbound_working', 0.0),
                'amazon_inbound_shipped': summary.get('inbound_shipped', 0.0),
                'amazon_inbound_receiving': summary.get('inbound_receiving', 0.0),
                'odoo_received': odoo_values['received'],
                'odoo_sellable': odoo_values['sellable'],
                'odoo_reserved': odoo_values['reserved'],
                'odoo_unsellable': odoo_values['unsellable'],
                'odoo_transit': odoo_values['transit'],
                'difference_sellable': differences['sellable'],
                'difference_reserved': differences['reserved'],
                'difference_unsellable': differences['unsellable'],
                'difference_inbound': differences['transit'],
                'pending_sale_event_qty': pending_sale_qty,
                'sale_overlap_state': sale_overlap_state,
                'suggested_action': suggested,
                'status': status,
                'severity': severity,
                'raw_response': self._json(summary.get('raw', [])),
            }
            if overlap_message:
                values['error_message'] = overlap_message
            elif status == 'not_returned':
                values['error_message'] = _(
                    "Mapped FBA SKU was not returned by this complete Amazon snapshot. "
                    "Amazon does not guarantee omitted SKUs represent zero inventory."
                )
            elif status == 'unmapped':
                values['error_message'] = _(
                    "UNMAPPED AMAZON SKU: link SKU %s to an inventory-tracked Odoo product "
                    "and run reconciliation again.", sku,
                )
            commands.append(Command.create(values))
            if amazon_returned and amazon_product:
                amazon_product.amazon_qty = amazon_values['sellable']
        return commands

    def _refresh_counts(self):
        self.ensure_one()
        lines = self.reconciliation_ids
        self.write({
            'products_checked': len(lines),
            'matched_count': len(lines.filtered(lambda line: line.status == 'matched')),
            'issue_count': len(lines.filtered(lambda line: line.status != 'matched')),
            'mismatch_count': len(lines.filtered(
                lambda line: line.status in ('mismatch', 'pending_review')
            )),
            'unmapped_count': len(lines.filtered(lambda line: line.status == 'unmapped')),
            'not_returned_count': len(lines.filtered(
                lambda line: line.status == 'not_returned'
            )),
            'error_count': len(lines.filtered(lambda line: line.status in ('error', 'failed'))),
            'critical_count': len(lines.filtered(lambda line: line.severity == 'critical')),
            'pending_review_count': len(lines.filtered(
                lambda line: line.status in (
                    'mismatch', 'pending_review', 'unmapped', 'not_returned', 'error', 'failed',
                )
            )),
        })

    @staticmethod
    def _retry_metadata(error):
        """Return ``(retryable, retry_after_seconds)`` through wrapped causes."""
        current = error
        seen = set()
        while current and id(current) not in seen:
            seen.add(id(current))
            response = getattr(current, 'response', None)
            if response is not None:
                status = getattr(response, 'status_code', None)
                retry_after = (getattr(response, 'headers', {}) or {}).get('Retry-After')
                try:
                    retry_after = max(float(retry_after or 0.0), 0.0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                return status == 429 or (status is not None and status >= 500), retry_after
            if isinstance(current, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
                return True, 0.0
            current = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        text = str(error)
        retry_after_match = re.search(r'Retry-After:\s*([0-9]+(?:\.[0-9]+)?)', text, re.I)
        retry_after = float(retry_after_match.group(1)) if retry_after_match else 0.0
        retryable = bool(
            re.search(
                r'\b429\b|timeout|timed out|temporary|connection|already running|\b5\d\d\b',
                text, re.I,
            )
        )
        return retryable, retry_after

    def _schedule_retry(self, error_message):
        self.ensure_one()
        retryable, retry_after_seconds = self._retry_metadata(error_message)
        retries = self.retry_count + 1
        vals = {
            'retry_count': retries,
            'last_error': str(error_message)[:10000],
            'retry_after_seconds': retry_after_seconds,
            'snapshot_complete': False,
            'completed_at': False,
        }
        if not retryable or retries >= self.max_retries:
            vals.update(state='failed', next_run_at=False, error_count=1)
        else:
            exponential_seconds = min(2 ** max(retries - 1, 0), 60) * 60
            vals.update(
                state='queued',
                next_run_at=fields.Datetime.now() + timedelta(
                    seconds=max(exponential_seconds, retry_after_seconds),
                ),
            )
        self.write(vals)

    def _process_run(self):
        self.ensure_one()
        if self.state == 'completed':
            return False
        try:
            with self.env.cr.savepoint():
                self._lock_run()
                if self.state == 'completed':
                    return False
                self._lock_instance()
                self.write({
                    'state': 'running',
                    'next_run_at': False,
                    'last_error': False,
                    'snapshot_complete': False,
                    'completed_at': False,
                    'retry_after_seconds': 0.0,
                    'error_count': 0,
                })
                self.instance_id._check_required_fields()
                access_token = self.instance_id._get_access_token_or_raise()
                response = self.instance_id._api_call_safe(
                    AmazonAPI().get_all_inventory_summaries,
                    self.instance_id,
                    access_token,
                    details=True,
                    error_msg="Failed to retrieve Amazon FBA inventory summaries",
                )
                if not isinstance(response, dict) or response.get('_snapshot_complete') is not True:
                    raise ValidationError(_(
                        "Amazon inventory pagination did not complete. No snapshot or "
                        "reconciliation lines were accepted."
                    ))
                payload = response.get('payload') or {}
                summaries = payload.get('inventorySummaries')
                if not isinstance(summaries, list):
                    raise ValidationError(_(
                        "Amazon did not return a complete inventorySummaries list."
                    ))
                self.reconciliation_ids.unlink()
                self.write({
                    'reconciliation_ids': self._prepare_lines(response),
                    'raw_response': self._json(response),
                    'amazon_records_read': len(summaries),
                    'page_count': int(response.get('_page_count') or len(response.get('_pages') or [])),
                    'amazon_request_ids': self._json(
                        response.get('_amazon_request_ids') or []
                    ),
                })
                self._refresh_counts()
                completed_at = fields.Datetime.now()
                self.write({
                    'state': 'completed',
                    'snapshot_complete': True,
                    'completed_at': completed_at,
                })
                self.instance_id.sudo().write({
                    'last_inventory_audit_at': fields.Datetime.now(),
                    'last_stock_sync': fields.Datetime.now(),
                })
                log = self.env['amazon.sync.log'].sudo().log_start(
                    self.instance_id,
                    'inventory_reconciliation',
                    res_model=self._name,
                    res_id=self.id,
                )
                log.log_success(
                    summary=_(
                        "Inventory audit %s completed: %s checked, %s mismatches.",
                        self.name, self.products_checked, self.mismatch_count,
                    ),
                    records_processed=self.products_checked,
                    records_created=self.products_checked,
                    response_data={
                        'amazon_request_ids': response.get('_amazon_request_ids') or [],
                        'matched': self.matched_count,
                        'mismatches': self.mismatch_count,
                        'unmapped': self.unmapped_count,
                        'not_returned': self.not_returned_count,
                    },
                )
            return True
        except Exception as exc:
            _logger.exception("Amazon inventory audit %s failed", self.id)
            self._schedule_retry(exc)
            return False

    @api.model
    def cron_process_inventory_audits(self):
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_inventory_reconciliation_run
             WHERE state = 'queued'
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, run_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        """, [now])
        row = self.env.cr.fetchone()
        if not row:
            return False
        return self.browse(row[0]).with_context(
            amazon_source_model=self._name,
            amazon_source_id=row[0],
            amazon_operation='inventory_reconciliation',
        )._process_run()

    @api.model
    def cron_enqueue_inventory_audits(self):
        created = self.env[self._name]
        for instance in self.env['amazon.instance'].search([
            ('active', '=', True),
            ('inventory_reconciliation_enabled', '=', True),
        ]):
            pending = self.search_count([
                ('instance_id', '=', instance.id),
                ('state', 'in', ('queued', 'running')),
            ])
            if not pending:
                created |= self.create({
                    'instance_id': instance.id,
                    'trigger': 'scheduled',
                })
        return created

    def action_retry(self):
        for run in self:
            if run.state != 'failed':
                raise UserError(_("Only failed inventory audits can be retried."))
            run.write({
                'state': 'queued',
                'retry_count': 0,
                'next_run_at': False,
                'last_error': False,
            })
        return True

    def action_open_differences(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_action'
        ).read()[0]
        action['domain'] = [
            ('run_id', '=', self.id),
            ('status', '!=', 'matched'),
        ]
        action['context'] = {'default_run_id': self.id}
        return action

    def action_open_lines(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_action'
        ).read()[0]
        action['domain'] = [('run_id', '=', self.id)]
        action['context'] = {'default_run_id': self.id}
        return action

    def action_open_transfers(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Reconciliation Transfers'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('amazon_inventory_reconciliation_id.run_id', '=', self.id)],
        }


class AmazonInventoryReconciliation(models.Model):
    _name = 'amazon.inventory.reconciliation'
    _description = 'Amazon Inventory Reconciliation'
    _order = 'run_date desc, sku, id'
    _rec_name = 'sku'
    _check_company_auto = True

    run_id = fields.Many2one(
        'amazon.inventory.reconciliation.run', required=True,
        ondelete='cascade', index=True, check_company=True,
    )
    run_date = fields.Datetime(
        related='run_id.run_date', store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='run_id.instance_id', store=True,
        readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='run_id.company_id', store=True,
        readonly=True, index=True,
    )
    marketplace_id = fields.Char(
        related='instance_id.marketplace_id', store=True, readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one(
        'amazon.product', ondelete='set null', index=True,
    )
    odoo_product_id = fields.Many2one(
        'product.product', string='Product', ondelete='set null', index=True,
        check_company=True,
    )
    sku = fields.Char(string='SKU', required=True, index=True)
    amazon_returned = fields.Boolean(readonly=True, index=True)
    asin = fields.Char(string='ASIN', readonly=True)
    fnsku = fields.Char(string='FNSKU', readonly=True)
    amazon_condition = fields.Char(string='Amazon Condition', readonly=True)
    amazon_last_updated_raw = fields.Char(
        string='Amazon Last Updated', readonly=True,
        help="Exact lastUpdatedTime value returned by Amazon.",
    )
    amazon_sellable = fields.Float(readonly=True)
    amazon_reserved = fields.Float(readonly=True)
    amazon_unsellable = fields.Float(readonly=True)
    amazon_total = fields.Float(readonly=True)
    amazon_reserved_customer_orders = fields.Float(readonly=True)
    amazon_reserved_fc_transfers = fields.Float(readonly=True)
    amazon_reserved_fc_processing = fields.Float(readonly=True)
    amazon_inbound = fields.Float(
        readonly=True,
        help="Physical inbound: inboundShippedQuantity + inboundReceivingQuantity.",
    )
    amazon_inbound_working = fields.Float(
        readonly=True,
        help="Notified but not necessarily shipped; excluded from Odoo Transit comparison.",
    )
    amazon_inbound_shipped = fields.Float(readonly=True)
    amazon_inbound_receiving = fields.Float(readonly=True)
    odoo_received = fields.Float(
        string='Odoo Received / Staging', readonly=True,
        help="Physically received stock awaiting an Amazon disposition decision.",
    )
    odoo_sellable = fields.Float(readonly=True)
    odoo_reserved = fields.Float(readonly=True)
    odoo_unsellable = fields.Float(readonly=True)
    odoo_transit = fields.Float(readonly=True)
    difference_sellable = fields.Float(readonly=True)
    difference_reserved = fields.Float(readonly=True)
    difference_unsellable = fields.Float(readonly=True)
    difference_inbound = fields.Float(readonly=True)
    pending_sale_event_qty = fields.Float(
        string='Pending FBA Sale Event Qty', readonly=True, copy=False,
    )
    sale_overlap_state = fields.Selection([
        ('none', 'No Sale Event Overlap'),
        ('pending_event', 'Explained by Pending Sale Event'),
        ('snapshot_outflow', 'Snapshot Net Outflow / Await Event'),
        ('resolved', 'Resolved by Sale Event'),
    ], default='none', readonly=True, copy=False, index=True)
    suggested_action = fields.Selection([
        ('none', 'No Action'),
        ('sellable_to_reserved', 'Sellable → Reserved'),
        ('sellable_to_unsellable', 'Sellable → Unsellable'),
        ('transit_to_sellable', 'Transit → Sellable'),
        ('inventory_adjustment', 'Inventory Adjustment'),
        ('manual_review', 'Manual Review'),
    ], required=True, default='manual_review', readonly=True, index=True)
    severity = fields.Selection([
        ('normal', 'Normal'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    ], required=True, default='normal', readonly=True, index=True)
    status = fields.Selection([
        ('matched', 'Matched'),
        ('mismatch', 'Mismatch'),
        ('unmapped', 'Unmapped Amazon SKU'),
        ('not_returned', 'Not Returned / Review'),
        ('error', 'Error'),
        ('pending_review', 'Pending Review'),
        ('applied', 'Applied'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], required=True, default='pending_review', copy=False, index=True)
    adjustment_action = fields.Selection([
        ('none', 'No Transfer Selected'),
        ('received_to_sellable', 'Received / Staging → Sellable'),
        ('received_to_reserved', 'Received / Staging → Reserved'),
        ('received_to_unsellable', 'Received / Staging → Unsellable'),
        ('sellable_to_reserved', 'Sellable → Reserved'),
        ('sellable_to_unsellable', 'Sellable → Unsellable'),
        ('reserved_to_sellable', 'Reserved → Sellable'),
        ('reserved_to_unsellable', 'Reserved → Unsellable'),
        ('unsellable_to_sellable', 'Unsellable → Sellable'),
        ('unsellable_to_reserved', 'Unsellable → Reserved'),
    ], string='Reviewed Transfer', default='none', copy=False, index=True)
    adjustment_quantity = fields.Float(copy=False)
    adjustment_reason = fields.Text(copy=False)
    large_adjustment_confirmed = fields.Boolean(
        string='Confirm Large Adjustment', copy=False,
        help=(
            "Required when the transfer is at least 100 units or more than half "
            "of the currently available source stock."
        ),
    )
    adjustment_reviewed = fields.Boolean(readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, ondelete='set null',
    )
    applied_picking_id = fields.Many2one(
        'stock.picking', string='Applied Transfer', copy=False, readonly=True,
        ondelete='restrict', check_company=True,
    )
    applied_at = fields.Datetime(copy=False, readonly=True)
    applied_by_id = fields.Many2one(
        'res.users', copy=False, readonly=True, ondelete='set null',
    )
    error_message = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    raw_response = fields.Text(
        string='Raw Amazon Response', readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_sku_per_run = models.Constraint(
        'UNIQUE (run_id, sku)',
        'A SKU can appear only once in an inventory reconciliation run.',
    )

    def _check_apply_access(self):
        if (
            self.env.su
            or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager')
            or self.env.user.has_group('stock.group_stock_manager')
        ):
            return
        raise AccessError(_(
            "Only an Amazon Connector Manager or Inventory Administrator can "
            "apply inventory reconciliation actions."
        ))

    def _pending_sale_quantity(self):
        self.ensure_one()
        if not self.odoo_product_id:
            return 0.0
        events = self.env['amazon.fba.sale.stock.event'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('product_id', '=', self.odoo_product_id.id),
            ('state', 'in', ('pending', 'processing', 'manual_review', 'failed')),
        ])
        return sum(max(
            event.amazon_cumulative_fulfilled_qty - event.processed_fulfilled_qty,
            0.0,
        ) for event in events)

    def _refresh_sale_event_overlap(self):
        """Re-evaluate a stored snapshot against live event-owned stock."""
        refreshed_runs = self.env['amazon.inventory.reconciliation.run']
        for line in self:
            if not line.odoo_product_id or line.status in ('applied', 'ignored'):
                continue
            self.env['amazon.fba.sale.stock.event']._advisory_lock(
                line.instance_id.id, line.odoo_product_id.id,
            )
            locations = line.run_id._validate_locations()
            odoo_values = line.run_id._odoo_quantities(line.odoo_product_id, locations)
            amazon_values = {
                'sellable': line.amazon_sellable,
                'reserved': line.amazon_reserved,
                'unsellable': line.amazon_unsellable,
                'transit': line.amazon_inbound,
            }
            differences, suggested, status, severity = line.run_id._classification(
                amazon_values, odoo_values, line.odoo_product_id.uom_id.rounding or 0.01,
            )
            pending = line._pending_sale_quantity()
            amazon_disposition = sum(amazon_values[key] for key in ('sellable', 'reserved', 'unsellable'))
            odoo_disposition = sum(odoo_values[key] for key in ('sellable', 'reserved', 'unsellable'))
            overlap = 'none'
            message = False
            if status == 'matched':
                overlap = 'resolved' if line.sale_overlap_state in (
                    'pending_event', 'snapshot_outflow', 'resolved',
                ) else 'none'
            elif pending > 0:
                overlap = 'pending_event'
                message = _(
                    "The snapshot difference overlaps %s units of durable, unprocessed Amazon FBA sale events. "
                    "Do not apply a snapshot transfer; let the sale event processor consume Sellable first.",
                    pending,
                )
            elif amazon_disposition < odoo_disposition:
                overlap = 'snapshot_outflow'
                message = _(
                    "Amazon's snapshot shows %s fewer disposition units than Odoo. A snapshot is not a unique "
                    "sale event, so this net outflow cannot be applied as a disposition transfer. Await/import "
                    "the Amazon order-item fulfillment event or investigate the unexplained loss.",
                    odoo_disposition - amazon_disposition,
                )
            line.with_context(keep_inventory_adjustment_review=True).write({
                'odoo_received': odoo_values['received'],
                'odoo_sellable': odoo_values['sellable'],
                'odoo_reserved': odoo_values['reserved'],
                'odoo_unsellable': odoo_values['unsellable'],
                'odoo_transit': odoo_values['transit'],
                'difference_sellable': differences['sellable'],
                'difference_reserved': differences['reserved'],
                'difference_unsellable': differences['unsellable'],
                'difference_inbound': differences['transit'],
                'suggested_action': suggested,
                'status': status,
                'severity': severity,
                'pending_sale_event_qty': pending,
                'sale_overlap_state': overlap,
                'error_message': message,
            })
            refreshed_runs |= line.run_id
        for run in refreshed_runs:
            run._refresh_counts()
        return True

    def write(self, vals):
        review_inputs = {
            'adjustment_action', 'adjustment_quantity', 'adjustment_reason',
            'large_adjustment_confirmed',
        }
        if review_inputs.intersection(vals) and not self.env.context.get(
            'keep_inventory_adjustment_review'
        ):
            vals = dict(vals, adjustment_reviewed=False, reviewed_at=False, reviewed_by_id=False)
        return super().write(vals)

    def _movement(self):
        self.ensure_one()
        instance = self.instance_id
        locations = {
            'received': instance.fba_received_location_id,
            'sellable': instance.fba_sellable_location_id,
            'reserved': instance.fba_reserved_location_id,
            'unsellable': instance.fba_unsellable_location_id,
        }
        roles = {
            'received_to_sellable': ('received', 'sellable'),
            'received_to_reserved': ('received', 'reserved'),
            'received_to_unsellable': ('received', 'unsellable'),
            'sellable_to_reserved': ('sellable', 'reserved'),
            'sellable_to_unsellable': ('sellable', 'unsellable'),
            'reserved_to_sellable': ('reserved', 'sellable'),
            'reserved_to_unsellable': ('reserved', 'unsellable'),
            'unsellable_to_sellable': ('unsellable', 'sellable'),
            'unsellable_to_reserved': ('unsellable', 'reserved'),
        }
        if self.adjustment_action not in roles:
            raise UserError(_(
                "Select a reviewed transfer between existing Amazon FBA inventory locations."
            ))
        source_role, destination_role = roles[self.adjustment_action]
        return (
            locations[source_role], locations[destination_role],
            self.adjustment_quantity, 'reconciliation_disposition',
            source_role, destination_role,
        )

    def _validate_adjustment(self):
        self.ensure_one()
        if self.run_id.state != 'completed' or not self.run_id.snapshot_complete:
            raise UserError(_("Only a complete inventory snapshot can be adjusted."))
        latest_run = self.env['amazon.inventory.reconciliation.run'].search([
            ('instance_id', '=', self.instance_id.id),
            ('state', '=', 'completed'),
            ('snapshot_complete', '=', True),
        ], order='completed_at desc, id desc', limit=1)
        if latest_run and latest_run != self.run_id:
            raise UserError(_(
                "A newer complete inventory snapshot exists. Review that run instead."
            ))
        if self.status not in ('mismatch', 'pending_review'):
            raise UserError(_("Only a mapped inventory mismatch can be adjusted."))
        if not self.odoo_product_id or not self.odoo_product_id.is_storable:
            raise UserError(_("The SKU must map to an inventory-tracked Odoo product."))
        if not (self.adjustment_reason or '').strip():
            raise UserError(_("Record the reason and evidence for this reconciliation transfer."))
        source, destination, quantity, movement_type, source_role, destination_role = self._movement()
        if not source or not destination or source == destination:
            raise UserError(_("The configured reconciliation locations are invalid."))
        if source.company_id != self.company_id or destination.company_id != self.company_id:
            raise UserError(_("Reconciliation locations must belong to the audit company."))
        rounding = self.odoo_product_id.uom_id.rounding or 0.01
        if float_compare(quantity, 0.0, precision_rounding=rounding) <= 0:
            raise UserError(_("The reviewed transfer quantity must be positive."))
        differences = {
            'sellable': self.difference_sellable,
            'reserved': self.difference_reserved,
            'unsellable': self.difference_unsellable,
        }
        destination_shortage = differences[destination_role]
        if float_compare(destination_shortage, quantity, precision_rounding=rounding) < 0:
            raise UserError(_(
                "The reviewed quantity exceeds the %s difference in this snapshot.",
                destination_role,
            ))
        if source_role != 'received':
            source_excess = -differences[source_role]
            if float_compare(source_excess, quantity, precision_rounding=rounding) < 0:
                raise UserError(_(
                    "The reviewed quantity exceeds the %s excess in this snapshot.",
                    source_role,
                ))
        source_available = self.odoo_product_id.with_company(self.company_id).with_context(
            location=source.id,
        ).qty_available
        if float_compare(source_available, quantity, precision_rounding=rounding) < 0:
            raise UserError(_(
                "Only %s %s is currently available in %s.",
                source_available, self.odoo_product_id.uom_id.name, source.display_name,
            ))
        is_large = (
            float_compare(quantity, 100.0, precision_rounding=rounding) >= 0
            or float_compare(quantity, source_available * 0.5, precision_rounding=rounding) > 0
        )
        if is_large and not self.large_adjustment_confirmed:
            raise UserError(_(
                "This is a large reconciliation transfer (%s of %s available units). "
                "Review it and enable Confirm Large Adjustment before continuing.",
                quantity, source_available,
            ))
        return source, destination, quantity, movement_type

    def action_mark_adjustment_reviewed(self):
        self._check_apply_access()
        for reconciliation in self:
            reconciliation._validate_adjustment()
            reconciliation.with_context(keep_inventory_adjustment_review=True).write({
                'adjustment_reviewed': True,
                'reviewed_at': fields.Datetime.now(),
                'reviewed_by_id': self.env.user.id,
                'error_message': False,
            })
        return True

    def _internal_picking_type(self, source, destination):
        self.ensure_one()
        warehouse = destination.warehouse_id or source.warehouse_id
        picking_type = (
            warehouse.int_type_id
            if warehouse and warehouse.company_id == self.company_id
            else False
        )
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', self.company_id.id),
            ], order='warehouse_id, sequence, id', limit=1)
        if not picking_type:
            raise UserError(_("No Internal Transfer operation type exists for the audit company."))
        return picking_type

    def _apply_suggested_action(self):
        self.ensure_one()
        self._check_apply_access()
        with self.env.cr.savepoint():
            self.env.cr.execute(
                'SELECT id FROM amazon_inventory_reconciliation '
                'WHERE id = %s FOR UPDATE',
                [self.id],
            )
            self.invalidate_recordset()
            if self.odoo_product_id:
                self.env['amazon.fba.sale.stock.event']._advisory_lock(
                    self.instance_id.id, self.odoo_product_id.id,
                )
                self._refresh_sale_event_overlap()
                self.invalidate_recordset()
                if self.status == 'matched':
                    raise UserError(_(
                        "This snapshot now matches after FBA sale-event processing; no reconciliation transfer is needed."
                    ))
                if self.sale_overlap_state in ('pending_event', 'snapshot_outflow'):
                    raise UserError(self.error_message or _(
                        "This snapshot overlaps event-owned FBA sale depletion and cannot create a stock transfer."
                    ))
            if self.status not in ('mismatch', 'pending_review'):
                raise UserError(_("This reconciliation difference is no longer pending."))
            if self.applied_picking_id:
                raise UserError(_("This reconciliation difference already has a transfer."))
            if not self.adjustment_reviewed:
                raise UserError(_("Review this transfer before applying it."))
            source, destination, quantity, movement_type = self._validate_adjustment()
            picking_type = self._internal_picking_type(source, destination)
            picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
                'picking_type_id': picking_type.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
                'company_id': self.company_id.id,
                'origin': self.run_id.name,
                'amazon_instance_id': self.instance_id.id,
                'amazon_inventory_reconciliation_id': self.id,
                'amazon_fba_movement_type': movement_type,
                'move_type': 'one',
                'note': _(
                    "Reviewed Amazon FBA inventory reconciliation for %s.\nReason: %s",
                    self.sku, self.adjustment_reason,
                ),
                'move_ids': [Command.create({
                    'product_id': self.odoo_product_id.id,
                    'product_uom_qty': quantity,
                    'product_uom': self.odoo_product_id.uom_id.id,
                    'location_id': source.id,
                    'location_dest_id': destination.id,
                    'company_id': self.company_id.id,
                })],
            })
            picking.action_confirm()
            picking.action_assign()
            move = picking.move_ids
            if (
                move.state != 'assigned'
                or move.product_uom.compare(move.quantity, move.product_uom_qty) < 0
            ):
                raise UserError(_(
                    "Insufficient stock in %s; standard Odoo reservation could not reserve %s %s.",
                    source.display_name, quantity, self.odoo_product_id.uom_id.name,
                ))
            if not move.move_line_ids:
                raise UserError(_("Odoo did not create the required stock move line."))
            result = picking.with_context(
                picking_ids_not_to_backorder=picking.ids,
            ).button_validate()
            if isinstance(result, dict) or picking.state != 'done':
                raise UserError(_("Transfer %s requires manual stock details.", picking.name))
            self.write({
                'status': 'applied',
                'applied_picking_id': picking.id,
                'applied_at': fields.Datetime.now(),
                'applied_by_id': self.env.user.id,
                'error_message': False,
            })
            return picking

    def action_apply_suggested_action(self):
        for reconciliation in self:
            reconciliation._apply_suggested_action()
            reconciliation.run_id._refresh_counts()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Inventory Reconciliation'),
                'message': _("The reviewed stock transfer was applied."),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_ignore_difference(self):
        self._check_apply_access()
        for reconciliation in self:
            if reconciliation.status not in (
                'mismatch', 'pending_review', 'unmapped', 'not_returned', 'error', 'failed',
            ):
                raise UserError(_("Only a pending or failed difference can be ignored."))
            if reconciliation.applied_picking_id:
                raise UserError(_("An applied reconciliation transfer cannot be ignored."))
            reconciliation.write({
                'status': 'ignored',
            })
            reconciliation.run_id._refresh_counts()
        return True

    def action_open_transfers(self):
        self.ensure_one()
        if not self.applied_picking_id:
            raise UserError(_("This reconciliation difference has no applied transfer."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Reconciliation Transfer'),
            'res_model': 'stock.picking',
            'res_id': self.applied_picking_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class AmazonInstanceInventoryReconciliation(models.Model):
    _inherit = 'amazon.instance'

    inventory_reconciliation_enabled = fields.Boolean(
        string='Scheduled Inventory Audit', default=True,
        help="Create a read-only Amazon FBA inventory audit on the daily schedule.",
    )
    last_inventory_audit_at = fields.Datetime(readonly=True)
    reconciliation_run_ids = fields.One2many(
        'amazon.inventory.reconciliation.run', 'instance_id',
        string='Inventory Audit History',
    )
    reconciliation_run_count = fields.Integer(
        compute='_compute_reconciliation_run_count', string='Inventory Audits',
    )

    @api.depends('reconciliation_run_ids')
    def _compute_reconciliation_run_count(self):
        for instance in self:
            instance.reconciliation_run_count = len(instance.reconciliation_run_ids)

    def action_run_inventory_audit(self):
        self.ensure_one()
        self._check_required_fields()
        active_run = self.env['amazon.inventory.reconciliation.run'].search([
            ('instance_id', '=', self.id),
            ('state', 'in', ('queued', 'running')),
        ], limit=1)
        run = active_run or self.env['amazon.inventory.reconciliation.run'].create({
            'instance_id': self.id,
            'trigger': 'manual',
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Audit'),
            'res_model': 'amazon.inventory.reconciliation.run',
            'res_id': run.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_inventory_audits(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_run_action'
        ).read()[0]
        action['domain'] = [('instance_id', '=', self.id)]
        action['context'] = {'default_instance_id': self.id}
        return action


class StockPickingInventoryReconciliation(models.Model):
    _inherit = 'stock.picking'

    amazon_inventory_reconciliation_id = fields.Many2one(
        'amazon.inventory.reconciliation', string='Amazon Inventory Reconciliation',
        copy=False, readonly=True, ondelete='restrict', check_company=True,
    )
    amazon_fba_movement_type = fields.Selection(selection_add=[
        ('reconciliation_reserved', 'Amazon Sellable to Reserved'),
        ('reconciliation_unsellable', 'Amazon Sellable to Unsellable'),
        ('reconciliation_sellable', 'Amazon Transit to Sellable'),
        ('reconciliation_disposition', 'Reviewed Amazon FBA Reconciliation Transfer'),
    ], ondelete={
        'reconciliation_reserved': 'set null',
        'reconciliation_unsellable': 'set null',
        'reconciliation_sellable': 'set null',
        'reconciliation_disposition': 'set null',
    })
