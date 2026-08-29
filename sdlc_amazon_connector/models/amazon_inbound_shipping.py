import base64
import json
import logging
import re
from urllib.parse import urlparse

import requests

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .amazon_api import AmazonAPI
from .amazon_inbound_shipment import INBOUND_PLAN_ID_RE, OPERATION_ID_RE

_logger = logging.getLogger(__name__)

AMAZON_SHIPMENT_ID_RE = INBOUND_PLAN_ID_RE
AMAZON_FREIGHT_BILL_RE = re.compile(r'^[A-Za-z0-9._ -]{1,64}$')
MAX_SHIPPING_LABEL_BYTES = 25 * 1024 * 1024
PHASE4_STATES = {
    'placement_confirmed', 'picking_created', 'ready_to_ship', 'dispatched',
    'shipment_confirmed', 'waiting_receiving', 'partially_received',
    'received', 'closed',
}


class AmazonInboundShipmentShipping(models.Model):
    _inherit = 'amazon.inbound.shipment'

    state = fields.Selection(selection_add=[
        ('picking_created', 'Picking Created'),
        ('ready_to_ship', 'Ready to Dispatch'),
        ('dispatched', 'Dispatched'),
        ('shipment_confirmed', 'Shipment Confirmed'),
        ('waiting_receiving', 'Waiting for Amazon Receiving'),
    ], ondelete={
        'picking_created': 'set default',
        'ready_to_ship': 'set default',
        'dispatched': 'set default',
        'shipment_confirmed': 'set default',
        'waiting_receiving': 'set default',
    })
    picking_ids = fields.One2many(
        'stock.picking', 'amazon_inbound_shipment_id', string='Internal Pickings',
    )
    picking_count = fields.Integer(compute='_compute_picking_count')
    shipment_confirmation_id = fields.Char(
        'Shipment Confirmation ID', copy=False, index=True,
        help="Confirmed shipment identifier returned by getShipment, for example FBA1234ABCD.",
    )
    selected_transportation_option_id = fields.Char(
        'Selected Transportation Option ID', copy=False, readonly=True, index=True,
        help="Official selectedTransportationOptionId returned by getShipment.",
    )
    shipping_method = fields.Selection([
        ('spd', 'Small Parcel Delivery (SPD)'),
        ('ltl', 'Less Than Truckload (LTL)'),
    ], string='Shipping Method', copy=False)
    tracking_id = fields.Char('Tracking Number', copy=False)
    bill_of_lading_number = fields.Char('Bill of Lading Number', copy=False)
    shipment_status = fields.Char('Amazon Shipment Status', copy=False, readonly=True, index=True)
    shipment_confirmation_operation_id = fields.Char(
        'Tracking Update Operation ID', copy=False, readonly=True, index=True,
    )
    shipment_confirmation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string='Shipment Confirmation Status', copy=False, readonly=True, index=True)
    shipment_last_refresh_at = fields.Datetime(copy=False, readonly=True)
    shipment_error_code = fields.Char(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    shipment_error_message = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    shipment_response = fields.Text(
        string='Sanitized Shipment Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    @api.depends('picking_ids')
    def _compute_picking_count(self):
        for shipment in self:
            shipment.picking_count = len(shipment.picking_ids)

    def _lock_phase4_workflow(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM amazon_inbound_shipment WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

    def _active_fba_pickings(self):
        self.ensure_one()
        return self.picking_ids.filtered(lambda picking: picking.state != 'cancel')

    def _update_dispatch_state(self):
        """Aggregate physical-shipment dispatch without changing later Amazon states."""
        for inbound in self:
            if inbound.state in (
                'shipment_confirmed', 'waiting_receiving', 'partially_received',
                'received', 'closed', 'cancelled',
            ):
                continue
            physical_shipments = inbound.physical_shipment_ids
            if not physical_shipments:
                continue
            states = set(physical_shipments.mapped('dispatch_state'))
            if states == {'dispatched'}:
                state = 'dispatched'
            elif states <= {'ready_to_dispatch', 'dispatched'}:
                state = 'ready_to_ship'
            elif states != {'placement_confirmed'}:
                state = 'picking_created'
            else:
                state = 'placement_confirmed'
            if inbound.state != state:
                inbound.sudo().write({'state': state})

    def _validate_fba_stock_locations(self):
        self.ensure_one()
        source = self.instance_id.fba_source_location_id
        transit = self.instance_id.fba_transit_location_id
        if not source or not transit:
            raise UserError(_(
                "Configure both the FBA Source Location and Amazon Transit Location on the instance."
            ))
        if not source.active or source.usage != 'internal':
            raise UserError(_("The configured FBA Source Location must be an active internal location."))
        if not transit.active or transit.usage != 'transit':
            raise UserError(_("The configured Amazon Transit Location must be an active transit location."))
        if source == transit:
            raise UserError(_("The FBA Source and Amazon Transit locations must be different."))
        if source.company_id and source.company_id != self.company_id:
            raise UserError(_("The FBA Source Location belongs to another company."))
        if transit.company_id != self.company_id:
            raise UserError(_("The Amazon Transit Location must belong to the shipment company."))
        return source, transit

    def _get_internal_picking_type(self, source):
        self.ensure_one()
        warehouse = source.warehouse_id
        picking_type = warehouse.int_type_id if warehouse.company_id == self.company_id else False
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', self.company_id.id),
            ], order='warehouse_id, sequence, id', limit=1)
        if not picking_type:
            raise UserError(_("No Internal Transfer operation type exists for the shipment company."))
        return picking_type

    def action_create_picking(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase4_workflow()
        if self.state not in ('placement_confirmed', 'picking_created', 'ready_to_ship', 'dispatched'):
            raise UserError(_("Dispatch pickings can only be created after placement is confirmed."))
        physical_shipments = self.physical_shipment_ids.filtered(
            lambda physical: physical.placement_option_id.selected
            and physical.placement_option_id.status == 'ACCEPTED'
        )
        if not physical_shipments:
            raise UserError(_(
                "No confirmed Amazon physical shipments are available. Refresh the confirmed "
                "placement result first."
            ))

        # A pre-existing single-shipment Phase 4 picking is linked rather than duplicated.
        legacy_pickings = self._active_fba_pickings().filtered(
            lambda picking: not picking.amazon_fba_physical_shipment_id
        )
        if legacy_pickings:
            if len(physical_shipments) != 1 or len(legacy_pickings) != 1:
                raise UserError(_(
                    "Existing aggregate picking(s) cannot be allocated safely across the Amazon "
                    "shipment splits. Cancel them or link them manually before dispatch."
                ))
            physical_shipments._link_compatible_legacy_picking(legacy_pickings)

        created = self.env['stock.picking']
        for physical in physical_shipments:
            picking, was_created = physical._create_dispatch_picking()
            if was_created:
                created |= picking
        self._update_dispatch_state()
        active = physical_shipments.mapped('picking_ids').filtered(lambda p: p.state != 'cancel')
        self.sudo().write({'picking_id': active.id if len(active) == 1 else False})
        if not created:
            return self.action_open_picking()
        unreserved = created.filtered(lambda picking: picking.state != 'assigned')
        if unreserved:
            return self.instance_id._notify(
                _("FBA Dispatch Picking"),
                _(
                    "%s dispatch picking(s) were created. Some quantities are not fully reserved; "
                    "use standard Odoo availability checks after stock is replenished.",
                    len(created),
                ),
                'warning',
            )
        return self.instance_id._notify(
            _("FBA Dispatch Picking"),
            _("%s dispatch picking(s) were created and reserved.", len(created)),
            'success',
        )

    def action_open_picking(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        pickings = self._active_fba_pickings()
        if not pickings:
            raise UserError(_("No active internal picking is linked to this shipment."))
        action = {
            'type': 'ir.actions.act_window',
            'name': _("FBA Internal Pickings"),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
            'context': {'create': False},
        }
        if len(pickings) == 1:
            action.update(view_mode='form', res_id=pickings.id)
        return action

    def _prepare_tracking_details_payload(self):
        self.ensure_one()
        if self.shipping_method not in ('spd', 'ltl'):
            raise UserError(_("Select Small Parcel Delivery or Less Than Truckload."))
        if self.carrier_type == 'partnered':
            return False
        if self.shipping_method == 'spd':
            tracking_number = (self.tracking_id or '').strip()
            if not tracking_number:
                raise UserError(_("Enter the tracking number for the non-partnered SPD shipment."))
            if len(tracking_number) > 64:
                raise UserError(_("The SPD tracking number cannot exceed 64 characters."))
            packing = self.packing_option_ids.filtered('selected')
            if len(packing) != 1 or packing.status != 'ACCEPTED':
                raise UserError(_("The accepted packing option is required for SPD tracking."))
            boxes = packing.box_ids
            if not boxes or any(not (box.amazon_box_id or '').strip() for box in boxes):
                raise UserError(_(
                    "Every SPD box requires the official Amazon boxId before tracking can be submitted."
                ))
            box_ids = [(box.amazon_box_id or '').strip() for box in boxes]
            if len(box_ids) != len(set(box_ids)):
                raise UserError(_("Amazon box IDs must be unique before submitting SPD tracking."))
            return {
                'trackingDetails': {
                    'spdTrackingDetail': {
                        'spdTrackingItems': [
                            {'boxId': box_id, 'trackingId': tracking_number}
                            for box_id in box_ids
                        ],
                    },
                },
            }
        if self.shipping_method == 'ltl':
            freight_bill = (self.pro_number or self.tracking_id or '').strip()
            if not AMAZON_FREIGHT_BILL_RE.fullmatch(freight_bill):
                raise UserError(_(
                    "Enter a valid PRO/freight bill number (1-64 supported characters) for LTL tracking."
                ))
            detail = {'freightBillNumber': [freight_bill]}
            bill_of_lading = (self.bill_of_lading_number or '').strip()
            if len(bill_of_lading) > 1024:
                raise UserError(_("The bill of lading number cannot exceed 1024 characters."))
            if bill_of_lading:
                detail['billOfLadingNumber'] = bill_of_lading
            return {'trackingDetails': {'ltlTrackingDetail': detail}}
        raise UserError(_("Select Small Parcel Delivery or Less Than Truckload."))

    def _validate_shipment_confirmation_inputs(self):
        self.ensure_one()
        self._selected_amazon_shipment_id()
        if not self.selected_transportation_option_id:
            raise UserError(_(
                "Amazon transportation is not confirmed. Refresh Shipment Status after selecting "
                "a transportation option in the official Amazon workflow."
            ))
        if not self.shipment_confirmation_id:
            raise UserError(_(
                "Shipment Confirmation ID is not available. Refresh Shipment Status before shipping."
            ))
        if not (self.carrier_name or '').strip():
            raise UserError(_("Enter the carrier."))
        if not self.ship_date:
            raise UserError(_("Enter the shipment date."))
        self._prepare_tracking_details_payload()

    def _selected_amazon_shipment_id(self):
        """Return the single physical shipment ID for legacy plan-level buttons."""
        self.ensure_one()
        physical = self.physical_shipment_ids.filtered(
            lambda item: item.placement_option_id.selected
            and item.placement_option_id.status == 'ACCEPTED'
        )
        if not physical and self.physical_shipment_ids:
            physical = self.physical_shipment_ids
        if len(physical) == 1:
            shipment_id = (physical.amazon_shipment_id or '').strip()
        else:
            shipment_id = (self.shipment_id or '').strip()
        if not shipment_id or not AMAZON_SHIPMENT_ID_RE.fullmatch(shipment_id):
            raise UserError(_(
                "Select a single Amazon physical shipment. Transportation and tracking are "
                "shipment-level operations."
            ))
        return shipment_id

    def _validate_reserved_pickings(self):
        self.ensure_one()
        pickings = self._active_fba_pickings()
        if not pickings:
            raise UserError(_("Create and reserve the internal picking before confirming shipment."))
        for picking in pickings.filtered(lambda record: record.state != 'done'):
            if picking.state != 'assigned' or any(
                move.product_uom.compare(move.quantity, move.product_uom_qty) != 0
                for move in picking.move_ids.filtered(lambda move: move.state != 'cancel')
            ):
                raise UserError(_(
                    "Picking %s is not fully reserved. Resolve stock availability before shipping.",
                    picking.name,
                ))
        return pickings

    def _enqueue_shipment_confirmation_job(self):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('inbound_shipment_id', '=', self.id),
            ('operation_type', '=', 'confirm_shipment'),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        failed = Job.search([
            ('inbound_shipment_id', '=', self.id),
            ('operation_type', '=', 'confirm_shipment'),
            ('state', '=', 'failed'),
        ], order='id desc', limit=1)
        if failed:
            failed.write({
                'operation_id': False,
                'state': 'pending',
                'retry_count': 0,
                'next_run_at': fields.Datetime.now(),
                'last_error': False,
                'started_at': False,
                'finished_at': False,
                'raw_operation_status': False,
            })
            return failed, True
        return Job.create({
            'inbound_shipment_id': self.id,
            'operation_type': 'confirm_shipment',
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }), True

    def action_confirm_shipment(self):
        raise UserError(_(
            "This legacy combined dispatch/tracking action is disabled. Complete transportation, "
            "delivery window (when required), box labels, and tracking on every Amazon physical "
            "shipment; then validate each dispatch picking intentionally."
        ))

    def _merge_shipment_response(self, key, value):
        self.ensure_one()
        history = {}
        if self.shipment_response:
            try:
                history = json.loads(self.shipment_response)
            except (TypeError, ValueError):
                history = {'legacyResponse': self.shipment_response}
        history[key] = AmazonAPI._sanitize_for_log(value)
        return self._sanitized_json(history)

    def _refresh_shipment_status(self):
        self.ensure_one()
        shipment_id = self._selected_amazon_shipment_id()
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_shipment,
            self.instance_id,
            access_token,
            self.inbound_plan_id,
            shipment_id,
            error_msg=_("Failed to refresh Amazon inbound shipment status"),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        returned_id = str(result.get('shipmentId') or '').strip()
        if returned_id and returned_id != shipment_id:
            raise ValidationError(_("Amazon returned status for a different shipmentId."))
        destination = result.get('destination') or {}
        if not isinstance(destination, dict):
            destination = {}
        vals = {
            'shipment_status': str(result.get('status') or '').strip() or False,
            'shipment_confirmation_id': str(
                result.get('shipmentConfirmationId') or ''
            ).strip() or self.shipment_confirmation_id,
            'selected_transportation_option_id': str(
                result.get('selectedTransportationOptionId') or ''
            ).strip() or self.selected_transportation_option_id,
            'destination_fulfillment_center': str(
                destination.get('warehouseId') or ''
            ).strip() or self.destination_fulfillment_center,
            'shipment_last_refresh_at': fields.Datetime.now(),
            'shipment_error_code': False,
            'shipment_error_message': False,
            'shipment_response': self._merge_shipment_response('getShipment', result),
        }
        tracking = result.get('trackingDetails') or {}
        if isinstance(tracking, dict):
            spd = tracking.get('spdTrackingDetail') or {}
            items = spd.get('spdTrackingItems') or [] if isinstance(spd, dict) else []
            if items and isinstance(items[0], dict) and items[0].get('trackingId'):
                vals['tracking_id'] = str(items[0]['trackingId']).strip()
            ltl = tracking.get('ltlTrackingDetail') or {}
            freight_numbers = ltl.get('freightBillNumber') or [] if isinstance(ltl, dict) else []
            if freight_numbers:
                vals['pro_number'] = str(freight_numbers[0]).strip()
            if isinstance(ltl, dict) and ltl.get('billOfLadingNumber'):
                vals['bill_of_lading_number'] = str(ltl['billOfLadingNumber']).strip()
        self.sudo().write(vals)
        selected = self.placement_option_ids.filtered('selected')
        if selected and vals['destination_fulfillment_center']:
            selected.sudo().write({'destination_fc': vals['destination_fulfillment_center']})
        return result

    def _enqueue_shipment_status_job(self):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('inbound_shipment_id', '=', self.id),
            ('operation_type', '=', 'refresh_shipment_status'),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        return Job.create({
            'inbound_shipment_id': self.id,
            'operation_type': 'refresh_shipment_status',
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }), True

    def action_refresh_shipment_status(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase4_workflow()
        if self.state not in PHASE4_STATES:
            raise UserError(_("Shipment status is available only after placement is confirmed."))
        self._selected_amazon_shipment_id()
        _job, created = self._enqueue_shipment_status_job()
        return self.instance_id._notify(
            _("Amazon Shipment Status"),
            _("Shipment status refresh was queued.")
            if created else _("A shipment status refresh is already queued."),
            'success' if created else 'warning',
        )

    def _process_shipment_confirmation_job(self, job):
        self.ensure_one()
        if not job.operation_id:
            body = self._prepare_tracking_details_payload()
            if not body:
                self._refresh_shipment_status()
                self.sudo().write({
                    'shipment_confirmation_status': 'success',
                    'state': 'waiting_receiving',
                })
                return 'success'
            access_token = self.instance_id._get_access_token_or_raise()
            result = self.instance_id._api_call_safe(
                AmazonAPI().update_shipment_tracking_details,
                self.instance_id,
                access_token,
                self.inbound_plan_id,
                self._selected_amazon_shipment_id(),
                body,
                error_msg=_("Failed to update Amazon inbound shipment tracking"),
            )
            if not isinstance(result, dict):
                result = {'unexpectedResponse': result}
            operation_id = str(result.get('operationId') or '').strip()
            response_text = self._merge_shipment_response(
                'updateShipmentTrackingDetails', result,
            )
            self.sudo().write({
                'shipment_response': response_text,
                'shipment_confirmation_status': 'in_progress',
                'shipment_confirmation_operation_id': operation_id or False,
            })
            job.sudo().write({
                'response_data': self._sanitized_json(result),
                'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
            })
            if not OPERATION_ID_RE.fullmatch(operation_id):
                raise UserError(_("Amazon did not return a valid tracking update operationId."))
            job.sudo().write({'operation_id': operation_id})
            return 'in_progress'

        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_operation_status,
            self.instance_id,
            access_token,
            job.operation_id,
            error_msg=_("Failed to poll the shipment tracking update"),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        raw_status = str(result.get('operationStatus') or '').strip()
        normalized = raw_status.upper().replace('-', '_').replace(' ', '_')
        error_code, error_message = self._operation_problem_values(
            result.get('operationProblems')
        )
        self.sudo().write({
            'shipment_response': self._merge_shipment_response(
                'getInboundOperationStatus:confirm_shipment', result,
            ),
        })
        job.sudo().write({
            'raw_operation_status': raw_status or False,
            'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
            'response_data': self._sanitized_json(result),
        })
        if normalized in {'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'COMPLETE'}:
            self._refresh_shipment_status()
            self.sudo().write({
                'shipment_confirmation_status': 'success',
                'shipment_error_code': False,
                'shipment_error_message': False,
                'state': 'waiting_receiving',
            })
            return 'success'
        if normalized in {'FAILED', 'FAILURE', 'ERROR'}:
            self.sudo().write({
                'shipment_confirmation_status': 'failed',
                'shipment_error_code': error_code or 'AMAZON_OPERATION_FAILED',
                'shipment_error_message': error_message or _(
                    "Amazon reported that the tracking update failed."
                ),
            })
            return 'failed'
        self.sudo().write({
            'shipment_confirmation_status': (
                'pending' if normalized in {'PENDING', 'QUEUED', 'NOT_STARTED'}
                else 'in_progress'
            ),
            'shipment_error_code': error_code,
            'shipment_error_message': error_message,
        })
        return 'in_progress'

    def action_submit_shipment(self):
        """Backward-compatible button alias for the Phase 4 business action."""
        return self.action_confirm_shipment()

    def action_check_status(self):
        self.ensure_one()
        if self.state in PHASE4_STATES:
            return self.action_refresh_shipment_status()
        return super().action_check_status()


class AmazonFbaPhysicalShipmentDispatch(models.Model):
    _inherit = 'amazon.fba.physical.shipment'

    picking_ids = fields.One2many(
        'stock.picking', 'amazon_fba_physical_shipment_id', string='Dispatch Pickings',
    )
    picking_id = fields.Many2one(
        'stock.picking', string='Dispatch Picking', compute='_compute_dispatch_picking',
    )
    picking_state = fields.Char(string='Picking State', compute='_compute_dispatch_picking')
    dispatch_state = fields.Selection([
        ('placement_confirmed', 'Placement Confirmed'),
        ('picking_created', 'Picking Created'),
        ('ready_to_dispatch', 'Ready to Dispatch'),
        ('dispatched', 'Dispatched'),
    ], default='placement_confirmed', required=True, copy=False, index=True)
    source_location_id = fields.Many2one(
        related='instance_id.fba_source_location_id', string='Source Location', readonly=True,
    )
    transit_location_id = fields.Many2one(
        related='instance_id.fba_transit_location_id', string='Transit Location', readonly=True,
    )
    dispatch_quantity = fields.Integer(compute='_compute_dispatch_quantity')
    dispatch_date = fields.Datetime(copy=False, readonly=True)
    transportation_option_ids = fields.One2many(
        'amazon.fba.transportation.option', 'physical_shipment_id',
        string='Transportation Options',
    )
    selected_transportation_option_id = fields.Many2one(
        'amazon.fba.transportation.option', string='Selected Transportation Option',
        copy=False, check_company=True,
    )
    transportation_generation_operation_id = fields.Char(copy=False, readonly=True, index=True)
    transportation_generation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    transportation_confirmation_operation_id = fields.Char(copy=False, readonly=True, index=True)
    transportation_confirmation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    transportation_status = fields.Char(copy=False, readonly=True, index=True)
    transportation_error_code = fields.Char(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    transportation_error_message = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    transportation_last_sync_at = fields.Datetime(copy=False, readonly=True)
    transportation_response = fields.Text(
        string='Sanitized Transportation Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    delivery_window_option_ids = fields.One2many(
        'amazon.fba.delivery.window.option', 'physical_shipment_id',
        string='Delivery Window Options',
    )
    selected_delivery_window_option_id = fields.Many2one(
        'amazon.fba.delivery.window.option', string='Selected Delivery Window',
        copy=False, check_company=True,
    )
    delivery_window_required = fields.Boolean(copy=False, readonly=True, index=True)
    delivery_window_generation_operation_id = fields.Char(copy=False, readonly=True, index=True)
    delivery_window_generation_status = fields.Selection([
        ('pending', 'Pending'), ('in_progress', 'In Progress'),
        ('success', 'Success'), ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    delivery_window_confirmation_operation_id = fields.Char(copy=False, readonly=True, index=True)
    delivery_window_confirmation_status = fields.Selection([
        ('pending', 'Pending'), ('in_progress', 'In Progress'),
        ('success', 'Success'), ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    shipment_box_ids = fields.One2many(
        'amazon.fba.shipment.box', 'physical_shipment_id', string='Amazon Shipment Boxes',
    )
    label_page_type = fields.Selection([
        ('PackageLabel_A4_2', 'A4 - 2 labels'),
        ('PackageLabel_A4_4', 'A4 - 4 labels'),
        ('PackageLabel_Thermal_NonPCP', 'Thermal - Non-Partnered'),
        ('PackageLabel_Thermal_Unified', 'Thermal - Unified'),
    ], default='PackageLabel_A4_2', required=True, copy=False)
    labels_status = fields.Selection([
        ('success', 'Available'), ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    label_download_url = fields.Char(copy=False, readonly=True)
    shipping_label_attachment_id = fields.Many2one(
        'ir.attachment', string='Stored Box/Shipping Labels', copy=False, readonly=True,
        ondelete='set null',
    )
    shipping_label_filename = fields.Char(copy=False, readonly=True)
    shipping_label_content_type = fields.Char(copy=False, readonly=True)
    labels_last_synced_at = fields.Datetime(copy=False, readonly=True)
    product_labels_required = fields.Boolean(
        string='Seller Product/FNSKU Labels Required',
        compute='_compute_product_labels_required',
    )
    product_labels_confirmed = fields.Boolean(
        string='Product/FNSKU Labels Applied', copy=False, readonly=True,
    )
    product_labels_confirmed_at = fields.Datetime(copy=False, readonly=True)
    product_labels_confirmed_by_id = fields.Many2one(
        'res.users', string='Product Labels Confirmed By', copy=False, readonly=True,
        ondelete='set null',
    )
    carrier_type = fields.Selection([
        ('partnered', 'Amazon Partnered'),
        ('non_partnered', 'Non-Partnered'),
    ], string='Carrier Type', copy=False)
    carrier_name = fields.Char(copy=False)
    carrier_code = fields.Char(copy=False)
    shipping_mode = fields.Char(copy=False, readonly=True)
    tracking_number = fields.Char(copy=False)
    pro_number = fields.Char(copy=False)
    bill_of_lading_number = fields.Char(copy=False)
    ship_date = fields.Date(copy=False)
    estimated_arrival = fields.Date(copy=False)
    tracking_operation_id = fields.Char(copy=False, readonly=True, index=True)
    tracking_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, readonly=True, index=True)
    tracking_last_synced_at = fields.Datetime(copy=False, readonly=True)

    @api.depends('picking_ids.state')
    def _compute_dispatch_picking(self):
        for physical in self:
            picking = physical.picking_ids.filtered(
                lambda picking: picking.state != 'cancel'
                and picking.amazon_fba_movement_type == 'outbound_to_transit'
            )[:1]
            physical.picking_id = picking
            physical.picking_state = picking.state or False

    @api.depends('line_ids.quantity')
    def _compute_dispatch_quantity(self):
        for physical in self:
            physical.dispatch_quantity = sum(physical.line_ids.mapped('quantity'))

    @api.depends(
        'line_ids.msku',
        'inbound_shipment_id.line_ids.sku',
        'inbound_shipment_id.line_ids.label_owner',
    )
    def _compute_product_labels_required(self):
        for physical in self:
            seller_labeled_mskus = set(
                physical.inbound_shipment_id.line_ids.filtered(
                    lambda line: line.label_owner == 'SELLER'
                ).mapped('sku')
            )
            physical.product_labels_required = any(
                line.msku in seller_labeled_mskus for line in physical.line_ids
            )

    def action_confirm_product_labels(self):
        """Record the operator's physical product/FNSKU labeling checkpoint."""
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._lock_dispatch()
        if not self.product_labels_required:
            return self.instance_id._notify(
                _("FBA Product Labels"),
                _("Amazon does not require seller-applied product labels for this shipment."),
                'warning',
            )
        if self.product_labels_confirmed:
            return self.instance_id._notify(
                _("FBA Product Labels"),
                _("Seller-applied product/FNSKU labels are already confirmed."),
                'warning',
            )
        self.sudo().write({
            'product_labels_confirmed': True,
            'product_labels_confirmed_at': fields.Datetime.now(),
            'product_labels_confirmed_by_id': self.env.user.id,
        })
        return self.instance_id._notify(
            _("FBA Product Labels"),
            _("Seller-applied product/FNSKU labels were recorded as printed and applied."),
            'success',
        )

    def _lock_dispatch(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM amazon_fba_physical_shipment WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

    def _validate_dispatch_preconditions(self):
        self.ensure_one()
        inbound = self.inbound_shipment_id
        if not inbound.inbound_plan_id or not INBOUND_PLAN_ID_RE.fullmatch(inbound.inbound_plan_id):
            raise UserError(_("The inbound plan does not have a valid Amazon inboundPlanId."))
        if inbound.create_operation_status != 'success':
            raise UserError(_("The inbound plan must be created successfully before dispatch."))
        if inbound.packing_confirmation_status != 'success':
            raise UserError(_("Packing must be confirmed successfully before dispatch."))
        if inbound.packing_information_status != 'success':
            raise UserError(_("Box-level packing information must be submitted successfully before dispatch."))
        if inbound.placement_confirmation_status != 'success':
            raise UserError(_("Placement must be confirmed successfully before dispatch."))
        if not self.placement_option_id.selected or self.placement_option_id.status != 'ACCEPTED':
            raise UserError(_("The physical shipment must belong to the accepted placement option."))
        if not self.amazon_shipment_id or not AMAZON_SHIPMENT_ID_RE.fullmatch(
            self.amazon_shipment_id.strip()
        ):
            raise UserError(_("The Amazon physical shipment does not have a valid shipmentId."))
        if not (self.shipment_confirmation_id or '').strip():
            raise UserError(_(
                "Amazon has not returned the shipmentConfirmationId for this physical shipment."
            ))
        if not self.line_ids:
            raise UserError(_("The Amazon physical shipment has no final placement items."))
        return inbound._validate_fba_stock_locations()

    def _merge_transportation_response(self, key, value):
        self.ensure_one()
        history = {}
        if self.transportation_response:
            try:
                history = json.loads(self.transportation_response)
            except (TypeError, ValueError):
                history = {'legacyResponse': self.transportation_response}
        history[key] = AmazonAPI._sanitize_for_log(value)
        return self.inbound_shipment_id._sanitized_json(history)

    def _validate_transportation_preconditions(self):
        self.ensure_one()
        self._validate_dispatch_preconditions()
        for line in self.line_ids:
            product = line.amazon_product_id.odoo_product_id
            if not product:
                raise UserError(_("Physical shipment line %s has no mapped Odoo product.", line.msku))

    def _accepted_plan_physicals(self):
        self.ensure_one()
        return self.inbound_shipment_id.physical_shipment_ids.filtered(
            lambda physical: physical.placement_option_id.selected
            and physical.placement_option_id.status == 'ACCEPTED'
        )

    def _prepare_contact_information(self):
        self.ensure_one()
        partner = self.instance_id.fba_ship_from_partner_id
        if not partner:
            return False
        contact = {}
        if (partner.email or '').strip():
            contact['email'] = partner.email.strip()
        if (partner.name or '').strip():
            contact['name'] = partner.name.strip()
        if (partner.phone or '').strip():
            contact['phoneNumber'] = partner.phone.strip()
        return contact or False

    def _prepare_ready_to_ship_window(self):
        self.ensure_one()
        start = self.dispatch_date or fields.Datetime.now()
        if isinstance(start, str):
            start = fields.Datetime.from_string(start)
        return {'start': start.replace(microsecond=0).isoformat() + 'Z'}

    def _prepare_transportation_generation_payload(self):
        self.ensure_one()
        self._validate_transportation_preconditions()
        configs = []
        for physical in self._accepted_plan_physicals():
            physical._validate_transportation_preconditions()
            config = {
                'shipmentId': physical.amazon_shipment_id.strip(),
                'readyToShipWindow': physical._prepare_ready_to_ship_window(),
            }
            contact = physical._prepare_contact_information()
            if contact:
                config['contactInformation'] = contact
            configs.append(config)
        return {
            'placementOptionId': self.placement_option_id.amazon_placement_option_id,
            'shipmentTransportationConfigurations': configs,
        }

    def _enqueue_physical_job(self, operation_type, option=False):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('inbound_shipment_id', '=', self.inbound_shipment_id.id),
            ('physical_shipment_id', '=', self.id),
            ('operation_type', '=', operation_type),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        vals = {
            'inbound_shipment_id': self.inbound_shipment_id.id,
            'physical_shipment_id': self.id,
            'operation_type': operation_type,
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }
        if option:
            vals['transportation_option_id'] = option.id
        return Job.create(vals), True

    def action_generate_transportation_options(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self.inbound_shipment_id._lock_phase3_workflow()
        physicals = self._accepted_plan_physicals()
        if any(code == 'WRITE_OUTCOME_UNKNOWN' for code in physicals.mapped(
            'transportation_error_code'
        )):
            raise UserError(_(
                "A previous transportation write has an unknown Amazon outcome. Reconcile the "
                "plan with Amazon before any resubmission."
            ))
        if any(status in ('pending', 'in_progress', 'success') for status in physicals.mapped(
            'transportation_confirmation_status'
        )):
            raise UserError(_("Transportation options cannot be regenerated after plan confirmation starts."))
        if any(status in ('pending', 'in_progress', 'success') for status in physicals.mapped(
            'transportation_generation_status'
        )):
            raise UserError(_(
                "Transportation options were already generated or are still in progress. "
                "Refresh the existing options instead of generating a duplicate set."
            ))
        self._prepare_transportation_generation_payload()
        physicals.sudo().write({
            'transportation_generation_status': 'pending',
            'transportation_error_code': False,
            'transportation_error_message': False,
        })
        _job, created = self._enqueue_physical_job('generate_transportation_options')
        return self.instance_id._notify(
            _("FBA Transportation Options"),
            _("Transportation option generation was queued.")
            if created else _("Transportation option generation is already queued."),
            'success' if created else 'warning',
        )

    def action_refresh_transportation_options(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._validate_transportation_preconditions()
        self._refresh_transportation_options()
        return self.instance_id._notify(
            _("FBA Transportation Options"),
            _("Transportation options were refreshed."),
            'success',
        )

    def action_confirm_transportation(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self.inbound_shipment_id._lock_phase3_workflow()
        physicals = self._accepted_plan_physicals()
        if any(code == 'WRITE_OUTCOME_UNKNOWN' for code in physicals.mapped(
            'transportation_error_code'
        )):
            raise UserError(_(
                "A previous transportation write has an unknown Amazon outcome. Reconcile the "
                "plan with Amazon before any resubmission."
            ))
        for physical in physicals:
            physical._validate_transportation_preconditions()
            option = physical.selected_transportation_option_id
            if not option or option.physical_shipment_id != physical:
                raise UserError(_(
                    "Select one transportation option for every Amazon physical shipment before "
                    "confirming the plan. Missing selection for %s.", physical.amazon_shipment_id,
                ))
            if option.valid_until and option.valid_until <= fields.Datetime.now():
                raise UserError(_(
                    "The transportation option for shipment %s has expired. Do not confirm it.",
                    physical.amazon_shipment_id,
                ))
            if option.requires_delivery_window and physical.delivery_window_confirmation_status != 'success':
                raise UserError(_(
                    "Confirm a delivery window for shipment %s before transportation.",
                    physical.amazon_shipment_id,
                ))
        if any(status in ('pending', 'in_progress', 'success') for status in physicals.mapped(
            'transportation_confirmation_status'
        )):
            raise UserError(_("Plan-level transportation confirmation is already queued or completed."))
        physicals.sudo().write({
            'transportation_confirmation_status': 'pending',
            'transportation_error_code': False,
            'transportation_error_message': False,
        })
        _job, created = self._enqueue_physical_job(
            'confirm_transportation_options', option=self.selected_transportation_option_id,
        )
        return self.instance_id._notify(
            _("FBA Transportation Confirmation"),
            _("Transportation confirmation was queued.")
            if created else _("Transportation confirmation is already queued."),
            'success' if created else 'warning',
        )

    def action_generate_delivery_window_options(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._lock_dispatch()
        self._validate_transportation_preconditions()
        if self.transportation_error_code == 'WRITE_OUTCOME_UNKNOWN':
            raise UserError(_("A previous Amazon write has an unknown outcome; reconcile it before retry."))
        if not self.delivery_window_required:
            raise UserError(_(
                "The selected transportation option does not require a confirmed delivery window."
            ))
        if self.delivery_window_generation_status in ('pending', 'in_progress', 'success'):
            raise UserError(_(
                "Delivery-window options were already generated or are still in progress. "
                "Refresh the existing options instead."
            ))
        if self.delivery_window_confirmation_status in ('pending', 'in_progress', 'success'):
            raise UserError(_("The delivery window is already queued or confirmed."))
        self.sudo().write({
            'delivery_window_generation_status': 'pending',
            'transportation_error_code': False,
            'transportation_error_message': False,
        })
        _job, created = self._enqueue_physical_job('generate_delivery_window_options')
        return self.instance_id._notify(
            _("FBA Delivery Windows"),
            _("Delivery-window generation was queued.") if created
            else _("Delivery-window generation is already queued."),
            'success' if created else 'warning',
        )

    def action_refresh_delivery_window_options(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._validate_transportation_preconditions()
        self._refresh_delivery_window_options()
        return self.instance_id._notify(
            _("FBA Delivery Windows"), _("Delivery-window options were refreshed."), 'success',
        )

    def action_confirm_delivery_window(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._lock_dispatch()
        if self.transportation_error_code == 'WRITE_OUTCOME_UNKNOWN':
            raise UserError(_("A previous Amazon write has an unknown outcome; reconcile it before retry."))
        option = self.selected_delivery_window_option_id
        if not self.delivery_window_required:
            raise UserError(_("The selected transportation option does not require a delivery window."))
        if not option or option.physical_shipment_id != self:
            raise UserError(_("Select one available delivery window before confirmation."))
        if option.availability_type == 'BLOCKED':
            raise UserError(_("A blocked delivery window cannot be confirmed."))
        if option.valid_until and option.valid_until <= fields.Datetime.now():
            raise UserError(_("The selected delivery window has expired. Generate fresh options."))
        if self.delivery_window_confirmation_status in ('pending', 'in_progress', 'success'):
            raise UserError(_("Delivery-window confirmation is already queued or completed."))
        self.sudo().write({
            'delivery_window_confirmation_status': 'pending',
            'transportation_error_code': False,
            'transportation_error_message': False,
        })
        _job, created = self._enqueue_physical_job('confirm_delivery_window_option')
        return self.instance_id._notify(
            _("FBA Delivery Window"),
            _("Delivery-window confirmation was queued.") if created
            else _("Delivery-window confirmation is already queued."),
            'success' if created else 'warning',
        )

    def _refresh_delivery_window_options(self):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        values = []
        token = None
        for _page in range(100):
            result = self.instance_id._api_call_safe(
                AmazonAPI().list_delivery_window_options,
                self.instance_id, access_token,
                self.inbound_shipment_id.inbound_plan_id, self.amazon_shipment_id,
                20, token,
                error_msg=_("Failed to list Amazon delivery-window options"),
            )
            if not isinstance(result, dict):
                result = {'unexpectedResponse': result}
            values.extend(result.get('deliveryWindowOptions') or [])
            token = (result.get('pagination') or {}).get('nextToken')
            if not token:
                break
        else:
            raise UserError(_("Amazon delivery-window pagination exceeded 100 pages."))
        return self._sync_delivery_window_options(values)

    def _sync_delivery_window_options(self, values):
        self.ensure_one()
        Option = self.env['amazon.fba.delivery.window.option'].sudo()
        seen = set()
        for raw in values:
            option_id = str((raw or {}).get('deliveryWindowOptionId') or '').strip()
            if not option_id:
                continue
            seen.add(option_id)
            vals = {
                'instance_id': self.instance_id.id,
                'inbound_shipment_id': self.inbound_shipment_id.id,
                'physical_shipment_id': self.id,
                'amazon_delivery_window_option_id': option_id,
                'start_date': self._parse_amazon_datetime(raw.get('startDate')),
                'end_date': self._parse_amazon_datetime(raw.get('endDate')),
                'valid_until': self._parse_amazon_datetime(raw.get('validUntil')),
                'availability_type': str(raw.get('availabilityType') or '').strip() or False,
                'raw_response': self.inbound_shipment_id._sanitized_json(raw),
            }
            existing = Option.search([
                ('physical_shipment_id', '=', self.id),
                ('amazon_delivery_window_option_id', '=', option_id),
            ], limit=1)
            (existing.write(vals) if existing else Option.create(vals))
        stale = self.delivery_window_option_ids.filtered(
            lambda option: option.amazon_delivery_window_option_id not in seen and not option.selected
        )
        stale.unlink()
        self.sudo().write({'transportation_last_sync_at': fields.Datetime.now()})
        return self.delivery_window_option_ids

    def _refresh_shipment_boxes(self):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        boxes = []
        token = None
        for _page in range(100):
            result = self.instance_id._api_call_safe(
                AmazonAPI().list_shipment_boxes,
                self.instance_id, access_token,
                self.inbound_shipment_id.inbound_plan_id, self.amazon_shipment_id,
                20, token,
                error_msg=_("Failed to list Amazon shipment boxes"),
            )
            if not isinstance(result, dict):
                result = {'unexpectedResponse': result}
            boxes.extend(result.get('boxes') or [])
            token = (result.get('pagination') or {}).get('nextToken')
            if not token:
                break
        else:
            raise UserError(_("Amazon shipment-box pagination exceeded 100 pages."))
        Box = self.env['amazon.fba.shipment.box'].sudo()
        seen = set()
        for raw in boxes:
            box_id = str((raw or {}).get('boxId') or '').strip()
            if not box_id:
                raise UserError(_("Amazon returned a shipment box without boxId."))
            seen.add(box_id)
            vals = {
                'instance_id': self.instance_id.id,
                'inbound_shipment_id': self.inbound_shipment_id.id,
                'physical_shipment_id': self.id,
                'amazon_box_id': box_id,
                'package_id': str(raw.get('packageId') or '').strip() or False,
                'quantity': int(raw.get('quantity') or 1),
                'raw_response': self.inbound_shipment_id._sanitized_json(raw),
            }
            existing = Box.search([
                ('physical_shipment_id', '=', self.id), ('amazon_box_id', '=', box_id),
            ], limit=1)
            (existing.write(vals) if existing else Box.create(vals))
        self.shipment_box_ids.filtered(
            lambda box: box.amazon_box_id not in seen
        ).unlink()
        if not self.shipment_box_ids:
            raise UserError(_("Amazon has not returned any box IDs for this physical shipment."))
        return self.shipment_box_ids

    def action_get_shipping_labels(self):
        """Retrieve official box IDs and durably store the Amazon label document."""
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        if self.transportation_confirmation_status != 'success':
            raise UserError(_("Confirm transportation successfully before requesting box labels."))
        if not (self.shipment_confirmation_id or '').strip():
            raise UserError(_("Amazon shipmentConfirmationId is required for shipping labels."))
        if self.labels_status == 'success' and self.shipping_label_attachment_id:
            return {
                'type': 'ir.actions.act_url',
                'url': '/web/content/%s?download=true' % self.shipping_label_attachment_id.id,
                'target': 'new',
            }
        boxes = self._refresh_shipment_boxes()
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_labels_v0,
            self.instance_id, access_token, self.shipment_confirmation_id.strip(),
            self.label_page_type, 'UNIQUE', len(boxes), boxes.mapped('amazon_box_id'),
            error_msg=_("Failed to retrieve Amazon inbound shipping labels"),
        )
        payload = result.get('payload') if isinstance(result, dict) else False
        url = str((payload or {}).get('DownloadURL') or '').strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme != 'https' or not parsed_url.netloc:
            self.sudo().write({'labels_status': 'failed', 'label_download_url': False})
            raise UserError(_("Amazon did not return a secure shipping-label download URL."))
        try:
            response = requests.get(url, timeout=(10, 60), allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            self.sudo().write({'labels_status': 'failed', 'label_download_url': False})
            raise UserError(_("Amazon shipping-label document download failed: %s", exc)) from exc
        final_url = urlparse(str(response.url or url))
        if final_url.scheme != 'https' or not final_url.netloc:
            self.sudo().write({'labels_status': 'failed', 'label_download_url': False})
            raise UserError(_("Amazon redirected the shipping-label download to an insecure URL."))
        content = response.content or b''
        if not content or len(content) > MAX_SHIPPING_LABEL_BYTES:
            self.sudo().write({'labels_status': 'failed', 'label_download_url': False})
            raise UserError(_(
                "Amazon returned an empty or oversized shipping-label document (maximum 25 MiB)."
            ))
        safe_reference = re.sub(
            r'[^A-Za-z0-9._-]+', '_', self.shipment_confirmation_id.strip()
        )
        if content.startswith(b'%PDF-'):
            extension = 'pdf'
            content_type = 'application/pdf'
        elif content.startswith(b'PK\x03\x04'):
            extension = 'zip'
            content_type = 'application/zip'
        else:
            self.sudo().write({'labels_status': 'failed', 'label_download_url': False})
            raise UserError(_("Amazon returned an unsupported shipping-label document format."))
        filename = 'amazon_fba_%s_box_labels.%s' % (safe_reference, extension)
        attachment_vals = {
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(content),
            'mimetype': content_type,
            'res_model': self._name,
            'res_id': self.id,
        }
        attachment = self.shipping_label_attachment_id.sudo()
        if attachment:
            attachment.write(attachment_vals)
        else:
            attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
        self.sudo().write({
            'labels_status': 'success',
            'label_download_url': False,
            'shipping_label_attachment_id': attachment.id,
            'shipping_label_filename': filename,
            'shipping_label_content_type': content_type,
            'labels_last_synced_at': fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'new',
        }

    def action_submit_tracking(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._lock_dispatch()
        if self.transportation_error_code == 'WRITE_OUTCOME_UNKNOWN':
            raise UserError(_("A previous Amazon write has an unknown outcome; reconcile it before retry."))
        if self.transportation_confirmation_status != 'success':
            raise UserError(_("Confirm transportation successfully before submitting tracking."))
        self._prepare_physical_tracking_details_payload()
        if self.tracking_status in ('pending', 'in_progress', 'success'):
            raise UserError(_("Tracking submission is already queued or completed."))
        self.sudo().write({'tracking_status': 'pending'})
        _job, created = self._enqueue_physical_job('submit_transportation_tracking')
        return self.instance_id._notify(
            _("FBA Tracking"),
            _("Tracking submission was queued.") if created else _("Tracking submission is already queued."),
            'success' if created else 'warning',
        )

    def action_refresh_physical_shipment_status(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        self._refresh_physical_shipment_status()
        return self.instance_id._notify(
            _("FBA Shipment Status"), _("Shipment status was refreshed."), 'success',
        )

    def _refresh_transportation_options(self):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        options = []
        token = None
        for _page in range(100):
            result = self.instance_id._api_call_safe(
                AmazonAPI().list_transportation_options,
                self.instance_id,
                access_token,
                self.inbound_shipment_id.inbound_plan_id,
                20,
                token,
                self.placement_option_id.amazon_placement_option_id,
                self.amazon_shipment_id,
                error_msg=_("Failed to list Amazon transportation options"),
            )
            if not isinstance(result, dict):
                result = {'unexpectedResponse': result}
            options.extend(result.get('transportationOptions') or [])
            self.sudo().write({
                'transportation_response': self._merge_transportation_response(
                    'listTransportationOptions', result,
                ),
                'transportation_last_sync_at': fields.Datetime.now(),
            })
            pagination = result.get('pagination') or {}
            token = pagination.get('nextToken') or pagination.get('paginationToken')
            if not token:
                break
        else:
            raise UserError(_("Amazon transportation option pagination exceeded 100 pages."))
        self._sync_transportation_options(options)
        return options

    def _sync_transportation_options(self, options):
        self.ensure_one()
        Option = self.env['amazon.fba.transportation.option'].sudo()
        synced = Option
        for raw in options:
            if not isinstance(raw, dict):
                continue
            shipment_id = str(raw.get('shipmentId') or '').strip()
            option_id = str(raw.get('transportationOptionId') or '').strip()
            if shipment_id != self.amazon_shipment_id or not option_id:
                continue
            carrier = raw.get('carrier') or {}
            quote = raw.get('quote') or {}
            cost = quote.get('cost') or {}
            appointment = raw.get('carrierAppointment') or {}
            shipping_solution = str(raw.get('shippingSolution') or '').strip() or False
            vals = {
                'instance_id': self.instance_id.id,
                'inbound_shipment_id': self.inbound_shipment_id.id,
                'physical_shipment_id': self.id,
                'amazon_transportation_option_id': option_id,
                'shipment_id': shipment_id,
                'shipping_mode': str(raw.get('shippingMode') or '').strip() or False,
                'shipping_solution': shipping_solution,
                'carrier_name': str(carrier.get('name') or '').strip() or False,
                'carrier_alpha_code': str(carrier.get('alphaCode') or '').strip() or False,
                'estimated_cost': cost.get('amount') or 0.0,
                'currency_id': self._currency_from_code(cost.get('code')),
                'valid_until': self._parse_amazon_datetime(quote.get('expiration')),
                'appointment_start': self._parse_amazon_datetime(appointment.get('startTime')),
                'appointment_end': self._parse_amazon_datetime(appointment.get('endTime')),
                'preconditions': json.dumps(raw.get('preconditions') or []),
                'requires_delivery_window': (
                    shipping_solution == 'USE_YOUR_OWN_CARRIER'
                    or 'CONFIRMED_DELIVERY_WINDOW' in (raw.get('preconditions') or [])
                ),
                'raw_response': self.inbound_shipment_id._sanitized_json(raw),
            }
            existing = Option.search([
                ('physical_shipment_id', '=', self.id),
                ('amazon_transportation_option_id', '=', option_id),
            ], limit=1)
            if existing:
                existing.write(vals)
                synced |= existing
            else:
                synced |= Option.create(vals)
        return synced

    def _currency_from_code(self, code):
        code = str(code or '').strip().upper()
        if not code:
            return False
        return self.env['res.currency'].sudo().search([('name', '=', code)], limit=1).id or False

    def _parse_amazon_datetime(self, value):
        return self.inbound_shipment_id._amazon_datetime(value)

    def _prepare_transportation_confirmation_payload(self):
        self.ensure_one()
        selections = []
        for physical in self._accepted_plan_physicals():
            option = physical.selected_transportation_option_id
            if not option:
                raise UserError(_("Every physical shipment must have a transportation selection."))
            selection = {
                'shipmentId': physical.amazon_shipment_id,
                'transportationOptionId': option.amazon_transportation_option_id,
            }
            contact = physical._prepare_contact_information()
            if contact:
                selection['contactInformation'] = contact
            selections.append(selection)
        return {'transportationSelections': selections}

    def _prepare_physical_tracking_details_payload(self):
        self.ensure_one()
        mode = (self.shipping_mode or self.selected_transportation_option_id.shipping_mode or '').upper()
        if 'SMALL_PARCEL' in mode:
            tracking_number = (self.tracking_number or '').strip()
            if not tracking_number:
                raise UserError(_("Enter the tracking number before submitting SPD tracking."))
            boxes = self.shipment_box_ids
            if not boxes:
                raise UserError(_("Refresh the official Amazon shipment boxes before submitting SPD tracking."))
            return {
                'trackingDetails': {
                    'spdTrackingDetail': {
                        'spdTrackingItems': [
                            {
                                'boxId': box.amazon_box_id.strip(),
                                'trackingId': tracking_number,
                            }
                            for box in boxes
                        ],
                    },
                },
            }
        freight_bill = (self.pro_number or self.tracking_number or '').strip()
        if not freight_bill:
            raise UserError(_("Enter the freight bill/PRO number before submitting LTL tracking."))
        detail = {'freightBillNumber': [freight_bill]}
        if (self.bill_of_lading_number or '').strip():
            detail['billOfLadingNumber'] = self.bill_of_lading_number.strip()
        return {'trackingDetails': {'ltlTrackingDetail': detail}}

    def _refresh_physical_shipment_status(self):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_shipment,
            self.instance_id,
            access_token,
            self.inbound_shipment_id.inbound_plan_id,
            self.amazon_shipment_id,
            error_msg=_("Failed to refresh Amazon physical shipment status"),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        if str(result.get('shipmentId') or '').strip() not in ('', self.amazon_shipment_id):
            raise ValidationError(_("Amazon returned a different physical shipment."))
        selected = result.get('selectedTransportationOptionId') or (
            (result.get('acceptedTransportationSelection') or {}).get('transportationOptionId')
            if isinstance(result.get('acceptedTransportationSelection'), dict) else False
        )
        vals = {
            'status': str(result.get('status') or '').strip() or self.status,
            'transportation_status': str(result.get('status') or '').strip() or self.transportation_status,
            'shipment_confirmation_id': str(
                result.get('shipmentConfirmationId') or ''
            ).strip() or self.shipment_confirmation_id,
            'transportation_last_sync_at': fields.Datetime.now(),
            'transportation_response': self._merge_transportation_response('getShipment', result),
        }
        if selected:
            option = self.transportation_option_ids.filtered(
                lambda item: item.amazon_transportation_option_id == selected
            )[:1]
            if option:
                vals['selected_transportation_option_id'] = option.id
                vals['shipping_mode'] = option.shipping_mode
                vals['carrier_name'] = option.carrier_name
                vals['carrier_code'] = option.carrier_alpha_code
                vals['carrier_type'] = (
                    'partnered' if option.shipping_solution == 'AMAZON_PARTNERED_CARRIER'
                    else 'non_partnered'
                )
                option.write({'selected': True})
        self.sudo().write(vals)
        return result

    def _operation_problem_values(self, problems):
        return self.inbound_shipment_id._operation_problem_values(problems)

    def _process_transportation_job(self, job):
        self.ensure_one()
        if job.operation_type == 'refresh_transportation_options':
            self._refresh_transportation_options()
            return 'success'

        if job.operation_type == 'generate_delivery_window_options':
            if not job.operation_id:
                access_token = self.instance_id._get_access_token_or_raise()
                result = self.instance_id._api_call_safe(
                    AmazonAPI().generate_delivery_window_options,
                    self.instance_id, access_token, self.inbound_shipment_id.inbound_plan_id,
                    self.amazon_shipment_id,
                    error_msg=_("Failed to generate Amazon delivery-window options"),
                )
                if not isinstance(result, dict):
                    result = {'unexpectedResponse': result}
                operation_id = str(result.get('operationId') or '').strip()
                self.sudo().write({
                    'delivery_window_generation_operation_id': operation_id or False,
                    'delivery_window_generation_status': 'in_progress',
                    'transportation_last_sync_at': fields.Datetime.now(),
                })
                job.sudo().write({
                    'operation_id': operation_id or False,
                    'response_data': self.inbound_shipment_id._sanitized_json(result),
                })
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise UserError(_("Amazon did not return a valid delivery-window operationId."))
                return 'in_progress'
            return self._poll_delivery_window_operation(
                job, 'delivery_window_generation_status', refresh_options=True,
            )

        if job.operation_type == 'confirm_delivery_window_option':
            if not job.operation_id:
                option = self.selected_delivery_window_option_id
                if not option:
                    raise UserError(_("Select one delivery window before confirmation."))
                access_token = self.instance_id._get_access_token_or_raise()
                result = self.instance_id._api_call_safe(
                    AmazonAPI().confirm_delivery_window_option,
                    self.instance_id, access_token, self.inbound_shipment_id.inbound_plan_id,
                    self.amazon_shipment_id, option.amazon_delivery_window_option_id,
                    error_msg=_("Failed to confirm Amazon delivery window"),
                )
                if not isinstance(result, dict):
                    result = {'unexpectedResponse': result}
                operation_id = str(result.get('operationId') or '').strip()
                self.sudo().write({
                    'delivery_window_confirmation_operation_id': operation_id or False,
                    'delivery_window_confirmation_status': 'in_progress',
                    'transportation_last_sync_at': fields.Datetime.now(),
                })
                job.sudo().write({
                    'operation_id': operation_id or False,
                    'response_data': self.inbound_shipment_id._sanitized_json(result),
                })
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise UserError(_("Amazon did not return a valid delivery-window confirmation operationId."))
                return 'in_progress'
            return self._poll_delivery_window_operation(
                job, 'delivery_window_confirmation_status', refresh_options=False,
            )

        if job.operation_type == 'generate_transportation_options':
            if not job.operation_id:
                body = self._prepare_transportation_generation_payload()
                access_token = self.instance_id._get_access_token_or_raise()
                result = self.instance_id._api_call_safe(
                    AmazonAPI().generate_transportation_options,
                    self.instance_id,
                    access_token,
                    self.inbound_shipment_id.inbound_plan_id,
                    body,
                    error_msg=_("Failed to generate Amazon transportation options"),
                )
                if not isinstance(result, dict):
                    result = {'unexpectedResponse': result}
                operation_id = str(result.get('operationId') or '').strip()
                self._accepted_plan_physicals().sudo().write({
                    'transportation_generation_operation_id': operation_id or False,
                    'transportation_generation_status': 'in_progress',
                    'transportation_response': self._merge_transportation_response(
                        'generateTransportationOptions', result,
                    ),
                    'transportation_last_sync_at': fields.Datetime.now(),
                })
                job.sudo().write({
                    'operation_id': operation_id or False,
                    'response_data': self.inbound_shipment_id._sanitized_json(result),
                    'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
                })
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise UserError(_("Amazon did not return a valid transportation operationId."))
                return 'in_progress'
            return self._poll_transportation_operation(
                job, 'transportation_generation_status', refresh_options=True,
            )

        if job.operation_type == 'confirm_transportation_options':
            if not job.operation_id:
                body = self._prepare_transportation_confirmation_payload()
                access_token = self.instance_id._get_access_token_or_raise()
                result = self.instance_id._api_call_safe(
                    AmazonAPI().confirm_transportation_options,
                    self.instance_id,
                    access_token,
                    self.inbound_shipment_id.inbound_plan_id,
                    body,
                    error_msg=_("Failed to confirm Amazon transportation option"),
                )
                if not isinstance(result, dict):
                    result = {'unexpectedResponse': result}
                operation_id = str(result.get('operationId') or '').strip()
                self._accepted_plan_physicals().sudo().write({
                    'transportation_confirmation_operation_id': operation_id or False,
                    'transportation_confirmation_status': 'in_progress',
                    'transportation_response': self._merge_transportation_response(
                        'confirmTransportationOptions', result,
                    ),
                    'transportation_last_sync_at': fields.Datetime.now(),
                })
                job.sudo().write({
                    'operation_id': operation_id or False,
                    'response_data': self.inbound_shipment_id._sanitized_json(result),
                    'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
                })
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise UserError(_("Amazon did not return a valid transportation confirmation operationId."))
                return 'in_progress'
            return self._poll_transportation_operation(
                job, 'transportation_confirmation_status', refresh_shipment=True,
            )

        if job.operation_type == 'submit_transportation_tracking':
            if not job.operation_id:
                body = self._prepare_physical_tracking_details_payload()
                access_token = self.instance_id._get_access_token_or_raise()
                result = self.instance_id._api_call_safe(
                    AmazonAPI().update_shipment_tracking_details,
                    self.instance_id,
                    access_token,
                    self.inbound_shipment_id.inbound_plan_id,
                    self.amazon_shipment_id,
                    body,
                    error_msg=_("Failed to update Amazon inbound shipment tracking"),
                )
                if not isinstance(result, dict):
                    result = {'unexpectedResponse': result}
                operation_id = str(result.get('operationId') or '').strip()
                self.sudo().write({
                    'tracking_operation_id': operation_id or False,
                    'tracking_status': 'in_progress',
                    'transportation_response': self._merge_transportation_response(
                        'updateShipmentTrackingDetails', result,
                    ),
                    'tracking_last_synced_at': fields.Datetime.now(),
                })
                job.sudo().write({
                    'operation_id': operation_id or False,
                    'response_data': self.inbound_shipment_id._sanitized_json(result),
                    'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
                })
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise UserError(_("Amazon did not return a valid tracking update operationId."))
                return 'in_progress'
            return self._poll_transportation_operation(
                job, 'tracking_status', refresh_shipment=True,
            )
        raise UserError(_("Unsupported physical shipment operation: %s", job.operation_type))

    def _poll_delivery_window_operation(self, job, status_field, refresh_options=False):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_operation_status,
            self.instance_id, access_token, job.operation_id,
            error_msg=_("Failed to poll Amazon delivery-window operation"),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        raw_status = str(result.get('operationStatus') or '').strip()
        normalized = raw_status.upper().replace('-', '_').replace(' ', '_')
        error_code, error_message = self._operation_problem_values(
            result.get('operationProblems')
        )
        job.sudo().write({
            'raw_operation_status': raw_status or False,
            'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
            'response_data': self.inbound_shipment_id._sanitized_json(result),
        })
        if normalized == 'SUCCESS':
            self.sudo().write({
                status_field: 'success',
                'transportation_error_code': False,
                'transportation_error_message': False,
            })
            if refresh_options:
                self._refresh_delivery_window_options()
            return 'success'
        if normalized == 'FAILED':
            self.sudo().write({
                status_field: 'failed',
                'transportation_error_code': error_code or 'AMAZON_OPERATION_FAILED',
                'transportation_error_message': error_message or _(
                    "Amazon reported that the delivery-window operation failed."
                ),
            })
            return 'failed'
        self.sudo().write({
            status_field: 'pending' if normalized in ('PENDING', 'QUEUED') else 'in_progress',
            'transportation_error_code': error_code,
            'transportation_error_message': error_message,
        })
        return 'in_progress'

    def _poll_transportation_operation(self, job, status_field, refresh_options=False,
                                       refresh_shipment=False):
        self.ensure_one()
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_operation_status,
            self.instance_id,
            access_token,
            job.operation_id,
            error_msg=_("Failed to poll Amazon inbound operation"),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        raw_status = str(result.get('operationStatus') or '').strip()
        normalized = raw_status.upper().replace('-', '_').replace(' ', '_')
        error_code, error_message = self._operation_problem_values(
            result.get('operationProblems')
        )
        targets = (
            self._accepted_plan_physicals()
            if job.operation_type in ('generate_transportation_options', 'confirm_transportation_options')
            else self
        )
        for target in targets:
            target.sudo().write({
                'transportation_response': target._merge_transportation_response(
                    'getInboundOperationStatus:%s' % job.operation_type, result,
                ),
                'transportation_last_sync_at': fields.Datetime.now(),
            })
        job.sudo().write({
            'raw_operation_status': raw_status or False,
            'amazon_request_id': str(result.get('_amazon_request_id') or '').strip() or False,
            'response_data': self.inbound_shipment_id._sanitized_json(result),
        })
        if normalized == 'SUCCESS':
            vals = {
                status_field: 'success',
                'transportation_error_code': False,
                'transportation_error_message': False,
            }
            targets.sudo().write(vals)
            if refresh_options:
                for target in targets:
                    target._refresh_transportation_options()
            if refresh_shipment:
                for target in targets:
                    target._refresh_physical_shipment_status()
            return 'success'
        if normalized == 'FAILED':
            targets.sudo().write({
                status_field: 'failed',
                'transportation_error_code': error_code or 'AMAZON_OPERATION_FAILED',
                'transportation_error_message': error_message or _(
                    "Amazon reported that the operation failed."
                ),
            })
            return 'failed'
        if normalized == 'IN_PROGRESS':
            targets.sudo().write({
                status_field: 'in_progress',
                'transportation_error_code': error_code,
                'transportation_error_message': error_message,
            })
            return 'in_progress'
        targets.sudo().write({
            'transportation_status': raw_status or False,
            'transportation_error_code': error_code,
            'transportation_error_message': error_message,
        })
        return 'in_progress'

    def _prepare_dispatch_move_commands(self, source, transit):
        self.ensure_one()
        commands = []
        errors = []
        for position, line in enumerate(self.line_ids, start=1):
            line_errors = []
            amazon_product = line.amazon_product_id
            product = amazon_product.odoo_product_id
            if not amazon_product or not product:
                errors.append(_("Line %s (%s) has no mapped Odoo product.", position, line.msku))
                continue
            if amazon_product.instance_id != self.instance_id:
                line_errors.append(_("Line %s product mapping belongs to another Amazon instance.", position))
            if (amazon_product.sku or '').strip() != (line.msku or '').strip():
                line_errors.append(_("Line %s MSKU no longer matches its Amazon Product mapping.", position))
            if product.company_id and product.company_id != self.company_id:
                line_errors.append(_("Line %s product belongs to another company.", position))
            if product.type != 'consu' or not product.is_storable:
                line_errors.append(_("Line %s product must be an inventory-tracked Goods product.", position))
            if line.quantity <= 0:
                line_errors.append(_("Line %s must have a positive final shipment quantity.", position))
            if line_errors:
                errors.extend(line_errors)
                continue
            commands.append(Command.create({
                'product_id': product.id,
                'product_uom_qty': line.quantity,
                'product_uom': product.uom_id.id,
                'location_id': source.id,
                'location_dest_id': transit.id,
                'company_id': self.company_id.id,
                'origin': self.shipment_confirmation_id,
            }))
        if errors:
            raise UserError(_("Cannot create the FBA dispatch picking:\n%s", "\n".join(errors)))
        return commands

    def _expected_product_quantities(self):
        self.ensure_one()
        quantities = {}
        for line in self.line_ids:
            product = line.amazon_product_id.odoo_product_id
            if product:
                quantities[product.id] = quantities.get(product.id, 0) + line.quantity
        return quantities

    def _link_compatible_legacy_picking(self, picking):
        self.ensure_one()
        picking.ensure_one()
        source, transit = self._validate_dispatch_preconditions()
        actual = {}
        for move in picking.move_ids.filtered(lambda move: move.state != 'cancel'):
            actual[move.product_id.id] = actual.get(move.product_id.id, 0) + move.product_uom._compute_quantity(
                move.product_uom_qty, move.product_id.uom_id,
            )
        if (
            picking.company_id != self.company_id
            or picking.location_id != source
            or picking.location_dest_id != transit
            or actual != self._expected_product_quantities()
        ):
            raise UserError(_(
                "The existing aggregate picking does not match this physical shipment's final "
                "Amazon quantity distribution and cannot be linked safely."
            ))
        picking.write({
            'amazon_fba_physical_shipment_id': self.id,
            'amazon_fba_movement_type': 'outbound_to_transit',
        })
        self._sync_dispatch_state_from_picking(picking)

    def _sync_dispatch_state_from_picking(self, picking=False):
        for physical in self:
            active = picking if (
                picking
                and picking.amazon_fba_physical_shipment_id == physical
                and picking.amazon_fba_movement_type == 'outbound_to_transit'
            ) else (
                physical.picking_ids.filtered(
                    lambda item: item.state != 'cancel'
                    and item.amazon_fba_movement_type == 'outbound_to_transit'
                )[:1]
            )
            vals = {}
            if not active:
                vals['dispatch_state'] = 'placement_confirmed'
            elif active.state == 'done':
                vals.update({
                    'dispatch_state': 'dispatched',
                    'dispatch_date': active.date_done or fields.Datetime.now(),
                })
            elif active.state == 'assigned' and all(
                move.product_uom.compare(move.quantity, move.product_uom_qty) >= 0
                for move in active.move_ids.filtered(lambda move: move.state != 'cancel')
            ):
                vals['dispatch_state'] = 'ready_to_dispatch'
            else:
                vals['dispatch_state'] = 'picking_created'
            physical.sudo().write(vals)
        self.mapped('inbound_shipment_id')._update_dispatch_state()

    def _create_dispatch_picking(self):
        self.ensure_one()
        self._lock_dispatch()
        active = self.picking_ids.filtered(
            lambda picking: picking.state != 'cancel'
            and picking.amazon_fba_movement_type == 'outbound_to_transit'
        )
        if active:
            if len(active) > 1:
                raise UserError(_("More than one active dispatch picking is linked to this shipment."))
            return active, False
        source, transit = self._validate_dispatch_preconditions()
        picking_type = self.inbound_shipment_id._get_internal_picking_type(source)
        picking = self.env['stock.picking'].with_company(self.company_id).create({
            'picking_type_id': picking_type.id,
            'location_id': source.id,
            'location_dest_id': transit.id,
            'company_id': self.company_id.id,
            'origin': "%s / %s" % (
                self.inbound_shipment_id.name, self.shipment_confirmation_id,
            ),
            'amazon_instance_id': self.instance_id.id,
            'amazon_inbound_shipment_id': self.inbound_shipment_id.id,
            'amazon_fba_physical_shipment_id': self.id,
            'amazon_fba_movement_type': 'outbound_to_transit',
            'move_type': 'one',
            'move_ids': self._prepare_dispatch_move_commands(source, transit),
        })
        picking.action_confirm()
        picking.action_assign()
        self._sync_dispatch_state_from_picking(picking)
        return picking, True

    def action_create_dispatch_picking(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        picking, created = self._create_dispatch_picking()
        if not created:
            return self.action_open_dispatch_picking()
        if picking.state != 'assigned':
            return self.instance_id._notify(
                _("FBA Dispatch Picking"),
                _(
                    "Dispatch picking %s was created, but stock is not fully reserved. "
                    "Replenish stock and use Check Availability before validation.", picking.name,
                ),
                'warning',
            )
        return self.instance_id._notify(
            _("FBA Dispatch Picking"),
            _("Dispatch picking %s was created and reserved.", picking.name),
            'success',
        )

    def action_open_dispatch_picking(self):
        self.ensure_one()
        self.inbound_shipment_id._check_inbound_manager_access()
        picking = self.picking_id
        if not picking:
            raise UserError(_("No active dispatch picking is linked to this physical shipment."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("FBA Dispatch Picking"),
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': picking.id,
            'context': {'create': False},
        }


class StockPickingAmazonInbound(models.Model):
    _inherit = 'stock.picking'

    amazon_inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', string='Amazon Inbound Shipment', copy=False,
        ondelete='restrict', index=True, check_company=True,
    )
    amazon_fba_physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', string='Amazon Physical Shipment', copy=False,
        ondelete='restrict', index=True, check_company=True,
    )
    amazon_shipment_id = fields.Char(
        related='amazon_fba_physical_shipment_id.amazon_shipment_id',
        string='Amazon Shipment ID', readonly=True,
    )
    amazon_shipment_confirmation_id = fields.Char(
        related='amazon_fba_physical_shipment_id.shipment_confirmation_id',
        string='Amazon Shipment Confirmation ID', readonly=True,
    )

    _unique_active_physical_dispatch = models.UniqueIndex(
        '(amazon_fba_physical_shipment_id) WHERE '
        "amazon_fba_physical_shipment_id IS NOT NULL "
        "AND amazon_fba_movement_type = 'outbound_to_transit' AND state != 'cancel'",
        'Only one active dispatch picking can be linked to an Amazon physical shipment.',
    )

    def action_assign(self):
        result = super().action_assign()
        dispatch_pickings = self.filtered(
            lambda picking: picking.amazon_fba_physical_shipment_id
            and picking.amazon_fba_movement_type == 'outbound_to_transit'
        )
        if dispatch_pickings:
            dispatch_pickings.mapped(
                'amazon_fba_physical_shipment_id'
            )._sync_dispatch_state_from_picking()
        return result

    def action_cancel(self):
        physical_shipments = self.mapped('amazon_fba_physical_shipment_id')
        result = super().action_cancel()
        if physical_shipments:
            physical_shipments._sync_dispatch_state_from_picking()
        return result

    def button_validate(self):
        dispatch_pickings = self.filtered(
            lambda picking: picking.amazon_fba_physical_shipment_id
            and picking.amazon_fba_movement_type == 'outbound_to_transit'
            and picking.state not in ('done', 'cancel')
        )
        for picking in dispatch_pickings:
            physical = picking.amazon_fba_physical_shipment_id
            if physical.transportation_confirmation_status != 'success':
                raise UserError(_(
                    "Confirm Amazon transportation before dispatching picking %s.", picking.name,
                ))
            if physical.delivery_window_required and (
                physical.delivery_window_confirmation_status != 'success'
            ):
                raise UserError(_(
                    "Confirm the required Amazon delivery window before dispatching picking %s.",
                    picking.name,
                ))
            if physical.labels_status != 'success' or not physical.shipping_label_attachment_id:
                raise UserError(_(
                    "Retrieve and print the Amazon box/shipping labels before dispatching picking %s.",
                    picking.name,
                ))
            if physical.product_labels_required and not physical.product_labels_confirmed:
                raise UserError(_(
                    "Confirm that seller-applied product/FNSKU labels are printed and applied before "
                    "dispatching picking %s.", picking.name,
                ))
            option = physical.selected_transportation_option_id
            if option.shipping_solution == 'USE_YOUR_OWN_CARRIER' and physical.tracking_status != 'success':
                raise UserError(_(
                    "Submit seller-arranged carrier tracking successfully before dispatching picking %s.",
                    picking.name,
                ))
            incomplete = picking.move_ids.filtered(
                lambda move: move.state != 'cancel'
                and move.product_uom.compare(move.quantity, move.product_uom_qty) < 0
            )
            if incomplete:
                raise UserError(_(
                    "Dispatch picking %s cannot be validated until its full Amazon shipment "
                    "quantity is available and reserved.", picking.name,
                ))
        result = super().button_validate()
        completed = dispatch_pickings.filtered(lambda picking: picking.state == 'done')
        if completed:
            physicals = completed.mapped('amazon_fba_physical_shipment_id')
            physicals._sync_dispatch_state_from_picking()
            for inbound in physicals.mapped('inbound_shipment_id'):
                dispatched = inbound.physical_shipment_ids.filtered(
                    lambda physical: physical.dispatch_state == 'dispatched'
                )
                for line in inbound.line_ids:
                    line.sudo().write({
                        'quantity_shipped': sum(
                            item.quantity for item in dispatched.line_ids
                            if item.msku == line.sku
                        ),
                    })
        return result


class AmazonFbaDeliveryWindowOption(models.Model):
    _name = 'amazon.fba.delivery.window.option'
    _description = 'Amazon FBA Delivery Window Option'
    _order = 'start_date, id'
    _check_company_auto = True

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    amazon_delivery_window_option_id = fields.Char(required=True, copy=False, index=True)
    start_date = fields.Datetime(required=True, copy=False)
    end_date = fields.Datetime(required=True, copy=False)
    valid_until = fields.Datetime(required=True, copy=False)
    availability_type = fields.Char(required=True, copy=False, index=True)
    selected = fields.Boolean(copy=False, index=True)
    raw_response = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_physical_window = models.Constraint(
        'UNIQUE (physical_shipment_id, amazon_delivery_window_option_id)',
        'A delivery window can occur only once per physical Amazon shipment.',
    )
    _single_selected_window = models.UniqueIndex(
        '(physical_shipment_id) WHERE selected IS TRUE',
        'Only one delivery window can be selected per physical Amazon shipment.',
    )

    def action_select_delivery_window(self):
        self.ensure_one()
        physical = self.physical_shipment_id
        physical.inbound_shipment_id._check_inbound_manager_access()
        if physical.delivery_window_confirmation_status in ('pending', 'in_progress', 'success'):
            raise UserError(_("The delivery window is already queued or confirmed."))
        if self.availability_type == 'BLOCKED':
            raise UserError(_("A blocked delivery window cannot be selected."))
        if self.valid_until <= fields.Datetime.now():
            raise UserError(_("This delivery window has expired."))
        (physical.delivery_window_option_ids - self).sudo().write({'selected': False})
        self.sudo().write({'selected': True})
        physical.sudo().write({'selected_delivery_window_option_id': self.id})
        return physical.instance_id._notify(
            _("FBA Delivery Window"),
            _("Delivery window selected locally. Confirm it separately to send it to Amazon."),
            'success',
        )


class AmazonFbaShipmentBox(models.Model):
    _name = 'amazon.fba.shipment.box'
    _description = 'Amazon FBA Physical Shipment Box'
    _order = 'physical_shipment_id, amazon_box_id, id'
    _check_company_auto = True

    instance_id = fields.Many2one('amazon.instance', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    amazon_box_id = fields.Char(required=True, copy=False, index=True)
    package_id = fields.Char(copy=False, index=True)
    quantity = fields.Integer(default=1, required=True, copy=False)
    raw_response = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_physical_box = models.Constraint(
        'UNIQUE (physical_shipment_id, amazon_box_id)',
        'An Amazon box can occur only once per physical shipment.',
    )


class AmazonFbaTransportationOption(models.Model):
    _name = 'amazon.fba.transportation.option'
    _description = 'Amazon FBA Transportation Option'
    _order = 'physical_shipment_id, amazon_transportation_option_id, id'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    amazon_transportation_option_id = fields.Char(required=True, copy=False, index=True)
    shipment_id = fields.Char(required=True, copy=False, index=True)
    shipping_mode = fields.Char(copy=False, index=True)
    shipping_solution = fields.Char(copy=False, index=True)
    carrier_name = fields.Char(copy=False)
    carrier_alpha_code = fields.Char(copy=False)
    estimated_cost = fields.Monetary(currency_field='currency_id', copy=False)
    currency_id = fields.Many2one('res.currency', ondelete='restrict')
    appointment_start = fields.Datetime(copy=False)
    appointment_end = fields.Datetime(copy=False)
    valid_until = fields.Datetime(copy=False)
    preconditions = fields.Text(copy=False)
    requires_delivery_window = fields.Boolean(copy=False, readonly=True, index=True)
    status = fields.Char(copy=False, index=True)
    selected = fields.Boolean(copy=False, index=True)
    raw_response = fields.Text(
        copy=False, readonly=True, groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_physical_option = models.Constraint(
        'UNIQUE (physical_shipment_id, amazon_transportation_option_id)',
        'A transportation option can occur only once per physical Amazon shipment.',
    )

    @api.constrains('inbound_shipment_id', 'physical_shipment_id', 'shipment_id')
    def _check_physical_scope(self):
        for option in self:
            if option.physical_shipment_id.inbound_shipment_id != option.inbound_shipment_id:
                raise ValidationError(_("The transportation option must belong to the same inbound plan."))
            if option.physical_shipment_id.amazon_shipment_id != option.shipment_id:
                raise ValidationError(_("The transportation option shipmentId must match the physical shipment."))

    def action_select_transportation_option(self):
        self.ensure_one()
        physical = self.physical_shipment_id
        physical.inbound_shipment_id._check_inbound_manager_access()
        if any(status in ('pending', 'in_progress', 'success') for status in physical._accepted_plan_physicals().mapped(
            'transportation_confirmation_status'
        )):
            raise UserError(_("Plan-level transportation is already queued or confirmed."))
        if self.valid_until and self.valid_until <= fields.Datetime.now():
            raise UserError(_("This transportation option has expired."))
        (physical.transportation_option_ids - self).sudo().write({'selected': False})
        self.sudo().write({'selected': True})
        physical.sudo().write({
            'selected_transportation_option_id': self.id,
            'shipping_mode': self.shipping_mode,
            'carrier_name': self.carrier_name,
            'carrier_code': self.carrier_alpha_code,
            'carrier_type': (
                'partnered' if self.shipping_solution == 'AMAZON_PARTNERED_CARRIER'
                else 'non_partnered'
            ),
            'delivery_window_required': self.requires_delivery_window,
        })
        return physical.instance_id._notify(
            _("FBA Transportation Option"),
            _("Transportation option was selected locally. Confirm it separately to send it to Amazon."),
            'success',
        )


class AmazonInboundOperationJobShipping(models.Model):
    _inherit = 'amazon.inbound.operation.job'

    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', ondelete='cascade', check_company=True,
    )
    transportation_option_id = fields.Many2one(
        'amazon.fba.transportation.option', ondelete='set null', check_company=True,
    )

    operation_type = fields.Selection(selection_add=[
        ('confirm_shipment', 'Confirm Shipment / Update Tracking'),
        ('refresh_shipment_status', 'Refresh Shipment Status'),
        ('generate_transportation_options', 'Generate Transportation Options'),
        ('refresh_transportation_options', 'Refresh Transportation Options'),
        ('confirm_transportation_options', 'Confirm Transportation Options'),
        ('submit_transportation_tracking', 'Submit Transportation Tracking'),
        ('generate_delivery_window_options', 'Generate Delivery Window Options'),
        ('confirm_delivery_window_option', 'Confirm Delivery Window Option'),
    ], ondelete={
        'confirm_shipment': 'cascade',
        'refresh_shipment_status': 'cascade',
        'generate_transportation_options': 'cascade',
        'refresh_transportation_options': 'cascade',
        'confirm_transportation_options': 'cascade',
        'submit_transportation_tracking': 'cascade',
        'generate_delivery_window_options': 'cascade',
        'confirm_delivery_window_option': 'cascade',
    })

    def _process_operation(self):
        self.ensure_one()
        if self.operation_type in (
            'generate_transportation_options', 'refresh_transportation_options',
            'confirm_transportation_options', 'submit_transportation_tracking',
            'generate_delivery_window_options', 'confirm_delivery_window_option',
        ):
            return self._process_physical_transportation_operation()
        if self.operation_type not in ('confirm_shipment', 'refresh_shipment_status'):
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
            if self.operation_type == 'refresh_shipment_status':
                result = shipment._refresh_shipment_status()
                self.write({'response_data': shipment._sanitized_json(result)})
                self._mark_done()
                return True
            status = shipment._process_shipment_confirmation_job(self)
            if status == 'success':
                self._mark_done()
                return True
            if status == 'failed':
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'next_run_at': False,
                    'last_error': shipment.shipment_error_message
                    or _("Amazon shipment confirmation failed."),
                })
                return False
            self._schedule_retry()
            return False
        except Exception as exc:
            message = str(exc)
            _logger.warning(
                "Amazon Phase 4 job %s (%s) failed: %s",
                self.id, self.operation_type, message,
            )
            shipment.write({
                'shipment_error_code': 'BACKGROUND_JOB_FAILED',
                'shipment_error_message': message,
                'shipment_last_refresh_at': fields.Datetime.now(),
            })
            self._schedule_retry(error_message=message)
            if self.state == 'failed' and self.operation_type == 'confirm_shipment':
                shipment.write({'shipment_confirmation_status': 'failed'})
            return False

    def _process_physical_transportation_operation(self):
        self.ensure_one()
        if self.state in ('done', 'failed'):
            return False
        physical = self.physical_shipment_id.sudo()
        if not physical:
            self.write({'state': 'failed', 'last_error': _("Missing physical shipment.")})
            return False
        now = fields.Datetime.now()
        vals = {'state': 'in_progress', 'next_run_at': False}
        if not self.started_at:
            vals['started_at'] = now
        self.write(vals)
        try:
            status = physical._process_transportation_job(self)
            if status == 'success':
                self._mark_done()
                return True
            if status == 'failed':
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'next_run_at': False,
                    'last_error': physical.transportation_error_message
                    or _("Amazon transportation operation failed."),
                })
                return False
            self._schedule_retry()
            return False
        except Exception as exc:
            message = str(exc)
            _logger.warning(
                "Amazon physical shipment job %s (%s) failed: %s",
                self.id, self.operation_type, message,
            )
            write_start = self.operation_type in (
                'generate_transportation_options', 'confirm_transportation_options',
                'submit_transportation_tracking', 'generate_delivery_window_options',
                'confirm_delivery_window_option',
            ) and not self.operation_id
            cause = getattr(exc, '__cause__', None)
            response = getattr(cause, 'response', None)
            status_code = getattr(response, 'status_code', None)
            ambiguous = write_start and (status_code is None or status_code >= 500)
            error_targets = (
                physical._accepted_plan_physicals()
                if self.operation_type in (
                    'generate_transportation_options', 'confirm_transportation_options',
                ) else physical
            )
            error_targets.write({
                'transportation_error_code': (
                    'WRITE_OUTCOME_UNKNOWN' if ambiguous else 'BACKGROUND_JOB_FAILED'
                ),
                'transportation_error_message': message,
                'transportation_last_sync_at': fields.Datetime.now(),
            })
            if write_start:
                self.write({
                    'state': 'failed',
                    'finished_at': fields.Datetime.now(),
                    'next_run_at': False,
                    'last_error': message,
                })
            else:
                self._schedule_retry(error_message=message)
            if self.state == 'failed':
                if self.operation_type == 'generate_transportation_options':
                    error_targets.write({'transportation_generation_status': 'failed'})
                elif self.operation_type == 'confirm_transportation_options':
                    error_targets.write({'transportation_confirmation_status': 'failed'})
                elif self.operation_type == 'submit_transportation_tracking':
                    physical.write({'tracking_status': 'failed'})
                elif self.operation_type == 'generate_delivery_window_options':
                    physical.write({'delivery_window_generation_status': 'failed'})
                elif self.operation_type == 'confirm_delivery_window_option':
                    physical.write({'delivery_window_confirmation_status': 'failed'})
            return False
