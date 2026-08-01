from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_ID = 'po1234abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_ID = 'pl1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
TRANSPORTATION_OPTION_ID = 'to1234abcd-1234-abcd-5678-1234abcd5678'
TRACKING_OPERATION_ID = '77777777-7777-7777-7777-777777777777'


@tagged('post_install', '-at_install')
class TestFbaShippingPhase4(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 4 Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Phase 4 Source Warehouse',
            'code': 'P4SRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Phase 4 FBA Warehouse',
            'code': 'P4FBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 4 Test Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ATVPDKIKX0DER',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 4 Storable Product',
            'default_code': 'P4-MSKU-001',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 4 Amazon Product',
            'instance_id': self.instance.id,
            'sku': 'P4-MSKU-001',
            'odoo_product_id': self.product.id,
        })
        self.shipment = self._create_phase4_shipment('P4-PLAN-001', quantity=4)
        self._receive_source_stock(10)

    def _create_phase4_shipment(self, name, quantity=4, state='placement_confirmed'):
        shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': name,
            'shipment_name': name,
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID if name == 'P4-PLAN-001' else (
                'wf5678abcd-1234-abcd-5678-1234abcd5678'
            ),
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'placement_confirmation_status': 'success',
            'state': state,
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'odoo_product_id': self.product.id,
                'sku': self.amazon_product.sku,
                'planned_quantity': quantity,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            })],
        })
        packing = self.env['amazon.fba.packing.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': shipment.id,
            'amazon_packing_option_id': (
                PACKING_OPTION_ID if name == 'P4-PLAN-001'
                else 'po5678abcd-1234-abcd-5678-1234abcd5678'
            ),
            'option_name': 'Accepted Packing',
            'status': 'ACCEPTED',
            'selected': True,
        })
        self.env['amazon.fba.box'].sudo().create({
            'packing_option_id': packing.id,
            'amazon_box_id': 'FBA10ABC0YY100001',
            'amazon_packing_group_id': 'pg1234abcd-1234-abcd-5678-1234abcd5678',
            'length': 10,
            'width': 8,
            'height': 6,
            'weight': 2,
            'dimension_unit': 'IN',
            'weight_unit': 'LB',
        })
        self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'amazon_placement_option_id': (
                PLACEMENT_OPTION_ID if name == 'P4-PLAN-001'
                else 'pl5678abcd-1234-abcd-5678-1234abcd5678'
            ),
            'status': 'ACCEPTED',
            'amazon_shipment_ids': '["%s"]' % SHIPMENT_ID,
            'selected': True,
        })
        return shipment

    def _receive_source_stock(self, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.source_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': self.instance.fba_source_location_id.id,
            'company_id': self.company.id,
            'origin': 'P4-TEST-RECEIPT',
            'move_ids': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': quantity,
                'product_uom': self.product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': self.instance.fba_source_location_id.id,
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
    def _get_shipment_response(status='WORKING'):
        return {
            'placementOptionId': PLACEMENT_OPTION_ID,
            'shipmentId': SHIPMENT_ID,
            'shipmentConfirmationId': 'FBA1234ABCD',
            'selectedTransportationOptionId': TRANSPORTATION_OPTION_ID,
            'destination': {
                'destinationType': 'AMAZON_WAREHOUSE',
                'warehouseId': 'ONT8',
            },
            'source': {'sourceType': 'SELLER_FACILITY'},
            'status': status,
            'trackingDetails': {
                'spdTrackingDetail': {
                    'spdTrackingItems': [{
                        'boxId': 'FBA10ABC0YY100001',
                        'trackingId': '1Z999PHASE4',
                        'trackingNumberValidationStatus': 'VALIDATED',
                    }],
                },
            },
            '_amazon_request_id': 'phase4-get-shipment-request',
        }

    def _refresh_amazon_shipment(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                return_value=self._get_shipment_response(),
            ),
        ):
            self.shipment.action_refresh_shipment_status()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'refresh_shipment_status'
                and item.state in ('pending', 'in_progress')
            )
            self.assertEqual(len(job), 1)
            job._process_operation()

    def _prepare_ready_shipment(self):
        self.shipment.action_create_picking()
        self._refresh_amazon_shipment()
        self.shipment.write({
            'carrier_type': 'non_partnered',
            'carrier_name': 'UPS',
            'shipping_method': 'spd',
            'tracking_id': '1Z999PHASE4',
            'ship_date': fields.Date.today(),
        })

    def test_01_picking_is_created_and_fully_reserved_once(self):
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        source_before = self._quantity_at(source)
        sellable_before = self._quantity_at(sellable)

        self.shipment.action_create_picking()

        self.assertEqual(self.shipment.state, 'ready_to_ship')
        self.assertEqual(len(self.shipment.picking_ids), 1)
        self.assertEqual(self.shipment.picking_id, self.shipment.picking_ids)
        picking = self.shipment.picking_ids
        self.assertEqual(picking.picking_type_code, 'internal')
        self.assertEqual(picking.location_id, source)
        self.assertEqual(picking.location_dest_id, transit)
        self.assertEqual(picking.state, 'assigned')
        self.assertEqual(len(picking.move_ids), 1)
        self.assertEqual(picking.move_ids.product_uom_qty, 4)
        self.assertEqual(picking.move_ids.quantity, 4)
        self.assertTrue(picking.move_ids.move_line_ids)
        self.assertEqual(self._quantity_at(source), source_before)
        self.assertEqual(self._quantity_at(transit), 0)
        self.assertEqual(self._quantity_at(sellable), sellable_before)

        with self.assertRaisesRegex(UserError, 'after placement is confirmed|already exists'):
            self.shipment.action_create_picking()
        self.assertEqual(len(self.shipment.picking_ids), 1)

    def test_02_insufficient_stock_creates_no_picking(self):
        shipment = self._create_phase4_shipment('P4-PLAN-002', quantity=20)
        source_before = self._quantity_at(self.instance.fba_source_location_id)

        with self.assertRaisesRegex(UserError, 'Insufficient stock'):
            shipment.action_create_picking()

        self.assertEqual(shipment.state, 'placement_confirmed')
        self.assertFalse(shipment.picking_ids)
        self.assertFalse(shipment.picking_id)
        self.assertEqual(
            self._quantity_at(self.instance.fba_source_location_id), source_before,
        )

    def test_03_placement_is_required(self):
        self.shipment.state = 'packing_confirmed'
        with self.assertRaisesRegex(UserError, 'after placement is confirmed'):
            self.shipment.action_create_picking()
        self.assertFalse(self.shipment.picking_ids)

    def test_04_confirmation_moves_only_source_to_transit_and_tracks(self):
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        source_before = self._quantity_at(source)
        transit_before = self._quantity_at(transit)
        sellable_before = self._quantity_at(sellable)
        self._prepare_ready_shipment()

        self.shipment.action_confirm_shipment()

        picking = self.shipment.picking_ids
        self.assertEqual(picking.state, 'done')
        self.assertEqual(self.shipment.state, 'shipment_confirmed')
        self.assertEqual(self.shipment.shipment_confirmation_status, 'pending')
        self.assertEqual(self.shipment.line_ids.quantity_shipped, 4)
        self.assertEqual(self._quantity_at(source), source_before - 4)
        self.assertEqual(self._quantity_at(transit), transit_before + 4)
        self.assertEqual(self._quantity_at(sellable), sellable_before)

        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_shipment'
        )
        self.assertEqual(len(job), 1)
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'update_shipment_tracking_details', autospec=True,
                return_value={
                    'operationId': TRACKING_OPERATION_ID,
                    '_amazon_request_id': 'phase4-update-tracking-request',
                },
            ) as update_tracking,
        ):
            job._process_operation()
        self.assertEqual(job.operation_id, TRACKING_OPERATION_ID)
        self.assertEqual(job.state, 'in_progress')
        body = update_tracking.call_args.args[-1]
        self.assertEqual(
            body['trackingDetails']['spdTrackingDetail']['spdTrackingItems'],
            [{'boxId': 'FBA10ABC0YY100001', 'trackingId': '1Z999PHASE4'}],
        )

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={
                    'operationId': TRACKING_OPERATION_ID,
                    'operationStatus': 'SUCCESS',
                    'operationProblems': [],
                },
            ),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                return_value=self._get_shipment_response(status='SHIPPED'),
            ),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'done')
        self.assertEqual(self.shipment.state, 'waiting_receiving')
        self.assertEqual(self.shipment.shipment_confirmation_status, 'success')
        self.assertEqual(self.shipment.shipment_confirmation_id, 'FBA1234ABCD')
        self.assertEqual(self.shipment.shipment_status, 'SHIPPED')
        self.assertEqual(self.shipment.tracking_id, '1Z999PHASE4')
        self.assertEqual(self._quantity_at(sellable), sellable_before)

        with self.assertRaisesRegex(UserError, 'already queued or completed'):
            self.shipment.action_confirm_shipment()
        self.assertEqual(self._quantity_at(source), source_before - 4)
        self.assertEqual(self._quantity_at(transit), transit_before + 4)

    def test_05_status_refresh_is_idempotent_and_does_not_move_stock(self):
        self.shipment.action_create_picking()
        source_before = self._quantity_at(self.instance.fba_source_location_id)
        transit_before = self._quantity_at(self.instance.fba_transit_location_id)
        sellable_before = self._quantity_at(self.instance.fba_sellable_location_id)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                return_value=self._get_shipment_response(),
            ),
        ):
            self.shipment.action_refresh_shipment_status()
            self.shipment.action_refresh_shipment_status()
            jobs = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'refresh_shipment_status'
                and item.state in ('pending', 'in_progress')
            )
            self.assertEqual(len(jobs), 1)
            jobs._process_operation()

        self.assertEqual(self.shipment.shipment_confirmation_id, 'FBA1234ABCD')
        self.assertEqual(
            self.shipment.selected_transportation_option_id,
            TRANSPORTATION_OPTION_ID,
        )
        self.assertEqual(self.shipment.destination_fulfillment_center, 'ONT8')
        self.assertEqual(self._quantity_at(self.instance.fba_source_location_id), source_before)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), transit_before)
        self.assertEqual(self._quantity_at(self.instance.fba_sellable_location_id), sellable_before)

    def test_06_failed_confirmation_can_retry_without_moving_twice(self):
        self._prepare_ready_shipment()
        self.shipment.action_confirm_shipment()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_shipment'
        )
        job.max_retries = 1
        source_after_ship = self._quantity_at(self.instance.fba_source_location_id)
        transit_after_ship = self._quantity_at(self.instance.fba_transit_location_id)
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'update_shipment_tracking_details', autospec=True,
                side_effect=RuntimeError('temporary tracking failure'),
            ),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.shipment.shipment_confirmation_status, 'failed')

        self.shipment.action_confirm_shipment()

        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.retry_count, 0)
        self.assertEqual(self.shipment.picking_ids.state, 'done')
        self.assertEqual(
            self._quantity_at(self.instance.fba_source_location_id), source_after_ship,
        )
        self.assertEqual(
            self._quantity_at(self.instance.fba_transit_location_id), transit_after_ship,
        )

    def test_07_ltl_tracking_payload_uses_official_fields(self):
        self.shipment.write({
            'carrier_type': 'non_partnered',
            'shipping_method': 'ltl',
            'pro_number': 'PRO-12345',
            'bill_of_lading_number': 'BOL-67890',
        })
        self.assertEqual(self.shipment._prepare_tracking_details_payload(), {
            'trackingDetails': {
                'ltlTrackingDetail': {
                    'freightBillNumber': ['PRO-12345'],
                    'billOfLadingNumber': 'BOL-67890',
                },
            },
        })

    def test_08_api_wrappers_use_current_official_paths(self):
        api = AmazonAPI()
        response = MagicMock()
        response.headers = {'x-amzn-RequestId': 'phase4-request-id'}
        response.json.return_value = {'operationId': TRACKING_OPERATION_ID}
        payload = {
            'trackingDetails': {
                'ltlTrackingDetail': {'freightBillNumber': ['PRO-12345']},
            },
        }
        with patch.object(api, '_amazon_request', return_value=response) as request:
            result = api.update_shipment_tracking_details(
                self.instance, 'test-token', PLAN_ID, SHIPMENT_ID, payload,
            )
        self.assertEqual(result['_amazon_request_id'], 'phase4-request-id')
        self.assertEqual(request.call_args.args[2], 'PUT')
        self.assertTrue(request.call_args.args[3].endswith(
            '/inbound/fba/2024-03-20/inboundPlans/%s/shipments/%s/trackingDetails'
            % (PLAN_ID, SHIPMENT_ID)
        ))
        self.assertEqual(request.call_args.kwargs['body'], payload)

        response.json.return_value = self._get_shipment_response()
        with patch.object(api, '_amazon_request', return_value=response) as request:
            result = api.get_shipment(
                self.instance, 'test-token', PLAN_ID, SHIPMENT_ID,
            )
        self.assertEqual(result['shipmentId'], SHIPMENT_ID)
        self.assertEqual(request.call_args.args[2], 'GET')
        self.assertTrue(request.call_args.args[3].endswith(
            '/inbound/fba/2024-03-20/inboundPlans/%s/shipments/%s'
            % (PLAN_ID, SHIPMENT_ID)
        ))
