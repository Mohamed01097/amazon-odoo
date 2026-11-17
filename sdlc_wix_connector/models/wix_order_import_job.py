"""Durable queue applying verified Wix order webhooks to Odoo.

The webhook controller only stores the raw signed payload and returns; all
business work happens here, driven by cron, so a slow or failing import can
never delay or lose an inbound Wix delivery.

Three order events are handled, and Wix wraps the order differently in each
(verified against dev.wix.com):

====================  ==========================================
``created``           ``createdEvent.entity``
``updated``           ``updatedEvent.currentEntity``
``canceled``          ``actionEvent.body.order``
====================  ==========================================

Payload parsing is deliberately defensive: every field is read through
``.get()`` chains because the exact Wix order schema is only verified against
the published documentation, not yet against live traffic from this client's
store.
"""

import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .wix_api import extract_event_payload

_logger = logging.getLogger(__name__)

MAX_RETRY_COUNT = 5
RETRY_BACKOFF_BASE_MINUTES = 2


class WixOrderImportJob(models.Model):
    _name = 'wix.order.import.job'
    _description = 'Wix Order Import Job'
    _order = 'create_date desc, id desc'

    instance_id = fields.Many2one(
        'wix.instance', string='Connection', required=True, index=True, ondelete='cascade',
    )
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)

    state = fields.Selection([
        ('draft', 'Queued'),
        ('done', 'Done'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], default='draft', required=True, index=True)

    event_slug = fields.Selection([
        ('created', 'Order Created'),
        ('updated', 'Order Updated'),
        ('canceled', 'Order Canceled'),
    ], default='created', required=True, index=True)

    payload_json = fields.Text(
        string='Raw Payload', required=True,
        help="Verified Wix JWT claim set, stored verbatim so an import can be "
             "replayed or audited after a schema correction.",
    )
    wix_order_id = fields.Char(
        string='Wix Order ID', index=True,
        help="Wix's own order identifier, used to find the order this event "
             "belongs to.",
    )
    wix_event_id = fields.Char(
        string='Wix Event ID', index=True,
        help="Wix's per-event identifier. Wix documents it as the value to "
             "deduplicate on, so it is what makes a redelivery a no-op.",
    )

    sale_order_id = fields.Many2one('sale.order', string='Sale Order', readonly=True)
    sync_log_id = fields.Many2one('wix.sync.log', string='Sync Log', readonly=True)

    next_run_at = fields.Datetime(index=True)
    retry_count = fields.Integer(default=0)
    last_error_message = fields.Text(readonly=True)

    # Keyed on the event, not the order: one order legitimately produces a
    # created event, then updates, then possibly a cancellation.
    _wix_event_unique_per_instance = models.Constraint(
        'UNIQUE (instance_id, wix_event_id)',
        'This Wix event has already been recorded for this connection.',
    )

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    @api.model
    def cron_process_order_import_jobs(self, limit=20):
        """Claim and process queued jobs.

        ``FOR UPDATE SKIP LOCKED`` lets several cron workers drain the queue
        concurrently without two of them taking the same row. The cron
        transaction is the batch transaction; no explicit commit is used.
        """
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id
              FROM wix_order_import_job
             WHERE state = 'draft'
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY COALESCE(next_run_at, create_date), id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
        """, [now, limit])
        job_ids = [row[0] for row in self.env.cr.fetchall()]
        if not job_ids:
            return False

        for job in self.browse(job_ids):
            # One savepoint per job: a single bad payload must not roll back
            # the whole batch.
            try:
                with self.env.cr.savepoint():
                    job._process_job()
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                _logger.exception("Wix order import job %s failed", job.id)
                job._mark_failed(str(exc))
        return True

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process_job(self):
        self.ensure_one()
        instance = self.instance_id

        if self.event_slug == 'created':
            if not instance.order_import_enabled:
                self._defer(_("Order import is disabled for this connection."))
                return
        elif not instance.order_status_sync_enabled:
            self._defer(_("Order status sync is disabled for this connection."))
            return

        log = self.env['wix.sync.log'].log_start(
            instance, 'order_import', source=self,
        )
        self.sudo().write({'sync_log_id': log.id})

        data = self._extract_order()
        if not data.get('wix_order_id'):
            raise ValueError(_("Could not find an order id in the Wix payload."))

        if self.event_slug == 'created':
            self._apply_created(data, log)
        else:
            self._apply_status_event(data, log)

    # -- created ------------------------------------------------------

    def _apply_created(self, data, log):
        self.ensure_one()
        existing = self._find_sale_order(data['wix_order_id'])
        if existing:
            # A redelivery, or an event that raced ahead of this one.
            self.sudo().write({
                'state': 'done',
                'sale_order_id': existing.id,
                'next_run_at': False,
            })
            log.log_success(_("Order already imported as %s.", existing.name))
            return

        order = self._create_sale_order(data)
        self._write_status_fields(order, data)
        self.sudo().write({
            'state': 'done',
            'sale_order_id': order.id,
            'next_run_at': False,
            'last_error_message': False,
        })
        log.log_success(_("Created sale order %s.", order.name))

    # -- updated / canceled -------------------------------------------

    def _apply_status_event(self, data, log):
        """Refresh the linked order's Wix status, and act on a cancellation.

        A status event can arrive before the order has been imported, so a
        missing order is deferred rather than treated as an error.
        """
        self.ensure_one()
        order = self._find_sale_order(data['wix_order_id'])
        if not order:
            self._defer(_(
                "No Odoo sale order exists yet for Wix order %s.",
                data['wix_order_id'],
            ))
            return

        self._write_status_fields(order, data)
        summary = _(
            "Wix %(event)s: status %(status)s, payment %(payment)s.",
            event=self.event_slug,
            status=data.get('status') or '-',
            payment=data.get('payment_status') or '-',
        )
        order.message_post(body=summary)

        if self._is_cancellation(data):
            self._handle_cancellation(order, data)

        self.sudo().write({
            'state': 'done',
            'sale_order_id': order.id,
            'next_run_at': False,
            'last_error_message': False,
        })
        log.log_success(summary)

    def _is_cancellation(self, data):
        self.ensure_one()
        return (
            self.event_slug == 'canceled'
            or (data.get('status') or '').upper() == 'CANCELED'
        )

    def _handle_cancellation(self, order, data):
        """Mirror a Wix cancellation onto the Odoo order, but never destructively.

        An order that has already shipped or been invoiced is flagged for a
        human instead of being cancelled: cancelling it here would contradict
        stock that has physically left and accounting that is already posted.
        """
        self.ensure_one()
        order.sudo().write({'wix_review_needed': True})

        if order.state == 'cancel':
            return

        blockers = self._cancellation_blockers(order)
        policy = self.instance_id.order_cancel_policy

        if blockers:
            order.message_post(body=_(
                "Wix cancelled this order, but it was not cancelled in Odoo "
                "because %(reasons)s. Please review.",
                reasons='; '.join(blockers),
            ))
            return

        if policy != 'cancel_when_safe':
            order.message_post(body=_(
                "Wix cancelled this order. It was left unchanged because the "
                "connection is set to flag cancellations for review only."
            ))
            return

        order.sudo()._action_cancel()
        order.message_post(body=_(
            "Cancelled in Odoo because Wix reported the order as cancelled. "
            "No delivery was validated and no invoice was posted."
        ))

    @staticmethod
    def _cancellation_blockers(order):
        """Reasons this order must not be cancelled automatically."""
        blockers = []
        if any(picking.state == 'done' for picking in order.picking_ids):
            blockers.append(_("a delivery is already validated"))
        posted = order.invoice_ids.filtered(lambda move: move.state == 'posted')
        if posted:
            blockers.append(_("an invoice is already posted"))
        return blockers

    # -- shared -------------------------------------------------------

    def _find_sale_order(self, wix_order_id):
        """Locate the Odoo order for a Wix order id."""
        self.ensure_one()
        return self.env['sale.order'].sudo().search([
            ('wix_instance_id', '=', self.instance_id.id),
            ('wix_order_id', '=', wix_order_id),
        ], limit=1)

    def _write_status_fields(self, order, data):
        """Store what Wix reported. These fields never drive accounting."""
        self.ensure_one()
        vals = {
            'wix_instance_id': self.instance_id.id,
            'wix_order_id': data.get('wix_order_id'),
            'wix_last_event_at': fields.Datetime.now(),
        }
        for field_name, key in (
            ('wix_order_number', 'wix_order_number'),
            ('wix_order_status', 'status'),
            ('wix_payment_status', 'payment_status'),
            ('wix_fulfillment_status', 'fulfillment_status'),
        ):
            value = data.get(key)
            if value:
                vals[field_name] = str(value)
        order.sudo().write(vals)

    def _extract_order(self):
        """Pull the fields needed for a sale order out of the raw payload."""
        self.ensure_one()
        try:
            claims = json.loads(self.payload_json or '{}')
        except ValueError as exc:
            raise ValueError(_("Stored Wix payload is not valid JSON.")) from exc

        event = extract_event_payload(claims)
        entity = self._extract_entity(event)

        buyer = entity.get('buyerInfo') or {}
        recipient = entity.get('recipientInfo') or entity.get('shippingInfo') or {}
        contact = (recipient.get('contactDetails')
                   or (recipient.get('logistics') or {}).get('shippingDestination', {}).get('contactDetails')
                   or {})

        price_summary = entity.get('priceSummary') or {}
        shipping_info = entity.get('shippingInfo') or {}
        shipping_cost = shipping_info.get('cost') or {}

        return {
            'wix_order_id': entity.get('id') or event.get('entityId'),
            'wix_order_number': entity.get('number'),
            'status': entity.get('status'),
            'payment_status': entity.get('paymentStatus'),
            'fulfillment_status': entity.get('fulfillmentStatus'),
            'email': buyer.get('email'),
            'first_name': contact.get('firstName'),
            'last_name': contact.get('lastName'),
            'phone': contact.get('phone'),
            'currency': entity.get('currency'),
            'line_items': entity.get('lineItems') or [],
            # Wix documents priceSummary as the authoritative set of order
            # totals, and `discount` as the sum of every discount applied.
            'shipping_amount': self._amount(
                shipping_cost.get('price') or price_summary.get('shipping')
            ),
            'shipping_title': shipping_info.get('title'),
            'discount_amount': self._amount(price_summary.get('discount')),
            'additional_fees': entity.get('additionalFees') or [],
            'reported_total': self._amount(price_summary.get('total')),
            'tax_included_in_prices': bool(entity.get('taxIncludedInPrices')),
        }

    @staticmethod
    def _amount(price):
        """Read a Wix ``Price`` object, which carries its amount as a string."""
        if not isinstance(price, dict):
            return 0.0
        try:
            return float(price.get('amount') or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _extract_entity(event):
        """Pull the order object out of whichever envelope Wix used.

        Each event type nests the order differently, so all three documented
        shapes are tried before the generic fallbacks.
        """
        data = event.get('data') or {}
        if not isinstance(data, dict):
            return {}

        candidates = (
            (data.get('createdEvent') or {}).get('entity'),
            (data.get('updatedEvent') or {}).get('currentEntity'),
            ((data.get('actionEvent') or {}).get('body') or {}).get('order'),
            data.get('entity'),
            data,
        )
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('id'):
                return candidate
        return {}

    def _resolve_partner(self, data):
        """Find or create the customer for this order."""
        self.ensure_one()
        Partner = self.env['res.partner']
        email = (data.get('email') or '').strip()
        name = ' '.join(
            part for part in (data.get('first_name'), data.get('last_name')) if part
        ).strip()

        if email:
            partner = Partner.search([('email', '=ilike', email)], limit=1)
            if partner:
                return partner
        if not name and not email:
            raise ValueError(_("The Wix payload has neither a buyer email nor a name."))

        return Partner.create({
            'name': name or email,
            'email': email or False,
            'phone': (data.get('phone') or '').strip() or False,
            'company_id': False,
        })

    def _resolve_product(self, line):
        """Map one Wix line item to an Odoo product via its SKU."""
        self.ensure_one()
        physical = line.get('physicalProperties') or {}
        sku = (physical.get('sku') or line.get('sku') or '').strip()
        if not sku:
            raise ValueError(_(
                "A Wix line item has no SKU, so it cannot be mapped to an Odoo product."
            ))

        products = self.env['product.product'].search([('default_code', '=', sku)])
        if not products:
            raise ValueError(_(
                "No Odoo product has the internal reference %(sku)s used by this "
                "Wix order. Create or correct the product mapping, then retry.",
                sku=sku,
            ))
        if len(products) > 1:
            raise ValueError(_(
                "The internal reference %(sku)s matches %(count)s Odoo products. "
                "Duplicate references must be resolved before import.",
                sku=sku, count=len(products),
            ))
        return products

    def _build_order_line_vals(self, line):
        product = self._resolve_product(line)
        quantity = line.get('quantity')
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            quantity = 0.0
        if quantity <= 0:
            raise ValueError(_("A Wix line item has a non-positive quantity."))

        price = ((line.get('price') or {}).get('amount')
                 or (line.get('priceBeforeDiscounts') or {}).get('amount'))
        try:
            price_unit = float(price)
        except (TypeError, ValueError):
            raise ValueError(_(
                "Could not read a unit price for Wix line item %(sku)s.",
                sku=product.default_code,
            )) from None

        return {
            'product_id': product.id,
            'product_uom_qty': quantity,
            'price_unit': price_unit,
            'tax_ids': self._line_taxes(),
        }

    def _line_taxes(self):
        """Taxes to put on an imported line.

        Odoo would otherwise apply each product's own default tax on top of
        the price Wix already quoted, which makes the Odoo total disagree with
        what the customer was charged. The tax is therefore explicit: the one
        configured on the connection, or none at all.
        """
        self.ensure_one()
        tax = self.instance_id.sales_tax_id
        return [(6, 0, tax.ids if tax else [])]

    def _build_shipping_line(self, data):
        """Shipping Wix charged the customer, as its own service line."""
        self.ensure_one()
        amount = data.get('shipping_amount') or 0.0
        if not amount:
            return None
        product = self.instance_id._get_shipping_product()
        return {
            'product_id': product.id,
            'name': data.get('shipping_title') or product.name,
            'product_uom_qty': 1,
            'price_unit': amount,
            'tax_ids': self._line_taxes(),
        }

    def _build_discount_line(self, data):
        """Order-level discount as a single negative line.

        Wix states that ``priceSummary.discount`` is the sum of every discount
        on the order, so it is taken whole rather than reconstructed from the
        individual discount records.
        """
        self.ensure_one()
        amount = data.get('discount_amount') or 0.0
        if not amount:
            return None
        product = self.instance_id._get_discount_product()
        return {
            'product_id': product.id,
            'name': _("Wix discount"),
            'product_uom_qty': 1,
            'price_unit': -abs(amount),
            'tax_ids': self._line_taxes(),
        }

    def _build_fee_lines(self, data):
        """Additional fees Wix applied, one line each."""
        self.ensure_one()
        lines = []
        product = False
        for fee in data.get('additional_fees') or []:
            if not isinstance(fee, dict):
                continue
            amount = self._amount(fee.get('price'))
            if not amount:
                continue
            product = product or self.instance_id._get_fee_product()
            lines.append({
                'product_id': product.id,
                'name': fee.get('name') or _("Wix fee"),
                'product_uom_qty': 1,
                'price_unit': amount,
                'tax_ids': self._line_taxes(),
            })
        return lines

    def _create_sale_order(self, data):
        """Create and confirm the sale order for a Wix order.

        Confirmation is the standard ``action_confirm()``: Odoo creates the
        delivery from the order's own warehouse, and invoicing and payment
        follow the normal sale flow. Nothing here touches stock directly.
        """
        self.ensure_one()
        instance = self.instance_id
        if not data.get('line_items'):
            raise ValueError(_("The Wix order has no line items."))

        partner = self._resolve_partner(data)
        line_vals = [self._build_order_line_vals(line) for line in data['line_items']]

        # Shipping, discount and fees are part of what the customer paid, so
        # they belong on the order; without them the Odoo total is wrong and
        # so is any cash-on-delivery amount derived from it.
        shipping_line = self._build_shipping_line(data)
        if shipping_line:
            line_vals.append(shipping_line)
        discount_line = self._build_discount_line(data)
        if discount_line:
            line_vals.append(discount_line)
        line_vals.extend(self._build_fee_lines(data))

        order_vals = {
            'partner_id': partner.id,
            'company_id': instance.company_id.id,
            'origin': _("Wix %s", data.get('wix_order_number') or data['wix_order_id']),
            'order_line': [(0, 0, vals) for vals in line_vals],
            'wix_instance_id': instance.id,
            'wix_order_id': data['wix_order_id'],
        }
        if instance.default_warehouse_id:
            order_vals['warehouse_id'] = instance.default_warehouse_id.id
        if instance.default_pricelist_id:
            order_vals['pricelist_id'] = instance.default_pricelist_id.id
        if instance.default_fiscal_position_id:
            order_vals['fiscal_position_id'] = instance.default_fiscal_position_id.id
        if instance.default_sales_team_id:
            order_vals['team_id'] = instance.default_sales_team_id.id

        order = self.env['sale.order'].create(order_vals)
        self._verify_total(order, data)
        order.action_confirm()
        return order

    def _verify_total(self, order, data):
        """Compare the Odoo total against the total Wix reported.

        A mismatch means the money in Odoo does not match what the customer
        was charged, which would then propagate into invoicing and into the
        cash-on-delivery amount a courier collects. It is therefore never
        passed over silently.
        """
        self.ensure_one()
        reported = data.get('reported_total') or 0.0
        if not reported:
            # Nothing to compare against; say so rather than implying a match.
            order.sudo().write({'wix_review_needed': True})
            order.message_post(body=_(
                "Wix sent no order total, so the Odoo total could not be "
                "verified against it. Please check the amounts."
            ))
            return

        instance = self.instance_id
        difference = order.amount_total - reported
        if abs(difference) <= (instance.total_mismatch_tolerance or 0.0):
            return

        message = _(
            "The Odoo total (%(odoo)s) does not match the total Wix reported "
            "(%(wix)s); difference %(diff)s. This usually means a tax is "
            "applied in Odoo that Wix did not charge, or a discount or fee "
            "was not mapped.",
            odoo=round(order.amount_total, 2),
            wix=round(reported, 2),
            diff=round(difference, 2),
        )
        if instance.total_mismatch_policy == 'fail':
            raise ValueError(message)

        order.sudo().write({'wix_review_needed': True})
        order.message_post(body=message)

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _defer(self, reason):
        """Leave the job queued and try again later, without consuming a retry."""
        self.ensure_one()
        self.sudo().write({
            'next_run_at': fields.Datetime.now() + timedelta(minutes=15),
            'last_error_message': reason,
        })

    def _mark_failed(self, error_message):
        """Record a failure, backing off until the bounded retry cap is hit."""
        self.ensure_one()
        retry_count = self.retry_count + 1
        vals = {'retry_count': retry_count, 'last_error_message': error_message}
        if retry_count >= MAX_RETRY_COUNT:
            vals.update(state='failed', next_run_at=False)
        else:
            delay = RETRY_BACKOFF_BASE_MINUTES ** retry_count
            vals['next_run_at'] = fields.Datetime.now() + timedelta(minutes=delay)
        self.sudo().write(vals)

        if self.sync_log_id:
            self.sync_log_id.log_fail(error_message, error_category='payload')

    def action_retry(self):
        """Re-queue a failed job for one more attempt."""
        for job in self:
            if job.state != 'failed':
                continue
            job.sudo().write({
                'state': 'draft',
                'retry_count': 0,
                'next_run_at': False,
                'last_error_message': False,
            })
        return True
