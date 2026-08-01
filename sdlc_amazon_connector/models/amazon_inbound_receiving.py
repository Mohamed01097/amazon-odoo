import json
import logging
from datetime import timedelta

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)

RECEIVING_STATES = {'waiting_receiving', 'partially_received', 'received'}
AMAZON_TERMINAL_SHIPMENT_STATUSES = {'ABANDONED', 'CANCELLED', 'CLOSED', 'DELETED'}


class AmazonInboundShipmentReceiving(models.Model):
    _inherit = 'amazon.inbound.shipment'

    state = fields.Selection(selection_add=[
        ('partially_received', 'Partially Received'),
        ('received', 'Received'),
    ], ondelete={
        'partially_received': 'set default',
        'received': 'set default',
    })
    sent_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
    )
    received_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
    )
    remaining_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
    )
    damaged_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
        help=(
            "The current official inbound item operations do not expose damaged "
            "quantity. This remains zero unless Amazon adds a documented, "
            "shipment-scoped field in a future API version."
        ),
    )
    lost_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
        help=(
            "The current official inbound item operations do not expose lost "
            "quantity. Closed shortages are recorded as unresolved discrepancies, "
            "not silently classified as lost."
        ),
    )
    receiving_status = fields.Char(
        string='Amazon Receiving Status', copy=False, readonly=True, index=True,
    )
    receiving_sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    last_receiving_sync_at = fields.Datetime(copy=False, readonly=True)
    receiving_error_message = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    receiving_response = fields.Text(
        string='Sanitized Receiving Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    receiving_discrepancy_ids = fields.One2many(
        'amazon.fba.inventory.discrepancy', 'shipment_id',
        string='Receiving Discrepancies',
    )
    receiving_discrepancy_count = fields.Integer(
        compute='_compute_receiving_discrepancy_count',
    )

    @api.depends(
        'line_ids.quantity_shipped',
        'line_ids.quantity_received',
        'line_ids.quantity_damaged',
        'line_ids.quantity_lost',
    )
    def _compute_receiving_totals(self):
        for shipment in self:
            sent = sum(shipment.line_ids.mapped('quantity_shipped'))
            received = sum(shipment.line_ids.mapped('quantity_received'))
            shipment.sent_quantity = sent
            shipment.received_quantity = received
            shipment.remaining_quantity = max(sent - received, 0.0)
            shipment.damaged_quantity = sum(
                shipment.line_ids.mapped('quantity_damaged')
            )
            shipment.lost_quantity = sum(shipment.line_ids.mapped('quantity_lost'))

    @api.depends('receiving_discrepancy_ids.status')
    def _compute_receiving_discrepancy_count(self):
        for shipment in self:
            shipment.receiving_discrepancy_count = len(
                shipment.receiving_discrepancy_ids.filtered(
                    lambda discrepancy: discrepancy.status == 'open'
                )
            )

    def _validate_receiving_locations(self):
        self.ensure_one()
        transit = self.instance_id.fba_transit_location_id
        sellable = self.instance_id.fba_sellable_location_id
        unsellable = self.instance_id.fba_unsellable_location_id
        if not transit or not sellable or not unsellable:
            raise UserError(_(
                "Configure Amazon Transit, Sellable, and Unsellable locations "
                "on the Amazon instance before receiving."
            ))
        if not transit.active or transit.usage != 'transit':
            raise UserError(_("The configured Amazon Transit Location is invalid."))
        for label, location in ((_('Sellable'), sellable), (_('Unsellable'), unsellable)):
            if not location.active or location.usage != 'internal':
                raise UserError(_("The configured Amazon %s Location is invalid.", label))
        locations = transit | sellable | unsellable
        if len(locations) != 3:
            raise UserError(_("Amazon receiving locations must be distinct."))
        if any(location.company_id != self.company_id for location in locations):
            raise UserError(_("Amazon receiving locations must belong to the shipment company."))
        return transit, sellable, unsellable

    @staticmethod
    def _amazon_quantity(value, field_name):
        if isinstance(value, bool):
            raise ValidationError(_("Amazon returned an invalid %s.", field_name))
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Amazon returned an invalid %s.", field_name)) from exc
        if number < 0 or not number.is_integer():
            raise ValidationError(_("Amazon returned an invalid %s.", field_name))
        return number

    def _normalized_receiving_items(self, items_response):
        """Map the preserved v0 item response to this shipment's existing lines."""
        self.ensure_one()
        payload = items_response.get('payload') if isinstance(items_response, dict) else None
        raw_items = payload.get('ItemData') if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            raise ValidationError(_("Amazon did not return a valid inbound ItemData list."))
        aggregated = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError(_("Amazon returned an invalid inbound shipment item."))
            sku = str(raw_item.get('SellerSKU') or '').strip()
            fnsku = str(raw_item.get('FulfillmentNetworkSKU') or '').strip()
            if not sku:
                raise ValidationError(_("Amazon returned an inbound item without SellerSKU."))
            key = (sku, fnsku)
            bucket = aggregated.setdefault(key, {
                'sku': sku,
                'fnsku': fnsku,
                'quantity_shipped': 0.0,
                'quantity_received': 0.0,
            })
            bucket['quantity_shipped'] += self._amazon_quantity(
                raw_item.get('QuantityShipped'), 'QuantityShipped',
            )
            bucket['quantity_received'] += self._amazon_quantity(
                raw_item.get('QuantityReceived', 0), 'QuantityReceived',
            )

        normalized = []
        mapped_lines = self.env['amazon.inbound.shipment.line']
        for values in aggregated.values():
            candidates = self.line_ids.filtered(lambda line: line.sku == values['sku'])
            if values['fnsku']:
                exact = candidates.filtered(lambda line: line.fnsku == values['fnsku'])
                if exact:
                    candidates = exact
            if len(candidates) != 1:
                raise ValidationError(_(
                    "Amazon item %s could not be mapped to exactly one shipment line.",
                    values['sku'],
                ))
            line = candidates
            if line in mapped_lines:
                raise ValidationError(_(
                    "Amazon returned duplicate item groups for shipment line %s.",
                    line.sku,
                ))
            mapped_lines |= line
            normalized.append((line, values))
        if mapped_lines != self.line_ids:
            missing = ', '.join((self.line_ids - mapped_lines).mapped('sku'))
            raise ValidationError(_(
                "Amazon omitted shipment items from the receiving response: %s.", missing
            ))
        return normalized

    def _upsert_receiving_discrepancy(self, line, discrepancy_type, quantity,
                                      amazon_status, raw_response):
        self.ensure_one()
        Discrepancy = self.env['amazon.fba.inventory.discrepancy'].sudo()
        record = Discrepancy.search([
            ('shipment_id', '=', self.id),
            ('line_id', '=', line.id),
            ('discrepancy_type', '=', discrepancy_type),
        ], limit=1)
        if quantity <= 0:
            if record and record.status == 'open':
                record.write({'status': 'resolved', 'resolved_at': fields.Datetime.now()})
            return record
        vals = {
            'shipment_id': self.id,
            'line_id': line.id,
            'sku': line.sku,
            'discrepancy_type': discrepancy_type,
            'quantity': quantity,
            'amazon_status': amazon_status or False,
            'status': 'open',
            'resolved_at': False,
            'raw_response': raw_response,
        }
        if record:
            record.write(vals)
            return record
        return Discrepancy.create(vals)

    def _create_receiving_picking(self, line_deltas, destination, movement_type):
        self.ensure_one()
        if not line_deltas:
            return self.env['stock.picking']
        transit = self.instance_id.fba_transit_location_id
        picking_type = self._get_internal_picking_type(destination)
        move_commands = []
        for line, quantity in line_deltas:
            product = line.odoo_product_id or line.amazon_product_id.odoo_product_id
            if not product or not product.is_storable:
                raise UserError(_(
                    "Shipment line %s must map to an inventory-tracked Odoo product.",
                    line.sku,
                ))
            move_commands.append(Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': transit.id,
                'location_dest_id': destination.id,
                'company_id': self.company_id.id,
                'origin': self.name,
            }))
        picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
            'picking_type_id': picking_type.id,
            'location_id': transit.id,
            'location_dest_id': destination.id,
            'company_id': self.company_id.id,
            'origin': self.name,
            'amazon_instance_id': self.instance_id.id,
            'amazon_inbound_shipment_id': self.id,
            'amazon_fba_movement_type': movement_type,
            'move_type': 'one',
            'move_ids': move_commands,
        })
        picking.action_confirm()
        picking.action_assign()
        missing = picking.move_ids.filtered(
            lambda move: move.state != 'assigned'
            or move.product_uom.compare(move.quantity, move.product_uom_qty) < 0
        )
        if missing:
            details = ', '.join(
                "%s (%s/%s)" % (
                    move.product_id.display_name,
                    move.quantity,
                    move.product_uom_qty,
                ) for move in missing
            )
            raise UserError(_(
                "Insufficient Amazon Transit stock; standard reservation could not reserve: %s.",
                details,
            ))
        if any(not move.move_line_ids for move in picking.move_ids):
            raise UserError(_("Odoo did not create the required receiving stock move lines."))
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
        ).button_validate()
        if isinstance(result, dict) or picking.state != 'done':
            raise UserError(_(
                "Receiving transfer %s requires manual stock details and was not completed.",
                picking.name,
            ))
        return picking

    def _receiving_state(self, amazon_status, sent, received):
        self.ensure_one()
        status = str(amazon_status or '').strip().upper()
        if status in AMAZON_TERMINAL_SHIPMENT_STATUSES:
            return 'closed'
        if sent > 0 and received >= sent:
            return 'received'
        if received > 0:
            return 'partially_received'
        return 'waiting_receiving'

    def _apply_receiving_snapshot(self, status_response, items_response):
        """Atomically apply cumulative Amazon quantities as newly received deltas."""
        self.ensure_one()
        with self.env.cr.savepoint():
            self._lock_phase4_workflow()
            if self.state not in RECEIVING_STATES and self.state != 'closed':
                raise UserError(_(
                    "Amazon receiving can only synchronize after shipment confirmation."
                ))
            _transit, sellable, _unsellable = self._validate_receiving_locations()
            normalized = self._normalized_receiving_items(items_response)
            amazon_status = str(status_response.get('status') or '').strip().upper()
            raw_response = self._sanitized_json({
                'getShipment': status_response,
                'getShipmentItemsByShipmentId': items_response,
            })
            sellable_deltas = []
            line_values = []
            sent_total = 0.0
            received_total = 0.0
            for line, values in normalized:
                shipped = values['quantity_shipped']
                received = values['quantity_received']
                movable_total = min(received, shipped, float(line.planned_quantity))
                delta = max(movable_total - line.received_moved_quantity, 0.0)
                if delta:
                    sellable_deltas.append((line, delta))
                line_values.append((line, shipped, received, movable_total))
                sent_total += shipped
                received_total += received

            picking = self._create_receiving_picking(
                sellable_deltas, sellable, 'receiving_sellable',
            )
            for line, shipped, received, movable_total in line_values:
                previous_moved = line.received_moved_quantity
                line.sudo().write({
                    'quantity_shipped': shipped,
                    'quantity_received': received,
                    'received_moved_quantity': max(previous_moved, movable_total),
                    # These quantities are intentionally not inferred. Neither
                    # official inbound item operation exposes them today.
                    'quantity_damaged': 0.0,
                    'quantity_lost': 0.0,
                })
                self._upsert_receiving_discrepancy(
                    line,
                    'shipped_quantity_mismatch',
                    abs(shipped - line.planned_quantity),
                    amazon_status,
                    raw_response,
                )
                self._upsert_receiving_discrepancy(
                    line,
                    'received_overage',
                    max(received - min(shipped, float(line.planned_quantity)), 0.0),
                    amazon_status,
                    raw_response,
                )
                self._upsert_receiving_discrepancy(
                    line,
                    'received_quantity_decrease',
                    max(previous_moved - received, 0.0),
                    amazon_status,
                    raw_response,
                )
                closed_shortage = (
                    max(shipped - received, 0.0)
                    if amazon_status in AMAZON_TERMINAL_SHIPMENT_STATUSES else 0.0
                )
                self._upsert_receiving_discrepancy(
                    line,
                    'closed_shortage',
                    closed_shortage,
                    amazon_status,
                    raw_response,
                )

            next_state = self._receiving_state(
                amazon_status, sent_total, received_total,
            )
            self.sudo().write({
                'state': next_state,
                'receiving_status': amazon_status or False,
                'receiving_sync_status': 'success',
                'last_receiving_sync_at': fields.Datetime.now(),
                'receiving_error_message': False,
                'receiving_response': raw_response,
            })
            return {
                'state': next_state,
                'sentQuantity': sent_total,
                'receivedQuantity': received_total,
                'remainingQuantity': max(sent_total - received_total, 0.0),
                'receivingPickingId': picking.id or False,
                'amazonStatus': amazon_status,
            }

    def _sync_amazon_receiving(self):
        self.ensure_one()
        if self.state not in RECEIVING_STATES:
            raise UserError(_(
                "Amazon receiving can only synchronize shipments waiting for receiving."
            ))
        status_response = self._refresh_shipment_status()
        if not self.shipment_confirmation_id:
            raise UserError(_(
                "Amazon did not return a shipmentConfirmationID; receiving quantities "
                "cannot be requested safely."
            ))
        access_token = self.instance_id._get_access_token_or_raise()
        items_response = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_shipment_items_v0,
            self.instance_id,
            access_token,
            self.shipment_confirmation_id,
            error_msg=_("Failed to retrieve Amazon inbound received quantities"),
        )
        return self._apply_receiving_snapshot(status_response, items_response)

    def _enqueue_receiving_job(self, retry_failed=False):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('inbound_shipment_id', '=', self.id),
            ('operation_type', '=', 'sync_receiving'),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        if retry_failed:
            failed = Job.search([
                ('inbound_shipment_id', '=', self.id),
                ('operation_type', '=', 'sync_receiving'),
                ('state', '=', 'failed'),
            ], order='id desc', limit=1)
            if failed:
                failed.write({
                    'state': 'pending',
                    'retry_count': 0,
                    'next_run_at': fields.Datetime.now(),
                    'last_error': False,
                    'started_at': False,
                    'finished_at': False,
                    'response_data': False,
                    'amazon_request_id': False,
                })
                return failed, True
        return Job.create({
            'inbound_shipment_id': self.id,
            'operation_type': 'sync_receiving',
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }), True

    def action_sync_receiving(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase4_workflow()
        if self.state not in RECEIVING_STATES:
            raise UserError(_(
                "Receiving synchronization is available only after shipment confirmation."
            ))
        if not self.shipment_confirmation_id:
            raise UserError(_(
                "Refresh Shipment Status to retrieve the shipmentConfirmationID first."
            ))
        _job, created = self._enqueue_receiving_job(retry_failed=True)
        self.sudo().write({'receiving_sync_status': 'pending'})
        return self.instance_id._notify(
            _("Amazon Receiving"),
            _("Receiving synchronization was queued.")
            if created else _("Receiving synchronization is already queued."),
            'success' if created else 'warning',
        )

    @api.model
    def cron_enqueue_receiving_sync(self):
        cutoff = fields.Datetime.now() - timedelta(hours=1)
        shipments = self.sudo().search([
            ('state', 'in', tuple(RECEIVING_STATES)),
            ('shipment_confirmation_id', '!=', False),
            ('instance_id.active', '=', True),
        ], order='last_receiving_sync_at, id', limit=50)
        queued = 0
        for shipment in shipments:
            failed = shipment.operation_job_ids.filtered(
                lambda job: job.operation_type == 'sync_receiving'
                and job.state == 'failed'
            ).sorted('id', reverse=True)[:1]
            retry_failed = bool(
                failed and failed.finished_at and failed.finished_at <= cutoff
            )
            if failed and not retry_failed:
                continue
            _job, created = shipment._enqueue_receiving_job(
                retry_failed=retry_failed,
            )
            if created:
                shipment.write({'receiving_sync_status': 'pending'})
                queued += 1
        return queued


class AmazonInboundShipmentLineReceiving(models.Model):
    _inherit = 'amazon.inbound.shipment.line'

    received_moved_quantity = fields.Float(
        string='Received Qty Moved', default=0.0, copy=False, readonly=True,
        help="Cumulative received quantity already moved from Transit to Sellable.",
    )
    quantity_damaged = fields.Float('Qty Damaged', default=0.0, copy=False, readonly=True)
    damaged_moved_quantity = fields.Float(
        string='Damaged Qty Moved', default=0.0, copy=False, readonly=True,
    )
    quantity_lost = fields.Float('Qty Lost', default=0.0, copy=False, readonly=True)

    @api.constrains(
        'received_moved_quantity', 'quantity_damaged',
        'damaged_moved_quantity', 'quantity_lost',
    )
    def _check_receiving_quantities(self):
        for line in self:
            if any(value < 0 for value in (
                line.received_moved_quantity,
                line.quantity_damaged,
                line.damaged_moved_quantity,
                line.quantity_lost,
            )):
                raise ValidationError(_("Receiving quantities cannot be negative."))


class AmazonFbaInventoryDiscrepancy(models.Model):
    _name = 'amazon.fba.inventory.discrepancy'
    _description = 'Amazon FBA Inventory Discrepancy'
    _order = 'create_date desc, id desc'
    _check_company_auto = True

    shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade',
        index=True, check_company=True,
    )
    line_id = fields.Many2one(
        'amazon.inbound.shipment.line', required=True, ondelete='cascade', index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='shipment_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='shipment_id.company_id',
        store=True, readonly=True, index=True,
    )
    sku = fields.Char(required=True, readonly=True, index=True)
    discrepancy_type = fields.Selection([
        ('closed_shortage', 'Unresolved Closed Shortage'),
        ('shipped_quantity_mismatch', 'Shipped Quantity Mismatch'),
        ('received_overage', 'Received Quantity Overage'),
        ('received_quantity_decrease', 'Received Quantity Decreased'),
    ], required=True, readonly=True, index=True)
    quantity = fields.Float(required=True, readonly=True)
    amazon_status = fields.Char(readonly=True, index=True)
    amazon_reported_lost = fields.Boolean(
        default=False, readonly=True,
        help=(
            "False for current Phase 5 records because the preserved official "
            "inbound item operation does not expose lost quantity."
        ),
    )
    status = fields.Selection([
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('ignored', 'Ignored'),
    ], required=True, default='open', index=True)
    resolved_at = fields.Datetime(readonly=True)
    notes = fields.Text()
    raw_response = fields.Text(
        readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_shipment_line_type = models.Constraint(
        'UNIQUE (shipment_id, line_id, discrepancy_type)',
        'Only one discrepancy of each type can exist per shipment line.',
    )
    _positive_quantity = models.Constraint(
        'CHECK (quantity > 0)',
        'Inventory discrepancy quantity must be positive.',
    )


class StockPickingAmazonInboundReceiving(models.Model):
    _inherit = 'stock.picking'

    amazon_fba_movement_type = fields.Selection([
        ('outbound_to_transit', 'Source to Amazon Transit'),
        ('receiving_sellable', 'Amazon Transit to Sellable'),
        ('receiving_unsellable', 'Amazon Transit to Unsellable'),
    ], copy=False, readonly=True, index=True)


class AmazonInboundOperationJobReceiving(models.Model):
    _inherit = 'amazon.inbound.operation.job'

    operation_type = fields.Selection(selection_add=[
        ('sync_receiving', 'Synchronize Amazon Receiving'),
    ], ondelete={'sync_receiving': 'cascade'})

    def _process_operation(self):
        self.ensure_one()
        if self.operation_type != 'sync_receiving':
            return super()._process_operation()
        if self.state in ('done', 'failed'):
            return False
        now = fields.Datetime.now()
        vals = {'state': 'in_progress', 'next_run_at': False}
        if not self.started_at:
            vals['started_at'] = now
        self.write(vals)
        shipment = self.inbound_shipment_id.sudo()
        try:
            result = shipment._sync_amazon_receiving()
            request_ids = []
            if shipment.receiving_response:
                try:
                    response = json.loads(shipment.receiving_response)
                    item_response = response.get('getShipmentItemsByShipmentId') or {}
                    request_ids = item_response.get('_amazon_request_ids') or []
                except (TypeError, ValueError):
                    request_ids = []
            self.write({
                'response_data': shipment._sanitized_json(result),
                'amazon_request_id': request_ids[-1] if request_ids else False,
            })
            self._mark_done()
            return True
        except Exception as exc:
            message = str(exc)
            _logger.warning(
                "Amazon Phase 5 receiving job %s failed: %s", self.id, message,
            )
            self._schedule_retry(error_message=message)
            shipment.write({
                'receiving_sync_status': (
                    'failed' if self.state == 'failed' else 'pending'
                ),
                'receiving_error_message': message,
            })
            return False
