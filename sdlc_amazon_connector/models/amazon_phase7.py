import csv
import hashlib
import io
import json
import logging
from datetime import datetime, time, timedelta, timezone
from xml.etree import ElementTree

from odoo import _, api, Command, fields, models
from odoo.exceptions import UserError, ValidationError

from .amazon_api import (
    AmazonAPI,
    FEED_FBA_CREATE_REMOVAL,
    REPORT_FBA_INVENTORY_ADJUSTMENT,
    REPORT_FBA_REIMBURSEMENTS,
    REPORT_FBA_RETURNS,
    REPORT_REMOVAL_ORDER_DETAIL,
    REPORT_REMOVAL_SHIPMENT_DETAIL,
)

_logger = logging.getLogger(__name__)


ADJUSTMENT_CLASSIFIERS = {
    'lost': ('lost', ('lost', 'missing')),
    'found': ('found', ('found',)),
    'damaged': ('damaged', ('damaged', 'warehouse damage')),
    'destroyed': ('destroyed', ('destroyed', 'disposed')),
    'transfer': ('transfer', ('transfer',)),
    'correction': ('correction', ('correction',)),
    'reimbursement': ('reimbursement', ('reimburse',)),
}


class AmazonSmartAlertPhase7(models.Model):
    _inherit = 'amazon.smart.alert'

    alert_type = fields.Selection(selection_add=[
        ('phase7_manual_review', 'FBA Event Manual Review'),
        ('phase7_job_failed', 'FBA Returns/Removals Job Failed'),
    ], ondelete={
        'phase7_manual_review': 'cascade',
        'phase7_job_failed': 'cascade',
    })

    @api.model
    def phase7_alert(self, instance, issue_key, title, description,
                     source=False, product=False, critical=False):
        values = {
            'instance_id': instance.id,
            'product_id': product.id if product else False,
            'alert_type': 'phase7_job_failed' if critical else 'phase7_manual_review',
            'severity': '3_urgent' if critical else '2_warning',
            'title': title, 'description': description,
            'suggested_action': _("Review the raw Amazon value and approve a mapping before applying stock or matching."),
            'issue_key': issue_key,
            'is_operational': True,
            'source_model': source._name if source else False,
            'source_id': source.id if source else 0,
        }
        alert = self.sudo().search([('issue_key', '=', issue_key)], limit=1)
        if alert:
            alert.write({**values, 'state': 'new'})
            return alert
        return self.sudo().create(values)


class AmazonPhase7StockService(models.AbstractModel):
    _name = 'amazon.phase7.stock.service'
    _description = 'Amazon Phase 7 Stock Safety Service'

    @api.model
    def number(self, value, default=0.0):
        try:
            return float(value if value not in (None, '') else default)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Amazon returned an invalid quantity: %s", value)) from exc

    @api.model
    def datetime(self, value):
        if not value:
            return False
        if isinstance(value, datetime):
            result = value
        else:
            normalized = str(value).strip()
            if not normalized:
                return False
            if normalized.endswith('Z'):
                normalized = normalized[:-1] + '+00:00'
            result = False
            for candidate in (normalized, normalized.replace('/', '-')):
                try:
                    result = datetime.fromisoformat(candidate)
                    break
                except ValueError:
                    continue
            if not result:
                for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%m/%d/%Y'):
                    try:
                        result = datetime.strptime(normalized, pattern)
                        break
                    except ValueError:
                        continue
            if not result:
                raise ValidationError(_("Amazon returned an invalid date: %s", value))
        if result.tzinfo:
            result = result.astimezone(timezone.utc).replace(tzinfo=None)
        return result.replace(microsecond=0)

    @api.model
    def resolve_product(self, instance, sku=False, fnsku=False, asin=False):
        domain = [('instance_id', '=', instance.id)]
        identity = []
        if sku:
            identity.append(('sku', '=', str(sku).strip()))
        if asin:
            identity.append(('asin', '=', str(asin).strip()))
        amazon_product = self.env['amazon.product']
        for condition in identity:
            amazon_product = self.env['amazon.product'].search(domain + [condition], limit=1)
            if amazon_product:
                break
        return {
            'amazon_product_id': amazon_product.id or False,
            'odoo_product_id': amazon_product.odoo_product_id.id if amazon_product.odoo_product_id else False,
        }

    @api.model
    def _internal_picking_type(self, company, source, destination):
        picking_type = self.env['stock.picking.type'].sudo().search([
            ('code', '=', 'internal'), ('company_id', '=', company.id),
            ('default_location_src_id', '=', source.id),
            ('default_location_dest_id', '=', destination.id),
        ], limit=1)
        if not picking_type:
            picking_type = self.env['stock.picking.type'].sudo().search([
                ('code', '=', 'internal'), ('company_id', '=', company.id),
            ], order='warehouse_id, sequence, id', limit=1)
        if not picking_type:
            raise UserError(_("No Internal Transfer operation type exists for %s.", company.display_name))
        return picking_type

    @api.model
    def _create_picking(self, instance, product, quantity, source, destination,
                        origin, movement_type, validate, removal_order=False,
                        removal_shipment=False):
        company = instance.company_id
        if not product or not product.is_storable:
            raise UserError(_("The Amazon event requires an inventory-tracked Odoo product mapping."))
        if product.company_id and product.company_id != company:
            raise UserError(_("The mapped product belongs to another company."))
        if not source or not destination or source == destination:
            raise UserError(_("The Amazon stock locations are not configured correctly."))
        if source.company_id != company or destination.company_id != company:
            raise UserError(_("Amazon stock locations must belong to the instance company."))
        if quantity <= 0:
            raise UserError(_("Stock movement quantity must be positive."))
        picking = self.env['stock.picking'].sudo().with_company(company).create({
            'picking_type_id': self._internal_picking_type(company, source, destination).id,
            'location_id': source.id, 'location_dest_id': destination.id,
            'company_id': company.id, 'origin': origin,
            'amazon_instance_id': instance.id,
            'amazon_fba_movement_type': movement_type,
            'amazon_removal_order_id': removal_order.id if removal_order else False,
            'amazon_removal_shipment_id': removal_shipment.id if removal_shipment else False,
            'move_type': 'one',
            'move_ids': [Command.create({
                'product_id': product.id, 'product_uom_qty': quantity,
                'product_uom': product.uom_id.id, 'location_id': source.id,
                'location_dest_id': destination.id, 'company_id': company.id,
                'origin': origin,
            })],
        })
        picking.action_confirm()
        if not validate:
            # Customer warehouse receipt is intentionally not validated from
            # Amazon shipment data. Reservation remains an Odoo/user decision.
            return picking
        picking.action_assign()
        move = picking.move_ids
        if source.usage in ('internal', 'transit') and (
            move.state != 'assigned'
            or move.product_uom.compare(move.quantity, move.product_uom_qty) < 0
        ):
            picking.action_cancel()
            raise UserError(_("Insufficient stock in %s for confirmed Amazon event.", source.display_name))
        if not move.move_line_ids:
            raise UserError(_("Odoo did not create stock details for the Amazon event."))
        result = picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        if isinstance(result, dict) or picking.state != 'done':
            raise UserError(_("Stock transfer %s requires manual stock details.", picking.name))
        return picking

    @api.model
    def apply_return(self, event):
        instance = event.instance_id
        if event.inventory_reflected:
            event.stock_action_state = 'already_reflected'
            return False
        if instance.return_stock_policy == 'informational':
            event.stock_action_state = 'informational'
            return False
        if instance.return_stock_policy == 'audit_only':
            event.stock_action_state = 'audit_only'
            return False
        if event.linked_stock_move_id:
            event.stock_action_state = 'moved'
            return event.linked_stock_move_id.picking_id
        destination = (
            instance.fba_sellable_location_id
            if event.operational_disposition == 'sellable'
            else instance.fba_unsellable_location_id
        )
        try:
            picking = self._create_picking(
                instance, event.odoo_product_id, event.quantity,
                instance.fba_return_source_location_id, destination,
                event.event_key, 'return_sellable' if event.operational_disposition == 'sellable' else 'return_unsellable',
                True,
            )
            event.write({
                'linked_stock_move_id': picking.move_ids.id,
                'stock_action_state': 'moved',
            })
            return picking
        except Exception as exc:
            event.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'review_reason': str(exc)[:5000],
            })
            _logger.warning("Return stock action %s requires review: %s", event.event_key, exc)
            return False

    @api.model
    def apply_removal_shipment(self, shipment):
        line = shipment.line_id
        order = shipment.order_id
        quantity = max(shipment.shipped_quantity - shipment.dispatched_stock_quantity, 0.0)
        if not quantity:
            return shipment.dispatch_move_id.picking_id if shipment.dispatch_move_id else False
        if not line.odoo_product_id:
            order.write({'manual_review_required': True, 'stock_action_state': 'manual_review'})
            return False
        source = (
            order.instance_id.fba_sellable_location_id
            if (shipment.disposition or line.disposition).strip().lower() == 'sellable'
            else order.instance_id.fba_unsellable_location_id
        )
        try:
            dispatch = self._create_picking(
                order.instance_id, line.odoo_product_id, quantity,
                source, order.instance_id.fba_removal_transit_location_id,
                order.removal_order_id, 'removal_dispatch', True,
                removal_order=order, removal_shipment=shipment,
            )
            receipt = self._create_picking(
                order.instance_id, line.odoo_product_id, quantity,
                order.instance_id.fba_removal_transit_location_id,
                order.instance_id.fba_source_location_id,
                order.removal_order_id, 'removal_receipt', False,
                removal_order=order, removal_shipment=shipment,
            )
            shipment.write({
                'dispatch_move_id': dispatch.move_ids.id,
                'receipt_picking_id': receipt.id,
                'dispatched_stock_quantity': shipment.dispatched_stock_quantity + quantity,
            })
            line.write({
                'dispatch_move_id': dispatch.move_ids.id,
                'receipt_move_id': receipt.move_ids.id,
                'dispatched_stock_quantity': line.dispatched_stock_quantity + quantity,
            })
            order.write({
                'picking_ids': [Command.link(dispatch.id), Command.link(receipt.id)],
                'stock_action_state': 'awaiting_receipt', 'state': 'awaiting_receipt',
            })
            return dispatch
        except Exception as exc:
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'error_message': str(exc)[:5000],
            })
            _logger.warning("Removal dispatch %s requires review: %s", shipment.shipment_key, exc)
            return False

    @api.model
    def apply_disposal(self, line, delta):
        order = line.order_id
        if order.removal_type != 'disposal' or delta <= 0:
            return False
        if order.instance_id.adjustment_stock_policy != 'event_moves':
            order.stock_action_state = 'informational'
            return False
        source = (
            order.instance_id.fba_sellable_location_id
            if line.disposition.strip().lower() == 'sellable'
            else order.instance_id.fba_unsellable_location_id
        )
        unapplied = max(line.disposed_quantity - line.disposed_stock_quantity, 0.0)
        if not unapplied:
            return False
        try:
            picking = self._create_picking(
                order.instance_id, line.odoo_product_id, unapplied, source,
                order.instance_id.fba_disposal_location_id,
                order.removal_order_id, 'removal_disposal', True,
                removal_order=order,
            )
            line.write({
                'disposal_move_id': picking.move_ids.id,
                'disposed_stock_quantity': line.disposed_stock_quantity + unapplied,
            })
            order.write({
                'picking_ids': [Command.link(picking.id)],
                'stock_action_state': 'disposed',
            })
            return picking
        except Exception as exc:
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'error_message': str(exc)[:5000],
            })
            return False

    @api.model
    def apply_adjustment(self, event):
        instance = event.instance_id
        if event.inventory_reflected or instance.adjustment_stock_policy != 'event_moves':
            event.stock_action_state = 'already_reflected' if event.inventory_reflected else 'informational'
            return False
        if event.linked_stock_move_id:
            event.stock_action_state = 'moved'
            return event.linked_stock_move_id.picking_id
        source = destination = False
        if event.event_category == 'lost':
            source, destination = instance.fba_sellable_location_id, instance.fba_disposal_location_id
        elif event.event_category == 'damaged':
            source, destination = instance.fba_sellable_location_id, instance.fba_unsellable_location_id
        elif event.event_category == 'destroyed':
            source, destination = instance.fba_unsellable_location_id, instance.fba_disposal_location_id
        elif event.event_category == 'found':
            prior = event.reversal_of_adjustment_id
            if not prior or not prior.linked_stock_move_id:
                event.write({
                    'manual_review_required': True, 'stock_action_state': 'manual_review',
                    'review_reason': _("Found inventory was not uniquely linked to an applied loss."),
                })
                return False
            source = prior.linked_stock_move_id.location_dest_id
            destination = prior.linked_stock_move_id.location_id
        else:
            event.write({'manual_review_required': True, 'stock_action_state': 'manual_review'})
            return False
        try:
            picking = self._create_picking(
                instance, event.odoo_product_id, abs(event.quantity), source, destination,
                event.event_key, 'inventory_%s' % event.event_category, True,
            )
            event.write({'linked_stock_move_id': picking.move_ids.id, 'stock_action_state': 'moved'})
            return picking
        except Exception as exc:
            event.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'review_reason': str(exc)[:5000],
            })
            return False


class AmazonFBAInventoryAdjustment(models.Model):
    _name = 'amazon.fba.inventory.adjustment'
    _description = 'Amazon FBA Inventory Adjustment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'event_date desc, id desc'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True, check_company=True,
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, readonly=True, index=True)
    event_key = fields.Char(required=True, readonly=True, copy=False, index=True)
    event_date = fields.Datetime(required=True, index=True)
    sku = fields.Char(index=True)
    fnsku = fields.Char(index=True)
    asin = fields.Char(index=True)
    amazon_product_id = fields.Many2one('amazon.product', ondelete='set null', index=True)
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    fulfillment_center_id = fields.Char(index=True)
    quantity = fields.Float()
    reason_code = fields.Char(index=True, tracking=True)
    reference_id = fields.Char(index=True)
    transaction_event_id = fields.Char(index=True)
    disposition = fields.Char(index=True)
    event_category = fields.Selection([
        ('lost', 'Lost'), ('found', 'Found'), ('damaged', 'Damaged'),
        ('destroyed', 'Destroyed/Disposed'), ('transfer', 'Transfer'),
        ('correction', 'Correction'), ('reimbursement', 'Reimbursement Related'),
        ('unknown', 'Unknown'),
    ], default='unknown', required=True, index=True, tracking=True)
    reconciled_quantity = fields.Float()
    unreconciled_quantity = fields.Float()
    raw_report_reference = fields.Char(index=True)
    raw_report_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    linked_stock_move_id = fields.Many2one('stock.move', ondelete='restrict', copy=False)
    linked_reimbursement_id = fields.Many2one('amazon.fba.reimbursement', ondelete='set null')
    reversal_of_adjustment_id = fields.Many2one('amazon.fba.inventory.adjustment', ondelete='restrict')
    state = fields.Selection([
        ('imported', 'Imported'), ('matched', 'Matched'), ('applied', 'Applied'),
        ('manual_review', 'Manual Review'),
    ], default='imported', required=True, index=True, tracking=True)
    stock_action_state = fields.Selection([
        ('not_evaluated', 'Not Evaluated'), ('informational', 'Informational'),
        ('already_reflected', 'Already Reflected'), ('moved', 'Stock Move Created'),
        ('manual_review', 'Manual Review'),
    ], default='not_evaluated', required=True, index=True)
    inventory_reflected = fields.Boolean(default=False)
    manual_review_required = fields.Boolean(default=False, index=True, tracking=True)
    review_reason = fields.Text()
    imported_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    last_synced_at = fields.Datetime(readonly=True)
    stock_move_count = fields.Integer(compute='_compute_link_counts')
    reimbursement_count = fields.Integer(compute='_compute_link_counts')
    sync_log_count = fields.Integer(compute='_compute_link_counts')

    _event_unique = models.Constraint(
        'UNIQUE(instance_id, event_key)', 'This Amazon inventory adjustment was already imported.',
    )

    def _compute_link_counts(self):
        reimbursement_model = self.env['amazon.fba.reimbursement'].sudo()
        sync_log_model = self.env['amazon.sync.log'].sudo()
        for record in self:
            record.stock_move_count = bool(record.linked_stock_move_id)
            record.reimbursement_count = reimbursement_model.search_count([
                ('linked_adjustment_id', '=', record.id),
            ])
            record.sync_log_count = sync_log_model.search_count([
                ('source_model', '=', record._name), ('source_id', '=', record.id),
            ])

    def _open_linked_records(self, title, model_name, records):
        self.ensure_one()
        records = records.exists()
        action = {
            'type': 'ir.actions.act_window', 'name': title,
            'res_model': model_name, 'view_mode': 'list,form',
            'domain': [('id', 'in', records.ids)],
        }
        if len(records) == 1:
            action.update({'view_mode': 'form', 'res_id': records.id})
        return action

    def action_view_stock_move(self):
        return self._open_linked_records(_('Stock Move'), 'stock.move', self.linked_stock_move_id)

    def action_view_reimbursements(self):
        records = self.env['amazon.fba.reimbursement'].search([('linked_adjustment_id', '=', self.id)])
        return self._open_linked_records(_('Reimbursements'), records._name, records)

    def action_view_sync_logs(self):
        records = self.env['amazon.sync.log'].search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ])
        return self._open_linked_records(_('Sync Logs'), records._name, records)

    @api.model
    def _category(self, reason, quantity):
        text = str(reason or '').strip().lower().replace('_', ' ')
        for category, (_label, tokens) in ADJUSTMENT_CLASSIFIERS.items():
            if any(token in text for token in tokens):
                return category
        # Signed ledger quantities alone are insufficient to infer physical cause.
        return 'unknown'

    @api.model
    def import_row(self, instance, row, report_reference=False):
        values_by_name = {str(key).strip().lower().replace(' ', '-'): value for key, value in row.items()}
        get = values_by_name.get
        event_date = self.env['amazon.phase7.stock.service'].datetime(get('date'))
        quantity = self.env['amazon.phase7.stock.service'].number(get('quantity'))
        reason = get('reason') or ''
        components = [
            instance.id, get('referenceid'), get('date'), get('fnsku'), get('asin'),
            get('msku'), get('eventtype'), get('fulfillmentcenter'),
            get('disposition'), reason, get('quantity'), get('country'),
        ]
        key = hashlib.sha256('|'.join(str(value or '').strip() for value in components).encode()).hexdigest()
        event = self.search([('instance_id', '=', instance.id), ('event_key', '=', key)], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            instance, get('msku'), get('fnsku'), get('asin')
        )
        category = self._category(reason, quantity)
        vals = {
            'instance_id': instance.id, 'event_key': key, 'event_date': event_date,
            'sku': get('msku') or '', 'fnsku': get('fnsku') or '', 'asin': get('asin') or '',
            'fulfillment_center_id': get('fulfillmentcenter') or '', 'quantity': quantity,
            'reason_code': reason, 'reference_id': get('referenceid') or '',
            'transaction_event_id': get('referenceid') or '',
            'disposition': get('disposition') or '', 'event_category': category,
            'reconciled_quantity': self.env['amazon.phase7.stock.service'].number(get('reconciledquantity')),
            'unreconciled_quantity': self.env['amazon.phase7.stock.service'].number(get('unreconciledquantity')),
            'raw_report_reference': report_reference,
            'raw_report_data': json.dumps(row, default=str, sort_keys=True),
            'last_synced_at': fields.Datetime.now(),
            'manual_review_required': category == 'unknown',
            'state': 'manual_review' if category == 'unknown' else 'imported',
            **product_values,
        }
        if event:
            for protected in ('instance_id', 'event_key', 'stock_action_state', 'linked_stock_move_id'):
                vals.pop(protected, None)
            event.write(vals)
        else:
            event = self.create(vals)
        if category == 'found' and not event.reversal_of_adjustment_id:
            candidates = self.search([
                ('instance_id', '=', instance.id), ('event_category', '=', 'lost'),
                ('fnsku', '=', event.fnsku), ('quantity', '=', -event.quantity),
                ('event_date', '<=', event.event_date),
            ], order='event_date desc', limit=2)
            if len(candidates) == 1:
                event.reversal_of_adjustment_id = candidates.id
            else:
                event.write({
                    'manual_review_required': True, 'state': 'manual_review',
                    'review_reason': _("Found event has no unique prior lost event."),
                })
        if event.manual_review_required:
            self.env['amazon.smart.alert'].phase7_alert(
                instance, 'adjustment:%s' % event.event_key,
                _("FBA inventory adjustment requires review"),
                event.review_reason or _("Unknown Amazon ledger reason: %s", event.reason_code or _('empty')),
                source=event, product=event.amazon_product_id,
            )
        self.env['amazon.phase7.stock.service'].apply_adjustment(event)
        return event


class AmazonFBAReimbursement(models.Model):
    _name = 'amazon.fba.reimbursement'
    _description = 'Amazon FBA Reimbursement'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'approval_date desc, id desc'
    _check_company_auto = True

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True, check_company=True,
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, readonly=True, index=True)
    event_key = fields.Char(required=True, readonly=True, copy=False, index=True)
    reimbursement_id = fields.Char(required=True, index=True, tracking=True)
    case_id = fields.Char(index=True)
    approval_date = fields.Datetime(index=True)
    reimbursement_reason = fields.Char(index=True, tracking=True)
    reimbursement_classification = fields.Selection([
        ('lost_inventory', 'Lost Inventory'), ('damaged_inventory', 'Damaged Inventory'),
        ('customer_return', 'Customer Return'), ('removal', 'Removal'),
        ('reversal', 'Reversal'), ('other', 'Other/Unknown'),
    ], default='other', required=True, index=True)
    amazon_order_id = fields.Char(index=True)
    amazon_order_item_id = fields.Char(index=True)
    sku = fields.Char(index=True)
    fnsku = fields.Char(index=True)
    asin = fields.Char(index=True)
    amazon_product_id = fields.Many2one('amazon.product', ondelete='set null', index=True)
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    quantity_reimbursed_cash = fields.Float()
    quantity_reimbursed_inventory = fields.Float()
    quantity_reimbursed_total = fields.Float(compute='_compute_quantity_total', store=True)
    amount_per_unit = fields.Monetary(currency_field='currency_id')
    amount_total = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', ondelete='restrict')
    currency_code = fields.Char()
    original_reimbursement_id = fields.Char(index=True)
    original_reimbursement_type = fields.Char()
    linked_return_id = fields.Many2one('amazon.return.report.line', ondelete='set null', check_company=True)
    linked_adjustment_id = fields.Many2one('amazon.fba.inventory.adjustment', ondelete='set null', check_company=True)
    linked_removal_order_id = fields.Many2one('amazon.removal.order', ondelete='set null', check_company=True)
    linked_settlement_id = fields.Many2one('amazon.settlement.report', ondelete='set null')
    accounting_state = fields.Selection([
        ('not_ready', 'Not Ready'), ('ready_for_phase8', 'Ready for Phase 8'),
    ], default='not_ready', required=True, index=True)
    review_state = fields.Selection([
        ('unmatched', 'Unmatched'), ('matched', 'Matched'),
        ('manual_review', 'Manual Review'), ('ignored', 'Ignored'),
    ], default='unmatched', required=True, index=True, tracking=True)
    match_method = fields.Char(readonly=True)
    review_note = fields.Text()
    raw_report_reference = fields.Char(index=True)
    raw_report_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    imported_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    last_synced_at = fields.Datetime(readonly=True)
    amazon_order_count = fields.Integer(compute='_compute_link_counts')
    sale_order_count = fields.Integer(compute='_compute_link_counts')
    return_count = fields.Integer(compute='_compute_link_counts')
    adjustment_count = fields.Integer(compute='_compute_link_counts')
    removal_order_count = fields.Integer(compute='_compute_link_counts')
    sync_log_count = fields.Integer(compute='_compute_link_counts')

    _event_unique = models.Constraint(
        'UNIQUE(instance_id, event_key)', 'This Amazon reimbursement row was already imported.',
    )

    def _amazon_orders(self):
        self.ensure_one()
        if not self.amazon_order_id:
            return self.env['amazon.sale.order']
        return self.env['amazon.sale.order'].search([
            ('instance_id', '=', self.instance_id.id),
            ('amazon_order_ref', '=', self.amazon_order_id),
        ])

    def _compute_link_counts(self):
        sync_log_model = self.env['amazon.sync.log'].sudo()
        for record in self:
            amazon_orders = record._amazon_orders()
            record.amazon_order_count = len(amazon_orders)
            record.sale_order_count = len(amazon_orders.sale_order_id)
            record.return_count = bool(record.linked_return_id)
            record.adjustment_count = bool(record.linked_adjustment_id)
            record.removal_order_count = bool(record.linked_removal_order_id)
            record.sync_log_count = sync_log_model.search_count([
                ('source_model', '=', record._name), ('source_id', '=', record.id),
            ])

    def _open_linked_records(self, title, model_name, records):
        self.ensure_one()
        records = records.exists()
        action = {
            'type': 'ir.actions.act_window', 'name': title,
            'res_model': model_name, 'view_mode': 'list,form',
            'domain': [('id', 'in', records.ids)],
        }
        if len(records) == 1:
            action.update({'view_mode': 'form', 'res_id': records.id})
        return action

    def action_view_amazon_order(self):
        records = self._amazon_orders()
        return self._open_linked_records(_('Amazon Order'), records._name, records)

    def action_view_sale_order(self):
        records = self._amazon_orders().sale_order_id
        return self._open_linked_records(_('Odoo Sale Order'), records._name, records)

    def action_view_return(self):
        return self._open_linked_records(
            _('Customer Return'), 'amazon.return.report.line', self.linked_return_id,
        )

    def action_view_adjustment(self):
        return self._open_linked_records(
            _('Inventory Adjustment'), 'amazon.fba.inventory.adjustment', self.linked_adjustment_id,
        )

    def action_view_removal_order(self):
        return self._open_linked_records(
            _('Removal Order'), 'amazon.removal.order', self.linked_removal_order_id,
        )

    def action_view_sync_logs(self):
        records = self.env['amazon.sync.log'].search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ])
        return self._open_linked_records(_('Sync Logs'), records._name, records)

    @api.depends('quantity_reimbursed_cash', 'quantity_reimbursed_inventory')
    def _compute_quantity_total(self):
        for rec in self:
            rec.quantity_reimbursed_total = (
                rec.quantity_reimbursed_cash + rec.quantity_reimbursed_inventory
            )

    @api.model
    def _classification(self, reason, original_type):
        text = ('%s %s' % (reason or '', original_type or '')).lower()
        if 'reversal' in text:
            return 'reversal'
        if 'removal' in text:
            return 'removal'
        if 'return' in text:
            return 'customer_return'
        if 'damage' in text:
            return 'damaged_inventory'
        if 'lost' in text or 'warehouse' in text:
            return 'lost_inventory'
        return 'other'

    @api.model
    def import_row(self, instance, row, report_reference=False):
        reimbursement_id = row.get('reimbursement-id') or ''
        components = [
            instance.id, reimbursement_id, row.get('approval-date'), row.get('amazon-order-id'),
            row.get('sku'), row.get('fnsku'), row.get('asin'), row.get('reason'),
            row.get('condition'), row.get('currency-unit'), row.get('amount-total'),
            row.get('quantity-reimbursed-cash'), row.get('quantity-reimbursed-inventory'),
            row.get('original-reimbursement-id'), row.get('original-reimbursement-type'),
        ]
        key = hashlib.sha256('|'.join(str(value or '').strip() for value in components).encode()).hexdigest()
        reimbursement = self.search([
            ('instance_id', '=', instance.id), ('event_key', '=', key),
        ], limit=1)
        currency_code = (row.get('currency-unit') or '').strip()
        currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            instance, row.get('sku'), row.get('fnsku'), row.get('asin')
        )
        vals = {
            'instance_id': instance.id, 'event_key': key,
            'reimbursement_id': reimbursement_id,
            'case_id': row.get('case-id') or '',
            'approval_date': self.env['amazon.phase7.stock.service'].datetime(row.get('approval-date')),
            'reimbursement_reason': row.get('reason') or '',
            'reimbursement_classification': self._classification(
                row.get('reason'), row.get('original-reimbursement-type')
            ),
            'amazon_order_id': row.get('amazon-order-id') or '',
            'amazon_order_item_id': row.get('amazon-order-item-id') or '',
            'sku': row.get('sku') or '', 'fnsku': row.get('fnsku') or '',
            'asin': row.get('asin') or '',
            'quantity_reimbursed_cash': self.env['amazon.phase7.stock.service'].number(row.get('quantity-reimbursed-cash')),
            'quantity_reimbursed_inventory': self.env['amazon.phase7.stock.service'].number(row.get('quantity-reimbursed-inventory')),
            'amount_per_unit': self.env['amazon.phase7.stock.service'].number(row.get('amount-per-unit')),
            'amount_total': self.env['amazon.phase7.stock.service'].number(row.get('amount-total')),
            'currency_id': currency.id or False, 'currency_code': currency_code,
            'original_reimbursement_id': row.get('original-reimbursement-id') or '',
            'original_reimbursement_type': row.get('original-reimbursement-type') or '',
            'raw_report_reference': report_reference,
            'raw_report_data': json.dumps(row, default=str, sort_keys=True),
            'last_synced_at': fields.Datetime.now(),
            **product_values,
        }
        if reimbursement:
            for protected in ('instance_id', 'event_key', 'review_state', 'match_method'):
                vals.pop(protected, None)
            reimbursement.write(vals)
        else:
            reimbursement = self.create(vals)
        return reimbursement

    def action_match(self):
        for reimbursement in self:
            reimbursement._match_one()
        return True

    def _set_match(self, field_name, record, method):
        self.ensure_one()
        if record.company_id != self.company_id:
            raise ValidationError(_("A reimbursement cannot link across companies."))
        self.write({
            field_name: record.id, 'review_state': 'matched',
            'match_method': method, 'review_note': False,
            'accounting_state': 'ready_for_phase8',
        })
        if field_name == 'linked_adjustment_id':
            record.linked_reimbursement_id = self
        return True

    def _match_one(self):
        self.ensure_one()
        if self.review_state == 'matched':
            return True
        instance_domain = [('instance_id', '=', self.instance_id.id)]
        candidates = []
        # Strong report references first.
        if self.original_reimbursement_id:
            prior = self.search(instance_domain + [
                ('reimbursement_id', '=', self.original_reimbursement_id),
                ('id', '!=', self.id),
            ], limit=2)
            if len(prior) == 1:
                self.write({
                    'review_state': 'matched', 'match_method': 'original_reimbursement_id',
                    'accounting_state': 'ready_for_phase8',
                })
                return True
        if self.amazon_order_id:
            candidates = self.env['amazon.return.report.line'].search(instance_domain + [
                ('amazon_order_id', '=', self.amazon_order_id),
            ], limit=3)
            if self.amazon_order_item_id:
                exact = candidates.filtered(lambda rec: rec.amazon_order_item_id == self.amazon_order_item_id)
                candidates = exact or candidates
            if len(candidates) == 1:
                return self._set_match('linked_return_id', candidates, 'amazon_order_item')
            if len(candidates) > 1:
                return self._ambiguous(_('Multiple customer returns match the Amazon order.'))
        adjustment = self.env['amazon.fba.inventory.adjustment'].search(instance_domain + [
            ('reference_id', 'in', [self.case_id, self.reimbursement_id]),
        ], limit=2) if self.case_id or self.reimbursement_id else self.env['amazon.fba.inventory.adjustment']
        if len(adjustment) == 1:
            return self._set_match('linked_adjustment_id', adjustment, 'adjustment_reference')
        if len(adjustment) > 1:
            return self._ambiguous(_('Multiple inventory adjustments match the reference.'))
        removal = self.env['amazon.removal.order'].search(instance_domain + [
            ('removal_order_id', 'in', [self.case_id, self.amazon_order_id]),
        ], limit=2) if self.case_id or self.amazon_order_id else self.env['amazon.removal.order']
        if len(removal) == 1:
            return self._set_match('linked_removal_order_id', removal, 'removal_order_id')
        if len(removal) > 1:
            return self._ambiguous(_('Multiple removal orders match the reference.'))
        # Controlled fallback: same FNSKU/SKU, close date, exact absolute quantity.
        if self.approval_date and (self.fnsku or self.sku) and self.quantity_reimbursed_total:
            start = self.approval_date - timedelta(days=45)
            end = self.approval_date + timedelta(days=45)
            domain = instance_domain + [('event_date', '>=', start), ('event_date', '<=', end)]
            domain += [('fnsku', '=', self.fnsku)] if self.fnsku else [('sku', '=', self.sku)]
            possible = self.env['amazon.fba.inventory.adjustment'].search(domain, limit=3).filtered(
                lambda item: abs(abs(item.quantity) - abs(self.quantity_reimbursed_total)) < 0.00001
            )
            if len(possible) == 1:
                return self._set_match('linked_adjustment_id', possible, 'sku_date_quantity_fallback')
            if len(possible) > 1:
                return self._ambiguous(_('Fallback SKU/date/quantity match is ambiguous.'))
        self.write({
            'review_state': 'manual_review',
            'review_note': _("No strong operational match was found."),
        })
        self.env['amazon.smart.alert'].phase7_alert(
            self.instance_id, 'reimbursement:%s' % self.event_key,
            _("FBA reimbursement is unmatched"), self.review_note,
            source=self, product=self.amazon_product_id,
        )
        return False

    def _ambiguous(self, message):
        self.ensure_one()
        self.write({'review_state': 'manual_review', 'review_note': message})
        self.env['amazon.smart.alert'].phase7_alert(
            self.instance_id, 'reimbursement:%s' % self.event_key,
            _("FBA reimbursement match is ambiguous"), message,
            source=self, product=self.amazon_product_id,
        )
        return False


class AmazonPhase7Job(models.Model):
    _name = 'amazon.phase7.job'
    _description = 'Amazon Phase 7 Persistent Job'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default='New', readonly=True, copy=False)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True, check_company=True,
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, readonly=True, index=True)
    operation_type = fields.Selection([
        ('customer_returns', 'Customer Return Import'),
        ('removal_submit', 'Removal Order Submission'),
        ('removal_feed_poll', 'Removal Feed Result Poll'),
        ('removal_status', 'Removal Status Import'),
        ('inventory_adjustments', 'Inventory Adjustment Import'),
        ('reimbursements', 'Reimbursement Import'),
        ('reimbursement_matching', 'Reimbursement Matching'),
    ], required=True, index=True)
    source_model = fields.Char(index=True)
    source_id = fields.Integer(index=True)
    state = fields.Selection([
        ('pending', 'Pending'), ('running', 'Running'), ('waiting_amazon', 'Waiting for Amazon'),
        ('done', 'Done'), ('failed', 'Failed'),
    ], default='pending', required=True, index=True, tracking=True)
    stage = fields.Selection([
        ('request', 'Request'), ('poll', 'Poll'), ('download', 'Download'),
        ('process', 'Process'), ('feed_poll', 'Feed Poll'), ('match', 'Match'),
        ('done', 'Done'),
    ], default='request', required=True, index=True)
    report_kind = fields.Char(index=True)
    date_from = fields.Date(index=True)
    date_to = fields.Date(index=True)
    amazon_report_id = fields.Char(copy=False, index=True)
    amazon_report_document_id = fields.Char(copy=False)
    feed_id = fields.Char(copy=False, index=True)
    feed_document_id = fields.Char(copy=False)
    raw_document = fields.Text(copy=False, groups='sdlc_amazon_connector.group_amazon_technical_admin')
    cursor_index = fields.Integer(default=0)
    batch_size = fields.Integer(default=100)
    total_found = fields.Integer(default=0, readonly=True)
    total_processed = fields.Integer(default=0, readonly=True)
    total_failed = fields.Integer(default=0, readonly=True)
    retry_count = fields.Integer(default=0, readonly=True)
    max_retries = fields.Integer(default=12)
    next_run_at = fields.Datetime(default=fields.Datetime.now, index=True)
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    last_activity_at = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    last_error_code = fields.Char(copy=False, readonly=True, index=True)
    last_error_message = fields.Text(copy=False, readonly=True)
    amazon_request_id = fields.Char(copy=False, readonly=True)
    responsible_user_id = fields.Many2one('res.users', default=lambda self: self.env.user, index=True)

    _retry_limits = models.Constraint(
        'CHECK(retry_count >= 0 AND max_retries BETWEEN 1 AND 100 AND batch_size BETWEEN 1 AND 1000)',
        'Phase 7 job retry or batch limits are invalid.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.phase7.job') or 'New'
        return super().create(vals_list)

    @api.model
    def enqueue(self, instance, operation_type, source=False, date_from=False, date_to=False):
        source_model = source._name if source else False
        source_id = source.id if source else 0
        domain = [
            ('instance_id', '=', instance.id), ('operation_type', '=', operation_type),
            ('state', 'in', ('pending', 'running', 'waiting_amazon')),
        ]
        if operation_type in ('removal_submit', 'removal_feed_poll'):
            domain += [('source_model', '=', source_model or False), ('source_id', '=', source_id)]
        active = self.sudo().search(domain, limit=1)
        if active:
            return active
        return self.sudo().create({
            'instance_id': instance.id, 'operation_type': operation_type,
            'source_model': source_model, 'source_id': source_id,
            'date_from': date_from, 'date_to': date_to,
            'stage': 'match' if operation_type == 'reimbursement_matching' else (
                'feed_poll' if operation_type == 'removal_feed_poll' else 'request'
            ),
        })

    def _source(self):
        self.ensure_one()
        if (
            not self.source_model or not self.source_id
            or self.source_model not in self.env.registry.models
        ):
            return self.env['amazon.instance']
        return self.env[self.source_model].sudo().browse(self.source_id).exists()

    @api.model
    def cron_process_jobs(self, limit=10):
        processed = 0
        for _index in range(limit):
            now = fields.Datetime.now()
            self.env.cr.execute("""
                SELECT id FROM amazon_phase7_job
                 WHERE state IN ('pending', 'running', 'waiting_amazon')
                   AND (next_run_at IS NULL OR next_run_at <= %s)
                 ORDER BY COALESCE(next_run_at, create_date), id
                 FOR UPDATE SKIP LOCKED LIMIT 1
            """, [now])
            row = self.env.cr.fetchone()
            if not row:
                break
            self.browse(row[0]).with_context(
                amazon_source_model=self._name, amazon_source_id=row[0],
                amazon_operation='phase7_job',
            )._run_one_turn()
            processed += 1
        return processed

    def _run_one_turn(self):
        self.ensure_one()
        if self.state in ('done', 'failed'):
            return False
        values = {'state': 'running', 'next_run_at': False, 'last_activity_at': fields.Datetime.now()}
        if not self.started_at:
            values['started_at'] = fields.Datetime.now()
        self.write(values)
        try:
            with self.env.cr.savepoint():
                if self.operation_type == 'removal_submit':
                    return self._submit_removal()
                if self.operation_type == 'removal_feed_poll':
                    return self._poll_removal_feed()
                if self.operation_type == 'reimbursement_matching':
                    return self._match_reimbursements()
                return self._report_turn()
        except Exception as exc:
            self._fail_or_retry(exc)
            return False

    def _report_configuration(self):
        if self.operation_type == 'customer_returns':
            return REPORT_FBA_RETURNS, False, 'returns'
        if self.operation_type == 'inventory_adjustments':
            return REPORT_FBA_INVENTORY_ADJUSTMENT, {'eventType': 'Adjustments'}, 'adjustments'
        if self.operation_type == 'reimbursements':
            return REPORT_FBA_REIMBURSEMENTS, False, 'reimbursements'
        if self.operation_type == 'removal_status':
            if self.report_kind == 'removal_shipments':
                return REPORT_REMOVAL_SHIPMENT_DETAIL, False, 'removal_shipments'
            return REPORT_REMOVAL_ORDER_DETAIL, False, 'removal_orders'
        raise ValidationError(_("Unsupported Phase 7 report job."))

    def _iso_bounds(self):
        date_from = self.date_from or fields.Date.today() - timedelta(days=30)
        date_to = self.date_to or fields.Date.today()
        start = datetime.combine(date_from, time.min).strftime('%Y-%m-%dT%H:%M:%SZ')
        end = datetime.combine(date_to + timedelta(days=1), time.min).strftime('%Y-%m-%dT%H:%M:%SZ')
        return start, end

    def _report_turn(self):
        report_type, options, report_kind = self._report_configuration()
        api = AmazonAPI()
        if self.stage == 'request':
            token = self.instance_id._get_access_token_or_raise()
            start, end = self._iso_bounds()
            response = self.instance_id._api_call_safe(
                api.create_report, self.instance_id, token, report_type, start, end, options,
                error_msg=_("Failed to request Amazon report %s", report_type),
            )
            report_id = response.get('reportId')
            if not report_id:
                raise UserError(_("Amazon did not return a reportId."))
            self.write({
                'amazon_report_id': report_id, 'report_kind': report_kind,
                'stage': 'poll', 'state': 'waiting_amazon',
                'next_run_at': fields.Datetime.now() + timedelta(minutes=2),
            })
            source = self._source()
            if source and 'amazon_report_id' in source._fields:
                source.write({'amazon_report_id': report_id, 'state': 'requested'})
            return False
        if self.stage == 'poll':
            token = self.instance_id._get_access_token_or_raise()
            response = self.instance_id._api_call_safe(
                api.get_report, self.instance_id, token, self.amazon_report_id,
                error_msg=_("Failed to poll Amazon report %s", self.amazon_report_id),
            )
            status = response.get('processingStatus') or ''
            if status in ('IN_QUEUE', 'IN_PROGRESS'):
                return self._wait(status)
            if status == 'CANCELLED':
                # Officially, CANCELLED can mean the requested interval has no data.
                return self._complete_report_part(no_data=True)
            if status != 'DONE':
                raise UserError(_("Amazon report ended with status %s.", status or _('empty')))
            document_id = response.get('reportDocumentId')
            if not document_id:
                raise UserError(_("Amazon report is DONE but has no reportDocumentId."))
            self.write({'amazon_report_document_id': document_id, 'stage': 'download', 'state': 'running'})
            return False
        if self.stage == 'download':
            token = self.instance_id._get_access_token_or_raise()
            metadata = self.instance_id._api_call_safe(
                api.get_report_document, self.instance_id, token, self.amazon_report_document_id,
                error_msg=_("Failed to retrieve Amazon report document metadata"),
            )
            raw = api.download_report_document(
                metadata.get('url'), compression=metadata.get('compressionAlgorithm'),
                encryption=metadata.get('encryptionDetails'), instance=self.instance_id,
            )
            rows = self._parse_rows(raw)
            self.write({
                'raw_document': json.dumps(rows, default=str), 'total_found': len(rows),
                'cursor_index': 0, 'stage': 'process', 'state': 'running',
                'next_run_at': fields.Datetime.now(),
            })
            return False
        if self.stage == 'process':
            rows = json.loads(self.raw_document or '[]')
            batch = rows[self.cursor_index:self.cursor_index + self.batch_size]
            success = failed = 0
            for row in batch:
                try:
                    with self.env.cr.savepoint():
                        self._process_report_row(row)
                    success += 1
                except Exception as exc:
                    failed += 1
                    _logger.exception("Phase 7 row failed for job %s: %s", self.id, exc)
            cursor = self.cursor_index + len(batch)
            self.write({
                'cursor_index': cursor,
                'total_processed': self.total_processed + success,
                'total_failed': self.total_failed + failed,
                'last_activity_at': fields.Datetime.now(),
            })
            if cursor < len(rows):
                self.write({'state': 'pending', 'next_run_at': fields.Datetime.now()})
                return False
            return self._complete_report_part()
        raise ValidationError(_("Unknown report job stage %s.", self.stage))

    @staticmethod
    def _parse_rows(raw):
        normalized = (raw or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', ' ')
        return list(csv.DictReader(io.StringIO(normalized), delimiter='\t', quoting=csv.QUOTE_NONE))

    def _process_report_row(self, row):
        if self.report_kind == 'returns':
            report = self._source()
            if not report or report._name != 'amazon.return.report':
                report = self.env['amazon.return.report'].create({
                    'instance_id': self.instance_id.id,
                    'report_date': self.date_to or fields.Date.today(),
                    'amazon_report_id': self.amazon_report_id,
                    'state': 'downloaded',
                })
                self.write({'source_model': report._name, 'source_id': report.id})
            line = self.env['amazon.return.report.line'].import_row(report, row)
            line._classify_and_apply()
        elif self.report_kind == 'adjustments':
            self.env['amazon.fba.inventory.adjustment'].import_row(
                self.instance_id, row, self.amazon_report_id
            )
        elif self.report_kind == 'reimbursements':
            self.env['amazon.fba.reimbursement'].import_row(
                self.instance_id, row, self.amazon_report_id
            )
        elif self.report_kind == 'removal_orders':
            order = self.env['amazon.removal.order'].import_detail_row(
                self.instance_id, row, self.amazon_report_id
            )
            if self.source_model == 'amazon.removal.order' and self.source_id != order.id:
                return False
        elif self.report_kind == 'removal_shipments':
            self.env['amazon.removal.shipment'].import_row(
                self.instance_id, row, self.amazon_report_id
            )
        return True

    def _complete_report_part(self, no_data=False):
        if self.operation_type == 'removal_status' and self.report_kind != 'removal_shipments':
            self.write({
                'stage': 'request', 'state': 'pending', 'report_kind': 'removal_shipments',
                'amazon_report_id': False, 'amazon_report_document_id': False,
                'raw_document': False, 'cursor_index': 0,
                'next_run_at': fields.Datetime.now(),
            })
            return False
        source = self._source()
        if source and source._name == 'amazon.return.report':
            source.write({
                'state': 'downloaded', 'amazon_report_document_id': self.amazon_report_document_id,
                'imported_at': fields.Datetime.now(), 'last_error': False,
            })
        cursor_field = {
            'customer_returns': 'last_fba_return_sync_at',
            'removal_status': 'last_fba_removal_sync_at',
            'inventory_adjustments': 'last_fba_adjustment_sync_at',
            'reimbursements': 'last_fba_reimbursement_sync_at',
        }.get(self.operation_type)
        if cursor_field:
            self.instance_id.write({cursor_field: fields.Datetime.now()})
        self.write({
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
            'next_run_at': False, 'raw_document': False,
        })
        return True

    def _submit_removal(self):
        order = self._source()
        if not order or order._name != 'amazon.removal.order':
            raise UserError(_("Removal submission job has no removal order."))
        order._validate_submission()
        api = AmazonAPI()
        token = order.instance_id._get_access_token_or_raise()
        content_type = 'text/tab-separated-values; charset=UTF-8'
        content = api.build_fba_removal_flat_file(order)
        document = order.instance_id._api_call_safe(
            api.create_feed_document, order.instance_id, token, content_type,
            error_msg=_("Failed to create FBA removal feed document"),
        )
        api.upload_feed_document(
            document.get('url'), content, content_type=content_type, instance=order.instance_id,
        )
        response = order.instance_id._api_call_safe(
            api.create_feed, order.instance_id, token, FEED_FBA_CREATE_REMOVAL,
            document.get('feedDocumentId'), error_msg=_("Failed to create FBA removal feed"),
        )
        feed_id = response.get('feedId')
        if not feed_id:
            raise UserError(_("Amazon did not return a feedId."))
        order.write({
            'state': 'feed_processing', 'feed_document_id': document.get('feedDocumentId'),
            'feed_id': feed_id, 'feed_processing_status': 'IN_QUEUE',
            'raw_response': json.dumps(response, default=str, sort_keys=True),
        })
        self.write({
            'feed_document_id': document.get('feedDocumentId'), 'feed_id': feed_id,
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
        })
        self.enqueue(order.instance_id, 'removal_feed_poll', source=order)
        return True

    def _poll_removal_feed(self):
        order = self._source()
        if not order or order._name != 'amazon.removal.order' or not order.feed_id:
            raise UserError(_("Removal feed polling job has no submitted feed."))
        api = AmazonAPI()
        token = order.instance_id._get_access_token_or_raise()
        response = order.instance_id._api_call_safe(
            api.get_feed, order.instance_id, token, order.feed_id,
            error_msg=_("Failed to poll FBA removal feed"),
        )
        status = response.get('processingStatus') or ''
        order.write({
            'feed_processing_status': status,
            'raw_response': json.dumps(response, default=str, sort_keys=True),
        })
        if status in ('IN_QUEUE', 'IN_PROGRESS'):
            return self._wait(status)
        if status != 'DONE':
            order.write({'state': 'failed', 'error_code': status, 'error_message': _('Amazon feed failed.')})
            raise UserError(_("Amazon removal feed ended with %s.", status or _('empty')))
        result_document_id = response.get('resultFeedDocumentId')
        errors = []
        raw_result = ''
        if result_document_id:
            metadata = order.instance_id._api_call_safe(
                api.get_feed_document, order.instance_id, token, result_document_id,
                error_msg=_("Failed to retrieve removal feed processing report"),
            )
            raw_result = api.download_report_document(
                metadata.get('url'), compression=metadata.get('compressionAlgorithm'),
                encryption=metadata.get('encryptionDetails'), instance=order.instance_id,
            )
            errors = self._feed_result_errors(raw_result)
        if errors:
            message = '\n'.join(errors)[:10000]
            order.write({
                'state': 'failed', 'feed_result_document_id': result_document_id,
                'error_code': 'FEED_RECORD_REJECTED', 'error_message': message,
                'raw_response': raw_result,
            })
            raise ValidationError(message)
        order.write({
            'state': 'submitted', 'feed_result_document_id': result_document_id,
            'error_code': False, 'error_message': False,
            'raw_response': raw_result or order.raw_response,
            'requested_at': order.requested_at or fields.Datetime.now(),
        })
        self.write({
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
            'next_run_at': False,
        })
        return True

    @api.model
    def _feed_result_errors(self, raw):
        text = raw or ''
        errors = []
        try:
            root = ElementTree.fromstring(text)
            for result in root.iter():
                if result.tag.split('}')[-1] != 'Result':
                    continue
                values = {child.tag.split('}')[-1]: (child.text or '') for child in result}
                if values.get('ResultCode', '').lower() in ('error', 'fatal'):
                    errors.append('%s: %s' % (
                        values.get('ResultMessageCode') or 'ERROR',
                        values.get('ResultDescription') or values.get('ResultCode'),
                    ))
        except ElementTree.ParseError:
            rows = self._parse_rows(text)
            for row in rows:
                result_code = row.get('result-code') or row.get('ResultCode') or row.get('status-code') or ''
                if str(result_code).strip().lower() in ('error', 'fatal'):
                    errors.append('%s: %s' % (
                        row.get('result-message-code') or row.get('error-code') or 'ERROR',
                        row.get('result-description') or row.get('error-message') or result_code,
                    ))
        return errors

    def _match_reimbursements(self):
        records = self.env['amazon.fba.reimbursement'].search([
            ('instance_id', '=', self.instance_id.id),
            ('review_state', 'in', ('unmatched', 'manual_review')),
        ], order='approval_date, id', limit=self.batch_size)
        for record in records:
            with self.env.cr.savepoint():
                record._match_one()
        if len(records) >= self.batch_size:
            self.write({
                'state': 'pending', 'next_run_at': fields.Datetime.now(),
                'total_processed': self.total_processed + len(records),
            })
            return False
        self.write({
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
            'next_run_at': False, 'total_processed': self.total_processed + len(records),
        })
        return True

    def _wait(self, raw_status):
        retries = self.retry_count + 1
        if retries >= self.max_retries:
            raise UserError(_("Amazon remained in %s beyond the polling limit.", raw_status))
        delay = min(2 ** min(retries, 6), 60)
        self.write({
            'state': 'waiting_amazon', 'retry_count': retries,
            'next_run_at': fields.Datetime.now() + timedelta(minutes=delay),
            'last_error_code': raw_status, 'last_error_message': False,
        })
        return False

    def _fail_or_retry(self, exc):
        retry_count = self.retry_count + 1
        message = str(exc)[:10000]
        retryable = any(token in message.lower() for token in (
            '429', 'throttl', 'timeout', 'connection', 'temporar', '503', '500', '502', '504',
        ))
        if retryable and retry_count < self.max_retries:
            self.write({
                'state': 'pending', 'retry_count': retry_count,
                'next_run_at': fields.Datetime.now() + timedelta(minutes=min(2 ** retry_count, 60)),
                'last_error_code': 'RETRYABLE', 'last_error_message': message,
            })
        else:
            self.write({
                'state': 'failed', 'retry_count': retry_count, 'next_run_at': False,
                'finished_at': fields.Datetime.now(), 'last_error_code': 'PHASE7_ERROR',
                'last_error_message': message,
            })
            source = self._source()
            if source and 'state' in source._fields and source._name in ('amazon.return.report', 'amazon.removal.order'):
                values = {'state': 'failed'}
                if 'last_error' in source._fields:
                    values['last_error'] = message
                if 'error_message' in source._fields:
                    values['error_message'] = message
                source.write(values)
            self.env['amazon.operation.control'].sudo().record_source_failure(self)
            self.env['amazon.smart.alert'].phase7_alert(
                self.instance_id, 'phase7-job:%s' % self.id,
                _("FBA returns/removals job failed"), message,
                source=self, critical=True,
            )
        _logger.exception("Amazon Phase 7 job %s failed: %s", self.id, exc)


class AmazonInstancePhase7(models.Model):
    _inherit = 'amazon.instance'

    def _phase7_window(self, cursor_field, default_days=30):
        self.ensure_one()
        end = fields.Date.today()
        cursor = self[cursor_field]
        start = fields.Date.to_date(cursor) - timedelta(days=2) if cursor else end - timedelta(days=default_days)
        # Thirty days is a conservative connector window, not an Amazon
        # published maximum. Overlap is safe because event keys are idempotent.
        return max(start, end - timedelta(days=30)), end

    def action_import_fba_customer_returns(self):
        self.ensure_one()
        active = self.env['amazon.phase7.job'].search([
            ('instance_id', '=', self.id), ('operation_type', '=', 'customer_returns'),
            ('state', 'in', ('pending', 'running', 'waiting_amazon')),
        ], limit=1)
        if active:
            return self._notify(
                _("Customer Returns"), _("Import job %s is already active.", active.display_name),
                'warning',
            )
        date_from, date_to = self._phase7_window('last_fba_return_sync_at')
        report = self.env['amazon.return.report'].create({
            'instance_id': self.id, 'report_date': date_to,
        })
        job = self.env['amazon.phase7.job'].enqueue(
            self, 'customer_returns', source=report,
            date_from=date_from, date_to=date_to,
        )
        report.state = 'queued'
        return self._notify(_("Customer Returns"), _("Import job %s was queued.", job.display_name))

    def action_import_fba_inventory_adjustments(self):
        self.ensure_one()
        date_from, date_to = self._phase7_window('last_fba_adjustment_sync_at')
        job = self.env['amazon.phase7.job'].enqueue(
            self, 'inventory_adjustments', date_from=date_from, date_to=date_to,
        )
        return self._notify(_("Inventory Adjustments"), _("Import job %s was queued.", job.display_name))

    def action_import_fba_reimbursements(self):
        self.ensure_one()
        date_from, date_to = self._phase7_window('last_fba_reimbursement_sync_at')
        job = self.env['amazon.phase7.job'].enqueue(
            self, 'reimbursements', date_from=date_from, date_to=date_to,
        )
        return self._notify(_("Reimbursements"), _("Import job %s was queued.", job.display_name))

    @api.model
    def _cron_enqueue_phase7(self, operation_type, cursor_field):
        queued = 0
        for instance in self.sudo().search([('active', '=', True)]):
            active = self.env['amazon.phase7.job'].sudo().search_count([
                ('instance_id', '=', instance.id), ('operation_type', '=', operation_type),
                ('state', 'in', ('pending', 'running', 'waiting_amazon')),
            ])
            if active:
                continue
            date_from, date_to = instance._phase7_window(cursor_field)
            source = False
            if operation_type == 'customer_returns':
                source = self.env['amazon.return.report'].sudo().create({
                    'instance_id': instance.id, 'report_date': date_to, 'state': 'queued',
                })
            job = self.env['amazon.phase7.job'].enqueue(
                instance, operation_type, source=source,
                date_from=date_from, date_to=date_to,
            )
            queued += bool(job)
        return queued

    @api.model
    def cron_import_fba_customer_returns(self):
        return self._cron_enqueue_phase7('customer_returns', 'last_fba_return_sync_at')

    @api.model
    def cron_refresh_fba_removal_status(self):
        return self._cron_enqueue_phase7('removal_status', 'last_fba_removal_sync_at')

    @api.model
    def cron_import_fba_inventory_adjustments(self):
        return self._cron_enqueue_phase7('inventory_adjustments', 'last_fba_adjustment_sync_at')

    @api.model
    def cron_import_fba_reimbursements(self):
        return self._cron_enqueue_phase7('reimbursements', 'last_fba_reimbursement_sync_at')

    @api.model
    def cron_match_fba_reimbursements(self):
        count = 0
        for instance in self.sudo().search([('active', '=', True)]):
            self.env['amazon.phase7.job'].enqueue(instance, 'reimbursement_matching')
            count += 1
        return count


class StockPickingAmazonPhase7(models.Model):
    _inherit = 'stock.picking'

    amazon_removal_order_id = fields.Many2one(
        'amazon.removal.order', ondelete='restrict', check_company=True,
        copy=False, readonly=True,
    )
    amazon_removal_shipment_id = fields.Many2one(
        'amazon.removal.shipment', ondelete='restrict', check_company=True,
        copy=False, readonly=True,
    )
    amazon_fba_movement_type = fields.Selection(selection_add=[
        ('return_sellable', 'Customer Return to Amazon Sellable'),
        ('return_unsellable', 'Customer Return to Amazon Unsellable'),
        ('removal_dispatch', 'Amazon FBA to Removal Transit'),
        ('removal_receipt', 'Removal Transit to Customer Warehouse'),
        ('removal_disposal', 'Amazon FBA to Disposal/Loss'),
        ('inventory_lost', 'Amazon Inventory Lost'),
        ('inventory_found', 'Amazon Inventory Found'),
        ('inventory_damaged', 'Amazon Inventory Damaged'),
        ('inventory_destroyed', 'Amazon Inventory Destroyed'),
    ], ondelete={
        'return_sellable': 'set null', 'return_unsellable': 'set null',
        'removal_dispatch': 'set null', 'removal_receipt': 'set null',
        'removal_disposal': 'set null', 'inventory_lost': 'set null',
        'inventory_found': 'set null', 'inventory_damaged': 'set null',
        'inventory_destroyed': 'set null',
    })

    def button_validate(self):
        result = super().button_validate()
        for picking in self.filtered(lambda rec: (
            rec.state == 'done' and rec.amazon_fba_movement_type == 'removal_receipt'
            and rec.amazon_removal_order_id
        )):
            order = picking.amazon_removal_order_id
            shipment = picking.amazon_removal_shipment_id
            received = sum(picking.move_ids.mapped('quantity'))
            line = shipment.line_id if shipment else order.line_ids.filtered(
                lambda item: item.odoo_product_id in picking.move_ids.product_id
            )[:1]
            if line:
                line.received_quantity = min(
                    line.shipped_quantity,
                    line.received_quantity + received,
                )
            total_received = sum(order.line_ids.mapped('received_quantity'))
            total_shipped = sum(order.line_ids.mapped('shipped_quantity'))
            order.write({
                'stock_action_state': 'received' if total_received >= total_shipped else 'partially_received',
                'state': 'completed' if total_shipped and total_received >= total_shipped else 'awaiting_receipt',
            })
        return result
