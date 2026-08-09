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
    REPORT_SETTLEMENT,
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
        ('phase7_job_failed', 'FBA / Settlement Job Failed'),
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
        """Resolve an existing mapping without ever creating a product.

        Seller SKU is the connector's authoritative direct key.  FNSKU is not
        stored on ``amazon.product``, so it is used only through existing,
        instance-scoped inventory evidence.  ASIN is accepted only when it
        identifies one Amazon product for the seller instance.
        """
        sku = str(sku or '').strip()
        fnsku = str(fnsku or '').strip()
        asin = str(asin or '').strip()
        product_model = self.env['amazon.product']
        amazon_product = product_model

        if sku:
            candidates = product_model.search([
                ('instance_id', '=', instance.id), ('sku', '=', sku),
            ], limit=2)
            if len(candidates) == 1:
                amazon_product = candidates

        if not amazon_product and fnsku:
            # Reuse the FNSKU mappings already observed by inventory
            # reconciliation and the detailed inventory ledger.
            mapped_products = self.env['amazon.product']
            reconciliation_rows = self.env['amazon.inventory.reconciliation'].search([
                ('instance_id', '=', instance.id), ('fnsku', '=', fnsku),
                ('amazon_product_id', '!=', False),
            ], order='run_date desc, id desc', limit=20)
            mapped_products |= reconciliation_rows.mapped('amazon_product_id')
            adjustment_rows = self.env['amazon.fba.inventory.adjustment'].search([
                ('instance_id', '=', instance.id), ('fnsku', '=', fnsku),
                ('amazon_product_id', '!=', False),
            ], order='event_date desc, id desc', limit=20)
            mapped_products |= adjustment_rows.mapped('amazon_product_id')
            if len(mapped_products) == 1:
                amazon_product = mapped_products

        if not amazon_product and asin:
            candidates = product_model.search([
                ('instance_id', '=', instance.id), ('asin', '=', asin),
            ], limit=2)
            if len(candidates) == 1:
                amazon_product = candidates
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
        """Keep return reports inventory-informational.

        The FBA customer-return report proves that Amazon received a returned
        unit and reports its disposition, but applying that row as stock in
        addition to the FBA Inventory snapshot can count one unit twice.
        Inventory changes therefore remain in the reviewed reconciliation flow.
        """
        if event.inventory_reflected:
            event.stock_action_state = 'already_reflected'
            return False
        event.stock_action_state = 'audit_only'
        return False

    @api.model
    def _removal_source_location(self, instance, disposition):
        normalized = (disposition or '').strip().lower()
        if normalized == 'sellable':
            return instance.fba_sellable_location_id
        if normalized in ('unsellable', 'unfulfillable'):
            return instance.fba_unsellable_location_id
        return self.env['stock.location']

    @api.model
    def _tracked_quantity(self, product, location, company):
        if not product or not location:
            return 0.0
        return product.sudo().with_company(company).with_context(location=location.id).qty_available

    @api.model
    def _removal_discrepancy(self, shipment, code, message):
        shipment.write({
            'stock_action_state': 'manual_review',
            'discrepancy_code': code,
            'discrepancy_message': message,
        })
        shipment.order_id.write({
            'manual_review_required': True,
            'stock_action_state': 'manual_review',
            'discrepancy_code': code,
            'discrepancy_message': message,
        })
        self.env['amazon.smart.alert'].phase7_alert(
            shipment.instance_id,
            'removal-shipment:%s:%s' % (shipment.id, code),
            _("FBA removal shipment requires review"),
            message,
            source=shipment,
            product=shipment.line_id.amazon_product_id,
        )

    @api.model
    def apply_removal_shipment(self, shipment, reviewed=False):
        """Record shipment evidence; move stock only after an explicit review.

        Amazon inventory summaries can reflect the same physical departure
        before the removal report is imported. Automatic report-driven moves
        would then reduce FBA inventory twice. The import path is therefore
        audit-only; the shipment button is the deliberate event-level path
        when its documented disposition and current Odoo source stock agree.
        """
        line = shipment.line_id
        order = shipment.order_id
        if not reviewed:
            shipment.stock_action_state = 'audit_only'
            if order.stock_action_state != 'manual_review':
                order.stock_action_state = 'audit_only'
            return False

        self.env.cr.execute(
            'SELECT id FROM amazon_removal_shipment WHERE id = %s FOR UPDATE',
            [shipment.id],
        )
        shipment.invalidate_recordset()
        quantity = max(shipment.shipped_quantity - shipment.dispatched_stock_quantity, 0.0)
        if not quantity:
            return shipment.dispatch_move_id.picking_id if shipment.dispatch_move_id else False
        if order.removal_type != 'return_to_address':
            self._removal_discrepancy(
                shipment, 'NOT_RETURN_TO_SELLER',
                _("Only return-to-seller shipments can move to Removal Transit."),
            )
            return False
        if not line.odoo_product_id:
            self._removal_discrepancy(
                shipment, 'UNMAPPED_SKU',
                _("Removal SKU %s is not mapped to an inventory product.", shipment.sku),
            )
            return False
        if (
            line.requested_quantity > 0
            and line.odoo_product_id.uom_id.compare(
                shipment.shipped_quantity, line.requested_quantity,
            ) > 0
        ):
            self._removal_discrepancy(
                shipment, 'REMOVAL_EXCEEDS_REQUESTED_QUANTITY',
                _(
                    "Amazon reports %s shipped units for a line that requested %s. "
                    "No stock move was created.",
                    shipment.shipped_quantity, line.requested_quantity,
                ),
            )
            return False
        source = self._removal_source_location(
            order.instance_id, shipment.disposition or line.disposition,
        )
        if not source:
            self._removal_discrepancy(
                shipment, 'UNKNOWN_SOURCE_DISPOSITION',
                _(
                    "Amazon disposition %s does not identify Sellable or Unsellable stock; "
                    "use Inventory Reconciliation instead.",
                    shipment.disposition or line.disposition or _('empty'),
                ),
            )
            return False
        available = self._tracked_quantity(line.odoo_product_id, source, order.company_id)
        if line.odoo_product_id.uom_id.compare(available, quantity) < 0:
            self._removal_discrepancy(
                shipment, 'REMOVAL_EXCEEDS_TRACKED_STOCK',
                _(
                    "Amazon reports %s newly shipped units, but Odoo tracks only %s in %s. "
                    "No stock move was created; the Amazon snapshot may already reflect this removal.",
                    quantity, available, source.display_name,
                ),
            )
            return False
        try:
            dispatch = self._create_picking(
                order.instance_id, line.odoo_product_id, quantity,
                source, order.instance_id.fba_removal_transit_location_id,
                order.removal_order_id, 'removal_dispatch', True,
                removal_order=order, removal_shipment=shipment,
            )
            shipment.write({
                'dispatch_move_id': dispatch.move_ids.id,
                'dispatch_picking_ids': [Command.link(dispatch.id)],
                'dispatched_stock_quantity': shipment.dispatched_stock_quantity + quantity,
                'stock_action_state': 'in_transit',
                'discrepancy_code': False,
                'discrepancy_message': False,
            })
            line.write({
                'dispatch_move_id': dispatch.move_ids.id,
                'dispatched_stock_quantity': line.dispatched_stock_quantity + quantity,
            })
            order.write({
                'picking_ids': [Command.link(dispatch.id)],
                'stock_action_state': 'awaiting_receipt', 'state': 'awaiting_receipt',
                'discrepancy_code': False, 'discrepancy_message': False,
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
        # Disposal detail is operational evidence. Inventory snapshots can
        # already include the same decrease, so imports never create a second
        # stock reduction. Reconciliation remains the stock authority.
        if not line.odoo_product_id:
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'discrepancy_code': 'UNMAPPED_SKU',
                'discrepancy_message': _("Disposal SKU %s is not mapped.", line.sku),
            })
            return False
        if (
            line.requested_quantity > 0
            and line.odoo_product_id.uom_id.compare(
                line.disposed_quantity, line.requested_quantity,
            ) > 0
        ):
            line.write({
                'discrepancy_code': 'DISPOSAL_EXCEEDS_REQUESTED_QUANTITY',
                'discrepancy_message': _(
                    "Amazon reports %s disposed units for a line that requested %s. "
                    "No stock move was created.",
                    line.disposed_quantity, line.requested_quantity,
                ),
            })
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'discrepancy_code': line.discrepancy_code,
                'discrepancy_message': line.discrepancy_message,
            })
            return False
        source = self._removal_source_location(order.instance_id, line.disposition)
        if not source:
            line.write({
                'discrepancy_code': 'UNKNOWN_SOURCE_DISPOSITION',
                'discrepancy_message': _(
                    "Amazon disposition %s cannot be mapped safely to an FBA location.",
                    line.disposition or _('empty'),
                ),
            })
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'discrepancy_code': line.discrepancy_code,
                'discrepancy_message': line.discrepancy_message,
            })
            return False
        available = self._tracked_quantity(line.odoo_product_id, source, order.company_id)
        if line.odoo_product_id.uom_id.compare(available, line.disposed_quantity) < 0:
            line.write({
                'discrepancy_code': 'DISPOSAL_EXCEEDS_TRACKED_STOCK',
                'discrepancy_message': _(
                    "Amazon reports %s disposed units, but Odoo tracks only %s in %s. "
                    "No stock move was created.",
                    line.disposed_quantity, available, source.display_name,
                ),
            })
            order.write({
                'manual_review_required': True, 'stock_action_state': 'manual_review',
                'discrepancy_code': line.discrepancy_code,
                'discrepancy_message': line.discrepancy_message,
            })
            return False
        order.stock_action_state = 'audit_only'
        return False

    @api.model
    def create_removal_receipt(self, shipment):
        shipment.ensure_one()
        order = shipment.order_id
        instance = order.instance_id
        if order.removal_type != 'return_to_address':
            raise UserError(_("Disposal orders never create customer warehouse receipts."))
        package_shipments = shipment._package_shipments()
        existing = package_shipments.mapped('receipt_picking_id')
        if existing:
            if len(existing) > 1:
                raise UserError(_("This Amazon package is linked to conflicting Odoo receipts."))
            if existing.state == 'cancel':
                raise UserError(_("The existing customer receipt is cancelled; review it manually."))
            package_shipments.filtered(lambda item: not item.receipt_picking_id).write({
                'receipt_picking_id': existing.id,
            })
            return existing
        if not instance.fba_removal_transit_location_id or not instance.fba_source_location_id:
            raise UserError(_("Configure Removal Transit and the customer warehouse destination."))

        quantities = {}
        for item in package_shipments:
            product = item.line_id.odoo_product_id
            if not product or not product.is_storable:
                raise UserError(_("Every shipment row must map to an inventory-tracked product."))
            if product.company_id and product.company_id != order.company_id:
                raise UserError(_("A shipment product belongs to another company."))
            if item.shipped_quantity <= 0:
                raise UserError(_("Amazon has not reported a positive shipped quantity."))
            quantities[product] = quantities.get(product, 0.0) + item.shipped_quantity

        source = instance.fba_removal_transit_location_id
        destination = instance.fba_source_location_id
        picking = self.env['stock.picking'].sudo().with_company(order.company_id).create({
            'picking_type_id': self._internal_picking_type(
                order.company_id, source, destination,
            ).id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'company_id': order.company_id.id,
            'origin': '%s%s' % (
                order.removal_order_id,
                (' / %s' % shipment.tracking_number) if shipment.tracking_number else '',
            ),
            'amazon_instance_id': instance.id,
            'amazon_fba_movement_type': 'removal_receipt',
            'amazon_removal_order_id': order.id,
            'amazon_removal_shipment_id': shipment.id,
            'move_type': 'one',
            'note': _(
                "Warehouse verification is required. Amazon tracking or delivery status does not validate this receipt."
            ),
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
                'company_id': order.company_id.id,
                'origin': order.removal_order_id,
            }) for product, quantity in quantities.items()],
        })
        picking.action_confirm()
        package_shipments.write({
            'receipt_picking_id': picking.id,
            'stock_action_state': 'awaiting_receipt',
        })
        for line in package_shipments.mapped('line_id'):
            move = picking.move_ids.filtered(lambda item: item.product_id == line.odoo_product_id)[:1]
            if move:
                line.receipt_move_id = move.id
        order.write({
            'picking_ids': [Command.link(picking.id)],
            'state': 'awaiting_receipt',
            'stock_action_state': 'awaiting_receipt',
        })
        return picking

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
    marketplace_id = fields.Char(
        related='instance_id.marketplace_id', store=True, readonly=True, index=True,
    )
    event_key = fields.Char(
        string='Internal Reimbursement Event Key', required=True,
        readonly=True, copy=False, index=True,
    )
    line_key = fields.Char(
        string='Reimbursement Line Key', related='event_key',
        store=True, readonly=True, index=True,
        help="Deterministic identity for one item row within an Amazon reimbursement ID.",
    )
    reimbursement_id = fields.Char(
        string='Amazon Reimbursement ID', required=True, index=True, tracking=True,
    )
    case_id = fields.Char(index=True)
    approval_date = fields.Datetime(index=True)
    reason_raw = fields.Char(string='Amazon Reason', index=True, tracking=True)
    reimbursement_reason = fields.Char(index=True, tracking=True)
    reimbursement_classification = fields.Selection([
        ('lost_inventory', 'Lost Inventory'), ('damaged_inventory', 'Damaged Inventory'),
        ('customer_return', 'Customer Return'), ('removal', 'Removal'),
        ('reversal', 'Reversal'), ('other', 'Other/Unknown'),
    ], default='other', required=True, index=True)
    amazon_order_id = fields.Char(string='Amazon Order ID', index=True)
    amazon_order_item_id = fields.Char(
        string='Legacy Amazon Order Item ID', index=True,
        help="Upgrade-only compatibility field. It is not part of the current FBA Reimbursements Report and is never populated by this importer.",
    )
    sku = fields.Char(index=True)
    fnsku = fields.Char(index=True)
    asin = fields.Char(index=True)
    product_name = fields.Char()
    condition = fields.Char(index=True)
    amazon_product_id = fields.Many2one('amazon.product', ondelete='set null', index=True)
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    product_mapping_state = fields.Selection([
        ('mapped', 'Mapped'), ('unmapped', 'Unmapped Product'),
    ], default='unmapped', required=True, readonly=True, index=True)
    amazon_order_record_id = fields.Many2one(
        'amazon.sale.order', string='Linked Amazon Order', ondelete='set null', index=True,
    )
    order_link_state = fields.Selection([
        ('not_applicable', 'No Order Reported'), ('linked', 'Order Linked'),
        ('order_not_found', 'Order Not Found'),
    ], default='not_applicable', required=True, readonly=True, index=True)
    quantity_reimbursed_cash = fields.Float()
    quantity_reimbursed_inventory = fields.Float()
    quantity_reimbursed_total = fields.Float(
        help="Exact quantity-reimbursed-total reported by Amazon; it is not computed locally.",
    )
    has_reported_quantity_total = fields.Boolean(readonly=True)
    quantity_anomaly = fields.Boolean(readonly=True, index=True)
    amount_per_unit = fields.Monetary(currency_field='currency_id')
    amount_total = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', ondelete='restrict')
    currency_code = fields.Char()
    amount_currency_anomaly = fields.Boolean(readonly=True, index=True)
    original_reimbursement_id = fields.Char(string='Original Reimbursement ID', index=True)
    original_reimbursement_type = fields.Char()
    original_reimbursement_record_id = fields.Many2one(
        'amazon.fba.reimbursement', string='Linked Original Reimbursement',
        ondelete='restrict', index=True, check_company=True,
    )
    reversal_reimbursement_ids = fields.One2many(
        'amazon.fba.reimbursement', 'original_reimbursement_record_id',
        string='Related Reimbursements / Reversals', readonly=True,
    )
    original_link_state = fields.Selection([
        ('not_applicable', 'Not Applicable'), ('linked', 'Original Linked'),
        ('not_found', 'Original Not Found'), ('ambiguous', 'Original Ambiguous'),
    ], default='not_applicable', required=True, readonly=True, index=True)
    linked_return_id = fields.Many2one('amazon.return.report.line', ondelete='set null', check_company=True)
    linked_adjustment_id = fields.Many2one('amazon.fba.inventory.adjustment', ondelete='set null', check_company=True)
    linked_inventory_event_id = fields.Many2one(
        'amazon.fba.inventory.adjustment', string='Matched Inventory Event',
        related='linked_adjustment_id', store=True, readonly=False,
    )
    linked_removal_order_id = fields.Many2one('amazon.removal.order', ondelete='set null', check_company=True)
    linked_settlement_id = fields.Many2one('amazon.settlement.report', ondelete='set null')
    accounting_state = fields.Selection([
        ('not_ready', 'Not Ready'), ('ready_for_phase8', 'Ready for Phase 8'),
    ], default='not_ready', required=True, index=True)
    review_state = fields.Selection([
        ('unmatched', 'Unmatched'), ('matched', 'Matched'),
        ('manual_review', 'Manual Review'), ('ignored', 'Ignored'),
    ], default='unmatched', required=True, index=True, tracking=True,
       help="Legacy review state retained for upgrade compatibility.")
    matching_state = fields.Selection([
        ('unmatched', 'Unmatched'), ('matched', 'Matched'),
        ('ambiguous', 'Ambiguous'), ('manually_matched', 'Manually Matched'),
        ('ignored', 'Ignored'),
    ], default='unmatched', required=True, index=True, tracking=True)
    financial_state = fields.Selection([
        ('imported', 'Imported'), ('unmatched', 'Unmatched'),
        ('matched', 'Matched'), ('ready_for_finance', 'Ready for Finance'),
        ('posted_later', 'Posted Later'),
    ], default='imported', required=True, index=True, tracking=True,
       help="Reserved for the later settlement/accounting phase. Importing does not post anything.")
    match_method = fields.Char(readonly=True)
    matching_explanation = fields.Text(readonly=True)
    requires_review = fields.Boolean(readonly=True, index=True)
    review_note = fields.Text()
    source_report_id = fields.Char(index=True)
    raw_report_reference = fields.Char(index=True)
    raw_report_row = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    raw_report_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    imported_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    last_synced_at = fields.Datetime(readonly=True)
    last_match_attempt_at = fields.Datetime(readonly=True, index=True)
    amazon_order_count = fields.Integer(compute='_compute_link_counts')
    sale_order_count = fields.Integer(compute='_compute_link_counts')
    return_count = fields.Integer(compute='_compute_link_counts')
    adjustment_count = fields.Integer(compute='_compute_link_counts')
    removal_order_count = fields.Integer(compute='_compute_link_counts')
    original_reimbursement_count = fields.Integer(compute='_compute_link_counts')
    reversal_reimbursement_count = fields.Integer(compute='_compute_link_counts')
    sync_log_count = fields.Integer(compute='_compute_link_counts')

    _event_unique = models.Constraint(
        'UNIQUE(instance_id, event_key)', 'This Amazon reimbursement row was already imported.',
    )

    def _amazon_orders(self):
        self.ensure_one()
        if self.amazon_order_record_id:
            return self.amazon_order_record_id
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
            record.original_reimbursement_count = bool(record.original_reimbursement_record_id)
            record.reversal_reimbursement_count = len(record.reversal_reimbursement_ids)
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

    def action_view_amazon_product(self):
        return self._open_linked_records(
            _('Amazon Product'), 'amazon.product', self.amazon_product_id,
        )

    def action_view_odoo_product(self):
        return self._open_linked_records(
            _('Odoo Product'), 'product.product', self.odoo_product_id,
        )

    def action_view_original_reimbursement(self):
        return self._open_linked_records(
            _('Original Reimbursement'), self._name, self.original_reimbursement_record_id,
        )

    def action_view_reversal_reimbursements(self):
        return self._open_linked_records(
            _('Related Reimbursements / Reversals'), self._name,
            self.reversal_reimbursement_ids,
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

    @api.model
    def _classification(self, reason, original_type):
        text = ('%s %s' % (reason or '', original_type or '')).lower()
        if 'reversal' in text:
            return 'reversal'
        if any(token in text for token in ('removal', 'destroy', 'dispos')):
            return 'removal'
        if 'return' in text:
            return 'customer_return'
        if 'damage' in text:
            return 'damaged_inventory'
        if 'lost' in text or 'warehouse' in text:
            return 'lost_inventory'
        return 'other'

    @api.model
    def _normalize_row(self, row):
        return {
            str(key or '').strip().lstrip('\ufeff').lower().replace('_', '-').replace(' ', '-'): (
                value.strip() if isinstance(value, str) else value
            )
            for key, value in (row or {}).items()
            if key and key != '_extra_fields'
        }

    @api.model
    def _line_key_for_row(self, instance, row):
        """Build a stable item identity without mutable amount/quantity values.

        Amazon documents reimbursement-id as the reimbursement identifier, but
        the report is itemized and the same ID can span multiple products.  The
        documented item/order/original identifiers therefore form the line
        identity.  Reported money and quantities remain mutable upsert data.
        """
        components = [
            instance.id, row.get('reimbursement-id'), row.get('case-id'),
            row.get('amazon-order-id'), row.get('sku'), row.get('fnsku'),
            row.get('asin'),
            row.get('product-name') if not any(
                row.get(key) for key in ('sku', 'fnsku', 'asin')
            ) else '',
            row.get('reason'),
            row.get('condition'),
            row.get('original-reimbursement-id'), row.get('original-reimbursement-type'),
        ]
        return hashlib.sha256(
            '|'.join(str(value or '').strip() for value in components).encode()
        ).hexdigest()

    @api.model
    def _legacy_row_candidates(self, instance, row):
        domain = [
            ('instance_id', '=', instance.id),
            ('reimbursement_id', '=', row.get('reimbursement-id')),
            ('sku', '=', row.get('sku') or ''),
            ('fnsku', '=', row.get('fnsku') or ''),
            ('asin', '=', row.get('asin') or ''),
            ('amazon_order_id', '=', row.get('amazon-order-id') or ''),
            ('case_id', '=', row.get('case-id') or ''),
            ('reimbursement_reason', '=', row.get('reason') or ''),
            ('original_reimbursement_id', '=', row.get('original-reimbursement-id') or ''),
        ]
        return self.search(domain, limit=2)

    @api.model
    def _order_values(self, instance, amazon_order_id):
        amazon_order_id = str(amazon_order_id or '').strip()
        if not amazon_order_id:
            return {'amazon_order_record_id': False, 'order_link_state': 'not_applicable'}
        order = self.env['amazon.sale.order'].search([
            ('instance_id', '=', instance.id), ('amazon_order_ref', '=', amazon_order_id),
        ], limit=1)
        return {
            'amazon_order_record_id': order.id or False,
            'order_link_state': 'linked' if order else 'order_not_found',
        }

    def _base_review_issues(self):
        self.ensure_one()
        issues = []
        if self.product_mapping_state == 'unmapped':
            issues.append(_("UNMAPPED PRODUCT: %s", self.sku or self.fnsku or self.asin or _('empty')))
        if self.order_link_state == 'order_not_found':
            issues.append(_("ORDER NOT FOUND: %s", self.amazon_order_id))
        if self.quantity_anomaly:
            if not self.has_reported_quantity_total:
                issues.append(_("QUANTITY ANOMALY: Amazon omitted quantity-reimbursed-total."))
            else:
                issues.append(_(
                    "QUANTITY ANOMALY: reported total %s differs from cash %s plus inventory %s.",
                    self.quantity_reimbursed_total, self.quantity_reimbursed_cash,
                    self.quantity_reimbursed_inventory,
                ))
        if self.amount_currency_anomaly:
            issues.append(_(
                "AMOUNT/CURRENCY ANOMALY: %s is not available for the reported amount.",
                self.currency_code or _('currency code'),
            ))
        if self.original_link_state == 'not_found':
            issues.append(_(
                "ORIGINAL REIMBURSEMENT NOT FOUND: %s", self.original_reimbursement_id,
            ))
        elif self.original_link_state == 'ambiguous':
            issues.append(_(
                "ORIGINAL REIMBURSEMENT AMBIGUOUS: %s", self.original_reimbursement_id,
            ))
        return issues

    def _write_review(self, matching_issue=False):
        self.ensure_one()
        issues = self._base_review_issues()
        if matching_issue:
            issues.append(matching_issue)
        self.write({
            'requires_review': bool(issues),
            'review_note': '\n'.join(issues) or False,
        })
        return issues

    def _original_candidates(self):
        self.ensure_one()
        if not self.original_reimbursement_id:
            return self.env[self._name]
        candidates = self.search([
            ('instance_id', '=', self.instance_id.id),
            ('reimbursement_id', '=', self.original_reimbursement_id),
            ('id', '!=', self.id),
        ], limit=20)
        for field_name in ('fnsku', 'sku', 'asin'):
            value = self[field_name]
            if value:
                exact = candidates.filtered(lambda item: item[field_name] == value)
                if exact:
                    return exact
        return candidates

    def _resolve_original_relationship(self):
        self.ensure_one()
        if not self.original_reimbursement_id:
            self.write({
                'original_reimbursement_record_id': False,
                'original_link_state': 'not_applicable',
            })
            return self.env[self._name]
        candidates = self._original_candidates()
        if len(candidates) == 1:
            self.write({
                'original_reimbursement_record_id': candidates.id,
                'original_link_state': 'linked',
            })
            return candidates
        self.write({
            'original_reimbursement_record_id': False,
            'original_link_state': 'ambiguous' if candidates else 'not_found',
        })
        return self.env[self._name]

    @api.model
    def import_row(self, instance, row, report_reference=False):
        row = self._normalize_row(row)
        reimbursement_id = row.get('reimbursement-id') or ''
        missing = []
        if not reimbursement_id:
            missing.append('reimbursement-id')
        if not row.get('approval-date'):
            missing.append('approval-date')
        if missing:
            raise ValidationError(_(
                "Malformed FBA reimbursement row; missing %s.", ', '.join(missing),
            ))
        key = self._line_key_for_row(instance, row)
        reimbursement = self.search([
            ('instance_id', '=', instance.id), ('event_key', '=', key),
        ], limit=1)
        if not reimbursement:
            legacy = self._legacy_row_candidates(instance, row)
            reimbursement = legacy if len(legacy) == 1 else self.env[self._name]
        currency_code = (row.get('currency-unit') or '').strip()
        currency = self.env['res.currency'].with_context(active_test=False).search([
            ('name', '=', currency_code),
        ], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            instance, row.get('sku'), row.get('fnsku'), row.get('asin')
        )
        cash_quantity = self.env['amazon.phase7.stock.service'].number(
            row.get('quantity-reimbursed-cash')
        )
        inventory_quantity = self.env['amazon.phase7.stock.service'].number(
            row.get('quantity-reimbursed-inventory')
        )
        has_total = row.get('quantity-reimbursed-total') not in (None, '')
        total_quantity = self.env['amazon.phase7.stock.service'].number(
            row.get('quantity-reimbursed-total')
        )
        quantity_anomaly = (
            not has_total
            or abs(total_quantity - cash_quantity - inventory_quantity) > 0.00001
        )
        amount_total = self.env['amazon.phase7.stock.service'].number(row.get('amount-total'))
        diagnostic = json.dumps(row, default=str, sort_keys=True)[:20000]
        vals = {
            'instance_id': instance.id, 'event_key': key,
            'reimbursement_id': reimbursement_id,
            'case_id': row.get('case-id') or '',
            'approval_date': self.env['amazon.phase7.stock.service'].datetime(row.get('approval-date')),
            'reason_raw': row.get('reason') or '',
            'reimbursement_reason': row.get('reason') or '',
            'reimbursement_classification': self._classification(
                row.get('reason'), row.get('original-reimbursement-type')
            ),
            'amazon_order_id': row.get('amazon-order-id') or '',
            'sku': row.get('sku') or '', 'fnsku': row.get('fnsku') or '',
            'asin': row.get('asin') or '', 'product_name': row.get('product-name') or '',
            'condition': row.get('condition') or '',
            'product_mapping_state': (
                'mapped'
                if product_values.get('amazon_product_id') and product_values.get('odoo_product_id')
                else 'unmapped'
            ),
            'quantity_reimbursed_cash': cash_quantity,
            'quantity_reimbursed_inventory': inventory_quantity,
            'quantity_reimbursed_total': total_quantity,
            'has_reported_quantity_total': has_total,
            'quantity_anomaly': quantity_anomaly,
            'amount_per_unit': self.env['amazon.phase7.stock.service'].number(row.get('amount-per-unit')),
            'amount_total': amount_total,
            'currency_id': currency.id or False, 'currency_code': currency_code,
            'amount_currency_anomaly': bool(amount_total and (not currency_code or not currency)),
            'original_reimbursement_id': row.get('original-reimbursement-id') or '',
            'original_reimbursement_type': row.get('original-reimbursement-type') or '',
            'source_report_id': report_reference,
            'raw_report_reference': report_reference,
            'raw_report_row': diagnostic,
            'raw_report_data': diagnostic,
            'last_synced_at': fields.Datetime.now(),
            **self._order_values(instance, row.get('amazon-order-id')),
            **product_values,
        }
        if reimbursement:
            for protected in (
                'instance_id', 'matching_state', 'review_state', 'financial_state',
                'linked_return_id', 'linked_adjustment_id', 'linked_removal_order_id',
                'match_method', 'matching_explanation',
            ):
                vals.pop(protected, None)
            reimbursement.write(vals)
        else:
            reimbursement = self.create({
                **vals, 'imported_at': fields.Datetime.now(), 'financial_state': 'imported',
            })
        reimbursement._resolve_original_relationship()
        reimbursement._write_review()
        if reimbursement.requires_review:
            self.env['amazon.smart.alert'].phase7_alert(
                instance, 'reimbursement-review:%s' % reimbursement.event_key,
                _("FBA reimbursement requires review"), reimbursement.review_note,
                source=reimbursement, product=reimbursement.amazon_product_id,
            )
        return reimbursement

    def action_match(self):
        for reimbursement in self:
            reimbursement._match_one()
        return True

    def action_confirm_manual_match(self):
        for reimbursement in self:
            event = reimbursement.linked_inventory_event_id
            if not event:
                raise ValidationError(_("Select an inventory event before confirming a manual match."))
            reimbursement._set_match(
                'linked_adjustment_id', event, 'manual_inventory_event',
                _("Manually matched to inventory event %s by %s.", event.display_name, self.env.user.display_name),
                manual=True,
            )
        return True

    @api.constrains(
        'original_reimbursement_record_id', 'linked_adjustment_id',
        'linked_return_id', 'linked_removal_order_id', 'amazon_order_record_id',
    )
    def _check_instance_links(self):
        for reimbursement in self:
            for record in (
                reimbursement.original_reimbursement_record_id,
                reimbursement.linked_adjustment_id,
                reimbursement.linked_return_id,
                reimbursement.linked_removal_order_id,
                reimbursement.amazon_order_record_id,
            ):
                if record and record.instance_id != reimbursement.instance_id:
                    raise ValidationError(_("A reimbursement cannot link across Amazon instances."))

    def _set_match(self, field_name, record, method, explanation=False, manual=False):
        self.ensure_one()
        if not record or record.instance_id != self.instance_id:
            raise ValidationError(_("A reimbursement cannot link across Amazon instances."))
        state = 'manually_matched' if manual else 'matched'
        self.write({
            field_name: record.id, 'review_state': 'matched',
            'matching_state': state, 'match_method': method,
            'matching_explanation': explanation or method.replace('_', ' ').title(),
        })
        if field_name == 'linked_adjustment_id' and not record.linked_reimbursement_id:
            record.linked_reimbursement_id = self
        self._write_review()
        return True

    def _same_product_as_event(self, event):
        self.ensure_one()
        for field_name in ('fnsku', 'sku', 'asin'):
            reimbursement_value = (self[field_name] or '').strip()
            event_value = (event[field_name] or '').strip()
            if reimbursement_value and event_value:
                return reimbursement_value == event_value
        return bool(
            self.amazon_product_id and event.amazon_product_id
            and self.amazon_product_id == event.amazon_product_id
        )

    def _quantity_matches_event(self, event):
        self.ensure_one()
        return bool(
            self.has_reported_quantity_total and not self.quantity_anomaly
            and abs(abs(event.quantity) - abs(self.quantity_reimbursed_total)) < 0.00001
        )

    def _allowed_event_categories(self):
        self.ensure_one()
        return {
            'lost_inventory': {'lost'},
            'damaged_inventory': {'damaged'},
            'removal': {'destroyed'},
            'reversal': {'found'},
        }.get(self.reimbursement_classification, set())

    def _filter_inventory_events(self, records, require_quantity=False):
        self.ensure_one()
        allowed = self._allowed_event_categories()
        if not allowed:
            return self.env['amazon.fba.inventory.adjustment']
        return records.filtered(lambda event: (
            event.event_category in allowed
            and self._same_product_as_event(event)
            and (not require_quantity or self._quantity_matches_event(event))
        ))

    def _match_found_reversal(self):
        self.ensure_one()
        original = self.original_reimbursement_record_id
        loss = original.linked_adjustment_id if original else self.env['amazon.fba.inventory.adjustment']
        if not loss or loss.event_category != 'lost':
            return False
        candidates = self.env['amazon.fba.inventory.adjustment'].search([
            ('instance_id', '=', self.instance_id.id),
            ('event_category', '=', 'found'),
            ('reversal_of_adjustment_id', '=', loss.id),
        ], limit=3)
        candidates = candidates.filtered(lambda event: (
            self._same_product_as_event(event)
            and (not self.has_reported_quantity_total or self._quantity_matches_event(event))
        ))
        if len(candidates) == 1:
            return self._set_match(
                'linked_adjustment_id', candidates,
                'original_reimbursement_found_event',
                _("Matched by original reimbursement + linked Lost event + Found reversal event."),
            )
        if len(candidates) > 1:
            return self._ambiguous(_(
                "Multiple Found events reverse the Lost event linked to the original reimbursement."
            ))
        return False

    def _match_one(self):
        self.ensure_one()
        if self.matching_state in ('matched', 'manually_matched', 'ignored'):
            return True
        self.last_match_attempt_at = fields.Datetime.now()
        instance_domain = [('instance_id', '=', self.instance_id.id)]
        self._resolve_original_relationship()
        if self.reimbursement_classification == 'reversal' and self._match_found_reversal():
            return True

        # Strong ledger references, with compatible reason and product evidence.
        references = list(dict.fromkeys(filter(None, (
            self.case_id, self.reimbursement_id, self.amazon_order_id,
        ))))
        if references:
            candidates = self.env['amazon.fba.inventory.adjustment'].search(instance_domain + [
                ('reference_id', 'in', references),
            ], limit=10)
            candidates = self._filter_inventory_events(candidates)
            if len(candidates) == 1:
                return self._set_match(
                    'linked_adjustment_id', candidates, 'inventory_reference_product_reason',
                    _("Matched by inventory ReferenceID + product identity + compatible reason."),
                )
            if len(candidates) > 1:
                return self._ambiguous(_(
                    "Multiple inventory events match the reference, product, and reason."
                ))

        # Customer-return reimbursements need order, product, and quantity.
        if self.amazon_order_id and self.reimbursement_classification == 'customer_return':
            candidates = self.env['amazon.return.report.line'].search(instance_domain + [
                ('amazon_order_id', '=', self.amazon_order_id),
            ], limit=20).filtered(lambda event: (
                self._same_product_as_event(event)
                and self.has_reported_quantity_total and not self.quantity_anomaly
                and abs(abs(event.quantity) - abs(self.quantity_reimbursed_total)) < 0.00001
            ))
            if len(candidates) == 1:
                return self._set_match(
                    'linked_return_id', candidates, 'amazon_order_product_quantity',
                    _("Matched by Amazon Order ID + product identity + quantity."),
                )
            if len(candidates) > 1:
                return self._ambiguous(_(
                    "Multiple customer-return events match the order, product, and quantity."
                ))

        # A removal/disposal link requires both an explicit removal reference and
        # an item identity on that order.
        if self.reimbursement_classification == 'removal' and (self.case_id or self.amazon_order_id):
            removal = self.env['amazon.removal.order'].search(instance_domain + [
                ('removal_order_id', 'in', list(filter(None, (self.case_id, self.amazon_order_id)))),
            ], limit=3)
            removal = removal.filtered(lambda order: any(
                (self.fnsku and line.fnsku == self.fnsku)
                or (self.sku and line.sku == self.sku)
                or (self.amazon_product_id and line.amazon_product_id == self.amazon_product_id)
                for line in order.line_ids
            ))
            if len(removal) == 1:
                return self._set_match(
                    'linked_removal_order_id', removal, 'removal_reference_product',
                    _("Matched by removal order reference + product identity."),
                )
            if len(removal) > 1:
                return self._ambiguous(_(
                    "Multiple removal orders match the reference and product."
                ))

        # Controlled fallback is limited to a reason-compatible ledger event,
        # a single product identity, a close date, and the exact reported total.
        if (
            self.approval_date and (self.fnsku or self.sku or self.asin or self.amazon_product_id)
            and self.has_reported_quantity_total and not self.quantity_anomaly
            and self._allowed_event_categories()
        ):
            start = self.approval_date - timedelta(days=45)
            end = self.approval_date + timedelta(days=45)
            domain = instance_domain + [
                ('event_date', '>=', start), ('event_date', '<=', end),
                ('event_category', 'in', list(self._allowed_event_categories())),
            ]
            if self.fnsku:
                domain.append(('fnsku', '=', self.fnsku))
            elif self.sku:
                domain.append(('sku', '=', self.sku))
            elif self.asin:
                domain.append(('asin', '=', self.asin))
            else:
                domain.append(('amazon_product_id', '=', self.amazon_product_id.id))
            possible = self.env['amazon.fba.inventory.adjustment'].search(
                domain, limit=10,
            )
            possible = self._filter_inventory_events(possible, require_quantity=True)
            if len(possible) == 1:
                identity = 'FNSKU' if self.fnsku else ('SKU' if self.sku else 'ASIN/product')
                return self._set_match(
                    'linked_adjustment_id', possible, 'reason_product_date_quantity',
                    _("Matched by compatible reason + %s + approval-date window + exact quantity.", identity),
                )
            if len(possible) > 1:
                return self._ambiguous(_(
                    "Multiple reason/product/date/quantity inventory events match."
                ))
        self.write({
            'matching_state': 'unmatched', 'review_state': 'manual_review',
            'match_method': False, 'matching_explanation': False,
        })
        self._write_review(_("No strong operational match was found."))
        self.env['amazon.smart.alert'].phase7_alert(
            self.instance_id, 'reimbursement:%s' % self.event_key,
            _("FBA reimbursement is unmatched"), self.review_note,
            source=self, product=self.amazon_product_id,
        )
        return False

    def _ambiguous(self, message):
        self.ensure_one()
        self.write({
            'matching_state': 'ambiguous', 'review_state': 'manual_review',
            'match_method': False, 'matching_explanation': False,
        })
        self._write_review(message)
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
        ('settlements', 'Settlement Import'),
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
    row_error_log = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
        help="Rejected report rows, without customer comments or credentials.",
    )
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
        if self.operation_type == 'settlements':
            return self._settlement_turn()
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
                'amazon_request_id': response.get('_amazon_request_id') or self.amazon_request_id,
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
            if response.get('_amazon_request_id'):
                self.amazon_request_id = response['_amazon_request_id']
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
            if metadata.get('_amazon_request_id'):
                self.amazon_request_id = metadata['_amazon_request_id']
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
            row_errors = []
            for offset, row in enumerate(batch, start=self.cursor_index + 2):
                try:
                    with self.env.cr.savepoint():
                        self._process_report_row(row)
                    success += 1
                except Exception as exc:
                    failed += 1
                    row_errors.append(
                        "Row %s (order=%s, sku=%s): %s" % (
                            offset,
                            str(row.get('order-id') or row.get('amazon-order-id') or '')[:80],
                            str(row.get('sku') or row.get('fnsku') or row.get('asin') or '')[:80],
                            str(exc)[:1000],
                        )
                    )
                    _logger.exception("Phase 7 row failed for job %s: %s", self.id, exc)
            cursor = self.cursor_index + len(batch)
            error_log = '\n'.join(filter(None, [self.row_error_log, *row_errors]))[-10000:] or False
            self.write({
                'cursor_index': cursor,
                'total_processed': self.total_processed + success,
                'total_failed': self.total_failed + failed,
                'row_error_log': error_log,
                'last_activity_at': fields.Datetime.now(),
            })
            if cursor < len(rows):
                self.write({'state': 'pending', 'next_run_at': fields.Datetime.now()})
                return False
            return self._complete_report_part()
        raise ValidationError(_("Unknown report job stage %s.", self.stage))

    def _settlement_turn(self):
        """Discover Amazon-generated V2 reports and import one document per turn."""
        self.ensure_one()
        api = AmazonAPI()
        if self.stage == 'request':
            token = self.instance_id._get_access_token_or_raise()
            now = fields.Datetime.now()
            earliest = now - timedelta(days=90)
            if self.date_from:
                created_since = datetime.combine(self.date_from, time.min)
            elif self.instance_id.last_settlement_sync_at:
                # Settlement documents can appear after their economic period.
                # A seven-day overlap is safe because body IDs/lines are upserted.
                created_since = self.instance_id.last_settlement_sync_at - timedelta(days=7)
            else:
                created_since = earliest
            created_since = max(created_since, earliest)
            if self.date_to:
                created_until = min(
                    datetime.combine(self.date_to + timedelta(days=1), time.min), now,
                )
            else:
                created_until = now
            reports = self.instance_id._api_call_safe(
                api.get_settlement_reports_list, self.instance_id, token,
                processing_statuses='DONE',
                created_since=created_since.strftime('%Y-%m-%dT%H:%M:%SZ'),
                created_until=created_until.strftime('%Y-%m-%dT%H:%M:%SZ'),
                error_msg=_("Failed to discover Amazon V2 settlement reports"),
            )
            complete_reports = [
                report for report in reports
                if report.get('reportId') and report.get('reportDocumentId')
                and report.get('reportType') == REPORT_SETTLEMENT
            ]
            self.write({
                'report_kind': 'settlements',
                'raw_document': json.dumps(complete_reports, default=str),
                'total_found': len(complete_reports), 'cursor_index': 0,
                'stage': 'download', 'state': 'pending',
                'next_run_at': fields.Datetime.now(),
            })
            if not complete_reports:
                return self._complete_settlement_job()
            return False

        if self.stage == 'download':
            reports = json.loads(self.raw_document or '[]')
            if self.cursor_index >= len(reports):
                return self._complete_settlement_job()
            report = reports[self.cursor_index]
            document_id = report.get('reportDocumentId')
            if not document_id:
                raise ValidationError(_(
                    "A completed settlement report has no reportDocumentId."
                ))
            token = self.instance_id._get_access_token_or_raise()
            document = self.instance_id._api_call_safe(
                api.get_report_document, self.instance_id, token, document_id,
                error_msg=_("Failed to retrieve Amazon settlement document metadata"),
            )
            raw = api.download_report_document(
                document.get('url'),
                compression=document.get('compressionAlgorithm'),
                encryption=document.get('encryptionDetails'),
                instance=self.instance_id,
            )
            result = self.env['amazon.settlement.report'].import_flat_file(
                self.instance_id, raw, report,
            )
            imported = result['settlements']
            report_errors = '\n'.join(
                filter(None, imported.mapped('parsing_error_log'))
            )
            cursor = self.cursor_index + 1
            values = {
                'amazon_report_id': report.get('reportId') or False,
                'amazon_report_document_id': document_id,
                'amazon_request_id': (
                    document.get('_amazon_request_id') or self.amazon_request_id
                ),
                'cursor_index': cursor,
                'total_processed': self.total_processed + result['processed'],
                'total_failed': self.total_failed + result['failed'],
                'row_error_log': '\n'.join(filter(None, (
                    self.row_error_log, report_errors,
                )))[-10000:] or False,
                'last_activity_at': fields.Datetime.now(),
            }
            if cursor < len(reports):
                values.update({
                    'state': 'pending', 'next_run_at': fields.Datetime.now(),
                })
                self.write(values)
                return False
            self.write(values)
            return self._complete_settlement_job()

        raise ValidationError(_("Unknown settlement job stage %s.", self.stage))

    def _complete_settlement_job(self):
        self.ensure_one()
        if not self.total_failed:
            self.instance_id.last_settlement_sync_at = fields.Datetime.now()
        self.write({
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
            'last_error_code': 'ROW_ERRORS' if self.total_failed else False,
            'last_error_message': self.row_error_log if self.total_failed else False,
            'next_run_at': False, 'raw_document': False,
        })
        return True

    @staticmethod
    def _parse_rows(raw):
        normalized = (raw or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', ' ')
        if not normalized.strip():
            return []
        reader = csv.DictReader(
            io.StringIO(normalized), delimiter='\t', quoting=csv.QUOTE_NONE,
            restkey='_extra_fields', restval='',
        )
        return [row for row in reader if any(value not in (None, '') for value in row.values())]

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
            warning = self.row_error_log if self.total_failed else False
            source.write({
                'state': 'processed', 'amazon_report_document_id': self.amazon_report_document_id,
                'imported_at': fields.Datetime.now(), 'last_error': warning,
            })
        cursor_field = {
            'customer_returns': 'last_fba_return_sync_at',
            'removal_status': 'last_fba_removal_sync_at',
            'inventory_adjustments': 'last_fba_adjustment_sync_at',
            'reimbursements': 'last_fba_reimbursement_sync_at',
        }.get(self.operation_type)
        # Never advance an incremental cursor past rejected operational source
        # rows. Valid rows remain committed and idempotent, so the next run can
        # safely overlap and recover rejected evidence.
        if cursor_field and not (
            self.operation_type in (
                'customer_returns', 'removal_status', 'reimbursements',
            )
            and self.total_failed
        ):
            self.instance_id.write({cursor_field: fields.Datetime.now()})
        self.write({
            'state': 'done', 'stage': 'done', 'finished_at': fields.Datetime.now(),
            'last_error_code': 'ROW_ERRORS' if self.total_failed else False,
            'last_error_message': self.row_error_log if self.total_failed else False,
            'next_run_at': False, 'raw_document': False,
        })
        if self.operation_type == 'reimbursements':
            self.enqueue(self.instance_id, 'reimbursement_matching')
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
            ('matching_state', '=', 'unmatched'),
            '|', ('last_match_attempt_at', '=', False),
            ('last_match_attempt_at', '<', self.create_date),
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
        cause = exc.__cause__ or exc
        response = getattr(cause, 'response', None)
        status_code = getattr(response, 'status_code', None)
        reimbursement_authorization_error = self.operation_type == 'reimbursements' and (
            status_code in (401, 403)
            or any(token in message.lower() for token in (
                '401', '403', 'unauthorized', 'forbidden', 'accessdenied',
                'role is not authorized', 'revoked authorization',
            ))
        )
        if reimbursement_authorization_error:
            message = _(
                "FBA Reimbursements Report authorization failed. Amazon requires "
                "the Pricing or Amazon Fulfillment role for GET_FBA_REIMBURSEMENTS_DATA. %s",
                message,
            )[:10000]
        settlement_authorization_error = self.operation_type == 'settlements' and (
            status_code in (401, 403)
            or any(token in message.lower() for token in (
                '401', '403', 'unauthorized', 'forbidden', 'accessdenied',
                'role is not authorized', 'revoked authorization',
            ))
        )
        if settlement_authorization_error:
            message = _(
                "Settlement report authorization failed. Amazon requires the "
                "Finance and Accounting role for %s. %s",
                REPORT_SETTLEMENT, message,
            )[:10000]
        retryable = status_code == 429 or (status_code and status_code >= 500) or any(
            token in message.lower() for token in (
            '429', 'throttl', 'timeout', 'connection', 'temporar', '503', '500', '502', '504',
            )
        )
        if retryable and retry_count < self.max_retries:
            try:
                retry_after = max(float(response.headers.get('Retry-After') or 0), 0.0)
            except (AttributeError, TypeError, ValueError):
                retry_after = 0.0
            next_run_at = (
                fields.Datetime.now() + timedelta(seconds=retry_after)
                if retry_after
                else fields.Datetime.now() + timedelta(minutes=min(2 ** retry_count, 60))
            )
            self.write({
                'state': 'pending', 'retry_count': retry_count,
                'next_run_at': next_run_at,
                'last_error_code': str(status_code or 'RETRYABLE'),
                'last_error_message': message,
            })
        else:
            self.write({
                'state': 'failed', 'retry_count': retry_count, 'next_run_at': False,
                'finished_at': fields.Datetime.now(),
                'last_error_code': (
                    'SETTLEMENT_ROLE_MISSING' if settlement_authorization_error else (
                        'REPORT_ROLE_MISSING'
                        if reimbursement_authorization_error else 'PHASE7_ERROR'
                    )
                ),
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
                _(
                    "Amazon settlement import failed"
                    if self.operation_type == 'settlements'
                    else "FBA operational report job failed"
                ), message,
                source=self, critical=True,
            )
        _logger.exception("Amazon Phase 7 job %s failed: %s", self.id, exc)


class AmazonInstancePhase7(models.Model):
    _inherit = 'amazon.instance'

    def _phase7_window(self, cursor_field, default_days=30):
        self.ensure_one()
        end = fields.Date.today()
        cursor = self[cursor_field]
        overlap_days = 7 if cursor_field == 'last_fba_reimbursement_sync_at' else 2
        start = (
            fields.Date.to_date(cursor) - timedelta(days=overlap_days)
            if cursor else end - timedelta(days=default_days)
        )
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

    def _create_backorder_picking(self):
        backorder = super()._create_backorder_picking()
        if self.amazon_fba_movement_type == 'removal_receipt':
            backorder.write({
                'amazon_instance_id': self.amazon_instance_id.id,
                'amazon_removal_order_id': self.amazon_removal_order_id.id,
                'amazon_removal_shipment_id': self.amazon_removal_shipment_id.id,
                'amazon_fba_movement_type': 'removal_receipt',
            })
            if self.amazon_removal_order_id:
                self.amazon_removal_order_id.picking_ids = [Command.link(backorder.id)]
        return backorder

    def _removal_receipt_root(self):
        self.ensure_one()
        root = self
        while root.backorder_id and root.backorder_id.amazon_fba_movement_type == 'removal_receipt':
            root = root.backorder_id
        return root

    def _sync_removal_receipt_quantities(self):
        for picking in self.filtered(lambda rec: (
            rec.state == 'done' and rec.amazon_fba_movement_type == 'removal_receipt'
            and rec.amazon_removal_order_id
        )):
            order = picking.amazon_removal_order_id
            root = picking._removal_receipt_root()
            shipments = self.env['amazon.removal.shipment'].search([
                ('receipt_picking_id', '=', root.id),
            ], order='id')
            received_by_product = {}
            for move in picking.move_ids.filtered(lambda item: item.state == 'done'):
                received_by_product[move.product_id.id] = (
                    received_by_product.get(move.product_id.id, 0.0) + move.quantity
                )
            for shipment in shipments:
                product = shipment.line_id.odoo_product_id
                available = received_by_product.get(product.id, 0.0) if product else 0.0
                remaining = max(shipment.shipped_quantity - shipment.received_quantity, 0.0)
                allocated = min(available, remaining)
                if allocated:
                    shipment.received_quantity += allocated
                    received_by_product[product.id] = available - allocated
                shipment.stock_action_state = (
                    'received'
                    if shipment.shipped_quantity and shipment.received_quantity >= shipment.shipped_quantity
                    else 'partially_received'
                )
            for line in shipments.mapped('line_id'):
                line.received_quantity = min(
                    line.shipped_quantity,
                    sum(order.shipment_ids.filtered(lambda item: item.line_id == line).mapped(
                        'received_quantity'
                    )),
                )
            total_received = sum(order.line_ids.mapped('received_quantity'))
            total_shipped = sum(order.line_ids.mapped('shipped_quantity'))
            order.write({
                'stock_action_state': 'received' if total_received >= total_shipped else 'partially_received',
                'state': 'completed' if total_shipped and total_received >= total_shipped else 'awaiting_receipt',
            })

    def _action_done(self):
        result = super()._action_done()
        self._sync_removal_receipt_quantities()
        return result

    def button_validate(self):
        removal_receipts = self.filtered(lambda rec: (
            rec.state not in ('done', 'cancel')
            and rec.amazon_fba_movement_type == 'removal_receipt'
            and rec.amazon_removal_order_id
        ))
        for picking in removal_receipts:
            quantities = {}
            for move in picking.move_ids.filtered(lambda item: item.state != 'cancel'):
                quantities[move.product_id] = quantities.get(move.product_id, 0.0) + move.quantity
            for product, quantity in quantities.items():
                if product.uom_id.compare(quantity, 0.0) <= 0:
                    continue
                physical = product.sudo().with_company(picking.company_id).with_context(
                    location=picking.location_id.id,
                ).qty_available
                if product.uom_id.compare(physical, quantity) < 0:
                    raise UserError(_(
                        "Only %s %s is physically recorded in Removal Transit for %s. "
                        "Reconcile the Amazon departure before validating %s received units.",
                        physical, product.uom_id.name, product.display_name, quantity,
                    ))
        result = super().button_validate()
        return result
