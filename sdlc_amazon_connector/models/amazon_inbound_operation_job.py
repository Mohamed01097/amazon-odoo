import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class AmazonInboundOperationJob(models.Model):
    _name = 'amazon.inbound.operation.job'
    _description = 'Amazon Inbound Operation Job'
    _order = 'create_date desc, id desc'
    _check_company_auto = True

    inbound_shipment_id = fields.Many2one(
        'amazon.inbound.shipment', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    instance_id = fields.Many2one(
        'amazon.instance', related='inbound_shipment_id.instance_id',
        store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='inbound_shipment_id.company_id',
        store=True, readonly=True, index=True,
    )
    operation_type = fields.Selection([
        ('create_inbound_plan', 'Create Inbound Plan'),
        ('generate_packing_options', 'Generate Packing Options'),
        ('refresh_packing_options', 'Refresh Packing Options'),
        ('confirm_packing_option', 'Confirm Packing Option'),
        ('generate_placement_options', 'Generate Placement Options'),
        ('refresh_placement_options', 'Refresh Placement Options'),
        ('confirm_placement_option', 'Confirm Placement Option'),
    ], required=True, default='create_inbound_plan', index=True)
    operation_id = fields.Char(
        index=True, copy=False,
        help="Amazon operationId for asynchronous POST operations. Refresh jobs use no Amazon operation ID.",
    )
    packing_option_id = fields.Many2one(
        'amazon.fba.packing.option', ondelete='set null', check_company=True,
    )
    placement_option_id = fields.Many2one(
        'amazon.fba.placement.option', ondelete='set null', check_company=True,
    )
    state = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], required=True, default='pending', index=True)
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=12)
    next_run_at = fields.Datetime(index=True)
    last_error = fields.Text(groups='sdlc_amazon_connector.group_amazon_manager')
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    response_data = fields.Text(groups='sdlc_amazon_connector.group_amazon_manager')
    raw_operation_status = fields.Char(copy=False)
    amazon_request_id = fields.Char(
        copy=False, groups='sdlc_amazon_connector.group_amazon_manager',
    )

    _unique_operation = models.Constraint(
        'UNIQUE (operation_type, operation_id)',
        'An Amazon inbound operation can have only one persistent polling job.',
    )
    _valid_retry_limits = models.Constraint(
        'CHECK (retry_count >= 0 AND max_retries >= 1 AND max_retries <= 100)',
        'Inbound operation retry limits are invalid.',
    )

    @api.model
    def cron_process_inbound_operation_jobs(self):
        """Poll one durable operation with a row lock to avoid concurrent workers."""
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM amazon_inbound_operation_job
             WHERE state IN ('pending', 'in_progress')
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, create_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        """, [now])
        row = self.env.cr.fetchone()
        if not row:
            return False
        self.browse(row[0]).with_context(
            amazon_source_model=self._name,
            amazon_source_id=row[0],
            amazon_operation='inbound_operation',
        )._process_operation()
        return True

    def _next_delay_minutes(self, retry_count):
        return min(2 ** max(retry_count - 1, 0), 15)

    def _schedule_retry(self, error_message=False):
        self.ensure_one()
        retry_count = self.retry_count + 1
        vals = {
            'retry_count': retry_count,
        }
        if retry_count >= self.max_retries:
            vals.update(
                state='failed',
                finished_at=fields.Datetime.now(),
                next_run_at=False,
                last_error=error_message or _(
                    "Automatic polling reached its retry limit while Amazon still reported a non-final status."
                ),
            )
        else:
            vals.update(
                state='in_progress',
                next_run_at=fields.Datetime.now() + timedelta(
                    minutes=self._next_delay_minutes(retry_count)
                ),
                last_error=error_message or False,
            )
        self.write(vals)

    def _mark_done(self):
        self.ensure_one()
        self.write({
            'state': 'done',
            'finished_at': fields.Datetime.now(),
            'next_run_at': False,
            'last_error': False,
        })

    def _sync_from_shipment_status(self):
        self.ensure_one()
        shipment = self.inbound_shipment_id
        vals = {'response_data': shipment.create_operation_response or False}
        if shipment.create_operation_status == 'success':
            vals.update(state='done', finished_at=fields.Datetime.now(), next_run_at=False, last_error=False)
        elif shipment.create_operation_status == 'failed':
            vals.update(
                state='failed', finished_at=fields.Datetime.now(), next_run_at=False,
                last_error=shipment.create_operation_error_message or _("Amazon operation failed."),
            )
        elif self.state not in ('pending', 'in_progress'):
            vals.update(state='in_progress', finished_at=False, next_run_at=fields.Datetime.now())
        self.write(vals)

    def _process_operation(self):
        self.ensure_one()
        if self.state in ('done', 'failed'):
            return False
        now = fields.Datetime.now()
        vals = {'state': 'in_progress', 'next_run_at': False}
        if not self.started_at:
            vals['started_at'] = now
        self.write(vals)

        shipment = self.inbound_shipment_id.sudo()
        try:
            if self.operation_type == 'create_inbound_plan':
                shipment._poll_create_operation_status()
            elif self.operation_type == 'refresh_packing_options':
                shipment._refresh_packing_options()
                self._mark_done()
                return True
            elif self.operation_type == 'refresh_placement_options':
                shipment._refresh_placement_options()
                self._mark_done()
                return True
            else:
                status = shipment._poll_phase3_operation(self)
                if status == 'success':
                    self._mark_done()
                    return True
                if status == 'failed':
                    config = shipment._phase3_operation_config(self.operation_type)
                    self.write({
                        'state': 'failed',
                        'finished_at': fields.Datetime.now(),
                        'next_run_at': False,
                        'last_error': shipment[config['error_message_field']] or _("Amazon operation failed."),
                    })
                    return False
                self._schedule_retry()
                return False
        except Exception as exc:
            message = str(exc)
            _logger.warning(
                "Amazon inbound operation poll failed for job %s operation %s: %s",
                self.id, self.operation_id, message,
            )
            if self.operation_type == 'create_inbound_plan':
                shipment.write({
                    'last_operation_check_at': fields.Datetime.now(),
                    'create_operation_error_code': 'POLL_REQUEST_FAILED',
                    'create_operation_error_message': message,
                })
            else:
                shipment._record_phase3_job_error(self, message)
            self._schedule_retry(error_message=message)
            return False

        if self.operation_type == 'create_inbound_plan' and shipment.create_operation_status in ('success', 'failed'):
            self._sync_from_shipment_status()
            return shipment.create_operation_status == 'success'
        self._schedule_retry()
        return False
