import logging
from odoo import models, fields, api
from odoo.exceptions import UserError

from ..services.ai_service import AmazonAIService

_logger = logging.getLogger(__name__)


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
        ('Pending', 'Pending'),
        ('Unshipped', 'Unshipped'),
        ('PartiallyShipped', 'Partially Shipped'),
        ('Shipped', 'Shipped'),
        ('Canceled', 'Canceled'),
        ('Unfulfillable', 'Unfulfillable'),
    ], string='Order Status', default='Pending')
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

    _sql_constraints = [
        ('unique_order_instance', 'unique(amazon_order_ref, instance_id)', 'Amazon Order ID must be unique per instance.'),
    ]

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.order_line_ids)

    @api.depends('order_status', 'tracking_number', 'ship_date', 'actual_delivery_date')
    def _compute_delivery_status(self):
        status_map = {
            'Pending': 'pending',
            'Unshipped': 'processing',
            'PartiallyShipped': 'shipped',
            'Shipped': 'shipped',
            'Canceled': 'pending',
            'Unfulfillable': 'failed',
        }
        for rec in self:
            if rec.actual_delivery_date:
                rec.delivery_status = 'delivered'
            elif rec.order_status == 'Canceled':
                rec.delivery_status = 'pending'
            elif rec.tracking_number and rec.ship_date:
                rec.delivery_status = 'in_transit'
            else:
                rec.delivery_status = status_map.get(rec.order_status, 'pending')

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

    def action_create_sale_order(self):
        """Create an Odoo sale.order from this Amazon order."""
        self.ensure_one()
        if self.sale_order_id:
            raise UserError("Odoo sale order already exists: %s" % self.sale_order_id.name)

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
        }
        if self.currency_id:
            order_vals['currency_id'] = self.currency_id.id

        lines = []
        for line in self.order_line_ids:
            product = line.odoo_product_id or self.env.ref('product.product_product_1', raise_if_not_found=False)
            lines.append((0, 0, {
                'product_id': product.id if product else False,
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
