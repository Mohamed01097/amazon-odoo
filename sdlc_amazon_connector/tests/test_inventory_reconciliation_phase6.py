from unittest.mock import MagicMock, patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install', 'amazon_phase6')
class TestAmazonInventoryReconciliationPhase6(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 6 Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Phase 6 Source Warehouse',
            'code': 'P6SRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Phase 6 FBA Warehouse',
            'code': 'P6FBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 6 Test Instance',
            'company_id': self.company.id,
            'seller_id': 'PHASE6SELLER',
            'marketplace_id': 'ATVPDKIKX0DER',
            'refresh_token': 'phase6-refresh-token',
            'client_id': 'phase6-client-id',
            'client_secret': 'phase6-client-secret',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.env['ir.config_parameter'].sudo().set_param(
            'amazon_connector.inventory_reconciliation_mode', 'manual',
        )

    def _create_product(self, sku):
        product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 6 %s' % sku,
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon %s' % sku,
            'instance_id': self.instance.id,
            'sku': sku,
            'fulfillment_channel': 'AFN',
            'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _put_stock(self, product, location, quantity):
        if not quantity:
            return self.env['stock.picking']
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': location.id,
            'company_id': self.company.id,
            'origin': 'PHASE6-TEST-BALANCE',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': location.id,
                'company_id': self.company.id,
            })],
        })
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
        ).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(picking.state, 'done')
        return picking

    def _quantity_at(self, product, location):
        product.invalidate_recordset()
        return product.with_company(self.company).with_context(
            location=location.id,
        ).qty_available

    @staticmethod
    def _summary(sku, sellable=0, reserved=0, unsellable=0,
                 working=0, shipped=0, receiving=0):
        return {
            'asin': 'B0PHASE6TEST',
            'fnSku': 'X00%s' % sku,
            'sellerSku': sku,
            'condition': 'NewItem',
            'inventoryDetails': {
                'fulfillableQuantity': sellable,
                'inboundWorkingQuantity': working,
                'inboundShippedQuantity': shipped,
                'inboundReceivingQuantity': receiving,
                'reservedQuantity': {
                    'totalReservedQuantity': reserved,
                    'pendingCustomerOrderQuantity': reserved,
                    'pendingTransshipmentQuantity': 0,
                    'fcProcessingQuantity': 0,
                },
                'unfulfillableQuantity': {
                    'totalUnfulfillableQuantity': unsellable,
                },
            },
        }

    @staticmethod
    def _response(*summaries):
        return {
            'payload': {'inventorySummaries': list(summaries)},
            '_amazon_request_ids': ['phase6-request-id'],
            '_pages': [{'payload': {'inventorySummaries': list(summaries)}}],
        }

    def _run(self, *summaries, mode='manual'):
        self.env['ir.config_parameter'].sudo().set_param(
            'amazon_connector.inventory_reconciliation_mode', mode,
        )
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id,
            'trigger': 'manual',
        })
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise',
                return_value='phase6-access-token',
            ),
            patch.object(
                AmazonAPI, 'get_all_inventory_summaries', autospec=True,
                return_value=self._response(*summaries),
            ),
        ):
            self.assertTrue(run._process_run(), run.last_error)
        self.assertEqual(run.state, 'completed')
        return run

    def test_01_matching_snapshot_and_inbound_semantics(self):
        product, amazon_product = self._create_product('P6-MATCH')
        locations = {
            'sellable': self.instance.fba_sellable_location_id,
            'reserved': self.instance.fba_reserved_location_id,
            'unsellable': self.instance.fba_unsellable_location_id,
            'transit': self.instance.fba_transit_location_id,
        }
        self._put_stock(product, locations['sellable'], 10)
        self._put_stock(product, locations['reserved'], 2)
        self._put_stock(product, locations['unsellable'], 1)
        self._put_stock(product, locations['transit'], 3)

        run = self._run(self._summary(
            'P6-MATCH', sellable=10, reserved=2, unsellable=1,
            working=9, shipped=2, receiving=1,
        ))

        line = run.reconciliation_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.status, 'matched')
        self.assertEqual(line.suggested_action, 'none')
        self.assertEqual(line.amazon_inbound, 3)
        self.assertEqual(line.amazon_inbound_working, 9)
        self.assertEqual(run.products_checked, 1)
        self.assertEqual(run.matched_count, 1)
        self.assertEqual(run.mismatch_count, 0)
        self.assertEqual(amazon_product.amazon_qty, 10)
        self.assertFalse(line.applied_picking_id)

    def test_02_all_supported_mismatch_classifications(self):
        products = {}
        for sku in ('P6-RESERVED', 'P6-UNSELLABLE', 'P6-INBOUND', 'P6-TOTAL'):
            products[sku] = self._create_product(sku)[0]
        self._put_stock(
            products['P6-RESERVED'], self.instance.fba_sellable_location_id, 10,
        )
        self._put_stock(
            products['P6-UNSELLABLE'], self.instance.fba_sellable_location_id, 10,
        )
        self._put_stock(
            products['P6-INBOUND'], self.instance.fba_transit_location_id, 5,
        )
        self._put_stock(
            products['P6-TOTAL'], self.instance.fba_sellable_location_id, 5,
        )

        run = self._run(
            self._summary('P6-RESERVED', sellable=6, reserved=4),
            self._summary('P6-UNSELLABLE', sellable=7, unsellable=3),
            self._summary('P6-INBOUND', sellable=2, shipped=2, receiving=1),
            self._summary('P6-TOTAL', sellable=6),
        )
        lines = {line.sku: line for line in run.reconciliation_ids}
        self.assertEqual(lines['P6-RESERVED'].suggested_action, 'sellable_to_reserved')
        self.assertEqual(lines['P6-UNSELLABLE'].suggested_action, 'sellable_to_unsellable')
        self.assertEqual(lines['P6-INBOUND'].suggested_action, 'transit_to_sellable')
        self.assertEqual(lines['P6-TOTAL'].suggested_action, 'inventory_adjustment')
        self.assertEqual(lines['P6-TOTAL'].severity, 'critical')
        self.assertEqual(run.products_checked, 4)
        self.assertEqual(run.mismatch_count, 4)
        self.assertEqual(run.critical_count, 1)
        self.assertEqual(run.pending_review_count, 4)
        self.assertFalse(run.reconciliation_ids.applied_picking_id)

    def test_03_manual_apply_uses_one_standard_transfer_and_is_idempotent(self):
        product, _amazon_product = self._create_product('P6-MANUAL')
        sellable = self.instance.fba_sellable_location_id
        reserved = self.instance.fba_reserved_location_id
        self._put_stock(product, sellable, 10)
        run = self._run(self._summary('P6-MANUAL', sellable=6, reserved=4))
        line = run.reconciliation_ids

        self.assertFalse(line.applied_picking_id)
        line.action_apply_suggested_action()

        picking = line.applied_picking_id
        self.assertTrue(picking)
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.amazon_fba_movement_type, 'reconciliation_reserved')
        self.assertEqual(picking.amazon_inventory_reconciliation_id, line)
        self.assertEqual(len(picking.move_ids), 1)
        self.assertTrue(picking.move_ids.move_line_ids)
        self.assertEqual(self._quantity_at(product, sellable), 6)
        self.assertEqual(self._quantity_at(product, reserved), 4)
        picking_ids = picking.ids
        with self.assertRaises(UserError):
            line.action_apply_suggested_action()
        self.assertEqual(line.applied_picking_id.ids, picking_ids)

    def test_04_automatic_mode_applies_only_safe_location_transfer(self):
        product, _amazon_product = self._create_product('P6-AUTO')
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, transit, 5)

        run = self._run(
            self._summary('P6-AUTO', sellable=2, shipped=3),
            mode='automatic',
        )

        line = run.reconciliation_ids
        self.assertEqual(run.mode, 'automatic')
        self.assertEqual(line.status, 'applied')
        self.assertEqual(line.suggested_action, 'transit_to_sellable')
        self.assertEqual(line.applied_picking_id.state, 'done')
        self.assertEqual(self._quantity_at(product, transit), 3)
        self.assertEqual(self._quantity_at(product, sellable), 2)
        self.assertEqual(run.pending_review_count, 0)

    def test_05_automatic_mode_never_applies_total_adjustment(self):
        product, _amazon_product = self._create_product('P6-AUTO-MANUAL')
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, sellable, 5)

        run = self._run(
            self._summary('P6-AUTO-MANUAL', sellable=8),
            mode='automatic',
        )

        line = run.reconciliation_ids
        self.assertEqual(line.suggested_action, 'inventory_adjustment')
        self.assertEqual(line.status, 'pending_review')
        self.assertFalse(line.applied_picking_id)
        self.assertEqual(self._quantity_at(product, sellable), 5)
        with self.assertRaises(UserError):
            line.action_apply_suggested_action()

    def test_06_history_and_unique_sku_per_run(self):
        self._create_product('P6-HISTORY')
        first = self._run(self._summary('P6-HISTORY'))
        second = self._run(self._summary('P6-HISTORY'))
        self.assertNotEqual(first, second)
        self.assertEqual(first.reconciliation_ids.sku, 'P6-HISTORY')
        self.assertEqual(second.reconciliation_ids.sku, 'P6-HISTORY')
        self.assertEqual(
            self.env['amazon.inventory.reconciliation'].search_count([
                ('run_id', 'in', (first.id, second.id)),
                ('sku', '=', 'P6-HISTORY'),
            ]),
            2,
        )
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['amazon.inventory.reconciliation'].sudo().create({
                'run_id': first.id,
                'sku': 'P6-HISTORY',
                'suggested_action': 'manual_review',
                'severity': 'critical',
                'status': 'pending_review',
            })

    def test_07_failed_job_is_retried_without_partial_lines(self):
        self._create_product('P6-RETRY')
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id,
            'max_retries': 2,
        })
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise',
                return_value='phase6-access-token',
            ),
            patch.object(
                AmazonAPI, 'get_all_inventory_summaries', autospec=True,
                side_effect=UserError('temporary Amazon failure'),
            ),
        ):
            self.assertFalse(run._process_run())
            self.assertEqual(run.state, 'queued')
            self.assertEqual(run.retry_count, 1)
            self.assertTrue(run.next_run_at)
            self.assertFalse(run.reconciliation_ids)
            self.assertFalse(run._process_run())
        self.assertEqual(run.state, 'failed')
        self.assertEqual(run.retry_count, 2)
        self.assertFalse(run.reconciliation_ids)

    def test_08_inventory_api_details_and_immediate_pagination(self):
        first_response = MagicMock()
        first_response.headers = {'x-amzn-RequestId': 'phase6-page-1'}
        first_response.json.return_value = {
            'payload': {'inventorySummaries': [self._summary('P6-PAGE-1')]},
            'pagination': {'nextToken': 'phase6-next-token'},
        }
        second_response = MagicMock()
        second_response.headers = {'x-amzn-RequestId': 'phase6-page-2'}
        second_response.json.return_value = {
            'payload': {'inventorySummaries': [self._summary('P6-PAGE-2')]},
        }
        api = AmazonAPI()

        with patch.object(
            api, '_amazon_request', side_effect=[first_response, second_response],
        ) as request_mock:
            result = api.get_all_inventory_summaries(
                self.instance, 'phase6-access-token', details=True,
            )

        self.assertEqual(len(result['payload']['inventorySummaries']), 2)
        self.assertEqual(
            result['_amazon_request_ids'],
            ['phase6-page-1', 'phase6-page-2'],
        )
        self.assertEqual(request_mock.call_count, 2)
        first_params = request_mock.call_args_list[0].kwargs['params']
        second_params = request_mock.call_args_list[1].kwargs['params']
        self.assertEqual(first_params['details'], 'true')
        self.assertNotIn('startDateTime', first_params)
        self.assertEqual(second_params['nextToken'], 'phase6-next-token')
        self.assertIn('/fba/inventory/v1/summaries', request_mock.call_args_list[0].args[3])

    def test_09_manual_button_reuses_queued_run(self):
        first_action = self.instance.action_run_inventory_audit()
        second_action = self.instance.action_pull_stock()
        self.assertEqual(first_action['res_id'], second_action['res_id'])
        self.assertEqual(
            self.env['amazon.inventory.reconciliation.run'].search_count([
                ('instance_id', '=', self.instance.id),
                ('state', '=', 'queued'),
            ]),
            1,
        )
