"""Tests that the Odoo order total matches what Wix charged the customer."""

import json

from odoo.tests.common import TransactionCase, tagged

from .test_wix_jwt import generate_keypair


def claims_with_totals(order_id='tot-order-1', sku='WIX-TOT-1',
                       qty=2, unit='100.00',
                       shipping=None, discount=None, fees=None, total=None,
                       event_id='tot-evt-1'):
    """Build a created-order payload with a full priceSummary."""
    def price(value):
        return {'amount': str(value)} if value is not None else None

    entity = {
        'id': order_id,
        'number': 3001,
        'currency': 'EGP',
        'status': 'APPROVED',
        'paymentStatus': 'PAID',
        'taxIncludedInPrices': True,
        'buyerInfo': {'email': 'tot.buyer@example.com'},
        'recipientInfo': {'contactDetails': {
            'firstName': 'Tot', 'lastName': 'Buyer', 'phone': '+201333333333',
        }},
        'lineItems': [{
            'quantity': qty,
            'physicalProperties': {'sku': sku},
            'price': {'amount': unit},
        }],
        'priceSummary': {
            'subtotal': price(float(unit) * qty),
            'shipping': price(shipping),
            'discount': price(discount),
            'total': price(total),
        },
    }
    if shipping is not None:
        entity['shippingInfo'] = {
            'title': 'Standard',
            'cost': {'price': {'amount': str(shipping)}},
        }
    if fees:
        entity['additionalFees'] = [
            {'name': name, 'price': {'amount': str(amount)}}
            for name, amount in fees
        ]

    return {
        'data': json.dumps({
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': 'created',
            'id': event_id,
            'data': json.dumps({
                'entityId': order_id,
                'createdEvent': {'entity': entity},
            }),
        }),
    }


@tagged('post_install', '-at_install')
class TestWixOrderTotals(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, public_pem = generate_keypair()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['wix.instance'].create({
            'name': 'Wix Totals Store',
            'webhook_public_key': public_pem,
            'order_import_enabled': True,
            'default_warehouse_id': cls.warehouse.id,
        })
        # A product carrying its own tax, to prove the importer does not let
        # that tax inflate the order beyond what Wix charged.
        cls.tax_15 = cls.env['account.tax'].create({
            'name': 'Wix Test Tax 15',
            'amount': 15.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.env.company.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Wix Totals Product',
            'default_code': 'WIX-TOT-1',
            'is_storable': True,
            'list_price': 100.0,
            'taxes_id': [(6, 0, cls.tax_15.ids)],
        })

    def _import(self, claims, order_id='tot-order-1', event_id='tot-evt-1'):
        job = self.env['wix.order.import.job'].create({
            'instance_id': self.instance.id,
            'payload_json': json.dumps(claims),
            'wix_order_id': order_id,
            'wix_event_id': event_id,
        })
        self.env['wix.order.import.job'].cron_process_order_import_jobs()
        return job

    # ------------------------------------------------------------------
    # The bug this phase fixes
    # ------------------------------------------------------------------

    def test_product_default_tax_does_not_inflate_the_total(self):
        """Odoo must not add a tax Wix never charged.

        The product carries 15% tax, but no tax is configured on the
        connection, so the Odoo total must equal the Wix total exactly.
        """
        job = self._import(claims_with_totals(total='200.00'))
        order = job.sale_order_id

        self.assertEqual(job.state, 'done')
        self.assertEqual(order.amount_total, 200.0)
        self.assertFalse(order.order_line[0].tax_ids)
        self.assertFalse(order.wix_review_needed)

    def test_configured_tax_is_applied_when_set(self):
        self.instance.sudo().write({'sales_tax_id': self.tax_15.id})
        job = self._import(claims_with_totals(total='230.00'))
        order = job.sale_order_id

        self.assertEqual(order.order_line[0].tax_ids, self.tax_15)
        self.assertEqual(order.amount_total, 230.0)
        self.assertFalse(order.wix_review_needed)

    # ------------------------------------------------------------------
    # Shipping, discount, fees
    # ------------------------------------------------------------------

    def test_shipping_becomes_its_own_line(self):
        job = self._import(claims_with_totals(shipping='45.00', total='245.00'))
        order = job.sale_order_id

        shipping_lines = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'WIX-SHIPPING'
        )
        self.assertEqual(len(shipping_lines), 1)
        self.assertEqual(shipping_lines.price_unit, 45.0)
        self.assertEqual(shipping_lines.name, 'Standard')
        self.assertEqual(order.amount_total, 245.0)
        self.assertFalse(order.wix_review_needed)

    def test_discount_becomes_a_negative_line(self):
        job = self._import(claims_with_totals(discount='30.00', total='170.00'))
        order = job.sale_order_id

        discount_lines = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'WIX-DISCOUNT'
        )
        self.assertEqual(len(discount_lines), 1)
        self.assertEqual(discount_lines.price_unit, -30.0)
        self.assertEqual(order.amount_total, 170.0)
        self.assertFalse(order.wix_review_needed)

    def test_additional_fees_become_lines(self):
        job = self._import(claims_with_totals(
            fees=[('Gift wrap', '10.00'), ('Handling', '5.50')],
            total='215.50',
        ))
        order = job.sale_order_id

        fee_lines = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'WIX-FEE'
        )
        self.assertEqual(len(fee_lines), 2)
        self.assertEqual(sorted(fee_lines.mapped('price_unit')), [5.5, 10.0])
        self.assertEqual(order.amount_total, 215.50)
        self.assertFalse(order.wix_review_needed)

    def test_shipping_discount_and_fees_together(self):
        job = self._import(claims_with_totals(
            shipping='40.00', discount='25.00',
            fees=[('Handling', '5.00')], total='220.00',
        ))
        order = job.sale_order_id

        self.assertEqual(order.amount_total, 220.0)
        self.assertEqual(len(order.order_line), 4)
        self.assertFalse(order.wix_review_needed)

    def test_zero_amounts_create_no_lines(self):
        job = self._import(claims_with_totals(
            shipping='0', discount='0', total='200.00',
        ))
        order = job.sale_order_id
        self.assertEqual(len(order.order_line), 1)

    def test_service_products_are_reused_not_duplicated(self):
        self._import(claims_with_totals(shipping='10.00', total='210.00'))
        self._import(
            claims_with_totals(order_id='tot-order-2', shipping='10.00',
                               total='210.00', event_id='tot-evt-2'),
            order_id='tot-order-2', event_id='tot-evt-2',
        )
        self.assertEqual(
            self.env['product.product'].search_count(
                [('default_code', '=', 'WIX-SHIPPING')]
            ), 1,
        )

    # ------------------------------------------------------------------
    # Reconciliation against the Wix total
    # ------------------------------------------------------------------

    def test_mismatch_is_flagged_and_explained(self):
        """A wrong total must never pass silently: COD depends on it."""
        job = self._import(claims_with_totals(total='999.00'))
        order = job.sale_order_id

        self.assertEqual(job.state, 'done')
        self.assertTrue(order.wix_review_needed)
        messages = order.message_ids.mapped('body')
        self.assertTrue(any('999' in str(body) for body in messages))

    def test_mismatch_can_be_made_a_hard_failure(self):
        self.instance.sudo().write({'total_mismatch_policy': 'fail'})
        job = self._import(claims_with_totals(total='999.00'))

        self.assertFalse(job.sale_order_id)
        self.assertTrue(job.last_error_message)
        self.assertEqual(
            self.env['sale.order'].search_count([('wix_order_id', '=', 'tot-order-1')]),
            0,
        )

    def test_rounding_within_tolerance_is_accepted(self):
        job = self._import(claims_with_totals(total='200.01'))
        self.assertFalse(job.sale_order_id.wix_review_needed)

    def test_missing_wix_total_is_flagged_not_assumed_correct(self):
        job = self._import(claims_with_totals(total=None))
        order = job.sale_order_id

        self.assertTrue(order)
        self.assertTrue(order.wix_review_needed)

    def test_amount_parser_tolerates_bad_values(self):
        Job = self.env['wix.order.import.job']
        self.assertEqual(Job._amount({'amount': '12.5'}), 12.5)
        self.assertEqual(Job._amount({'amount': None}), 0.0)
        self.assertEqual(Job._amount({'amount': 'not-a-number'}), 0.0)
        self.assertEqual(Job._amount(None), 0.0)
        self.assertEqual(Job._amount('nonsense'), 0.0)
