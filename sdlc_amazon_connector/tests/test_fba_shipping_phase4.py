import base64
from unittest.mock import MagicMock, patch

import requests

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
TRANSPORTATION_OPERATION_ID = '88888888-8888-8888-8888-888888888888'
DELIVERY_GENERATE_OPERATION_ID = '99999999-9999-9999-9999-999999999999'
DELIVERY_CONFIRM_OPERATION_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
DELIVERY_OPTION_ID = 'dw1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID_2 = 'sh5678abcd-1234-abcd-5678-1234abcd5678'


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
        token_patcher = patch.object(
            type(self.instance), '_get_access_token_or_raise', return_value='test-token',
        )
        token_patcher.start()
        self.addCleanup(token_patcher.stop)
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
            'packing_information_status': 'success',
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
        placement = shipment.placement_option_ids.filtered('selected')
        physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'placement_option_id': placement.id,
            'amazon_shipment_id': SHIPMENT_ID,
            'shipment_confirmation_id': 'FBA1234ABCD',
            'status': 'WORKING',
            'destination_fc': 'ONT8',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'quantity': quantity,
            })],
        })
        self.env['amazon.fba.shipment.box'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_box_id': 'FBA10ABC0YY100001',
            'quantity': 1,
        })
        shipment.write({'shipment_confirmation_id': physical.shipment_confirmation_id})
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

    def _prepare_dispatched_physical(self):
        physical = self.shipment.physical_shipment_ids
        physical.action_create_dispatch_picking()
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'phase4-test-labels.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF-1.4 phase4 test labels'),
            'mimetype': 'application/pdf',
            'res_model': physical._name,
            'res_id': physical.id,
        })
        physical.write({
            'transportation_confirmation_status': 'success',
            'labels_status': 'success',
            'shipping_label_attachment_id': attachment.id,
            'product_labels_confirmed': True,
        })
        result = physical.picking_id.with_context(
            picking_ids_not_to_backorder=physical.picking_id.ids,
        ).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(physical.dispatch_state, 'dispatched')
        return physical

    @staticmethod
    def _transportation_option(option_id=TRANSPORTATION_OPTION_ID, shipment_id=SHIPMENT_ID):
        return {
            'transportationOptionId': option_id,
            'shipmentId': shipment_id,
            'shippingMode': 'GROUND_SMALL_PARCEL',
            'shippingSolution': 'USE_YOUR_OWN_CARRIER',
            'carrier': {'name': 'UPS', 'alphaCode': 'UPSN'},
            'preconditions': [],
            'quote': {'cost': {'amount': 0.0, 'code': 'USD'}},
        }

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

        action = self.shipment.action_create_picking()
        self.assertEqual(action.get('res_id'), picking.id)
        self.assertEqual(len(self.shipment.picking_ids), 1)

    def test_02_insufficient_stock_creates_unreserved_picking(self):
        shipment = self._create_phase4_shipment('P4-PLAN-002', quantity=20)
        source_before = self._quantity_at(self.instance.fba_source_location_id)

        shipment.action_create_picking()

        self.assertEqual(shipment.state, 'picking_created')
        self.assertEqual(len(shipment.picking_ids), 1)
        self.assertEqual(shipment.picking_id, shipment.picking_ids)
        self.assertNotEqual(shipment.picking_id.state, 'assigned')
        self.assertEqual(
            self._quantity_at(self.instance.fba_source_location_id), source_before,
        )

    def test_03_placement_is_required(self):
        self.shipment.state = 'packing_confirmed'
        with self.assertRaisesRegex(UserError, 'after placement is confirmed'):
            self.shipment.action_create_picking()
        self.assertFalse(self.shipment.picking_ids)

    def test_04_legacy_combined_dispatch_and_tracking_is_disabled(self):
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        sellable = self.instance.fba_sellable_location_id
        source_before = self._quantity_at(source)
        transit_before = self._quantity_at(transit)
        sellable_before = self._quantity_at(sellable)
        self._prepare_ready_shipment()
        with self.assertRaisesRegex(UserError, 'legacy combined'):
            self.shipment.action_confirm_shipment()
        self.assertEqual(self._quantity_at(source), source_before)
        self.assertEqual(self._quantity_at(transit), transit_before)
        self.assertEqual(self._quantity_at(sellable), sellable_before)
        self.assertFalse(self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_shipment'
        ))

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

    def test_06_legacy_combined_action_never_moves_stock(self):
        self._prepare_ready_shipment()
        source_before = self._quantity_at(self.instance.fba_source_location_id)
        transit_before = self._quantity_at(self.instance.fba_transit_location_id)
        with self.assertRaisesRegex(UserError, 'legacy combined'):
            self.shipment.action_confirm_shipment()
        self.assertEqual(self.shipment.picking_ids.state, 'assigned')
        self.assertEqual(self._quantity_at(self.instance.fba_source_location_id), source_before)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), transit_before)

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
        self.assertEqual(request.call_args.kwargs['max_retries'], 0)

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

        response.json.return_value = {'operationId': DELIVERY_GENERATE_OPERATION_ID}
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.generate_delivery_window_options(
                self.instance, 'test-token', PLAN_ID, SHIPMENT_ID,
            )
        self.assertEqual(request.call_args.args[2], 'POST')
        self.assertTrue(request.call_args.args[3].endswith(
            '/shipments/%s/deliveryWindowOptions' % SHIPMENT_ID
        ))
        self.assertEqual(request.call_args.kwargs['max_retries'], 0)

        response.json.return_value = {'deliveryWindowOptions': []}
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.list_delivery_window_options(
                self.instance, 'test-token', PLAN_ID, SHIPMENT_ID, 20, 'next-page',
            )
        self.assertEqual(request.call_args.args[2], 'GET')
        self.assertEqual(request.call_args.kwargs['params']['paginationToken'], 'next-page')

        response.json.return_value = {'operationId': DELIVERY_CONFIRM_OPERATION_ID}
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.confirm_delivery_window_option(
                self.instance, 'test-token', PLAN_ID, SHIPMENT_ID, DELIVERY_OPTION_ID,
            )
        self.assertTrue(request.call_args.args[3].endswith(
            '/deliveryWindowOptions/%s/confirmation' % DELIVERY_OPTION_ID
        ))
        self.assertEqual(request.call_args.kwargs['max_retries'], 0)

        response.json.return_value = {'boxes': []}
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.list_shipment_boxes(self.instance, 'test-token', PLAN_ID, SHIPMENT_ID)
        self.assertTrue(request.call_args.args[3].endswith('/shipments/%s/boxes' % SHIPMENT_ID))

        response.json.return_value = {'payload': {'DownloadURL': 'https://example.test/labels.pdf'}}
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.get_inbound_labels_v0(
                self.instance, 'test-token', 'FBA1234ABCD', 'PackageLabel_A4_2',
                'UNIQUE', 1, ['BOX-1'],
            )
        self.assertTrue(request.call_args.args[3].endswith(
            '/fba/inbound/v0/shipments/FBA1234ABCD/labels'
        ))
        self.assertEqual(request.call_args.kwargs['params']['PackageLabelsToPrint'], ['BOX-1'])

    def test_09_transportation_precedes_physical_dispatch(self):
        physical = self.shipment.physical_shipment_ids
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ):
            physical.action_generate_transportation_options()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_transportation_options'
            )
            job._process_operation()
        self.assertEqual(job.operation_id, TRANSPORTATION_OPERATION_ID)
        self.assertFalse(physical.picking_id)

    def test_10_generate_and_list_transportation_options_are_idempotent(self):
        physical = self.shipment.physical_shipment_ids
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_transportation_options', autospec=True,
                return_value={
                    'operationId': TRANSPORTATION_OPERATION_ID,
                    '_amazon_request_id': 'transport-generate',
                },
            ) as generate_mock,
        ):
            physical.action_generate_transportation_options()
            job = physical.inbound_shipment_id.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_transportation_options'
            )
            self.assertEqual(len(job), 1)
            job._process_operation()
        self.assertEqual(job.operation_id, TRANSPORTATION_OPERATION_ID)
        body = generate_mock.call_args.args[-1]
        self.assertEqual(body['placementOptionId'], PLACEMENT_OPTION_ID)
        self.assertEqual(
            body['shipmentTransportationConfigurations'][0]['shipmentId'], SHIPMENT_ID,
        )
        self.assertIn('readyToShipWindow', body['shipmentTransportationConfigurations'][0])

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'list_transportation_options', autospec=True,
                return_value={
                    'transportationOptions': [
                        self._transportation_option(),
                        self._transportation_option('to5678abcd-1234-abcd-5678-1234abcd5678'),
                    ],
                },
            ),
        ):
            job._process_operation()
            physical.action_refresh_transportation_options()
            physical.action_refresh_transportation_options()
        self.assertEqual(len(physical.transportation_option_ids), 2)
        self.assertEqual(len(physical.transportation_option_ids), 2)

    def test_11_selection_is_one_option_and_does_not_confirm(self):
        physical = self.shipment.physical_shipment_ids
        option_a = self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': TRANSPORTATION_OPTION_ID,
            'shipment_id': SHIPMENT_ID,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'shipping_solution': 'USE_YOUR_OWN_CARRIER',
            'carrier_name': 'UPS',
        })
        option_b = self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': 'to5678abcd-1234-abcd-5678-1234abcd5678',
            'shipment_id': SHIPMENT_ID,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'shipping_solution': 'AMAZON_PARTNERED_CARRIER',
            'carrier_name': 'Amazon Partnered',
        })
        option_a.action_select_transportation_option()
        option_b.action_select_transportation_option()
        self.assertFalse(option_a.selected)
        self.assertTrue(option_b.selected)
        self.assertEqual(physical.selected_transportation_option_id, option_b)
        self.assertFalse(physical.transportation_confirmation_operation_id)

    def test_12_confirm_transportation_payload_and_async_success(self):
        physical = self.shipment.physical_shipment_ids
        option = self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': TRANSPORTATION_OPTION_ID,
            'shipment_id': SHIPMENT_ID,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'shipping_solution': 'USE_YOUR_OWN_CARRIER',
            'carrier_name': 'UPS',
        })
        option.action_select_transportation_option()
        source_before = self._quantity_at(self.instance.fba_source_location_id)
        transit_before = self._quantity_at(self.instance.fba_transit_location_id)
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'confirm_transportation_options', autospec=True,
                return_value={
                    'operationId': TRANSPORTATION_OPERATION_ID,
                    '_amazon_request_id': 'transport-confirm',
                },
            ) as confirm_mock,
        ):
            physical.action_confirm_transportation()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'confirm_transportation_options'
            )
            job._process_operation()
        payload = confirm_mock.call_args.args[-1]
        self.assertEqual(payload['transportationSelections'], [{
            'shipmentId': SHIPMENT_ID,
            'transportationOptionId': TRANSPORTATION_OPTION_ID,
        }])
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                return_value=self._get_shipment_response(status='SHIPPED'),
            ),
        ):
            job._process_operation()
        self.assertEqual(physical.transportation_confirmation_status, 'success')
        self.assertEqual(self._quantity_at(self.instance.fba_source_location_id), source_before)
        self.assertEqual(self._quantity_at(self.instance.fba_transit_location_id), transit_before)
        with self.assertRaisesRegex(UserError, 'already queued or completed'):
            physical.action_confirm_transportation()

    def test_13_transportation_async_in_progress_and_failure(self):
        physical = self.shipment.physical_shipment_ids
        job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'operation_type': 'confirm_transportation_options',
            'operation_id': TRANSPORTATION_OPERATION_ID,
        })
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'IN_PROGRESS', 'operationProblems': []},
            ),
        ):
            job._process_operation()
        self.assertEqual(physical.transportation_confirmation_status, 'in_progress')
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={
                    'operationStatus': 'FAILED',
                    'operationProblems': [{'code': 'INVALID_OPTION', 'message': 'Bad option'}],
                },
            ),
        ):
            job._process_operation()
        self.assertEqual(physical.transportation_confirmation_status, 'failed')
        self.assertIn('Bad option', physical.transportation_error_message)

    def test_14_blank_tracking_blocked_and_payload_is_shipment_level(self):
        physical = self.shipment.physical_shipment_ids
        option = self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': TRANSPORTATION_OPTION_ID,
            'shipment_id': SHIPMENT_ID,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'shipping_solution': 'USE_YOUR_OWN_CARRIER',
            'carrier_name': 'UPS',
        })
        option.action_select_transportation_option()
        physical.write({'transportation_confirmation_status': 'success'})
        with self.assertRaisesRegex(UserError, 'tracking number'):
            physical.action_submit_tracking()
        physical.tracking_number = '1Z999PHASE4'
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'update_shipment_tracking_details', autospec=True,
                return_value={'operationId': TRACKING_OPERATION_ID},
            ) as update_mock,
        ):
            physical.action_submit_tracking()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'submit_transportation_tracking'
            )
            job._process_operation()
        self.assertEqual(update_mock.call_args.args[4], SHIPMENT_ID)
        body = update_mock.call_args.args[-1]
        self.assertEqual(
            body['trackingDetails']['spdTrackingDetail']['spdTrackingItems'][0]['trackingId'],
            '1Z999PHASE4',
        )

    def test_15_required_delivery_window_blocks_transport_until_confirmed(self):
        physical = self.shipment.physical_shipment_ids
        option = physical._sync_transportation_options([{
            'transportationOptionId': TRANSPORTATION_OPTION_ID,
            'shipmentId': SHIPMENT_ID,
            'shippingMode': 'GROUND_SMALL_PARCEL',
            'shippingSolution': 'USE_YOUR_OWN_CARRIER',
            'preconditions': [],
        }])
        self.assertTrue(option.requires_delivery_window)
        option.action_select_transportation_option()
        self.assertTrue(physical.delivery_window_required)
        with self.assertRaisesRegex(UserError, 'Confirm a delivery window'):
            physical.action_confirm_transportation()

        with patch.object(
            AmazonAPI, 'generate_delivery_window_options', autospec=True,
            return_value={'operationId': DELIVERY_GENERATE_OPERATION_ID},
        ):
            physical.action_generate_delivery_window_options()
            generation_job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_delivery_window_options'
            )
            generation_job._process_operation()
        with (
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'list_delivery_window_options', autospec=True,
                return_value={'deliveryWindowOptions': [{
                    'deliveryWindowOptionId': DELIVERY_OPTION_ID,
                    'startDate': '2030-01-01T08:00:00Z',
                    'endDate': '2030-01-01T12:00:00Z',
                    'validUntil': '2030-01-01T00:00:00Z',
                    'availabilityType': 'AVAILABLE',
                }]},
            ),
        ):
            generation_job._process_operation()
        window = physical.delivery_window_option_ids
        window.action_select_delivery_window()
        with patch.object(
            AmazonAPI, 'confirm_delivery_window_option', autospec=True,
            return_value={'operationId': DELIVERY_CONFIRM_OPERATION_ID},
        ) as confirm_mock:
            physical.action_confirm_delivery_window()
            confirmation_job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'confirm_delivery_window_option'
            )
            confirmation_job._process_operation()
        self.assertEqual(confirm_mock.call_args.args[-1], DELIVERY_OPTION_ID)
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
        ):
            confirmation_job._process_operation()
        self.assertEqual(physical.delivery_window_confirmation_status, 'success')

    def test_16_multi_shipment_transportation_is_confirmed_once_at_plan_level(self):
        first = self.shipment.physical_shipment_ids
        second = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'placement_option_id': first.placement_option_id.id,
            'amazon_shipment_id': SHIPMENT_ID_2,
            'shipment_confirmation_id': 'FBA5678EFGH',
            'status': 'WORKING',
            'destination_fc': 'CAI2',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'quantity': 4,
            })],
        })
        options = self.env['amazon.fba.transportation.option'].sudo()
        for physical, option_id in (
            (first, TRANSPORTATION_OPTION_ID),
            (second, 'to5678abcd-1234-abcd-5678-1234abcd5678'),
        ):
            options |= self.env['amazon.fba.transportation.option'].sudo().create({
                'instance_id': self.instance.id,
                'inbound_shipment_id': self.shipment.id,
                'physical_shipment_id': physical.id,
                'amazon_transportation_option_id': option_id,
                'shipment_id': physical.amazon_shipment_id,
                'shipping_mode': 'GROUND_SMALL_PARCEL',
                'shipping_solution': 'USE_YOUR_OWN_CARRIER',
            })
        for option in options:
            option.action_select_transportation_option()
        with patch.object(
            AmazonAPI, 'confirm_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ) as confirm_mock:
            first.action_confirm_transportation()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'confirm_transportation_options'
            )
            job._process_operation()
        selections = confirm_mock.call_args.args[-1]['transportationSelections']
        self.assertEqual({item['shipmentId'] for item in selections}, {SHIPMENT_ID, SHIPMENT_ID_2})
        self.assertEqual(set((first | second).mapped('transportation_confirmation_status')), {'in_progress'})
        with self.assertRaisesRegex(UserError, 'already queued or completed'):
            second.action_confirm_transportation()

    def test_17_shipping_labels_use_confirmation_id_and_official_box_ids(self):
        physical = self.shipment.physical_shipment_ids
        physical.write({'transportation_confirmation_status': 'success'})
        download_response = MagicMock()
        download_response.url = 'https://example.test/labels.pdf'
        download_response.content = b'%PDF-1.4 mocked Amazon labels'
        download_response.headers = {'Content-Type': 'application/pdf'}
        with (
            patch.object(
                AmazonAPI, 'list_shipment_boxes', autospec=True,
                return_value={'boxes': [{
                    'boxId': 'FBA10ABC0YY100001', 'packageId': 'pkg-1', 'quantity': 1,
                }]},
            ),
            patch.object(
                AmazonAPI, 'get_inbound_labels_v0', autospec=True,
                return_value={'payload': {'DownloadURL': 'https://example.test/labels.pdf'}},
            ) as labels_mock,
            patch(
                'odoo.addons.sdlc_amazon_connector.models.amazon_inbound_shipping.requests.get',
                return_value=download_response,
            ) as download_mock,
        ):
            action = physical.action_get_shipping_labels()
            repeated_action = physical.action_get_shipping_labels()
        self.assertEqual(action['url'], repeated_action['url'])
        self.assertEqual(
            action['url'],
            '/web/content/%s?download=true' % physical.shipping_label_attachment_id.id,
        )
        self.assertEqual(labels_mock.call_args.args[3], 'FBA1234ABCD')
        self.assertEqual(labels_mock.call_args.args[-1], ['FBA10ABC0YY100001'])
        self.assertEqual(labels_mock.call_count, 1)
        self.assertEqual(download_mock.call_count, 1)
        self.assertEqual(physical.labels_status, 'success')
        self.assertEqual(physical.shipping_label_attachment_id.mimetype, 'application/pdf')
        self.assertEqual(
            physical.shipping_label_filename,
            'amazon_fba_FBA1234ABCD_box_labels.pdf',
        )
        self.assertFalse(physical.label_download_url)

    def test_18_ambiguous_transport_write_is_not_replayed(self):
        physical = self.shipment.physical_shipment_ids
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            side_effect=UserError('request timed out after submission'),
        ) as generate_mock:
            physical.action_generate_transportation_options()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_transportation_options'
            )
            job._process_operation()
            job._process_operation()
            with self.assertRaisesRegex(UserError, 'unknown Amazon outcome'):
                physical.action_generate_transportation_options()
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(physical.transportation_error_code, 'WRITE_OUTCOME_UNKNOWN')

    def test_19_transport_write_http_failures_are_never_automatically_replayed(self):
        physical = self.shipment.physical_shipment_ids
        for status_code in (400, 403, 409, 500):
            with self.subTest(status_code=status_code):
                physical.write({
                    'transportation_generation_operation_id': False,
                    'transportation_generation_status': False,
                    'transportation_error_code': False,
                    'transportation_error_message': False,
                })
                physical.action_generate_transportation_options()
                job = self.shipment.operation_job_ids.filtered(
                    lambda item: item.operation_type == 'generate_transportation_options'
                    and item.state in ('pending', 'in_progress')
                )
                self.assertEqual(len(job), 1)

                response = requests.Response()
                response.status_code = status_code
                http_error = requests.HTTPError(
                    'HTTP %s' % status_code, response=response,
                )

                def fail_write(*_args, **_kwargs):
                    try:
                        raise http_error
                    except requests.HTTPError as exc:
                        raise UserError('HTTP Status: %s' % status_code) from exc

                with patch.object(
                    AmazonAPI, 'generate_transportation_options', autospec=True,
                    side_effect=fail_write,
                ) as generate_mock:
                    job._process_operation()
                    job._process_operation()

                self.assertEqual(generate_mock.call_count, 1)
                self.assertEqual(job.state, 'failed')
                self.assertEqual(
                    physical.transportation_error_code,
                    'WRITE_OUTCOME_UNKNOWN' if status_code >= 500 else 'BACKGROUND_JOB_FAILED',
                )
                job.unlink()
