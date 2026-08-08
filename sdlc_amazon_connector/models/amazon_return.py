import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
    event_key = fields.Char(required=True, copy=False, index=True, readonly=True)
    amazon_order_id = fields.Char(index=True, tracking=True)
    amazon_order_item_id = fields.Char(index=True)
    order_id = fields.Many2one(
        'amazon.sale.order', string='Amazon Order',
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

    def _classify_and_apply(self):
        self.ensure_one()
        disposition = (self.detailed_disposition or '').strip().upper().replace(' ', '_')
        operational = RETURN_DISPOSITION_MAP.get(disposition, 'manual_review')
        vals = {'operational_disposition': operational, 'last_synced_at': fields.Datetime.now()}
        if operational == 'manual_review':
            vals.update(
                manual_review_required=True,
                stock_action_state='manual_review',
                review_reason=_("Unknown Amazon return disposition: %s", self.detailed_disposition or _('empty')),
                state='processed',
            )
            self.write(vals)
            self.env['amazon.smart.alert'].phase7_alert(
                self.instance_id, 'return:%s' % self.event_key,
                _("Unknown FBA return disposition"), vals['review_reason'],
                source=self, product=self.amazon_product_id,
            )
            return False
        self.write(vals)
        self.env['amazon.phase7.stock.service'].apply_return(self)
        self.write({'state': 'processed'})
        return True

    @api.model
    def import_row(self, report, row):
        """Idempotently upsert one official customer-return report row."""
        key = self.event_key_for_row(report.instance_id, row)
        record = self.search([
            ('instance_id', '=', report.instance_id.id), ('event_key', '=', key),
        ], limit=1)
        product_values = self.env['amazon.phase7.stock.service'].resolve_product(
            report.instance_id, row.get('sku'), row.get('fnsku'), row.get('asin')
        )
        order = self.env['amazon.sale.order'].search([
            ('instance_id', '=', report.instance_id.id),
            ('amazon_order_ref', '=', row.get('order-id') or ''),
        ], limit=1)
        vals = {
            'report_id': report.id,
            'event_key': key,
            'amazon_order_id': row.get('order-id') or '',
            'sku': row.get('sku') or '', 'asin': row.get('asin') or '',
            'fnsku': row.get('fnsku') or '', 'product_name': row.get('product-name') or '',
            'quantity': self.env['amazon.phase7.stock.service'].number(row.get('quantity'), 1.0),
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
            'order_id': order.id or False,
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
