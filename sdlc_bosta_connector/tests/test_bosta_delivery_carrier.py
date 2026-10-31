from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBostaDeliveryCarrier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['bosta.instance'].create({
            'name': 'Bosta Carrier Test',
            'pickup_location_code': 'PICKUP-1',
            'warehouse_id': cls.warehouse.id,
        })
        cls.delivery_product = cls.env['product.product'].create({
            'name': 'Bosta Shipping',
            'type': 'service',
            'invoice_policy': 'order',
        })
        cls.carrier = cls.env['delivery.carrier'].create({
            'name': 'Bosta Standard',
            'delivery_type': 'bosta',
            'bosta_instance_id': cls.instance.id,
            'bosta_flat_rate': 55.0,
            'integration_level': 'rate',
            'product_id': cls.delivery_product.id,
        })
        cls.partner = cls.env['res.partner'].create({'name': 'Bosta Customer'})
        cls.product = cls.env['product.product'].create({
            'name': 'Bosta Test Item',
            'is_storable': True,
            'list_price': 200.0,
        })

    def _make_order(self):
        return self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })],
        })

    def test_bosta_is_a_selectable_delivery_type(self):
        types = dict(
            self.env['delivery.carrier']._fields['delivery_type'].selection
            if isinstance(
                self.env['delivery.carrier']._fields['delivery_type'].selection, list
            )
            else self.env['delivery.carrier']._fields['delivery_type']
            ._description_selection(self.env)
        )
        self.assertIn('bosta', types)

    def test_rate_shipment_returns_flat_rate(self):
        order = self._make_order()
        result = self.carrier.rate_shipment(order)
        self.assertTrue(result['success'])
        self.assertEqual(result['price'], 55.0)
        self.assertFalse(result['error_message'])

    def test_rate_shipment_without_account_fails_gracefully(self):
        """The rate hook must report failure rather than raise.

        A saved Bosta carrier always has an account (the constraint enforces
        it), so this exercises the hook on an unsaved in-memory record.
        """
        carrier = self.env['delivery.carrier'].new({
            'name': 'Bosta Unconfigured',
            'delivery_type': 'bosta',
            'bosta_instance_id': False,
        })
        result = carrier.bosta_rate_shipment(self._make_order())
        self.assertFalse(result['success'])
        self.assertTrue(result['error_message'])

    def test_carrier_requires_bosta_account(self):
        with self.assertRaises(ValidationError):
            self.env['delivery.carrier'].create({
                'name': 'Bosta No Account',
                'delivery_type': 'bosta',
                'product_id': self.delivery_product.id,
            })

    def test_integration_level_must_stay_rate(self):
        """Automatic shipment creation must be blocked at this phase."""
        with self.assertRaises(ValidationError):
            self.carrier.write({'integration_level': 'rate_and_ship'})

    def test_tracking_link_uses_template(self):
        picking = self.env['stock.picking']
        fake = picking.new({'carrier_tracking_ref': 'BST123456'})
        link = self.carrier.bosta_get_tracking_link(fake)
        self.assertIn('BST123456', link)

    def test_tracking_link_is_none_without_tracking_ref(self):
        fake = self.env['stock.picking'].new({'carrier_tracking_ref': False})
        self.assertIsNone(self.carrier.bosta_get_tracking_link(fake))

    def test_send_shipping_is_wired_to_the_provider_contract(self):
        """Odoo's documented hook must exist and be dispatched to this provider."""
        self.assertTrue(hasattr(self.carrier, 'bosta_send_shipping'))
        # No pickings means nothing to book, and no API call.
        self.assertEqual(self.carrier.bosta_send_shipping(self.env['stock.picking']), [])

    def test_cancel_shipment_raises_until_implemented(self):
        """Must fail loudly: a silent success would leave a live collection booked."""
        with self.assertRaises(UserError):
            self.carrier.bosta_cancel_shipment(self.env['stock.picking'])

    def test_carrier_can_be_used_on_a_sale_order(self):
        """The whole point of this phase: Bosta is selectable like any carrier."""
        order = self._make_order()
        order.write({'carrier_id': self.carrier.id})
        self.assertEqual(order.carrier_id, self.carrier)
        order.action_confirm()
        self.assertEqual(order.state, 'sale')
        self.assertTrue(order.picking_ids)

    def test_other_carrier_types_still_work(self):
        """Regression: adding a provider must not break core carriers."""
        fixed = self.env['delivery.carrier'].create({
            'name': 'Fixed Regression',
            'delivery_type': 'fixed',
            'fixed_price': 10.0,
            'product_id': self.delivery_product.id,
        })
        result = fixed.rate_shipment(self._make_order())
        self.assertTrue(result['success'])
        self.assertEqual(result['price'], 10.0)
