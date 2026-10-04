from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AmazonFbaScheduleAppointmentWizard(models.TransientModel):
    """Confirm scheduling of ONE selected self-ship FC appointment slot.

    Opening the wizard performs no Amazon call. It shows a stable snapshot of
    the slot that will be scheduled (taken from the physical shipment's currently
    selected slot) and only the explicit Confirm Schedule button initiates the
    synchronous Amazon scheduleSelfShipAppointment write.
    """

    _name = 'amazon.fba.schedule.appointment.wizard'
    _description = 'Amazon FBA Schedule FC Appointment Wizard'

    physical_shipment_id = fields.Many2one(
        'amazon.fba.physical.shipment', string='Physical Shipment',
        required=True, ondelete='cascade',
    )
    destination_fc = fields.Char(string='Destination FC', readonly=True)
    amazon_slot_id = fields.Char(string='Amazon Slot ID', readonly=True)
    start_time = fields.Datetime(string='Selected Start Time', readonly=True)
    end_time = fields.Datetime(string='Selected End Time', readonly=True)
    duration_minutes = fields.Integer(string='Duration (min)', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        physical = self.env['amazon.fba.physical.shipment'].browse(
            res.get('physical_shipment_id')
        )
        if not physical:
            return res
        slot = physical.selected_appointment_slot_id
        if not slot:
            raise UserError(_(
                "Please select an FC appointment slot before scheduling the appointment."
            ))
        res.update({
            'destination_fc': physical.destination_fc or '',
            'amazon_slot_id': slot.amazon_appointment_slot_id or '',
            'start_time': slot.start_date,
            'end_time': slot.end_date,
            'duration_minutes': slot.duration_minutes,
        })
        return res

    def action_confirm_schedule(self):
        self.ensure_one()
        physical = self.physical_shipment_id
        if not physical:
            raise UserError(_("Select the Amazon physical shipment to schedule."))
        result = physical._schedule_selected_appointment()
        if isinstance(result, dict) and result.get('tag') == 'display_notification':
            result = dict(result, params=dict(
                result.get('params') or {},
                next={'type': 'ir.actions.act_window_close'},
            ))
        return result
