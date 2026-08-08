import json
import logging
import re

from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .amazon_api import AmazonAPI
from .amazon_inbound_shipment import INBOUND_PLAN_ID_RE, OPERATION_ID_RE

_logger = logging.getLogger(__name__)

AMAZON_SHIPMENT_ID_RE = INBOUND_PLAN_ID_RE
AMAZON_FREIGHT_BILL_RE = re.compile(r'^[A-Za-z0-9._ -]{1,64}$')
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
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase4_workflow()
        if self.state in ('shipment_confirmed', 'waiting_receiving') and (
            self.shipment_confirmation_status in ('pending', 'in_progress', 'success')
        ):
            raise UserError(_("This shipment confirmation is already queued or completed."))
        retry = self.state == 'shipment_confirmed' and self.shipment_confirmation_status == 'failed'
        if self.state != 'ready_to_ship' and not retry:
            raise UserError(_("The shipment must be fully reserved and Ready to Ship first."))
        self._validate_shipment_confirmation_inputs()
        pickings = self._validate_reserved_pickings()
        if not retry:
            for picking in pickings.filtered(lambda record: record.state != 'done'):
                result = picking.with_context(
                    picking_ids_not_to_backorder=picking.ids,
                ).button_validate()
                if isinstance(result, dict) or picking.state != 'done':
                    raise UserError(_(
                        "Picking %s requires manual stock details and was not completed.", picking.name
                    ))
            self.line_ids.sudo().write({'quantity_shipped': 0.0})
            for line in self.line_ids:
                line.sudo().write({'quantity_shipped': line.planned_quantity})
        self.sudo().write({
            'state': 'shipment_confirmed',
            'shipment_confirmation_status': 'pending',
            'shipment_confirmation_operation_id': False,
            'shipment_error_code': False,
            'shipment_error_message': False,
        })
        _job, created = self._enqueue_shipment_confirmation_job()
        return self.instance_id._notify(
            _("FBA Shipment Confirmation"),
            _("Shipment confirmation was queued; inventory is now in Amazon Transit.")
            if created else _("Shipment confirmation is already queued."),
            'success' if created else 'warning',
        )

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
                self.shipment_id,
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

    @api.depends('picking_ids.state')
    def _compute_dispatch_picking(self):
        for physical in self:
            picking = physical.picking_ids.filtered(
                lambda picking: picking.state != 'cancel'
            )[:1]
            physical.picking_id = picking
            physical.picking_state = picking.state or False

    @api.depends('line_ids.quantity')
    def _compute_dispatch_quantity(self):
        for physical in self:
            physical.dispatch_quantity = sum(physical.line_ids.mapped('quantity'))

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
        picking.write({'amazon_fba_physical_shipment_id': self.id})
        self._sync_dispatch_state_from_picking(picking)

    def _sync_dispatch_state_from_picking(self, picking=False):
        for physical in self:
            active = picking if picking and picking.amazon_fba_physical_shipment_id == physical else (
                physical.picking_ids.filtered(lambda item: item.state != 'cancel')[:1]
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
        active = self.picking_ids.filtered(lambda picking: picking.state != 'cancel')
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
        "amazon_fba_physical_shipment_id IS NOT NULL AND state != 'cancel'",
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
            completed.mapped('amazon_fba_physical_shipment_id')._sync_dispatch_state_from_picking()
        return result


class AmazonInboundOperationJobShipping(models.Model):
    _inherit = 'amazon.inbound.operation.job'

    operation_type = fields.Selection(selection_add=[
        ('confirm_shipment', 'Confirm Shipment / Update Tracking'),
        ('refresh_shipment_status', 'Refresh Shipment Status'),
    ], ondelete={
        'confirm_shipment': 'cascade',
        'refresh_shipment_status': 'cascade',
    })

    def _process_operation(self):
        self.ensure_one()
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
