import inspect
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_ID = 'pl1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_A = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_B = 'sh5678abcd-1234-abcd-5678-1234abcd5678'


@tagged('post_install', '-at_install', 'fba_dispatch')
class TestFbaDispatch(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({'name': 'FBA Dispatch Company'})
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Dispatch Source', 'code': 'DSP', 'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Dispatch FBA', 'code': 'DFBA', 'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Egypt FBA Dispatch',
            'company_id': self.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product_a, self.amazon_product_a = self._create_product('SKU-A')
        self.product_b, self.amazon_product_b = self._create_product('SKU-B')

    def _create_product(self, sku):
        product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku,
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': sku,
            'instance_id': self.instance.id,
            'sku': sku,
            'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _create_plan(self, splits, name='DISPATCH-PLAN', **overrides):
        planned = {}
        products = {'SKU-A': self.amazon_product_a, 'SKU-B': self.amazon_product_b}
        for _shipment_id, lines in splits:
            for sku, quantity in lines:
                planned[sku] = planned.get(sku, 0) + quantity
        values = {
            'name': name,
            'shipment_name': name,
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID,
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'placement_confirmation_status': 'success',
            'state': 'placement_confirmed',
            'line_ids': [Command.create({
                'amazon_product_id': products[sku].id,
                'odoo_product_id': products[sku].odoo_product_id.id,
                'sku': sku,
                'planned_quantity': quantity,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            }) for sku, quantity in planned.items()],
        }
        values.update(overrides)
        plan = self.env['amazon.inbound.shipment'].sudo().create(values)
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': plan.id,
            'amazon_placement_option_id': PLACEMENT_ID,
            'status': 'ACCEPTED',
            'selected': True,
            'amazon_shipment_ids': str([shipment_id for shipment_id, _lines in splits]).replace("'", '"'),
        })
        physicals = self.env['amazon.fba.physical.shipment']
        for index, (shipment_id, lines) in enumerate(splits, start=1):
            physicals |= self.env['amazon.fba.physical.shipment'].sudo().create({
                'inbound_shipment_id': plan.id,
                'placement_option_id': placement.id,
                'amazon_shipment_id': shipment_id,
                'shipment_confirmation_id': 'FBA-DISPATCH-%s' % index,
                'destination_fc': 'CAI%s' % index,
                'status': 'WORKING',
                'line_ids': [Command.create({
                    'amazon_product_id': products[sku].id,
                    'msku': sku,
                    'quantity': quantity,
                }) for sku, quantity in lines],
            })
        return plan, physicals

    def _receive(self, product, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.source_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': self.instance.fba_source_location_id.id,
            'company_id': self.company.id,
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': self.instance.fba_source_location_id.id,
                'company_id': self.company.id,
            })],
        })
        result = picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(picking.state, 'done')

    def _quantity(self, product, location):
        product.invalidate_recordset()
        return product.with_context(location=location.id).qty_available

    def test_one_physical_shipment_creates_one_reserved_picking_once(self):
        plan, physicals = self._create_plan([(SHIPMENT_A, [('SKU-A', 4)])])
        self._receive(self.product_a, 10)

        with patch.object(AmazonAPI, '_amazon_request') as amazon_request:
            plan.action_create_picking()
            plan.action_create_picking()

        self.assertFalse(amazon_request.called)
        self.assertEqual(len(plan.picking_ids), 1)
        self.assertEqual(physicals.picking_id, plan.picking_ids)
        self.assertEqual(physicals.dispatch_state, 'ready_to_dispatch')
        self.assertEqual(plan.state, 'ready_to_ship')
        self.assertEqual(plan.picking_ids.state, 'assigned')
        self.assertEqual(plan.picking_ids.move_ids.product_uom_qty, 4)
        self.assertEqual(plan.picking_ids.amazon_instance_id, self.instance)
        self.assertEqual(plan.picking_ids.amazon_inbound_shipment_id, plan)
        self.assertEqual(plan.picking_ids.amazon_fba_physical_shipment_id, physicals)
        self.assertEqual(plan.picking_ids.amazon_shipment_id, SHIPMENT_A)

    def test_multiple_shipments_use_amazon_final_quantity_distribution(self):
        plan, physicals = self._create_plan([
            (SHIPMENT_A, [('SKU-A', 10), ('SKU-B', 10)]),
            (SHIPMENT_B, [('SKU-A', 10)]),
        ])
        self._receive(self.product_a, 20)
        self._receive(self.product_b, 10)

        plan.action_create_picking()

        self.assertEqual(len(plan.picking_ids), 2)
        self.assertFalse(plan.picking_id)
        by_shipment = {physical.amazon_shipment_id: physical.picking_id for physical in physicals}
        quantities_a = {
            move.product_id.default_code: move.product_uom_qty
            for move in by_shipment[SHIPMENT_A].move_ids
        }
        quantities_b = {
            move.product_id.default_code: move.product_uom_qty
            for move in by_shipment[SHIPMENT_B].move_ids
        }
        self.assertEqual(quantities_a, {'SKU-A': 10, 'SKU-B': 10})
        self.assertEqual(quantities_b, {'SKU-A': 10})
        self.assertEqual(set(physicals.mapped('destination_fc')), {'CAI1', 'CAI2'})

    def test_dispatch_preconditions(self):
        plan, physical = self._create_plan([(SHIPMENT_A, [('SKU-A', 1)])])
        plan.placement_confirmation_status = 'failed'
        with self.assertRaisesRegex(UserError, 'Placement must be confirmed'):
            physical.action_create_dispatch_picking()
        plan.placement_confirmation_status = 'success'
        plan.packing_confirmation_status = 'failed'
        with self.assertRaisesRegex(UserError, 'Packing must be confirmed'):
            physical.action_create_dispatch_picking()
        plan.packing_confirmation_status = 'success'
        physical.amazon_shipment_id = ' '
        with self.assertRaisesRegex(UserError, 'valid shipmentId'):
            physical.action_create_dispatch_picking()
        physical.amazon_shipment_id = SHIPMENT_A
        physical.shipment_confirmation_id = False
        with self.assertRaisesRegex(UserError, 'shipmentConfirmationId'):
            physical.action_create_dispatch_picking()

    def test_missing_locations(self):
        _plan, physical = self._create_plan([(SHIPMENT_A, [('SKU-A', 1)])])
        self.instance.fba_transit_location_id = False
        with self.assertRaisesRegex(UserError, 'Configure both'):
            physical.action_create_dispatch_picking()

    def test_insufficient_stock_keeps_standard_unreserved_picking(self):
        _plan, physical = self._create_plan([(SHIPMENT_A, [('SKU-A', 5)])])
        source_before = self._quantity(self.product_a, self.instance.fba_source_location_id)

        result = physical.action_create_dispatch_picking()

        self.assertEqual(result['params']['type'], 'warning')
        self.assertTrue(physical.picking_id)
        self.assertNotEqual(physical.picking_id.state, 'assigned')
        self.assertEqual(physical.dispatch_state, 'picking_created')
        with self.assertRaisesRegex(UserError, 'full Amazon shipment quantity'):
            physical.picking_id.button_validate()
        self.assertEqual(
            self._quantity(self.product_a, self.instance.fba_source_location_id), source_before,
        )

    def test_validation_moves_source_to_transit_only_and_marks_dispatched(self):
        plan, physical = self._create_plan([(SHIPMENT_A, [('SKU-A', 3)])])
        self._receive(self.product_a, 10)
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        source_before = self._quantity(self.product_a, source)
        transit_before = self._quantity(self.product_a, transit)
        sellable_before = self._quantity(self.product_a, sellable)
        physical.action_create_dispatch_picking()

        with patch.object(AmazonAPI, '_amazon_request') as amazon_request:
            result = physical.picking_id.with_context(
                picking_ids_not_to_backorder=physical.picking_id.ids,
            ).button_validate()

        self.assertNotIsInstance(result, dict)
        self.assertFalse(amazon_request.called)
        self.assertEqual(physical.picking_id.state, 'done')
        self.assertEqual(physical.dispatch_state, 'dispatched')
        self.assertTrue(physical.dispatch_date)
        self.assertEqual(plan.state, 'dispatched')
        self.assertEqual(self._quantity(self.product_a, source), source_before - 3)
        self.assertEqual(self._quantity(self.product_a, transit), transit_before + 3)
        self.assertEqual(self._quantity(self.product_a, sellable), sellable_before)

    def test_dispatch_code_has_no_direct_quant_write(self):
        model = type(self.env['amazon.fba.physical.shipment'])
        source = inspect.getsource(model._create_dispatch_picking)
        source += inspect.getsource(model._prepare_dispatch_move_commands)
        self.assertNotIn("env['stock.quant']", source)
        self.assertNotIn('stock.quant', source)

    def test_physical_shipment_record_rule_is_company_isolated(self):
        _plan, physical = self._create_plan([(SHIPMENT_A, [('SKU-A', 1)])])
        other_company = self.env['res.company'].sudo().create({'name': 'Other Dispatch Company'})
        OtherWarehouse = self.env['stock.warehouse'].sudo().with_company(other_company)
        other_source = OtherWarehouse.create({
            'name': 'Other Dispatch Source', 'code': 'ODS', 'company_id': other_company.id,
        })
        other_fba = OtherWarehouse.create({
            'name': 'Other Dispatch FBA', 'code': 'ODF', 'company_id': other_company.id,
        })
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'Other Egypt FBA',
            'company_id': other_company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'fba_warehouse_id': other_fba.id,
            'fba_source_location_id': other_source.lot_stock_id.id,
        })
        other_plan = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'OTHER-DISPATCH-PLAN',
            'instance_id': other_instance.id,
            'inbound_plan_id': 'wf9999abcd-1234-abcd-5678-1234abcd5678',
        })
        other_placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': other_plan.id,
            'amazon_placement_option_id': 'pl9999abcd-1234-abcd-5678-1234abcd5678',
            'status': 'ACCEPTED',
            'selected': True,
            'amazon_shipment_ids': '["sh9999abcd-1234-abcd-5678-1234abcd5678"]',
        })
        other_physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': other_plan.id,
            'placement_option_id': other_placement.id,
            'amazon_shipment_id': 'sh9999abcd-1234-abcd-5678-1234abcd5678',
        })
        user = self.env['res.users'].sudo().create({
            'name': 'Dispatch Manager',
            'login': 'dispatch-manager-test',
            'company_id': self.company.id,
            'company_ids': [Command.set([self.company.id])],
            'group_ids': [Command.set([
                self.env.ref('base.group_user').id,
                self.env.ref('sdlc_amazon_connector.group_amazon_manager').id,
            ])],
        })
        visible = self.env['amazon.fba.physical.shipment'].with_user(user).search([
            ('id', 'in', (physical | other_physical).ids),
        ])
        self.assertEqual(visible, physical)
        self.assertNotIn(other_company, user.company_ids)
