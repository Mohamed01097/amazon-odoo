import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)


class AmazonImportOrderByIdWizard(models.TransientModel):
    """Manager-only controlled import of exactly one Amazon order by its ID.

    This is deliberately separate from the broad "Import Orders" button: it uses
    ``getOrder`` (a single order), reuses the existing normalization/upsert
    pipeline, preserves idempotency, and must NOT advance ``last_order_sync``,
    activate any cron, or enable auto-sync. For the first controlled validation it
    can hold the resulting FBA stock event for review so no stock moves until a
    human releases it.
    """

    _name = 'amazon.import.order.by.id.wizard'
    _description = 'Import a Single Amazon Order by ID'

    instance_id = fields.Many2one(
        'amazon.instance', string='Amazon Instance', required=True,
        default=lambda self: self.env['amazon.instance'].search([], limit=1),
    )
    amazon_order_ref = fields.Char('Amazon Order ID', required=True)
    hold_fba_stock = fields.Boolean(
        'Hold FBA stock for review', default=True,
        help="Create the FBA sale stock event in manual review so this controlled "
             "import does not move stock automatically. Release it with Retry after "
             "validating the imported order.",
    )

    def action_import(self):
        self.ensure_one()
        instance = self.instance_id
        if not instance:
            raise UserError(_("Select an Amazon instance."))
        instance._check_amazon_manager_access()
        order_ref = (self.amazon_order_ref or '').strip()
        if not order_ref:
            raise UserError(_("Enter an Amazon Order ID."))

        instance._auto_fix_region()
        instance._check_required_fields()
        access_token = instance._get_access_token_or_raise()
        api = AmazonAPI()
        data = api.get_order(instance, access_token, order_ref)
        order_data = data.get('payload') or {}
        if not order_data.get('AmazonOrderId'):
            raise UserError(_("Amazon returned no order for ID %s.") % order_ref)

        # A terminal-state job row only to reuse the shared import pipeline. It is
        # created as 'done' with no next_run_at, so the processing cron (which
        # selects state IN ('draft','running')) never touches it. last_order_sync
        # and all cron/auto-sync state are intentionally left unchanged.
        now = fields.Datetime.now()
        job = self.env['amazon.order.import.job'].create({
            'instance_id': instance.id,
            'state': 'done',
            'batch_size': 1,
            'next_token': False,
            'next_run_at': False,
            'is_single_order': True,
            'single_order_ref': order_ref,
            'started_at': now,
            'finished_at': now,
        })
        job.with_context(amazon_hold_fba_stock=self.hold_fba_stock)._import_one_order(
            api, access_token, order_data,
        )

        order_rec = self.env['amazon.sale.order'].search([
            ('instance_id', '=', instance.id),
            ('amazon_order_ref', '=', order_ref),
        ], limit=1)
        if not order_rec:
            raise UserError(_("The order was fetched but could not be located after import."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'amazon.sale.order',
            'res_id': order_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }
