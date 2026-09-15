import logging
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

from .amazon_api import AmazonAPI, REPORT_FBA_SHIPMENT

_logger = logging.getLogger(__name__)

REPORT_MAX_DAYS = 30
REPORT_LATENCY_HOURS = 4


class AmazonFbaSaleStockCutoverBaseline(models.Model):
    """Per-order-item fulfillment evidence captured before cutover.

    Populated once from GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL.
    Each record stores B — units fulfilled on or before cutover — for
    one (instance, order, item) triple.  Used during historical order
    import to initialise processed_fulfilled_qty so delta processing
    depletes only the post-cutover portion.
    """

    _name = 'amazon.fba.sale.stock.cutover.baseline'
    _description = 'FBA Cutover Fulfillment Evidence'
    _order = 'id'
    _check_company_auto = True
    _rec_name = 'amazon_order_item_id'

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    run_id = fields.Many2one(
        'amazon.fba.sale.stock.cutover.run', required=True,
        ondelete='cascade', index=True, readonly=True,
    )
    amazon_order_ref = fields.Char(required=True, index=True, readonly=True)
    amazon_order_item_id = fields.Char(required=True, index=True, readonly=True)
    sku = fields.Char(required=True, index=True, readonly=True)
    fulfilled_before_cutover = fields.Float(
        required=True, default=0.0, readonly=True,
        help="SUM(quantity-shipped) from the FBA Fulfilled Shipments report "
             "where shipment-date <= cutover_at.",
    )
    cutover_at = fields.Datetime(required=True, readonly=True)
    shipment_report_rows = fields.Integer(
        default=0, readonly=True,
        help="Number of shipment report rows aggregated into this baseline.",
    )

    _unique_baseline = models.Constraint(
        'UNIQUE (instance_id, amazon_order_ref, amazon_order_item_id)',
        'One cutover baseline per order item per instance.',
    )
    _valid_baseline_qty = models.Constraint(
        'CHECK (fulfilled_before_cutover >= 0)',
        'Fulfilled-before-cutover quantity must be non-negative.',
    )


class AmazonFbaSaleStockCutoverRun(models.Model):
    """One-time cutover evidence build run.

    Tracks the lifecycle of building shipment-evidence baselines from
    GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL report windows.
    """

    _name = 'amazon.fba.sale.stock.cutover.run'
    _description = 'FBA Cutover Evidence Build Run'
    _order = 'create_date desc, id desc'
    _check_company_auto = True
    _rec_name = 'display_name'

    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        check_company=True, readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True,
        readonly=True, index=True,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('building', 'Building'),
        ('ready', 'Ready'),
        ('activated', 'Activated'),
        ('cancelled', 'Cancelled'),
    ], required=True, default='draft', index=True, copy=False)
    history_start_at = fields.Datetime(
        required=True, readonly=True,
        help="Start of the historical coverage period.  Orders older than "
             "this that were fulfilled before cutover will NOT have baseline "
             "evidence and must be handled explicitly.",
    )
    cutover_at = fields.Datetime(
        required=True, readonly=True,
        help="The cutover boundary.  shipment-date <= cutover_at → included "
             "in the baseline (fulfilled_before_cutover).",
    )
    report_window_count = fields.Integer(
        default=0, readonly=True,
        help="Number of ≤30-day report windows needed to cover the period.",
    )
    report_windows_completed = fields.Integer(
        default=0, readonly=True,
    )
    report_windows_failed = fields.Integer(
        default=0, readonly=True,
    )
    raw_row_count = fields.Integer(default=0, readonly=True)
    included_row_count = fields.Integer(
        default=0, readonly=True,
        help="Rows with shipment-date <= cutover_at.",
    )
    excluded_row_count = fields.Integer(
        default=0, readonly=True,
        help="Rows with shipment-date > cutover_at (excluded from baselines).",
    )
    baseline_count = fields.Integer(default=0, readonly=True)
    total_fulfilled_before_cutover = fields.Float(default=0.0, readonly=True)
    unique_orders = fields.Integer(default=0, readonly=True)
    unique_skus = fields.Integer(default=0, readonly=True)
    safety_delay_hours = fields.Integer(
        default=REPORT_LATENCY_HOURS, readonly=True,
        help="Minimum hours after cutover_at before the build can be "
             "finalised, to account for Amazon report pipeline latency.",
    )
    last_error = fields.Text(readonly=True)
    baseline_ids = fields.One2many(
        'amazon.fba.sale.stock.cutover.baseline', 'run_id',
        string='Baseline Evidence', readonly=True,
    )
    shipment_evidence_ids = fields.One2many(
        'amazon.fba.sale.stock.cutover.shipment', 'run_id',
        string='Raw Shipment Evidence', readonly=True,
    )

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Cutover %s / %s" % (
                rec.instance_id.name or '?',
                fields.Datetime.to_string(rec.cutover_at) if rec.cutover_at else 'no date',
            )

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    @api.model
    def create_cutover_run(self, instance_id, cutover_at, history_start_at):
        """Manager-only entry point to create a cutover evidence run."""
        self.env['amazon.instance']._check_amazon_manager_access()
        instance = self.env['amazon.instance'].browse(instance_id)
        if not instance.exists():
            raise UserError(_("Amazon instance %s not found.", instance_id))
        instance.ensure_one()

        cutover_dt = fields.Datetime.to_datetime(cutover_at)
        history_dt = fields.Datetime.to_datetime(history_start_at)
        if not cutover_dt or not history_dt:
            raise UserError(_("Both cutover_at and history_start_at are required."))
        if history_dt >= cutover_dt:
            raise UserError(_("history_start_at must be before cutover_at."))

        existing = self.sudo().search([
            ('instance_id', '=', instance.id),
            ('state', 'in', ('draft', 'building', 'ready', 'activated')),
        ], limit=1)
        if existing:
            raise UserError(_(
                "An active cutover run already exists for this instance: %s (state=%s). "
                "Cancel it first to create a new one.",
                existing.display_name, existing.state,
            ))

        total_days = (cutover_dt - history_dt).total_seconds() / 86400
        window_count = max(1, int(total_days / REPORT_MAX_DAYS) + (1 if total_days % REPORT_MAX_DAYS else 0))

        return self.sudo().create({
            'instance_id': instance.id,
            'cutover_at': cutover_dt,
            'history_start_at': history_dt,
            'report_window_count': window_count,
        })

    def action_build_baselines(self, access_token=None):
        """Fetch shipment reports and build per-order-item baselines.

        This is the main entry point — calls Amazon read-only report API,
        stores raw evidence rows, then aggregates into baselines.
        """
        self.ensure_one()
        self.env['amazon.instance']._check_amazon_manager_access()
        if self.state not in ('draft', 'building'):
            raise UserError(_("Baseline build is only allowed in draft or building state."))

        self.write({'state': 'building', 'last_error': False})

        instance = self.instance_id
        api = AmazonAPI()
        if not access_token:
            access_token = instance._get_access_token_or_raise()

        cutover_dt = self.cutover_at
        history_dt = self.history_start_at

        windows = self._compute_report_windows(history_dt, cutover_dt)
        self.write({'report_window_count': len(windows)})

        completed = 0
        failed = 0
        total_raw = 0

        for win_start, win_end in windows:
            start_iso = win_start.strftime('%Y-%m-%dT%H:%M:%SZ')
            end_iso = win_end.strftime('%Y-%m-%dT%H:%M:%SZ')
            _logger.info(
                "Cutover run %s: requesting shipment report %s → %s",
                self.id, start_iso, end_iso,
            )
            try:
                rows = api.fetch_report_rows(
                    instance, access_token, REPORT_FBA_SHIPMENT,
                    start_date=start_iso, end_date=end_iso,
                )
                self._store_raw_evidence(rows, win_start, win_end)
                total_raw += len(rows)
                completed += 1
            except Exception as exc:
                failed += 1
                _logger.error(
                    "Cutover run %s: report window %s→%s failed: %s",
                    self.id, start_iso, end_iso, exc,
                )
                self.write({
                    'report_windows_completed': completed,
                    'report_windows_failed': failed,
                    'raw_row_count': total_raw,
                    'last_error': "Window %s→%s failed: %s" % (start_iso, end_iso, str(exc)[:2000]),
                })
                return False

        self.write({
            'report_windows_completed': completed,
            'report_windows_failed': failed,
            'raw_row_count': total_raw,
        })

        self._aggregate_baselines()

        all_complete = (completed == len(windows) and failed == 0)
        if all_complete and self._is_safety_delay_passed():
            self.write({'state': 'ready'})
        elif all_complete:
            self.write({
                'last_error': "All report windows complete but safety delay has not passed. "
                              "cutover_at + %d hours = %s. Current time must be after that." % (
                                  self.safety_delay_hours,
                                  fields.Datetime.to_string(
                                      cutover_dt + timedelta(hours=self.safety_delay_hours)
                                  ),
                              ),
            })

        return all_complete

    def action_mark_ready(self):
        """Manually transition to ready after safety delay passes."""
        self.ensure_one()
        self.env['amazon.instance']._check_amazon_manager_access()
        if self.state != 'building':
            raise UserError(_("Only a building run can be marked ready."))
        if self.report_windows_failed > 0:
            raise UserError(_("Cannot mark ready: %d report windows failed.", self.report_windows_failed))
        if self.report_windows_completed < self.report_window_count:
            raise UserError(_("Cannot mark ready: only %d/%d report windows completed.",
                              self.report_windows_completed, self.report_window_count))
        if not self._is_safety_delay_passed():
            raise UserError(_(
                "Safety delay has not passed. cutover_at + %(hours)d hours = %(deadline)s.",
                hours=self.safety_delay_hours,
                deadline=fields.Datetime.to_string(
                    self.cutover_at + timedelta(hours=self.safety_delay_hours)
                ),
            ))
        self.write({'state': 'ready'})

    def action_activate(self):
        """Activate this cutover run — sets fba_sale_stock_cutover_at on the instance."""
        self.ensure_one()
        self.env['amazon.instance']._check_amazon_manager_access()
        if self.state != 'ready':
            raise UserError(_("Only a ready cutover run can be activated."))
        preflight = self._preflight_check()
        if preflight['blocked']:
            raise UserError(_("Cutover activation blocked:\n%s", '\n'.join(preflight['reasons'])))
        self.instance_id.sudo().write({
            'fba_sale_stock_cutover_at': self.cutover_at,
        })
        self.write({'state': 'activated'})

    def action_cancel(self):
        self.ensure_one()
        self.env['amazon.instance']._check_amazon_manager_access()
        if self.state == 'activated':
            raise UserError(_("Cannot cancel an activated cutover run."))
        self.write({'state': 'cancelled'})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_report_windows(history_start, cutover_at):
        windows = []
        current = history_start
        while current < cutover_at:
            win_end = min(current + timedelta(days=REPORT_MAX_DAYS), cutover_at)
            windows.append((current, win_end))
            current = win_end
        return windows

    def _is_safety_delay_passed(self):
        deadline = self.cutover_at + timedelta(hours=self.safety_delay_hours)
        return fields.Datetime.now() >= deadline

    def _store_raw_evidence(self, rows, window_start, window_end):
        """Store raw shipment evidence rows, deduplicated by shipment_item_id."""
        ShipmentEvidence = self.env['amazon.fba.sale.stock.cutover.shipment'].sudo()
        for row in rows:
            shipment_item_id = row.get('shipment-item-id', '')
            if not shipment_item_id:
                continue
            existing = ShipmentEvidence.search([
                ('run_id', '=', self.id),
                ('shipment_item_id', '=', shipment_item_id),
            ], limit=1)
            if existing:
                continue

            shipment_date_raw = row.get('shipment-date', '')
            shipment_dt = self._parse_amazon_timestamp(shipment_date_raw)

            ShipmentEvidence.create({
                'run_id': self.id,
                'instance_id': self.instance_id.id,
                'amazon_order_ref': row.get('amazon-order-id', ''),
                'amazon_order_item_id': row.get('amazon-order-item-id', ''),
                'sku': row.get('sku', ''),
                'shipment_id': row.get('shipment-id', ''),
                'shipment_item_id': shipment_item_id,
                'shipment_date': shipment_dt,
                'quantity_shipped': int(float(row.get('quantity-shipped', 0) or 0)),
                'purchase_date_raw': row.get('purchase-date', ''),
                'report_window_start': window_start,
                'report_window_end': window_end,
            })

    @staticmethod
    def _parse_amazon_timestamp(ts_str):
        if not ts_str:
            return False
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            return False

    def _aggregate_baselines(self):
        """Build per-order-item baselines from raw shipment evidence."""
        self.ensure_one()
        if self.state not in ('draft', 'building'):
            raise UserError(_("Baselines cannot be rebuilt after the run leaves building state."))
        self.baseline_ids.unlink()

        cutover_dt = self.cutover_at
        evidence = self.shipment_evidence_ids

        included = 0
        excluded = 0
        aggregated = {}

        for ev in evidence:
            if not ev.shipment_date:
                excluded += 1
                continue
            if ev.shipment_date > cutover_dt:
                excluded += 1
                continue
            included += 1
            key = (ev.amazon_order_ref, ev.amazon_order_item_id)
            if key not in aggregated:
                aggregated[key] = {
                    'sku': ev.sku,
                    'qty': 0.0,
                    'rows': 0,
                }
            aggregated[key]['qty'] += ev.quantity_shipped
            aggregated[key]['rows'] += 1

        BaseLine = self.env['amazon.fba.sale.stock.cutover.baseline'].sudo()
        orders = set()
        skus = set()
        total_qty = 0.0

        for (order_ref, item_id), data in aggregated.items():
            BaseLine.create({
                'run_id': self.id,
                'instance_id': self.instance_id.id,
                'amazon_order_ref': order_ref,
                'amazon_order_item_id': item_id,
                'sku': data['sku'],
                'fulfilled_before_cutover': data['qty'],
                'cutover_at': cutover_dt,
                'shipment_report_rows': data['rows'],
            })
            orders.add(order_ref)
            skus.add(data['sku'])
            total_qty += data['qty']

        self.write({
            'included_row_count': included,
            'excluded_row_count': excluded,
            'baseline_count': len(aggregated),
            'total_fulfilled_before_cutover': total_qty,
            'unique_orders': len(orders),
            'unique_skus': len(skus),
        })

    def _preflight_check(self):
        """Verify all prerequisites for activation."""
        self.ensure_one()
        reasons = []
        instance = self.instance_id

        if self.state != 'ready':
            reasons.append("Cutover run is not in ready state (current: %s)." % self.state)

        if self.report_windows_failed > 0:
            reasons.append("%d report windows failed." % self.report_windows_failed)

        if self.report_windows_completed < self.report_window_count:
            reasons.append("Only %d/%d report windows completed." % (
                self.report_windows_completed, self.report_window_count))

        if not self._is_safety_delay_passed():
            reasons.append("Safety delay not passed (cutover_at + %dh)." % self.safety_delay_hours)

        if self.baseline_count < 0:
            reasons.append("Invalid baseline count.")

        neg = self.baseline_ids.filtered(
            lambda b: float_compare(b.fulfilled_before_cutover, 0.0, precision_digits=2) < 0
        )
        if neg:
            reasons.append("%d baseline records have negative quantities." % len(neg))

        if instance.fba_sale_stock_cutover_at:
            reasons.append("Instance already has a cutover set: %s." % (
                fields.Datetime.to_string(instance.fba_sale_stock_cutover_at)))

        if not instance.fba_sellable_location_id:
            reasons.append("FBA Sellable location not configured.")

        if not instance.fba_sold_customer_location_id:
            reasons.append("FBA Sold/Customer location not configured.")

        if not instance.fba_warehouse_id:
            reasons.append("FBA warehouse not configured.")

        return {'blocked': bool(reasons), 'reasons': reasons}

    def get_baseline_for_order_item(self, amazon_order_ref, amazon_order_item_id):
        """Look up the pre-cutover fulfilled quantity for one order item."""
        self.ensure_one()
        baseline = self.baseline_ids.filtered(
            lambda b: b.amazon_order_ref == amazon_order_ref
            and b.amazon_order_item_id == amazon_order_item_id
        )
        if baseline:
            return baseline[0].fulfilled_before_cutover
        return 0.0

    def is_order_within_coverage(self, purchase_date):
        """Return True when purchase_date falls within [history_start_at, cutover_at)."""
        self.ensure_one()
        if not purchase_date:
            return False
        return self.history_start_at <= purchase_date < self.cutover_at


class AmazonFbaSaleStockCutoverShipment(models.Model):
    """Raw shipment evidence row from GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL.

    Deduplicated by shipment_item_id within a run.  These rows are the
    audit trail for baseline aggregation.
    """

    _name = 'amazon.fba.sale.stock.cutover.shipment'
    _description = 'FBA Cutover Raw Shipment Evidence'
    _order = 'shipment_date, id'
    _rec_name = 'shipment_item_id'

    run_id = fields.Many2one(
        'amazon.fba.sale.stock.cutover.run', required=True,
        ondelete='cascade', index=True, readonly=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', required=True, ondelete='cascade', index=True,
        readonly=True,
    )
    amazon_order_ref = fields.Char(required=True, index=True, readonly=True)
    amazon_order_item_id = fields.Char(required=True, index=True, readonly=True)
    sku = fields.Char(required=True, readonly=True)
    shipment_id = fields.Char(required=True, readonly=True)
    shipment_item_id = fields.Char(required=True, index=True, readonly=True)
    shipment_date = fields.Datetime(readonly=True)
    quantity_shipped = fields.Integer(required=True, readonly=True)
    purchase_date_raw = fields.Char(readonly=True)
    report_window_start = fields.Datetime(readonly=True)
    report_window_end = fields.Datetime(readonly=True)

    _unique_shipment_item = models.Constraint(
        'UNIQUE (run_id, shipment_item_id)',
        'Each shipment-item-id must appear at most once per cutover run.',
    )
