import json
import logging
from datetime import timedelta

import requests

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .amazon_api import AmazonAPI


_logger = logging.getLogger(__name__)

AMAZON_TERMINAL_SHIPMENT_STATUSES = {'ABANDONED', 'CANCELLED', 'CLOSED', 'DELETED'}
PHYSICAL_RECEIVING_TERMINAL_STATES = {'received', 'closed'}


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
        help="Not inferred by receiving. Shipment-scoped disposition is not exposed by the inbound item API.",
    )
    lost_quantity = fields.Float(
        compute='_compute_receiving_totals', store=True, readonly=True,
        help="Closed shortages remain discrepancies and are not silently classified as lost.",
    )
    receiving_status = fields.Char(
        string='Amazon Receiving Status', compute='_compute_receiving_totals',
        store=True, readonly=True, index=True,
    )
    receiving_sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], compute='_compute_receiving_totals', store=True, readonly=True, index=True)
    last_receiving_sync_at = fields.Datetime(
        compute='_compute_receiving_totals', store=True, readonly=True,
    )
    receiving_error_message = fields.Text(
        compute='_compute_receiving_totals', store=True, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    receiving_response = fields.Text(
        string='Legacy Receiving Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
        help="Retained for compatibility. Current responses are stored per physical shipment and job.",
    )
    receiving_discrepancy_ids = fields.One2many(
        'amazon.fba.inventory.discrepancy', 'shipment_id',
        string='Receiving Discrepancies',
    )
    receiving_discrepancy_count = fields.Integer(
        compute='_compute_receiving_discrepancy_count',
    )

    @api.depends(
        'physical_shipment_ids.status',
        'physical_shipment_ids.receiving_sync_status',
        'physical_shipment_ids.last_receiving_sync_at',
        'physical_shipment_ids.receiving_error_message',
        'physical_shipment_ids.line_ids.dispatched_quantity',
        'physical_shipment_ids.line_ids.amazon_received_quantity',
        'physical_shipment_ids.line_ids.processed_received_quantity',
    )
    def _compute_receiving_totals(self):
        for shipment in self:
            physical = shipment.physical_shipment_ids
            lines = physical.mapped('line_ids')
            shipment.sent_quantity = sum(lines.mapped('dispatched_quantity'))
            shipment.received_quantity = sum(lines.mapped('amazon_received_quantity'))
            shipment.remaining_quantity = sum(lines.mapped('remaining_in_transit_quantity'))
            shipment.damaged_quantity = 0.0
            shipment.lost_quantity = 0.0
            raw_statuses = sorted(set(filter(None, physical.mapped('status'))))
            shipment.receiving_status = (
                raw_statuses[0] if len(raw_statuses) == 1
                else ', '.join(raw_statuses) if raw_statuses else False
            )
            sync_statuses = set(filter(None, physical.mapped('receiving_sync_status')))
            if 'failed' in sync_statuses:
                sync_status = 'failed'
            elif 'in_progress' in sync_statuses:
                sync_status = 'in_progress'
            elif 'pending' in sync_statuses:
                sync_status = 'pending'
            elif sync_statuses:
                sync_status = 'success'
            else:
                sync_status = False
            shipment.receiving_sync_status = sync_status
            sync_dates = list(filter(None, physical.mapped('last_receiving_sync_at')))
            shipment.last_receiving_sync_at = max(sync_dates) if sync_dates else False
            errors = list(dict.fromkeys(filter(None, physical.mapped('receiving_error_message'))))
            shipment.receiving_error_message = '\n'.join(errors) or False

    @api.depends('receiving_discrepancy_ids.status')
    def _compute_receiving_discrepancy_count(self):
        for shipment in self:
            shipment.receiving_discrepancy_count = len(
                shipment.receiving_discrepancy_ids.filtered(
                    lambda discrepancy: discrepancy.status == 'open'
                )
            )

    def _sync_legacy_receiving_lines(self):
        """Keep pre-physical plan totals readable without using them as stock truth."""
        for shipment in self:
            by_msku = {}
            for line in shipment.physical_shipment_ids.mapped('line_ids'):
                totals = by_msku.setdefault(line.msku, [0.0, 0.0, 0.0])
                totals[0] += line.dispatched_quantity
                totals[1] += line.amazon_received_quantity
                totals[2] += line.processed_received_quantity
            for line in shipment.line_ids:
                shipped, received, processed = by_msku.get(line.sku, (0.0, 0.0, 0.0))
                line.sudo().write({
                    'quantity_shipped': shipped,
                    'quantity_received': received,
                    'received_moved_quantity': processed,
                    'quantity_damaged': 0.0,
                    'quantity_lost': 0.0,
                })

    def _update_receiving_state_from_physical(self):
        for shipment in self:
            physical = shipment.physical_shipment_ids
            if not physical:
                continue
            raw_statuses = {
                str(value or '').strip().upper() for value in physical.mapped('status')
            }
            lines = physical.mapped('line_ids')
            open_discrepancies = shipment.receiving_discrepancy_ids.filtered(
                lambda discrepancy: discrepancy.status == 'open'
            )
            all_processed = bool(lines) and all(
                line.dispatched_quantity > 0
                and line.processed_received_quantity >= line.dispatched_quantity
                for line in lines
            )
            if raw_statuses and raw_statuses <= AMAZON_TERMINAL_SHIPMENT_STATUSES:
                next_state = 'closed'
            elif all_processed and not open_discrepancies:
                next_state = 'received'
            elif any(line.processed_received_quantity > 0 for line in lines):
                next_state = 'partially_received'
            else:
                next_state = 'waiting_receiving'
            if shipment.state not in ('cancelled', 'failed') and shipment.state != next_state:
                shipment.sudo().write({'state': next_state})

    def action_sync_receiving(self):
        """Compatibility plan action: enqueue one independent job per physical shipment."""
        self.ensure_one()
        self._check_inbound_manager_access()
        eligible = self.physical_shipment_ids.filtered(
            lambda physical: physical.dispatch_state == 'dispatched'
            and physical.receiving_state not in PHYSICAL_RECEIVING_TERMINAL_STATES
            and not (
                str(physical.status or '').upper() in AMAZON_TERMINAL_SHIPMENT_STATUSES
                and physical.receiving_sync_status == 'success'
            )
        )
        if not eligible:
            raise UserError(_(
                "No non-terminal dispatched Amazon physical shipment is available for receiving sync."
            ))
        created = 0
        for physical in eligible:
            _job, was_created = physical._enqueue_receiving_job()
            created += int(was_created)
        return self.instance_id._notify(
            _("Amazon Receiving"),
            _("Queued %s physical shipment receiving synchronization(s).", created)
            if created else _("Receiving synchronization is already queued."),
            'success' if created else 'warning',
        )


class AmazonFbaPhysicalShipmentReceiving(models.Model):
    _inherit = 'amazon.fba.physical.shipment'

    receiving_state = fields.Selection([
        ('not_started', 'Not Started'),
        ('in_transit', 'In Transit'),
        ('checked_in', 'Checked In'),
        ('receiving', 'Receiving'),
        ('partially_received', 'Partially Received'),
        ('received', 'Received'),
        ('closed', 'Closed'),
        ('discrepancy', 'Discrepancy'),
    ], default='not_started', required=True, copy=False, readonly=True, index=True)
    receiving_sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    receiving_error_code = fields.Char(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    receiving_error_message = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    last_receiving_sync_at = fields.Datetime(copy=False, readonly=True, index=True)
    receiving_response = fields.Text(
        string='Sanitized Receiving Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    receiving_dispatched_quantity = fields.Float(
        compute='_compute_receiving_totals', readonly=True,
    )
    amazon_received_quantity = fields.Float(
        compute='_compute_receiving_totals', readonly=True,
    )
    processed_received_quantity = fields.Float(
        compute='_compute_receiving_totals', readonly=True,
    )
    remaining_in_transit_quantity = fields.Float(
        compute='_compute_receiving_totals', readonly=True,
    )
    receiving_difference = fields.Float(
        compute='_compute_receiving_totals', readonly=True,
        help="Amazon reported received quantity minus Odoo dispatched quantity.",
    )
    received_location_id = fields.Many2one(
        related='instance_id.fba_received_location_id', string='Received / Staging Location',
        readonly=True,
    )
    receiving_picking_ids = fields.One2many(
        'stock.picking', 'amazon_fba_physical_shipment_id',
        string='Receiving Pickings', domain=[('amazon_fba_movement_type', '=', 'receiving_staging')],
    )
    receiving_picking_count = fields.Integer(compute='_compute_receiving_picking_count')
    receiving_discrepancy_ids = fields.One2many(
        'amazon.fba.inventory.discrepancy', 'physical_shipment_id',
        string='Receiving Discrepancies',
    )
    receiving_discrepancy_count = fields.Integer(
        compute='_compute_receiving_discrepancy_count',
    )

    @api.depends(
        'line_ids.dispatched_quantity',
        'line_ids.amazon_received_quantity',
        'line_ids.processed_received_quantity',
    )
    def _compute_receiving_totals(self):
        for physical in self:
            lines = physical.line_ids
            dispatched = sum(lines.mapped('dispatched_quantity'))
            reported = sum(lines.mapped('amazon_received_quantity'))
            processed = sum(lines.mapped('processed_received_quantity'))
            physical.receiving_dispatched_quantity = dispatched
            physical.amazon_received_quantity = reported
            physical.processed_received_quantity = processed
            physical.remaining_in_transit_quantity = max(dispatched - processed, 0.0)
            physical.receiving_difference = reported - dispatched

    @api.depends('receiving_picking_ids.state')
    def _compute_receiving_picking_count(self):
        for physical in self:
            physical.receiving_picking_count = len(physical.receiving_picking_ids)

    @api.depends('receiving_discrepancy_ids.status')
    def _compute_receiving_discrepancy_count(self):
        for physical in self:
            physical.receiving_discrepancy_count = len(
                physical.receiving_discrepancy_ids.filtered(
                    lambda discrepancy: discrepancy.status == 'open'
                )
            )

    def _lock_receiving(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM amazon_fba_physical_shipment WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

    def _validate_receiving_preconditions(self):
        self.ensure_one()
        inbound = self.inbound_shipment_id
        if not inbound.inbound_plan_id:
            raise UserError(_("The Amazon inbound plan ID is missing."))
        if inbound.create_operation_status != 'success':
            raise UserError(_("The inbound plan must be created successfully before receiving."))
        if inbound.packing_confirmation_status != 'success':
            raise UserError(_("Packing must be confirmed successfully before receiving."))
        if inbound.placement_confirmation_status != 'success':
            raise UserError(_("Placement must be confirmed successfully before receiving."))
        if not self.placement_option_id.selected or self.placement_option_id.status != 'ACCEPTED':
            raise UserError(_("The physical shipment must belong to the accepted placement option."))
        if not (self.amazon_shipment_id or '').strip():
            raise UserError(_("The Amazon physical shipment ID is missing."))
        if not (self.shipment_confirmation_id or '').strip():
            raise UserError(_("The Amazon shipment confirmation ID is missing."))
        if self.dispatch_state != 'dispatched' or not self.picking_id or self.picking_id.state != 'done':
            raise UserError(_(
                "Amazon receiving can start only after this physical shipment's dispatch picking is done."
            ))
        if not self.line_ids:
            raise UserError(_("The physical shipment has no final placement items."))
        transit = self.instance_id.fba_transit_location_id
        received = self.instance_id.fba_received_location_id
        if not transit or not received:
            raise UserError(_(
                "Configure Amazon FBA Transit and Received / Staging locations before receiving."
            ))
        if not transit.active or transit.usage != 'transit':
            raise UserError(_("The configured Amazon Transit location is invalid."))
        if not received.active or received.usage != 'internal':
            raise UserError(_("The configured Amazon Received / Staging location is invalid."))
        if transit == received:
            raise UserError(_("Amazon Transit and Received / Staging locations must be distinct."))
        if transit.company_id != self.company_id or received.company_id != self.company_id:
            raise UserError(_("Amazon receiving locations must belong to the shipment company."))
        return transit, received

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
        """Return safely mapped and unmapped cumulative v0 item snapshots."""
        self.ensure_one()
        payload = items_response.get('payload') if isinstance(items_response, dict) else None
        raw_items = payload.get('ItemData') if isinstance(payload, dict) else None
        if not isinstance(raw_items, list):
            raise ValidationError(_("Amazon did not return a valid inbound ItemData list."))
        grouped = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError(_("Amazon returned an invalid inbound shipment item."))
            confirmation_id = str(raw_item.get('ShipmentId') or '').strip()
            if confirmation_id and confirmation_id != self.shipment_confirmation_id:
                raise ValidationError(_("Amazon returned an item for a different physical shipment."))
            sku = str(raw_item.get('SellerSKU') or '').strip()
            fnsku = str(raw_item.get('FulfillmentNetworkSKU') or '').strip()
            if not sku:
                raise ValidationError(_("Amazon returned an inbound item without SellerSKU."))
            bucket = grouped.setdefault(sku, {
                'sku': sku,
                'fnskus': set(),
                'quantity_shipped': 0.0,
                'quantity_received': 0.0,
            })
            if fnsku:
                bucket['fnskus'].add(fnsku)
            bucket['quantity_shipped'] += self._amazon_quantity(
                raw_item.get('QuantityShipped'), 'QuantityShipped',
            )
            bucket['quantity_received'] += self._amazon_quantity(
                raw_item.get('QuantityReceived', 0), 'QuantityReceived',
            )

        mapped = []
        unmapped = []
        mapped_lines = self.env['amazon.fba.physical.shipment.line']
        for values in grouped.values():
            fnskus = values.pop('fnskus')
            values['fnsku'] = next(iter(fnskus)) if len(fnskus) == 1 else False
            candidates = self.line_ids.filtered(lambda line: line.msku == values['sku'])
            if values['fnsku']:
                exact = candidates.filtered(lambda line: not line.fnsku or line.fnsku == values['fnsku'])
                candidates = exact
            product = candidates.amazon_product_id.odoo_product_id if len(candidates) == 1 else False
            if len(fnskus) > 1 or len(candidates) != 1 or not product or not product.is_storable:
                unmapped.append(values)
                continue
            line = candidates
            if line in mapped_lines:
                unmapped.append(values)
                continue
            mapped_lines |= line
            mapped.append((line, values))
        return mapped, unmapped, self.line_ids - mapped_lines

    def _upsert_receiving_discrepancy(self, physical_line, sku, discrepancy_type,
                                      quantity, amazon_quantity, odoo_quantity,
                                      amazon_status, raw_response):
        self.ensure_one()
        if quantity <= 0:
            return self.env['amazon.fba.inventory.discrepancy']
        Discrepancy = self.env['amazon.fba.inventory.discrepancy'].sudo()
        record = Discrepancy.search([
            ('physical_shipment_id', '=', self.id),
            ('physical_line_id', '=', physical_line.id if physical_line else False),
            ('sku', '=', sku),
            ('discrepancy_type', '=', discrepancy_type),
        ], limit=1)
        vals = {
            'shipment_id': self.inbound_shipment_id.id,
            'physical_shipment_id': self.id,
            'physical_line_id': physical_line.id if physical_line else False,
            'sku': sku,
            'discrepancy_type': discrepancy_type,
            'quantity': quantity,
            'amazon_quantity': amazon_quantity,
            'odoo_quantity': odoo_quantity,
            'amazon_status': amazon_status or False,
            'status': 'open',
            'resolved_at': False,
            'raw_response': raw_response,
        }
        if record:
            record.write(vals)
            return record
        return Discrepancy.create(vals)

    def _create_receiving_picking(self, line_deltas, destination, job=False):
        self.ensure_one()
        if not line_deltas:
            return self.env['stock.picking']
        transit = self.instance_id.fba_transit_location_id
        picking_type = self.inbound_shipment_id._get_internal_picking_type(destination)
        move_commands = []
        for line, quantity, before, after in line_deltas:
            product = line.amazon_product_id.odoo_product_id
            if not product or not product.is_storable:
                raise UserError(_(
                    "Physical shipment item %s is not mapped to a storable Odoo product.", line.msku,
                ))
            move_commands.append(Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': transit.id,
                'location_dest_id': destination.id,
                'company_id': self.company_id.id,
                'origin': "%s / %s" % (
                    self.inbound_shipment_id.name, self.shipment_confirmation_id,
                ),
                'amazon_fba_physical_shipment_line_id': line.id,
                'amazon_received_before': before,
                'amazon_received_after': after,
                'amazon_receiving_delta': quantity,
            }))
        picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
            'picking_type_id': picking_type.id,
            'location_id': transit.id,
            'location_dest_id': destination.id,
            'company_id': self.company_id.id,
            'origin': "%s / %s" % (
                self.inbound_shipment_id.name, self.shipment_confirmation_id,
            ),
            'amazon_instance_id': self.instance_id.id,
            'amazon_inbound_shipment_id': self.inbound_shipment_id.id,
            'amazon_fba_physical_shipment_id': self.id,
            'amazon_inbound_operation_job_id': job.id if job else False,
            'amazon_fba_movement_type': 'receiving_staging',
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
                    move.product_id.display_name, move.quantity, move.product_uom_qty,
                ) for move in missing
            )
            raise UserError(_(
                "Insufficient Amazon Transit stock for receiving delta: %s.", details,
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

    def _local_receiving_state(self, amazon_status):
        self.ensure_one()
        if self.receiving_discrepancy_ids.filtered(lambda item: item.status == 'open'):
            return 'discrepancy'
        dispatched = self.receiving_dispatched_quantity
        processed = self.processed_received_quantity
        if amazon_status in AMAZON_TERMINAL_SHIPMENT_STATUSES:
            return 'closed'
        if dispatched > 0 and processed >= dispatched:
            return 'received'
        if processed > 0:
            return 'partially_received'
        return {
            'SHIPPED': 'in_transit',
            'IN_TRANSIT': 'in_transit',
            'DELIVERED': 'in_transit',
            'CHECKED_IN': 'checked_in',
            'RECEIVING': 'receiving',
        }.get(amazon_status, 'not_started')

    def _apply_receiving_snapshot(self, status_response, items_response, job=False):
        """Atomically apply only positive, validated cumulative receipt deltas."""
        self.ensure_one()
        with self.env.cr.savepoint():
            self._lock_receiving()
            _transit, received_location = self._validate_receiving_preconditions()
            if not isinstance(status_response, dict):
                raise ValidationError(_("Amazon returned an invalid getShipment response."))
            returned_shipment_id = str(status_response.get('shipmentId') or '').strip()
            if returned_shipment_id and returned_shipment_id != self.amazon_shipment_id:
                raise ValidationError(_("Amazon returned a different physical shipment."))
            amazon_status = str(status_response.get('status') or self.status or '').strip().upper()
            raw_response = self.inbound_shipment_id._sanitized_json({
                'getShipment': status_response,
                'getShipmentItemsByShipmentId': items_response,
            })
            mapped, unmapped, missing_lines = self._normalized_receiving_items(items_response)
            line_updates = []
            line_deltas = []

            for values in unmapped:
                quantity = max(values['quantity_received'], values['quantity_shipped'])
                self._upsert_receiving_discrepancy(
                    False, values['sku'], 'unmapped_amazon_sku', quantity,
                    values['quantity_received'], 0.0, amazon_status, raw_response,
                )
            for line in missing_lines:
                self._upsert_receiving_discrepancy(
                    line, line.msku, 'missing_amazon_item', float(line.quantity),
                    0.0, float(line.quantity), amazon_status, raw_response,
                )

            for line, values in mapped:
                dispatched = float(line.quantity)
                amazon_shipped = values['quantity_shipped']
                amazon_received = values['quantity_received']
                previous_reported = line.amazon_received_quantity
                previous_processed = line.processed_received_quantity
                overage = max(amazon_received - dispatched, 0.0)
                decrease = max(previous_reported - amazon_received, 0.0)
                shipped_mismatch = abs(amazon_shipped - dispatched)
                closed_shortage = (
                    max(dispatched - amazon_received, 0.0)
                    if amazon_status in AMAZON_TERMINAL_SHIPMENT_STATUSES else 0.0
                )
                self._upsert_receiving_discrepancy(
                    line, line.msku, 'received_overage', overage,
                    amazon_received, dispatched, amazon_status, raw_response,
                )
                self._upsert_receiving_discrepancy(
                    line, line.msku, 'received_quantity_decrease', decrease,
                    amazon_received, previous_reported, amazon_status, raw_response,
                )
                self._upsert_receiving_discrepancy(
                    line, line.msku, 'shipped_quantity_mismatch', shipped_mismatch,
                    amazon_shipped, dispatched, amazon_status, raw_response,
                )
                self._upsert_receiving_discrepancy(
                    line, line.msku, 'closed_shortage', closed_shortage,
                    amazon_received, dispatched, amazon_status, raw_response,
                )

                delta = 0.0
                if not overage and not decrease and amazon_received >= previous_processed:
                    delta = amazon_received - previous_processed
                if delta > 0:
                    line_deltas.append(
                        (line, delta, previous_processed, previous_processed + delta)
                    )
                line_updates.append((line, {
                    'dispatched_quantity': dispatched,
                    'amazon_shipped_quantity': amazon_shipped,
                    'amazon_received_quantity': amazon_received,
                    'processed_received_quantity': previous_processed + delta,
                    'last_receiving_sync_at': fields.Datetime.now(),
                    'receiving_raw_response': self.inbound_shipment_id._sanitized_json(values),
                }))

            picking = self._create_receiving_picking(
                line_deltas, received_location, job=job,
            )
            for line, values in line_updates:
                line.sudo().write(values)
            self.sudo().write({
                'status': amazon_status or self.status,
                'receiving_sync_status': 'success',
                'receiving_error_code': False,
                'receiving_error_message': False,
                'last_receiving_sync_at': fields.Datetime.now(),
                'receiving_response': raw_response,
            })
            self.sudo().write({'receiving_state': self._local_receiving_state(amazon_status)})
            self.inbound_shipment_id._sync_legacy_receiving_lines()
            self.inbound_shipment_id._update_receiving_state_from_physical()
            delta_total = sum(value[1] for value in line_deltas)
            return {
                'shipmentId': self.amazon_shipment_id,
                'shipmentConfirmationId': self.shipment_confirmation_id,
                'amazonStatus': amazon_status,
                'dispatchedQuantity': self.receiving_dispatched_quantity,
                'amazonReceivedQuantity': self.amazon_received_quantity,
                'processedReceivedQuantity': self.processed_received_quantity,
                'deltaReceived': delta_total,
                'remainingInTransitQuantity': self.remaining_in_transit_quantity,
                'receivingPickingId': picking.id or False,
                'discrepancyCount': self.receiving_discrepancy_count,
            }

    def _sync_amazon_receiving(self, job=False):
        self.ensure_one()
        self._validate_receiving_preconditions()
        access_token = self.instance_id._get_access_token_or_raise()
        api = AmazonAPI()
        status_response = api.get_shipment(
            self.instance_id, access_token,
            self.inbound_shipment_id.inbound_plan_id, self.amazon_shipment_id,
        )
        items_response = api.get_inbound_shipment_items_v0(
            self.instance_id, access_token, self.shipment_confirmation_id,
        )
        return self._apply_receiving_snapshot(
            status_response, items_response, job=job,
        )

    def _enqueue_receiving_job(self):
        self.ensure_one()
        self._lock_receiving()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('physical_shipment_id', '=', self.id),
            ('operation_type', '=', 'sync_receiving'),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        job = Job.create({
            'inbound_shipment_id': self.inbound_shipment_id.id,
            'physical_shipment_id': self.id,
            'operation_type': 'sync_receiving',
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        })
        self.sudo().write({
            'receiving_sync_status': 'pending',
            'receiving_error_code': False,
            'receiving_error_message': False,
        })
        return job, True

    def action_sync_receiving(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._validate_receiving_preconditions()
        _job, created = self._enqueue_receiving_job()
        return self.instance_id._notify(
            _("Amazon Receiving"),
            _("Physical shipment receiving synchronization was queued.")
            if created else _("Receiving synchronization is already queued."),
            'success' if created else 'warning',
        )

    def action_open_receiving_pickings(self):
        self.ensure_one()
        pickings = self.receiving_picking_ids
        if not pickings:
            raise UserError(_("No receiving picking is linked to this physical shipment."))
        action = {
            'type': 'ir.actions.act_window',
            'name': _("Amazon FBA Receiving Pickings"),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
            'context': {'create': False},
        }
        if len(pickings) == 1:
            action.update(view_mode='form', res_id=pickings.id)
        return action

    @api.model
    def cron_enqueue_receiving_sync(self):
        domain = [
            ('dispatch_state', '=', 'dispatched'),
            ('shipment_confirmation_id', '!=', False),
            ('instance_id.active', '=', True),
            ('receiving_state', 'not in', tuple(PHYSICAL_RECEIVING_TERMINAL_STATES)),
            '|',
            ('receiving_sync_status', '!=', 'success'),
            ('status', 'not in', tuple(AMAZON_TERMINAL_SHIPMENT_STATUSES)),
        ]
        physical_shipments = self.sudo().search(
            domain, order='last_receiving_sync_at, id', limit=50,
        )
        queued = 0
        for physical in physical_shipments:
            _job, created = physical._enqueue_receiving_job()
            queued += int(created)
        return queued


class AmazonFbaPhysicalShipmentLineReceiving(models.Model):
    _inherit = 'amazon.fba.physical.shipment.line'

    dispatched_quantity = fields.Float(
        string='Dispatched Qty', default=0.0, copy=False, readonly=True,
        help="Odoo quantity dispatched for this physical shipment item.",
    )
    amazon_shipped_quantity = fields.Float(
        string='Amazon Shipped Qty', default=0.0, copy=False, readonly=True,
    )
    amazon_received_quantity = fields.Float(
        string='Amazon Received Qty', default=0.0, copy=False, readonly=True,
        help="Latest cumulative QuantityReceived reported by Amazon for this physical shipment item.",
    )
    processed_received_quantity = fields.Float(
        string='Processed Received Qty', default=0.0, copy=False, readonly=True,
        help="Cumulative quantity already moved from Amazon Transit to Received / Staging.",
    )
    remaining_in_transit_quantity = fields.Float(
        string='Remaining Transit Qty', compute='_compute_receiving_quantities', readonly=True,
    )
    receiving_difference = fields.Float(
        string='Difference', compute='_compute_receiving_quantities', readonly=True,
        help="Amazon reported received quantity minus Odoo dispatched quantity.",
    )
    last_receiving_sync_at = fields.Datetime(copy=False, readonly=True)
    receiving_raw_response = fields.Text(
        string='Sanitized Receiving Item Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    @api.depends(
        'dispatched_quantity', 'amazon_received_quantity', 'processed_received_quantity',
    )
    def _compute_receiving_quantities(self):
        for line in self:
            line.remaining_in_transit_quantity = max(
                line.dispatched_quantity - line.processed_received_quantity, 0.0,
            )
            line.receiving_difference = (
                line.amazon_received_quantity - line.dispatched_quantity
            )

    @api.constrains(
        'dispatched_quantity', 'amazon_shipped_quantity',
        'amazon_received_quantity', 'processed_received_quantity',
    )
    def _check_receiving_quantities(self):
        for line in self:
            if any(value < 0 for value in (
                line.dispatched_quantity,
                line.amazon_shipped_quantity,
                line.amazon_received_quantity,
                line.processed_received_quantity,
            )):
                raise ValidationError(_("Receiving quantities cannot be negative."))
            if line.processed_received_quantity > line.dispatched_quantity:
                raise ValidationError(_(
                    "Processed received quantity cannot exceed Odoo dispatched quantity."
                ))


class AmazonInboundShipmentLineReceiving(models.Model):
    _inherit = 'amazon.inbound.shipment.line'

    received_moved_quantity = fields.Float(
        string='Received Qty Moved', default=0.0, copy=False, readonly=True,
        help="Compatibility aggregate of physical quantities moved to Received / Staging.",
    )
    quantity_damaged = fields.Float('Qty Damaged', default=0.0, copy=False, readonly=True)
    damaged_moved_quantity = fields.Float(
        string='Damaged Qty Moved', default=0.0, copy=False, readonly=True,
    )
    quantity_lost = fields.Float('Qty Lost', default=0.0, copy=False, readonly=True)


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
        'amazon.inbound.shipment.line', ondelete='cascade', index=True,
        help="Legacy plan-level line link.",
    )
    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', ondelete='cascade', index=True,
        check_company=True,
    )
    physical_line_id = fields.Many2one(
        'amazon.fba.physical.shipment.line', ondelete='cascade', index=True,
        check_company=True,
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
        ('received_overage', 'Receiving Quantity Mismatch'),
        ('received_quantity_decrease', 'Received Quantity Decreased'),
        ('unmapped_amazon_sku', 'Unmapped Amazon SKU'),
        ('missing_amazon_item', 'Amazon Omitted Expected Item'),
    ], required=True, readonly=True, index=True)
    quantity = fields.Float(required=True, readonly=True, help="Absolute discrepancy quantity.")
    amazon_quantity = fields.Float(readonly=True)
    odoo_quantity = fields.Float(readonly=True)
    amazon_status = fields.Char(readonly=True, index=True)
    amazon_reported_lost = fields.Boolean(
        default=False, readonly=True,
        help="False because the supported inbound item operation does not expose lost disposition.",
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
        'Only one legacy discrepancy of each type can exist per inbound plan line.',
    )
    _unique_physical_line_type = models.UniqueIndex(
        '(physical_shipment_id, COALESCE(physical_line_id, 0), discrepancy_type, sku) '
        'WHERE physical_shipment_id IS NOT NULL',
        'Only one discrepancy of each type can exist per physical shipment item.',
    )
    _positive_quantity = models.Constraint(
        'CHECK (quantity > 0)',
        'Inventory discrepancy quantity must be positive.',
    )

    @api.constrains('shipment_id', 'physical_shipment_id', 'physical_line_id')
    def _check_receiving_scope(self):
        for discrepancy in self:
            if (
                discrepancy.physical_shipment_id
                and discrepancy.physical_shipment_id.inbound_shipment_id != discrepancy.shipment_id
            ):
                raise ValidationError(_(
                    "The discrepancy physical shipment must belong to the inbound plan."
                ))
            if (
                discrepancy.physical_line_id
                and discrepancy.physical_line_id.physical_shipment_id
                != discrepancy.physical_shipment_id
            ):
                raise ValidationError(_(
                    "The discrepancy item must belong to the physical shipment."
                ))


class StockPickingAmazonInboundReceiving(models.Model):
    _inherit = 'stock.picking'

    amazon_fba_movement_type = fields.Selection([
        ('outbound_to_transit', 'Source to Amazon Transit'),
        ('receiving_staging', 'Amazon Transit to Received / Staging'),
        ('receiving_sellable', 'Legacy: Amazon Transit to Sellable'),
        ('receiving_unsellable', 'Legacy: Amazon Transit to Unsellable'),
    ], copy=False, readonly=True, index=True)
    amazon_inbound_operation_job_id = fields.Many2one(
        'amazon.inbound.operation.job', string='Amazon Receiving Sync Job',
        copy=False, readonly=True, ondelete='set null', index=True,
    )


class StockMoveAmazonInboundReceiving(models.Model):
    _inherit = 'stock.move'

    amazon_fba_physical_shipment_line_id = fields.Many2one(
        'amazon.fba.physical.shipment.line', string='Amazon Physical Shipment Item',
        copy=False, readonly=True, ondelete='restrict', index=True,
    )
    amazon_received_before = fields.Float(copy=False, readonly=True)
    amazon_received_after = fields.Float(copy=False, readonly=True)
    amazon_receiving_delta = fields.Float(copy=False, readonly=True)


class AmazonInboundOperationJobReceiving(models.Model):
    _inherit = 'amazon.inbound.operation.job'

    operation_type = fields.Selection(selection_add=[
        ('sync_receiving', 'Synchronize Amazon Receiving'),
    ], ondelete={'sync_receiving': 'cascade'})

    _unique_active_receiving_job = models.UniqueIndex(
        '(physical_shipment_id) WHERE operation_type = \'sync_receiving\' '
        "AND state IN ('pending', 'in_progress')",
        'Only one active receiving synchronization can exist per physical shipment.',
    )

    @staticmethod
    def _receiving_exception_details(exc):
        current = exc
        for _depth in range(5):
            if isinstance(current, requests.exceptions.HTTPError):
                response = current.response
                status = response.status_code if response is not None else None
                retry_after = response.headers.get('Retry-After') if response is not None else None
                return status, retry_after, status == 429 or bool(status and status >= 500)
            if isinstance(current, (
                requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            )):
                return None, None, True
            current = getattr(current, '__cause__', None)
            if current is None:
                break
        return None, None, False

    def _process_operation(self):
        self.ensure_one()
        if self.operation_type != 'sync_receiving':
            return super()._process_operation()
        if self.state in ('done', 'failed'):
            return False
        physical = self.physical_shipment_id.sudo()
        if not physical:
            self.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'last_error': _("Missing physical shipment."),
            })
            return False
        now = fields.Datetime.now()
        vals = {'state': 'in_progress', 'next_run_at': False}
        if not self.started_at:
            vals['started_at'] = now
        self.write(vals)
        physical.write({'receiving_sync_status': 'in_progress'})
        try:
            result = physical._sync_amazon_receiving(job=self)
            self.write({'response_data': physical.inbound_shipment_id._sanitized_json(result)})
            if physical.receiving_response:
                response = json.loads(physical.receiving_response)
                item_response = response.get('getShipmentItemsByShipmentId') or {}
                request_ids = item_response.get('_amazon_request_ids') or []
                self.amazon_request_id = request_ids[-1] if request_ids else False
            self._mark_done()
            return True
        except Exception as exc:
            message = str(exc)
            status_code, retry_after, transient = self._receiving_exception_details(exc)
            if status_code:
                error_code = 'HTTP_%s' % status_code
            elif transient:
                error_code = 'NETWORK_ERROR'
            elif isinstance(exc, (UserError, ValidationError)):
                error_code = 'VALIDATION_ERROR'
            else:
                error_code = 'RECEIVING_ERROR'
            _logger.warning(
                "Amazon physical receiving job %s failed (%s): %s",
                self.id, error_code, message,
            )
            if transient:
                self._schedule_retry(error_message=message)
                if retry_after and self.state != 'failed':
                    try:
                        retry_at = fields.Datetime.now() + timedelta(
                            seconds=max(float(retry_after), 0.0)
                        )
                    except (TypeError, ValueError):
                        retry_at = False
                    if retry_at and (not self.next_run_at or retry_at > self.next_run_at):
                        self.next_run_at = retry_at
            else:
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'next_run_at': False,
                    'last_error': message,
                })
            physical.write({
                'receiving_sync_status': 'failed' if self.state == 'failed' else 'pending',
                'receiving_error_code': error_code,
                'receiving_error_message': message,
            })
            return False
