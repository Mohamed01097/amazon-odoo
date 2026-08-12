import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


RETURN_DISPOSITION_MAP = {
    'SELLABLE': 'sellable',
    'CUSTOMER_DAMAGED': 'unsellable',
    'CARRIER_DAMAGED': 'unsellable',
    'DAMAGED': 'unsellable',
    'DEFECTIVE': 'unsellable',
    'EXPIRED': 'unsellable',
    'UNSELLABLE': 'unsellable',
}


class AmazonReturnReport(models.Model):
    _name = 'amazon.return.report'
    _description = 'Amazon FBA Customer Return Import'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'report_date desc, id desc'
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
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('requested', 'Requested'),
        ('downloaded', 'Downloaded'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True, tracking=True)
    line_ids = fields.One2many('amazon.return.report.line', 'report_id', copy=False)
    line_count = fields.Integer(compute='_compute_line_count')
    report_date = fields.Date(default=fields.Date.today, required=True, index=True)
    amazon_report_id = fields.Char(copy=False, index=True)
    amazon_report_document_id = fields.Char(copy=False)
    last_error = fields.Text(copy=False, groups='sdlc_amazon_connector.group_amazon_manager')
    imported_at = fields.Datetime(copy=False, readonly=True)

    _amazon_report_unique = models.Constraint(
        'UNIQUE(instance_id, amazon_report_id)',
        'An Amazon customer-return report can be stored only once per instance.',
    )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.return.report') or 'New'
        return super().create(vals_list)

    def action_download_report(self):
        """Queue a durable Reports API request; do not wait in the browser."""
        self.ensure_one()
        if self.state not in ('draft', 'failed'):
            raise UserError(_("Only a draft or failed import can be queued."))
        job = self.env['amazon.phase7.job'].enqueue(
            self.instance_id, 'customer_returns', source=self,
            date_from=self.report_date, date_to=self.report_date,
        )
        if job.source_model != self._name or job.source_id != self.id:
            return self.instance_id._notify(
                _("Customer Returns"), _("Import job %s is already active.", job.display_name),
                'warning',
            )
        self.write({'state': 'queued', 'last_error': False})
        return self.instance_id._notify(
            _("Customer Returns"), _("Import job %s was queued.", job.display_name)
        )

    def action_process_returns(self):
        """Classify imported rows and apply only the configured stock policy.

        Phase 7 deliberately does not create credit notes or any accounting
        document. The legacy implementation did so here and was unsafe.
        """
        self.ensure_one()
        if self.state not in ('downloaded', 'processed'):
            raise UserError(_("Download the report first."))
        for line in self.line_ids.filtered(lambda item: item.state != 'processed'):
            with self.env.cr.savepoint():
                line._classify_and_apply()
        self.write({'state': 'processed'})
        return self.instance_id._notify(
            _("Customer Returns"), _("%s return event(s) were classified.", len(self.line_ids))
        )


class AmazonReturnReportLine(models.Model):
    _name = 'amazon.return.report.line'
    _description = 'Amazon FBA Customer Return'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'return_date desc, id desc'
    _check_company_auto = True

    report_id = fields.Many2one(
        'amazon.return.report', required=True, ondelete='restrict', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='report_id.instance_id', store=True,
        readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='report_id.company_id', store=True,
        readonly=True, index=True,
    )
    marketplace_id = fields.Char(
        related='instance_id.marketplace_id', store=True, readonly=True, index=True,
    )
    event_key = fields.Char(required=True, copy=False, index=True, readonly=True)
    amazon_order_id = fields.Char('Amazon Order ID', index=True, tracking=True)
    amazon_order_item_id = fields.Char(index=True)
    order_id = fields.Many2one(
        'amazon.sale.order', string='Amazon Order Record',
        ondelete='set null', index=True,
    )
    amazon_order_line_id = fields.Many2one(
        'amazon.sale.order.line', string='Amazon Order Line',
        ondelete='set null', index=True,
    )
    linked_sale_order_id = fields.Many2one(
        'sale.order', related='order_id.sale_order_id', store=True, readonly=True,
    )
    linked_sale_order_line_id = fields.Many2one('sale.order.line', ondelete='set null')
    sku = fields.Char(index=True)
    asin = fields.Char(index=True)
    fnsku = fields.Char(index=True)
    amazon_product_id = fields.Many2one(
        'amazon.product', ondelete='set null', index=True,
    )
    odoo_product_id = fields.Many2one('product.product', ondelete='restrict', index=True)
    product_name = fields.Char()
    quantity = fields.Float(default=1.0)
    fulfillment_center_id = fields.Char(index=True)
    return_date = fields.Datetime(index=True)
    detailed_disposition = fields.Char(index=True, tracking=True)
    return_reason = fields.Char(string='Reason', index=True)
    status = fields.Char(string='Amazon Status', index=True, tracking=True)
    license_plate_number = fields.Char(index=True)
    customer_comments = fields.Text(groups='sdlc_amazon_connector.group_amazon_manager')
    operational_disposition = fields.Selection([
        ('sellable', 'Sellable'),
        ('unsellable', 'Unsellable'),
        ('manual_review', 'Manual Review'),
    ], readonly=True, index=True, tracking=True)
    product_mapping_status = fields.Selection([
        ('mapped', 'Mapped'),
        ('unmapped', 'Unmapped Return Item'),
    ], default='unmapped', required=True, readonly=True, index=True)
    order_link_status = fields.Selection([
        ('line_linked', 'Order and Line Linked'),
        ('order_linked', 'Order Linked; Line Not Found'),
        ('line_ambiguous', 'Order Linked; Line Ambiguous'),
        ('order_not_found', 'Order Not Found'),
    ], default='order_not_found', required=True, readonly=True, index=True)
    raw_report_reference = fields.Char(index=True)
    raw_response = fields.Text(groups='sdlc_amazon_connector.group_amazon_technical_admin')
    imported_at = fields.Datetime(readonly=True)
    last_synced_at = fields.Datetime(readonly=True)
    linked_stock_move_id = fields.Many2one('stock.move', ondelete='restrict', copy=False)
    amazon_order_count = fields.Integer(compute='_compute_link_counts')
    sale_order_count = fields.Integer(compute='_compute_link_counts')
    stock_move_count = fields.Integer(compute='_compute_link_counts')
    reimbursement_count = fields.Integer(compute='_compute_link_counts')
    sync_log_count = fields.Integer(compute='_compute_link_counts')
    stock_action_state = fields.Selection([
        ('not_evaluated', 'Not Evaluated'),
        ('informational', 'Informational Only'),
        ('audit_only', 'Inventory Audit Only'),
        ('moved', 'Stock Move Created'),
        ('already_reflected', 'Already Reflected'),
        ('manual_review', 'Manual Review'),
    ], default='not_evaluated', required=True, index=True, tracking=True)
    inventory_reflected = fields.Boolean(
        string='Already Reflected by Inventory Audit', default=False, tracking=True,
    )
    manual_review_required = fields.Boolean(default=False, index=True, tracking=True)
    review_reason = fields.Text()
    state = fields.Selection([
        ('pending', 'Pending'), ('processed', 'Processed'), ('failed', 'Failed'),
    ], default='pending', required=True, index=True, tracking=True)
    # Kept only to preserve an installed legacy column. Phase 7 never creates it.
    credit_note_id = fields.Many2one('account.move', readonly=True)

    _event_unique = models.Constraint(
        'UNIQUE(instance_id, event_key)',
        'This Amazon customer-return event was already imported.',
    )
    _quantity_positive = models.Constraint(
        'CHECK(quantity > 0)', 'Amazon customer-return quantity must be positive.',
    )

    def _compute_link_counts(self):
        reimbursement_model = self.env['amazon.fba.reimbursement'].sudo()
        sync_log_model = self.env['amazon.sync.log'].sudo()
        for record in self:
            record.amazon_order_count = bool(record.order_id)
            record.sale_order_count = bool(record.linked_sale_order_id)
            record.stock_move_count = bool(record.linked_stock_move_id)
            record.reimbursement_count = reimbursement_model.search_count([
                ('linked_return_id', '=', record.id),
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

    def action_view_amazon_order(self):
        return self._open_linked_records(_('Amazon Order'), 'amazon.sale.order', self.order_id)

    def action_view_sale_order(self):
        return self._open_linked_records(_('Odoo Sale Order'), 'sale.order', self.linked_sale_order_id)

    def action_view_stock_move(self):
        return self._open_linked_records(_('Stock Move'), 'stock.move', self.linked_stock_move_id)

    def action_view_reimbursements(self):
        records = self.env['amazon.fba.reimbursement'].search([('linked_return_id', '=', self.id)])
        return self._open_linked_records(_('Reimbursements'), records._name, records)

    def action_view_sync_logs(self):
        records = self.env['amazon.sync.log'].search([
            ('source_model', '=', self._name), ('source_id', '=', self.id),
        ])
        return self._open_linked_records(_('Sync Logs'), records._name, records)

    @api.model
    def event_key_for_row(self, instance, row):
        # Amazon's customer-return report exposes no event/order-item ID. This
        # stable compound identity is based only on documented report columns.
        components = [
            instance.id, row.get('order-id'), row.get('sku'), row.get('fnsku'),
            row.get('return-date'), row.get('quantity'), row.get('fulfillment-center-id'),
            row.get('license-plate-number'), row.get('detailed-disposition'), row.get('reason'),
        ]
        return hashlib.sha256('|'.join(str(value or '').strip() for value in components).encode()).hexdigest()

    @api.model
    def _normalize_and_validate_row(self, row):
        """Normalize official header names and reject unsafe fabricated values."""
        normalized = {
            str(key or '').strip().lstrip('\ufeff').lower(): (
                value.strip() if isinstance(value, str) else value
            )
            for key, value in (row or {}).items()
            if key and key != '_extra_fields'
        }
        missing = []
        if not normalized.get('return-date'):
            missing.append('return-date')
        if not normalized.get('order-id'):
            missing.append('order-id')
        if not any(normalized.get(key) for key in ('sku', 'fnsku', 'asin')):
            missing.append('sku/fnsku/asin')
        if normalized.get('quantity') in (None, ''):
            missing.append('quantity')
        if missing:
            raise ValidationError(
                _("Malformed FBA customer-return row; missing %s.", ', '.join(missing))
            )
        quantity = self.env['amazon.phase7.stock.service'].number(
            normalized.get('quantity'), 0.0,
        )
        if quantity <= 0:
            raise ValidationError(_("FBA customer-return quantity must be positive."))
        # Validate the date before an event key can be persisted.
        self.env['amazon.phase7.stock.service'].datetime(normalized['return-date'])
        normalized['quantity'] = quantity
        return normalized

    @api.model
    def _resolve_order_links(self, instance, row, product_values):
        order = self.env['amazon.sale.order'].search([
            ('instance_id', '=', instance.id),
            ('amazon_order_ref', '=', row.get('order-id') or ''),
        ], limit=1)
        values = {
            'order_id': order.id or False,
            'amazon_order_line_id': False,
            'linked_sale_order_line_id': False,
            'order_link_status': 'order_not_found',
        }
        if not order:
            return values

        candidates = order.order_line_ids
        sku = row.get('sku') or ''
        asin = row.get('asin') or ''
        matches = candidates.filtered(lambda line: sku and (line.sku or '').strip() == sku)
        if not matches:
            matches = candidates.filtered(lambda line: asin and (line.asin or '').strip() == asin)
        if not matches and product_values.get('amazon_product_id'):
            matches = candidates.filtered(
                lambda line: line.amazon_product_id.id == product_values['amazon_product_id']
            )
        if len(matches) != 1:
            values['order_link_status'] = 'line_ambiguous' if len(matches) > 1 else 'order_linked'
            return values

        amazon_line = matches
        values.update({
            'amazon_order_line_id': amazon_line.id,
            'order_link_status': 'line_linked',
        })
        sale_order = order.sale_order_id
        product_id = amazon_line.odoo_product_id.id or product_values.get('odoo_product_id')
        if sale_order and product_id:
            sale_lines = sale_order.order_line.filtered(lambda line: line.product_id.id == product_id)
            if len(sale_lines) == 1:
                values['linked_sale_order_line_id'] = sale_lines.id
        return values

    def _classify_and_apply(self):
        self.ensure_one()
        disposition = (self.detailed_disposition or '').strip().upper().replace(' ', '_')
        operational = RETURN_DISPOSITION_MAP.get(disposition, 'manual_review')
        issues = []
        if self.product_mapping_status != 'mapped':
            issues.append(_("UNMAPPED RETURN ITEM: %s", self.sku or self.fnsku or self.asin))
        if self.order_link_status == 'order_not_found':
            issues.append(_("ORDER NOT FOUND: %s", self.amazon_order_id))
        elif self.order_link_status == 'line_ambiguous':
            issues.append(_("Amazon order line is ambiguous because the report has no order-item ID."))
        elif self.order_link_status == 'order_linked':
            issues.append(_("Amazon order was linked, but no unique item line matched the return."))
        if operational == 'manual_review':
            issues.append(_("Unknown Amazon return disposition: %s", self.detailed_disposition or _('empty')))

        vals = {
            'operational_disposition': operational,
            'last_synced_at': fields.Datetime.now(),
            'manual_review_required': bool(issues),
            'review_reason': '\n'.join(issues) or False,
            # Return rows are operational evidence. FBA Inventory reconciliation
            # remains the sole stock source of truth, preventing double counting.
            'stock_action_state': 'manual_review' if issues else 'audit_only',
            'state': 'processed',
        }
        self.write(vals)
        if operational == 'manual_review':
            self.env['amazon.smart.alert'].phase7_alert(
                self.instance_id, 'return:%s' % self.event_key,
                _("Unknown FBA return disposition"), vals['review_reason'],
                source=self, product=self.amazon_product_id,
            )
        if self.product_mapping_status != 'mapped':
            self.env['amazon.smart.alert'].phase7_alert(
                self.instance_id, 'return-unmapped:%s' % self.event_key,
                _("Unmapped FBA return item"), vals['review_reason'], source=self,
            )
        if self.order_link_status == 'order_not_found':
            self.env['amazon.smart.alert'].phase7_alert(
                self.instance_id, 'return-order:%s' % self.event_key,
                _("FBA return order not found"), vals['review_reason'],
                source=self, product=self.amazon_product_id,
            )
        return True

    @api.model
    def import_row(self, report, row):
        """Idempotently upsert one official customer-return report row."""
        row = self._normalize_and_validate_row(row)
        key = self.event_key_for_row(report.instance_id, row)
        record = self.search([
            ('instance_id', '=', report.instance_id.id), ('event_key', '=', key),
        ], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            report.instance_id, row.get('sku'), row.get('fnsku'), row.get('asin')
        )
        order_values = self._resolve_order_links(report.instance_id, row, product_values)
        vals = {
            'report_id': report.id,
            'event_key': key,
            'amazon_order_id': row.get('order-id') or '',
            'sku': row.get('sku') or '', 'asin': row.get('asin') or '',
            'fnsku': row.get('fnsku') or '', 'product_name': row.get('product-name') or '',
            'quantity': row['quantity'],
            'fulfillment_center_id': row.get('fulfillment-center-id') or '',
            'return_date': self.env['amazon.phase7.stock.service'].datetime(row.get('return-date')),
            'detailed_disposition': row.get('detailed-disposition') or '',
            'return_reason': row.get('reason') or '', 'status': row.get('status') or '',
            'license_plate_number': row.get('license-plate-number') or '',
            'customer_comments': row.get('customer-comments') or '',
            'raw_report_reference': report.amazon_report_id,
            'raw_response': json.dumps(row, default=str, sort_keys=True),
            'imported_at': record.imported_at or fields.Datetime.now(),
            'last_synced_at': fields.Datetime.now(),
            'product_mapping_status': (
                'mapped'
                if product_values.get('amazon_product_id') and product_values.get('odoo_product_id')
                else 'unmapped'
            ),
            **order_values,
            **product_values,
        }
        if record:
            # Preserve stock/review decisions while refreshing mutable Amazon fields.
            vals.pop('report_id', None)
            vals.pop('event_key', None)
            record.write(vals)
        else:
            record = self.create(vals)
        return record
