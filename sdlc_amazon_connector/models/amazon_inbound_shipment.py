import json
import logging
import re
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .amazon_api import AmazonAPI

_logger = logging.getLogger(__name__)

AMAZON_OWNER_SELECTION = [
    ('AMAZON', 'Amazon'),
    ('SELLER', 'Seller'),
    ('NONE', 'None'),
]
INBOUND_PLAN_ID_RE = re.compile(r'^[A-Za-z0-9-]{38}$')
OPERATION_ID_RE = re.compile(r'^[A-Za-z0-9-]{36,38}$')
WORKFLOW_STATE_RANK = {
    'draft': 0,
    'planning': 1,
    'failed': 1,
    'plan_created': 2,
    'packing_generated': 3,
    'packing_confirmed': 4,
    'placement_generated': 5,
    'placement_confirmed': 6,
    'submitted': 7,
    'shipped': 8,
    'in_transit': 9,
    'receiving': 10,
    'closed': 11,
    'cancelled': 99,
}


class AmazonInboundShipment(models.Model):
    _name = 'amazon.inbound.shipment'
    _description = 'Amazon Inbound Shipment'
    _order = 'create_date desc'
    _check_company_auto = True

    name = fields.Char('Name', required=True, default='New')
    instance_id = fields.Many2one(
        'amazon.instance', string='Instance', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='instance_id.company_id', store=True, readonly=True, index=True,
    )
    shipment_id = fields.Char(
        'Legacy Amazon Shipment ID', index=True,
        help="Legacy connector field retained for compatibility. It is not the inbound plan ID authority.",
    )
    inbound_plan_id = fields.Char(
        'Inbound Plan ID', index=True, copy=False,
        help="Amazon Fulfillment Inbound plan identifier returned by createInboundPlan.",
    )
    create_operation_id = fields.Char(
        'Create Operation ID', index=True, copy=False,
        help="Amazon asynchronous operation identifier returned by createInboundPlan.",
    )
    create_operation_reported_id = fields.Char(
        'Returned Create Operation ID', copy=False,
        help=(
            "Last operationId returned by getInboundOperationStatus. Kept for audit "
            "when Amazon diagnostic metadata differs from the stored operation ID."
        ),
    )
    create_operation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string='Create Operation Status', copy=False, index=True)
    raw_create_operation_status = fields.Char(
        'Raw Amazon Operation Status', copy=False,
        help="Unmodified operationStatus returned by Amazon.",
    )
    create_operation_error_code = fields.Char(
        'Create Operation Error Code', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    create_operation_error_message = fields.Text(
        'Create Operation Error', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    create_operation_response = fields.Text(
        'Sanitized Operation Response', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
        help="Sanitized createInboundPlan and operation-status responses. Credentials are never stored here.",
    )
    create_operation_request_id = fields.Char(
        'Create Amazon Request ID', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    create_retry_after_at = fields.Datetime(
        'Create Retry After', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
        help="Earliest safe manual retry time after Amazon returns HTTP 429.",
    )
    last_operation_request_id = fields.Char(
        'Last Operation Amazon Request ID', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    plan_created_at = fields.Datetime('Plan Created At', copy=False)
    last_operation_check_at = fields.Datetime('Last Operation Check', copy=False)
    raw_plan_status = fields.Char('Raw Amazon Plan Status', copy=False)
    packing_generation_operation_id = fields.Char(copy=False, index=True)
    packing_generation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, index=True)
    packing_confirmation_operation_id = fields.Char(copy=False, index=True)
    packing_confirmation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, index=True)
    packing_information_operation_id = fields.Char(copy=False, index=True)
    packing_information_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, index=True)
    packing_last_refresh_at = fields.Datetime(copy=False)
    packing_error_code = fields.Char(
        copy=False, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    packing_error_message = fields.Text(
        copy=False, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    packing_response = fields.Text(
        string='Sanitized Packing Response', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    placement_generation_operation_id = fields.Char(copy=False, index=True)
    placement_generation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, index=True)
    placement_confirmation_operation_id = fields.Char(copy=False, index=True)
    placement_confirmation_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], copy=False, index=True)
    placement_last_refresh_at = fields.Datetime(copy=False)
    placement_error_code = fields.Char(
        copy=False, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    placement_error_message = fields.Text(
        copy=False, groups='sdlc_amazon_connector.group_amazon_manager',
    )
    placement_response = fields.Text(
        string='Sanitized Placement Response', copy=False,
        groups='sdlc_amazon_connector.group_amazon_manager',
    )
    shipment_name = fields.Char('Shipment Name')
    destination_fulfillment_center = fields.Char('Destination FC')
    label_prep_type = fields.Selection([
        ('NO_LABEL', 'No Label'),
        ('SELLER_LABEL', 'Seller Label'),
        ('AMAZON_LABEL', 'Amazon Label'),
    ], string='Label Prep', default='SELLER_LABEL')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('planning', 'Planning'),
        ('plan_created', 'Plan Created'),
        ('packing_generated', 'Packing Generated'),
        ('packing_confirmed', 'Packing Confirmed'),
        ('placement_generated', 'Placement Generated'),
        ('placement_confirmed', 'Placement Confirmed'),
        ('failed', 'Failed'),
        # Retained for later workflow phases and compatibility with legacy records.
        ('submitted', 'Submitted'),
        ('shipped', 'Shipped'),
        ('in_transit', 'In Transit'),
        ('receiving', 'Receiving'),
        ('closed', 'Closed'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, index=True)

    # Carrier fields are retained for later inbound phases.
    carrier_type = fields.Selection([
        ('partnered', 'Amazon Partnered'),
        ('non_partnered', 'Non-Partnered'),
    ], string='Carrier Type', default='non_partnered')
    carrier_name = fields.Char('Carrier Name')
    tracking_id = fields.Char('Tracking ID')
    pro_number = fields.Char('PRO Number')
    ship_date = fields.Date('Ship Date')
    estimated_arrival = fields.Date('Estimated Arrival')

    line_ids = fields.One2many('amazon.inbound.shipment.line', 'shipment_id', string='Items')
    line_count = fields.Integer(compute='_compute_line_count')
    operation_job_ids = fields.One2many(
        'amazon.inbound.operation.job', 'inbound_shipment_id', string='Inbound Operation Jobs',
    )
    packing_option_ids = fields.One2many(
        'amazon.fba.packing.option', 'inbound_shipment_id', string='Packing Options',
    )
    placement_option_ids = fields.One2many(
        'amazon.fba.placement.option', 'inbound_shipment_id', string='Placement Options',
    )
    physical_shipment_ids = fields.One2many(
        'amazon.fba.physical.shipment', 'inbound_shipment_id',
        string='Amazon Physical Shipments',
    )
    packing_options_expired = fields.Boolean(compute='_compute_options_expired')
    placement_options_expired = fields.Boolean(compute='_compute_options_expired')

    # Retained for later stock-movement phases. Phase 2 never creates or writes it.
    picking_id = fields.Many2one(
        'stock.picking', string='Delivery Order', check_company=True,
    )

    _unique_shipment = models.Constraint(
        'UNIQUE (shipment_id, instance_id)',
        'Shipment ID must be unique per instance.',
    )
    _unique_inbound_plan = models.Constraint(
        'UNIQUE (instance_id, inbound_plan_id)',
        'Inbound Plan ID must be unique per Amazon instance.',
    )

    def _auto_init(self):
        """One-time-safe backfill from the legacy field during module upgrades."""
        result = super()._auto_init()
        self.env.cr.execute("""
            UPDATE amazon_inbound_shipment
               SET inbound_plan_id = shipment_id
             WHERE inbound_plan_id IS NULL
               AND shipment_id ~ '^[A-Za-z0-9-]{38}$'
        """)
        if self.env.cr.rowcount:
            _logger.info(
                "Backfilled inbound_plan_id on %s legacy inbound shipment(s).",
                self.env.cr.rowcount,
            )
        return result

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends(
        'packing_option_ids.status', 'packing_option_ids.expiration_date',
        'placement_option_ids.status', 'placement_option_ids.expiration_date',
    )
    def _compute_options_expired(self):
        now = fields.Datetime.now()

        def all_expired(options):
            return bool(options) and all(
                option.status == 'EXPIRED'
                or (option.expiration_date and option.expiration_date <= now)
                for option in options
            )

        for shipment in self:
            shipment.packing_options_expired = all_expired(shipment.packing_option_ids)
            shipment.placement_options_expired = all_expired(shipment.placement_option_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('amazon.inbound.shipment') or 'New'
        return super().create(vals_list)

    def _check_inbound_manager_access(self):
        if self.env.su or self.env.user.has_group('sdlc_amazon_connector.group_amazon_manager'):
            return
        raise AccessError(_("Only an Amazon Connector Manager can manage inbound plan operations."))

    @staticmethod
    def _sanitized_json(value):
        return json.dumps(
            AmazonAPI._sanitize_for_log(value), default=str, ensure_ascii=False, indent=2,
        )

    def _merge_operation_response(self, key, value):
        self.ensure_one()
        history = {}
        if self.create_operation_response:
            try:
                history = json.loads(self.create_operation_response)
            except (TypeError, ValueError):
                history = {'legacyResponse': self.create_operation_response}
        history[key] = AmazonAPI._sanitize_for_log(value)
        return self._sanitized_json(history)

    def _prepare_source_address(self):
        self.ensure_one()
        partner = self.instance_id.fba_ship_from_partner_id
        if not partner:
            raise UserError(_("Select the FBA Ship-From Address on the Amazon instance."))
        if not partner.active:
            raise UserError(_("The selected FBA Ship-From Address is archived."))
        if partner.company_id and partner.company_id != self.company_id:
            raise UserError(_("The FBA Ship-From Address belongs to another company."))

        required = [
            ('name', _("Name")),
            ('street', _("Street")),
            ('city', _("City")),
            ('zip', _("ZIP/Postal Code")),
            ('country_id', _("Country")),
            ('phone', _("Phone")),
        ]
        missing = []
        for field_name, label in required:
            value = partner[field_name]
            if field_name == 'country_id':
                is_missing = not value
            else:
                is_missing = not str(value or '').strip()
            if is_missing:
                missing.append(label)
        country_code = (partner.country_id.code or '').strip().upper()
        if partner.country_id and not country_code:
            missing.append(_("Country ISO Code"))
        if missing:
            raise UserError(_(
                "The FBA Ship-From Address is missing: %s.",
                ", ".join(missing),
            ))
        if not re.fullmatch(r'[A-Z]{2}', country_code):
            raise UserError(_("The FBA Ship-From country must have a two-letter ISO country code."))

        address = {
            'name': partner.name.strip(),
            'addressLine1': partner.street.strip(),
            'city': partner.city.strip(),
            'postalCode': partner.zip.strip(),
            'countryCode': country_code,
            'phoneNumber': partner.phone.strip(),
        }
        if partner.street2:
            address['addressLine2'] = partner.street2.strip()
        if partner.commercial_company_name:
            address['companyName'] = partner.commercial_company_name.strip()
        if partner.state_id:
            state_code = (partner.state_id.code or partner.state_id.name or '').strip()
            if state_code:
                address['stateOrProvinceCode'] = state_code
        if partner.email:
            address['email'] = partner.email.strip()

        length_limits = {
            'name': 50,
            'companyName': 50,
            'addressLine1': 180,
            'addressLine2': 60,
            'city': 30,
            'stateOrProvinceCode': 64,
            'postalCode': 32,
            'phoneNumber': 20,
            'email': 1024,
        }
        too_long = [
            "%s (max %s)" % (key, limit)
            for key, limit in length_limits.items()
            if address.get(key) and len(address[key]) > limit
        ]
        if too_long:
            raise UserError(_(
                "The FBA Ship-From Address exceeds Amazon's limits: %s.",
                ", ".join(too_long),
            ))
        return address

    def _prepare_create_inbound_plan_payload(self):
        """Build and validate the v2024-03-20 createInboundPlan body."""
        self.ensure_one()
        instance = self.instance_id
        marketplace_id = (instance.marketplace_id or '').strip()
        if not marketplace_id:
            raise UserError(_("Configure the Amazon Marketplace ID before creating an inbound plan."))
        if len(marketplace_id) > 20:
            raise UserError(_("The configured Amazon Marketplace ID is invalid."))
        if not self.line_ids:
            raise UserError(_("Add items before creating an inbound plan."))
        if len(self.line_ids) > 2000:
            raise UserError(_("Amazon allows at most 2000 items in one inbound plan request."))

        plan_name = (self.shipment_name or self.name or '').strip()
        if not plan_name:
            raise UserError(_("Enter a name for the inbound plan."))
        if len(plan_name) > 40:
            raise UserError(_("The inbound plan name cannot exceed 40 characters."))

        items = []
        validation_errors = []
        for position, line in enumerate(self.line_ids, start=1):
            line_errors = []
            product = line.amazon_product_id
            if not product:
                line_errors.append(_("Line %s: select a mapped Amazon Product.", position))
                validation_errors.extend(line_errors)
                continue
            if product.instance_id != instance:
                line_errors.append(_("Line %s: the Amazon Product belongs to another instance.", position))
                validation_errors.extend(line_errors)
                continue
            if not product.odoo_product_id:
                line_errors.append(_("Line %s: the Amazon Product is not mapped to an Odoo product.", position))
            msku = (product.sku or '').strip()
            if not msku:
                line_errors.append(_("Line %s: the Amazon Product has no Seller SKU/MSKU.", position))
            elif len(msku) > 255:
                line_errors.append(_("Line %s: MSKU cannot exceed 255 characters.", position))
            if line.sku and line.sku.strip() != msku:
                line_errors.append(_("Line %s: the line SKU does not match the mapped Amazon Product MSKU.", position))

            quantity = line.planned_quantity
            if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 500000:
                line_errors.append(_("Line %s: planned quantity must be an integer from 1 to 500000.", position))
            if line.prep_owner not in dict(AMAZON_OWNER_SELECTION):
                line_errors.append(_("Line %s: select Prep Owner explicitly.", position))
            elif marketplace_id == 'ATVPDKIKX0DER' and line.prep_owner == 'AMAZON':
                line_errors.append(_(
                    "Line %s: Amazon is not an accepted Prep Owner in the US marketplace.",
                    position,
                ))
            if line.label_owner not in dict(AMAZON_OWNER_SELECTION):
                line_errors.append(_("Line %s: select Label Owner explicitly.", position))
            elif marketplace_id == 'ATVPDKIKX0DER' and line.label_owner == 'AMAZON':
                line_errors.append(_(
                    "Line %s: Amazon is not an accepted Label Owner in the US marketplace.",
                    position,
                ))

            validation_errors.extend(line_errors)
            if not line_errors:
                item = {
                    'msku': msku,
                    'quantity': quantity,
                    'prepOwner': line.prep_owner,
                    'labelOwner': line.label_owner,
                }
                if line.expiration_date:
                    # Amazon Fulfillment Inbound v2024-03-20 ItemInput expects
                    # physical SKU expiration in `expiration` as YYYY-MM-DD.
                    item['expiration'] = fields.Date.to_string(line.expiration_date)
                items.append(item)

        if validation_errors:
            raise UserError(_("Inbound plan item validation failed:\n%s", "\n".join(validation_errors)))

        return {
            'name': plan_name,
            'destinationMarketplaces': [marketplace_id],
            'sourceAddress': self._prepare_source_address(),
            'items': items,
        }

    def _ensure_create_operation_job(self):
        self.ensure_one()
        if not self.create_operation_id:
            return self.env['amazon.inbound.operation.job']
        Job = self.env['amazon.inbound.operation.job'].sudo()
        job = Job.search([
            ('operation_type', '=', 'create_inbound_plan'),
            ('operation_id', '=', self.create_operation_id),
        ], limit=1)
        if job:
            repair_vals = {}
            if job.inbound_shipment_id != self:
                raise ValidationError(_("This Amazon operation is already linked to another inbound shipment."))
            if job.state == 'failed' and self.create_operation_status in ('pending', 'in_progress'):
                repair_vals.update(state='pending', next_run_at=fields.Datetime.now(), finished_at=False)
            if repair_vals:
                job.write(repair_vals)
            return job
        return Job.create({
            'inbound_shipment_id': self.id,
            'operation_type': 'create_inbound_plan',
            'operation_id': self.create_operation_id,
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        })

    def _apply_create_inbound_plan_response(self, result):
        """Persist both asynchronous identifiers without touching legacy shipment_id."""
        self.ensure_one()
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        plan_id = str(result.get('inboundPlanId') or '').strip()
        operation_id = str(result.get('operationId') or '').strip()
        request_id = str(result.get('_amazon_request_id') or '').strip()
        response_text = self._merge_operation_response('createInboundPlan', result)

        problems = []
        if not INBOUND_PLAN_ID_RE.fullmatch(plan_id):
            problems.append(_("Amazon did not return a valid inboundPlanId."))
        if not OPERATION_ID_RE.fullmatch(operation_id):
            problems.append(_("Amazon did not return a valid operationId."))
        if self.inbound_plan_id and plan_id and self.inbound_plan_id != plan_id:
            problems.append(_("Amazon returned an inboundPlanId different from the one already stored."))

        vals = {
            'create_operation_response': response_text,
            'create_operation_request_id': request_id or False,
            'create_retry_after_at': False,
            'create_operation_error_code': False,
            'create_operation_error_message': False,
        }
        if plan_id and (not self.inbound_plan_id or self.inbound_plan_id == plan_id):
            vals['inbound_plan_id'] = plan_id
        if operation_id:
            vals['create_operation_id'] = operation_id

        if problems:
            vals.update(
                create_operation_status='failed',
                create_operation_error_code='INVALID_CREATE_RESPONSE',
                create_operation_error_message='\n'.join(problems),
                state='failed',
            )
            self.sudo().write(vals)
            _logger.error(
                "Invalid createInboundPlan response for inbound shipment %s: %s",
                self.id, response_text,
            )
            return False

        vals.update(create_operation_status='pending', state='planning')
        self.sudo().write(vals)
        try:
            with self.env.cr.savepoint():
                self._ensure_create_operation_job()
        except Exception as exc:
            # Keep the external identifiers durable even if job creation needs repair.
            _logger.exception(
                "Could not create the polling job for Amazon inbound operation %s",
                operation_id,
            )
            self.sudo().write({
                'create_operation_error_code': 'POLL_JOB_CREATE_FAILED',
                'create_operation_error_message': str(exc),
            })
        return True

    def _operation_problem_values(self, problems):
        errors = [
            problem for problem in (problems or [])
            if isinstance(problem, dict) and (problem.get('severity') or '').upper() == 'ERROR'
        ]
        relevant = errors or [problem for problem in (problems or []) if isinstance(problem, dict)]
        if not relevant:
            return False, False
        first = relevant[0]
        messages = []
        for problem in relevant:
            message = str(problem.get('message') or '').strip()
            details = problem.get('details')
            if details:
                details = details if isinstance(details, str) else json.dumps(details, default=str)
                message = "%s (%s)" % (message, details) if message else str(details)
            if message:
                messages.append(message)
        return str(first.get('code') or '').strip() or False, '\n'.join(messages) or False

    def _state_rank(self, state=False):
        return WORKFLOW_STATE_RANK.get(state or self.state, -1)

    def _monotonic_state(self, proposed_state):
        self.ensure_one()
        if self._state_rank(self.state) > self._state_rank(proposed_state):
            return self.state
        return proposed_state

    def _create_plan_has_authoritatively_advanced(self):
        self.ensure_one()
        later_phase_started = any((
            self.packing_generation_operation_id,
            self.packing_generation_status,
            self.packing_confirmation_operation_id,
            self.packing_confirmation_status,
            self.packing_information_operation_id,
            self.packing_information_status,
            self.placement_generation_operation_id,
            self.placement_generation_status,
            self.placement_confirmation_operation_id,
            self.placement_confirmation_status,
        ))
        return (
            bool(self.inbound_plan_id)
            and bool(self.plan_created_at)
            and (
                self._state_rank(self.state) >= self._state_rank('plan_created')
                or self.create_operation_status == 'success'
                or later_phase_started
            )
        )

    def _planned_expirations_by_msku(self):
        self.ensure_one()
        expirations = {}
        for line in self.line_ids:
            msku = (line.sku or '').strip()
            if msku and line.expiration_date:
                expirations.setdefault(msku, set()).add(fields.Date.to_string(line.expiration_date))
        return expirations

    def _inbound_plan_identity_verification(self, plan_result, plan_items):
        """Verify a mismatched SUCCESS against read-only Amazon plan evidence.

        Amazon's Fulfillment Inbound v2024-03-20 schema exposes plan metadata
        through getInboundPlan and physical item inputs through
        listInboundPlanItems. A mismatched operationId is never accepted from the
        status payload alone; the stored inboundPlanId and item quantities must
        independently match the local shipment.
        """
        self.ensure_one()
        evidence = {
            'verified': False,
            'checks': [],
            'expected': {},
            'amazon': {},
        }
        failures = []

        if not isinstance(plan_result, dict):
            failures.append(_("Amazon returned an invalid inbound plan response."))
            evidence['failures'] = [str(item) for item in failures]
            return evidence
        if not isinstance(plan_items, list):
            failures.append(_("Amazon returned invalid inbound plan items."))
            evidence['failures'] = [str(item) for item in failures]
            return evidence

        returned_plan_id = str(plan_result.get('inboundPlanId') or '').strip()
        expected_plan_id = (self.inbound_plan_id or '').strip()
        evidence['expected']['inboundPlanId'] = expected_plan_id
        evidence['amazon']['inboundPlanId'] = returned_plan_id or False
        if returned_plan_id != expected_plan_id:
            failures.append(_(
                "Amazon returned inboundPlanId %(returned)s while this shipment stores %(expected)s.",
                returned=returned_plan_id or _("(missing)"),
                expected=expected_plan_id,
            ))
        else:
            evidence['checks'].append('inboundPlanId')

        status = str(plan_result.get('status') or '').strip().upper()
        evidence['amazon']['status'] = status or False
        if status != 'ACTIVE':
            failures.append(_(
                "Amazon inbound plan %(plan)s is %(status)s, not ACTIVE.",
                plan=expected_plan_id,
                status=status or _("(missing)"),
            ))
        else:
            evidence['checks'].append('status')

        marketplace_id = (self.instance_id.marketplace_id or '').strip()
        marketplace_ids = [
            str(value or '').strip()
            for value in (plan_result.get('marketplaceIds') or [])
            if str(value or '').strip()
        ]
        evidence['expected']['marketplaceIds'] = [marketplace_id] if marketplace_id else []
        evidence['amazon']['marketplaceIds'] = marketplace_ids
        if marketplace_id and marketplace_id not in marketplace_ids:
            failures.append(_(
                "Amazon inbound plan marketplaceIds %(returned)s do not include the expected marketplace %(expected)s.",
                returned=marketplace_ids,
                expected=marketplace_id,
            ))
        elif marketplace_id:
            evidence['checks'].append('marketplaceIds')

        try:
            expected_address = self._prepare_source_address()
        except UserError:
            expected_address = {}
        returned_address = plan_result.get('sourceAddress') if isinstance(
            plan_result.get('sourceAddress'), dict
        ) else {}
        address_keys = ('countryCode', 'postalCode')
        address_expected = {
            key: str(expected_address.get(key) or '').strip().upper()
            for key in address_keys
            if expected_address.get(key)
        }
        address_returned = {
            key: str(returned_address.get(key) or '').strip().upper()
            for key in address_keys
            if returned_address.get(key)
        }
        evidence['expected']['sourceAddress'] = address_expected
        evidence['amazon']['sourceAddress'] = address_returned
        for key, expected in address_expected.items():
            returned = address_returned.get(key)
            if returned and returned != expected:
                failures.append(_(
                    "Amazon inbound plan sourceAddress %(key)s is %(returned)s; expected %(expected)s.",
                    key=key,
                    returned=returned,
                    expected=expected,
                ))
        if address_expected:
            evidence['checks'].append('sourceAddress')

        expected_quantities = self._planned_msku_quantities()
        returned_quantities = {}
        returned_expirations = {}
        returned_label_owners = {}
        item_count = 0
        for item in plan_items:
            if not isinstance(item, dict):
                failures.append(_("Amazon returned an invalid item in listInboundPlanItems."))
                continue
            item_count += 1
            msku = str(item.get('msku') or '').strip()
            quantity = item.get('quantity')
            if not msku:
                failures.append(_("Amazon returned an inbound plan item without an MSKU."))
                continue
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                failures.append(_("Amazon returned an invalid quantity for MSKU %s.", msku))
                continue
            returned_quantities[msku] = returned_quantities.get(msku, 0) + quantity
            expiration = str(item.get('expiration') or '').strip()
            if expiration:
                returned_expirations.setdefault(msku, set()).add(expiration)
            label_owner = str(item.get('labelOwner') or '').strip()
            if label_owner:
                returned_label_owners.setdefault(msku, set()).add(label_owner)

        evidence['expected']['itemQuantities'] = expected_quantities
        evidence['amazon']['itemQuantities'] = returned_quantities
        evidence['amazon']['itemCount'] = item_count
        if returned_quantities != expected_quantities:
            failures.append(_(
                "Amazon inbound plan item quantities do not match this shipment. Expected: %(expected)s; Amazon: %(returned)s.",
                expected=expected_quantities,
                returned=returned_quantities,
            ))
        else:
            evidence['checks'].append('itemQuantities')

        expected_expirations = self._planned_expirations_by_msku()
        evidence['expected']['expirations'] = {
            msku: sorted(values) for msku, values in expected_expirations.items()
        }
        evidence['amazon']['expirations'] = {
            msku: sorted(values) for msku, values in returned_expirations.items()
        }
        for msku, values in returned_expirations.items():
            expected_values = expected_expirations.get(msku)
            if expected_values and not values.issubset(expected_values):
                failures.append(_(
                    "Amazon returned expiration %(returned)s for MSKU %(msku)s; expected %(expected)s.",
                    returned=sorted(values),
                    msku=msku,
                    expected=sorted(expected_values),
                ))
        if expected_expirations or returned_expirations:
            evidence['checks'].append('expiration')

        expected_label_owners = {}
        for line in self.line_ids:
            msku = (line.sku or '').strip()
            if msku and line.label_owner:
                expected_label_owners.setdefault(msku, set()).add(line.label_owner)
        evidence['expected']['labelOwners'] = {
            msku: sorted(values) for msku, values in expected_label_owners.items()
        }
        evidence['amazon']['labelOwners'] = {
            msku: sorted(values) for msku, values in returned_label_owners.items()
        }
        for msku, values in returned_label_owners.items():
            expected_values = expected_label_owners.get(msku)
            if expected_values and values != expected_values:
                failures.append(_(
                    "Amazon returned labelOwner %(returned)s for MSKU %(msku)s; expected %(expected)s.",
                    returned=sorted(values),
                    msku=msku,
                    expected=sorted(expected_values),
                ))
        if returned_label_owners:
            evidence['checks'].append('labelOwner')

        evidence['verified'] = not failures
        evidence['failures'] = [str(item) for item in failures]
        return evidence

    def _apply_create_operation_status(self, result, plan_result=None, plan_verification=None):
        self.ensure_one()
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        raw_status = str(result.get('operationStatus') or '').strip()
        normalized = raw_status.upper().replace('-', '_').replace(' ', '_')
        response_operation_id = str(result.get('operationId') or '').strip()
        operation_id_mismatch = bool(
            self.create_operation_id and response_operation_id != self.create_operation_id
        )
        create_plan_already_advanced = self._create_plan_has_authoritatively_advanced()
        previous_reported_id = str(self.create_operation_reported_id or '').strip()
        reported_id_to_store = response_operation_id or False
        reported_ids = []
        for operation_id in (previous_reported_id, response_operation_id):
            if operation_id and operation_id not in reported_ids:
                reported_ids.append(operation_id)
        if operation_id_mismatch and previous_reported_id and previous_reported_id != response_operation_id:
            reported_id_to_store = previous_reported_id
        request_id = str(result.get('_amazon_request_id') or '').strip()
        error_code, error_message = self._operation_problem_values(result.get('operationProblems'))
        response_value = result if plan_result is None else {
            'operation': result,
            'inboundPlan': plan_result,
        }
        if plan_verification is not None:
            if not isinstance(response_value, dict) or 'operation' not in response_value:
                response_value = {'operation': response_value}
            response_value['planVerification'] = plan_verification
        if operation_id_mismatch:
            response_value = {
                'operation': response_value,
                'operationIdAudit': {
                    'storedOperationId': self.create_operation_id,
                    'returnedOperationId': response_operation_id or False,
                    'preservedReportedOperationId': reported_id_to_store or False,
                    'allReturnedOperationIds': reported_ids,
                    'mismatch': True,
                    'reconciledByGetInboundPlan': bool(
                        plan_verification and plan_verification.get('verified')
                    ),
                },
            }
        if create_plan_already_advanced:
            if not isinstance(response_value, dict):
                response_value = {'operation': response_value}
            response_value['staleCreateOperationGuard'] = {
                'currentState': self.state,
                'createOperationStatus': self.create_operation_status,
                'preserveAdvancedWorkflowState': True,
            }
        vals = {
            'last_operation_check_at': fields.Datetime.now(),
            'last_operation_request_id': request_id or False,
            'raw_create_operation_status': raw_status or False,
            'create_operation_reported_id': reported_id_to_store,
            'create_operation_response': self._merge_operation_response(
                'getInboundOperationStatus', response_value,
            ),
        }
        if plan_result and isinstance(plan_result, dict):
            vals['raw_plan_status'] = str(plan_result.get('status') or '').strip() or False

        success_values = {'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'COMPLETE'}
        failed_values = {'FAILED', 'FAILURE', 'ERROR'}
        pending_values = {'PENDING', 'QUEUED', 'NOT_STARTED'}
        in_progress_values = {'IN_PROGRESS', 'PROCESSING', 'RUNNING'}
        if normalized in success_values:
            if operation_id_mismatch and not (
                plan_verification and plan_verification.get('verified')
            ):
                if create_plan_already_advanced:
                    vals.update(
                        create_operation_status=self.create_operation_status or 'success',
                        create_operation_error_code=False,
                        create_operation_error_message=False,
                    )
                else:
                    verification_failures = []
                    if isinstance(plan_verification, dict):
                        verification_failures = plan_verification.get('failures') or []
                        if plan_verification.get('error'):
                            verification_failures.append(plan_verification.get('error'))
                    verification_text = "\n".join(str(item) for item in verification_failures if item)
                    vals.update(
                        create_operation_status='failed',
                        create_operation_error_code='OPERATION_ID_MISMATCH_UNVERIFIED',
                        create_operation_error_message=_(
                            "Amazon returned SUCCESS for operationId %(returned)s while the stored "
                            "createInboundPlan operationId is %(stored)s. The success was not accepted "
                            "because the stored inbound plan could not be independently verified.%(details)s",
                            returned=response_operation_id or _("(missing)"),
                            stored=self.create_operation_id,
                            details=("\n" + verification_text) if verification_text else "",
                        ),
                        state='failed',
                    )
            else:
                vals.update(
                    create_operation_status='success',
                    create_operation_error_code=error_code,
                    create_operation_error_message=error_message,
                    plan_created_at=self.plan_created_at or fields.Datetime.now(),
                    state=self._monotonic_state('plan_created'),
                )
        elif normalized in failed_values:
            if create_plan_already_advanced:
                vals.update(
                    create_operation_status='success',
                    create_operation_error_code=False,
                    create_operation_error_message=False,
                )
            else:
                vals.update(
                    create_operation_status='failed',
                    create_operation_error_code=error_code or 'AMAZON_OPERATION_FAILED',
                    create_operation_error_message=error_message or _("Amazon reported that inbound plan creation failed."),
                    state='failed',
                )
        elif normalized in pending_values:
            if create_plan_already_advanced:
                vals.update(
                    create_operation_status=self.create_operation_status or 'success',
                    create_operation_error_code=False,
                    create_operation_error_message=False,
                )
            else:
                vals.update(create_operation_status='pending', state='planning')
        elif normalized in in_progress_values:
            if create_plan_already_advanced:
                vals.update(
                    create_operation_status=self.create_operation_status or 'success',
                    create_operation_error_code=False,
                    create_operation_error_message=False,
                )
            else:
                vals.update(create_operation_status='in_progress', state='planning')
        else:
            if create_plan_already_advanced:
                vals.update(
                    create_operation_status=self.create_operation_status or 'success',
                    create_operation_error_code=False,
                    create_operation_error_message=False,
                )
            elif self.create_operation_status not in ('pending', 'in_progress'):
                vals['create_operation_status'] = 'in_progress'
            if not create_plan_already_advanced and self.state not in ('planning',):
                vals['state'] = 'planning'
            _logger.warning(
                "Unknown Amazon inbound operation status %r for shipment %s; keeping it non-final.",
                raw_status, self.id,
            )
        self.sudo().write(vals)
        return self.create_operation_status

    def _poll_create_operation_status(self):
        self.ensure_one()
        if not self.create_operation_id:
            raise UserError(_("No create operation ID is available to check."))
        access_token = self.instance_id._get_access_token_or_raise()
        api = AmazonAPI()
        result = self.instance_id._api_call_safe(
            api.get_inbound_operation_status,
            self.instance_id,
            access_token,
            self.create_operation_id,
            error_msg=_("Failed to check inbound plan creation operation"),
        )
        response_operation_id = str((result or {}).get('operationId') or '').strip()
        operation_id_matches = response_operation_id == self.create_operation_id
        plan_result = None
        plan_verification = None
        raw_status = str((result or {}).get('operationStatus') or '').strip().upper()
        if raw_status == 'SUCCESS' and self.inbound_plan_id and operation_id_matches:
            try:
                plan_result = self.instance_id._api_call_safe(
                    api.get_inbound_plan,
                    self.instance_id,
                    access_token,
                    self.inbound_plan_id,
                    error_msg=_("Failed to refresh the created inbound plan"),
                )
            except UserError as exc:
                _logger.warning(
                    "Inbound plan %s was created but its raw plan status could not be refreshed: %s",
                    self.inbound_plan_id, exc,
                )
        elif raw_status == 'SUCCESS' and self.inbound_plan_id and not operation_id_matches:
            try:
                plan_result = self.instance_id._api_call_safe(
                    api.get_inbound_plan,
                    self.instance_id,
                    access_token,
                    self.inbound_plan_id,
                    error_msg=_("Failed to verify the created inbound plan"),
                )
                plan_items, item_pages = self._fetch_all_option_pages(
                    'list_inbound_plan_items', 'items',
                )
                plan_verification = self._inbound_plan_identity_verification(
                    plan_result, plan_items,
                )
                plan_verification['itemPages'] = len(item_pages)
                plan_verification['method'] = 'getInboundPlan+listInboundPlanItems'
            except UserError as exc:
                plan_verification = {
                    'verified': False,
                    'method': 'getInboundPlan+listInboundPlanItems',
                    'error': str(exc),
                    'failures': [str(exc)],
                }
                _logger.warning(
                    "Could not verify mismatched Amazon createInboundPlan SUCCESS for shipment %s: %s",
                    self.id, exc,
                )
        elif raw_status == 'SUCCESS' and not self.inbound_plan_id and not operation_id_matches:
            plan_verification = {
                'verified': False,
                'method': 'getInboundPlan+listInboundPlanItems',
                'error': _("No inboundPlanId is stored for independent verification."),
                'failures': [str(_("No inboundPlanId is stored for independent verification."))],
            }
        return self._apply_create_operation_status(
            result, plan_result=plan_result, plan_verification=plan_verification,
        )

    def action_create_shipment_plan(self):
        """Start createInboundPlan and enqueue polling without waiting in the browser."""
        self.ensure_one()
        self._check_inbound_manager_access()
        if self.inbound_plan_id:
            raise UserError(_(
                "Inbound Plan ID %s is already stored. Review that Amazon plan instead of creating a duplicate.",
                self.inbound_plan_id,
            ))
        if self.create_operation_status in ('pending', 'in_progress') and self.create_operation_id:
            self._ensure_create_operation_job()
            return self.instance_id._notify(
                _("Inbound Plan Creation"),
                _("Amazon operation %s is already in progress. No duplicate request was sent.", self.create_operation_id),
                'warning',
            )
        if self.create_operation_id:
            raise UserError(_(
                "Amazon Operation ID %s is already stored. Check or review that operation before any retry.",
                self.create_operation_id,
            ))
        if self.create_operation_error_code == 'CREATE_OUTCOME_UNKNOWN':
            raise UserError(_(
                "The previous Create Inbound Plan request outcome is unknown. Review Amazon before any "
                "manual retry to avoid creating a duplicate inbound plan."
            ))
        if (
            self.create_operation_error_code == 'CREATE_RATE_LIMITED'
            and self.create_retry_after_at
            and self.create_retry_after_at > fields.Datetime.now()
        ):
            raise UserError(_(
                "Amazon's rate limit is still active. Retry Create Inbound Plan after %s.",
                fields.Datetime.to_string(self.create_retry_after_at),
            ))
        if self.state not in ('draft', 'failed'):
            raise UserError(_("An inbound plan can only be created from Draft or a retryable Failed state."))

        # Validation intentionally happens before authentication and before any API call.
        payload = self._prepare_create_inbound_plan_payload()
        return self.instance_id._create_inbound_shipment_plan(self, payload=payload)

    def action_check_create_operation_status(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        try:
            status = self._poll_create_operation_status()
        except UserError as exc:
            self.sudo().write({
                'last_operation_check_at': fields.Datetime.now(),
                'create_operation_error_code': 'POLL_REQUEST_FAILED',
                'create_operation_error_message': str(exc),
            })
            return self.instance_id._notify(
                _("Inbound Plan Creation"), str(exc), 'danger', sticky=True,
            )

        job = self.operation_job_ids.filtered(
            lambda item: item.operation_id == self.create_operation_id
            and item.operation_type == 'create_inbound_plan'
        )[:1]
        if job:
            job.sudo()._sync_from_shipment_status()
        notification_type = 'success' if status == 'success' else ('danger' if status == 'failed' else 'info')
        return self.instance_id._notify(
            _("Inbound Plan Creation"),
            _("Amazon operation %s is %s.", self.create_operation_id, self.create_operation_status),
            notification_type,
        )

    @staticmethod
    def _phase3_operation_config(operation_type):
        return {
            'generate_packing_options': {
                'operation_field': 'packing_generation_operation_id',
                'status_field': 'packing_generation_status',
                'error_code_field': 'packing_error_code',
                'error_message_field': 'packing_error_message',
                'response_field': 'packing_response',
                'label': 'Packing option generation',
            },
            'confirm_packing_option': {
                'operation_field': 'packing_confirmation_operation_id',
                'status_field': 'packing_confirmation_status',
                'error_code_field': 'packing_error_code',
                'error_message_field': 'packing_error_message',
                'response_field': 'packing_response',
                'label': 'Packing option confirmation',
            },
            'set_packing_information': {
                'operation_field': 'packing_information_operation_id',
                'status_field': 'packing_information_status',
                'error_code_field': 'packing_error_code',
                'error_message_field': 'packing_error_message',
                'response_field': 'packing_response',
                'label': 'Packing information submission',
            },
            'generate_placement_options': {
                'operation_field': 'placement_generation_operation_id',
                'status_field': 'placement_generation_status',
                'error_code_field': 'placement_error_code',
                'error_message_field': 'placement_error_message',
                'response_field': 'placement_response',
                'label': 'Placement option generation',
            },
            'confirm_placement_option': {
                'operation_field': 'placement_confirmation_operation_id',
                'status_field': 'placement_confirmation_status',
                'error_code_field': 'placement_error_code',
                'error_message_field': 'placement_error_message',
                'response_field': 'placement_response',
                'label': 'Placement option confirmation',
            },
        }.get(operation_type)

    def _merge_phase3_response(self, response_field, key, value):
        self.ensure_one()
        history = {}
        current = self[response_field]
        if current:
            try:
                history = json.loads(current)
            except (TypeError, ValueError):
                history = {'legacyResponse': current}
        history[key] = AmazonAPI._sanitize_for_log(value)
        return self._sanitized_json(history)

    @staticmethod
    def _amazon_datetime(value):
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            _logger.warning("Could not parse Amazon ISO datetime %r", value)
            return False

    @staticmethod
    def _option_fee(fees):
        valid = []
        for fee in fees or []:
            value = fee.get('value') if isinstance(fee, dict) else None
            if not isinstance(value, dict):
                continue
            code = str(value.get('code') or '').strip().upper()
            try:
                amount = float(value.get('amount') or 0.0)
            except (TypeError, ValueError):
                continue
            if code:
                valid.append((code, amount))
        if not valid:
            return 0.0, False
        currency = valid[0][0]
        return sum(amount for code, amount in valid if code == currency), currency

    def _fetch_all_option_pages(self, api_method_name, response_key, *scope_args):
        self.ensure_one()
        if not self.inbound_plan_id:
            raise UserError(_("The inbound plan must be created before loading Amazon options."))
        access_token = self.instance_id._get_access_token_or_raise()
        api = AmazonAPI()
        api_method = getattr(api, api_method_name)
        pages = []
        items = []
        token = None
        seen_tokens = set()
        for _page_number in range(100):
            result = self.instance_id._api_call_safe(
                api_method,
                self.instance_id,
                access_token,
                self.inbound_plan_id,
                *scope_args,
                20,
                token,
                error_msg=_("Failed to retrieve Amazon %s", response_key),
            )
            if not isinstance(result, dict) or not isinstance(result.get(response_key), list):
                raise UserError(_("Amazon returned an invalid %s response.", response_key))
            pages.append(result)
            items.extend(result[response_key])
            token = str((result.get('pagination') or {}).get('nextToken') or '').strip()
            if not token:
                return items, pages
            if token in seen_tokens:
                raise UserError(_("Amazon returned a repeated pagination token for %s.", response_key))
            seen_tokens.add(token)
        raise UserError(_("Amazon returned too many pages while loading %s.", response_key))

    def _planned_msku_quantities(self):
        self.ensure_one()
        quantities = {}
        for line in self.line_ids:
            msku = (line.sku or '').strip()
            if msku:
                quantities[msku] = quantities.get(msku, 0) + line.planned_quantity
        return quantities

    def _amazon_product_for_msku(self, msku):
        self.ensure_one()
        plan_line = self.line_ids.filtered(lambda line: (line.sku or '').strip() == msku)[:1]
        if plan_line.amazon_product_id:
            return plan_line.amazon_product_id
        return self.env['amazon.product'].sudo().search([
            ('instance_id', '=', self.instance_id.id),
            ('sku', '=', msku),
        ], limit=1)

    def _validated_amazon_items(self, item_values, context_label):
        validated = []
        seen = set()
        for value in item_values:
            if not isinstance(value, dict):
                raise UserError(_("Amazon returned an invalid item for %s.", context_label))
            msku = str(value.get('msku') or '').strip()
            quantity = value.get('quantity')
            if not msku:
                raise UserError(_("Amazon returned an item without an MSKU for %s.", context_label))
            if msku in seen:
                raise UserError(_("Amazon returned duplicate MSKU %s for %s.", msku, context_label))
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 500000:
                raise UserError(_("Amazon returned an invalid quantity for MSKU %s in %s.", msku, context_label))
            seen.add(msku)
            validated.append((msku, quantity, value))
        return validated

    def _assert_quantity_distribution(self, actual, context_label):
        self.ensure_one()
        planned = self._planned_msku_quantities()
        if planned and actual != planned:
            raise UserError(_(
                "%s quantities do not match the inbound plan. Planned: %s; Amazon: %s",
                context_label, planned, actual,
            ))

    def _sync_packing_groups(self, option, packing_group_ids):
        self.ensure_one()
        Group = self.env['amazon.fba.packing.group'].sudo()
        Item = self.env['amazon.fba.packing.group.item'].sudo()
        group_ids = [str(value or '').strip() for value in packing_group_ids or []]
        if not group_ids or len(group_ids) != len(set(group_ids)) or any(
            not INBOUND_PLAN_ID_RE.fullmatch(group_id) for group_id in group_ids
        ):
            raise UserError(_("Amazon returned invalid packingGroupIds for packing option %s.", option.amazon_packing_option_id))

        totals = {}
        for group_id in group_ids:
            items, pages = self._fetch_all_option_pages(
                'list_packing_group_items', 'items', group_id,
            )
            group = Group.search([
                ('packing_option_id', '=', option.id),
                ('amazon_packing_group_id', '=', group_id),
            ], limit=1)
            vals = {
                'packing_option_id': option.id,
                'amazon_packing_group_id': group_id,
                'raw_response': self._sanitized_json(pages),
            }
            if group:
                group.write(vals)
            else:
                group = Group.create(vals)

            seen_mskus = set()
            for msku, quantity, value in self._validated_amazon_items(
                items, _("packing group %s", group_id),
            ):
                seen_mskus.add(msku)
                totals[msku] = totals.get(msku, 0) + quantity
                product = self._amazon_product_for_msku(msku)
                item_vals = {
                    'packing_group_id': group.id,
                    'amazon_product_id': product.id or False,
                    'msku': msku,
                    'asin': str(value.get('asin') or '').strip() or False,
                    'fnsku': str(value.get('fnsku') or '').strip() or False,
                    'quantity': quantity,
                    'raw_response': self._sanitized_json(value),
                }
                item = Item.search([
                    ('packing_group_id', '=', group.id), ('msku', '=', msku),
                ], limit=1)
                if item:
                    item.write(item_vals)
                else:
                    Item.create(item_vals)
            group.item_ids.filtered(lambda item: item.msku not in seen_mskus).unlink()

        option.packing_group_ids.filtered(
            lambda group: group.amazon_packing_group_id not in group_ids
        ).unlink()
        self._assert_quantity_distribution(
            totals, _("Packing option %s", option.amazon_packing_option_id),
        )

    def _sync_packing_options(self, option_values):
        self.ensure_one()
        Option = self.env['amazon.fba.packing.option'].sudo()
        seen = set()
        synced = Option
        accepted = Option
        for position, value in enumerate(option_values, start=1):
            if not isinstance(value, dict):
                raise UserError(_("Amazon returned an invalid packing option."))
            option_id = str(value.get('packingOptionId') or '').strip()
            if not INBOUND_PLAN_ID_RE.fullmatch(option_id):
                raise UserError(_("Amazon returned an invalid packingOptionId."))
            if option_id in seen:
                raise UserError(_("Amazon returned duplicate packing option %s.", option_id))
            seen.add(option_id)
            status = str(value.get('status') or '').strip().upper()
            fee_amount, fee_currency = self._option_fee(value.get('fees'))
            discount_amount, discount_currency = self._option_fee(value.get('discounts'))
            vals = {
                'instance_id': self.instance_id.id,
                'inbound_shipment_id': self.id,
                'amazon_packing_option_id': option_id,
                'option_name': _("Amazon Packing Option %s", position),
                'status': status or False,
                'expiration_date': self._amazon_datetime(value.get('expiration')),
                'fee_amount': fee_amount,
                'fee_currency': fee_currency,
                'discount_amount': discount_amount,
                'discount_currency': discount_currency,
                'amazon_packing_group_ids': json.dumps(
                    value.get('packingGroups') or [], ensure_ascii=False,
                ),
                'raw_response': self._sanitized_json(value),
            }
            option = Option.search([
                ('inbound_shipment_id', '=', self.id),
                ('amazon_packing_option_id', '=', option_id),
            ], limit=1)
            if option:
                vals.pop('option_name')
                option.write(vals)
            else:
                option = Option.create(vals)
            synced |= option
            if status == 'ACCEPTED':
                accepted |= option

        if len(accepted) > 1:
            raise UserError(_("Amazon returned more than one accepted packing option."))
        all_options = Option.search([('inbound_shipment_id', '=', self.id)])
        if accepted:
            all_options.filtered('selected').with_context(
                amazon_sync_option_selection=True,
            ).write({'selected': False})
            accepted.with_context(amazon_sync_option_selection=True).write({'selected': True})
        else:
            all_options.filtered(
                lambda option: option.selected and option.status == 'EXPIRED'
            ).with_context(amazon_sync_option_selection=True).write({'selected': False})

        vals = {}
        if accepted and self.state in ('plan_created', 'packing_generated'):
            vals['state'] = 'packing_confirmed'
        elif synced and self.state == 'plan_created':
            vals['state'] = 'packing_generated'
        if vals:
            self.sudo().write(vals)
        return synced

    def _sync_placement_options(self, option_values):
        self.ensure_one()
        Option = self.env['amazon.fba.placement.option'].sudo()
        seen = set()
        synced = Option
        accepted = Option
        for value in option_values:
            if not isinstance(value, dict):
                raise UserError(_("Amazon returned an invalid placement option."))
            option_id = str(value.get('placementOptionId') or '').strip()
            if not INBOUND_PLAN_ID_RE.fullmatch(option_id):
                raise UserError(_("Amazon returned an invalid placementOptionId."))
            if option_id in seen:
                raise UserError(_("Amazon returned duplicate placement option %s.", option_id))
            seen.add(option_id)
            status = str(value.get('status') or '').strip().upper()
            fee_amount, fee_currency = self._option_fee(value.get('fees'))
            discount_amount, discount_currency = self._option_fee(value.get('discounts'))
            vals = {
                'inbound_shipment_id': self.id,
                'amazon_placement_option_id': option_id,
                'status': status or False,
                'amazon_shipment_ids': json.dumps(
                    value.get('shipmentIds') or [], ensure_ascii=False,
                ),
                'fee': fee_amount,
                'currency': fee_currency,
                'discount': discount_amount,
                'discount_currency': discount_currency,
                'expiration_date': self._amazon_datetime(value.get('expiration')),
                'raw_response': self._sanitized_json(value),
            }
            option = Option.search([
                ('inbound_shipment_id', '=', self.id),
                ('amazon_placement_option_id', '=', option_id),
            ], limit=1)
            if option:
                option.write(vals)
            else:
                option = Option.create(vals)
            synced |= option
            if status == 'ACCEPTED':
                accepted |= option

        if len(accepted) > 1:
            raise UserError(_("Amazon returned more than one accepted placement option."))
        all_options = Option.search([('inbound_shipment_id', '=', self.id)])
        if accepted:
            all_options.filtered('selected').with_context(
                amazon_sync_option_selection=True,
            ).write({'selected': False})
            accepted.with_context(amazon_sync_option_selection=True).write({'selected': True})
        else:
            all_options.filtered(
                lambda option: option.selected and option.status == 'EXPIRED'
            ).with_context(amazon_sync_option_selection=True).write({'selected': False})

        vals = {}
        if accepted and self.state in ('packing_confirmed', 'placement_generated'):
            vals['state'] = 'placement_confirmed'
        elif synced and self.state == 'packing_confirmed':
            vals['state'] = 'placement_generated'
        if vals:
            self.sudo().write(vals)
        return synced

    def _refresh_packing_options(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            options, pages = self._fetch_all_option_pages('list_packing_options', 'packingOptions')
            synced = self._sync_packing_options(options)
            by_id = {
                str(value.get('packingOptionId') or '').strip(): value
                for value in options if isinstance(value, dict)
            }
            for option in synced:
                self._sync_packing_groups(
                    option, (by_id.get(option.amazon_packing_option_id) or {}).get('packingGroups'),
                )
            self.sudo().write({
                'packing_last_refresh_at': fields.Datetime.now(),
                'packing_error_code': False,
                'packing_error_message': False,
                'packing_response': self._merge_phase3_response(
                    'packing_response', 'listPackingOptions', pages,
                ),
            })
        return synced

    def _refresh_placement_options(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            options, pages = self._fetch_all_option_pages('list_placement_options', 'placementOptions')
            synced = self._sync_placement_options(options)
            self.sudo().write({
                'placement_last_refresh_at': fields.Datetime.now(),
                'placement_error_code': False,
                'placement_error_message': False,
                'placement_response': self._merge_phase3_response(
                    'placement_response', 'listPlacementOptions', pages,
                ),
            })
        return synced

    def _sync_confirmed_physical_shipments(self, placement_option):
        """Materialize shipment splits only after placement confirmation succeeds."""
        self.ensure_one()
        PhysicalShipment = self.env['amazon.fba.physical.shipment'].sudo()
        PhysicalLine = self.env['amazon.fba.physical.shipment.line'].sudo()
        try:
            shipment_ids = json.loads(placement_option.amazon_shipment_ids or '[]')
        except (TypeError, ValueError):
            shipment_ids = []
        shipment_ids = [str(value or '').strip() for value in shipment_ids]
        if not shipment_ids or len(shipment_ids) != len(set(shipment_ids)) or any(
            not INBOUND_PLAN_ID_RE.fullmatch(shipment_id) for shipment_id in shipment_ids
        ):
            raise UserError(_("The confirmed placement option has invalid Amazon shipment IDs."))

        access_token = self.instance_id._get_access_token_or_raise()
        api = AmazonAPI()
        totals = {}
        synced = PhysicalShipment
        destinations = set()
        for shipment_id in shipment_ids:
            result = self.instance_id._api_call_safe(
                api.get_shipment,
                self.instance_id,
                access_token,
                self.inbound_plan_id,
                shipment_id,
                error_msg=_("Failed to retrieve Amazon shipment %s", shipment_id),
            )
            if not isinstance(result, dict):
                raise UserError(_("Amazon returned an invalid getShipment response."))
            returned_shipment_id = str(result.get('shipmentId') or '').strip()
            returned_option_id = str(result.get('placementOptionId') or '').strip()
            confirmation_id = str(result.get('shipmentConfirmationId') or '').strip()
            if returned_shipment_id != shipment_id:
                raise UserError(_("Amazon getShipment returned a mismatched shipmentId."))
            if returned_option_id != placement_option.amazon_placement_option_id:
                raise UserError(_("Amazon getShipment returned a mismatched placementOptionId."))
            if not confirmation_id:
                raise UserError(_("Amazon has not produced a shipmentConfirmationId yet."))
            destination = result.get('destination') if isinstance(result.get('destination'), dict) else {}
            destination_fc = str(destination.get('warehouseId') or '').strip()
            if destination_fc:
                destinations.add(destination_fc)
            shipment_vals = {
                'inbound_shipment_id': self.id,
                'placement_option_id': placement_option.id,
                'amazon_shipment_id': shipment_id,
                'shipment_confirmation_id': confirmation_id,
                'amazon_reference_id': str(result.get('amazonReferenceId') or '').strip() or False,
                'name': str(result.get('name') or '').strip() or False,
                'status': str(result.get('status') or '').strip().upper() or False,
                'destination_fc': destination_fc or False,
                'raw_response': self._sanitized_json(result),
            }
            physical = PhysicalShipment.search([
                ('inbound_shipment_id', '=', self.id),
                ('amazon_shipment_id', '=', shipment_id),
            ], limit=1)
            if physical:
                physical.write(shipment_vals)
            else:
                physical = PhysicalShipment.create(shipment_vals)
            synced |= physical

            items, pages = self._fetch_all_option_pages(
                'list_shipment_items', 'items', shipment_id,
            )
            seen_mskus = set()
            for msku, quantity, value in self._validated_amazon_items(
                items, _("shipment %s", shipment_id),
            ):
                seen_mskus.add(msku)
                totals[msku] = totals.get(msku, 0) + quantity
                product = self._amazon_product_for_msku(msku)
                line_vals = {
                    'physical_shipment_id': physical.id,
                    'amazon_product_id': product.id or False,
                    'msku': msku,
                    'asin': str(value.get('asin') or '').strip() or False,
                    'fnsku': str(value.get('fnsku') or '').strip() or False,
                    'quantity': quantity,
                    'raw_response': self._sanitized_json(value),
                }
                line = PhysicalLine.search([
                    ('physical_shipment_id', '=', physical.id), ('msku', '=', msku),
                ], limit=1)
                if line:
                    line.write(line_vals)
                else:
                    PhysicalLine.create(line_vals)
            physical.line_ids.filtered(lambda line: line.msku not in seen_mskus).unlink()
            physical.write({
                'raw_response': self._sanitized_json({
                    'getShipment': result,
                    'listShipmentItems': pages,
                }),
            })

        self.physical_shipment_ids.filtered(
            lambda shipment: shipment.amazon_shipment_id not in shipment_ids
        ).unlink()
        self._assert_quantity_distribution(totals, _("Placement shipment distribution"))
        placement_option.sudo().write({
            'destination_fc': next(iter(destinations)) if len(destinations) == 1 else False,
        })
        return synced

    def _ensure_phase3_operation_job(self, operation_type, operation_id, option=False):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        job = Job.search([
            ('operation_type', '=', operation_type),
            ('operation_id', '=', operation_id),
        ], limit=1)
        config = self._phase3_operation_config(operation_type)
        if job:
            if job.inbound_shipment_id != self:
                raise ValidationError(_("This Amazon operation is already linked to another inbound shipment."))
            if job.state == 'failed' and self[config['status_field']] in ('pending', 'in_progress'):
                job.write({
                    'state': 'pending',
                    'next_run_at': fields.Datetime.now(),
                    'finished_at': False,
                    'last_error': False,
                })
            return job
        vals = {
            'inbound_shipment_id': self.id,
            'operation_type': operation_type,
            'operation_id': operation_id,
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }
        if option and option._name == 'amazon.fba.packing.option':
            vals['packing_option_id'] = option.id
        elif option and option._name == 'amazon.fba.placement.option':
            vals['placement_option_id'] = option.id
        return Job.create(vals)

    def _enqueue_refresh_job(self, operation_type):
        self.ensure_one()
        Job = self.env['amazon.inbound.operation.job'].sudo()
        active = Job.search([
            ('inbound_shipment_id', '=', self.id),
            ('operation_type', '=', operation_type),
            ('state', 'in', ('pending', 'in_progress')),
        ], limit=1)
        if active:
            return active, False
        return Job.create({
            'inbound_shipment_id': self.id,
            'operation_type': operation_type,
            'state': 'pending',
            'next_run_at': fields.Datetime.now(),
        }), True

    def _start_phase3_operation(self, operation_type, api_method_name, api_args=(), option=False):
        self.ensure_one()
        config = self._phase3_operation_config(operation_type)
        if not config:
            raise UserError(_("Unsupported inbound operation type: %s", operation_type))
        operation_field = config['operation_field']
        status_field = config['status_field']
        if (
            not self[operation_field]
            and self[status_field] == 'failed'
            and self[config['response_field']]
        ):
            raise UserError(_(
                "Amazon returned an invalid response for %s. Review the stored response before any retry; resubmitting could create a duplicate.",
                config['label'],
            ))
        if self[operation_field]:
            if self[status_field] in ('pending', 'in_progress'):
                self._ensure_phase3_operation_job(
                    operation_type, self[operation_field], option=option,
                )
                return self.instance_id._notify(
                    config['label'],
                    _("Amazon operation %s is already in progress. No duplicate request was sent.", self[operation_field]),
                    'warning',
                )
            raise UserError(_(
                "Amazon operation %s is already stored for %s. A duplicate request is unsafe.",
                self[operation_field], config['label'],
            ))

        access_token = self.instance_id._get_access_token_or_raise()
        api = AmazonAPI()
        try:
            result = self.instance_id._api_call_safe(
                getattr(api, api_method_name),
                self.instance_id,
                access_token,
                self.inbound_plan_id,
                *api_args,
                error_msg=_("Failed to start %s", config['label']),
            )
        except UserError as exc:
            cause = getattr(exc, '__cause__', None)
            response = getattr(cause, 'response', None)
            status_code = getattr(response, 'status_code', None)
            if status_code is not None and status_code < 500:
                raise
            response_text = self._merge_phase3_response(
                config['response_field'], api_method_name,
                {'error': str(exc), 'outcome': 'UNKNOWN'},
            )
            self.sudo().write({
                status_field: 'failed',
                config['error_code_field']: 'WRITE_OUTCOME_UNKNOWN',
                config['error_message_field']: _(
                    "Amazon may have accepted this write, but no operationId was received. "
                    "Do not retry until the plan is reconciled in Amazon."
                ),
                config['response_field']: response_text,
            })
            return self.instance_id._notify(
                config['label'],
                _("Amazon write outcome is unknown. No automatic retry will be attempted."),
                'danger', sticky=True,
            )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        operation_id = str(result.get('operationId') or '').strip()
        response_text = self._merge_phase3_response(
            config['response_field'], api_method_name, result,
        )
        if not OPERATION_ID_RE.fullmatch(operation_id):
            self.sudo().write({
                status_field: 'failed',
                config['error_code_field']: 'INVALID_OPERATION_RESPONSE',
                config['error_message_field']: _("Amazon did not return a valid operationId."),
                config['response_field']: response_text,
            })
            return self.instance_id._notify(
                config['label'], _("Amazon did not return a valid operationId."),
                'danger', sticky=True,
            )
        self.sudo().write({
            operation_field: operation_id,
            status_field: 'pending',
            config['error_code_field']: False,
            config['error_message_field']: False,
            config['response_field']: response_text,
        })
        self._ensure_phase3_operation_job(operation_type, operation_id, option=option)
        return self.instance_id._notify(
            config['label'],
            _("Amazon operation %s was queued for background polling.", operation_id),
            'success',
        )

    def _complete_phase3_operation(self, job):
        self.ensure_one()
        operation_type = job.operation_type
        config = self._phase3_operation_config(operation_type)
        if operation_type == 'generate_packing_options':
            if not self._refresh_packing_options():
                raise UserError(_("Amazon completed packing generation but no packing options are available yet."))
            state = self._monotonic_state('packing_generated') if self.state == 'plan_created' else self.state
        elif operation_type == 'confirm_packing_option':
            self._refresh_packing_options()
            state = self._monotonic_state('packing_confirmed')
        elif operation_type == 'set_packing_information':
            state = self.state
        elif operation_type == 'generate_placement_options':
            if not self._refresh_placement_options():
                raise UserError(_("Amazon completed placement generation but no placement options are available yet."))
            state = self._monotonic_state('placement_generated') if self.state == 'packing_confirmed' else self.state
        elif operation_type == 'confirm_placement_option':
            self._refresh_placement_options()
            selected = self.placement_option_ids.filtered('selected')
            if len(selected) != 1 or selected.status != 'ACCEPTED':
                raise UserError(_("Amazon did not return exactly one accepted placement option."))
            self._sync_confirmed_physical_shipments(selected)
            state = self._monotonic_state('placement_confirmed')
        else:
            raise UserError(_("Unsupported inbound operation type: %s", operation_type))
        self.sudo().write({
            config['status_field']: 'success',
            config['error_code_field']: False,
            config['error_message_field']: False,
            'state': state,
        })

    def _poll_phase3_operation(self, job):
        self.ensure_one()
        config = self._phase3_operation_config(job.operation_type)
        if not config or not job.operation_id:
            raise UserError(_("The inbound operation job has no pollable Amazon operation ID."))
        if self[config['status_field']] == 'success':
            return 'success'
        access_token = self.instance_id._get_access_token_or_raise()
        result = self.instance_id._api_call_safe(
            AmazonAPI().get_inbound_operation_status,
            self.instance_id,
            access_token,
            job.operation_id,
            error_msg=_("Failed to poll %s", config['label']),
        )
        if not isinstance(result, dict):
            result = {'unexpectedResponse': result}
        raw_status = str(result.get('operationStatus') or '').strip()
        normalized = raw_status.upper().replace('-', '_').replace(' ', '_')
        request_id = str(result.get('_amazon_request_id') or '').strip()
        error_code, error_message = self._operation_problem_values(result.get('operationProblems'))
        response_text = self._merge_phase3_response(
            config['response_field'], 'getInboundOperationStatus:%s' % job.operation_type, result,
        )
        self.sudo().write({config['response_field']: response_text})
        job.sudo().write({
            'raw_operation_status': raw_status or False,
            'amazon_request_id': request_id or False,
            'response_data': self._sanitized_json(result),
        })

        if normalized in {'SUCCESS', 'SUCCEEDED', 'COMPLETED', 'COMPLETE'}:
            with self.env.cr.savepoint():
                self._complete_phase3_operation(job)
            return 'success'
        if normalized in {'FAILED', 'FAILURE', 'ERROR'}:
            self.sudo().write({
                config['status_field']: 'failed',
                config['error_code_field']: error_code or 'AMAZON_OPERATION_FAILED',
                config['error_message_field']: error_message or _("Amazon reported that the operation failed."),
            })
            return 'failed'
        if normalized in {'PENDING', 'QUEUED', 'NOT_STARTED'}:
            status = 'pending'
        else:
            status = 'in_progress'
            if normalized not in {'IN_PROGRESS', 'PROCESSING', 'RUNNING'}:
                _logger.warning(
                    "Unknown Amazon inbound operation status %r for job %s; keeping it non-final.",
                    raw_status, job.id,
                )
        self.sudo().write({
            config['status_field']: status,
            config['error_code_field']: error_code,
            config['error_message_field']: error_message,
        })
        return status

    def _record_phase3_job_error(self, job, message):
        self.ensure_one()
        config = self._phase3_operation_config(job.operation_type)
        if config and self[config['status_field']] == 'success':
            return
        if job.operation_type in ('refresh_packing_options',) or (
            config and config['response_field'] == 'packing_response'
        ):
            self.sudo().write({
                'packing_error_code': 'BACKGROUND_JOB_FAILED',
                'packing_error_message': message,
            })
        elif job.operation_type in ('refresh_placement_options',) or config:
            self.sudo().write({
                'placement_error_code': 'BACKGROUND_JOB_FAILED',
                'placement_error_message': message,
            })

    def _lock_phase3_workflow(self):
        """Serialize Phase 3 buttons so concurrent requests cannot submit twice."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM amazon_inbound_shipment WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()

    def _ensure_plan_ready_for_packing(self):
        self.ensure_one()
        if not self.inbound_plan_id:
            raise UserError(_("Packing options require a valid inboundPlanId."))
        if self.create_operation_status != 'success':
            raise UserError(_("Packing options require a successfully completed Create Inbound Plan operation."))

    def _ensure_packing_confirmed_for_placement(self):
        self.ensure_one()
        selected = self.packing_option_ids.filtered('selected')
        if (
            self.packing_confirmation_status != 'success'
            or len(selected) != 1
            or selected.status != 'ACCEPTED'
        ):
            raise UserError(_("Placement options require one successfully confirmed packing option."))
        if self.packing_information_status != 'success':
            raise UserError(_(
                "Submit box-level packing information successfully before generating placement options."
            ))

    def _prepare_packing_information_payload(self):
        """Validate local cartons and build the official SetPackingInformationRequest."""
        self.ensure_one()
        selected = self.packing_option_ids.filtered('selected')
        if (
            self.packing_confirmation_status != 'success'
            or len(selected) != 1
            or selected.status != 'ACCEPTED'
        ):
            raise UserError(_("Confirm exactly one Amazon packing option before submitting boxes."))
        groups = selected.packing_group_ids
        if not groups:
            raise UserError(_("The accepted packing option has no Amazon packing groups."))
        boxes = selected.box_ids
        if not boxes:
            raise UserError(_("Enter at least one box with dimensions, weight, and contents."))

        inbound_lines = {(line.sku or '').strip(): line for line in self.line_ids}
        package_groupings = []
        for group in groups:
            group_id = (group.amazon_packing_group_id or '').strip()
            group_boxes = boxes.filtered(
                lambda box: (box.amazon_packing_group_id or '').strip() == group_id
            )
            if not group_boxes:
                raise UserError(_("Packing group %s has no local box information.", group_id))
            expected = {(item.msku or '').strip(): item.quantity for item in group.item_ids}
            actual = {}
            box_values = []
            for box in group_boxes:
                if not box.line_ids:
                    raise UserError(_("Every box in packing group %s must contain at least one item.", group_id))
                item_values = []
                for box_line in box.line_ids:
                    msku = (box_line.msku or '').strip()
                    inbound_line = inbound_lines.get(msku)
                    if not inbound_line:
                        raise UserError(_("Box MSKU %s is not present on the inbound plan.", msku))
                    actual[msku] = actual.get(msku, 0) + box_line.quantity
                    item_value = {
                        'msku': msku,
                        'quantity': box_line.quantity,
                        'prepOwner': inbound_line.prep_owner,
                        'labelOwner': inbound_line.label_owner,
                    }
                    if inbound_line.expiration_date:
                        # Amazon Fulfillment Inbound v2024-03-20 ItemInput
                        # uses the same physical expiration field in both
                        # createInboundPlan and setPackingInformation.
                        item_value['expiration'] = fields.Date.to_string(inbound_line.expiration_date)
                    item_values.append(item_value)
                box_values.append({
                    'contentInformationSource': 'BOX_CONTENT_PROVIDED',
                    'dimensions': {
                        'unitOfMeasurement': box.dimension_unit,
                        'length': box.length,
                        'width': box.width,
                        'height': box.height,
                    },
                    'weight': {'unit': box.weight_unit, 'value': box.weight},
                    'quantity': 1,
                    'items': item_values,
                })
            if actual != expected:
                raise UserError(_(
                    "Box contents for packing group %s must exactly match Amazon's group items. "
                    "Expected %s; found %s.", group_id, expected, actual,
                ))
            package_groupings.append({
                'packingGroupId': group_id,
                'boxes': box_values,
            })
        assigned_groups = {
            (box.amazon_packing_group_id or '').strip() for box in boxes
        }
        expected_groups = set(groups.mapped('amazon_packing_group_id'))
        if assigned_groups != expected_groups:
            raise UserError(_("Every local box must reference a packing group from the accepted option."))
        return {'packageGroupings': package_groupings}

    def action_generate_packing_options(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        self._ensure_plan_ready_for_packing()
        regeneration = self.state == 'packing_generated' and self.packing_options_expired
        if self.state != 'plan_created' and not regeneration:
            raise UserError(_("Packing options can only be generated after the inbound plan is created."))
        if self.packing_option_ids and not regeneration:
            raise UserError(_("Packing options already exist. Refresh them instead of generating duplicates."))
        if regeneration:
            if self.packing_confirmation_status in ('pending', 'in_progress', 'success'):
                raise UserError(_("Packing options cannot be regenerated after confirmation starts."))
            self.packing_option_ids.filtered('selected').with_context(
                amazon_sync_option_selection=True,
            ).write({'selected': False})
            self.sudo().write({
                'state': 'plan_created',
                'packing_generation_operation_id': False,
                'packing_generation_status': False,
                'packing_confirmation_operation_id': False,
                'packing_confirmation_status': False,
            })
        return self._start_phase3_operation(
            'generate_packing_options', 'generate_packing_options',
        )

    def action_refresh_packing_options(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        self._ensure_plan_ready_for_packing()
        _job, created = self._enqueue_refresh_job('refresh_packing_options')
        return self.instance_id._notify(
            _("Packing Options"),
            _("Packing option refresh was queued.") if created else _("A packing option refresh is already queued."),
            'success' if created else 'warning',
        )

    def action_confirm_packing_option(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        if self.state == 'packing_confirmed':
            return self.instance_id._notify(
                _("Packing Option"), _("A packing option is already confirmed."), 'warning',
            )
        if self.state != 'packing_generated':
            raise UserError(_("Generate and refresh packing options before confirmation."))
        selected = self.packing_option_ids.filtered('selected')
        if len(selected) != 1:
            raise UserError(_("Select exactly one packing option before confirmation."))
        if selected.status == 'EXPIRED' or (
            selected.expiration_date and selected.expiration_date <= fields.Datetime.now()
        ):
            raise UserError(_("The selected packing option has expired. Generate fresh options."))
        if selected.status != 'OFFERED':
            raise UserError(_(
                "Only an Amazon packing option with status OFFERED can be confirmed. Refresh packing options first."
            ))
        return self._start_phase3_operation(
            'confirm_packing_option', 'confirm_packing_option',
            api_args=(selected.amazon_packing_option_id,), option=selected,
        )

    def action_set_packing_information(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        if self.packing_information_status == 'success':
            return self.instance_id._notify(
                _("Packing Information"), _("Box-level packing information is already confirmed."),
                'warning',
            )
        body = self._prepare_packing_information_payload()
        return self._start_phase3_operation(
            'set_packing_information', 'set_packing_information', api_args=(body,),
        )

    def action_open_bulk_box_wizard(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        selected = self.packing_option_ids.filtered('selected')
        if (
            self.state != 'packing_confirmed'
            or self.packing_confirmation_status != 'success'
            or len(selected) != 1
            or selected.status != 'ACCEPTED'
        ):
            raise UserError(_("Confirm exactly one Amazon packing option before creating boxes."))
        groups = selected.packing_group_ids
        default_group = groups if len(groups) == 1 else self.env['amazon.fba.packing.group']
        default_item = (
            default_group.item_ids
            if default_group and len(default_group.item_ids) == 1
            else self.env['amazon.fba.packing.group.item']
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _("Bulk Create Boxes"),
            'res_model': 'amazon.fba.bulk.box.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_inbound_shipment_id': self.id,
                'default_packing_option_id': selected.id,
                'default_packing_group_id': default_group.id if default_group else False,
                'default_packing_group_item_id': default_item.id if default_item else False,
            },
        }

    def action_generate_placement_options(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        self._ensure_packing_confirmed_for_placement()
        regeneration = self.state == 'placement_generated' and self.placement_options_expired
        if self.state != 'packing_confirmed' and not regeneration:
            raise UserError(_("Placement options cannot be generated before packing confirmation."))
        if self.placement_option_ids and not regeneration:
            raise UserError(_("Placement options already exist. Refresh them instead of generating duplicates."))
        if regeneration:
            if self.placement_confirmation_status in ('pending', 'in_progress', 'success'):
                raise UserError(_("Placement options cannot be regenerated after confirmation starts."))
            self.placement_option_ids.filtered('selected').with_context(
                amazon_sync_option_selection=True,
            ).write({'selected': False})
            self.sudo().write({
                'state': 'packing_confirmed',
                'placement_generation_operation_id': False,
                'placement_generation_status': False,
                'placement_confirmation_operation_id': False,
                'placement_confirmation_status': False,
            })
        # The official schema requires a JSON body. customPlacement is optional and India-only.
        return self._start_phase3_operation(
            'generate_placement_options', 'generate_placement_options', api_args=({},),
        )

    def action_refresh_placement_options(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        self._ensure_packing_confirmed_for_placement()
        if self.state not in ('packing_confirmed', 'placement_generated', 'placement_confirmed'):
            raise UserError(_("Confirm packing before refreshing placement options."))
        _job, created = self._enqueue_refresh_job('refresh_placement_options')
        return self.instance_id._notify(
            _("Placement Options"),
            _("Placement option refresh was queued.") if created else _("A placement option refresh is already queued."),
            'success' if created else 'warning',
        )

    def action_confirm_placement_option(self):
        self.ensure_one()
        self._check_inbound_manager_access()
        self._lock_phase3_workflow()
        if self.state == 'placement_confirmed':
            return self.instance_id._notify(
                _("Placement Option"), _("A placement option is already confirmed."), 'warning',
            )
        if self.state != 'placement_generated':
            raise UserError(_("Generate and refresh placement options before confirmation."))
        selected = self.placement_option_ids.filtered('selected')
        if len(selected) != 1:
            raise UserError(_("Select exactly one placement option before confirmation."))
        if selected.status == 'EXPIRED' or (
            selected.expiration_date and selected.expiration_date <= fields.Datetime.now()
        ):
            raise UserError(_("The selected placement option has expired. Generate fresh options."))
        if selected.status != 'OFFERED':
            raise UserError(_(
                "Only an Amazon placement option with status OFFERED can be confirmed. Refresh placement options first."
            ))
        return self._start_phase3_operation(
            'confirm_placement_option', 'confirm_placement_option',
            api_args=(selected.amazon_placement_option_id,), option=selected,
        )

    @staticmethod
    def _later_phase_error():
        raise UserError(_("This action belongs to a later FBA workflow phase."))

    def action_submit_shipment(self):
        self.ensure_one()
        return self._later_phase_error()

    def action_mark_shipped(self):
        self.ensure_one()
        return self._later_phase_error()

    def action_check_status(self):
        """Backward-compatible alias; Phase 2 checks only create operation status."""
        return self.action_check_create_operation_status()

    def action_import_by_shipment_id(self):
        self.ensure_one()
        return self._later_phase_error()

    def action_cancel(self):
        self.ensure_one()
        if self.inbound_plan_id or self.create_operation_id:
            raise UserError(_("An Amazon plan or operation already exists; local cancellation is unsafe."))
        if self.state != 'draft':
            raise UserError(_("Only a draft with no Amazon identifiers can be cancelled locally."))
        self.state = 'cancelled'

    def action_get_labels(self):
        self.ensure_one()
        return self._later_phase_error()


class AmazonInboundShipmentLine(models.Model):
    _name = 'amazon.inbound.shipment.line'
    _description = 'Amazon Inbound Shipment Line'
    _check_company_auto = True

    shipment_id = fields.Many2one(
        'amazon.inbound.shipment', string='Shipment', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        'res.company', related='shipment_id.company_id', store=True, readonly=True, index=True,
    )
    amazon_product_id = fields.Many2one('amazon.product', string='Amazon Product')
    odoo_product_id = fields.Many2one(
        'product.product', string='Odoo Product', check_company=True,
    )
    sku = fields.Char('SKU', required=True)
    fnsku = fields.Char('FNSKU')
    planned_quantity = fields.Integer(
        'Planned Quantity',
        help="Positive integer quantity sent in createInboundPlan. This is separate from later physical shipped quantity.",
    )
    prep_owner = fields.Selection(AMAZON_OWNER_SELECTION, string='Prep Owner')
    label_owner = fields.Selection(AMAZON_OWNER_SELECTION, string='Label Owner')
    expiration_date = fields.Date(
        'Expiration Date',
        help=(
            "Physical inventory expiration date for this planned inbound item. Enter it when "
            "Amazon requires an expiration date for the MSKU; it is sent to Amazon as the "
            "createInboundPlan item expiration date and is different from packing or placement "
            "option expiration times."
        ),
    )
    quantity_shipped = fields.Float('Qty Shipped')
    quantity_received = fields.Float('Qty Received')
    quantity_in_case = fields.Float('Qty Per Case')
    quantity_discrepancy = fields.Float('Discrepancy', compute='_compute_discrepancy', store=True)

    @api.model
    def _values_from_amazon_product(self, amazon_product_id):
        product = self.env['amazon.product'].browse(amazon_product_id).exists()
        return {
            'sku': product.sku or False,
            'odoo_product_id': product.odoo_product_id.id or False,
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('amazon_product_id'):
                vals.update(self._values_from_amazon_product(vals['amazon_product_id']))
        return super().create(vals_list)

    def write(self, vals):
        if 'amazon_product_id' in vals:
            vals = dict(vals)
            vals.update(self._values_from_amazon_product(vals['amazon_product_id']))
        return super().write(vals)

    @api.onchange('amazon_product_id')
    def _onchange_amazon_product_id(self):
        self.sku = self.amazon_product_id.sku or False
        self.odoo_product_id = self.amazon_product_id.odoo_product_id

    @api.constrains('planned_quantity')
    def _check_planned_quantity(self):
        for line in self:
            if not isinstance(line.planned_quantity, int) or not 1 <= line.planned_quantity <= 500000:
                raise ValidationError(_("Planned quantity must be an integer from 1 to 500000."))

    @api.constrains('amazon_product_id', 'shipment_id')
    def _check_amazon_product_instance(self):
        for line in self:
            if (
                line.amazon_product_id
                and line.amazon_product_id.instance_id != line.shipment_id.instance_id
            ):
                raise ValidationError(_("The Amazon Product must belong to the shipment's Amazon instance."))

    @api.depends('quantity_shipped', 'quantity_received')
    def _compute_discrepancy(self):
        for line in self:
            line.quantity_discrepancy = line.quantity_shipped - line.quantity_received
