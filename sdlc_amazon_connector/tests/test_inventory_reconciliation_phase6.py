import inspect
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import amazon_api as amazon_api_module
from ..models.amazon_api import AmazonAPI
from ..models.amazon_inventory_reconciliation import (
    AmazonInventoryReconciliation,
    AmazonInventoryReconciliationRun,
)


@tagged('post_install', '-at_install', 'amazon_phase6')
class TestAmazonInventoryReconciliationPhase6(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Reconciliation Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Reconciliation Source Warehouse',
            'code': 'RCSRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Reconciliation FBA Warehouse',
            'code': 'RCFBA',
            'company_id': self.company.id,
        })
        self.instance = self._create_instance(
            self.company, self.source_warehouse, self.fba_warehouse,
            suffix='ONE', marketplace='A1F83G8C2ARO7P',
        )
        self.env['ir.config_parameter'].sudo().set_param(
            'amazon_connector.inventory_reconciliation_mode', 'manual',
        )

    def _create_instance(self, company, source_warehouse, fba_warehouse,
                         suffix, marketplace):
        instance = self.env['amazon.instance'].sudo().create({
            'name': 'Reconciliation Instance %s' % suffix,
            'company_id': company.id,
            'seller_id': 'RECONSELLER%s' % suffix,
            'marketplace_id': marketplace,
            'refresh_token': 'recon-refresh-%s' % suffix,
            'client_id': 'recon-client-%s' % suffix,
            'client_secret': 'recon-secret-%s' % suffix,
            'fba_warehouse_id': fba_warehouse.id,
            'fba_source_location_id': source_warehouse.lot_stock_id.id,
        })
        instance.action_create_fba_stock_structure()
        return instance

    def _create_product(self, sku, instance=None, company=None):
        instance = instance or self.instance
        company = company or instance.company_id
        product = self.env['product.product'].sudo().with_company(company).create({
            'name': 'Reconciliation %s' % sku,
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'company_id': company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon %s' % sku,
            'instance_id': instance.id,
            'sku': sku,
            'fulfillment_channel': 'AFN',
            'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _put_stock(self, product, location, quantity, instance=None):
        if not quantity:
            return self.env['stock.picking']
        instance = instance or self.instance
        company = instance.company_id
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(company).create({
            'picking_type_id': instance.fba_warehouse_id.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': location.id,
            'company_id': company.id,
            'origin': 'RECONCILIATION-TEST-BALANCE',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': location.id,
                'company_id': company.id,
            })],
        })
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
        ).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(picking.state, 'done')
        return picking

    @staticmethod
    def _quantity_at(product, location, company):
        product.invalidate_recordset()
        return product.with_company(company).with_context(location=location.id).qty_available

    @staticmethod
    def _summary(sku, sellable=0, reserved=0, unsellable=0,
                 working=0, shipped=0, receiving=0):
        return {
            'asin': 'B0RECONTEST',
            'fnSku': 'X00%s' % sku,
            'sellerSku': sku,
            'condition': 'NewItem',
            'lastUpdatedTime': '2026-08-09T08:30:00Z',
            'totalQuantity': (
                sellable + reserved + unsellable + working + shipped + receiving
            ),
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
        page = {'payload': {'inventorySummaries': list(summaries)}}
        return {
            'payload': {'inventorySummaries': list(summaries)},
            '_amazon_request_ids': ['reconciliation-request-id'],
            '_pages': [page],
            '_snapshot_complete': True,
            '_page_count': 1,
        }

    def _run(self, *summaries, instance=None):
        instance = instance or self.instance
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': instance.id,
            'trigger': 'manual',
        })
        with (
            patch.object(
                type(instance), '_get_access_token_or_raise',
                return_value='reconciliation-access-token',
            ),
            patch.object(
                AmazonAPI, 'get_all_inventory_summaries', autospec=True,
                return_value=self._response(*summaries),
            ),
        ):
            self.assertTrue(run._process_run(), run.last_error)
        self.assertEqual(run.state, 'completed')
        self.assertTrue(run.snapshot_complete)
        return run

    def test_a_complete_matching_snapshot(self):
        product, amazon_product = self._create_product('RC-MATCH')
        self._put_stock(product, self.instance.fba_sellable_location_id, 50)
        self._put_stock(product, self.instance.fba_reserved_location_id, 10)
        self._put_stock(product, self.instance.fba_unsellable_location_id, 2)

        run = self._run(self._summary(
            'RC-MATCH', sellable=50, reserved=10, unsellable=2,
            working=7, shipped=0, receiving=0,
        ))
        line = run.reconciliation_ids

        self.assertEqual(line.status, 'matched')
        self.assertEqual(line.difference_sellable, 0)
        self.assertEqual(line.difference_reserved, 0)
        self.assertEqual(line.difference_unsellable, 0)
        self.assertEqual(line.amazon_inbound_working, 7)
        self.assertEqual(line.amazon_reserved_customer_orders, 10)
        self.assertEqual(line.amazon_total, 69)
        self.assertEqual(line.asin, 'B0RECONTEST')
        self.assertEqual(line.fnsku, 'X00RC-MATCH')
        self.assertEqual(amazon_product.amazon_qty, 50)
        self.assertEqual(run.amazon_records_read, 1)
        self.assertEqual(run.page_count, 1)
        self.assertEqual(run.matched_count, 1)
        self.assertEqual(run.issue_count, 0)
        self.assertTrue(run.completed_at)

    def test_b_sellable_mismatch_never_changes_stock_automatically(self):
        product, _amazon_product = self._create_product('RC-SELLABLE')
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, sellable, 50)
        before_pickings = self.env['stock.picking'].search_count([])
        self.env['ir.config_parameter'].sudo().set_param(
            'amazon_connector.inventory_reconciliation_mode', 'automatic',
        )

        run = self._run(self._summary('RC-SELLABLE', sellable=48))
        line = run.reconciliation_ids

        self.assertEqual(run.mode, 'manual')
        self.assertEqual(line.status, 'mismatch')
        self.assertEqual(line.difference_sellable, -2)
        self.assertEqual(line.suggested_action, 'manual_review')
        self.assertFalse(line.applied_picking_id)
        self.assertEqual(self._quantity_at(product, sellable, self.company), 50)
        self.assertEqual(self.env['stock.picking'].search_count([]), before_pickings)

    def test_c_reserved_difference(self):
        product, _amazon_product = self._create_product('RC-RESERVED')
        self._put_stock(product, self.instance.fba_reserved_location_id, 10)

        line = self._run(self._summary('RC-RESERVED', reserved=12)).reconciliation_ids

        self.assertEqual(line.status, 'mismatch')
        self.assertEqual(line.difference_reserved, 2)
        self.assertFalse(line.applied_picking_id)

    def test_d_unsellable_difference(self):
        product, _amazon_product = self._create_product('RC-UNSELLABLE')
        self._put_stock(product, self.instance.fba_unsellable_location_id, 1)

        line = self._run(self._summary('RC-UNSELLABLE', unsellable=3)).reconciliation_ids

        self.assertEqual(line.status, 'mismatch')
        self.assertEqual(line.difference_unsellable, 2)
        self.assertFalse(line.applied_picking_id)

    def test_e_unmapped_amazon_sku(self):
        run = self._run(self._summary(
            'RC-UNMAPPED', sellable=4, reserved=2, unsellable=1,
        ))
        line = run.reconciliation_ids

        self.assertEqual(line.status, 'unmapped')
        self.assertFalse(line.amazon_product_id)
        self.assertFalse(line.odoo_product_id)
        self.assertEqual(line.amazon_sellable, 4)
        self.assertIn('UNMAPPED AMAZON SKU', line.error_message)
        self.assertEqual(run.unmapped_count, 1)

    def test_f_incomplete_pagination_rejects_entire_snapshot(self):
        product, _amazon_product = self._create_product('RC-INCOMPLETE')
        self._put_stock(product, self.instance.fba_sellable_location_id, 5)
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id,
        })
        incomplete = self._response(self._summary('RC-INCOMPLETE', sellable=5))
        incomplete['_snapshot_complete'] = False

        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise',
                return_value='reconciliation-access-token',
            ),
            patch.object(
                AmazonAPI, 'get_all_inventory_summaries', autospec=True,
                return_value=incomplete,
            ),
        ):
            self.assertFalse(run._process_run())

        self.assertEqual(run.state, 'failed')
        self.assertFalse(run.snapshot_complete)
        self.assertFalse(run.reconciliation_ids)
        self.assertEqual(run.error_count, 1)
        self.assertEqual(
            self._quantity_at(product, self.instance.fba_sellable_location_id, self.company),
            5,
        )

    def test_g_http_429_defers_run_and_keeps_stock_unchanged(self):
        product, _amazon_product = self._create_product('RC-429')
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, sellable, 5)
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id,
        })
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '120'
        response._content = b'{"errors":[{"code":"QuotaExceeded"}]}'
        error = requests.exceptions.HTTPError('HTTP 429', response=response)
        before = fields.Datetime.now()

        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise',
                return_value='reconciliation-access-token',
            ),
            patch.object(
                AmazonAPI, 'get_all_inventory_summaries', autospec=True,
                side_effect=error,
            ),
        ):
            self.assertFalse(run._process_run())

        self.assertEqual(run.state, 'queued')
        self.assertEqual(run.retry_count, 1)
        self.assertEqual(run.retry_after_seconds, 120)
        self.assertGreaterEqual((run.next_run_at - before).total_seconds(), 119)
        self.assertFalse(run.reconciliation_ids)
        self.assertEqual(self._quantity_at(product, sellable, self.company), 5)

    def test_h_repeated_runs_keep_history_without_stock_change(self):
        product, _amazon_product = self._create_product('RC-HISTORY')
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, sellable, 5)

        first = self._run(self._summary('RC-HISTORY', sellable=4))
        second = self._run(self._summary('RC-HISTORY', sellable=5))

        self.assertNotEqual(first, second)
        self.assertEqual(first.reconciliation_ids.difference_sellable, -1)
        self.assertEqual(second.reconciliation_ids.difference_sellable, 0)
        self.assertEqual(
            self.env['amazon.inventory.reconciliation'].search_count([
                ('run_id', 'in', (first.id, second.id)),
                ('sku', '=', 'RC-HISTORY'),
            ]),
            2,
        )
        self.assertFalse(second._process_run())
        self.assertEqual(self._quantity_at(product, sellable, self.company), 5)

    def test_i_instances_and_companies_are_isolated(self):
        second_company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Reconciliation Second Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(second_company)
        second_source = Warehouse.create({
            'name': 'Second Reconciliation Source',
            'code': 'R2SRC',
            'company_id': second_company.id,
        })
        second_fba = Warehouse.create({
            'name': 'Second Reconciliation FBA',
            'code': 'R2FBA',
            'company_id': second_company.id,
        })
        second_instance = self._create_instance(
            second_company, second_source, second_fba,
            suffix='TWO', marketplace='ATVPDKIKX0DER',
        )
        first_product, _first_amazon = self._create_product('RC-SHARED')
        second_product, _second_amazon = self._create_product(
            'RC-SHARED', instance=second_instance, company=second_company,
        )
        self._put_stock(
            first_product, self.instance.fba_sellable_location_id, 2,
            instance=self.instance,
        )
        self._put_stock(
            second_product, second_instance.fba_sellable_location_id, 7,
            instance=second_instance,
        )

        first_run = self._run(self._summary('RC-SHARED', sellable=2))
        second_run = self._run(
            self._summary('RC-SHARED', sellable=7), instance=second_instance,
        )

        self.assertEqual(first_run.reconciliation_ids.status, 'matched')
        self.assertEqual(second_run.reconciliation_ids.status, 'matched')
        self.assertEqual(first_run.company_id, self.company)
        self.assertEqual(second_run.company_id, second_company)
        self.assertEqual(first_run.reconciliation_ids.instance_id, self.instance)
        self.assertEqual(second_run.reconciliation_ids.instance_id, second_instance)

    def test_j_reviewed_adjustment_is_standard_and_cannot_apply_twice(self):
        product, _amazon_product = self._create_product('RC-APPLY')
        staging = self.instance.fba_received_location_id
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, staging, 4)
        line = self._run(self._summary('RC-APPLY', sellable=4)).reconciliation_ids
        line.write({
            'adjustment_action': 'received_to_sellable',
            'adjustment_quantity': 4,
            'adjustment_reason': 'Reviewed against Amazon inventory evidence.',
        })

        with self.assertRaises(UserError):
            line.action_apply_suggested_action()
        with self.assertRaises(UserError):
            line.action_mark_adjustment_reviewed()
        line.large_adjustment_confirmed = True
        line.action_mark_adjustment_reviewed()
        line.action_apply_suggested_action()

        picking = line.applied_picking_id
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.amazon_fba_movement_type, 'reconciliation_disposition')
        self.assertEqual(picking.amazon_inventory_reconciliation_id, line)
        self.assertIn('Reviewed against Amazon', picking.note)
        self.assertEqual(self._quantity_at(product, staging, self.company), 0)
        self.assertEqual(self._quantity_at(product, sellable, self.company), 4)
        picking_id = picking.id
        with self.assertRaises(UserError):
            line.action_apply_suggested_action()
        self.assertEqual(line.applied_picking_id.id, picking_id)

    def test_k_reconciliation_code_has_no_direct_quant_write(self):
        source = '\n'.join((
            inspect.getsource(AmazonInventoryReconciliationRun),
            inspect.getsource(AmazonInventoryReconciliation),
        )).lower()

        self.assertNotIn("env['stock.quant']", source)
        self.assertNotIn('update stock_quant', source)
        self.assertNotIn('_update_available_quantity', source)

    def test_l_mapped_sku_not_returned_is_not_assumed_zero(self):
        product, amazon_product = self._create_product('RC-NOT-RETURNED')
        sellable = self.instance.fba_sellable_location_id
        self._put_stock(product, sellable, 6)
        amazon_product.amazon_qty = 6

        run = self._run()
        line = run.reconciliation_ids

        self.assertEqual(line.status, 'not_returned')
        self.assertFalse(line.amazon_returned)
        self.assertEqual(line.difference_sellable, 0)
        self.assertEqual(line.odoo_sellable, 6)
        self.assertEqual(amazon_product.amazon_qty, 6)
        self.assertEqual(run.not_returned_count, 1)

    def test_m_inventory_api_consumes_all_pages_before_completion(self):
        first_response = MagicMock()
        first_response.headers = {'x-amzn-RequestId': 'recon-page-1'}
        first_response.json.return_value = {
            'payload': {'inventorySummaries': [self._summary('RC-PAGE-1')]},
            'pagination': {'nextToken': 'recon-next-token'},
        }
        second_response = MagicMock()
        second_response.headers = {'x-amzn-RequestId': 'recon-page-2'}
        second_response.json.return_value = {
            'payload': {'inventorySummaries': [self._summary('RC-PAGE-2')]},
        }
        api = AmazonAPI()

        with (
            patch.object(
                api, '_amazon_request', side_effect=[first_response, second_response],
            ) as request_mock,
            patch.object(amazon_api_module.time, 'sleep') as sleep_mock,
        ):
            result = api.get_all_inventory_summaries(
                self.instance, 'reconciliation-access-token', details=True,
            )

        self.assertTrue(result['_snapshot_complete'])
        self.assertEqual(result['_page_count'], 2)
        self.assertEqual(len(result['payload']['inventorySummaries']), 2)
        self.assertEqual(
            result['_amazon_request_ids'], ['recon-page-1', 'recon-page-2'],
        )
        self.assertEqual(request_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.5)
        self.assertEqual(
            request_mock.call_args_list[1].kwargs['params']['nextToken'],
            'recon-next-token',
        )
        self.assertNotIn('startDateTime', request_mock.call_args_list[0].kwargs['params'])
