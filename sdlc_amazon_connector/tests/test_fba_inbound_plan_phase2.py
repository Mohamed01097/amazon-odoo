import json
from unittest.mock import patch

import requests

from odoo import Command, api, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
OPERATION_ID = '1234abcd-1234-abcd-5678-1234abcd5678'


@tagged('post_install', '-at_install')
class TestFbaInboundPlanPhase2(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 2 Test Company',
        })
        self.ship_from = self.env['res.partner'].sudo().create({
            'name': 'Phase 2 Shipping Contact',
            'company_id': self.company.id,
            'street': '10 Test Street',
            'street2': 'Unit 4',
            'city': 'Cairo',
            'zip': '11511',
            'country_id': self.env.ref('base.eg').id,
            'phone': '+20 100 000 0000',
            'email': 'warehouse@example.test',
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 2 Test Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'fba_ship_from_partner_id': self.ship_from.id,
        })
        self.odoo_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 2 Product',
            'default_code': 'P2-MSKU-001',
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 2 Amazon Product',
            'instance_id': self.instance.id,
            'sku': 'P2-MSKU-001',
            'odoo_product_id': self.odoo_product.id,
        })
        self.shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P2-PLAN-001',
            'shipment_name': 'Phase 2 Plan',
            'instance_id': self.instance.id,
        })
        self.line = self.env['amazon.inbound.shipment.line'].sudo().create({
            'shipment_id': self.shipment.id,
            'amazon_product_id': self.amazon_product.id,
            'odoo_product_id': self.odoo_product.id,
            'sku': self.amazon_product.sku,
            'planned_quantity': 12,
            'prep_owner': 'SELLER',
            'label_owner': 'SELLER',
        })

    def _create_response(self):
        return {
            'inboundPlanId': PLAN_ID,
            'operationId': OPERATION_ID,
            '_amazon_request_id': 'phase2-create-request-id',
        }

    def _matching_plan_response(self, plan_id=PLAN_ID):
        return {
            'inboundPlanId': plan_id,
            'name': 'Phase 2 Plan',
            'status': 'ACTIVE',
            'marketplaceIds': ['ARBP9OOSHTCHU'],
            'sourceAddress': {
                'addressLine1': '10 Test Street',
                'addressLine2': 'Unit 4',
                'city': 'Cairo',
                'postalCode': '11511',
                'countryCode': 'EG',
                'phoneNumber': '+20 100 000 0000',
            },
            '_amazon_request_id': 'phase2-get-plan-id',
        }

    def _matching_plan_items_response(self, items=None):
        return {
            'items': items if items is not None else [{
                'msku': 'P2-MSKU-001',
                'quantity': 12,
                'expiration': '2031-06-30',
                'labelOwner': 'SELLER',
                'asin': 'TESTASIN',
                'fnsku': 'TESTFNSKU',
                'prepInstructions': [],
            }],
            'pagination': {},
            '_amazon_request_id': 'phase2-list-plan-items-id',
        }

    def _create_mapped_product(self, amazon_sku, default_code):
        odoo_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': amazon_sku,
            'default_code': default_code,
            'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': amazon_sku,
            'instance_id': self.instance.id,
            'sku': amazon_sku,
            'odoo_product_id': odoo_product.id,
        })
        return amazon_product, odoo_product

    def _start_plan(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True, return_value=self._create_response()),
        ):
            return self.shipment.action_create_shipment_plan()

    def test_01_valid_payload_builder(self):
        payload = self.shipment._prepare_create_inbound_plan_payload()

        self.assertEqual(payload['name'], 'Phase 2 Plan')
        self.assertEqual(payload['destinationMarketplaces'], ['ARBP9OOSHTCHU'])
        self.assertEqual(payload['sourceAddress']['countryCode'], 'EG')
        self.assertEqual(payload['sourceAddress']['addressLine1'], '10 Test Street')
        self.assertEqual(payload['sourceAddress']['addressLine2'], 'Unit 4')
        self.assertEqual(payload['sourceAddress']['phoneNumber'], '+20 100 000 0000')
        self.assertEqual(payload['items'], [{
            'msku': 'P2-MSKU-001',
            'quantity': 12,
            'prepOwner': 'SELLER',
            'labelOwner': 'SELLER',
        }])

    def test_01b_expiration_date_is_optional_and_uses_amazon_item_field(self):
        self.assertIn('expiration_date', self.line._fields)
        self.assertEqual(
            list(name for name in self.line._fields if name == 'expiration_date'),
            ['expiration_date'],
        )

        payload_without_expiration = self.shipment._prepare_create_inbound_plan_payload()
        self.assertNotIn('expiration', payload_without_expiration['items'][0])

        self.line.expiration_date = '2031-06-30'
        payload_with_expiration = self.shipment._prepare_create_inbound_plan_payload()
        self.assertEqual(payload_with_expiration['items'][0]['expiration'], '2031-06-30')
        self.assertNotIn('expirationDate', payload_with_expiration['items'][0])

    def test_01c_reported_sku_expiration_payload(self):
        amazon_product, _odoo_product = self._create_mapped_product(
            '6224003090026', '6224003090026',
        )
        shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P2-EXPIRATION-SKU',
            'shipment_name': 'Phase 2 Expiration SKU',
            'instance_id': self.instance.id,
            'line_ids': [Command.create({
                'amazon_product_id': amazon_product.id,
                'planned_quantity': 100,
                'prep_owner': 'NONE',
                'label_owner': 'SELLER',
                'expiration_date': '2031-06-30',
            })],
        })

        payload = shipment._prepare_create_inbound_plan_payload()

        self.assertEqual(payload['items'], [{
            'msku': '6224003090026',
            'quantity': 100,
            'prepOwner': 'NONE',
            'labelOwner': 'SELLER',
            'expiration': '2031-06-30',
        }])

    def test_02_missing_address_lists_fields(self):
        incomplete_partner = self.env['res.partner'].sudo().create({
            'name': 'Incomplete Source',
            'company_id': self.company.id,
        })
        self.instance.write({'fba_ship_from_partner_id': incomplete_partner.id})

        with self.assertRaises(UserError) as error:
            self.shipment._prepare_create_inbound_plan_payload()
        message = str(error.exception)
        for label in ('Street', 'City', 'ZIP/Postal Code', 'Country', 'Phone'):
            self.assertIn(label, message)

    def test_03_missing_sku_prevents_api_call(self):
        self.amazon_product.sku = False
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock,
        ):
            with self.assertRaisesRegex(UserError, 'no Seller SKU/MSKU'):
                self.shipment.action_create_shipment_plan()
        create_mock.assert_not_called()

    def test_04_invalid_quantity_is_rejected_without_api(self):
        with self.assertRaises(ValidationError):
            self.line.write({'planned_quantity': 0})
        with self.assertRaises(ValidationError):
            self.line.write({'planned_quantity': -1})

    def test_05_response_identifiers_are_separate_and_job_is_persistent(self):
        action = self._start_plan()

        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(self.shipment.inbound_plan_id, PLAN_ID)
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)
        self.assertFalse(self.shipment.shipment_id)
        self.assertEqual(self.shipment.create_operation_status, 'pending')
        self.assertEqual(self.shipment.state, 'planning')
        self.assertEqual(len(self.shipment.operation_job_ids), 1)
        self.assertEqual(self.shipment.operation_job_ids.state, 'pending')

    def test_06_missing_operation_id_is_durable_failure(self):
        response = {
            'inboundPlanId': PLAN_ID,
            '_amazon_request_id': 'missing-operation-request-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True, return_value=response),
        ):
            action = self.shipment.action_create_shipment_plan()

        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(self.shipment.inbound_plan_id, PLAN_ID)
        self.assertFalse(self.shipment.create_operation_id)
        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertIn('operationId', self.shipment.create_operation_error_message)

    def test_07_async_success(self):
        self._start_plan()
        operation_response = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
            '_amazon_request_id': 'phase2-poll-success-id',
        }
        plan_response = {
            'inboundPlanId': PLAN_ID,
            'status': 'ACTIVE',
            '_amazon_request_id': 'phase2-get-plan-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=operation_response),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True, return_value=plan_response),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertTrue(self.shipment.plan_created_at)
        self.assertEqual(self.shipment.raw_plan_status, 'ACTIVE')
        self.assertEqual(self.shipment.operation_job_ids.state, 'done')

    def test_08_async_failure_preserves_identifiers(self):
        self._start_plan()
        operation_response = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'InvalidItem',
                'message': 'The item cannot be planned.',
            }],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=operation_response),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_error_code, 'InvalidItem')
        self.assertIn('cannot be planned', self.shipment.create_operation_error_message)
        self.assertEqual(self.shipment.inbound_plan_id, PLAN_ID)
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)

    def test_09_unknown_status_is_non_final(self):
        self._start_plan()
        operation_response = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'WAITING_FOR_AMAZON',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=operation_response),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertIn(self.shipment.create_operation_status, ('pending', 'in_progress'))
        self.assertEqual(self.shipment.raw_create_operation_status, 'WAITING_FOR_AMAZON')
        self.assertEqual(self.shipment.state, 'planning')
        self.assertFalse(self.shipment.plan_created_at)
        self.assertIn('WAITING_FOR_AMAZON', self.shipment.create_operation_response)

    def test_10_create_is_idempotent(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True, return_value=self._create_response()) as create_mock,
        ):
            self.shipment.action_create_shipment_plan()
            with self.assertRaisesRegex(UserError, 'already stored'):
                self.shipment.action_create_shipment_plan()
        self.assertEqual(create_mock.call_count, 1)
        self.assertEqual(len(self.shipment.operation_job_ids), 1)

    def test_11_pending_job_survives_environment_reload(self):
        self._start_plan()
        job = self.shipment.operation_job_ids
        in_progress = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'IN_PROGRESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=in_progress),
        ):
            job._process_operation()
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(job.state, 'in_progress')
        self.assertTrue(job.next_run_at)

        job_id = job.id
        self.env.flush_all()
        self.env.invalidate_all()

        reloaded_env = api.Environment(self.env.cr, self.env.uid, dict(self.env.context))
        reloaded_job = reloaded_env['amazon.inbound.operation.job'].sudo().browse(job_id).exists()
        self.assertTrue(reloaded_job)
        self.assertEqual(reloaded_job.operation_id, OPERATION_ID)
        self.assertIn(reloaded_job.state, ('pending', 'in_progress'))

    def test_12_plan_creation_does_not_change_stock(self):
        Picking = self.env['stock.picking'].sudo()
        Move = self.env['stock.move'].sudo()
        Quant = self.env['stock.quant'].sudo()
        before_pickings = Picking.search_count([])
        before_moves = Move.search_count([])
        before_quantities = {quant.id: quant.quantity for quant in Quant.search([])}

        self._start_plan()

        self.assertEqual(Picking.search_count([]), before_pickings)
        self.assertEqual(Move.search_count([]), before_moves)
        self.assertEqual(
            {quant.id: quant.quantity for quant in Quant.search([])},
            before_quantities,
        )

    def test_api_operation_status_uses_existing_http_client(self):
        class Response:
            headers = {'x-amzn-RequestId': 'phase2-wrapper-request-id'}

            @staticmethod
            def json():
                return {'operationStatus': 'IN_PROGRESS'}

        api_client = AmazonAPI()
        with patch.object(api_client, '_amazon_request', return_value=Response()) as request_mock:
            result = api_client.get_inbound_operation_status(
                self.instance, 'test-token', OPERATION_ID,
            )

        args = request_mock.call_args.args
        self.assertEqual(args[2], 'GET')
        self.assertTrue(args[3].endswith('/inbound/fba/2024-03-20/operations/%s' % OPERATION_ID))
        self.assertEqual(result['_amazon_request_id'], 'phase2-wrapper-request-id')

    def test_api_list_inbound_plan_items_uses_existing_http_client(self):
        class Response:
            headers = {'x-amzn-RequestId': 'phase2-list-items-request-id'}

            @staticmethod
            def json():
                return {'items': []}

        api_client = AmazonAPI()
        with patch.object(api_client, '_amazon_request', return_value=Response()) as request_mock:
            result = api_client.list_inbound_plan_items(
                self.instance, 'test-token', PLAN_ID, page_size=50, pagination_token='token-2',
            )

        args = request_mock.call_args.args
        kwargs = request_mock.call_args.kwargs
        self.assertEqual(args[2], 'GET')
        self.assertTrue(args[3].endswith('/inbound/fba/2024-03-20/inboundPlans/%s/items' % PLAN_ID))
        self.assertEqual(kwargs['params'], {'pageSize': 50, 'paginationToken': 'token-2'})
        self.assertEqual(result['_amazon_request_id'], 'phase2-list-items-request-id')

    def test_13_multiple_products_are_preserved_in_payload(self):
        second_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 2 Product B',
            'default_code': 'P2-MSKU-002',
            'company_id': self.company.id,
        })
        second_amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 2 Amazon Product B',
            'instance_id': self.instance.id,
            'sku': 'P2-MSKU-002',
            'odoo_product_id': second_product.id,
        })
        self.env['amazon.inbound.shipment.line'].sudo().create({
            'shipment_id': self.shipment.id,
            'amazon_product_id': second_amazon_product.id,
            'odoo_product_id': second_product.id,
            'sku': second_amazon_product.sku,
            'planned_quantity': 5,
            'prep_owner': 'SELLER',
            'label_owner': 'SELLER',
        })
        self.line.expiration_date = '2031-06-30'

        payload = self.shipment._prepare_create_inbound_plan_payload()

        self.assertEqual(payload['items'], [
            {
                'msku': 'P2-MSKU-001', 'quantity': 12,
                'prepOwner': 'SELLER', 'labelOwner': 'SELLER',
                'expiration': '2031-06-30',
            },
            {
                'msku': 'P2-MSKU-002', 'quantity': 5,
                'prepOwner': 'SELLER', 'labelOwner': 'SELLER',
            },
        ])

    def test_14_unmapped_product_is_blocked_before_api(self):
        self.amazon_product.odoo_product_id = False
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock,
        ):
            with self.assertRaisesRegex(UserError, 'not mapped to an Odoo product'):
                self.shipment.action_create_shipment_plan()
        create_mock.assert_not_called()

    def test_15_missing_marketplace_is_blocked_before_api(self):
        self.instance.marketplace_id = False
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock,
        ):
            with self.assertRaisesRegex(UserError, 'Marketplace ID'):
                self.shipment.action_create_shipment_plan()
        create_mock.assert_not_called()

    def test_16_cross_instance_product_is_blocked_before_api(self):
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 2 Other Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'fba_ship_from_partner_id': self.ship_from.id,
        })
        other_amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 2 Other Amazon Product',
            'instance_id': other_instance.id,
            'sku': 'P2-OTHER-001',
            'odoo_product_id': self.odoo_product.id,
        })
        with patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock:
            with self.assertRaisesRegex(ValidationError, "must belong to the shipment's Amazon instance"):
                self.line.amazon_product_id = other_amazon_product
        create_mock.assert_not_called()

    def test_17_pending_operation_blocks_duplicate_without_plan_id(self):
        self.shipment.write({
            'create_operation_id': OPERATION_ID,
            'create_operation_status': 'in_progress',
            'state': 'planning',
        })
        with patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock:
            action = self.shipment.action_create_shipment_plan()

        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)
        self.assertFalse(self.shipment.inbound_plan_id)
        self.assertEqual(len(self.shipment.operation_job_ids), 1)
        create_mock.assert_not_called()

    def test_25_line_create_derives_marketplace_sku_and_odoo_product(self):
        amazon_product, odoo_product = self._create_mapped_product(
            'AMAZON-MSKU-003', 'ODOO-INTERNAL-003',
        )
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as api_mock:
            line = self.env['amazon.inbound.shipment.line'].sudo().create({
                'shipment_id': self.shipment.id,
                'amazon_product_id': amazon_product.id,
                'planned_quantity': 1,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            })

        self.assertEqual(line.sku, 'AMAZON-MSKU-003')
        self.assertNotEqual(line.sku, odoo_product.default_code)
        self.assertEqual(line.odoo_product_id, odoo_product)
        api_mock.assert_not_called()

    def test_26_parent_one2many_create_derives_required_sku(self):
        amazon_product, odoo_product = self._create_mapped_product(
            'AMAZON-MSKU-004', 'ODOO-INTERNAL-004',
        )
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as api_mock:
            shipment = self.env['amazon.inbound.shipment'].sudo().create({
                'shipment_name': 'One2many SKU Regression',
                'instance_id': self.instance.id,
                'line_ids': [Command.create({
                    'amazon_product_id': amazon_product.id,
                    'planned_quantity': 1,
                    'prep_owner': 'SELLER',
                    'label_owner': 'SELLER',
                })],
            })

        self.assertEqual(shipment.line_ids.sku, 'AMAZON-MSKU-004')
        self.assertEqual(shipment.line_ids.odoo_product_id, odoo_product)
        api_mock.assert_not_called()

    def test_27_changing_amazon_product_replaces_stale_mapped_values(self):
        amazon_product, odoo_product = self._create_mapped_product(
            'AMAZON-MSKU-005', 'ODOO-INTERNAL-005',
        )
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as api_mock:
            self.line.write({
                'amazon_product_id': amazon_product.id,
                'sku': 'STALE-SKU',
                'odoo_product_id': self.odoo_product.id,
            })

        self.assertEqual(self.line.sku, 'AMAZON-MSKU-005')
        self.assertEqual(self.line.odoo_product_id, odoo_product)
        api_mock.assert_not_called()

        onchange_line = self.env['amazon.inbound.shipment.line'].new({
            'amazon_product_id': amazon_product.id,
        })
        onchange_line._onchange_amazon_product_id()
        self.assertEqual(onchange_line.sku, 'AMAZON-MSKU-005')
        self.assertEqual(onchange_line.odoo_product_id, odoo_product)
        onchange_line.amazon_product_id = False
        onchange_line._onchange_amazon_product_id()
        self.assertFalse(onchange_line.sku)
        self.assertFalse(onchange_line.odoo_product_id)

    def test_18_full_mock_lifecycle_has_zero_stock_side_effect(self):
        Picking = self.env['stock.picking'].sudo()
        Move = self.env['stock.move'].sudo()
        Quant = self.env['stock.quant'].sudo()
        before = {
            'pickings': Picking.search_count([]),
            'moves': Move.search_count([]),
            'product_quants': {
                quant.location_id.id: (quant.quantity, quant.reserved_quantity)
                for quant in Quant.search([('product_id', '=', self.odoo_product.id)])
            },
        }
        self._start_plan()
        in_progress = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'IN_PROGRESS',
            'operationProblems': [],
        }
        success = dict(in_progress, operationStatus='SUCCESS')
        plan = {'inboundPlanId': PLAN_ID, 'status': 'ACTIVE'}
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                side_effect=[in_progress, success],
            ),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True, return_value=plan),
        ):
            self.shipment.action_check_create_operation_status()
            self.shipment.action_check_create_operation_status()

        after_product_quants = {
            quant.location_id.id: (quant.quantity, quant.reserved_quantity)
            for quant in Quant.search([('product_id', '=', self.odoo_product.id)])
        }
        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertEqual(Picking.search_count([]), before['pickings'])
        self.assertEqual(Move.search_count([]), before['moves'])
        self.assertEqual(after_product_quants, before['product_quants'])

    def test_19_async_failure_retains_request_id_and_diagnostics(self):
        self._start_plan()
        failed = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'InvalidItem',
                'message': 'The item cannot be planned.',
            }],
            '_amazon_request_id': 'phase2-failed-request-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=failed),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.last_operation_request_id, 'phase2-failed-request-id')
        self.assertEqual(self.shipment.create_operation_error_code, 'InvalidItem')
        self.assertIn('cannot be planned', self.shipment.create_operation_error_message)
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)
        self.assertEqual(self.shipment.inbound_plan_id, PLAN_ID)

    def test_20_timeout_create_is_not_retried_or_resubmittable(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'create_inbound_plan', autospec=True,
                side_effect=requests.exceptions.Timeout('temporary create timeout'),
            ) as create_mock,
        ):
            action = self.shipment.action_create_shipment_plan()
            self.assertEqual(action['params']['type'], 'danger')
            self.assertEqual(self.shipment.create_operation_error_code, 'CREATE_OUTCOME_UNKNOWN')
            with self.assertRaisesRegex(UserError, 'outcome is unknown'):
                self.shipment.action_create_shipment_plan()
        self.assertEqual(create_mock.call_count, 1)
        self.assertFalse(self.shipment.create_operation_id)
        self.assertFalse(self.shipment.inbound_plan_id)

    def test_21_create_transport_does_not_retry_timeout(self):
        api_client = AmazonAPI()
        with (
            patch('odoo.addons.sdlc_amazon_connector.models.amazon_api.requests.request',
                  side_effect=requests.exceptions.Timeout('ambiguous timeout')) as request_mock,
            patch.object(api_client, '_log_amazon_request'),
            patch('odoo.addons.sdlc_amazon_connector.models.amazon_api.time.sleep') as sleep_mock,
        ):
            with self.assertRaises(requests.exceptions.Timeout):
                api_client.create_inbound_plan(self.instance, 'test-token', {'items': []})

        self.assertEqual(request_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_22_http_429_retry_after_is_deferred_without_resubmission(self):
        response = requests.Response()
        response.status_code = 429
        response.headers.update({
            'Retry-After': '120',
            'x-amzn-RequestId': 'phase2-throttle-request-id',
        })
        response._content = json.dumps({
            'errors': [{'code': 'QuotaExceeded', 'message': 'Rate exceeded'}],
        }).encode()
        response.url = 'https://sellingpartnerapi-eu.amazon.com/inbound/fba/2024-03-20/inboundPlans'
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch('odoo.addons.sdlc_amazon_connector.models.amazon_api.requests.request',
                  return_value=response) as request_mock,
            patch.object(AmazonAPI, '_log_amazon_request'),
            patch('odoo.addons.sdlc_amazon_connector.models.amazon_api.time.sleep') as sleep_mock,
        ):
            action = self.shipment.action_create_shipment_plan()
            self.assertEqual(action['params']['type'], 'danger')
            self.assertEqual(self.shipment.create_operation_error_code, 'CREATE_RATE_LIMITED')
            self.assertTrue(self.shipment.create_retry_after_at)
            with self.assertRaisesRegex(UserError, 'rate limit'):
                self.shipment.action_create_shipment_plan()

        self.assertEqual(request_mock.call_count, 1)
        sleep_mock.assert_not_called()
        self.assertEqual(self.shipment.create_operation_request_id, 'phase2-throttle-request-id')

    def test_23_mismatched_failed_operation_preserves_amazon_business_error(self):
        self._start_plan()
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': 'f8e9b032-fa6c-4737-9428-4d937ed0675c',
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'FBA_INB_0180',
                'details': "There's an input error with the resource '6224003090026'.",
                'message': 'ERROR: Expiration date required',
            }],
            '_amazon_request_id': 'phase2-mismatch-failed-request-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True) as get_plan_mock,
        ):
            action = self.shipment.action_check_create_operation_status()

        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)
        self.assertEqual(self.shipment.create_operation_reported_id, 'f8e9b032-fa6c-4737-9428-4d937ed0675c')
        self.assertEqual(self.shipment.raw_create_operation_status, 'FAILED')
        self.assertEqual(self.shipment.last_operation_request_id, 'phase2-mismatch-failed-request-id')
        self.assertEqual(self.shipment.create_operation_error_code, 'FBA_INB_0180')
        self.assertIn('Expiration date required', self.shipment.create_operation_error_message)
        self.assertIn('operationProblems', self.shipment.create_operation_response)
        self.assertIn('operationIdAudit', self.shipment.create_operation_response)
        self.assertIn(OPERATION_ID, self.shipment.create_operation_response)
        self.assertIn('f8e9b032-fa6c-4737-9428-4d937ed0675c', self.shipment.create_operation_response)
        get_plan_mock.assert_not_called()

    def test_23b_mismatched_success_without_verified_plan_is_not_accepted(self):
        self._start_plan()
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': '9999abcd-1234-abcd-5678-1234abcd5678',
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True, side_effect=UserError('404 not found')) as get_plan_mock,
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True) as list_items_mock,
        ):
            action = self.shipment.action_check_create_operation_status()

        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_id, OPERATION_ID)
        self.assertEqual(self.shipment.create_operation_reported_id, '9999abcd-1234-abcd-5678-1234abcd5678')
        self.assertEqual(self.shipment.create_operation_error_code, 'OPERATION_ID_MISMATCH_UNVERIFIED')
        self.assertIn('could not be independently verified', self.shipment.create_operation_error_message)
        self.assertFalse(self.shipment.plan_created_at)
        get_plan_mock.assert_called_once()
        list_items_mock.assert_not_called()

    def test_23c_mismatched_success_is_reconciled_when_plan_identity_matches(self):
        self.line.expiration_date = '2031-06-30'
        real_stored_id = '64f4c494-ba38-472c-a795-1d8713e7cfc3'
        first_returned_id = '1b75e1e7-261c-4544-a03c-27c2ef21273a'
        latest_returned_id = 'ea256204-7cdf-4310-9c39-aa42618d99b6'
        real_plan_id = 'wfca96c672-bf73-4709-b926-640cc2807653'
        self._start_plan()
        self.shipment.write({
            'inbound_plan_id': real_plan_id,
            'create_operation_id': real_stored_id,
            'create_operation_reported_id': first_returned_id,
        })
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': latest_returned_id,
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
            '_amazon_request_id': 'phase2-real-mismatch-success-request-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True,
                         return_value=self._matching_plan_response(real_plan_id)),
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True,
                         return_value=self._matching_plan_items_response()),
        ):
            action = self.shipment.action_check_create_operation_status()

        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertEqual(self.shipment.create_operation_id, real_stored_id)
        self.assertEqual(self.shipment.create_operation_reported_id, first_returned_id)
        self.assertEqual(self.shipment.raw_create_operation_status, 'SUCCESS')
        self.assertEqual(self.shipment.raw_plan_status, 'ACTIVE')
        self.assertTrue(self.shipment.plan_created_at)
        self.assertFalse(self.shipment.create_operation_error_code)
        response = json.loads(self.shipment.create_operation_response)
        audit = response['getInboundOperationStatus']['operationIdAudit']
        self.assertTrue(audit['reconciledByGetInboundPlan'])
        self.assertEqual(audit['preservedReportedOperationId'], first_returned_id)
        self.assertEqual(audit['allReturnedOperationIds'], [first_returned_id, latest_returned_id])
        self.assertTrue(
            response['getInboundOperationStatus']['operation']['planVerification']['verified']
        )

    def test_23d_mismatched_success_wrong_msku_is_not_reconciled(self):
        self._start_plan()
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': '9999abcd-1234-abcd-5678-1234abcd5678',
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True,
                         return_value=self._matching_plan_response()),
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True,
                         return_value=self._matching_plan_items_response([{
                             'msku': 'WRONG-MSKU',
                             'quantity': 12,
                             'labelOwner': 'SELLER',
                         }])),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_error_code, 'OPERATION_ID_MISMATCH_UNVERIFIED')
        self.assertIn('item quantities do not match', self.shipment.create_operation_error_message)

    def test_23e_mismatched_success_wrong_quantity_is_not_reconciled(self):
        self._start_plan()
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': '9999abcd-1234-abcd-5678-1234abcd5678',
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True,
                         return_value=self._matching_plan_response()),
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True,
                         return_value=self._matching_plan_items_response([{
                             'msku': 'P2-MSKU-001',
                             'quantity': 11,
                             'labelOwner': 'SELLER',
                         }])),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_error_code, 'OPERATION_ID_MISMATCH_UNVERIFIED')
        self.assertIn('item quantities do not match', self.shipment.create_operation_error_message)

    def test_23f_mismatched_success_multi_item_different_order_is_reconciled(self):
        second_amazon_product, second_odoo_product = self._create_mapped_product(
            'P2-MSKU-002', 'P2-MSKU-002',
        )
        self.env['amazon.inbound.shipment.line'].sudo().create({
            'shipment_id': self.shipment.id,
            'amazon_product_id': second_amazon_product.id,
            'odoo_product_id': second_odoo_product.id,
            'sku': second_amazon_product.sku,
            'planned_quantity': 5,
            'prep_owner': 'SELLER',
            'label_owner': 'SELLER',
        })
        self._start_plan()
        mismatched = {
            'operation': 'createInboundPlan',
            'operationId': '9999abcd-1234-abcd-5678-1234abcd5678',
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=mismatched),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True,
                         return_value=self._matching_plan_response()),
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True,
                         return_value=self._matching_plan_items_response([
                             {'msku': 'P2-MSKU-002', 'quantity': 5, 'labelOwner': 'SELLER'},
                             {'msku': 'P2-MSKU-001', 'quantity': 12, 'labelOwner': 'SELLER'},
                         ])),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertEqual(self.shipment.state, 'plan_created')

    def test_23g_stale_create_failure_cannot_regress_plan_created(self):
        self._start_plan()
        self.shipment.write({
            'create_operation_status': 'success',
            'state': 'plan_created',
            'plan_created_at': fields.Datetime.now(),
        })
        stale_failure = {
            'operation': 'createInboundPlan',
            'operationId': 'f8e9b032-fa6c-4737-9428-4d937ed0675c',
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'STALE_FAILURE',
                'message': 'This stale result must not regress the shipment.',
            }],
            '_amazon_request_id': 'phase2-stale-create-failed-request-id',
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=stale_failure),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertFalse(self.shipment.create_operation_error_code)
        self.assertIn('staleCreateOperationGuard', self.shipment.create_operation_response)
        self.assertIn('STALE_FAILURE', self.shipment.create_operation_response)

    def test_23h_stale_create_failure_cannot_regress_active_packing_generation(self):
        self._start_plan()
        self.shipment.write({
            'create_operation_status': 'success',
            'state': 'plan_created',
            'plan_created_at': fields.Datetime.now(),
            'packing_generation_operation_id': '11111111-1111-1111-1111-111111111111',
            'packing_generation_status': 'pending',
        })
        stale_failure = {
            'operation': 'createInboundPlan',
            'operationId': 'f8e9b032-fa6c-4737-9428-4d937ed0675c',
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'STALE_FAILURE',
                'message': 'This stale result must not cancel packing.',
            }],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=stale_failure),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertEqual(self.shipment.packing_generation_status, 'pending')
        self.assertFalse(self.shipment.create_operation_error_code)

    def test_23i_stale_create_mismatch_cannot_regress_packing_generated(self):
        self._start_plan()
        self.shipment.write({
            'create_operation_status': 'success',
            'state': 'packing_generated',
            'plan_created_at': fields.Datetime.now(),
            'packing_generation_operation_id': '11111111-1111-1111-1111-111111111111',
            'packing_generation_status': 'success',
        })
        stale_mismatch = {
            'operation': 'createInboundPlan',
            'operationId': '9999abcd-1234-abcd-5678-1234abcd5678',
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=stale_mismatch),
            patch.object(AmazonAPI, 'get_inbound_plan', autospec=True, side_effect=UserError('404 not found')),
            patch.object(AmazonAPI, 'list_inbound_plan_items', autospec=True) as list_items_mock,
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.state, 'packing_generated')
        self.assertEqual(self.shipment.create_operation_status, 'success')
        self.assertFalse(self.shipment.create_operation_error_code)
        self.assertIn('operationIdAudit', self.shipment.create_operation_response)
        self.assertIn('staleCreateOperationGuard', self.shipment.create_operation_response)
        list_items_mock.assert_not_called()

    def test_23j_current_create_failure_before_advancement_still_fails(self):
        self._start_plan()
        current_failure = {
            'operation': 'createInboundPlan',
            'operationId': OPERATION_ID,
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'InvalidItem',
                'message': 'The item cannot be planned.',
            }],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(AmazonAPI, 'get_inbound_operation_status', autospec=True, return_value=current_failure),
        ):
            self.shipment.action_check_create_operation_status()

        self.assertEqual(self.shipment.state, 'failed')
        self.assertEqual(self.shipment.create_operation_status, 'failed')
        self.assertEqual(self.shipment.create_operation_error_code, 'InvalidItem')

    def test_24_invalid_state_blocks_create_before_api(self):
        self.shipment.state = 'plan_created'
        with patch.object(AmazonAPI, 'create_inbound_plan', autospec=True) as create_mock:
            with self.assertRaisesRegex(UserError, 'Draft or a retryable Failed state'):
                self.shipment.action_create_shipment_plan()
        create_mock.assert_not_called()

    def test_28_amazon_product_creation_is_stockable_for_fba(self):
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 2 Imported FBA Product',
            'instance_id': self.instance.id,
            'sku': 'P2-IMPORTED-STORABLE-001',
            'fulfillment_channel': 'AFN',
        })

        odoo_product = amazon_product._create_odoo_product_from_amazon()

        self.assertTrue(odoo_product.product_tmpl_id.is_storable)

    def test_29_expiration_date_is_visible_in_planned_items_view(self):
        view = self.env.ref('sdlc_amazon_connector.amazon_inbound_shipment_form')
        self.assertIn('name="line_ids"', view.arch_db)
        self.assertIn('name="expiration_date"', view.arch_db)
