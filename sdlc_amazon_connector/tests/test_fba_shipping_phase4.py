import base64
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

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
            'ready_to_ship_at': fields.Datetime.now() + timedelta(hours=1),
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

    def _create_transportation_option(self, physical, option_id=TRANSPORTATION_OPTION_ID,
                                      solution='USE_YOUR_OWN_CARRIER',
                                      mode='GROUND_SMALL_PARCEL'):
        return self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': physical.inbound_shipment_id.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': option_id,
            'shipment_id': physical.amazon_shipment_id,
            'shipping_mode': mode,
            'shipping_solution': solution,
            'carrier_name': 'Test Carrier',
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
                'UNIQUE', 2, ['BOX-1', 'BOX-2'],
            )
        self.assertTrue(request.call_args.args[3].endswith(
            '/fba/inbound/v0/shipments/FBA1234ABCD/labels'
        ))
        self.assertEqual(request.call_args.kwargs['params']['NumberOfPackages'], 2)
        self.assertEqual(request.call_args.kwargs['params']['PackageLabelsToPrint'], 'BOX-1,BOX-2')

    def test_09_transportation_precedes_physical_dispatch(self):
        physical = self.shipment.physical_shipment_ids
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ):
            physical._action_generate_transportation_options()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_transportation_options'
            )
            job._process_operation()
        self.assertEqual(job.operation_id, TRANSPORTATION_OPERATION_ID)
        self.assertFalse(physical.picking_id)

    def test_09a_ready_to_ship_window_requires_explicit_future_value(self):
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        with self.assertRaisesRegex(UserError, 'Ready-to-Ship Date/Time'):
            physical._prepare_transportation_generation_payload()

        physical.ready_to_ship_at = fields.Datetime.now() - timedelta(minutes=1)
        with self.assertRaisesRegex(UserError, 'at least 15 minutes in the future'):
            physical._prepare_transportation_generation_payload()

        physical.ready_to_ship_at = fields.Datetime.now() + timedelta(minutes=5)
        with self.assertRaisesRegex(UserError, 'at least 15 minutes in the future'):
            physical._prepare_transportation_generation_payload()

    def test_09b_ready_to_ship_serializes_user_future_value(self):
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = fields.Datetime.from_string('2030-01-01 08:05:45')

        payload = physical._prepare_transportation_generation_payload()

        config = payload['shipmentTransportationConfigurations'][0]
        self.assertEqual(config['shipmentId'], SHIPMENT_ID)
        self.assertEqual(
            config['readyToShipWindow']['start'],
            '2030-01-01T08:05:00Z',
        )

    def test_09c_invalid_ready_to_ship_blocks_before_amazon_call(self):
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = fields.Datetime.now()

        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
        ) as generate_mock:
            with self.assertRaisesRegex(UserError, 'Ready-to-Ship Date/Time'):
                physical._action_generate_transportation_options()

        self.assertFalse(generate_mock.called)
        self.assertFalse(self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_transportation_options'
        ))

    # ------------------------------------------------------------------
    # Ready-to-Ship gate for transportation generation
    # ------------------------------------------------------------------
    READY_TO_SHIP_REQUIRED = re.escape(
        "Enter a Ready-to-Ship Date/Time before generating transportation options."
    )

    def _ready_to_ship_is_readonly(self, physical):
        """Evaluate the physical shipment form's readonly modifier for ready_to_ship_at."""
        view = self.env.ref('sdlc_amazon_connector.amazon_inbound_shipment_form')
        arch = self.env['amazon.inbound.shipment'].get_view(view.id, 'form')['arch']
        nodes = etree.fromstring(arch).xpath(
            "//field[@name='physical_shipment_ids']//form//field[@name='ready_to_ship_at']"
        )
        self.assertEqual(len(nodes), 1)
        expression = nodes[0].get('readonly')
        self.assertTrue(expression, "ready_to_ship_at must keep its conditional readonly rule")
        return bool(safe_eval(expression, {
            'transportation_generation_status': physical.transportation_generation_status or False,
            'transportation_confirmation_status': physical.transportation_confirmation_status or False,
        }))

    def _generation_jobs(self, states=None):
        jobs = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_transportation_options'
        )
        if states:
            jobs = jobs.filtered(lambda item: item.state in states)
        return jobs

    def test_09d_missing_ready_to_ship_blocks_generation_synchronously(self):
        """TEST A: empty Ready-to-Ship -> UserError, no job, no status change, no Amazon call."""
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        self.assertFalse(self._ready_to_ship_is_readonly(physical))

        with (
            patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock,
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True) as poll_mock,
        ):
            with self.assertRaisesRegex(UserError, self.READY_TO_SHIP_REQUIRED):
                physical._action_generate_transportation_options()

        self.assertFalse(generate_mock.called)
        self.assertFalse(poll_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertFalse(physical.transportation_generation_status)
        self.assertFalse(physical.transportation_generation_operation_id)
        self.assertFalse(physical.transportation_error_code)
        # The operator can still enter the value and generate normally.
        self.assertFalse(self._ready_to_ship_is_readonly(physical))
        physical.ready_to_ship_at = fields.Datetime.now() + timedelta(hours=2)
        physical._action_generate_transportation_options()
        self.assertEqual(physical.transportation_generation_status, 'pending')
        self.assertEqual(len(self._generation_jobs(('pending',))), 1)

    def test_09d2_missing_ready_to_ship_on_sibling_blocks_plan_generation(self):
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
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            with self.assertRaisesRegex(UserError, SHIPMENT_ID_2):
                first._action_generate_transportation_options()
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertFalse(first.transportation_generation_status)
        self.assertFalse(second.transportation_generation_status)

    def test_09d3_missing_ready_to_ship_blocks_regeneration_without_side_effects(self):
        physical = self.shipment.physical_shipment_ids
        option = self._create_transportation_option(physical)
        physical.write({
            'ready_to_ship_at': False,
            'transportation_generation_status': 'failed',
            'transportation_error_code': 'BACKGROUND_JOB_FAILED',
            'transportation_error_message': 'Previous failure.',
        })
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            with self.assertRaisesRegex(UserError, self.READY_TO_SHIP_REQUIRED):
                physical._action_regenerate_transportation_options()
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertEqual(physical.transportation_generation_status, 'failed')
        self.assertEqual(physical.transportation_error_code, 'BACKGROUND_JOB_FAILED')
        self.assertTrue(option.exists())
        self.assertFalse(self._ready_to_ship_is_readonly(physical))

    def test_09e_generation_with_ready_to_ship_follows_normal_flow(self):
        """TEST B: valid Ready-to-Ship -> queued -> in_progress -> success."""
        physical = self.shipment.physical_shipment_ids
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ) as generate_mock:
            physical._action_generate_transportation_options()
            # Queuing never calls Amazon synchronously.
            self.assertFalse(generate_mock.called)
            self.assertEqual(physical.transportation_generation_status, 'pending')
            self.assertTrue(self._ready_to_ship_is_readonly(physical))
            job = self._generation_jobs(('pending',))
            self.assertEqual(len(job), 1)
            job._process_operation()
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(physical.transportation_generation_status, 'in_progress')
        self.assertEqual(physical.transportation_generation_operation_id, TRANSPORTATION_OPERATION_ID)
        self.assertTrue(self._ready_to_ship_is_readonly(physical))

        with (
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'list_transportation_options', autospec=True,
                return_value={'transportationOptions': [self._transportation_option()]},
            ),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'done')
        self.assertEqual(physical.transportation_generation_status, 'success')
        self.assertEqual(len(physical.transportation_option_ids), 1)
        self.assertTrue(self._ready_to_ship_is_readonly(physical))

    def test_09f_prewrite_job_failure_is_recoverable_and_not_ambiguous(self):
        """TEST C: a generation that failed before reaching Amazon can be corrected and retried."""
        physical = self.shipment.physical_shipment_ids
        for label, stale_value in (
            ('missing', False),
            ('lead time elapsed', fields.Datetime.now() + timedelta(minutes=1)),
        ):
            with self.subTest(label):
                physical.write({
                    'ready_to_ship_at': fields.Datetime.now() + timedelta(hours=1),
                    'transportation_generation_operation_id': False,
                    'transportation_generation_status': False,
                    'transportation_error_code': False,
                    'transportation_error_message': False,
                })
                self._generation_jobs().unlink()
                physical._action_generate_transportation_options()
                job = self._generation_jobs(('pending',))
                self.assertEqual(len(job), 1)
                # The value became invalid before the worker ran (e.g. the lead
                # time elapsed while the job waited in the queue).
                physical.ready_to_ship_at = stale_value

                with patch.object(
                    AmazonAPI, 'generate_transportation_options', autospec=True,
                ) as generate_mock:
                    job._process_operation()
                    job._process_operation()
                self.assertFalse(generate_mock.called)
                self.assertEqual(job.state, 'failed')
                self.assertFalse(job.operation_id)
                self.assertEqual(physical.transportation_generation_status, 'failed')
                self.assertEqual(physical.transportation_error_code, 'PRE_WRITE_FAILED')
                self.assertIn('Ready-to-Ship Date/Time', physical.transportation_error_message)
                self.assertFalse(physical.transportation_generation_operation_id)
                # Field is editable again and retry is offered through Regenerate.
                self.assertFalse(self._ready_to_ship_is_readonly(physical))
                with self.assertRaisesRegex(UserError, 'Use Regenerate'):
                    physical._action_generate_transportation_options()

                corrected = fields.Datetime.from_string('2030-02-01 10:30:00')
                physical.ready_to_ship_at = corrected
                physical._action_regenerate_transportation_options()
                self.assertEqual(physical.transportation_generation_status, 'pending')
                self.assertFalse(physical.transportation_error_code)
                retry = self._generation_jobs(('pending', 'in_progress'))
                self.assertEqual(len(retry), 1)
                # Duplicate retry requests are still refused.
                with self.assertRaisesRegex(UserError, 'already queued or in progress'):
                    physical._action_regenerate_transportation_options()
                with patch.object(
                    AmazonAPI, 'generate_transportation_options', autospec=True,
                    return_value={'operationId': TRANSPORTATION_OPERATION_ID},
                ) as retry_mock:
                    retry._process_operation()
                self.assertEqual(retry_mock.call_count, 1)
                body = retry_mock.call_args.args[-1]
                self.assertEqual(
                    body['shipmentTransportationConfigurations'][0]['readyToShipWindow']['start'],
                    '2030-02-01T10:30:00Z',
                )
                self.assertEqual(physical.transportation_generation_status, 'in_progress')
                self.assertEqual(
                    physical.transportation_generation_operation_id, TRANSPORTATION_OPERATION_ID,
                )

    def test_09f2_ambiguous_amazon_write_still_blocks_retry(self):
        """A failure raised by the Amazon write itself keeps the no-replay protection."""
        physical = self.shipment.physical_shipment_ids
        physical._action_generate_transportation_options()
        job = self._generation_jobs(('pending',))
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            side_effect=UserError('request timed out after submission'),
        ) as generate_mock:
            job._process_operation()
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(physical.transportation_error_code, 'WRITE_OUTCOME_UNKNOWN')
        with self.assertRaisesRegex(UserError, 'unknown Amazon outcome'):
            physical._action_regenerate_transportation_options()

    def test_09g_ready_to_ship_locked_while_generation_active_or_succeeded(self):
        """TEST D: the field is locked once generation legitimately started or succeeded."""
        physical = self.shipment.physical_shipment_ids
        for generation, confirmation, locked in (
            (False, False, False),
            ('failed', False, False),
            ('pending', False, True),
            ('in_progress', False, True),
            ('success', False, True),
            ('success', 'pending', True),
            ('success', 'in_progress', True),
            ('success', 'success', True),
        ):
            with self.subTest(generation=generation, confirmation=confirmation):
                physical.write({
                    'transportation_generation_status': generation,
                    'transportation_confirmation_status': confirmation,
                })
                self.assertEqual(self._ready_to_ship_is_readonly(physical), locked)

        # Server side: no new generation may start while one is active, so the
        # Ready-to-Ship value used by the queued request cannot be superseded.
        physical.write({'transportation_confirmation_status': False})
        original = physical.ready_to_ship_at
        for status in ('pending', 'in_progress'):
            with self.subTest(status=status):
                physical.write({'transportation_generation_status': status})
                with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
                    with self.assertRaises(UserError):
                        physical.action_generate_transportation_options()
                    with self.assertRaisesRegex(UserError, 'already queued or in progress'):
                        physical.action_regenerate_transportation_options()
                self.assertFalse(generate_mock.called)
                self.assertFalse(self._generation_jobs())
                self.assertEqual(physical.transportation_generation_status, status)
                self.assertEqual(physical.ready_to_ship_at, original)

    # ------------------------------------------------------------------
    # Ready-to-Ship wizard (UI entry point for Generate / Regenerate)
    # ------------------------------------------------------------------
    WIZARD_MODEL = 'amazon.fba.transportation.ready.to.ship.wizard'

    def _assert_wizard_action(self, action, physical, operation_type):
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], self.WIZARD_MODEL)
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['context']['default_physical_shipment_id'], physical.id)
        self.assertEqual(action['context']['default_operation_type'], operation_type)

    def _wizard_from_action(self, action, **vals):
        """Create the wizard the way the web client does (context defaults)."""
        return self.env[self.WIZARD_MODEL].with_context(**action['context']).create(vals)

    def _add_second_physical(self, ready_to_ship_at=False):
        first = self.shipment.physical_shipment_ids
        return self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'placement_option_id': first.placement_option_id.id,
            'amazon_shipment_id': SHIPMENT_ID_2,
            'shipment_confirmation_id': 'FBA5678EFGH',
            'status': 'WORKING',
            'destination_fc': 'CAI2',
            'ready_to_ship_at': ready_to_ship_at,
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'quantity': 4,
            })],
        })

    def test_20_generate_button_opens_ready_to_ship_wizard(self):
        """TEST 1: Generate opens the wizard; nothing is queued or sent yet."""
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            action = physical.action_generate_transportation_options()
        self._assert_wizard_action(action, physical, 'generate')
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertFalse(physical.transportation_generation_status)
        self.assertFalse(physical.transportation_generation_operation_id)
        wizard = self._wizard_from_action(action)
        self.assertEqual(wizard.physical_shipment_id, physical)
        self.assertEqual(wizard.shipment_count, 1)
        self.assertFalse(wizard.ready_to_ship_at)

    def test_21_wizard_requires_ready_to_ship(self):
        """TEST 2: confirming without a value fails without side effects."""
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        action = physical.action_generate_transportation_options()
        # Client side: the field is required in the wizard form.
        form = Form(self.env[self.WIZARD_MODEL].with_context(**action['context']))
        with self.assertRaises(AssertionError):
            form.save()
        # Server side: the existing validation still blocks the confirmation.
        wizard = self._wizard_from_action(action)
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            with self.assertRaisesRegex(UserError, self.READY_TO_SHIP_REQUIRED):
                wizard.action_confirm()
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertFalse(physical.transportation_generation_status)
        self.assertFalse(physical.ready_to_ship_at)

    def test_22_wizard_rejects_past_or_too_close_ready_to_ship(self):
        """TEST 3: the existing lead-time rule is enforced before anything is saved."""
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        action = physical.action_generate_transportation_options()
        for value in (
            fields.Datetime.now() - timedelta(hours=1),
            fields.Datetime.now() + timedelta(minutes=5),
        ):
            with self.subTest(value=value):
                wizard = self._wizard_from_action(action, ready_to_ship_at=value)
                with patch.object(
                    AmazonAPI, 'generate_transportation_options', autospec=True,
                ) as generate_mock:
                    with self.assertRaisesRegex(UserError, 'at least 15 minutes in the future'):
                        wizard.action_confirm()
                self.assertFalse(generate_mock.called)
                self.assertFalse(self._generation_jobs())
                self.assertFalse(physical.transportation_generation_status)
                self.assertFalse(physical.ready_to_ship_at)

    def test_23_wizard_generate_saves_value_and_runs_existing_flow(self):
        """TEST 4: valid value -> saved -> pending -> in_progress -> success."""
        physical = self.shipment.physical_shipment_ids
        physical.ready_to_ship_at = False
        entered = fields.Datetime.from_string('2030-03-01 09:45:30')
        action = physical.action_generate_transportation_options()
        form = Form(self.env[self.WIZARD_MODEL].with_context(**action['context']))
        form.ready_to_ship_at = entered
        wizard = form.save()
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            result = wizard.action_confirm()
        self.assertFalse(generate_mock.called, "Confirmation only queues; Amazon is called by the job")
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['next'], {'type': 'ir.actions.act_window_close'})
        self.assertEqual(physical.ready_to_ship_at, entered)
        self.assertEqual(physical.transportation_generation_status, 'pending')
        job = self._generation_jobs(('pending',))
        self.assertEqual(len(job), 1)

        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ) as generate_mock:
            job._process_operation()
        self.assertEqual(generate_mock.call_count, 1)
        body = generate_mock.call_args.args[-1]
        self.assertEqual(
            body['shipmentTransportationConfigurations'][0]['readyToShipWindow']['start'],
            '2030-03-01T09:45:00Z',
        )
        self.assertEqual(physical.transportation_generation_status, 'in_progress')

        with (
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'list_transportation_options', autospec=True,
                return_value={'transportationOptions': [self._transportation_option()]},
            ),
        ):
            job._process_operation()
        self.assertEqual(physical.transportation_generation_status, 'success')
        self.assertEqual(len(physical.transportation_option_ids), 1)
        self.assertTrue(self._ready_to_ship_is_readonly(physical))

    def test_24_wizard_regenerate_prefills_and_sends_new_value(self):
        """TEST 5: Regenerate pre-fills the current value; the changed value is sent."""
        physical = self.shipment.physical_shipment_ids
        previous = fields.Datetime.from_string('2030-01-01 08:00:00')
        option = self._create_transportation_option(physical)
        physical.write({
            'ready_to_ship_at': previous,
            'transportation_generation_status': 'failed',
            'transportation_error_code': 'BACKGROUND_JOB_FAILED',
            'transportation_error_message': 'Previous failure.',
        })
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            action = physical.action_regenerate_transportation_options()
        self._assert_wizard_action(action, physical, 'regenerate')
        self.assertFalse(generate_mock.called)
        self.assertEqual(physical.transportation_generation_status, 'failed')
        self.assertTrue(option.exists(), "Opening the wizard must not discard options")

        form = Form(self.env[self.WIZARD_MODEL].with_context(**action['context']))
        self.assertEqual(form.ready_to_ship_at, previous)
        changed = fields.Datetime.from_string('2030-01-05 14:20:00')
        form.ready_to_ship_at = changed
        form.save().action_confirm()

        self.assertEqual(physical.ready_to_ship_at, changed)
        self.assertEqual(physical.transportation_generation_status, 'pending')
        self.assertFalse(physical.transportation_error_code)
        self.assertFalse(option.exists())
        job = self._generation_jobs(('pending',))
        self.assertEqual(len(job), 1)
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ) as generate_mock:
            job._process_operation()
        self.assertEqual(
            generate_mock.call_args.args[-1]['shipmentTransportationConfigurations'][0]
            ['readyToShipWindow']['start'],
            '2030-01-05T14:20:00Z',
        )

    def test_25_wizard_confirmation_runs_internal_logic_without_recursion(self):
        """TEST 6: confirmation calls the internal methods, never the wizard-opening buttons."""
        physical = self.shipment.physical_shipment_ids
        Physical = type(physical)
        for operation_type, internal_name in (
            ('generate', '_action_generate_transportation_options'),
            ('regenerate', '_action_regenerate_transportation_options'),
        ):
            with self.subTest(operation_type=operation_type):
                self._generation_jobs().unlink()
                physical.write({
                    'transportation_generation_status': (
                        'failed' if operation_type == 'regenerate' else False
                    ),
                    'transportation_generation_operation_id': False,
                    'transportation_error_code': False,
                })
                action = (
                    physical.action_regenerate_transportation_options()
                    if operation_type == 'regenerate'
                    else physical.action_generate_transportation_options()
                )
                wizard = self._wizard_from_action(
                    action, ready_to_ship_at=fields.Datetime.now() + timedelta(hours=3),
                )
                original = getattr(Physical, internal_name)
                with (
                    patch.object(
                        Physical, 'action_generate_transportation_options',
                        side_effect=AssertionError('wizard re-opened'),
                    ),
                    patch.object(
                        Physical, 'action_regenerate_transportation_options',
                        side_effect=AssertionError('wizard re-opened'),
                    ),
                    patch.object(
                        Physical, internal_name, autospec=True, side_effect=original,
                    ) as internal_mock,
                    patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock,
                ):
                    result = wizard.action_confirm()
                self.assertEqual(internal_mock.call_count, 1)
                self.assertFalse(generate_mock.called)
                self.assertEqual(result['type'], 'ir.actions.client')
                self.assertNotEqual(result.get('res_model'), self.WIZARD_MODEL)
                self.assertEqual(physical.transportation_generation_status, 'pending')
                self.assertEqual(len(self._generation_jobs(('pending',))), 1)

    def test_26_wizard_keeps_existing_duplicate_generation_protection(self):
        """TEST 7: pending / in_progress / success generation cannot be started again."""
        physical = self.shipment.physical_shipment_ids
        original = physical.ready_to_ship_at
        stale_action = physical.action_generate_transportation_options()
        for status in ('pending', 'in_progress', 'success'):
            with self.subTest(status=status):
                physical.write({'transportation_generation_status': status})
                with patch.object(
                    AmazonAPI, 'generate_transportation_options', autospec=True,
                ) as generate_mock:
                    with self.assertRaisesRegex(UserError, 'already generated'):
                        physical.action_generate_transportation_options()
                    # A wizard opened before another operator queued the
                    # generation cannot bypass the protection either.
                    wizard = self._wizard_from_action(
                        stale_action, ready_to_ship_at=fields.Datetime.now() + timedelta(days=2),
                    )
                    with self.assertRaisesRegex(UserError, 'already generated'):
                        wizard.action_confirm()
                    if status in ('pending', 'in_progress'):
                        with self.assertRaisesRegex(UserError, 'already queued or in progress'):
                            physical.action_regenerate_transportation_options()
                self.assertFalse(generate_mock.called)
                self.assertFalse(self._generation_jobs())
                self.assertEqual(physical.transportation_generation_status, status)
                self.assertEqual(physical.ready_to_ship_at, original)

    def test_27_wizard_handles_every_accepted_physical_shipment(self):
        """TEST 8: plan-level generation collects one Ready-to-Ship value per shipment."""
        first = self.shipment.physical_shipment_ids
        first_original = first.ready_to_ship_at
        second = self._add_second_physical()
        action = first.action_generate_transportation_options()
        wizard = self._wizard_from_action(action)
        self.assertEqual(wizard.shipment_count, 2)
        self.assertEqual(
            set(wizard.line_ids.mapped('amazon_shipment_id')), {SHIPMENT_ID, SHIPMENT_ID_2},
        )
        line_2 = wizard.line_ids.filtered(lambda line: line.physical_shipment_id == second)
        self.assertEqual(line_2.shipment_confirmation_id, 'FBA5678EFGH')
        self.assertEqual(line_2.destination_fc, 'CAI2')
        self.assertFalse(line_2.ready_to_ship_at)

        # Missing value on one shipment blocks everything; nothing is written.
        line_1 = wizard.line_ids.filtered(lambda line: line.physical_shipment_id == first)
        line_1.ready_to_ship_at = fields.Datetime.from_string('2030-04-01 07:00:00')
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            with self.assertRaisesRegex(UserError, SHIPMENT_ID_2):
                wizard.action_confirm()
            # Too-close value on one shipment is also reported per shipment.
            line_2.ready_to_ship_at = fields.Datetime.now() + timedelta(minutes=5)
            with self.assertRaisesRegex(UserError, '%s.*at least 15 minutes' % SHIPMENT_ID_2):
                wizard.action_confirm()
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())
        self.assertEqual(first.ready_to_ship_at, first_original)
        self.assertFalse(second.ready_to_ship_at)
        self.assertFalse(first.transportation_generation_status)
        self.assertFalse(second.transportation_generation_status)

        # Both values filled through the wizard form -> each reaches Amazon.
        form = Form(self.env[self.WIZARD_MODEL].with_context(**action['context']))
        values = {
            SHIPMENT_ID: fields.Datetime.from_string('2030-04-01 07:00:00'),
            SHIPMENT_ID_2: fields.Datetime.from_string('2030-04-02 16:30:00'),
        }
        for index in range(len(form.line_ids)):
            with form.line_ids.edit(index) as line:
                line.ready_to_ship_at = values[line.amazon_shipment_id]
        form.save().action_confirm()
        self.assertEqual(first.ready_to_ship_at, values[SHIPMENT_ID])
        self.assertEqual(second.ready_to_ship_at, values[SHIPMENT_ID_2])
        self.assertEqual(first.transportation_generation_status, 'pending')
        self.assertEqual(second.transportation_generation_status, 'pending')
        job = self._generation_jobs(('pending',))
        self.assertEqual(len(job), 1)
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={'operationId': TRANSPORTATION_OPERATION_ID},
        ) as generate_mock:
            job._process_operation()
        self.assertEqual(generate_mock.call_count, 1)
        sent = {
            config['shipmentId']: config['readyToShipWindow']['start']
            for config in generate_mock.call_args.args[-1]['shipmentTransportationConfigurations']
        }
        self.assertEqual(sent, {
            SHIPMENT_ID: '2030-04-01T07:00:00Z',
            SHIPMENT_ID_2: '2030-04-02T16:30:00Z',
        })

    def test_28_wizard_is_restricted_to_amazon_managers(self):
        model = self.env['ir.model']._get(self.WIZARD_MODEL)
        groups = self.env['ir.model.access'].search([('model_id', '=', model.id)]).group_id
        self.assertEqual(groups, self.env.ref('sdlc_amazon_connector.group_amazon_manager'))

    def test_29_wizard_models_are_registered_with_manager_access(self):
        """Regression: the wizard models must be loaded from this module, reflected
        in ir.model with their generated external ids, and reachable by Amazon
        managers only (the error seen when the database was not upgraded was
        "No group currently allows this operation")."""
        line_model = self.WIZARD_MODEL + '.line'
        manager_group = self.env.ref('sdlc_amazon_connector.group_amazon_manager')
        for model_name, xmlid in (
            (self.WIZARD_MODEL, 'sdlc_amazon_connector.model_amazon_fba_transportation_ready_to_ship_wizard'),
            (line_model, 'sdlc_amazon_connector.model_amazon_fba_transportation_ready_to_ship_wizard_line'),
        ):
            with self.subTest(model=model_name):
                # Python class loaded through sdlc_amazon_connector/wizard/__init__.py
                self.assertIn(model_name, self.env.registry)
                Model = self.env[model_name]
                self.assertTrue(Model._transient)
                self.assertEqual(Model._module, 'sdlc_amazon_connector')
                self.assertIn(
                    'odoo.addons.sdlc_amazon_connector.wizard.transportation_ready_to_ship_wizard',
                    [cls.__module__ for cls in type(Model).__mro__],
                )
                # Reflected in the database with the generated external id.
                ir_model = self.env['ir.model']._get(model_name)
                self.assertTrue(ir_model, "%s is missing from ir.model" % model_name)
                self.assertEqual(self.env.ref(xmlid), ir_model)
                # Access rights resolve to the Amazon manager group only.
                accesses = self.env['ir.model.access'].search([('model_id', '=', ir_model.id)])
                self.assertEqual(accesses.group_id, manager_group)
                self.assertTrue(all(
                    access.perm_read and access.perm_write and access.perm_create and access.perm_unlink
                    for access in accesses
                ))
                self.assertFalse(accesses.filtered(lambda access: not access.group_id))

        # Relational fields point at the exact model names.
        wizard_fields = self.env[self.WIZARD_MODEL]._fields
        self.assertEqual(wizard_fields['line_ids'].comodel_name, line_model)
        self.assertEqual(wizard_fields['physical_shipment_id'].comodel_name, 'amazon.fba.physical.shipment')
        self.assertEqual(self.env[line_model]._fields['wizard_id'].comodel_name, self.WIZARD_MODEL)

        # Real users: a manager can open and fill the wizard, an Amazon user cannot.
        Users = self.env['res.users'].with_context(no_reset_password=True)
        companies = [Command.set((self.company | self.env.company).ids)]
        manager = Users.create({
            'name': 'Wizard Manager', 'login': 'rts_wizard_manager',
            'company_id': self.company.id, 'company_ids': companies,
            'group_ids': [Command.set([self.env.ref('base.group_user').id, manager_group.id])],
        })
        amazon_user = Users.create({
            'name': 'Wizard Amazon User', 'login': 'rts_wizard_user',
            'company_id': self.company.id, 'company_ids': companies,
            'group_ids': [Command.set([
                self.env.ref('base.group_user').id,
                self.env.ref('sdlc_amazon_connector.group_amazon_user').id,
            ])],
        })
        physical = self.shipment.physical_shipment_ids
        physical_as_manager = physical.with_user(manager).with_company(self.company)
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            action = physical_as_manager.action_generate_transportation_options()
            wizard = self.env[self.WIZARD_MODEL].with_user(manager).with_company(self.company) \
                .with_context(**action['context']).create({})
            self.assertEqual(wizard.physical_shipment_id, physical)
            self.assertEqual(wizard.read(['ready_to_ship_at'])[0]['ready_to_ship_at'], physical.ready_to_ship_at)
        self.assertFalse(generate_mock.called)
        self.assertFalse(self._generation_jobs())

        with self.assertRaises(AccessError):
            physical.with_user(amazon_user).with_company(self.company).action_generate_transportation_options()
        with self.assertRaises(AccessError):
            self.env[self.WIZARD_MODEL].with_user(amazon_user).with_company(self.company).create({
                'physical_shipment_id': physical.id,
            })

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
            physical._action_generate_transportation_options()
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
        self.assertEqual(
            body['shipmentTransportationConfigurations'][0]['readyToShipWindow']['start'],
            physical.ready_to_ship_at.replace(second=0, microsecond=0).isoformat() + 'Z',
        )

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

    def test_10a_normal_generate_cannot_duplicate_successful_generation(self):
        physical = self.shipment.physical_shipment_ids
        physical.write({
            'transportation_generation_status': 'success',
            'transportation_generation_operation_id': TRANSPORTATION_OPERATION_ID,
        })
        with self.assertRaisesRegex(UserError, 'already generated'):
            physical._action_generate_transportation_options()

    def test_10b_regeneration_replaces_unconfirmed_options_and_uses_updated_ready_time(self):
        physical = self.shipment.physical_shipment_ids
        old_job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'operation_type': 'generate_transportation_options',
            'operation_id': '77777777-7777-7777-7777-777777777777',
            'state': 'done',
            'response_data': '{"oldGeneration": true}',
        })
        physical.write({
            'ready_to_ship_at': fields.Datetime.from_string('2030-01-01 08:00:00'),
            'transportation_generation_status': 'success',
            'transportation_generation_operation_id': old_job.operation_id,
            'transportation_response': '{"oldGeneration": true}',
        })
        old_a = self._create_transportation_option(
            physical, 'to-old-aaaa-1234-abcd-5678-1234abcd5678',
        )
        old_b = self._create_transportation_option(
            physical, 'to-old-bbbb-1234-abcd-5678-1234abcd5678',
            solution='AMAZON_PARTNERED_CARRIER',
        )
        old_a.action_select_transportation_option()
        delivery_window = self.env['amazon.fba.delivery.window.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_delivery_window_option_id': DELIVERY_OPTION_ID,
            'start_date': fields.Datetime.from_string('2030-01-02 08:00:00'),
            'end_date': fields.Datetime.from_string('2030-01-02 12:00:00'),
            'valid_until': fields.Datetime.from_string('2030-01-01 08:00:00'),
            'availability_type': 'AVAILABLE',
            'selected': True,
        })
        physical.write({
            'selected_delivery_window_option_id': delivery_window.id,
            'delivery_window_generation_status': 'success',
            'delivery_window_generation_operation_id': DELIVERY_GENERATE_OPERATION_ID,
        })

        before_pickings = self.env['stock.picking'].search_count([])
        before_moves = self.env['stock.move'].search_count([])
        before_sales = self.env['sale.order'].search_count([])
        before_accounting = self.env['account.move'].search_count([])
        with patch.object(AmazonAPI, 'generate_transportation_options', autospec=True) as generate_mock:
            physical._action_regenerate_transportation_options()

        self.assertFalse(generate_mock.called)
        self.assertFalse(old_a.exists())
        self.assertFalse(old_b.exists())
        self.assertFalse(delivery_window.exists())
        self.assertFalse(physical.selected_transportation_option_id)
        self.assertFalse(physical.delivery_window_required)
        self.assertFalse(physical.selected_delivery_window_option_id)
        self.assertEqual(physical.transportation_generation_status, 'pending')
        self.assertFalse(physical.transportation_generation_operation_id)
        self.assertIn('oldGeneration', physical.transportation_response)
        self.assertTrue(old_job.exists())
        self.assertEqual(self.env['stock.picking'].search_count([]), before_pickings)
        self.assertEqual(self.env['stock.move'].search_count([]), before_moves)
        self.assertEqual(self.env['sale.order'].search_count([]), before_sales)
        self.assertEqual(self.env['account.move'].search_count([]), before_accounting)

        jobs = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_transportation_options'
            and item.state in ('pending', 'in_progress')
        )
        self.assertEqual(len(jobs), 1)
        physical.ready_to_ship_at = fields.Datetime.from_string('2030-01-03 09:17:45')
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            return_value={
                'operationId': TRANSPORTATION_OPERATION_ID,
                '_amazon_request_id': 'transport-regenerate',
            },
        ) as regenerate_mock:
            jobs._process_operation()
        payload = regenerate_mock.call_args.args[-1]
        self.assertEqual(
            payload['shipmentTransportationConfigurations'][0]['readyToShipWindow']['start'],
            '2030-01-03T09:17:00Z',
        )
        self.assertEqual(physical.transportation_generation_operation_id, TRANSPORTATION_OPERATION_ID)
        with (
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value={'operationStatus': 'SUCCESS', 'operationProblems': []},
            ),
            patch.object(
                AmazonAPI, 'list_transportation_options', autospec=True,
                return_value={'transportationOptions': [
                    self._transportation_option('to-new-aaaa-1234-abcd-5678-1234abcd5678'),
                    self._transportation_option('to-new-bbbb-1234-abcd-5678-1234abcd5678'),
                ]},
            ),
        ):
            jobs._process_operation()
        self.assertEqual(physical.transportation_generation_status, 'success')
        self.assertEqual(
            set(physical.transportation_option_ids.mapped('amazon_transportation_option_id')),
            {
                'to-new-aaaa-1234-abcd-5678-1234abcd5678',
                'to-new-bbbb-1234-abcd-5678-1234abcd5678',
            },
        )

    def test_10c_regeneration_allowed_after_failed_generation_when_safe(self):
        physical = self.shipment.physical_shipment_ids
        physical.write({
            'transportation_generation_status': 'failed',
            'transportation_error_code': 'BACKGROUND_JOB_FAILED',
            'transportation_error_message': 'Previous synchronous rejection.',
        })
        physical._action_regenerate_transportation_options()
        self.assertEqual(physical.transportation_generation_status, 'pending')
        self.assertFalse(physical.transportation_error_code)
        self.assertEqual(len(self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_transportation_options'
            and item.state in ('pending', 'in_progress')
        )), 1)

    def test_10d_regeneration_duplicate_job_is_blocked(self):
        physical = self.shipment.physical_shipment_ids
        physical.write({'transportation_generation_status': 'success'})
        physical._action_regenerate_transportation_options()
        with self.assertRaisesRegex(UserError, 'already queued or in progress'):
            physical._action_regenerate_transportation_options()
        self.assertEqual(len(self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_transportation_options'
            and item.state in ('pending', 'in_progress')
        )), 1)

    def test_10e_transportation_refresh_removes_stale_unconfirmed_options(self):
        physical = self.shipment.physical_shipment_ids
        option_a = self._create_transportation_option(
            physical, 'to-old-aaaa-1234-abcd-5678-1234abcd5678',
        )
        option_b = self._create_transportation_option(
            physical, 'to-old-bbbb-1234-abcd-5678-1234abcd5678',
        )
        option_b.action_select_transportation_option()
        physical._sync_transportation_options([
            self._transportation_option('to-old-aaaa-1234-abcd-5678-1234abcd5678'),
        ])
        self.assertTrue(option_a.exists())
        self.assertFalse(option_b.exists())
        self.assertFalse(physical.selected_transportation_option_id)

        physical._sync_transportation_options([])
        self.assertFalse(option_a.exists())
        self.assertFalse(physical.transportation_option_ids)

    def test_10f_regeneration_blocks_after_commitment_or_later_workflow(self):
        cases = [
            ('transportation_confirmation_status', 'pending', 'confirmation starts'),
            ('transportation_confirmation_status', 'in_progress', 'confirmation starts'),
            ('transportation_confirmation_status', 'success', 'confirmation starts'),
            ('transportation_confirmation_operation_id', TRANSPORTATION_OPERATION_ID, 'already been submitted'),
            ('delivery_window_generation_status', 'pending', 'Delivery-window generation'),
            ('delivery_window_generation_status', 'in_progress', 'Delivery-window generation'),
            ('delivery_window_confirmation_status', 'success', 'Delivery-window confirmation'),
            ('tracking_status', 'success', 'Tracking has already started'),
            ('labels_status', 'success', 'Shipping labels already exist'),
            ('product_labels_confirmed', True, 'Product-label confirmation'),
            ('dispatch_state', 'picking_created', 'Dispatch has already started'),
        ]
        physical = self.shipment.physical_shipment_ids
        reset_values = {
            'transportation_generation_status': 'success',
            'transportation_generation_operation_id': False,
            'transportation_confirmation_status': False,
            'transportation_confirmation_operation_id': False,
            'delivery_window_generation_status': False,
            'delivery_window_generation_operation_id': False,
            'delivery_window_confirmation_status': False,
            'delivery_window_confirmation_operation_id': False,
            'tracking_status': False,
            'labels_status': False,
            'shipping_label_attachment_id': False,
            'product_labels_confirmed': False,
            'dispatch_state': 'placement_confirmed',
        }
        for field_name, value, message in cases:
            physical.write(reset_values)
            physical.write({field_name: value})
            with self.assertRaisesRegex(UserError, message):
                physical._action_regenerate_transportation_options()

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
        # SPD path: authoritative box IDs sent as PackageLabelsToPrint, no pagination.
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

    def test_17a_non_partnered_ltl_labels_blocked_without_live_call(self):
        """Non-Partnered LTL/FTL box labels are blocked before any Amazon call.

        Amazon cannot generate carton/carrier labels for a freight shipment set
        up without pallet/freight information (the connector does not submit it),
        so the request is refused with an actionable error and no live call.
        """
        physical = self.shipment.physical_shipment_ids
        physical.write({
            'transportation_confirmation_status': 'success',
            'shipping_mode': 'FREIGHT_LTL',
            'carrier_type': 'non_partnered',
        })
        self.assertTrue(physical._is_non_partnered_ltl())
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise', return_value='test-token',
            ) as token_mock,
            patch.object(AmazonAPI, 'list_shipment_boxes', autospec=True) as boxes_mock,
            patch.object(AmazonAPI, 'get_inbound_labels_v0', autospec=True) as labels_mock,
        ):
            with self.assertRaisesRegex(UserError, 'Non-Partnered LTL/FTL'):
                physical.action_get_shipping_labels()
        # Zero live Amazon work: no boxes refresh, no getLabels, no token fetch.
        self.assertEqual(boxes_mock.call_count, 0)
        self.assertEqual(labels_mock.call_count, 0)
        self.assertEqual(token_mock.call_count, 0)
        self.assertNotEqual(physical.labels_status, 'success')
        self.assertFalse(physical.shipping_label_attachment_id)

    def test_17b_ltl_block_reason_only_applies_to_non_partnered_freight(self):
        """The block targets Non-Partnered LTL/FTL only; SPD is unaffected."""
        physical = self.shipment.physical_shipment_ids
        # Small Parcel: no block.
        physical.write({'shipping_mode': 'GROUND_SMALL_PARCEL', 'carrier_type': 'non_partnered'})
        self.assertFalse(physical._is_non_partnered_ltl())
        self.assertFalse(physical._ltl_labels_block_reason())
        # Amazon-partnered freight: not a USE_YOUR_OWN_CARRIER block case.
        physical.write({'shipping_mode': 'FREIGHT_LTL', 'carrier_type': 'partnered'})
        physical.selected_transportation_option_id = False
        self.assertFalse(physical._is_non_partnered_ltl())
        self.assertFalse(physical._ltl_labels_block_reason())
        # Non-partnered LTL: blocked with an actionable reason.
        physical.write({'shipping_mode': 'FREIGHT_LTL', 'carrier_type': 'non_partnered'})
        self.assertTrue(physical._is_non_partnered_ltl())
        reason = physical._ltl_labels_block_reason()
        self.assertTrue(reason)
        self.assertIn('pallet and freight', reason)

    def test_17c_get_labels_wrapper_serializes_pagination_only_when_given(self):
        """The API wrapper adds PageSize/PageStartIndex only when supplied."""
        api = AmazonAPI()
        response = MagicMock()
        response.json.return_value = {'payload': {'DownloadURL': 'https://example.test/l.pdf'}}
        response.headers = {}
        response.status_code = 200
        # Without pagination (SPD): params must not contain PageSize/PageStartIndex.
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.get_inbound_labels_v0(
                self.instance, 'test-token', 'FBA1234ABCD', 'PackageLabel_A4_2',
                'UNIQUE', 2, ['BOX-1', 'BOX-2'],
            )
        params = request.call_args.kwargs['params']
        self.assertNotIn('PageSize', params)
        self.assertNotIn('PageStartIndex', params)
        self.assertEqual(params['PackageLabelsToPrint'], 'BOX-1,BOX-2')
        # With pagination supplied: both are serialized.
        with patch.object(api, '_amazon_request', return_value=response) as request:
            api.get_inbound_labels_v0(
                self.instance, 'test-token', 'FBA1234ABCD', 'PackageLabel_A4_2',
                'UNIQUE', 9, ['BOX-1'], page_size=9, page_start_index=0,
            )
        params = request.call_args.kwargs['params']
        self.assertEqual(params['PageSize'], 9)
        self.assertEqual(params['PageStartIndex'], 0)

    def test_17d_label_validation_blocks_invalid_requests(self):
        """Validation errors are raised before any Amazon getLabels request."""
        physical = self.shipment.physical_shipment_ids
        physical.write({'transportation_confirmation_status': 'success'})

        # Missing shipment confirmation id.
        physical.shipment_confirmation_id = False
        with self.assertRaisesRegex(UserError, 'shipmentConfirmationId'):
            physical.action_get_shipping_labels()
        physical.shipment_confirmation_id = 'FBA1234ABCD'

        # Missing page type.
        physical.label_page_type = False
        with self.assertRaisesRegex(UserError, 'label page type'):
            physical.action_get_shipping_labels()
        physical.label_page_type = 'PackageLabel_A4_2'

        # Zero boxes returned by Amazon -> no getLabels call.
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise', return_value='test-token',
            ),
            patch.object(
                AmazonAPI, 'list_shipment_boxes', autospec=True, return_value={'boxes': []},
            ),
            patch.object(AmazonAPI, 'get_inbound_labels_v0', autospec=True) as labels_mock,
        ):
            with self.assertRaisesRegex(UserError, 'box ID'):
                physical.action_get_shipping_labels()
        self.assertEqual(labels_mock.call_count, 0)

        # A box missing its official Amazon boxId is rejected before getLabels.
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise', return_value='test-token',
            ),
            patch.object(
                AmazonAPI, 'list_shipment_boxes', autospec=True,
                return_value={'boxes': [{'boxId': '', 'quantity': 1}]},
            ),
            patch.object(AmazonAPI, 'get_inbound_labels_v0', autospec=True) as labels_mock,
        ):
            with self.assertRaisesRegex(UserError, 'without boxId'):
                physical.action_get_shipping_labels()
        self.assertEqual(labels_mock.call_count, 0)

    def test_17e_label_failure_is_sanitized_and_leaves_no_success(self):
        """An Amazon label error surfaces as UserError without leaking secrets."""
        physical = self.shipment.physical_shipment_ids
        physical.write({'transportation_confirmation_status': 'success'})
        diagnostic = (
            'HTTP Status: 400\nAmazon Error Code: InvalidInput\n'
            'Request Headers: {"x-amz-access-token": "<redacted>"}'
        )
        http_error = requests.exceptions.HTTPError(diagnostic)
        http_error.amazon_diagnostic = diagnostic
        with (
            patch.object(
                type(self.instance), '_get_access_token_or_raise', return_value='test-token',
            ),
            patch.object(
                AmazonAPI, 'list_shipment_boxes', autospec=True,
                return_value={'boxes': [{'boxId': 'FBA1234ABCDU000001', 'quantity': 1}]},
            ),
            patch.object(
                AmazonAPI, 'get_inbound_labels_v0', autospec=True, side_effect=http_error,
            ),
        ):
            with self.assertRaises(UserError) as ctx:
                physical.action_get_shipping_labels()
        message = str(ctx.exception)
        self.assertIn('InvalidInput', message)
        self.assertNotIn('Atza|', message)
        self.assertNotEqual(physical.labels_status, 'success')
        self.assertFalse(physical.shipping_label_attachment_id)

    def test_17f_blocked_label_request_leaves_dispatch_state_untouched(self):
        """A blocked LTL label request must not mutate transportation/tracking state."""
        physical = self.shipment.physical_shipment_ids
        physical.write({
            'transportation_confirmation_status': 'success',
            'transportation_confirmation_operation_id': 'op-transport-confirm',
            'shipping_mode': 'FREIGHT_LTL',
            'carrier_type': 'non_partnered',
            'tracking_status': 'success',
            'tracking_number': 'OWN-CARRIER-123',
        })
        fields_to_check = [
            'transportation_confirmation_status', 'transportation_confirmation_operation_id',
            'selected_transportation_option_id', 'selected_appointment_slot_id',
            'appointment_generation_status', 'tracking_status', 'tracking_number', 'picking_id',
        ]

        def snapshot():
            return {
                f: (physical[f].id if hasattr(physical[f], 'id') else physical[f])
                for f in fields_to_check
            }

        before = snapshot()
        with self.assertRaisesRegex(UserError, 'Non-Partnered LTL/FTL'):
            physical.action_get_shipping_labels()
        self.assertNotEqual(physical.labels_status, 'success')
        self.assertEqual(before, snapshot())

    def test_18_ambiguous_transport_write_is_not_replayed(self):
        physical = self.shipment.physical_shipment_ids
        with patch.object(
            AmazonAPI, 'generate_transportation_options', autospec=True,
            side_effect=UserError('request timed out after submission'),
        ) as generate_mock:
            physical._action_generate_transportation_options()
            job = self.shipment.operation_job_ids.filtered(
                lambda item: item.operation_type == 'generate_transportation_options'
            )
            job._process_operation()
            job._process_operation()
            with self.assertRaisesRegex(UserError, 'unknown Amazon outcome'):
                physical._action_generate_transportation_options()
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
                physical._action_generate_transportation_options()
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
