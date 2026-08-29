import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare


_logger = logging.getLogger(__name__)


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
    last_error_code = fields.Char(readonly=True, copy=False, index=True)
    last_error_message = fields.Text(readonly=True, copy=False)
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

    @api.model
    def _advisory_lock(self, instance_id, product_id):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
            ['amazon_fba_sale_stock:%s' % int(instance_id), int(product_id)],
        )

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
            values.update({
                'instance_id': order.instance_id.id,
                'amazon_order_ref': order.amazon_order_ref,
                'amazon_order_item_id': order_line.amazon_order_item_id,
                'amazon_cumulative_fulfilled_qty': cumulative,
                'state': 'pending' if cumulative and product and product.is_storable else (
                    'manual_review' if cumulative else 'done'
                ),
                'next_run_at': fields.Datetime.now() if cumulative and product and product.is_storable else False,
                'finished_at': fields.Datetime.now() if not cumulative else False,
            })
            try:
                with self.env.cr.savepoint():
                    event = self.sudo().create(values)
            except IntegrityError:
                event = self.sudo().search(domain, limit=1)
                if not event:
                    raise
        self.env.cr.execute(
            'SELECT id FROM amazon_fba_sale_stock_event WHERE id = %s FOR UPDATE',
            [event.id],
        )
        event.invalidate_recordset()
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

    def _process_locked(self):
        self.ensure_one()
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
    ], ondelete={'fba_sale': 'set null'})
