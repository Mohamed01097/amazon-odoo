from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf5000abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_ID = 'pl5000abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID = 'sh5000abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_CONFIRMATION_ID = 'FBA19PHASE5'


@tagged('post_install', '-at_install')
class TestFbaReceivingPhase5(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 5 Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Phase 5 Source Warehouse',
            'code': 'P5SRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Phase 5 FBA Warehouse',
            'code': 'P5FBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 5 Test Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ATVPDKIKX0DER',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 5 Storable Product',
            'default_code': 'P5-MSKU-001',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 5 Amazon Product',
            'instance_id': self.instance.id,
            'sku': 'P5-MSKU-001',
            'odoo_product_id': self.product.id,
        })
        self.shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P5-PLAN-001',
            'shipment_name': 'P5-PLAN-001',
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID,
            'shipment_id': SHIPMENT_ID,
            'shipment_confirmation_id': SHIPMENT_CONFIRMATION_ID,
            'shipment_confirmation_status': 'success',
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'placement_confirmation_status': 'success',
            'state': 'waiting_receiving',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'odoo_product_id': self.product.id,
                'sku': self.amazon_product.sku,
                'fnsku': 'X00PHASE5FNSKU',
                'planned_quantity': 100,
                'quantity_shipped': 100,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            })],
        })
        self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'amazon_placement_option_id': PLACEMENT_OPTION_ID,
            'status': 'ACCEPTED',
            'amazon_shipment_ids': '["%s"]' % SHIPMENT_ID,
            'selected': True,
        })
        self._put_in_transit(100)

    def _put_in_transit(self, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        transit = self.instance.fba_transit_location_id
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': transit.id,
            'company_id': self.company.id,
            'origin': 'P5-TEST-TRANSIT-BALANCE',
            'move_ids': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': quantity,
                'product_uom': self.product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': transit.id,
                'company_id': self.company.id,
            })],
        })
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
        ).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(picking.state, 'done')

    def _quantity_at(self, location):
        self.product.invalidate_recordset()
        return self.product.with_context(location=location.id).qty_available

    @staticmethod
    def _status_response(status='RECEIVING'):
        return {
            'placementOptionId': PLACEMENT_OPTION_ID,
            'shipmentId': SHIPMENT_ID,
            'shipmentConfirmationId': SHIPMENT_CONFIRMATION_ID,
            'selectedTransportationOptionId': 'to-phase5-official',
            'destination': {
                'destinationType': 'AMAZON_WAREHOUSE',
                'warehouseId': 'ONT8',
            },
            'source': {'sourceType': 'SELLER_FACILITY'},
            'status': status,
            '_amazon_request_id': 'phase5-status-request',
        }

    @staticmethod
    def _items_response(received, shipped=100, extra=None):
        item = {
            'ShipmentId': SHIPMENT_CONFIRMATION_ID,
            'SellerSKU': 'P5-MSKU-001',
            'FulfillmentNetworkSKU': 'X00PHASE5FNSKU',
            'QuantityShipped': shipped,
            'QuantityReceived': received,
            'QuantityInCase': 0,
        }
        item.update(extra or {})
        return {
            'payload': {'ItemData': [item]},
            '_amazon_request_ids': ['phase5-items-request'],
            '_pages': [{'payload': {'ItemData': [item]}}],
        }

    def _sync(self, received, status='RECEIVING', shipped=100, extra=None):
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise',
                return_value='test-token',
            ),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                return_value=self._status_response(status),
            ),
            patch.object(
                AmazonAPI, 'get_inbound_shipment_items_v0', autospec=True,
                return_value=self._items_response(received, shipped, extra),
            ),
        ):
            self.shipment.action_sync_receiving()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'sync_receiving'
                and item.state in ('pending', 'in_progress')
            )
            self.assertEqual(len(job), 1)
            job._process_operation()
        return job

    def _receiving_pickings(self):
        return self.shipment.picking_ids.filtered(
            lambda picking: picking.amazon_fba_movement_type == 'receiving_sellable'
        )

    def test_01_partial_receipts_move_only_cumulative_delta(self):
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        unsellable = self.instance.fba_unsellable_location_id

        self._sync(20)
        self.assertEqual(self.shipment.state, 'partially_received')
        self.assertEqual(self.shipment.sent_quantity, 100)
        self.assertEqual(self.shipment.received_quantity, 20)
        self.assertEqual(self.shipment.remaining_quantity, 80)
        self.assertEqual(self.shipment.line_ids.received_moved_quantity, 20)
        self.assertEqual(self._quantity_at(transit), 80)
        self.assertEqual(self._quantity_at(sellable), 20)
        self.assertEqual(self._quantity_at(unsellable), 0)

        self._sync(30)
        self.assertEqual(self.shipment.received_quantity, 30)
        self.assertEqual(self.shipment.line_ids.received_moved_quantity, 30)
        self.assertEqual(self._quantity_at(transit), 70)
        self.assertEqual(self._quantity_at(sellable), 30)
        self.assertEqual(len(self._receiving_pickings()), 2)
        self.assertTrue(all(
            picking.state == 'done' for picking in self._receiving_pickings()
        ))
        self.assertTrue(all(
            move.move_line_ids
            for move in self._receiving_pickings().mapped('move_ids')
        ))
        self.assertEqual(
            sorted(self._receiving_pickings().mapped('move_ids').mapped('product_uom_qty')),
            [10, 20],
        )

    def test_02_duplicate_poll_creates_no_duplicate_move(self):
        self._sync(30)
        picking_ids = self._receiving_pickings().ids
        transit = self._quantity_at(self.instance.fba_transit_location_id)
        sellable = self._quantity_at(self.instance.fba_sellable_location_id)

        self._sync(30)

        self.assertEqual(self._receiving_pickings().ids, picking_ids)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), transit)
        self.assertEqual(self._quantity_at(self.instance.fba_sellable_location_id), sellable)

    def test_03_full_receipt_then_amazon_closed(self):
        self._sync(100)
        self.assertEqual(self.shipment.state, 'received')
        self.assertEqual(self.shipment.remaining_quantity, 0)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), 0)
        self.assertEqual(self._quantity_at(self.instance.fba_sellable_location_id), 100)
        picking_ids = self._receiving_pickings().ids

        self._sync(100, status='CLOSED')
        self.assertEqual(self.shipment.state, 'closed')
        self.assertEqual(self._receiving_pickings().ids, picking_ids)
        self.assertFalse(self.shipment.receiving_discrepancy_ids)

    def test_04_closed_shortage_is_discrepancy_not_silent_loss(self):
        self._sync(90, status='CLOSED')

        self.assertEqual(self.shipment.state, 'closed')
        self.assertEqual(self.shipment.received_quantity, 90)
        self.assertEqual(self.shipment.remaining_quantity, 10)
        self.assertEqual(self.shipment.lost_quantity, 0)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), 10)
        discrepancy = self.shipment.receiving_discrepancy_ids
        self.assertEqual(len(discrepancy), 1)
        self.assertEqual(discrepancy.discrepancy_type, 'closed_shortage')
        self.assertEqual(discrepancy.quantity, 10)
        self.assertFalse(discrepancy.amazon_reported_lost)

        # Re-apply the exact same official snapshot directly because terminal
        # shipments are no longer scheduled. The unique audit record is updated,
        # and no additional move or discrepancy can be created.
        pickings = self._receiving_pickings().ids
        self.shipment._apply_receiving_snapshot(
            self._status_response('CLOSED'), self._items_response(90),
        )
        self.assertEqual(self._receiving_pickings().ids, pickings)
        self.assertEqual(len(self.shipment.receiving_discrepancy_ids), 1)

    def test_05_undocumented_damage_field_is_not_inferred(self):
        self._sync(20, extra={'QuantityDamaged': 5, 'QuantityLost': 2})

        self.assertEqual(self.shipment.damaged_quantity, 0)
        self.assertEqual(self.shipment.lost_quantity, 0)
        self.assertEqual(self._quantity_at(self.instance.fba_unsellable_location_id), 0)
        self.assertEqual(self._quantity_at(self.instance.fba_sellable_location_id), 20)

    def test_06_received_quantity_decrease_never_reverses_inventory(self):
        self._sync(30)
        transit = self._quantity_at(self.instance.fba_transit_location_id)
        sellable = self._quantity_at(self.instance.fba_sellable_location_id)
        picking_ids = self._receiving_pickings().ids

        self._sync(25)

        self.assertEqual(self.shipment.received_quantity, 25)
        self.assertEqual(self.shipment.line_ids.received_moved_quantity, 30)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), transit)
        self.assertEqual(self._quantity_at(self.instance.fba_sellable_location_id), sellable)
        self.assertEqual(self._receiving_pickings().ids, picking_ids)
        discrepancy = self.shipment.receiving_discrepancy_ids.filtered(
            lambda item: item.discrepancy_type == 'received_quantity_decrease'
        )
        self.assertEqual(len(discrepancy), 1)
        self.assertEqual(discrepancy.quantity, 5)

    def test_07_api_wrapper_uses_preserved_paths_and_paginates(self):
        api = AmazonAPI()
        first = MagicMock()
        first.headers = {'x-amzn-RequestId': 'request-page-1'}
        first.json.return_value = {
            'payload': {
                'ItemData': [{'SellerSKU': 'SKU-1'}],
                'NextToken': 'next-token',
            },
        }
        second = MagicMock()
        second.headers = {'x-amzn-RequestId': 'request-page-2'}
        second.json.return_value = {
            'payload': {'ItemData': [{'SellerSKU': 'SKU-2'}]},
        }
        with patch.object(
            api, '_amazon_request', side_effect=[first, second],
        ) as request:
            result = api.get_inbound_shipment_items_v0(
                self.instance, 'test-token', SHIPMENT_CONFIRMATION_ID,
            )

        self.assertEqual(
            [item['SellerSKU'] for item in result['payload']['ItemData']],
            ['SKU-1', 'SKU-2'],
        )
        self.assertEqual(
            result['_amazon_request_ids'], ['request-page-1', 'request-page-2'],
        )
        self.assertTrue(request.call_args_list[0].args[3].endswith(
            '/fba/inbound/v0/shipments/%s/items' % SHIPMENT_CONFIRMATION_ID
        ))
        self.assertTrue(request.call_args_list[1].args[3].endswith(
            '/fba/inbound/v0/shipmentItems'
        ))
        self.assertEqual(request.call_args_list[1].kwargs['params'], {
            'QueryType': 'NEXT_TOKEN',
            'NextToken': 'next-token',
            'MarketplaceId': self.instance.marketplace_id,
        })

    def test_08_scheduler_deduplicates_active_jobs(self):
        self.assertEqual(
            self.env['amazon.inbound.shipment'].cron_enqueue_receiving_sync(), 1,
        )
        self.assertEqual(
            self.env['amazon.inbound.shipment'].cron_enqueue_receiving_sync(), 0,
        )
        jobs = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'sync_receiving'
            and item.state in ('pending', 'in_progress')
        )
        self.assertEqual(len(jobs), 1)
