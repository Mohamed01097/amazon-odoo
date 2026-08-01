import json
import logging
from datetime import timedelta

from odoo import _, Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_is_zero

from .amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)


class AmazonInventoryReconciliationRun(models.Model):
    _name = 'amazon.inventory.reconciliation.run'
    _description = 'Amazon Inventory Reconciliation Run'
    _order = 'run_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default='New', copy=False, readonly=True)
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    run_date = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    trigger = fields.Selection([
        ('manual', 'Manual'),
        ('scheduled', 'Scheduled'),
    ], required=True, default='manual', readonly=True)
    mode = fields.Selection([
        ('manual', 'Manual'),
        ('automatic', 'Automatic'),
    ], required=True, default='manual', readonly=True,
       help=(
           "Manual records suggestions only. Automatic may apply only exact, "
           "quantity-balanced transfers between configured FBA locations."
       ))
    state = fields.Selection([
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], required=True, default='queued', copy=False, index=True)
    retry_count = fields.Integer(default=0, copy=False, readonly=True)
    max_retries = fields.Integer(default=5, readonly=True)
    next_run_at = fields.Datetime(copy=False, readonly=True, index=True)
    last_error = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    amazon_request_ids = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    raw_response = fields.Text(
        string='Raw Amazon Response', copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    reconciliation_ids = fields.One2many(
        'amazon.inventory.reconciliation', 'run_id', string='Inventory Audit Lines',
        copy=False,
    )
    products_checked = fields.Integer(readonly=True)
    matched_count = fields.Integer(readonly=True)
    mismatch_count = fields.Integer(readonly=True)
    critical_count = fields.Integer(readonly=True)
    pending_review_count = fields.Integer(readonly=True)
    transfer_count = fields.Integer(compute='_compute_transfer_count')

    _valid_retry_limits = models.Constraint(
        'CHECK (retry_count >= 0 AND max_retries >= 1 AND max_retries <= 25)',
        'Inventory reconciliation retry limits are invalid.',
    )

    @api.depends('reconciliation_ids.applied_picking_id')
    def _compute_transfer_count(self):
        for run in self:
            run.transfer_count = len(run.reconciliation_ids.applied_picking_id)

    @api.model_create_multi
    def create(self, vals_list):
        mode = self.env['ir.config_parameter'].sudo().get_param(
            'amazon_connector.inventory_reconciliation_mode', 'manual',
        )
        if mode not in ('manual', 'automatic'):
            mode = 'manual'
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code(
                        'amazon.inventory.reconciliation.run'
                    ) or 'New'
                )
            vals.setdefault('mode', mode)
        return super().create(vals_list)

    def _lock_run(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM amazon_inventory_reconciliation_run '
            'WHERE id = %s FOR UPDATE',
            [self.id],
        )
        self.invalidate_recordset()

    @staticmethod
    def _json(value):
        return json.dumps(value, default=str, sort_keys=True, indent=2)

    @staticmethod
    def _amazon_quantity(value, field_name):
        if value in (None, False, ''):
            return 0.0
        if isinstance(value, bool):
            raise ValidationError(_("Amazon returned an invalid %s.", field_name))
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Amazon returned an invalid %s.", field_name)) from exc
        if number < 0:
            raise ValidationError(_("Amazon returned a negative %s.", field_name))
        return number

    def _aggregate_amazon_summaries(self, response):
        self.ensure_one()
        payload = response.get('payload') if isinstance(response, dict) else None
        summaries = payload.get('inventorySummaries') if isinstance(payload, dict) else None
        if not isinstance(summaries, list):
            raise ValidationError(_("Amazon did not return a valid inventorySummaries list."))
        aggregated = {}
        for summary in summaries:
            if not isinstance(summary, dict):
                raise ValidationError(_("Amazon returned an invalid inventory summary."))
            sku = str(summary.get('sellerSku') or '').strip()
            if not sku:
                raise ValidationError(_("Amazon returned an inventory summary without sellerSku."))
            details = summary.get('inventoryDetails')
            if not isinstance(details, dict):
                raise ValidationError(_(
                    "Amazon omitted inventoryDetails for SKU %s. The audit requires details=true.",
                    sku,
                ))
            reserved = details.get('reservedQuantity') or {}
            unfulfillable = details.get('unfulfillableQuantity') or {}
            if not isinstance(reserved, dict) or not isinstance(unfulfillable, dict):
                raise ValidationError(_("Amazon returned invalid inventory details for SKU %s.", sku))
            values = {
                'sellable': self._amazon_quantity(
                    details.get('fulfillableQuantity'), 'fulfillableQuantity',
                ),
                'reserved': self._amazon_quantity(
                    reserved.get('totalReservedQuantity'), 'totalReservedQuantity',
                ),
                'unsellable': self._amazon_quantity(
                    unfulfillable.get('totalUnfulfillableQuantity'),
                    'totalUnfulfillableQuantity',
                ),
                'inbound_working': self._amazon_quantity(
                    details.get('inboundWorkingQuantity'), 'inboundWorkingQuantity',
                ),
                'inbound_shipped': self._amazon_quantity(
                    details.get('inboundShippedQuantity'), 'inboundShippedQuantity',
                ),
                'inbound_receiving': self._amazon_quantity(
                    details.get('inboundReceivingQuantity'), 'inboundReceivingQuantity',
                ),
            }
            bucket = aggregated.setdefault(sku, {
                'sellable': 0.0,
                'reserved': 0.0,
                'unsellable': 0.0,
                'inbound_working': 0.0,
                'inbound_shipped': 0.0,
                'inbound_receiving': 0.0,
                'raw': [],
            })
            for key, quantity in values.items():
                bucket[key] += quantity
            bucket['raw'].append(summary)
        return aggregated

    def _validate_locations(self):
        self.ensure_one()
        instance = self.instance_id
        locations = {
            'sellable': instance.fba_sellable_location_id,
            'reserved': instance.fba_reserved_location_id,
            'unsellable': instance.fba_unsellable_location_id,
            'transit': instance.fba_transit_location_id,
        }
        if any(not location for location in locations.values()):
            raise UserError(_(
                "Configure Amazon Sellable, Reserved, Unsellable, and Transit locations first."
            ))
        expected_usage = {
            'sellable': 'internal',
            'reserved': 'internal',
            'unsellable': 'internal',
            'transit': 'transit',
        }
        if len({location.id for location in locations.values()}) != 4:
            raise UserError(_("The four Amazon inventory locations must be distinct."))
        for role, location in locations.items():
            if not location.active or location.usage != expected_usage[role]:
                raise UserError(_("The configured Amazon %s location is invalid.", role.title()))
            if location.company_id != self.company_id:
                raise UserError(_("Amazon inventory locations must belong to the audit company."))
        return locations

    def _odoo_quantities(self, product, locations):
        self.ensure_one()
        if product.company_id and product.company_id != self.company_id:
            raise ValidationError(_("Product %s belongs to another company.", product.display_name))
        product = product.with_company(self.company_id)
        return {
            role: product.with_context(location=location.id).qty_available
            for role, location in locations.items()
        }

    @staticmethod
    def _classification(amazon_values, odoo_values, rounding):
        differences = {
            key: amazon_values[key] - odoo_values[key]
            for key in ('sellable', 'reserved', 'unsellable', 'transit')
        }

        def zero(value):
            return float_is_zero(value, precision_rounding=rounding)

        def equal(left, right):
            return float_compare(left, right, precision_rounding=rounding) == 0

        if all(zero(value) for value in differences.values()):
            return differences, 'none', 'matched', 'normal'

        total_difference = sum(differences.values())
        if not zero(total_difference):
            return differences, 'inventory_adjustment', 'pending_review', 'critical'

        if (
            differences['sellable'] < 0
            and differences['reserved'] > 0
            and equal(-differences['sellable'], differences['reserved'])
            and zero(differences['unsellable'])
            and zero(differences['transit'])
        ):
            return differences, 'sellable_to_reserved', 'pending_review', 'warning'
        if (
            differences['sellable'] < 0
            and differences['unsellable'] > 0
            and equal(-differences['sellable'], differences['unsellable'])
            and zero(differences['reserved'])
            and zero(differences['transit'])
        ):
            return differences, 'sellable_to_unsellable', 'pending_review', 'warning'
        if (
            differences['transit'] < 0
            and differences['sellable'] > 0
            and equal(-differences['transit'], differences['sellable'])
            and zero(differences['reserved'])
            and zero(differences['unsellable'])
        ):
            return differences, 'transit_to_sellable', 'pending_review', 'warning'
        return differences, 'manual_review', 'pending_review', 'critical'

    def _prepare_lines(self, response):
        self.ensure_one()
        locations = self._validate_locations()
        amazon_by_sku = self._aggregate_amazon_summaries(response)
        amazon_products = self.env['amazon.product'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('sku', '!=', False),
        ])
        products_by_sku = {
            product.sku.strip(): product
            for product in amazon_products
            if product.sku and (
                product.fulfillment_channel == 'AFN'
                or product.sku.strip() in amazon_by_sku
            )
        }
        all_skus = sorted(set(amazon_by_sku) | set(products_by_sku))
        commands = []
        for sku in all_skus:
            summary = amazon_by_sku.get(sku, {})
            amazon_product = products_by_sku.get(sku)
            odoo_product = amazon_product.odoo_product_id if amazon_product else False
            amazon_values = {
                'sellable': summary.get('sellable', 0.0),
                'reserved': summary.get('reserved', 0.0),
                'unsellable': summary.get('unsellable', 0.0),
                # Odoo Transit represents physically shipped inventory. Amazon
                # inboundWorking is intentionally excluded because it is only
                # inventory the seller has notified Amazon about.
                'transit': (
                    summary.get('inbound_shipped', 0.0)
                    + summary.get('inbound_receiving', 0.0)
                ),
            }
            if odoo_product:
                odoo_values = self._odoo_quantities(odoo_product, locations)
                rounding = odoo_product.uom_id.rounding or 0.01
                differences, suggested, status, severity = self._classification(
                    amazon_values, odoo_values, rounding,
                )
            else:
                odoo_values = dict.fromkeys(
                    ('sellable', 'reserved', 'unsellable', 'transit'), 0.0,
                )
                differences = {
                    key: amazon_values[key] for key in amazon_values
                }
                suggested = 'manual_review'
                status = 'pending_review'
                severity = 'critical'
            values = {
                'sku': sku,
                'amazon_product_id': amazon_product.id if amazon_product else False,
                'odoo_product_id': odoo_product.id if odoo_product else False,
                'amazon_sellable': amazon_values['sellable'],
                'amazon_reserved': amazon_values['reserved'],
                'amazon_unsellable': amazon_values['unsellable'],
                'amazon_inbound': amazon_values['transit'],
                'amazon_inbound_working': summary.get('inbound_working', 0.0),
                'amazon_inbound_shipped': summary.get('inbound_shipped', 0.0),
                'amazon_inbound_receiving': summary.get('inbound_receiving', 0.0),
                'odoo_sellable': odoo_values['sellable'],
                'odoo_reserved': odoo_values['reserved'],
                'odoo_unsellable': odoo_values['unsellable'],
                'odoo_transit': odoo_values['transit'],
                'difference_sellable': differences['sellable'],
                'difference_reserved': differences['reserved'],
                'difference_unsellable': differences['unsellable'],
                'difference_inbound': differences['transit'],
                'suggested_action': suggested,
                'status': status,
                'severity': severity,
                'raw_response': self._json(summary.get('raw', [])),
            }
            commands.append(Command.create(values))
            if amazon_product:
                amazon_product.amazon_qty = amazon_values['sellable']
        return commands

    def _refresh_counts(self):
        self.ensure_one()
        lines = self.reconciliation_ids
        self.write({
            'products_checked': len(lines),
            'matched_count': len(lines.filtered(lambda line: line.status == 'matched')),
            'mismatch_count': len(lines.filtered(lambda line: line.status != 'matched')),
            'critical_count': len(lines.filtered(lambda line: line.severity == 'critical')),
            'pending_review_count': len(lines.filtered(
                lambda line: line.status in ('pending_review', 'failed')
            )),
        })

    def _schedule_retry(self, error_message):
        self.ensure_one()
        retries = self.retry_count + 1
        vals = {'retry_count': retries, 'last_error': str(error_message)[:10000]}
        if retries >= self.max_retries:
            vals.update(state='failed', next_run_at=False)
        else:
            vals.update(
                state='queued',
                next_run_at=fields.Datetime.now() + timedelta(
                    minutes=min(2 ** max(retries - 1, 0), 60)
                ),
            )
        self.write(vals)

    def _process_run(self):
        self.ensure_one()
        if self.state == 'completed':
            return False
        try:
            with self.env.cr.savepoint():
                self._lock_run()
                if self.state == 'completed':
                    return False
                self.write({'state': 'running', 'next_run_at': False, 'last_error': False})
                self.instance_id._check_required_fields()
                access_token = self.instance_id._get_access_token_or_raise()
                response = self.instance_id._api_call_safe(
                    AmazonAPI().get_all_inventory_summaries,
                    self.instance_id,
                    access_token,
                    details=True,
                    error_msg="Failed to retrieve Amazon FBA inventory summaries",
                )
                self.reconciliation_ids.unlink()
                self.write({
                    'reconciliation_ids': self._prepare_lines(response),
                    'raw_response': self._json(response),
                    'amazon_request_ids': self._json(
                        response.get('_amazon_request_ids') or []
                    ),
                })
                if self.mode == 'automatic':
                    candidates = self.reconciliation_ids.filtered(lambda line: (
                        line.status == 'pending_review'
                        and line.suggested_action in (
                            'sellable_to_reserved',
                            'sellable_to_unsellable',
                            'transit_to_sellable',
                        )
                    ))
                    for line in candidates:
                        try:
                            with self.env.cr.savepoint():
                                line.sudo().with_context(
                                    automatic_inventory_reconciliation=True,
                                )._apply_suggested_action()
                        except Exception as exc:
                            _logger.warning(
                                "Automatic Amazon inventory action failed for %s: %s",
                                line.sku, exc,
                            )
                            line.sudo().write({
                                'status': 'failed',
                                'error_message': str(exc)[:5000],
                            })
                self._refresh_counts()
                self.write({'state': 'completed'})
                self.instance_id.sudo().write({
                    'last_inventory_audit_at': fields.Datetime.now(),
                    'last_stock_sync': fields.Datetime.now(),
                })
                log = self.env['amazon.sync.log'].sudo().log_start(
                    self.instance_id,
                    'inventory_reconciliation',
                    res_model=self._name,
                    res_id=self.id,
                )
                log.log_success(
                    summary=_(
                        "Inventory audit %s completed: %s checked, %s mismatches.",
                        self.name, self.products_checked, self.mismatch_count,
                    ),
                    records_processed=self.products_checked,
                    records_created=self.products_checked,
                    response_data={
                        'amazon_request_ids': response.get('_amazon_request_ids') or [],
                        'matched': self.matched_count,
                        'mismatches': self.mismatch_count,
                    },
                )
            return True
        except Exception as exc:
            _logger.exception("Amazon inventory audit %s failed", self.id)
            self._schedule_retry(exc)
            return False

    @api.model
    def cron_process_inventory_audits(self):
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_inventory_reconciliation_run
             WHERE state = 'queued'
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, run_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        """, [now])
        row = self.env.cr.fetchone()
        if not row:
            return False
        return self.browse(row[0]).with_context(
            amazon_source_model=self._name,
            amazon_source_id=row[0],
            amazon_operation='inventory_reconciliation',
        )._process_run()

    @api.model
    def cron_enqueue_inventory_audits(self):
        created = self.env[self._name]
        for instance in self.env['amazon.instance'].search([
            ('active', '=', True),
            ('inventory_reconciliation_enabled', '=', True),
        ]):
            pending = self.search_count([
                ('instance_id', '=', instance.id),
                ('state', 'in', ('queued', 'running')),
            ])
            if not pending:
                created |= self.create({
                    'instance_id': instance.id,
                    'trigger': 'scheduled',
                })
        return created

    def action_retry(self):
        for run in self:
            if run.state != 'failed':
                raise UserError(_("Only failed inventory audits can be retried."))
            run.write({
                'state': 'queued',
                'retry_count': 0,
                'next_run_at': False,
                'last_error': False,
            })
        return True

    def action_open_differences(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_action'
        ).read()[0]
        action['domain'] = [
            ('run_id', '=', self.id),
            ('status', '!=', 'matched'),
        ]
        action['context'] = {'default_run_id': self.id}
        return action

    def action_open_lines(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_action'
        ).read()[0]
        action['domain'] = [('run_id', '=', self.id)]
        action['context'] = {'default_run_id': self.id}
        return action

    def action_open_transfers(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Reconciliation Transfers'),
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('amazon_inventory_reconciliation_id.run_id', '=', self.id)],
        }


class AmazonInventoryReconciliation(models.Model):
    _name = 'amazon.inventory.reconciliation'
    _description = 'Amazon Inventory Reconciliation'
    _order = 'run_date desc, sku, id'
    _rec_name = 'sku'
    _check_company_auto = True

    run_id = fields.Many2one(
        'amazon.inventory.reconciliation.run', required=True,
        ondelete='cascade', index=True, check_company=True,
    )
    run_date = fields.Datetime(
        related='run_id.run_date', store=True, readonly=True, index=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='run_id.instance_id', store=True,
        readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='run_id.company_id', store=True,
        readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one(
        'amazon.product', ondelete='set null', index=True,
    )
    odoo_product_id = fields.Many2one(
        'product.product', string='Product', ondelete='set null', index=True,
        check_company=True,
    )
    sku = fields.Char(string='SKU', required=True, index=True)
    amazon_sellable = fields.Float(readonly=True)
    amazon_reserved = fields.Float(readonly=True)
    amazon_unsellable = fields.Float(readonly=True)
    amazon_inbound = fields.Float(
        readonly=True,
        help="Physical inbound: inboundShippedQuantity + inboundReceivingQuantity.",
    )
    amazon_inbound_working = fields.Float(
        readonly=True,
        help="Notified but not necessarily shipped; excluded from Odoo Transit comparison.",
    )
    amazon_inbound_shipped = fields.Float(readonly=True)
    amazon_inbound_receiving = fields.Float(readonly=True)
    odoo_sellable = fields.Float(readonly=True)
    odoo_reserved = fields.Float(readonly=True)
    odoo_unsellable = fields.Float(readonly=True)
    odoo_transit = fields.Float(readonly=True)
    difference_sellable = fields.Float(readonly=True)
    difference_reserved = fields.Float(readonly=True)
    difference_unsellable = fields.Float(readonly=True)
    difference_inbound = fields.Float(readonly=True)
    suggested_action = fields.Selection([
        ('none', 'No Action'),
        ('sellable_to_reserved', 'Sellable → Reserved'),
        ('sellable_to_unsellable', 'Sellable → Unsellable'),
        ('transit_to_sellable', 'Transit → Sellable'),
        ('inventory_adjustment', 'Inventory Adjustment'),
        ('manual_review', 'Manual Review'),
    ], required=True, default='manual_review', readonly=True, index=True)
    severity = fields.Selection([
        ('normal', 'Normal'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    ], required=True, default='normal', readonly=True, index=True)
    status = fields.Selection([
        ('matched', 'Matched'),
        ('pending_review', 'Pending Review'),
        ('applied', 'Applied'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], required=True, default='pending_review', copy=False, index=True)
    applied_picking_id = fields.Many2one(
        'stock.picking', string='Applied Transfer', copy=False, readonly=True,
        ondelete='restrict', check_company=True,
    )
    applied_at = fields.Datetime(copy=False, readonly=True)
    applied_by_id = fields.Many2one(
        'res.users', copy=False, readonly=True, ondelete='set null',
    )
    error_message = fields.Text(
        copy=False, readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    raw_response = fields.Text(
        string='Raw Amazon Response', readonly=True,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_sku_per_run = models.Constraint(
        'UNIQUE (run_id, sku)',
        'A SKU can appear only once in an inventory reconciliation run.',
    )

    def _check_apply_access(self):
        if (
            self.env.su
            or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager')
            or self.env.user.has_group('stock.group_stock_manager')
        ):
            return
        raise AccessError(_(
            "Only an Amazon Connector Manager or Inventory Administrator can "
            "apply inventory reconciliation actions."
        ))

    def _movement(self):
        self.ensure_one()
        instance = self.instance_id
        if self.suggested_action == 'sellable_to_reserved':
            return (
                instance.fba_sellable_location_id,
                instance.fba_reserved_location_id,
                self.difference_reserved,
                'reconciliation_reserved',
            )
        if self.suggested_action == 'sellable_to_unsellable':
            return (
                instance.fba_sellable_location_id,
                instance.fba_unsellable_location_id,
                self.difference_unsellable,
                'reconciliation_unsellable',
            )
        if self.suggested_action == 'transit_to_sellable':
            return (
                instance.fba_transit_location_id,
                instance.fba_sellable_location_id,
                self.difference_sellable,
                'reconciliation_sellable',
            )
        if self.suggested_action == 'inventory_adjustment':
            raise UserError(_(
                "This audit found a total-quantity difference. Review the physical cause "
                "and use Odoo's standard Inventory Adjustment workflow; the connector "
                "will not write stock quantities directly."
            ))
        raise UserError(_("This difference requires manual review and has no safe transfer."))

    def _internal_picking_type(self, source, destination):
        self.ensure_one()
        warehouse = destination.warehouse_id or source.warehouse_id
        picking_type = (
            warehouse.int_type_id
            if warehouse and warehouse.company_id == self.company_id
            else False
        )
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', self.company_id.id),
            ], order='warehouse_id, sequence, id', limit=1)
        if not picking_type:
            raise UserError(_("No Internal Transfer operation type exists for the audit company."))
        return picking_type

    def _apply_suggested_action(self):
        self.ensure_one()
        self._check_apply_access()
        with self.env.cr.savepoint():
            self.env.cr.execute(
                'SELECT id FROM amazon_inventory_reconciliation '
                'WHERE id = %s FOR UPDATE',
                [self.id],
            )
            self.invalidate_recordset()
            if self.status != 'pending_review':
                raise UserError(_("This reconciliation difference is no longer pending."))
            if self.applied_picking_id:
                raise UserError(_("This reconciliation difference already has a transfer."))
            if not self.odoo_product_id or not self.odoo_product_id.is_storable:
                raise UserError(_("The SKU must map to an inventory-tracked Odoo product."))
            source, destination, quantity, movement_type = self._movement()
            if not source or not destination or source == destination:
                raise UserError(_("The configured reconciliation locations are invalid."))
            if source.company_id != self.company_id or destination.company_id != self.company_id:
                raise UserError(_("Reconciliation locations must belong to the audit company."))
            rounding = self.odoo_product_id.uom_id.rounding or 0.01
            if float_compare(quantity, 0.0, precision_rounding=rounding) <= 0:
                raise UserError(_("The suggested transfer quantity is not positive."))
            picking_type = self._internal_picking_type(source, destination)
            picking = self.env['stock.picking'].sudo().with_company(self.company_id).create({
                'picking_type_id': picking_type.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
                'company_id': self.company_id.id,
                'origin': self.run_id.name,
                'amazon_instance_id': self.instance_id.id,
                'amazon_inventory_reconciliation_id': self.id,
                'amazon_fba_movement_type': movement_type,
                'move_type': 'one',
                'move_ids': [Command.create({
                    'product_id': self.odoo_product_id.id,
                    'product_uom_qty': quantity,
                    'product_uom': self.odoo_product_id.uom_id.id,
                    'location_id': source.id,
                    'location_dest_id': destination.id,
                    'company_id': self.company_id.id,
                })],
            })
            picking.action_confirm()
            picking.action_assign()
            move = picking.move_ids
            if (
                move.state != 'assigned'
                or move.product_uom.compare(move.quantity, move.product_uom_qty) < 0
            ):
                raise UserError(_(
                    "Insufficient stock in %s; standard Odoo reservation could not reserve %s %s.",
                    source.display_name, quantity, self.odoo_product_id.uom_id.name,
                ))
            if not move.move_line_ids:
                raise UserError(_("Odoo did not create the required stock move line."))
            result = picking.with_context(
                picking_ids_not_to_backorder=picking.ids,
            ).button_validate()
            if isinstance(result, dict) or picking.state != 'done':
                raise UserError(_("Transfer %s requires manual stock details.", picking.name))
            self.write({
                'status': 'applied',
                'applied_picking_id': picking.id,
                'applied_at': fields.Datetime.now(),
                'applied_by_id': self.env.user.id,
                'error_message': False,
            })
            return picking

    def action_apply_suggested_action(self):
        for reconciliation in self:
            reconciliation._apply_suggested_action()
            reconciliation.run_id._refresh_counts()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Inventory Reconciliation'),
                'message': _("The suggested stock transfer was applied."),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_ignore_difference(self):
        self._check_apply_access()
        for reconciliation in self:
            if reconciliation.status not in ('pending_review', 'failed'):
                raise UserError(_("Only a pending or failed difference can be ignored."))
            if reconciliation.applied_picking_id:
                raise UserError(_("An applied reconciliation transfer cannot be ignored."))
            reconciliation.write({
                'status': 'ignored',
                'error_message': False,
            })
            reconciliation.run_id._refresh_counts()
        return True

    def action_open_transfers(self):
        self.ensure_one()
        if not self.applied_picking_id:
            raise UserError(_("This reconciliation difference has no applied transfer."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Reconciliation Transfer'),
            'res_model': 'stock.picking',
            'res_id': self.applied_picking_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class AmazonInstanceInventoryReconciliation(models.Model):
    _inherit = 'amazon.instance'

    inventory_reconciliation_enabled = fields.Boolean(
        string='Scheduled Inventory Audit', default=True,
        help="Create a read-only Amazon FBA inventory audit on the daily schedule.",
    )
    last_inventory_audit_at = fields.Datetime(readonly=True)
    reconciliation_run_ids = fields.One2many(
        'amazon.inventory.reconciliation.run', 'instance_id',
        string='Inventory Audit History',
    )
    reconciliation_run_count = fields.Integer(
        compute='_compute_reconciliation_run_count', string='Inventory Audits',
    )

    @api.depends('reconciliation_run_ids')
    def _compute_reconciliation_run_count(self):
        for instance in self:
            instance.reconciliation_run_count = len(instance.reconciliation_run_ids)

    def action_run_inventory_audit(self):
        self.ensure_one()
        self._check_required_fields()
        active_run = self.env['amazon.inventory.reconciliation.run'].search([
            ('instance_id', '=', self.id),
            ('state', 'in', ('queued', 'running')),
        ], limit=1)
        run = active_run or self.env['amazon.inventory.reconciliation.run'].create({
            'instance_id': self.id,
            'trigger': 'manual',
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Audit'),
            'res_model': 'amazon.inventory.reconciliation.run',
            'res_id': run.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_inventory_audits(self):
        self.ensure_one()
        action = self.env.ref(
            'sdlc_amazon_connector.amazon_inventory_reconciliation_run_action'
        ).read()[0]
        action['domain'] = [('instance_id', '=', self.id)]
        action['context'] = {'default_instance_id': self.id}
        return action


class StockPickingInventoryReconciliation(models.Model):
    _inherit = 'stock.picking'

    amazon_inventory_reconciliation_id = fields.Many2one(
        'amazon.inventory.reconciliation', string='Amazon Inventory Reconciliation',
        copy=False, readonly=True, ondelete='restrict', check_company=True,
    )
    amazon_fba_movement_type = fields.Selection(selection_add=[
        ('reconciliation_reserved', 'Amazon Sellable to Reserved'),
        ('reconciliation_unsellable', 'Amazon Sellable to Unsellable'),
        ('reconciliation_sellable', 'Amazon Transit to Sellable'),
    ], ondelete={
        'reconciliation_reserved': 'set null',
        'reconciliation_unsellable': 'set null',
        'reconciliation_sellable': 'set null',
    })
