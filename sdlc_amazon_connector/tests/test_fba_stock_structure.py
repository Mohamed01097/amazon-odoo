from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestFbaStockStructure(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 1 Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Phase 1 Source Warehouse',
            'code': 'P1SRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Phase 1 FBA Warehouse',
            'code': 'P1FBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 1 Test Instance',
            'company_id': self.company.id,
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })

    def _managed_locations(self):
        return self.env['stock.location'].sudo().with_context(active_test=False).search([
            ('amazon_instance_id', '=', self.instance.id),
            ('amazon_fba_location_type', 'in', [
                'transit', 'sellable', 'reserved', 'unsellable',
            ]),
        ])

    def test_01_first_setup(self):
        action = self.instance.action_create_fba_stock_structure()

        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(self.instance.fba_source_location_id, self.source_warehouse.lot_stock_id)
        self.assertTrue(self.instance.fba_transit_location_id)
        self.assertTrue(self.instance.fba_sellable_location_id)
        self.assertTrue(self.instance.fba_reserved_location_id)
        self.assertTrue(self.instance.fba_unsellable_location_id)
        self.assertEqual(self.instance.fba_transit_location_id.usage, 'transit')
        self.assertFalse(self.instance.fba_transit_location_id.location_id)

        for role in ('sellable', 'reserved', 'unsellable'):
            location = self.instance[f'fba_{role}_location_id']
            self.assertEqual(location.usage, 'internal')
            self.assertEqual(location.company_id, self.company)
            self.assertEqual(location.location_id, self.fba_warehouse.lot_stock_id)
            self.assertEqual(location.amazon_instance_id, self.instance)
            self.assertEqual(location.amazon_fba_location_type, role)

        self.assertEqual(self.instance.fba_transit_location_id.company_id, self.company)
        self.assertEqual(self.instance.fba_transit_location_id.amazon_instance_id, self.instance)
        self.assertEqual(self.instance.fba_transit_location_id.amazon_fba_location_type, 'transit')

    def test_02_setup_is_idempotent(self):
        self.instance.action_create_fba_stock_structure()
        first_ids = {
            role: self.instance[f'fba_{role}_location_id'].id
            for role in ('transit', 'sellable', 'reserved', 'unsellable')
        }

        self.instance.action_create_fba_stock_structure()
        second_ids = {
            role: self.instance[f'fba_{role}_location_id'].id
            for role in ('transit', 'sellable', 'reserved', 'unsellable')
        }

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(self._managed_locations()), 4)

    def test_03_missing_source_requires_explicit_selection(self):
        instance = self.env['amazon.instance'].sudo().create({
            'name': 'Missing Source Instance',
            'company_id': self.company.id,
            'fba_warehouse_id': self.fba_warehouse.id,
        })

        with self.assertRaisesRegex(UserError, 'FBA Source Location'):
            instance.action_create_fba_stock_structure()

    def test_04_company_mismatch_is_rejected(self):
        other_company = self.env['res.company'].sudo().create({
            'name': 'Other Phase 1 Test Company',
        })
        other_warehouse = self.env['stock.warehouse'].sudo().with_company(other_company).create({
            'name': 'Other Company Warehouse',
            'code': 'P1OTH',
            'company_id': other_company.id,
        })

        with self.assertRaises(ValidationError):
            self.instance.write({'fba_warehouse_id': other_warehouse.id})
        with self.assertRaises(ValidationError):
            self.instance.write({
                'fba_source_location_id': other_warehouse.lot_stock_id.id,
            })

    def test_05_location_usage_is_rejected(self):
        internal_location = self.env['stock.location'].sudo().create({
            'name': 'Wrong Transit Type',
            'usage': 'internal',
            'company_id': self.company.id,
            'location_id': self.fba_warehouse.lot_stock_id.id,
        })
        transit_location = self.env['stock.location'].sudo().create({
            'name': 'Wrong Sellable Type',
            'usage': 'transit',
            'company_id': self.company.id,
        })

        with self.assertRaises(ValidationError):
            self.instance.write({'fba_transit_location_id': internal_location.id})
        with self.assertRaises(ValidationError):
            self.instance.write({'fba_sellable_location_id': transit_location.id})

    def test_06_partial_existing_structure_is_reused(self):
        existing_sellable = self.env['stock.location'].sudo().create({
            'name': 'Amazon FBA Sellable',
            'usage': 'internal',
            'company_id': self.company.id,
            'location_id': self.fba_warehouse.lot_stock_id.id,
            'active': True,
        })

        self.instance.action_create_fba_stock_structure()

        self.assertEqual(self.instance.fba_sellable_location_id, existing_sellable)
        self.assertEqual(existing_sellable.amazon_instance_id, self.instance)
        self.assertEqual(existing_sellable.amazon_fba_location_type, 'sellable')
        self.assertEqual(len(self._managed_locations()), 4)

    def test_07_setup_does_not_change_stock(self):
        Picking = self.env['stock.picking'].sudo()
        Move = self.env['stock.move'].sudo()
        Quant = self.env['stock.quant'].sudo()
        picking_ids_before = Picking.search([]).ids
        move_ids_before = Move.search([]).ids
        quant_values_before = {
            quant.id: (quant.quantity, quant.reserved_quantity)
            for quant in Quant.search([])
        }

        self.instance.action_create_fba_stock_structure()

        self.assertEqual(Picking.search([]).ids, picking_ids_before)
        self.assertEqual(Move.search([]).ids, move_ids_before)
        self.assertEqual(
            {
                quant.id: (quant.quantity, quant.reserved_quantity)
                for quant in Quant.search([])
            },
            quant_values_before,
        )

    def test_08_setup_security(self):
        amazon_user = new_test_user(
            self.env,
            login='phase1_amazon_user',
            groups='sdlc_amazon_connector.group_amazon_user',
            company_id=self.company.id,
        ).with_company(self.company)
        amazon_manager = new_test_user(
            self.env,
            login='phase1_amazon_manager',
            groups='sdlc_amazon_connector.group_amazon_manager',
            company_id=self.company.id,
        ).with_company(self.company)

        with self.assertRaises(AccessError):
            self.instance.with_user(amazon_user).action_create_fba_stock_structure()

        action = self.instance.with_user(amazon_manager).action_create_fba_stock_structure()
        self.assertEqual(action['tag'], 'display_notification')
