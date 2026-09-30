from odoo import _, Command, api, fields, models
from odoo.exceptions import UserError

from ..models.amazon_inbound_shipping import READY_TO_SHIP_MIN_LEAD_MINUTES


class AmazonFbaTransportationReadyToShipWizard(models.TransientModel):
    """Collect Ready-to-Ship value(s) before (re)generating transportation options.

    Amazon generateTransportationOptions is a plan-level request: it carries one
    readyToShipWindow per accepted physical shipment of the placement option.
    The wizard therefore shows one Ready-to-Ship value when the plan has a single
    accepted physical shipment and one line per accepted physical shipment
    otherwise. On confirmation it validates every value, saves them on the
    physical shipments and runs the existing internal generation logic.
    """

    _name = 'amazon.fba.transportation.ready.to.ship.wizard'
    _description = 'Amazon FBA Transportation Ready-to-Ship Wizard'

    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', string='Physical Shipment',
        required=True, ondelete='cascade',
    )
    operation_type = fields.Selection([
        ('generate', 'Generate'),
        ('regenerate', 'Regenerate'),
    ], required=True, default='generate')
    ready_to_ship_at = fields.Datetime(
        string='Ready-to-Ship Date/Time',
        help=(
            "Earliest real date/time when all cartons are packed, labeled, staged, "
            "and physically available for carrier pickup. Sent to Amazon when "
            "generating transportation options."
        ),
    )
    line_ids = fields.One2many(
        'amazon.fba.transportation.ready.to.ship.wizard.line', 'wizard_id',
        string='Physical Shipments',
    )
    shipment_count = fields.Integer(readonly=True)
    min_lead_minutes = fields.Integer(
        string='Minimum Lead Time (minutes)', readonly=True,
        default=lambda self: READY_TO_SHIP_MIN_LEAD_MINUTES,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        physical = self.env['amazon.fba.physical.shipment'].browse(res.get('physical_shipment_id'))
        if not physical:
            return res
        physicals = physical._accepted_plan_physicals()
        primary = physical if physical in physicals else physicals[:1]
        if 'ready_to_ship_at' in fields_list and not res.get('ready_to_ship_at'):
            res['ready_to_ship_at'] = (primary or physical).ready_to_ship_at or False
        if 'shipment_count' in fields_list:
            res['shipment_count'] = len(physicals)
        if 'line_ids' in fields_list and len(physicals) > 1:
            # Plan-level request with several shipments: one value per shipment.
            res['line_ids'] = [Command.create({
                'physical_shipment_id': item.id,
                'ready_to_ship_at': item.ready_to_ship_at or False,
            }) for item in physicals]
        return res

    def _ready_to_ship_values(self, physicals):
        """Map accepted physical shipment ids to the values entered by the operator."""
        self.ensure_one()
        if self.shipment_count <= 1 and len(physicals) <= 1:
            target = physicals[:1] or self.physical_shipment_id
            return {target.id: self.ready_to_ship_at}
        return {
            line.physical_shipment_id.id: line.ready_to_ship_at
            for line in self.line_ids
            if line.physical_shipment_id in physicals
        }

    def action_confirm(self):
        self.ensure_one()
        physical = self.physical_shipment_id
        if not physical:
            raise UserError(_("Select the Amazon physical shipment to generate transportation for."))
        physical.inbound_shipment_id._check_inbound_manager_access()
        physicals = physical._accepted_plan_physicals()
        values = self._ready_to_ship_values(physicals)

        # 1. Validate every Ready-to-Ship value and the existing duplicate/order
        #    protections before anything is written.
        physicals._check_ready_to_ship_for_transportation_generation(values=values)
        if self.operation_type == 'regenerate':
            physical._ensure_transportation_regeneration_allowed(physicals)
        else:
            physical._ensure_transportation_generation_allowed(physicals)

        # 2. Save the operator's values on the physical shipments.
        for item in physicals:
            if item.id in values and values[item.id] != item.ready_to_ship_at:
                item.write({'ready_to_ship_at': values[item.id]})

        # 3. Run the existing internal generation logic (never the public
        #    button methods, which only open this wizard).
        if self.operation_type == 'regenerate':
            result = physical._action_regenerate_transportation_options()
        else:
            result = physical._action_generate_transportation_options()
        if isinstance(result, dict) and result.get('tag') == 'display_notification':
            result = dict(result, params=dict(
                result.get('params') or {}, next={'type': 'ir.actions.act_window_close'},
            ))
        return result


class AmazonFbaTransportationReadyToShipWizardLine(models.TransientModel):
    _name = 'amazon.fba.transportation.ready.to.ship.wizard.line'
    _description = 'Amazon FBA Transportation Ready-to-Ship Wizard Line'

    wizard_id = fields.Many2one(
        'amazon.fba.transportation.ready.to.ship.wizard', required=True, ondelete='cascade',
    )
    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', string='Physical Shipment',
        required=True, ondelete='cascade',
    )
    amazon_shipment_id = fields.Char(
        related='physical_shipment_id.amazon_shipment_id', string='Amazon Shipment ID',
    )
    shipment_confirmation_id = fields.Char(
        related='physical_shipment_id.shipment_confirmation_id', string='Shipment Confirmation ID',
    )
    destination_fc = fields.Char(
        related='physical_shipment_id.destination_fc', string='Destination FC',
    )
    ready_to_ship_at = fields.Datetime(string='Ready-to-Ship Date/Time')
