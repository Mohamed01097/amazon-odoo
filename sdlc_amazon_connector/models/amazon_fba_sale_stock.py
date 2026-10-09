import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare


_logger = logging.getLogger(__name__)

# Error code that marks an FBA sale stock event as intentionally held for human
# review by the controlled single-order import. While this hold is in place, no
# normal sync/import/upsert path may return the event to 'pending' (and thus to
# automatic processing); only the explicit Retry (action_retry) releases it.
CONTROLLED_IMPORT_HOLD_CODE = 'HELD_FOR_CONTROLLED_IMPORT'
CUTOVER_PRE_CODE = 'PRE_CUTOVER_FBA_ORDER'
CUTOVER_MISSING_DATE_CODE = 'MISSING_FBA_PURCHASE_DATE'
CUTOVER_NOT_CONFIGURED_CODE = 'FBA_CUTOVER_NOT_CONFIGURED'


class AmazonFbaSaleStockEvent(models.Model):
    """Durable, cumulative stock owner for Amazon-fulfilled order items."""

    _name = 'amazon.fba.sale.stock.event'
    _description = 'Amazon FBA Sale Stock Event'
    _order = 'next_run_at, id'
    _check_company_auto = True
    _rec_name = 'amazon_order_item_id'

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    order_id = fields.Many2one(
        'amazon.sale.order', required=True, ondelete='cascade', index=True,
        readonly=True,
    )
    order_line_id = fields.Many2one(
        'amazon.sale.order.line', required=True, ondelete='cascade', index=True,
        readonly=True,
    )
    amazon_order_ref = fields.Char(required=True, index=True, readonly=True)
    amazon_order_item_id = fields.Char(required=True, index=True, readonly=True)
    sku = fields.Char(required=True, index=True, readonly=True)
    product_id = fields.Many2one(
        'product.product', ondelete='restrict', index=True,
        check_company=True, readonly=True,
    )
    ordered_quantity = fields.Float(readonly=True)
    amazon_cumulative_fulfilled_qty = fields.Float(
        string='Amazon Cumulative Shipped', required=True, default=0.0,
        readonly=True,
    )
    processed_fulfilled_qty = fields.Float(
        string='Odoo Processed Shipped', required=True, default=0.0,
        readonly=True,
    )
    last_delta_qty = fields.Float(readonly=True, copy=False)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('historical', 'Historical Before Cutover'),
        ('done', 'Done'),
        ('manual_review', 'Manual Review'),
        ('failed', 'Failed'),
    ], required=True, default='pending', copy=False, index=True)
    picking_ids = fields.One2many(
        'stock.picking', 'amazon_fba_sale_stock_event_id',
        string='Sale Stock Pickings', readonly=True,
    )
    last_picking_id = fields.Many2one(
        'stock.picking', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
    )
    attempt_count = fields.Integer(default=0, readonly=True, copy=False)
    max_attempts = fields.Integer(default=5, readonly=True)
    next_run_at = fields.Datetime(default=fields.Datetime.now, index=True, copy=False)
    amazon_evidence_updated_at = fields.Datetime(readonly=True, copy=False)
    last_activity_at = fields.Datetime(default=fields.Datetime.now, index=True, copy=False)
    started_at = fields.Datetime(readonly=True, copy=False)
    finished_at = fields.Datetime(readonly=True, copy=False)
    last_processed_at = fields.Datetime(readonly=True, copy=False)
    historical_cutover_at = fields.Datetime(
        string='Historical Cutover At', readonly=True, copy=False,
        help="Cutover timestamp that classified this event as historical.",
    )
    cutover_baseline_fulfilled_qty = fields.Float(
        default=0.0, readonly=True, copy=False,
        help="Units fulfilled before/on cutover, from Amazon shipment evidence. "
             "Represents inventory already absent from the opening snapshot. "
             "processed_fulfilled_qty is initialised to this value for pre-cutover orders.",
    )
    historical_repaired_at = fields.Datetime(readonly=True, copy=False)
    historical_repaired_qty = fields.Float(readonly=True, copy=False)
    historical_reversal_picking_id = fields.Many2one(
        'stock.picking', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
    )
    last_error_code = fields.Char(readonly=True, copy=False, index=True)
    last_error_message = fields.Text(readonly=True, copy=False)
    cutover_manual_release = fields.Boolean(
        default=False, readonly=True, copy=False, index=True,
        help="Set when a human explicitly releases this event via Retry. It is the "
             "only authorized path that lets the automatic processor move stock for an "
             "order purchased before the FBA Automatic Stock Cutover.",
    )
    responsible_user_id = fields.Many2one(
        'res.users', default=lambda self: self.env.user, readonly=True, index=True,
    )

    _unique_amazon_item = models.Constraint(
        'UNIQUE (instance_id, amazon_order_ref, amazon_order_item_id)',
        'An Amazon FBA order item can have only one sale stock event per instance.',
    )
    _valid_quantities = models.Constraint(
        'CHECK (amazon_cumulative_fulfilled_qty >= 0 AND processed_fulfilled_qty >= 0 '
        'AND processed_fulfilled_qty <= amazon_cumulative_fulfilled_qty '
        'AND amazon_cumulative_fulfilled_qty <= ordered_quantity)',
        'FBA sale stock event quantities must be cumulative, non-negative, and not exceed the order quantity.',
    )

    def _is_controlled_import_hold(self):
        """True when this event is intentionally held for controlled-import review.

        The hold is the single source of truth for 'do not auto-process'. It is
        cleared only by the explicit Retry release (``action_retry``).
        """
        self.ensure_one()
        return (
            self.state == 'manual_review'
            and self.last_error_code == CONTROLLED_IMPORT_HOLD_CODE
        )

    @api.model
    def _fba_cutover_reason(self, instance, purchase_date, has_positive_cumulative=True):
        """Return a hold reason code when an order is NOT auto-eligible by the cutover,
        or None when it is eligible (or the cutover gate is inactive).

        The gate is ACTIVE only once ``fba_auto_process_from`` is configured on the
        instance. Comparison is UTC-naive (Odoo stores datetimes as naive UTC), so the
        Amazon purchase timestamp is compared directly to the cutover. Import/create
        dates are never substituted. Fails closed on a missing/invalid purchase date.
        """
        cutover = instance.fba_auto_process_from
        if not cutover:
            return None  # gate inactive; fail-closed for auto-import is enforced at the import boundary
        if not has_positive_cumulative:
            return None  # zero-quantity (e.g. canceled) carries no stock delta to gate
        if not purchase_date:
            return CUTOVER_MISSING_DATE_CODE
        if purchase_date < cutover:
            return CUTOVER_PRE_CODE
        return None

    @api.model
    def _advisory_lock(self, instance_id, product_id):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
            ['amazon_fba_sale_stock:%s' % int(instance_id), int(product_id)],
        )

    @api.model
    def _is_order_before_fba_sale_stock_cutover(self, order):
        order = order.sudo()
        cutover_at = order.instance_id.fba_sale_stock_cutover_at
        return bool(cutover_at and order.purchase_date and order.purchase_date < cutover_at)

    def _is_before_fba_sale_stock_cutover(self, cutover_at=False):
        self.ensure_one()
        cutover = cutover_at or self.instance_id.fba_sale_stock_cutover_at
        return bool(cutover and self.order_id.purchase_date and self.order_id.purchase_date < cutover)

    @api.model
    def _is_cutover_v2_active(self, instance):
        """Return True when the instance has an activated v2 cutover run."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run'].sudo()
        return bool(CutoverRun.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'activated'),
        ], limit=1))

    @api.model
    def _lookup_cutover_v2_baseline(self, order):
        """Look up the pre-cutover fulfilled quantity from the activated cutover run."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run'].sudo()
        run = CutoverRun.search([
            ('instance_id', '=', order.instance_id.id),
            ('state', '=', 'activated'),
        ], limit=1)
        if not run:
            return 0.0
        return run.get_baseline_for_order_item(
            order.amazon_order_ref,
            order.sudo().line_ids[0].amazon_order_item_id if order.sudo().line_ids else '',
        )

    @api.model
    def _lookup_cutover_v2_baseline_for_line(self, instance, amazon_order_ref, amazon_order_item_id):
        """Look up B from the activated cutover run for a specific order line."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run'].sudo()
        run = CutoverRun.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'activated'),
        ], limit=1)
        if not run:
            return 0.0
        return run.get_baseline_for_order_item(amazon_order_ref, amazon_order_item_id)

    @api.model
    def _is_order_within_v2_coverage(self, instance, purchase_date):
        """Return True when purchase_date is within the activated v2 run's coverage."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run'].sudo()
        run = CutoverRun.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'activated'),
        ], limit=1)
        if not run:
            return False
        return run.is_order_within_coverage(purchase_date)

    @api.model
    def _get_v2_coverage_start(self, instance):
        """Return the history_start_at of the activated v2 run, or False."""
        CutoverRun = self.env['amazon.fba.sale.stock.cutover.run'].sudo()
        run = CutoverRun.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'activated'),
        ], limit=1)
        return run.history_start_at if run else False

    def _mark_historical(self, cumulative_quantity=False, cutover_at=False,
                         reversal_picking=False, repaired=False,
                         repaired_quantity=False):
        self.ensure_one()
        cumulative = (
            self.amazon_cumulative_fulfilled_qty
            if cumulative_quantity is False
            else cumulative_quantity
        )
        now = fields.Datetime.now()
        values = {
            'amazon_cumulative_fulfilled_qty': cumulative,
            'processed_fulfilled_qty': cumulative,
            'last_delta_qty': 0,
            'state': 'historical',
            'next_run_at': False,
            'last_activity_at': now,
            'finished_at': now,
            'historical_cutover_at': cutover_at or self.instance_id.fba_sale_stock_cutover_at,
            'last_error_code': False,
            'last_error_message': False,
        }
        if reversal_picking:
            values['historical_reversal_picking_id'] = reversal_picking.id
        if repaired:
            values['historical_repaired_at'] = now
        if repaired_quantity is not False:
            values['historical_repaired_qty'] = repaired_quantity
        self.write(values)
        self.order_line_id.sudo().write({
            'amazon_cumulative_fulfilled_qty': cumulative,
            'odoo_processed_fulfilled_qty': cumulative,
        })
        control = self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ], limit=1)
        if control:
            control.mark_source_resolved()
        return True

    @api.model
    def upsert_from_order_line(self, order_line, cumulative_quantity, evidence_updated_at=False):
        """Persist trusted item-level cumulative fulfillment evidence idempotently."""
        order_line.ensure_one()
        order = order_line.order_id
        if order.fulfillment_channel != 'AFN':
            return self.browse()
        if not order_line.amazon_order_item_id:
            raise ValidationError(_("Amazon Order Item ID is required for FBA stock ownership."))
        if not order_line.sku:
            raise ValidationError(_("Amazon SKU is required for FBA stock ownership."))
        product = order_line.odoo_product_id
        try:
            cumulative = float(cumulative_quantity or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Amazon cumulative fulfilled quantity is invalid.")) from exc
        rounding = product.uom_id.rounding if product and product.is_storable else 0.01
        if float_compare(cumulative, 0.0, precision_rounding=rounding) < 0:
            raise ValidationError(_("Amazon cumulative fulfilled quantity cannot be negative."))
        if float_compare(cumulative, order_line.quantity, precision_rounding=rounding) > 0:
            raise ValidationError(_(
                "Amazon cumulative fulfilled quantity %s exceeds ordered quantity %s for %s.",
                cumulative, order_line.quantity, order_line.amazon_order_item_id,
            ))
        historical_before_cutover = self._is_order_before_fba_sale_stock_cutover(order)
        cutover_v2_active = self._is_cutover_v2_active(order.instance_id)
        domain = [
            ('instance_id', '=', order.instance_id.id),
            ('amazon_order_ref', '=', order.amazon_order_ref),
            ('amazon_order_item_id', '=', order_line.amazon_order_item_id),
        ]
        event = self.sudo().search(domain, limit=1)
        values = {
            'order_id': order.id,
            'order_line_id': order_line.id,
            'sku': order_line.sku,
            'product_id': product.id if product and product.is_storable else False,
            'ordered_quantity': order_line.quantity,
            'amazon_evidence_updated_at': evidence_updated_at or fields.Datetime.now(),
            'last_activity_at': fields.Datetime.now(),
        }
        if not event:
            if historical_before_cutover and cutover_v2_active:
                within_coverage = self._is_order_within_v2_coverage(
                    order.instance_id, order.purchase_date,
                )
                baseline = self._lookup_cutover_v2_baseline_for_line(
                    order.instance_id, order.amazon_order_ref,
                    order_line.amazon_order_item_id,
                )
                values.update({
                    'instance_id': order.instance_id.id,
                    'amazon_order_ref': order.amazon_order_ref,
                    'amazon_order_item_id': order_line.amazon_order_item_id,
                    'amazon_cumulative_fulfilled_qty': cumulative,
                    'cutover_baseline_fulfilled_qty': baseline,
                    'historical_cutover_at': order.instance_id.fba_sale_stock_cutover_at,
                })
                if not within_coverage and baseline == 0.0:
                    coverage_start = self._get_v2_coverage_start(order.instance_id)
                    values.update({
                        'processed_fulfilled_qty': 0.0,
                        'state': 'manual_review',
                        'next_run_at': False,
                        'finished_at': False,
                        'last_error_code': 'CUTOVER_BASELINE_OUTSIDE_COVERAGE',
                        'last_error_message': _(
                            "Order %s (purchased %s) is older than the cutover evidence "
                            "coverage start %s. B=0 cannot be proven — manual review required.",
                            order.amazon_order_ref,
                            fields.Datetime.to_string(order.purchase_date),
                            fields.Datetime.to_string(coverage_start) if coverage_start else 'unknown',
                        ),
                    })
                elif float_compare(baseline, cumulative, precision_rounding=rounding) > 0:
                    values.update({
                        'processed_fulfilled_qty': 0.0,
                        'state': 'manual_review',
                        'next_run_at': False,
                        'finished_at': False,
                        'last_error_code': 'CUTOVER_BASELINE_EXCEEDS_CUMULATIVE',
                        'last_error_message': _(
                            "Cutover baseline B=%s exceeds current cumulative C=%s for item %s. "
                            "Data inconsistency — manual review required.",
                            baseline, cumulative, order_line.amazon_order_item_id,
                        ),
                    })
                else:
                    values['processed_fulfilled_qty'] = baseline
                    delta_exists = (
                        product and product.is_storable
                        and float_compare(cumulative, baseline, precision_rounding=rounding) > 0
                    )
                    if delta_exists:
                        values.update({
                            'state': 'pending',
                            'next_run_at': fields.Datetime.now(),
                            'finished_at': False,
                        })
                    else:
                        values.update({
                            'state': 'done' if product and product.is_storable else 'manual_review',
                            'next_run_at': False,
                            'finished_at': fields.Datetime.now(),
                        })
                        if not product or not product.is_storable:
                            values['last_error_code'] = 'UNMAPPED_FBA_SKU'
                try:
                    with self.env.cr.savepoint():
                        event = self.sudo().create(values)
                except IntegrityError:
                    event = self.sudo().search(domain, limit=1)
                    if not event:
                        raise
                else:
                    order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
                    if event.state == 'manual_review':
                        event._record_manual_review()
                    return event
            elif historical_before_cutover:
                values.update({
                    'instance_id': order.instance_id.id,
                    'amazon_order_ref': order.amazon_order_ref,
                    'amazon_order_item_id': order_line.amazon_order_item_id,
                    'amazon_cumulative_fulfilled_qty': cumulative,
                    'processed_fulfilled_qty': cumulative,
                    'state': 'historical',
                    'next_run_at': False,
                    'finished_at': fields.Datetime.now(),
                    'historical_cutover_at': order.instance_id.fba_sale_stock_cutover_at,
                })
                try:
                    with self.env.cr.savepoint():
                        event = self.sudo().create(values)
                except IntegrityError:
                    event = self.sudo().search(domain, limit=1)
                    if not event:
                        raise
                else:
                    order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
                    return event
            else:
                storable = bool(cumulative and product and product.is_storable)
                # Cutover gate (Phase 11C): a storable AFN order purchased before the
                # configured cutover (or with a missing purchase date) is held and never
                # auto-processed, independent of import-job lineage or the hold flag.
                cutover_reason = self._fba_cutover_reason(
                    order.instance_id, order.purchase_date, has_positive_cumulative=storable,
                ) if storable else None
                values.update({
                    'instance_id': order.instance_id.id,
                    'amazon_order_ref': order.amazon_order_ref,
                    'amazon_order_item_id': order_line.amazon_order_item_id,
                    'amazon_cumulative_fulfilled_qty': cumulative,
                    'processed_fulfilled_qty': 0,
                    'state': (
                        ('manual_review' if cutover_reason else 'pending')
                        if storable else ('manual_review' if cumulative else 'done')
                    ),
                    'next_run_at': (
                        fields.Datetime.now() if (storable and not cutover_reason) else False
                    ),
                    'finished_at': fields.Datetime.now() if not cumulative else False,
                })
                if cutover_reason:
                    values['last_error_code'] = cutover_reason
                    values['last_error_message'] = _(
                        "Order %s is not eligible for automatic FBA stock processing "
                        "(%s). Held pending the FBA Automatic Stock Cutover policy.",
                        order.amazon_order_ref, cutover_reason,
                    )
                try:
                    with self.env.cr.savepoint():
                        event = self.sudo().create(values)
                except IntegrityError:
                    event = self.sudo().search(domain, limit=1)
                    if not event:
                        raise
                else:
                    if cutover_reason:
                        order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
                        event._record_manual_review()
                        return event
        self.env.cr.execute(
            'SELECT id FROM amazon_fba_sale_stock_event WHERE id = %s FOR UPDATE',
            [event.id],
        )
        event.invalidate_recordset()
        # Durable controlled-import hold (centralized): a human-held event must
        # never be returned to 'pending' by any normal sync / re-import / status-sync
        # / AFN upsert path, so it can never be auto-processed by the FBA stock cron.
        # Refresh only safe informational quantities and keep the hold intact. The
        # explicit Retry (action_retry) is the sole release boundary.
        if event._is_controlled_import_hold():
            safe_values = {
                'amazon_cumulative_fulfilled_qty': cumulative,
                'ordered_quantity': order_line.quantity,
                'amazon_evidence_updated_at': values.get('amazon_evidence_updated_at'),
                'last_activity_at': values.get('last_activity_at'),
            }
            event.write(safe_values)
            order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
            return event
        if historical_before_cutover and not cutover_v2_active:
            historical_values = dict(values, amazon_cumulative_fulfilled_qty=cumulative)
            event.write(historical_values)
            event._mark_historical(
                cumulative_quantity=cumulative,
                cutover_at=order.instance_id.fba_sale_stock_cutover_at,
            )
            return event
        if not product or not product.is_storable:
            message = _(
                "FBA order item %s (%s) is not mapped to an inventory-tracked Odoo product. "
                "No stock movement was attempted.",
                order_line.amazon_order_item_id, order_line.sku,
            )
            event.write(dict(
                values,
                amazon_cumulative_fulfilled_qty=cumulative,
                state='manual_review', next_run_at=False,
                last_error_code='UNMAPPED_FBA_SKU', last_error_message=message,
            ))
            event._record_manual_review()
            order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
            return event
        if float_compare(
            cumulative, event.amazon_cumulative_fulfilled_qty,
            precision_rounding=rounding,
        ) < 0:
            message = _(
                "Amazon cumulative fulfilled quantity decreased from %s to %s for item %s. "
                "No stock reversal was made; review the Amazon evidence.",
                event.amazon_cumulative_fulfilled_qty, cumulative,
                event.amazon_order_item_id,
            )
            event.write(dict(values, state='manual_review', next_run_at=False,
                             last_error_code='CUMULATIVE_QUANTITY_DECREASED',
                             last_error_message=message))
            event._record_manual_review()
            return event
        # Cutover gate on re-upsert (Phase 11C): a pre-cutover / missing-date order must
        # stay held and never be flipped to 'pending' by a later re-import or a cumulative
        # increase, unless a human explicitly released it (cutover_manual_release).
        cutover_reason = self._fba_cutover_reason(
            order.instance_id, order.purchase_date, has_positive_cumulative=True,
        )
        if cutover_reason and not event.cutover_manual_release:
            event.write(dict(
                values, amazon_cumulative_fulfilled_qty=cumulative,
                state='manual_review', next_run_at=False, finished_at=False,
                last_error_code=cutover_reason,
                last_error_message=_(
                    "Order %s is not eligible for automatic FBA stock processing (%s). "
                    "Held pending the FBA Automatic Stock Cutover policy.",
                    order.amazon_order_ref, cutover_reason,
                ),
            ))
            event._record_manual_review()
            order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
            return event
        values['amazon_cumulative_fulfilled_qty'] = cumulative
        if float_compare(cumulative, event.processed_fulfilled_qty, precision_rounding=rounding) > 0:
            values.update({
                'state': 'pending', 'next_run_at': fields.Datetime.now(),
                'finished_at': False, 'last_error_code': False,
                'last_error_message': False,
            })
        elif event.state not in ('manual_review', 'failed'):
            values.update({'state': 'done', 'next_run_at': False})
        event.write(values)
        order_line.sudo().write({'amazon_cumulative_fulfilled_qty': cumulative})
        return event

    def _validate_stock_configuration(self):
        self.ensure_one()
        instance = self.instance_id
        source = instance.fba_sellable_location_id
        destination = instance.fba_sold_customer_location_id
        picking_type = instance.fba_warehouse_id.out_type_id if instance.fba_warehouse_id else False
        if not source or source.usage != 'internal':
            raise UserError(_("Configure the Amazon FBA Sellable location before processing FBA sales."))
        if not destination or destination.usage != 'customer':
            raise UserError(_("Configure the Amazon FBA Sold / Customers location before processing FBA sales."))
        if not picking_type:
            raise UserError(_("Configure an FBA warehouse with an outgoing operation type."))
        return source, destination, picking_type

    def _available_sellable_quantity(self, source):
        self.ensure_one()
        return self.product_id.sudo().with_company(self.company_id).with_context(
            location=source.id,
        ).free_qty

    def _create_and_validate_delta_picking(self, delta):
        self.ensure_one()
        source, destination, picking_type = self._validate_stock_configuration()
        available = self._available_sellable_quantity(source)
        rounding = self.product_id.uom_id.rounding or 0.01
        if float_compare(available, delta, precision_rounding=rounding) < 0:
            raise UserError(_(
                "Insufficient FBA Sellable stock for %s: Amazon reports %s newly shipped, "
                "but Odoo has only %s available. Reconcile the inventory discrepancy; "
                "WH/Stock and negative stock were not used.",
                self.sku, delta, available,
            ))
        picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
            'picking_type_id': picking_type.id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'company_id': self.company_id.id,
            'origin': '%s / %s' % (self.amazon_order_ref, self.amazon_order_item_id),
            'amazon_instance_id': self.instance_id.id,
            'amazon_order_ref': self.amazon_order_ref,
            'amazon_fba_sale_stock_event_id': self.id,
            'amazon_fba_movement_type': 'fba_sale',
            'move_type': 'one',
            'note': _(
                "Amazon AFN cumulative fulfillment stock delta. Amazon order: %s; item: %s; SKU: %s.",
                self.amazon_order_ref, self.amazon_order_item_id, self.sku,
            ),
            'move_ids': [Command.create({
                'product_id': self.product_id.id,
                'product_uom_qty': delta,
                'product_uom': self.product_id.uom_id.id,
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
            or not move.move_line_ids
        ):
            raise UserError(_(
                "Odoo could not reserve the exact FBA sale delta %s for %s.", delta, self.sku,
            ))
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
            skip_backorder=True,
        ).button_validate()
        if isinstance(result, dict) or picking.state != 'done':
            raise UserError(_("FBA sale picking %s requires manual stock details.", picking.name))
        return picking

    def _validate_historical_repair_configuration(self):
        self.ensure_one()
        instance = self.instance_id
        source = instance.fba_sold_customer_location_id
        destination = instance.fba_sellable_location_id
        picking_type = instance.fba_warehouse_id.in_type_id if instance.fba_warehouse_id else False
        if not source or source.usage != 'customer':
            raise UserError(_("Configure the Amazon FBA Sold / Customers location before repairing historical FBA sales."))
        if not destination or destination.usage != 'internal':
            raise UserError(_("Configure the Amazon FBA Sellable location before repairing historical FBA sales."))
        if not picking_type:
            raise UserError(_("Configure an FBA warehouse with an incoming operation type."))
        return source, destination, picking_type

    def _quantity_at_location(self, location):
        self.ensure_one()
        return self.product_id.sudo().with_company(self.company_id).with_context(
            location=location.id,
        ).qty_available

    def _sum_done_picking_quantity(self, pickings, source, destination):
        self.ensure_one()
        quantity = 0.0
        for picking in pickings:
            if picking.state != 'done':
                raise UserError(_("Picking %s is not done.", picking.display_name))
            if picking.company_id != self.company_id:
                raise UserError(_("Picking %s belongs to a different company.", picking.display_name))
            if picking.location_id != source or picking.location_dest_id != destination:
                raise UserError(_(
                    "Picking %s does not use the expected %s -> %s locations.",
                    picking.display_name, source.display_name, destination.display_name,
                ))
            if not picking.move_ids:
                raise UserError(_("Picking %s has no stock moves.", picking.display_name))
            for move in picking.move_ids:
                if move.state != 'done':
                    raise UserError(_("Picking %s contains a non-done stock move.", picking.display_name))
                if move.product_id != self.product_id:
                    raise UserError(_(
                        "Picking %s contains product %s instead of %s.",
                        picking.display_name, move.product_id.display_name, self.product_id.display_name,
                    ))
                if move.location_id != source or move.location_dest_id != destination:
                    raise UserError(_(
                        "Picking %s contains a stock move with unexpected locations.",
                        picking.display_name,
                    ))
                quantity += move.product_uom._compute_quantity(
                    move.quantity, self.product_id.uom_id,
                )
        return quantity

    def _historical_repair_plan(self, cutover_at):
        self.ensure_one()
        if not self._is_before_fba_sale_stock_cutover(cutover_at):
            raise UserError(_(
                "FBA sale stock event %s is not historical for cutover %s.",
                self.display_name, fields.Datetime.to_string(cutover_at),
            ))
        if not self.product_id or not self.product_id.is_storable:
            raise UserError(_("Historical repair requires a mapped storable product."))
        source, destination, _picking_type = self._validate_historical_repair_configuration()
        sale_pickings = self.picking_ids.filtered(lambda picking: (
            picking.state == 'done' and picking.amazon_fba_movement_type == 'fba_sale'
        ))
        if not sale_pickings:
            raise UserError(_(
                "No completed event-owned FBA sale picking was found for %s.",
                self.display_name,
            ))
        sale_quantity = self._sum_done_picking_quantity(sale_pickings, destination, source)
        rounding = self.product_id.uom_id.rounding or 0.01
        if float_compare(sale_quantity, 0.0, precision_rounding=rounding) <= 0:
            raise UserError(_("Completed FBA sale pickings for %s have no positive quantity.", self.display_name))
        pending_reversal = self.picking_ids.filtered(lambda picking: (
            picking.amazon_fba_movement_type == 'fba_sale_historical_reversal'
            and picking.state != 'done'
        ))
        if pending_reversal:
            raise UserError(_(
                "Historical reversal picking %s is not done. Review it before rerunning repair.",
                ", ".join(pending_reversal.mapped('display_name')),
            ))
        reversal_pickings = self.picking_ids.filtered(lambda picking: (
            picking.state == 'done'
            and picking.amazon_fba_movement_type == 'fba_sale_historical_reversal'
        ))
        if reversal_pickings:
            reversal_quantity = self._sum_done_picking_quantity(reversal_pickings, source, destination)
            if float_compare(reversal_quantity, sale_quantity, precision_rounding=rounding) != 0:
                raise UserError(_(
                    "Existing historical reversal quantity %s does not match original sale quantity %s for %s.",
                    reversal_quantity, sale_quantity, self.display_name,
                ))
            return {
                'quantity': sale_quantity,
                'sale_picking_ids': sale_pickings.ids,
                'already_repaired': True,
                'reversal_picking_ids': reversal_pickings.ids,
            }

        available = self._quantity_at_location(source)
        if float_compare(available, sale_quantity, precision_rounding=rounding) < 0:
            raise UserError(_(
                "Cannot safely reverse historical event %s: Sold / Customers has %s available for %s, "
                "but the event-owned sale picking quantity is %s.",
                self.display_name, available, self.sku, sale_quantity,
            ))
        return {
            'quantity': sale_quantity,
            'sale_picking_ids': sale_pickings.ids,
            'already_repaired': False,
            'reversal_picking_ids': [],
        }

    def _create_historical_reversal_picking(self, quantity):
        self.ensure_one()
        source, destination, picking_type = self._validate_historical_repair_configuration()
        picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
            'picking_type_id': picking_type.id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'company_id': self.company_id.id,
            'origin': 'Historical FBA sale repair / %s / %s' % (
                self.amazon_order_ref, self.amazon_order_item_id,
            ),
            'amazon_instance_id': self.instance_id.id,
            'amazon_order_ref': self.amazon_order_ref,
            'amazon_fba_sale_stock_event_id': self.id,
            'amazon_fba_movement_type': 'fba_sale_historical_reversal',
            'move_type': 'one',
            'note': _(
                "Repair for pre-cutover Amazon AFN sale stock depletion. "
                "Restores only this event-owned completed movement from Sold / Customers to Sellable."
            ),
            'move_ids': [Command.create({
                'product_id': self.product_id.id,
                'product_uom_qty': quantity,
                'product_uom': self.product_id.uom_id.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
                'company_id': self.company_id.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
            skip_backorder=True,
        ).button_validate()
        if isinstance(result, dict) or picking.state != 'done':
            raise UserError(_("Historical FBA sale repair picking %s requires manual stock details.", picking.name))
        actual_quantity = self._sum_done_picking_quantity(picking, source, destination)
        rounding = self.product_id.uom_id.rounding or 0.01
        if float_compare(actual_quantity, quantity, precision_rounding=rounding) != 0:
            raise UserError(_(
                "Historical repair picking %s moved %s instead of %s.",
                picking.display_name, actual_quantity, quantity,
            ))
        return picking

    @api.model
    def repair_historical_processed_events(self, instance, cutover_at, dry_run=True, limit=None):
        """Repair pre-cutover FBA sale events that already consumed Sellable stock."""
        self.env['amazon.instance']._check_amazon_manager_access()
        if not instance:
            raise UserError(_("Historical FBA sale stock repair requires an Amazon instance."))
        if isinstance(instance, int):
            instance = self.env['amazon.instance'].browse(instance)
        instance = instance.exists()
        if len(instance) != 1:
            raise UserError(_("Historical FBA sale stock repair requires exactly one Amazon instance."))
        cutover = fields.Datetime.to_datetime(cutover_at)
        if not cutover:
            raise UserError(_("Historical FBA sale stock repair requires a cutover timestamp."))

        events = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('order_id.purchase_date', '<', cutover),
            ('processed_fulfilled_qty', '>', 0),
            ('picking_ids.state', '=', 'done'),
        ], order='id', limit=limit or None)
        summary = {
            'dry_run': bool(dry_run),
            'instance_id': instance.id,
            'instance_name': instance.display_name,
            'cutover_at': fields.Datetime.to_string(cutover),
            'candidate_count': len(events),
            'would_repair_count': 0,
            'would_restore_qty': 0.0,
            'repaired_count': 0,
            'restored_qty': 0.0,
            'already_repaired_count': 0,
            'manual_review_count': 0,
            'errors': [],
            'event_ids': events.ids,
        }

        for event in events:
            try:
                with self.env.cr.savepoint():
                    self.env.cr.execute(
                        'SELECT id FROM amazon_fba_sale_stock_event WHERE id = %s FOR UPDATE',
                        [event.id],
                    )
                    event.invalidate_recordset()
                    plan = event._historical_repair_plan(cutover)
                    quantity = plan['quantity']
                    if plan['already_repaired']:
                        summary['already_repaired_count'] += 1
                        if not dry_run and event.state != 'historical':
                            reversal = self.env['stock.picking'].sudo().browse(plan['reversal_picking_ids'][:1])
                            event._mark_historical(
                                cumulative_quantity=event.amazon_cumulative_fulfilled_qty,
                                cutover_at=cutover,
                                reversal_picking=reversal,
                                repaired_quantity=quantity,
                            )
                        continue
                    if dry_run:
                        summary['would_repair_count'] += 1
                        summary['would_restore_qty'] += quantity
                        continue
                    reversal_picking = event._create_historical_reversal_picking(quantity)
                    event._mark_historical(
                        cumulative_quantity=event.amazon_cumulative_fulfilled_qty,
                        cutover_at=cutover,
                        reversal_picking=reversal_picking,
                        repaired=True,
                        repaired_quantity=quantity,
                    )
                    summary['repaired_count'] += 1
                    summary['restored_qty'] += quantity
            except Exception as exc:
                message = str(exc)
                summary['manual_review_count'] += 1
                summary['errors'].append({
                    'event_id': event.id,
                    'amazon_order_ref': event.amazon_order_ref,
                    'amazon_order_item_id': event.amazon_order_item_id,
                    'sku': event.sku,
                    'error': message[:1000],
                })
                if not dry_run:
                    event.invalidate_recordset()
                    event.write({
                        'state': 'manual_review',
                        'next_run_at': False,
                        'last_error_code': 'HISTORICAL_REPAIR_REVIEW',
                        'last_error_message': message[:5000],
                        'last_activity_at': fields.Datetime.now(),
                    })
                    event._record_manual_review()
                _logger.warning("Historical FBA sale stock repair skipped event %s: %s", event.id, message)

        return summary

    def _process_locked(self):
        self.ensure_one()
        if self._is_before_fba_sale_stock_cutover():
            if self._is_cutover_v2_active(self.instance_id):
                pass  # v2: process normally using baseline delta
            else:
                self._mark_historical()
                return False
        self._advisory_lock(self.instance_id.id, self.product_id.id)
        rounding = self.product_id.uom_id.rounding or 0.01
        delta = self.amazon_cumulative_fulfilled_qty - self.processed_fulfilled_qty
        if float_compare(delta, 0.0, precision_rounding=rounding) <= 0:
            self.write({
                'state': 'done', 'next_run_at': False,
                'finished_at': fields.Datetime.now(),
                'last_activity_at': fields.Datetime.now(),
            })
            return False
        picking = self._create_and_validate_delta_picking(delta)
        now = fields.Datetime.now()
        self.write({
            'processed_fulfilled_qty': self.processed_fulfilled_qty + delta,
            'last_delta_qty': delta,
            'last_picking_id': picking.id,
            'state': 'done',
            'next_run_at': False,
            'last_processed_at': now,
            'last_activity_at': now,
            'finished_at': now,
            'last_error_code': False,
            'last_error_message': False,
        })
        self.order_line_id.sudo().write({
            'odoo_processed_fulfilled_qty': self.processed_fulfilled_qty,
        })
        self._refresh_inventory_overlap_lines()
        control = self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ], limit=1)
        if control:
            control.mark_source_resolved()
        return picking

    def _record_manual_review(self):
        for event in self:
            self.env['amazon.operation.control'].sudo().record_source_failure(event)

    def _refresh_inventory_overlap_lines(self):
        for event in self:
            lines = self.env['amazon.inventory.reconciliation'].sudo().search([
                ('instance_id', '=', event.instance_id.id),
                ('odoo_product_id', '=', event.product_id.id),
                ('status', 'in', ('mismatch', 'pending_review')),
                ('applied_picking_id', '=', False),
            ])
            lines._refresh_sale_event_overlap()

    def _process_one(self):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    'SELECT id FROM amazon_fba_sale_stock_event WHERE id = %s FOR UPDATE',
                    [self.id],
                )
                self.invalidate_recordset()
                # Defense in depth: never process a controlled-import hold, even if
                # one reaches the processor directly. Only Retry (which clears the
                # hold code and sets 'pending') may lead to processing.
                if self._is_controlled_import_hold():
                    return False
                if self.state == 'historical':
                    return False
                # Defense in depth (Phase 11C): never auto-process a pre-cutover /
                # missing-date order that reached 'pending' through any unintended path.
                # The ONLY authorized bypass is an explicit human release (Retry), which
                # sets cutover_manual_release. Otherwise re-hold without moving stock.
                cutover_reason = self._fba_cutover_reason(
                    self.instance_id, self.order_id.purchase_date, has_positive_cumulative=True,
                )
                if cutover_reason and not self.cutover_manual_release:
                    self.write({
                        'state': 'manual_review', 'next_run_at': False,
                        'last_error_code': cutover_reason,
                        'last_error_message': _(
                            "Blocked by the FBA Automatic Stock Cutover (%s). Not processed; "
                            "release explicitly with Retry if this stock movement is intended.",
                            cutover_reason,
                        ),
                        'last_activity_at': fields.Datetime.now(),
                    })
                    self._record_manual_review()
                    return False
                if self._is_before_fba_sale_stock_cutover():
                    if not self._is_cutover_v2_active(self.instance_id):
                        self._mark_historical()
                        return False
                if self.state == 'done' and self.amazon_cumulative_fulfilled_qty <= self.processed_fulfilled_qty:
                    return False
                self.write({
                    'state': 'processing',
                    'attempt_count': self.attempt_count + 1,
                    'started_at': self.started_at or fields.Datetime.now(),
                    'last_activity_at': fields.Datetime.now(),
                    'next_run_at': False,
                })
                return self._process_locked()
        except Exception as exc:
            message = str(exc)
            self.invalidate_recordset()
            insufficient = 'Insufficient FBA Sellable stock' in message
            attempts = self.attempt_count + 1
            terminal = attempts >= self.max_attempts
            if insufficient:
                values = {
                    'state': 'manual_review', 'next_run_at': False,
                    'last_error_code': 'INSUFFICIENT_FBA_SELLABLE_STOCK',
                    'last_error_message': message[:5000],
                    'last_activity_at': fields.Datetime.now(),
                    'attempt_count': attempts,
                }
            else:
                values = {
                    'state': 'failed' if terminal else 'pending',
                    'next_run_at': False if terminal else (
                        fields.Datetime.now() + timedelta(seconds=min(60 * (2 ** min(attempts, 8)), 3600))
                    ),
                    'last_error_code': 'LOCAL_STOCK_MOVE_FAILED',
                    'last_error_message': message[:5000],
                    'last_activity_at': fields.Datetime.now(),
                    'attempt_count': attempts,
                    'finished_at': fields.Datetime.now() if terminal else False,
                }
            self.write(values)
            self.env['amazon.operation.control'].sudo().record_source_failure(self)
            _logger.warning("FBA sale stock event %s failed: %s", self.id, message)
            return False

    @api.model
    def cron_process_fba_sale_stock_events(self, limit=50):
        processed = 0
        for _index in range(max(int(limit or 1), 1)):
            self.env.cr.execute("""
                SELECT id
                  FROM amazon_fba_sale_stock_event
                 WHERE state = 'pending'
                   AND (next_run_at IS NULL OR next_run_at <= %s)
                 ORDER BY COALESCE(next_run_at, create_date), id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            """, [fields.Datetime.now()])
            row = self.env.cr.fetchone()
            if not row:
                break
            self.browse(row[0]).sudo()._process_one()
            processed += 1
        return processed

    def action_retry(self):
        for event in self:
            if event.state not in ('manual_review', 'failed'):
                raise UserError(_("Only a failed or manual-review FBA sale event can be retried."))
            event.write({
                'state': 'pending', 'next_run_at': fields.Datetime.now(),
                'finished_at': False, 'last_error_code': False,
                'last_error_message': False,
                # Explicit human release: the sole authorized bypass of the cutover guard.
                'cutover_manual_release': True,
            })
        return True


class AmazonSaleOrderLineFbaStock(models.Model):
    _inherit = 'amazon.sale.order.line'

    amazon_cumulative_fulfilled_qty = fields.Float(
        string='Amazon Cumulative Shipped', readonly=True, copy=False,
    )
    odoo_processed_fulfilled_qty = fields.Float(
        string='Odoo Processed Shipped', readonly=True, copy=False,
    )
    fba_sale_stock_event_ids = fields.One2many(
        'amazon.fba.sale.stock.event', 'order_line_id',
        string='FBA Sale Stock Event', readonly=True,
    )


class StockPickingFbaSaleStock(models.Model):
    _inherit = 'stock.picking'

    amazon_fba_sale_stock_event_id = fields.Many2one(
        'amazon.fba.sale.stock.event', copy=False, readonly=True,
        ondelete='restrict', check_company=True, index=True,
    )
    amazon_fba_movement_type = fields.Selection(selection_add=[
        ('fba_sale', 'Amazon FBA Sale to Customer'),
        ('fba_sale_historical_reversal', 'Amazon FBA Historical Sale Reversal'),
    ], ondelete={
        'fba_sale': 'set null',
        'fba_sale_historical_reversal': 'set null',
    })
