from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWixInstance(TransactionCase):

    def setUp(self):
        super().setUp()
        self.warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )

    def test_create_defaults(self):
        instance = self.env['wix.instance'].create({
            'name': 'Test Store',
            'order_import_enabled': False,
        })
        self.assertTrue(instance.active)
        self.assertEqual(instance.company_id, self.env.company)

    def test_order_import_requires_warehouse(self):
        with self.assertRaises(ValidationError):
            self.env['wix.instance'].create({
                'name': 'Test Store Without Warehouse',
                'order_import_enabled': True,
                'default_warehouse_id': False,
            })

    def test_order_import_enabled_with_warehouse(self):
        instance = self.env['wix.instance'].create({
            'name': 'Test Store With Warehouse',
            'order_import_enabled': True,
            'default_warehouse_id': self.warehouse.id,
        })
        self.assertTrue(instance.order_import_enabled)

    def test_user_group_cannot_write(self):
        user = self.env['res.users'].create({
            'name': 'Wix Viewer',
            'login': 'wix_viewer_test',
            'group_ids': [(6, 0, [self.env.ref('sdlc_wix_connector.group_wix_user').id])],
        })
        instance = self.env['wix.instance'].create({
            'name': 'Test Store',
            'order_import_enabled': False,
        })
        with self.assertRaises(Exception):
            instance.with_user(user).write({'name': 'Hacked'})
