import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


KNOWN_REMOVAL_STATES = {
    'pending': 'processing', 'planning': 'processing', 'processing': 'processing',
    'in process': 'processing',
    'completed': 'completed', 'cancelled': 'cancelled', 'canceled': 'cancelled',
}


class AmazonRemovalOrder(models.Model):
    _name = 'amazon.removal.order'
    _description = 'Amazon FBA Removal Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'requested_at desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default='New', copy=False, tracking=True)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, tracking=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    marketplace_id = fields.Char(
        related='instance_id.marketplace_id', store=True, readonly=True, index=True,
    )
    removal_order_id = fields.Char('Amazon Removal Order ID', copy=False, index=True, tracking=True)
    amazon_removal_order_id = fields.Char(
        related='removal_order_id', readonly=False, store=True,
        string='Legacy Amazon Removal Order ID',
    )
    removal_type = fields.Selection([
        ('return_to_address', 'Return to Address'),
        ('disposal', 'Disposal'),
        ('liquidation', 'Liquidation'),
        ('other', 'Other'),
    ], default='return_to_address', required=True, index=True, tracking=True)
    # Legacy field retained for upgrade compatibility.
    order_type = fields.Selection([
        ('Return', 'Return to Seller'), ('Disposal', 'Disposal'),
        ('Liquidation', 'Liquidation'), ('Other', 'Other'),
    ], compute='_compute_order_type', inverse='_inverse_order_type', store=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('queued', 'Queued'), ('submitting', 'Submitting'),
        ('feed_processing', 'Feed Processing'), ('submitted', 'Accepted by Amazon'),
        ('processing', 'Processing'), ('in_transit', 'In Transit'),
        ('awaiting_receipt', 'Awaiting Warehouse Receipt'),
        ('completed', 'Completed'), ('cancelled', 'Cancelled'), ('failed', 'Failed'),
        ('manual_review', 'Manual Review'),
    ], default='draft', required=True, index=True, tracking=True)
    amazon_status = fields.Char(index=True, tracking=True)
    amazon_order_type_raw = fields.Char(readonly=True, index=True)
    previous_amazon_status = fields.Char(readonly=True)
    requested_at = fields.Datetime(index=True)
    last_updated_at = fields.Datetime(index=True)
    completed_at = fields.Datetime(index=True)
    last_synced_at = fields.Datetime(readonly=True)
    ship_to_partner_id = fields.Many2one(
        'res.partner', check_company=True, ondelete='restrict', tracking=True,
    )
    ship_to_address = fields.Text(compute='_compute_ship_to_address', store=True)
    shipping_notes = fields.Char()
    carrier = fields.Char()
    tracking_number = fields.Char(index=True)
    shipment_date = fields.Datetime(index=True)
    total_requested_quantity = fields.Float(compute='_compute_totals', store=True)
    total_shipped_quantity = fields.Float(compute='_compute_totals', store=True)
    total_cancelled_quantity = fields.Float(compute='_compute_totals', store=True)
    total_disposed_quantity = fields.Float(compute='_compute_totals', store=True)
    total_received_quantity = fields.Float(compute='_compute_totals', store=True)
    feed_document_id = fields.Char(copy=False)
    feed_id = fields.Char(copy=False, index=True)
    feed_processing_status = fields.Char(copy=False, index=True, tracking=True)
    feed_result_document_id = fields.Char(copy=False)
    error_code = fields.Char(copy=False, index=True)
    error_message = fields.Text(copy=False)
    discrepancy_code = fields.Char(copy=False, readonly=True, index=True)
    discrepancy_message = fields.Text(copy=False, readonly=True)
    raw_response = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    stock_action_state = fields.Selection([
        ('pending', 'Pending'), ('dispatched', 'Dispatched'),
        ('awaiting_receipt', 'Awaiting Receipt'), ('partially_received', 'Partially Received'),
        ('received', 'Received'), ('disposed', 'Disposed'),
        ('informational', 'Informational'), ('audit_only', 'Reconciliation / Audit Only'),
        ('manual_review', 'Manual Review'),
    ], default='pending', required=True, index=True, tracking=True)
    line_ids = fields.One2many('amazon.removal.order.line', 'order_id', copy=False)
    line_count = fields.Integer(compute='_compute_line_count')
    shipment_ids = fields.One2many('amazon.removal.shipment', 'order_id', copy=False)
    shipment_count = fields.Integer(compute='_compute_shipment_count')
    picking_ids = fields.Many2many(
        'stock.picking', 'amazon_removal_order_picking_rel', 'order_id', 'picking_id',
        string='Stock Pickings', copy=False,
    )
    picking_count = fields.Integer(compute='_compute_picking_count')
    stock_move_count = fields.Integer(compute='_compute_link_counts')
    reimbursement_ids = fields.One2many('amazon.fba.reimbursement', 'linked_removal_order_id')
    reimbursement_count = fields.Integer(compute='_compute_reimbursement_count')
    sync_log_count = fields.Integer(compute='_compute_link_counts')
    manual_review_required = fields.Boolean(default=False, index=True, tracking=True)

    _amazon_order_unique = models.Constraint(
        'UNIQUE(instance_id, removal_order_id)',
        'Amazon removal order ID must be unique per instance.',
    )

    @api.depends('removal_type')
    def _compute_order_type(self):
        values = {
            'return_to_address': 'Return', 'disposal': 'Disposal',
            'liquidation': 'Liquidation', 'other': 'Other',
        }
        for rec in self:
            rec.order_type = values.get(rec.removal_type, 'Other')

    def _inverse_order_type(self):
        values = {
            'Return': 'return_to_address', 'Disposal': 'disposal',
            'Liquidation': 'liquidation', 'Other': 'other',
        }
        for rec in self:
            rec.removal_type = values.get(rec.order_type, 'other')

    @api.depends('ship_to_partner_id', 'ship_to_partner_id.contact_address')
    def _compute_ship_to_address(self):
        for rec in self:
            rec.ship_to_address = rec.ship_to_partner_id.contact_address or False

    @api.depends(
        'line_ids.requested_quantity', 'line_ids.shipped_quantity',
        'line_ids.cancelled_quantity', 'line_ids.disposed_quantity',
        'line_ids.received_quantity',
    )
    def _compute_totals(self):
        for rec in self:
            rec.total_requested_quantity = sum(rec.line_ids.mapped('requested_quantity'))
            rec.total_shipped_quantity = sum(rec.line_ids.mapped('shipped_quantity'))
            rec.total_cancelled_quantity = sum(rec.line_ids.mapped('cancelled_quantity'))
            rec.total_disposed_quantity = sum(rec.line_ids.mapped('disposed_quantity'))
            rec.total_received_quantity = sum(rec.line_ids.mapped('received_quantity'))

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('shipment_ids')
    def _compute_shipment_count(self):
        for rec in self:
            rec.shipment_count = len(rec.shipment_ids)

    @api.depends('picking_ids')
    def _compute_picking_count(self):
        for rec in self:
            rec.picking_count = len(rec.picking_ids)

    @api.depends('reimbursement_ids')
    def _compute_reimbursement_count(self):
        for rec in self:
            rec.reimbursement_count = len(rec.reimbursement_ids)

    def _compute_link_counts(self):
        sync_log_model = self.env['amazon.sync.log'].sudo()
        for rec in self:
            rec.stock_move_count = len(rec.picking_ids.move_ids)
            rec.sync_log_count = sync_log_model.search_count([
                ('source_model', '=', rec._name), ('source_id', '=', rec.id),
            ])

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.removal.order') or 'New'
            vals.setdefault('removal_order_id', vals.get('name'))
            if vals.get('order_type') and not vals.get('removal_type'):
                vals['removal_type'] = {
                    'Return': 'return_to_address', 'Disposal': 'disposal',
                    'Liquidation': 'liquidation',
                }.get(vals['order_type'], 'other')
        return super().create(vals_list)

    @api.constrains('instance_id', 'ship_to_partner_id', 'removal_type', 'line_ids')
    def _check_removal_request(self):
        for order in self:
            if order.removal_type == 'return_to_address' and order.ship_to_partner_id:
                partner = order.ship_to_partner_id
                if partner.company_id and partner.company_id != order.company_id:
                    raise ValidationError(_("The removal address belongs to another company."))
            for line in order.line_ids:
                if line.requested_quantity <= 0:
                    raise ValidationError(_("Removal quantities must be positive."))

    def _validate_submission(self):
        self.ensure_one()
        if self.state not in ('draft', 'failed'):
            raise UserError(_("Only draft or failed removal requests can be submitted."))
        if not self.line_ids:
            raise UserError(_("Add at least one product line."))
        if self.removal_type not in ('return_to_address', 'disposal'):
            raise UserError(_("Only return-to-address and disposal feeds are supported."))
        for line in self.line_ids:
            if not line.sku or not line.fnsku:
                raise UserError(_("Every line requires a mapped SKU and FNSKU."))
            if not line.odoo_product_id:
                raise UserError(_("Every line requires an Odoo product mapping."))
            if line.requested_quantity <= 0 or not float(line.requested_quantity).is_integer():
                raise UserError(_("Amazon removal quantities must be positive whole numbers."))
        if self.removal_type == 'return_to_address':
            partner = self.ship_to_partner_id or self.instance_id.fba_removal_return_partner_id
            if not partner:
                raise UserError(_("Configure a dedicated FBA removal return address."))
            missing = [label for value, label in (
                (partner.name, _('Name')), (partner.street, _('Street')),
                (partner.city, _('City')), (partner.zip, _('Postal Code')),
                (partner.country_id.code, _('Country')), (partner.phone or partner.mobile, _('Phone')),
            ) if not value]
            if missing:
                raise UserError(_("Removal address is missing: %s", ', '.join(missing)))
            self.ship_to_partner_id = partner
        return True

    def action_submit_to_amazon(self):
        self.ensure_one()
        self._validate_submission()
        job = self.env['amazon.phase7.job'].enqueue(
            self.instance_id, 'removal_submit', source=self,
        )
        self.write({'state': 'queued', 'error_code': False, 'error_message': False})
        return self.instance_id._notify(
            _("Removal Order"), _("Submission job %s was queued.", job.display_name)
        )

    def action_check_status(self):
        self.ensure_one()
        job = self.env['amazon.phase7.job'].enqueue(
            self.instance_id, 'removal_status', source=self,
        )
        return self.instance_id._notify(
            _("Removal Order"), _("Status refresh job %s was queued.", job.display_name)
        )

    def action_cancel(self):
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "This button cancels only an unsubmitted Odoo draft. Amazon cancellation "
                "is not exposed by the supported Phase 7 feed workflow."
            ))
        self.state = 'cancelled'

    def action_view_pickings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Stock Pickings'),
            'res_model': 'stock.picking', 'view_mode': 'list,form',
            'domain': [('id', 'in', self.picking_ids.ids)],
        }

    def action_view_shipments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Removal Shipments'),
            'res_model': 'amazon.removal.shipment', 'view_mode': 'list,form',
            'domain': [('order_id', '=', self.id)],
        }

    def action_view_reimbursements(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Reimbursements'),
            'res_model': 'amazon.fba.reimbursement', 'view_mode': 'list,form',
            'domain': [('linked_removal_order_id', '=', self.id)],
        }

    def action_view_stock_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Stock Moves'),
            'res_model': 'stock.move', 'view_mode': 'list,form',
            'domain': [('picking_id', 'in', self.picking_ids.ids)],
        }

    def action_view_sync_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Sync Logs'),
            'res_model': 'amazon.sync.log', 'view_mode': 'list,form',
            'domain': [('source_model', '=', self._name), ('source_id', '=', self.id)],
        }

    @api.model
    def import_detail_row(self, instance, row, raw_report_reference=False):
        removal_id = (row.get('order-id') or '').strip()
        if not removal_id:
            raise ValidationError(_("Amazon removal detail row has no order-id."))
        order = self.search([
            ('instance_id', '=', instance.id), ('removal_order_id', '=', removal_id),
        ], limit=1)
        raw_type = row.get('order-type') or row.get('removal-order-type') or ''
        lower_type = raw_type.lower()
        removal_type = 'disposal' if 'dispos' in lower_type else (
            'liquidation' if 'liquid' in lower_type else (
                'return_to_address' if 'return' in lower_type else 'other'
            )
        )
        raw_status = row.get('order-status') or ''
        mapped_state = KNOWN_REMOVAL_STATES.get(raw_status.strip().lower())
        values = {
            'instance_id': instance.id, 'name': removal_id,
            'removal_order_id': removal_id, 'removal_type': removal_type,
            'amazon_order_type_raw': raw_type,
            'requested_at': self.env['amazon.phase7.stock.service'].datetime(row.get('request-date')),
            'last_updated_at': self.env['amazon.phase7.stock.service'].datetime(
                row.get('last-updated-date')
            ),
            'last_synced_at': fields.Datetime.now(), 'amazon_status': raw_status,
            'raw_response': json.dumps(row, default=str, sort_keys=True),
        }
        if mapped_state:
            if not order or order.state not in ('awaiting_receipt',):
                values['state'] = mapped_state
            if mapped_state == 'completed':
                values['completed_at'] = fields.Datetime.now()
        else:
            values.update(state='manual_review', manual_review_required=True)
        if order:
            values['previous_amazon_status'] = order.amazon_status if order.amazon_status != raw_status else order.previous_amazon_status
            order.write(values)
        else:
            order = self.create(values)
        order.line_ids.import_detail_row(order, row, raw_report_reference)
        return order


class AmazonRemovalOrderLine(models.Model):
    _name = 'amazon.removal.order.line'
    _description = 'Amazon FBA Removal Order Line'
    _order = 'id'
    _check_company_auto = True

    order_id = fields.Many2one(
        'amazon.removal.order', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(related='order_id.instance_id', store=True, readonly=True, index=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True, readonly=True, index=True)
    line_key = fields.Char(copy=False, index=True)
    amazon_product_id = fields.Many2one('amazon.product', ondelete='set null', index=True)
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    sku = fields.Char(required=True, index=True)
    fnsku = fields.Char(required=True, index=True)
    disposition = fields.Char(default='Sellable', required=True, index=True)
    fulfillment_center_id = fields.Char(index=True)
    tracking_package_reference = fields.Char(index=True)
    requested_quantity = fields.Float(default=1.0)
    shipped_quantity = fields.Float(default=0.0, readonly=True)
    cancelled_quantity = fields.Float(default=0.0, readonly=True)
    disposed_quantity = fields.Float(default=0.0, readonly=True)
    in_process_quantity = fields.Float(default=0.0, readonly=True)
    received_quantity = fields.Float(default=0.0, readonly=True)
    last_shipped_delta = fields.Float(default=0.0, readonly=True, copy=False)
    last_disposed_delta = fields.Float(default=0.0, readonly=True, copy=False)
    dispatched_stock_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    disposed_stock_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    dispatch_move_id = fields.Many2one('stock.move', ondelete='restrict', copy=False)
    disposal_move_id = fields.Many2one('stock.move', ondelete='restrict', copy=False)
    receipt_move_id = fields.Many2one('stock.move', ondelete='restrict', copy=False)
    raw_report_reference = fields.Char()
    raw_response = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    mapping_status = fields.Selection([
        ('mapped', 'Mapped'), ('unmapped', 'Unmapped SKU'),
    ], compute='_compute_mapping_status', store=True, readonly=True, index=True)
    discrepancy_code = fields.Char(copy=False, readonly=True, index=True)
    discrepancy_message = fields.Text(copy=False, readonly=True)

    _line_unique = models.Constraint(
        'UNIQUE(order_id, line_key)', 'This Amazon removal-order line was already imported.',
    )
    _non_negative_quantities = models.Constraint(
        'CHECK(requested_quantity >= 0 AND shipped_quantity >= 0 AND '
        'cancelled_quantity >= 0 AND disposed_quantity >= 0 AND received_quantity >= 0)',
        'Removal-order quantities cannot be negative.',
    )

    @api.depends('odoo_product_id')
    def _compute_mapping_status(self):
        for line in self:
            line.mapping_status = 'mapped' if line.odoo_product_id else 'unmapped'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('line_key'):
                vals['line_key'] = hashlib.sha256('|'.join(str(vals.get(key) or '') for key in (
                    'sku', 'fnsku', 'disposition', 'fulfillment_center_id',
                )).encode()).hexdigest()
        return super().create(vals_list)

    @api.model
    def import_detail_row(self, order, row, raw_report_reference=False):
        key = hashlib.sha256('|'.join(str(value or '') for value in (
            row.get('sku'), row.get('fnsku'), row.get('disposition'),
            row.get('fulfillment-center-id'),
        )).encode()).hexdigest()
        line = self.search([('order_id', '=', order.id), ('line_key', '=', key)], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            order.instance_id, row.get('sku'), row.get('fnsku'), False
        )
        number = self.env['amazon.phase7.stock.service'].number
        old_shipped = line.shipped_quantity if line else 0.0
        old_disposed = line.disposed_quantity if line else 0.0
        reported_shipped = number(row.get('shipped-quantity'))
        shipment_observed = sum(order.shipment_ids.filtered(
            lambda item: item.line_id == line
        ).mapped('shipped_quantity')) if line else 0.0
        new_shipped = max(reported_shipped, shipment_observed)
        new_disposed = number(row.get('disposed-quantity'))
        values = {
            'order_id': order.id, 'line_key': key, 'sku': row.get('sku') or '',
            'fnsku': row.get('fnsku') or '', 'disposition': row.get('disposition') or 'Unknown',
            'fulfillment_center_id': row.get('fulfillment-center-id') or '',
            'requested_quantity': number(row.get('requested-quantity')),
            'shipped_quantity': new_shipped,
            'cancelled_quantity': number(row.get('cancelled-quantity')),
            'disposed_quantity': new_disposed,
            'in_process_quantity': number(row.get('in-process-quantity')),
            'last_shipped_delta': max(new_shipped - old_shipped, 0.0),
            'last_disposed_delta': max(new_disposed - old_disposed, 0.0),
            'raw_report_reference': raw_report_reference,
            'raw_response': json.dumps(row, default=str, sort_keys=True),
            **product_values,
        }
        if line:
            values.pop('order_id'); values.pop('line_key')
            line.write(values)
        else:
            line = self.create(values)
        if new_shipped < old_shipped or new_disposed < old_disposed:
            line.write({
                'discrepancy_code': 'AMAZON_QUANTITY_DECREASED',
                'discrepancy_message': _(
                    "Amazon reduced a cumulative removal quantity. No reverse stock move was created."
                ),
            })
            order.write({
                'manual_review_required': True,
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'AMAZON_QUANTITY_DECREASED',
                'discrepancy_message': line.discrepancy_message,
            })
        elif not line.odoo_product_id:
            line.write({
                'discrepancy_code': 'UNMAPPED_SKU',
                'discrepancy_message': _(
                    "Amazon removal SKU %s is not mapped for this instance.", line.sku
                ),
            })
            order.write({
                'manual_review_required': True,
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'UNMAPPED_SKU',
                'discrepancy_message': line.discrepancy_message,
            })
        elif line.disposed_quantity > old_disposed:
            self.env['amazon.phase7.stock.service'].apply_disposal(
                line, line.disposed_quantity - old_disposed
            )
        elif order.stock_action_state not in ('manual_review', 'awaiting_receipt', 'received'):
            order.stock_action_state = 'audit_only'
        return line


class AmazonRemovalShipment(models.Model):
    _name = 'amazon.removal.shipment'
    _description = 'Amazon FBA Removal Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'shipment_date desc, id desc'
    _check_company_auto = True

    shipment_key = fields.Char(required=True, copy=False, index=True, readonly=True)
    order_id = fields.Many2one(
        'amazon.removal.order', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(related='order_id.instance_id', store=True, readonly=True, index=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True, readonly=True, index=True)
    removal_order_id = fields.Char(related='order_id.removal_order_id', store=True, readonly=True)
    request_date = fields.Datetime()
    shipment_date = fields.Datetime(index=True)
    sku = fields.Char(index=True)
    fnsku = fields.Char(index=True)
    disposition = fields.Char(index=True)
    shipped_quantity = fields.Float()
    previous_shipped_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    shipped_delta_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    received_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    carrier = fields.Char(index=True)
    tracking_number = fields.Char(index=True)
    package_reference = fields.Char(index=True)
    removal_order_type = fields.Char()
    line_id = fields.Many2one(
        'amazon.removal.order.line', ondelete='set null', check_company=True,
    )
    dispatch_move_id = fields.Many2one(
        'stock.move', ondelete='restrict', copy=False, check_company=True,
    )
    receipt_picking_id = fields.Many2one(
        'stock.picking', ondelete='restrict', copy=False, check_company=True,
    )
    dispatch_picking_ids = fields.Many2many(
        'stock.picking', 'amazon_removal_shipment_dispatch_picking_rel',
        'shipment_id', 'picking_id', string='Removal Transit Transfers', copy=False,
    )
    dispatched_stock_quantity = fields.Float(default=0.0, readonly=True, copy=False)
    stock_action_state = fields.Selection([
        ('audit_only', 'Reconciliation / Audit Only'),
        ('in_transit', 'Moved to Removal Transit'),
        ('awaiting_receipt', 'Awaiting Warehouse Receipt'),
        ('partially_received', 'Partially Received'),
        ('received', 'Received'),
        ('manual_review', 'Manual Review'),
    ], default='audit_only', required=True, readonly=True, index=True, tracking=True)
    discrepancy_code = fields.Char(copy=False, readonly=True, index=True)
    discrepancy_message = fields.Text(copy=False, readonly=True)
    raw_report_reference = fields.Char()
    raw_response = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')

    _shipment_unique = models.Constraint(
        'UNIQUE(instance_id, shipment_key)', 'This Amazon removal shipment was already imported.',
    )

    def _check_stock_action_access(self):
        if (
            self.env.su
            or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager')
            or self.env.user.has_group('stock.group_stock_manager')
        ):
            return
        raise AccessError(_(
            "Only an Amazon Connector Manager or Inventory Administrator can process removal stock."
        ))

    def _package_shipments(self):
        self.ensure_one()
        if not self.tracking_number:
            return self
        return self.search([
            ('order_id', '=', self.order_id.id),
            ('tracking_number', '=', self.tracking_number),
            ('carrier', '=', self.carrier),
            ('shipment_date', '=', self.shipment_date),
        ], order='id')

    def action_move_to_removal_transit(self):
        self.ensure_one()
        self._check_stock_action_access()
        moved = self.env['amazon.phase7.stock.service'].apply_removal_shipment(
            self, reviewed=True,
        )
        if not moved:
            return self.order_id.instance_id._notify(
                _("Removal Transit"),
                self.discrepancy_message or _("No unprocessed shipment quantity was available."),
                'warning',
            )
        return self.order_id.instance_id._notify(
            _("Removal Transit"), _("The reviewed shipment delta was moved to Removal Transit.")
        )

    def action_create_receipt(self):
        self.ensure_one()
        self._check_stock_action_access()
        picking = self.env['amazon.phase7.stock.service'].create_removal_receipt(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Amazon Removal Receipt"),
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': picking.id,
        }

    def action_view_receipt(self):
        self.ensure_one()
        if not self.receipt_picking_id:
            raise UserError(_("No customer warehouse receipt has been created."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Amazon Removal Receipt"),
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': self.receipt_picking_id.id,
        }

    @api.model
    def import_row(self, instance, row, raw_report_reference=False):
        removal_id = row.get('order-id') or ''
        order = self.env['amazon.removal.order'].search([
            ('instance_id', '=', instance.id), ('removal_order_id', '=', removal_id),
        ], limit=1)
        if not order:
            order = self.env['amazon.removal.order'].create({
                'instance_id': instance.id, 'name': removal_id or _('Imported Removal'),
                'removal_order_id': removal_id, 'removal_type': 'other',
                'state': 'manual_review', 'manual_review_required': True,
            })
        components = [
            instance.id, removal_id, row.get('shipment-date'), row.get('sku'),
            row.get('fnsku'), row.get('disposition'), row.get('carrier'),
            row.get('tracking-number'), row.get('removal-order-type'),
        ]
        key = hashlib.sha256('|'.join(str(value or '') for value in components).encode()).hexdigest()
        shipment = self.search([('instance_id', '=', instance.id), ('shipment_key', '=', key)], limit=1)
        line = order.line_ids.filtered(lambda item: (
            item.sku == (row.get('sku') or '') and item.fnsku == (row.get('fnsku') or '')
            and item.disposition.lower() == (row.get('disposition') or '').lower()
        ))[:1]
        old_shipped = shipment.shipped_quantity if shipment else 0.0
        new_shipped = self.env['amazon.phase7.stock.service'].number(row.get('shipped-quantity'))
        values = {
            'shipment_key': key, 'order_id': order.id,
            'request_date': self.env['amazon.phase7.stock.service'].datetime(row.get('request-date')),
            'shipment_date': self.env['amazon.phase7.stock.service'].datetime(row.get('shipment-date')),
            'sku': row.get('sku') or '', 'fnsku': row.get('fnsku') or '',
            'disposition': row.get('disposition') or '',
            'previous_shipped_quantity': old_shipped,
            'shipped_quantity': new_shipped,
            'shipped_delta_quantity': max(new_shipped - old_shipped, 0.0),
            'carrier': row.get('carrier') or '', 'tracking_number': row.get('tracking-number') or '',
            'package_reference': row.get('tracking-number') or '',
            'removal_order_type': row.get('removal-order-type') or '',
            'line_id': line.id or False, 'raw_report_reference': raw_report_reference,
            'raw_response': json.dumps(row, default=str, sort_keys=True),
        }
        if shipment:
            values.pop('shipment_key'); values.pop('order_id')
            shipment.write(values)
        else:
            shipment = self.create(values)
        shipment_total = 0.0
        if line:
            shipment_total = sum(order.shipment_ids.filtered(
                lambda item: item.line_id == line
            ).mapped('shipped_quantity'))
            if shipment_total > line.shipped_quantity:
                line.write({
                    'last_shipped_delta': shipment_total - line.shipped_quantity,
                    'shipped_quantity': shipment_total,
                })
        if new_shipped < old_shipped:
            shipment.write({
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'AMAZON_SHIPPED_QUANTITY_DECREASED',
                'discrepancy_message': _(
                    "Amazon reduced the cumulative shipped quantity. No reverse move was created."
                ),
            })
            order.write({
                'manual_review_required': True,
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'AMAZON_SHIPPED_QUANTITY_DECREASED',
                'discrepancy_message': shipment.discrepancy_message,
            })
        elif (
            line and line.requested_quantity > 0
            and line.odoo_product_id
            and line.odoo_product_id.uom_id.compare(
                shipment_total, line.requested_quantity,
            ) > 0
        ):
            self.env['amazon.phase7.stock.service']._removal_discrepancy(
                shipment,
                'REMOVAL_EXCEEDS_REQUESTED_QUANTITY',
                _(
                    "Amazon shipment rows total %s for a line that requested %s. "
                    "No stock move was created.",
                    shipment_total, line.requested_quantity,
                ),
            )
        elif not line or not line.odoo_product_id:
            shipment.write({
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'UNMAPPED_SKU',
                'discrepancy_message': _(
                    "Amazon removal shipment SKU %s is not mapped to an order line and product.",
                    shipment.sku,
                ),
            })
            order.write({
                'manual_review_required': True,
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'UNMAPPED_SKU',
                'discrepancy_message': shipment.discrepancy_message,
            })
        elif order.removal_type == 'return_to_address':
            self.env['amazon.phase7.stock.service'].apply_removal_shipment(shipment)
            order.write({
                'carrier': shipment.carrier,
                'tracking_number': shipment.tracking_number,
                'shipment_date': shipment.shipment_date,
                'state': 'awaiting_receipt',
                'stock_action_state': 'audit_only',
                'last_synced_at': fields.Datetime.now(),
            })
        else:
            shipment.write({
                'stock_action_state': 'manual_review',
                'discrepancy_code': 'UNEXPECTED_REMOVAL_SHIPMENT_TYPE',
                'discrepancy_message': _(
                    "Amazon shipment detail is only authoritative for return-to-seller removals."
                ),
            })
            order.write({
                'manual_review_required': True,
                'stock_action_state': 'manual_review',
                'last_synced_at': fields.Datetime.now(),
            })
        return shipment
