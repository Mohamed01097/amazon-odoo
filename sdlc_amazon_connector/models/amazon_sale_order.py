import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

from .amazon_api import AmazonAPI, amazon_to_utc_naive
from ..services.ai_service import AmazonAIService

_logger = logging.getLogger(__name__)

KNOWN_AMAZON_ORDER_STATUSES = {
    'PendingAvailability',
    'Pending',
    'Unshipped',
    'PartiallyShipped',
    'Shipped',
    'InvoiceUnconfirmed',
    'Canceled',
    'Unfulfillable',
}


class AmazonSaleOrder(models.Model):
    _name = 'amazon.sale.order'
    _description = 'Amazon Sale Order'
    _order = 'purchase_date desc'
    _rec_name = 'amazon_order_ref'

    amazon_order_ref = fields.Char('Amazon Order ID', required=True, index=True)
    instance_id = fields.Many2one('amazon.instance', string='Instance', required=True, ondelete='cascade')
    sale_order_id = fields.Many2one('sale.order', string='Odoo Sale Order', copy=False)
    partner_id = fields.Many2one('res.partner', string='Customer')

    # Order info
    order_status = fields.Selection([
        ('PendingAvailability', 'Pending Availability'),
        ('Pending', 'Pending'),
        ('Unshipped', 'Unshipped'),
        ('PartiallyShipped', 'Partially Shipped'),
        ('Shipped', 'Shipped'),
        ('InvoiceUnconfirmed', 'Invoice Unconfirmed'),
        ('Canceled', 'Canceled'),
        ('Unfulfillable', 'Unfulfillable'),
    ], string='Order Status', default='Pending', help="Known Amazon status mirror for legacy filters and views.")
    amazon_status = fields.Char(
        'Amazon Status',
        index=True,
        help="Raw Amazon Orders API status. This field is authoritative and can store future Amazon statuses safely.",
    )
    previous_amazon_status = fields.Char('Previous Amazon Status', readonly=True)
    status_last_synced_at = fields.Datetime('Status Last Synced At', readonly=True)
    amazon_last_update_date = fields.Datetime('Amazon Last Update Date', readonly=True)
    status_sync_error = fields.Text('Status Sync Error', readonly=True)
    status_sync_retry_count = fields.Integer('Status Sync Retry Count', default=0)
    requires_status_review = fields.Boolean('Requires Status Review', default=False)
    status_review_reason = fields.Text('Status Review Reason')
    amazon_cancellation_reason = fields.Text('Amazon Cancellation Reason')
    fulfillment_channel = fields.Selection([
        ('MFN', 'Fulfilled by Merchant (FBM)'),
        ('AFN', 'Fulfilled by Amazon (FBA)'),
    ], string='Fulfillment Channel')
    order_type = fields.Selection([
        ('StandardOrder', 'Standard Order'),
        ('LongLeadTimeOrder', 'Long Lead Time'),
        ('Preorder', 'Preorder'),
        ('BackOrder', 'Back Order'),
        ('SourcingOnDemandOrder', 'Sourcing On Demand'),
    ], string='Order Type', default='StandardOrder')

    purchase_date = fields.Datetime('Purchase Date')
    last_update_date = fields.Datetime('Last Updated')
    sales_channel = fields.Char('Sales Channel')
    is_prime = fields.Boolean('Prime Order')
    is_business_order = fields.Boolean('Business Order')

    # Amounts
    order_total = fields.Float('Order Total')
    currency_id = fields.Many2one('res.currency', string='Currency')

    # Shipping
    ship_service_level = fields.Char('Shipping Service')
    shipping_address_name = fields.Char('Ship To Name')
    shipping_address_line1 = fields.Char('Address Line 1')
    shipping_address_line2 = fields.Char('Address Line 2')
    shipping_city = fields.Char('City')
    shipping_state = fields.Char('State')
    shipping_postal_code = fields.Char('Postal Code')
    shipping_country_code = fields.Char('Country Code')
    carrier_name = fields.Char('Carrier')
    tracking_number = fields.Char('Tracking Number')
    ship_date = fields.Datetime('Ship Date')

    # Delivery details
    delivery_status = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('returned', 'Returned'),
        ('failed', 'Delivery Failed'),
    ], string='Delivery Status', compute='_compute_delivery_status', store=True)
    estimated_delivery_date = fields.Date('Estimated Delivery')
    actual_delivery_date = fields.Datetime('Actual Delivery Date')
    delivery_notes = fields.Text('Delivery Notes')

    # Customer display (computed for easy list views)
    customer_name = fields.Char('Customer Name', compute='_compute_customer_info', store=True)
    customer_email = fields.Char('Customer Email', compute='_compute_customer_info', store=True)
    customer_phone = fields.Char('Customer Phone', compute='_compute_customer_info', store=True)
    full_shipping_address = fields.Text('Full Address', compute='_compute_full_address')

    # Delivery picking link
    linked_sale_order_id = fields.Many2one(
        'sale.order',
        string='Linked Sale Order',
        related='sale_order_id',
        store=True,
        readonly=True,
    )
    picking_ids = fields.One2many(
        'stock.picking', compute='_compute_picking_ids', string='Deliveries',
    )
    picking_count = fields.Integer(compute='_compute_picking_ids')

    # AI Delivery
    ai_delivery_analysis = fields.Text('AI Delivery Analysis', readonly=True)

    # Invoice
    invoice_id = fields.Many2one('account.move', string='Invoice')
    amazon_invoice_number = fields.Char('Amazon Invoice Number')

    # Lines
    order_line_ids = fields.One2many('amazon.sale.order.line', 'order_id', string='Order Lines')
    line_count = fields.Integer(compute='_compute_line_count')

    _unique_order_instance = models.Constraint(
        'UNIQUE(amazon_order_ref, instance_id)',
        'Amazon Order ID must be unique per instance.',
    )

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.order_line_ids)

    @api.depends('order_status', 'amazon_status', 'tracking_number', 'ship_date', 'actual_delivery_date')
    def _compute_delivery_status(self):
        status_map = {
            'PendingAvailability': 'pending',
            'Pending': 'pending',
            'Unshipped': 'processing',
            'PartiallyShipped': 'shipped',
            'Shipped': 'shipped',
            'InvoiceUnconfirmed': 'shipped',
            'Canceled': 'pending',
            'Unfulfillable': 'failed',
        }
        for rec in self:
            if rec.actual_delivery_date:
                rec.delivery_status = 'delivered'
            elif (rec.amazon_status or rec.order_status) == 'Canceled':
                rec.delivery_status = 'pending'
            elif rec.tracking_number and rec.ship_date:
                rec.delivery_status = 'in_transit'
            else:
                rec.delivery_status = status_map.get(rec.amazon_status or rec.order_status, 'pending')

    @api.depends('partner_id', 'partner_id.name', 'partner_id.email', 'partner_id.phone', 'shipping_address_name')
    def _compute_customer_info(self):
        for rec in self:
            partner = rec.partner_id
            rec.customer_name = partner.name if partner else rec.shipping_address_name or ''
            rec.customer_email = partner.email if partner else ''
            rec.customer_phone = partner.phone if partner else ''

    def _compute_full_address(self):
        for rec in self:
            parts = filter(None, [
                rec.shipping_address_name,
                rec.shipping_address_line1,
                rec.shipping_address_line2,
                ', '.join(filter(None, [rec.shipping_city, rec.shipping_state, rec.shipping_postal_code])),
                rec.shipping_country_code,
            ])
            rec.full_shipping_address = '\n'.join(parts)

    def _compute_picking_ids(self):
        for rec in self:
            pickings = self.env['stock.picking'].search([
                ('amazon_order_ref', '=', rec.amazon_order_ref),
                ('amazon_instance_id', '=', rec.instance_id.id),
            ])
            rec.picking_ids = pickings
            rec.picking_count = len(pickings)

    def action_view_deliveries(self):
        """Open related delivery pickings."""
        self.ensure_one()
        pickings = self.env['stock.picking'].search([
            ('amazon_order_ref', '=', self.amazon_order_ref),
            ('amazon_instance_id', '=', self.instance_id.id),
        ])
        if len(pickings) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'stock.picking',
                'res_id': pickings.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': 'Deliveries',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pickings.ids)],
        }

    def action_mark_delivered(self):
        """Manually mark order as delivered."""
        self.ensure_one()
        self.actual_delivery_date = fields.Datetime.now()
        self.delivery_status = 'delivered'

    def action_ai_analyze_delivery(self):
        """AI analysis: shipping delay prediction, delivery issues, recommendations."""
        self.ensure_one()
        instance = self.instance_id
        if not instance or not instance.ai_api_key:
            from odoo.exceptions import UserError
            raise UserError("AI API Key is not configured on the instance.")

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # Collect product names
        products = ', '.join(
            [line.title or line.sku or 'Unknown' for line in self.order_line_ids[:5]]
        ) or 'N/A'

        prompt = """You are an e-commerce shipping and delivery analyst.

Analyze this Amazon order delivery and provide insights:

Order ID: {order_id}
Order Status: {status}
Fulfillment: {channel}
Purchase Date: {purchase_date}
Ship Date: {ship_date}
Carrier: {carrier}
Tracking: {tracking}
Shipping Service: {service}
Destination: {city}, {state}, {country}
Products: {products}
Order Total: {total} {currency}
Customer: {customer}
Estimated Delivery: {est_delivery}
Current Delivery Status: {delivery_status}

Return ONLY valid JSON:
{{
  "estimated_days_remaining": 0,
  "delay_risk": "low/medium/high",
  "delay_reason": "explanation if delay risk is high",
  "delivery_prediction": "predicted delivery date or status",
  "shipping_quality_score": 85,
  "recommendations": ["rec 1", "rec 2", "rec 3"],
  "customer_satisfaction_risk": "low/medium/high",
  "suggested_customer_action": "what to tell the customer if they ask",
  "carrier_performance_note": "note about carrier reliability"
}}""".format(
            order_id=self.amazon_order_ref,
            status=self.order_status,
            channel=self.fulfillment_channel or 'N/A',
            purchase_date=self.purchase_date or 'N/A',
            ship_date=self.ship_date or 'Not shipped yet',
            carrier=self.carrier_name or 'N/A',
            tracking=self.tracking_number or 'N/A',
            service=self.ship_service_level or 'Standard',
            city=self.shipping_city or 'N/A',
            state=self.shipping_state or 'N/A',
            country=self.shipping_country_code or 'N/A',
            products=products,
            total=self.order_total,
            currency=self.currency_id.name if self.currency_id else 'INR',
            customer=self.customer_name or 'N/A',
            est_delivery=self.estimated_delivery_date or 'Not set',
            delivery_status=self.delivery_status or 'pending',
        )

        try:
            result = AmazonAIService._call_and_parse(provider, api_key, model, prompt)
        except Exception as exc:
            from odoo.exceptions import UserError
            raise UserError("AI delivery analysis failed: %s" % exc) from exc

        # Store analysis
        analysis_parts = [
            "Delay Risk: %s" % result.get('delay_risk', 'unknown'),
            "Prediction: %s" % result.get('delivery_prediction', 'N/A'),
            "Est. Days Remaining: %s" % result.get('estimated_days_remaining', 'N/A'),
            "Quality Score: %s/100" % result.get('shipping_quality_score', 'N/A'),
            "Customer Satisfaction Risk: %s" % result.get('customer_satisfaction_risk', 'N/A'),
            "Carrier: %s" % result.get('carrier_performance_note', 'N/A'),
        ]
        recs = result.get('recommendations', [])
        if recs:
            analysis_parts.append("Recommendations:")
            for r in recs[:5]:
                analysis_parts.append("  - %s" % r)
        delay_reason = result.get('delay_reason', '')
        if delay_reason:
            analysis_parts.append("Delay Reason: %s" % delay_reason)
        suggested = result.get('suggested_customer_action', '')
        if suggested:
            analysis_parts.append("Customer Action: %s" % suggested)

        self.ai_delivery_analysis = '\n'.join(analysis_parts)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Delivery Analysis",
                "message": "Delay Risk: %s | Prediction: %s | Score: %s/100" % (
                    result.get('delay_risk', '?'),
                    result.get('delivery_prediction', '?'),
                    result.get('shipping_quality_score', '?'),
                ),
                "type": "warning" if result.get('delay_risk') == 'high' else "success",
                "sticky": result.get('delay_risk') == 'high',
            },
        }

    # ══════════════════════════════════════════════════
    # Amazon → Odoo status synchronization
    # ══════════════════════════════════════════════════

    @api.model
    def _amazon_status_to_order_status(self, amazon_status):
        """Return a safe legacy Selection value, or False for future/unknown statuses."""
        if not amazon_status:
            return False
        if amazon_status in dict(self._fields['order_status'].selection):
            return amazon_status
        _logger.warning("Unknown Amazon order status received: %s", amazon_status)
        return False

    @api.model
    def _map_amazon_status_to_odoo(self, amazon_status):
        """Central safe Amazon status policy.

        This mapping never replaces Odoo's native sale.order state with the Amazon
        status. It only describes which optional, instance-controlled workflow
        action may be considered.
        """
        policies = {
            'PendingAvailability': {
                'workflow': 'none',
                'review': False,
                'note': 'Pre-order/payment not authorized. Keep Odoo quotation untouched.',
            },
            'Pending': {
                'workflow': 'none',
                'review': False,
                'note': 'Payment not authorized. Keep Odoo quotation untouched.',
            },
            'Unshipped': {
                'workflow': 'confirm_if_enabled_unshipped',
                'review': False,
                'note': 'Ready for shipment. Confirmation is controlled by instance settings.',
            },
            'PartiallyShipped': {
                'workflow': 'confirm_if_enabled_shipped',
                'review': True,
                'note': 'Partial shipment detected. Exact line-level shipment synchronization is required before delivery completion.',
            },
            'Shipped': {
                'workflow': 'shipped_if_enabled',
                'review': False,
                'note': 'Amazon says shipped. Delivery/invoice automation remains disabled unless explicitly configured.',
            },
            'InvoiceUnconfirmed': {
                'workflow': 'accounting_review',
                'review': True,
                'note': 'Amazon invoice is unconfirmed. Accounting review is required.',
            },
            'Canceled': {
                'workflow': 'cancel_if_safe',
                'review': False,
                'note': 'Amazon canceled the order. Cancellation is applied only when safe and configured.',
            },
            'Unfulfillable': {
                'workflow': 'review',
                'review': True,
                'note': 'Amazon marked the order unfulfillable. Manual review required.',
            },
        }
        return policies.get(amazon_status, {
            'workflow': 'unknown',
            'review': True,
            'note': 'Unknown Amazon order status. Raw status was stored and no Odoo workflow action was applied.',
        })

    def _sync_amazon_status_from_payload(self, order_data, source='manual', create_chatter=True, apply_workflow=True):
        """Persist Amazon status data and apply safe configured workflow rules.

        Called by manual single-order refresh, bulk status-sync jobs, and order
        import refreshes. The method is idempotent: unchanged statuses update sync
        timestamps but do not post duplicate chatter messages.
        """
        self.ensure_one()
        order_data = order_data or {}
        amazon_order_id = order_data.get('AmazonOrderId') or self.amazon_order_ref
        if amazon_order_id and self.amazon_order_ref and amazon_order_id != self.amazon_order_ref:
            raise UserError(
                "Amazon response order ID %s does not match local order %s."
                % (amazon_order_id, self.amazon_order_ref)
            )

        new_status = order_data.get('OrderStatus') or self.amazon_status or self.order_status
        if not new_status:
            raise UserError("Amazon response does not contain an OrderStatus for %s." % self.amazon_order_ref)

        old_status = self.amazon_status or self.order_status or False
        changed = bool(old_status != new_status)
        synced_at = fields.Datetime.now()
        amazon_last_update = amazon_to_utc_naive(order_data.get('LastUpdateDate')) or self.amazon_last_update_date or self.last_update_date
        fulfillment_channel = order_data.get('FulfillmentChannel') or self.fulfillment_channel

        vals = {
            'amazon_status': new_status,
            'status_last_synced_at': synced_at,
            'status_sync_error': False,
            'status_sync_retry_count': 0,
        }
        if changed:
            vals['previous_amazon_status'] = old_status or False
            vals['requires_status_review'] = False
            vals['status_review_reason'] = False
        if amazon_last_update:
            vals['amazon_last_update_date'] = amazon_last_update
            vals['last_update_date'] = amazon_last_update
        if fulfillment_channel in ('MFN', 'AFN'):
            vals['fulfillment_channel'] = fulfillment_channel

        known_selection = self._amazon_status_to_order_status(new_status)
        if known_selection:
            vals['order_status'] = known_selection
        elif not self.order_status:
            vals['order_status'] = False

        self.write(vals)
        sale_order = self.sale_order_id
        if sale_order:
            self._write_sale_order_amazon_status(
                sale_order,
                new_status,
                old_status if changed else sale_order.previous_amazon_status,
                synced_at,
                amazon_last_update,
                fulfillment_channel,
            )
            if changed and create_chatter:
                self._post_status_change_message(sale_order, old_status, new_status, synced_at, source)

        action_taken = 'stored'
        workflow_note = ''
        if changed and apply_workflow:
            workflow = self._apply_amazon_status_workflow(old_status, new_status)
            action_taken = workflow.get('action') or action_taken
            workflow_note = workflow.get('note') or ''
        elif not changed:
            workflow_note = 'Amazon status unchanged.'

        return {
            'amazon_order_id': self.amazon_order_ref,
            'old_status': old_status,
            'new_status': new_status,
            'changed': changed,
            'action': action_taken,
            'note': workflow_note,
        }

    def _write_sale_order_amazon_status(self, sale_order, new_status, previous_status, synced_at, amazon_last_update, fulfillment_channel):
        vals = {
            'amazon_status': new_status,
            'amazon_status_last_synced_at': synced_at,
            'amazon_status_sync_error': False,
        }
        if previous_status:
            vals['previous_amazon_status'] = previous_status
        if amazon_last_update:
            vals['amazon_last_update_date'] = amazon_last_update
        if fulfillment_channel in ('MFN', 'AFN'):
            vals['amazon_fulfillment_channel'] = fulfillment_channel
        sale_order.write(vals)

    def _post_status_change_message(self, sale_order, old_status, new_status, synced_at, source):
        old_label = old_status or 'N/A'
        body = (
            "Amazon order status changed:<br/>"
            "%s → %s<br/>"
            "Amazon Order ID: %s<br/>"
            "Synced at: %s<br/>"
            "Source: %s"
        ) % (old_label, new_status, self.amazon_order_ref, synced_at, source)
        sale_order.message_post(body=body, subtype_xmlid='mail.mt_note')

    def _apply_amazon_status_workflow(self, old_status, new_status):
        self.ensure_one()
        policy = self._map_amazon_status_to_odoo(new_status)
        workflow = policy.get('workflow')
        note = policy.get('note') or ''
        sale_order = self.sale_order_id

        if policy.get('review') and workflow not in ('cancel_if_safe',):
            self._create_status_review_activity(
                "Amazon status requires review: %s" % new_status,
                note,
            )

        if not sale_order:
            return {'action': 'no_sale_order', 'note': 'No linked Odoo Sale Order. %s' % note}

        if workflow in ('none',):
            return {'action': 'skipped', 'note': note}
        if workflow == 'confirm_if_enabled_unshipped':
            return self._confirm_sale_order_if_configured(
                'auto_confirm_sale_order_on_unshipped',
                "Amazon status is Unshipped.",
            )
        if workflow == 'confirm_if_enabled_shipped':
            result = self._confirm_sale_order_if_configured(
                'auto_confirm_sale_order_on_shipped',
                "Amazon status is PartiallyShipped.",
            )
            self._create_status_review_activity(
                "Amazon partial shipment review required",
                note,
            )
            return result
        if workflow == 'shipped_if_enabled':
            result = self._confirm_sale_order_if_configured(
                'auto_confirm_sale_order_on_shipped',
                "Amazon status is Shipped.",
            )
            delivery_note = self._apply_amazon_shipped_delivery_invoice_rules()
            if delivery_note:
                result['note'] = "%s %s" % (result.get('note') or '', delivery_note)
            return result
        if workflow == 'cancel_if_safe':
            return self._apply_amazon_cancellation()
        if workflow == 'accounting_review':
            self._create_status_review_activity(
                "Amazon invoice status requires review",
                note,
            )
            return {'action': 'review_activity', 'note': note}
        if workflow == 'review':
            self._create_status_review_activity(
                "Amazon order requires review: %s" % new_status,
                note,
            )
            return {'action': 'review_activity', 'note': note}

        self._create_status_review_activity(
            "Unknown Amazon status: %s" % new_status,
            note,
        )
        return {'action': 'unknown_status_stored', 'note': note}

    def _confirm_sale_order_if_configured(self, config_field, reason):
        self.ensure_one()
        sale_order = self.sale_order_id
        enabled = bool(getattr(self.instance_id, config_field, False))
        if not enabled:
            return {'action': 'skipped', 'note': "%s Auto confirmation is disabled." % reason}
        if not sale_order:
            return {'action': 'skipped', 'note': "No linked Sale Order."}
        if sale_order.state in ('draft', 'sent'):
            sale_order.action_confirm()
            return {'action': 'sale_order_confirmed', 'note': "%s Sale Order confirmed by configuration." % reason}
        return {'action': 'skipped', 'note': "%s Sale Order state is %s." % (reason, sale_order.state)}

    def _apply_amazon_shipped_delivery_invoice_rules(self):
        self.ensure_one()
        sale_order = self.sale_order_id
        if not sale_order:
            return ''

        notes = []
        instance = self.instance_id
        if instance.auto_validate_delivery_on_shipped:
            notes.append(
                "Delivery validation was skipped because exact Amazon shipped quantities are not synchronized yet."
            )
            self._create_status_review_activity(
                "Amazon Shipped delivery validation review",
                notes[-1],
            )

        if instance.auto_create_invoice_on_shipped:
            if sale_order.state in ('draft', 'sent'):
                notes.append("Invoice creation skipped because the Sale Order is not confirmed.")
            elif sale_order.invoice_ids:
                notes.append("Invoice creation skipped because an invoice already exists.")
            else:
                invoices = sale_order._create_invoices()
                if invoices:
                    self.invoice_id = invoices[0].id
                    notes.append("Invoice created by explicit Amazon status configuration.")
                    if instance.auto_post_invoice:
                        invoices.action_post()
                        notes.append("Invoice posted by explicit Amazon status configuration.")
        return " ".join(notes)

    def _apply_amazon_cancellation(self):
        self.ensure_one()
        sale_order = self.sale_order_id
        if not sale_order:
            self._create_status_review_activity(
                "Amazon cancellation without linked Sale Order",
                "Amazon order %s is canceled but no Odoo Sale Order is linked." % self.amazon_order_ref,
            )
            return {'action': 'review_activity', 'note': 'No linked Sale Order.'}

        completed_pickings = sale_order.picking_ids.filtered(lambda picking: picking.state == 'done')
        invoices = sale_order.invoice_ids
        posted_invoices = invoices.filtered(lambda move: move.state == 'posted')
        paid_invoices = invoices.filtered(lambda move: move.payment_state in ('paid', 'in_payment', 'partial'))
        unsafe_reason = False
        if completed_pickings:
            unsafe_reason = "completed delivery exists"
        elif posted_invoices:
            unsafe_reason = "posted invoice exists"
        elif paid_invoices:
            unsafe_reason = "paid/in-payment invoice exists"

        if sale_order.state in ('draft', 'sent'):
            if self.instance_id.auto_cancel_draft_sale_order_on_amazon_cancellation:
                sale_order.action_cancel()
                return {'action': 'draft_sale_order_canceled', 'note': 'Draft quotation canceled by configuration.'}
            note = "Amazon canceled the order, but draft quotation auto-cancel is disabled."
            self._create_status_review_activity("Amazon cancellation review", note)
            return {'action': 'review_activity', 'note': note}

        if sale_order.state in ('sale', 'done'):
            if unsafe_reason:
                note = "Amazon canceled the order, but automatic cancellation is unsafe: %s." % unsafe_reason
                self._create_status_review_activity("Amazon cancellation conflict", note)
                return {'action': 'review_activity', 'note': note}
            if self.instance_id.auto_cancel_confirmed_sale_order_when_safe:
                sale_order.action_cancel()
                return {'action': 'confirmed_sale_order_canceled', 'note': 'Confirmed Sale Order canceled safely by configuration.'}
            note = "Amazon canceled the order; confirmed Sale Order auto-cancel is disabled."
            self._create_status_review_activity("Amazon cancellation review", note)
            return {'action': 'review_activity', 'note': note}

        if sale_order.state == 'cancel':
            return {'action': 'skipped', 'note': 'Sale Order was already canceled.'}

        note = "Amazon cancellation requires review for Sale Order state %s." % sale_order.state
        self._create_status_review_activity("Amazon cancellation review", note)
        return {'action': 'review_activity', 'note': note}

    def _create_status_review_activity(self, summary, note):
        self.ensure_one()
        self.write({
            'requires_status_review': True,
            'status_review_reason': note,
        })
        sale_order = self.sale_order_id
        if not sale_order:
            return
        if not self.instance_id.create_activity_on_status_conflict:
            sale_order.message_post(body="%s<br/>%s" % (summary, note), subtype_xmlid='mail.mt_note')
            return
        try:
            activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
            sale_order.activity_schedule(
                activity_type_id=activity_type.id if activity_type else False,
                summary=summary,
                note=note,
                user_id=self.env.user.id,
            )
        except Exception as exc:
            _logger.warning("Could not create Amazon status review activity for %s: %s", self.amazon_order_ref, exc)
            sale_order.message_post(body="%s<br/>%s" % (summary, note), subtype_xmlid='mail.mt_note')

    def action_refresh_status_from_amazon(self):
        """Fetch and apply this order's current Amazon status."""
        self.ensure_one()
        if not self.instance_id:
            raise UserError("Amazon instance is required.")
        if not self.amazon_order_ref:
            raise UserError("Amazon Order ID is required.")
        self.instance_id._auto_fix_region()
        self.instance_id._check_required_fields()
        access_token = self.instance_id._get_access_token_or_raise()
        data = AmazonAPI().get_order(self.instance_id, access_token, self.amazon_order_ref)
        result = self._sync_amazon_status_from_payload(
            data.get('payload', {}) or {},
            source='manual',
            create_chatter=True,
            apply_workflow=True,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Amazon Status Refreshed",
                "message": "%s: %s → %s" % (
                    self.amazon_order_ref,
                    result.get('old_status') or 'N/A',
                    result.get('new_status') or 'N/A',
                ) if result.get('changed') else "%s unchanged: %s" % (
                    self.amazon_order_ref,
                    result.get('new_status') or 'N/A',
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_create_sale_order(self):
        """Create an Odoo sale.order from this Amazon order."""
        self.ensure_one()
        if self.sale_order_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': self.sale_order_id.id,
                'view_mode': 'form',
            }

        existing_sale_order = self.env['sale.order'].search([
            ('amazon_order_id', '=', self.id),
        ], limit=1)
        if not existing_sale_order and self.amazon_order_ref and self.instance_id:
            existing_sale_order = self.env['sale.order'].search([
                ('client_order_ref', '=', self.amazon_order_ref),
                ('amazon_instance_id', '=', self.instance_id.id),
            ], limit=1)
        if existing_sale_order:
            self.sale_order_id = existing_sale_order.id
            if existing_sale_order.partner_id:
                self.partner_id = existing_sale_order.partner_id.id
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': existing_sale_order.id,
                'view_mode': 'form',
            }

        if not self.order_line_ids:
            raise UserError("Cannot create an Odoo sale order without imported Amazon order lines.")

        missing_product_lines = self.order_line_ids.filtered(lambda line: not line.odoo_product_id)
        if missing_product_lines:
            missing_refs = []
            for line in missing_product_lines[:10]:
                missing_refs.append(line.sku or line.asin or line.title or line.amazon_order_item_id or 'Unknown item')
            extra = len(missing_product_lines) - len(missing_refs)
            if extra > 0:
                missing_refs.append("+%d more" % extra)
            raise UserError(
                "Cannot create Odoo sale order for Amazon order %s. "
                "Missing Odoo product mapping for: %s"
                % (self.amazon_order_ref, ", ".join(missing_refs))
            )

        partner = self._get_or_create_partner()
        order_vals = {
            'partner_id': partner.id,
            'origin': self.amazon_order_ref,
            'client_order_ref': self.amazon_order_ref,
            'date_order': self.purchase_date or fields.Datetime.now(),
            # Link Amazon info to Odoo SO
            'amazon_order_id': self.id,
            'is_amazon_order': True,
            'amazon_instance_id': self.instance_id.id,
            'amazon_fulfillment_channel': self.fulfillment_channel,
            'amazon_status': self.amazon_status or self.order_status,
            'previous_amazon_status': self.previous_amazon_status,
            'amazon_status_last_synced_at': self.status_last_synced_at,
            'amazon_last_update_date': self.amazon_last_update_date or self.last_update_date,
        }
        if self.instance_id.company_id:
            order_vals['company_id'] = self.instance_id.company_id.id
        warehouse = self.instance_id.fba_warehouse_id if self.fulfillment_channel == 'AFN' else self.instance_id.fbm_warehouse_id
        if warehouse and 'warehouse_id' in self.env['sale.order']._fields:
            order_vals['warehouse_id'] = warehouse.id
        if self.currency_id:
            order_vals['currency_id'] = self.currency_id.id

        lines = []
        for line in self.order_line_ids:
            product = line.odoo_product_id
            lines.append((0, 0, {
                'product_id': product.id,
                'name': line.title or line.sku or 'Amazon Product',
                'product_uom_qty': line.quantity,
                'price_unit': line.item_price / line.quantity if line.quantity else line.item_price,
            }))
        order_vals['order_line'] = lines

        sale_order = self.env['sale.order'].create(order_vals)
        self.sale_order_id = sale_order.id
        self.partner_id = partner.id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': sale_order.id,
            'view_mode': 'form',
        }

    def _get_or_create_partner(self):
        """Find or create a res.partner for the Amazon buyer."""
        name = self.shipping_address_name or 'Amazon Customer'
        partner = self.env['res.partner'].search([('name', '=', name)], limit=1)
        if not partner:
            vals = {'name': name}
            if self.shipping_address_line1:
                vals['street'] = self.shipping_address_line1
            if self.shipping_address_line2:
                vals['street2'] = self.shipping_address_line2
            if self.shipping_city:
                vals['city'] = self.shipping_city
            if self.shipping_postal_code:
                vals['zip'] = self.shipping_postal_code
            if self.shipping_country_code:
                country = self.env['res.country'].search([('code', '=', self.shipping_country_code)], limit=1)
                if country:
                    vals['country_id'] = country.id
                if self.shipping_state and country:
                    state = self.env['res.country.state'].search([
                        ('country_id', '=', country.id),
                        '|', ('code', '=', self.shipping_state), ('name', '=', self.shipping_state),
                    ], limit=1)
                    if state:
                        vals['state_id'] = state.id
            partner = self.env['res.partner'].create(vals)
        return partner

    def action_confirm_shipment(self):
        """Export shipping confirmation to Amazon."""
        self.ensure_one()
        if not self.tracking_number:
            raise UserError("Please enter a tracking number before confirming shipment.")
        if self.fulfillment_channel == 'AFN':
            raise UserError("FBA orders are fulfilled by Amazon. Cannot confirm shipment.")
        self.instance_id._confirm_order_shipment(self)

    def action_cancel_order(self):
        """Cancel this Amazon order."""
        self.ensure_one()
        if self.order_status == 'Canceled':
            raise UserError("Order is already canceled.")
        self.instance_id._cancel_amazon_order(self)

    def action_create_invoice(self):
        """Create an invoice for this order."""
        self.ensure_one()
        if self.invoice_id:
            raise UserError("Invoice already exists: %s" % self.invoice_id.name)
        if not self.sale_order_id:
            raise UserError("Create the Odoo sale order first.")

        so = self.sale_order_id
        if so.state == 'draft':
            so.action_confirm()

        invoice = so._create_invoices()
        if invoice:
            self.invoice_id = invoice[0].id if isinstance(invoice, models.Model) else invoice.id
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.invoice_id.id,
            'view_mode': 'form',
        }

    def action_upload_invoice_to_amazon(self):
        """Upload invoice to Amazon (VCS)."""
        self.ensure_one()
        if not self.invoice_id:
            raise UserError("No invoice to upload.")
        self.instance_id._upload_invoice_to_amazon(self)

    # ══════════════════════════════════════════════════
    # AI Customer Support Auto-Reply
    # ══════════════════════════════════════════════════

    buyer_message = fields.Text('Buyer Message', help='Paste the buyer message here to generate an AI reply.')
    ai_reply = fields.Text('AI Generated Reply', readonly=True)

    def action_ai_generate_reply(self):
        """Generate an AI reply for a buyer message."""
        self.ensure_one()
        if not self.buyer_message or len(self.buyer_message.strip()) < 5:
            raise UserError("Paste the buyer's message first in the 'Buyer Message' field.")

        instance = self.instance_id
        if not instance or not instance.ai_api_key:
            raise UserError(
                "AI API Key is not configured.\n"
                "Go to Amazon > Configuration > Instances > AI Auto-Fill section."
            )

        provider = instance.ai_provider or 'groq'
        api_key = instance.ai_api_key
        model = instance.ai_model

        # Get product name from first line
        product_name = ''
        if self.order_line_ids:
            product_name = self.order_line_ids[0].title or self.order_line_ids[0].sku or ''

        try:
            result = AmazonAIService.generate_customer_reply(
                provider, api_key, model,
                buyer_message=self.buyer_message,
                product_name=product_name,
                order_status=self.order_status or '',
                seller_name=instance.name or '',
            )
        except Exception as exc:
            raise UserError("AI reply generation failed: %s" % exc) from exc

        reply_text = result.get('reply', '')
        sentiment = result.get('sentiment', '')
        category = result.get('category', '')
        escalation = result.get('escalation_needed', False)

        if reply_text:
            self.ai_reply = reply_text

        msg_parts = ["Sentiment: %s | Category: %s" % (sentiment, category)]
        if escalation:
            msg_parts.append("ESCALATION RECOMMENDED")
        actions = result.get('suggested_actions', [])
        if actions:
            msg_parts.append("Suggested: %s" % "; ".join(actions[:3]))

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Customer Reply",
                "message": " | ".join(msg_parts),
                "type": "warning" if escalation else "success",
                "sticky": escalation,
            },
        }


class AmazonSaleOrderLine(models.Model):
    _name = 'amazon.sale.order.line'
    _description = 'Amazon Sale Order Line'

    order_id = fields.Many2one('amazon.sale.order', string='Order', required=True, ondelete='cascade')
    amazon_order_item_id = fields.Char('Order Item ID')
    amazon_product_id = fields.Many2one('amazon.product', string='Amazon Product')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    sku = fields.Char('SKU')
    asin = fields.Char('ASIN')
    title = fields.Char('Title')
    quantity = fields.Float('Quantity', default=1)
    item_price = fields.Float('Item Price')
    item_tax = fields.Float('Item Tax')
    shipping_price = fields.Float('Shipping Price')
    shipping_tax = fields.Float('Shipping Tax')
    gift_wrap_price = fields.Float('Gift Wrap Price')
    gift_wrap_tax = fields.Float('Gift Wrap Tax')
    promotion_discount = fields.Float('Promotion Discount')
    line_total = fields.Float('Line Total', compute='_compute_line_total', store=True)

    @api.depends('item_price', 'item_tax', 'shipping_price', 'promotion_discount')
    def _compute_line_total(self):
        for line in self:
            line.line_total = line.item_price + line.item_tax + line.shipping_price - line.promotion_discount
