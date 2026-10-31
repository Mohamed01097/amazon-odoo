from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBostaInstance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )

    def test_create_defaults(self):
        instance = self.env['bosta.instance'].create({'name': 'Bosta Test'})
        self.assertTrue(instance.active)
        self.assertEqual(instance.company_id, self.env.company)
        # Booking real shipments must never be on by default.
        self.assertFalse(instance.shipment_booking_enabled)
        self.assertTrue(instance.default_cod_enabled)
        self.assertEqual(instance.api_base_url, 'https://app.bosta.co')

    def test_warehouse_must_match_company(self):
        other_company = self.env['res.company'].create({'name': 'Bosta Other Co'})
        with self.assertRaises(ValidationError):
            self.env['bosta.instance'].create({
                'name': 'Bosta Mismatch',
                'company_id': other_company.id,
                'warehouse_id': self.warehouse.id,
            })

    def test_warehouse_same_company_is_accepted(self):
        instance = self.env['bosta.instance'].create({
            'name': 'Bosta With Warehouse',
            'warehouse_id': self.warehouse.id,
        })
        self.assertEqual(instance.warehouse_id, self.warehouse)

    def test_user_group_cannot_write(self):
        user = self.env['res.users'].create({
            'name': 'Bosta Viewer',
            'login': 'bosta_viewer_test',
            'group_ids': [(6, 0, [self.env.ref('sdlc_bosta_connector.group_bosta_user').id])],
        })
        instance = self.env['bosta.instance'].create({'name': 'Bosta Test RO'})
        with self.assertRaises(Exception):
            instance.with_user(user).write({'name': 'Changed'})
