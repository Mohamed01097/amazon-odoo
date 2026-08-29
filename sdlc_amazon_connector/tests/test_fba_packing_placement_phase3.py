from unittest.mock import Mock, patch

import requests

from odoo import api
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_1 = 'po1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_2 = 'po5678abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_3 = 'po9999abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_1 = 'pl1234abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_2 = 'pl5678abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID_1 = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID_2 = 'sh5678abcd-1234-abcd-5678-1234abcd5678'
PACKING_GENERATE_OPERATION = '11111111-1111-1111-1111-111111111111'
PACKING_CONFIRM_OPERATION = '22222222-2222-2222-2222-222222222222'
PACKING_INFORMATION_OPERATION = '77777777-7777-7777-7777-777777777777'
PLACEMENT_GENERATE_OPERATION = '33333333-3333-3333-3333-333333333333'
PLACEMENT_CONFIRM_OPERATION = '44444444-4444-4444-4444-444444444444'
PACKING_REGENERATE_OPERATION = '55555555-5555-5555-5555-555555555555'
PLACEMENT_REGENERATE_OPERATION = '66666666-6666-6666-6666-666666666666'
PACKING_GROUP_1 = 'pg1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_GROUP_2 = 'pg5678abcd-1234-abcd-5678-1234abcd5678'
PACKING_GROUP_3 = 'pg9999abcd-1234-abcd-5678-1234abcd5678'


@tagged('post_install', '-at_install')
class TestFbaPackingPlacementPhase3(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Phase 3 Test Company',
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Phase 3 Test Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
        })
        self.odoo_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 3 Product A',
            'default_code': 'SKU-A',
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 3 Amazon Product A',
            'instance_id': self.instance.id,
            'sku': 'SKU-A',
            'odoo_product_id': self.odoo_product.id,
        })
        self.odoo_product_b = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Phase 3 Product B',
            'default_code': 'SKU-B',
            'company_id': self.company.id,
        })
        self.amazon_product_b = self.env['amazon.product'].sudo().create({
            'name': 'Phase 3 Amazon Product B',
            'instance_id': self.instance.id,
            'sku': 'SKU-B',
            'odoo_product_id': self.odoo_product_b.id,
        })
        self.shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P3-PLAN-001',
            'shipment_name': 'Phase 3 Plan',
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID,
            'create_operation_status': 'success',
            'state': 'plan_created',
        })
        self.line_a = self.env['amazon.inbound.shipment.line'].sudo().create({
            'shipment_id': self.shipment.id,
            'amazon_product_id': self.amazon_product.id,
            'odoo_product_id': self.odoo_product.id,
            'sku': 'SKU-A',
            'planned_quantity': 20,
            'prep_owner': 'SELLER',
            'label_owner': 'SELLER',
        })
        self.line_b = self.env['amazon.inbound.shipment.line'].sudo().create({
            'shipment_id': self.shipment.id,
            'amazon_product_id': self.amazon_product_b.id,
            'odoo_product_id': self.odoo_product_b.id,
            'sku': 'SKU-B',
            'planned_quantity': 10,
            'prep_owner': 'SELLER',
            'label_owner': 'SELLER',
        })

        # Hard safety boundary: every SP-API method reachable in this suite is
        # patched for the full test transaction. Individual tests can nest a
        # more specific mock without ever exposing a live call path.
        self.real_list_packing_group_items = AmazonAPI.list_packing_group_items
        self.real_get_shipment = AmazonAPI.get_shipment
        self.real_list_shipment_items = AmazonAPI.list_shipment_items
        token_patcher = patch.object(
            type(self.instance), '_get_access_token_or_raise', return_value='test-token',
        )
        token_patcher.start()
        self.addCleanup(token_patcher.stop)
        group_patcher = patch.object(
            AmazonAPI, 'list_packing_group_items', autospec=True,
            side_effect=self._packing_group_items_response,
        )
        self.packing_group_api_mock = group_patcher.start()
        self.addCleanup(group_patcher.stop)
        shipment_patcher = patch.object(
            AmazonAPI, 'get_shipment', autospec=True,
            side_effect=self._shipment_response,
        )
        shipment_patcher.start()
        self.addCleanup(shipment_patcher.stop)
        shipment_items_patcher = patch.object(
            AmazonAPI, 'list_shipment_items', autospec=True,
            side_effect=self._shipment_items_response,
        )
        self.shipment_items_api_mock = shipment_items_patcher.start()
        self.addCleanup(shipment_items_patcher.stop)

    @staticmethod
    def _packing_response(accepted=False):
        return {
            'packingOptions': [
                {
                    'packingOptionId': PACKING_OPTION_1,
                    'status': 'ACCEPTED' if accepted else 'OFFERED',
                    'packingGroups': [PACKING_GROUP_1],
                    'fees': [{
                        'type': 'FEE',
                        'target': 'Placement Services',
                        'description': 'Packing fee',
                        'value': {'amount': 1.25, 'code': 'USD'},
                    }],
                    'discounts': [{
                        'type': 'DISCOUNT',
                        'target': 'Fulfillment Fee Discount',
                        'description': 'Packing discount',
                        'value': {'amount': 0.25, 'code': 'USD'},
                    }],
                    'expiration': '2030-01-01T00:00:00.000Z',
                    'supportedConfigurations': [],
                    'supportedShippingConfigurations': [],
                },
                {
                    'packingOptionId': PACKING_OPTION_2,
                    'status': 'OFFERED',
                    'packingGroups': [PACKING_GROUP_2],
                    'fees': [],
                    'discounts': [],
                    'expiration': '2030-01-01T00:00:00.000Z',
                    'supportedConfigurations': [],
                    'supportedShippingConfigurations': [],
                },
                {
                    'packingOptionId': PACKING_OPTION_3,
                    'status': 'OFFERED',
                    'packingGroups': [PACKING_GROUP_3],
                    'fees': [],
                    'discounts': [],
                    'expiration': '2030-01-01T00:00:00.000Z',
                    'supportedConfigurations': [],
                    'supportedShippingConfigurations': [],
                },
            ],
            '_amazon_request_id': 'phase3-list-packing-request',
        }

    @staticmethod
    def _placement_response(accepted=False):
        return {
            'placementOptions': [
                {
                    'placementOptionId': PLACEMENT_OPTION_1,
                    'status': 'ACCEPTED' if accepted else 'OFFERED',
                    'shipmentIds': [SHIPMENT_ID_1, SHIPMENT_ID_2],
                    'fees': [{
                        'type': 'FEE',
                        'target': 'Placement Services',
                        'description': 'Placement fee',
                        'value': {'amount': 4.5, 'code': 'USD'},
                    }],
                    'discounts': [{
                        'type': 'DISCOUNT',
                        'target': 'Placement Services',
                        'description': 'Placement discount',
                        'value': {'amount': 1.0, 'code': 'USD'},
                    }],
                    'expiration': '2030-01-01T00:00:00.000Z',
                },
                {
                    'placementOptionId': PLACEMENT_OPTION_2,
                    'status': 'OFFERED',
                    'shipmentIds': [SHIPMENT_ID_1],
                    'fees': [],
                    'discounts': [],
                    'expiration': '2030-01-01T00:00:00.000Z',
                },
            ],
            '_amazon_request_id': 'phase3-list-placement-request',
        }

    def _packing_group_items_response(self, _api, _instance, _token, _plan_id,
                                      _group_id, _page_size=20, _pagination_token=None):
        return {
            'items': [
                {
                    'asin': 'B000000001', 'fnsku': 'X000000001',
                    'labelOwner': 'SELLER', 'msku': 'SKU-A',
                    'prepInstructions': [], 'quantity': self.line_a.planned_quantity,
                },
                {
                    'asin': 'B000000002', 'fnsku': 'X000000002',
                    'labelOwner': 'SELLER', 'msku': 'SKU-B',
                    'prepInstructions': [], 'quantity': self.line_b.planned_quantity,
                },
            ],
        }

    @staticmethod
    def _shipment_response(_api, _instance, _token, _plan_id, shipment_id):
        suffix = 'A' if shipment_id == SHIPMENT_ID_1 else 'B'
        return {
            'placementOptionId': PLACEMENT_OPTION_1,
            'shipmentId': shipment_id,
            'shipmentConfirmationId': 'FBA-SHIPMENT-%s' % suffix,
            'amazonReferenceId': 'AMZ-REF-%s' % suffix,
            'name': 'Physical Shipment %s' % suffix,
            'destination': {
                'destinationType': 'AMAZON_WAREHOUSE',
                'warehouseId': 'FC-%s' % suffix,
            },
            'source': {'sourceType': 'SELLER_FACILITY'},
            'status': 'WORKING',
        }

    @staticmethod
    def _shipment_items_response(_api, _instance, _token, _plan_id, shipment_id,
                                 _page_size=20, _pagination_token=None):
        items = [{
            'asin': 'B000000001', 'fnsku': 'X000000001',
            'labelOwner': 'SELLER', 'msku': 'SKU-A',
            'prepInstructions': [], 'quantity': 10,
        }]
        if shipment_id == SHIPMENT_ID_1:
            items.append({
                'asin': 'B000000002', 'fnsku': 'X000000002',
                'labelOwner': 'SELLER', 'msku': 'SKU-B',
                'prepInstructions': [], 'quantity': 10,
            })
        return {'items': items}

    @staticmethod
    def _success_operation(operation_id):
        return {
            'operationId': operation_id,
            'operationStatus': 'SUCCESS',
            'operationProblems': [],
            '_amazon_request_id': 'phase3-operation-request',
        }

    def _generate_packing(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_packing_options', autospec=True,
                return_value={'operationId': PACKING_GENERATE_OPERATION},
            ),
        ):
            self.shipment.action_generate_packing_options()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_packing_options'
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PACKING_GENERATE_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=self._packing_response(),
            ),
        ):
            job._process_operation()
        return job

    def _confirm_packing(self):
        self.shipment.packing_option_ids.filtered(
            lambda option: option.amazon_packing_option_id == PACKING_OPTION_1
        ).write({'selected': True})
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'confirm_packing_option', autospec=True,
                return_value={'operationId': PACKING_CONFIRM_OPERATION},
            ),
        ):
            self.shipment.action_confirm_packing_option()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_packing_option'
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PACKING_CONFIRM_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=self._packing_response(accepted=True),
            ),
        ):
            job._process_operation()
        return job

    def _set_packing_information(self):
        option = self.shipment.packing_option_ids.filtered('selected')
        if not option.box_ids:
            self.env['amazon.fba.box'].sudo().create({
                'packing_option_id': option.id,
                'amazon_packing_group_id': PACKING_GROUP_1,
                'length': 30,
                'width': 20,
                'height': 10,
                'dimension_unit': 'CM',
                'weight': 5.5,
                'weight_unit': 'KG',
                'line_ids': [
                    (0, 0, {
                        'amazon_product_id': self.amazon_product.id,
                        'msku': 'SKU-A',
                        'quantity': self.line_a.planned_quantity,
                    }),
                    (0, 0, {
                        'amazon_product_id': self.amazon_product_b.id,
                        'msku': 'SKU-B',
                        'quantity': self.line_b.planned_quantity,
                    }),
                ],
            })
        with patch.object(
            AmazonAPI, 'set_packing_information', autospec=True,
            return_value={'operationId': PACKING_INFORMATION_OPERATION},
        ) as set_mock:
            self.shipment.action_set_packing_information()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'set_packing_information'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=self._success_operation(PACKING_INFORMATION_OPERATION),
        ):
            job._process_operation()
        return set_mock.call_args.args[-1]

    def _generate_placement(self):
        if self.shipment.packing_information_status != 'success':
            self._set_packing_information()
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_placement_options', autospec=True,
                return_value={'operationId': PLACEMENT_GENERATE_OPERATION},
            ) as generate_mock,
        ):
            self.shipment.action_generate_placement_options()
        self.assertEqual(generate_mock.call_args.args[-1], {})
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'generate_placement_options'
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PLACEMENT_GENERATE_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_placement_options', autospec=True,
                return_value=self._placement_response(),
            ),
        ):
            job._process_operation()
        return job

    def _confirm_placement(self):
        self.shipment.placement_option_ids.filtered(
            lambda option: option.amazon_placement_option_id == PLACEMENT_OPTION_1
        ).write({'selected': True})
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'confirm_placement_option', autospec=True,
                return_value={'operationId': PLACEMENT_CONFIRM_OPERATION},
            ),
        ):
            self.shipment.action_confirm_placement_option()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_placement_option'
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PLACEMENT_CONFIRM_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_placement_options', autospec=True,
                return_value=self._placement_response(accepted=True),
            ),
        ):
            job._process_operation()
        return job

    def test_01_packing_generation_and_pagination_data(self):
        job = self._generate_packing()

        self.assertEqual(job.state, 'done')
        self.assertEqual(self.shipment.state, 'packing_generated')
        self.assertEqual(self.shipment.packing_generation_status, 'success')
        self.assertEqual(len(self.shipment.packing_option_ids), 3)
        first = self.shipment.packing_option_ids.filtered(
            lambda option: option.amazon_packing_option_id == PACKING_OPTION_1
        )
        self.assertEqual(first.fee_amount, 1.25)
        self.assertEqual(first.fee_currency, 'USD')
        self.assertEqual(first.discount_amount, 0.25)
        self.assertIn('pg1234abcd', first.amazon_packing_group_ids)
        self.assertEqual(len(first.packing_group_ids), 1)
        self.assertEqual(len(first.packing_group_ids.item_ids), 2)

    def test_01b_paginated_packing_refresh_is_idempotent(self):
        response = self._packing_response()
        first_page = {
            'packingOptions': [response['packingOptions'][0]],
            'pagination': {'nextToken': 'packing-next'},
        }
        second_page = {
            'packingOptions': response['packingOptions'][1:],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                side_effect=[first_page, second_page],
            ) as list_mock,
        ):
            self.shipment._refresh_packing_options()

        self.assertEqual(list_mock.call_count, 2)
        self.assertEqual(list_mock.call_args_list[1].args[-1], 'packing-next')
        self.assertEqual(len(self.shipment.packing_option_ids), 3)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=response,
            ),
        ):
            self.shipment._refresh_packing_options()
        self.assertEqual(len(self.shipment.packing_option_ids), 3)

    def test_02_packing_confirmation_and_single_selection(self):
        self._generate_packing()
        options = self.shipment.packing_option_ids.sorted('id')
        options[0].write({'selected': True})
        with self.assertRaises(ValidationError):
            options[1].write({'selected': True})
        self._confirm_packing()

        self.assertEqual(self.shipment.state, 'packing_confirmed')
        self.assertEqual(self.shipment.packing_confirmation_status, 'success')
        self.assertEqual(len(self.shipment.packing_option_ids.filtered('selected')), 1)
        self.assertEqual(self.shipment.packing_option_ids.filtered('selected').status, 'ACCEPTED')

    def test_02b_only_offered_option_can_be_confirmed(self):
        self._generate_packing()
        selected = self.shipment.packing_option_ids[0]
        selected.write({'selected': True, 'status': 'UNKNOWN'})
        with self.assertRaisesRegex(UserError, 'status OFFERED'):
            self.shipment.action_confirm_packing_option()

    def test_02c_packing_confirmation_cannot_be_submitted_twice(self):
        self._generate_packing()
        self._confirm_packing()
        with patch.object(
            AmazonAPI, 'confirm_packing_option', autospec=True,
            return_value={'operationId': '55555555-5555-5555-5555-555555555555'},
        ) as confirm_mock:
            action = self.shipment.action_confirm_packing_option()
        self.assertEqual(confirm_mock.call_count, 0)
        self.assertEqual(action['params']['type'], 'warning')

    def test_03_placement_requires_packing_confirmation(self):
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()

    def test_04_placement_generation_and_confirmation(self):
        self._generate_packing()
        self._confirm_packing()
        generation_job = self._generate_placement()

        self.assertEqual(generation_job.state, 'done')
        self.assertEqual(self.shipment.state, 'placement_generated')
        self.assertEqual(len(self.shipment.placement_option_ids), 2)
        first = self.shipment.placement_option_ids.filtered(
            lambda option: option.amazon_placement_option_id == PLACEMENT_OPTION_1
        )
        self.assertEqual(first.fee, 4.5)
        self.assertEqual(first.currency, 'USD')
        self.assertEqual(first.discount, 1.0)
        self.assertIn(SHIPMENT_ID_1, first.amazon_shipment_ids)
        self.assertFalse(first.destination_fc)

        confirmation_job = self._confirm_placement()
        self.assertEqual(confirmation_job.state, 'done')
        self.assertEqual(self.shipment.state, 'placement_confirmed')
        self.assertEqual(self.shipment.placement_confirmation_status, 'success')
        self.assertEqual(len(self.shipment.placement_option_ids.filtered('selected')), 1)

    def test_04b_placement_refresh_is_idempotent(self):
        self._generate_packing()
        self._confirm_packing()
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'list_placement_options', autospec=True,
                return_value=self._placement_response(),
            ),
        ):
            self.shipment._refresh_placement_options()
            self.shipment._refresh_placement_options()
        self.assertEqual(len(self.shipment.placement_option_ids), 2)

    def test_05_duplicate_generate_request_is_prevented(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_packing_options', autospec=True,
                return_value={'operationId': PACKING_GENERATE_OPERATION},
            ) as generate_mock,
        ):
            self.shipment.action_generate_packing_options()
            action = self.shipment.action_generate_packing_options()
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(len(self.shipment.operation_job_ids), 1)

    def test_05b_missing_operation_id_blocks_unsafe_retry(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_packing_options', autospec=True,
                return_value={'unexpected': 'response'},
            ) as generate_mock,
        ):
            action = self.shipment.action_generate_packing_options()
            self.assertEqual(action['params']['type'], 'danger')
            with self.assertRaisesRegex(UserError, 'could create a duplicate'):
                self.shipment.action_generate_packing_options()
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(self.shipment.packing_generation_status, 'failed')

    def test_05c_failed_amazon_operation_does_not_advance_state(self):
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_packing_options', autospec=True,
                return_value={'operationId': PACKING_GENERATE_OPERATION},
            ),
        ):
            self.shipment.action_generate_packing_options()
        job = self.shipment.operation_job_ids
        failed_response = {
            'operationId': PACKING_GENERATE_OPERATION,
            'operationStatus': 'FAILED',
            'operationProblems': [{
                'severity': 'ERROR',
                'code': 'InvalidPlanState',
                'message': 'Packing options cannot be generated.',
            }],
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=failed_response,
            ),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.shipment.packing_generation_status, 'failed')
        self.assertEqual(self.shipment.packing_generation_operation_id, PACKING_GENERATE_OPERATION)
        self.assertEqual(self.shipment.packing_error_code, 'InvalidPlanState')
        self.assertEqual(self.shipment.state, 'plan_created')

    def test_05d_expired_packing_options_can_be_regenerated_without_duplicates(self):
        self._generate_packing()
        self.shipment.packing_option_ids.write({
            'status': 'EXPIRED',
            'expiration_date': False,
        })
        self.assertTrue(self.shipment.packing_options_expired)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_packing_options', autospec=True,
                return_value={'operationId': PACKING_REGENERATE_OPERATION},
            ),
        ):
            self.shipment.action_generate_packing_options()
        self.assertEqual(self.shipment.state, 'plan_created')
        self.assertEqual(
            self.shipment.packing_generation_operation_id,
            PACKING_REGENERATE_OPERATION,
        )

        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_id == PACKING_REGENERATE_OPERATION
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PACKING_REGENERATE_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=self._packing_response(),
            ),
        ):
            job._process_operation()
        self.assertEqual(self.shipment.state, 'packing_generated')
        self.assertEqual(len(self.shipment.packing_option_ids), 3)

    def test_05e_expired_placement_options_can_be_regenerated_without_duplicates(self):
        self._generate_packing()
        self._confirm_packing()
        self._generate_placement()
        self.shipment.placement_option_ids.write({
            'status': 'EXPIRED',
            'expiration_date': False,
        })
        self.assertTrue(self.shipment.placement_options_expired)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'generate_placement_options', autospec=True,
                return_value={'operationId': PLACEMENT_REGENERATE_OPERATION},
            ),
        ):
            self.shipment.action_generate_placement_options()
        self.assertEqual(self.shipment.state, 'packing_confirmed')
        self.assertEqual(
            self.shipment.placement_generation_operation_id,
            PLACEMENT_REGENERATE_OPERATION,
        )

        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_id == PLACEMENT_REGENERATE_OPERATION
        )
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=self._success_operation(PLACEMENT_REGENERATE_OPERATION),
            ),
            patch.object(
                AmazonAPI, 'list_placement_options', autospec=True,
                return_value=self._placement_response(),
            ),
        ):
            job._process_operation()
        self.assertEqual(self.shipment.state, 'placement_generated')
        self.assertEqual(len(self.shipment.placement_option_ids), 2)

    def test_06_refresh_job_and_restart_persistence(self):
        action = self.shipment.action_refresh_packing_options()
        self.assertEqual(action['params']['type'], 'success')
        job = self.shipment.operation_job_ids
        job_id = job.id
        self.env.flush_all()
        self.env.invalidate_all()

        reloaded_env = api.Environment(self.env.cr, self.env.uid, dict(self.env.context))
        reloaded_job = reloaded_env['amazon.inbound.operation.job'].sudo().browse(job_id).exists()
        self.assertTrue(reloaded_job)
        self.assertEqual(reloaded_job.operation_type, 'refresh_packing_options')
        self.assertFalse(reloaded_job.operation_id)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=self._packing_response(),
            ),
        ):
            reloaded_job._process_operation()
        self.assertEqual(reloaded_job.state, 'done')
        self.assertEqual(len(self.shipment.packing_option_ids), 3)

    def test_06a_packing_prerequisites_and_identifier_separation(self):
        create_operation_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.shipment.create_operation_id = create_operation_id
        cases = [
            ({'inbound_plan_id': False, 'create_operation_status': 'success'}, 'inboundPlanId'),
            ({'inbound_plan_id': PLAN_ID, 'create_operation_status': 'in_progress'}, 'successfully completed'),
            ({'inbound_plan_id': PLAN_ID, 'create_operation_status': 'failed'}, 'successfully completed'),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                self.shipment.write(values)
                with patch.object(AmazonAPI, 'generate_packing_options', autospec=True) as generate_mock:
                    with self.assertRaisesRegex(UserError, message):
                        self.shipment.action_generate_packing_options()
                self.assertEqual(generate_mock.call_count, 0)

        self.shipment.write({
            'inbound_plan_id': PLAN_ID,
            'create_operation_status': 'success',
        })
        with patch.object(
            AmazonAPI, 'generate_packing_options', autospec=True,
            return_value={'operationId': PACKING_GENERATE_OPERATION},
        ) as generate_mock:
            self.shipment.action_generate_packing_options()
        self.assertEqual(generate_mock.call_args.args[-1], PLAN_ID)
        self.assertEqual(self.shipment.inbound_plan_id, PLAN_ID)
        self.assertEqual(self.shipment.create_operation_id, create_operation_id)
        self.assertEqual(self.shipment.packing_generation_operation_id, PACKING_GENERATE_OPERATION)

    def test_06b_packing_content_exact_and_idempotent(self):
        self.line_a.planned_quantity = 10
        self.line_b.planned_quantity = 5
        with patch.object(
            AmazonAPI, 'list_packing_options', autospec=True,
            return_value=self._packing_response(),
        ):
            self.shipment._refresh_packing_options()
            record_ids = self.shipment.packing_option_ids.ids
            self.shipment._refresh_packing_options()
        self.assertEqual(self.shipment.packing_option_ids.ids, record_ids)
        selected_option = self.shipment.packing_option_ids.filtered(
            lambda option: option.amazon_packing_option_id == PACKING_OPTION_1
        )
        quantities = {
            item.msku: item.quantity
            for item in selected_option.packing_group_ids.item_ids
        }
        self.assertEqual(quantities, {'SKU-A': 10, 'SKU-B': 5})
        self.assertFalse(self.shipment.packing_option_ids.filtered('selected'))

    def test_06c_invalid_packing_content_is_rejected(self):
        invalid_item_sets = {
            'missing': ([
                {'msku': 'SKU-A', 'quantity': 20},
            ], 'do not match the inbound plan'),
            'overpacked': ([
                {'msku': 'SKU-A', 'quantity': 21},
                {'msku': 'SKU-B', 'quantity': 10},
            ], 'do not match the inbound plan'),
            'zero': ([
                {'msku': 'SKU-A', 'quantity': 0},
                {'msku': 'SKU-B', 'quantity': 10},
            ], 'invalid quantity'),
            'negative': ([
                {'msku': 'SKU-A', 'quantity': -1},
                {'msku': 'SKU-B', 'quantity': 10},
            ], 'invalid quantity'),
            'duplicate': ([
                {'msku': 'SKU-A', 'quantity': 10},
                {'msku': 'SKU-A', 'quantity': 10},
                {'msku': 'SKU-B', 'quantity': 10},
            ], 'duplicate MSKU'),
        }
        for label, (item_values, error_pattern) in invalid_item_sets.items():
            self.packing_group_api_mock.side_effect = None
            self.packing_group_api_mock.return_value = {'items': item_values}
            with self.subTest(label=label), patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=self._packing_response(),
            ):
                with self.assertRaisesRegex(UserError, error_pattern):
                    self.shipment._refresh_packing_options()

    def test_06d_selection_is_local_and_confirmation_pending_is_idempotent(self):
        self._generate_packing()
        first = self.shipment.packing_option_ids.filtered(
            lambda option: option.amazon_packing_option_id == PACKING_OPTION_1
        )
        with patch.object(AmazonAPI, 'confirm_packing_option', autospec=True) as confirm_mock:
            first.write({'selected': True})
        self.assertEqual(confirm_mock.call_count, 0)
        self.assertEqual(len(self.shipment.packing_option_ids.filtered('selected')), 1)

        with patch.object(
            AmazonAPI, 'confirm_packing_option', autospec=True,
            return_value={'operationId': PACKING_CONFIRM_OPERATION},
        ) as confirm_mock:
            self.shipment.action_confirm_packing_option()
            duplicate_action = self.shipment.action_confirm_packing_option()
        self.assertEqual(confirm_mock.call_count, 1)
        self.assertEqual(duplicate_action['params']['type'], 'warning')
        self.assertEqual(self.shipment.state, 'packing_generated')
        self.assertEqual(self.shipment.packing_confirmation_status, 'pending')
        self.assertEqual(self.shipment.packing_confirmation_operation_id, PACKING_CONFIRM_OPERATION)

        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_packing_option'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value={
                'operationId': PACKING_CONFIRM_OPERATION,
                'operationStatus': 'IN_PROGRESS',
                'operationProblems': [],
            },
        ):
            job._process_operation()
        self.assertEqual(self.shipment.packing_confirmation_status, 'in_progress')
        self.assertEqual(self.shipment.state, 'packing_generated')
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()

    def test_06e_packing_confirmation_failure_blocks_placement(self):
        self._generate_packing()
        selected = self.shipment.packing_option_ids[0]
        selected.write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_packing_option', autospec=True,
            return_value={'operationId': PACKING_CONFIRM_OPERATION},
        ):
            self.shipment.action_confirm_packing_option()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_packing_option'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value={
                'operationId': PACKING_CONFIRM_OPERATION,
                'operationStatus': 'FAILED',
                'operationProblems': [{
                    'severity': 'ERROR', 'code': 'PackingRejected',
                    'message': 'Packing selection was rejected.',
                }],
            },
        ):
            job._process_operation()
        self.assertEqual(self.shipment.packing_confirmation_status, 'failed')
        self.assertEqual(self.shipment.packing_error_code, 'PackingRejected')
        self.assertIn('rejected', self.shipment.packing_error_message)
        self.assertEqual(self.shipment.packing_confirmation_operation_id, PACKING_CONFIRM_OPERATION)
        self.assertEqual(self.shipment.state, 'packing_generated')
        self.assertEqual(self.shipment.packing_option_ids.filtered('selected'), selected)
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()

    def test_06f_placement_prerequisites_pending_and_failure(self):
        self.shipment.write({
            'state': 'packing_confirmed',
            'packing_confirmation_status': 'success',
        })
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()

        self.shipment.write({'state': 'plan_created', 'packing_confirmation_status': False})
        self._generate_packing()
        self.shipment.packing_option_ids[0].write({'selected': True})
        self.shipment.write({'packing_confirmation_status': 'in_progress'})
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()
        self.shipment.write({'packing_confirmation_status': 'failed'})
        with self.assertRaisesRegex(UserError, 'successfully confirmed'):
            self.shipment.action_generate_placement_options()

    def test_06g_placement_pending_failure_and_no_fabricated_shipment(self):
        self._generate_packing()
        self._confirm_packing()
        self._generate_placement()
        selected = self.shipment.placement_option_ids.filtered(
            lambda option: option.amazon_placement_option_id == PLACEMENT_OPTION_1
        )
        selected.write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_placement_option', autospec=True,
            return_value={'operationId': PLACEMENT_CONFIRM_OPERATION},
        ) as confirm_mock:
            self.shipment.action_confirm_placement_option()
            duplicate_action = self.shipment.action_confirm_placement_option()
        self.assertEqual(confirm_mock.call_count, 1)
        self.assertEqual(duplicate_action['params']['type'], 'warning')
        self.assertEqual(self.shipment.placement_confirmation_status, 'pending')
        self.assertEqual(self.shipment.state, 'placement_generated')
        self.assertFalse(self.shipment.physical_shipment_ids)

        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_placement_option'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value={
                'operationId': PLACEMENT_CONFIRM_OPERATION,
                'operationStatus': 'FAILED',
                'operationProblems': [{
                    'severity': 'ERROR', 'code': 'PlacementRejected',
                    'message': 'Placement selection was rejected.',
                }],
            },
        ):
            job._process_operation()
        self.assertEqual(self.shipment.placement_confirmation_status, 'failed')
        self.assertEqual(self.shipment.placement_error_code, 'PlacementRejected')
        self.assertEqual(self.shipment.placement_confirmation_operation_id, PLACEMENT_CONFIRM_OPERATION)
        self.assertEqual(self.shipment.state, 'placement_generated')
        self.assertEqual(self.shipment.placement_option_ids.filtered('selected'), selected)
        self.assertFalse(self.shipment.physical_shipment_ids)

    def test_06h_multiple_shipments_and_line_distribution(self):
        self._generate_packing()
        self._confirm_packing()
        self._generate_placement()
        self._confirm_placement()

        physical = self.shipment.physical_shipment_ids.sorted('amazon_shipment_id')
        self.assertEqual(len(physical), 2)
        self.assertEqual(set(physical.mapped('amazon_shipment_id')), {SHIPMENT_ID_1, SHIPMENT_ID_2})
        self.assertEqual(
            set(physical.mapped('shipment_confirmation_id')),
            {'FBA-SHIPMENT-A', 'FBA-SHIPMENT-B'},
        )
        self.assertNotIn(self.shipment.inbound_plan_id, physical.mapped('amazon_shipment_id'))
        totals = {}
        for line in physical.line_ids:
            totals[line.msku] = totals.get(line.msku, 0) + line.quantity
        self.assertEqual(totals, {'SKU-A': 20, 'SKU-B': 10})
        self.assertEqual(
            {line.amazon_product_id.sku for line in physical.line_ids},
            {'SKU-A', 'SKU-B'},
        )

    def test_06i_incorrect_shipment_distribution_rolls_back_confirmation(self):
        self._generate_packing()
        self._confirm_packing()
        self._generate_placement()
        selected = self.shipment.placement_option_ids.filtered(
            lambda option: option.amazon_placement_option_id == PLACEMENT_OPTION_1
        )
        selected.write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_placement_option', autospec=True,
            return_value={'operationId': PLACEMENT_CONFIRM_OPERATION},
        ):
            self.shipment.action_confirm_placement_option()
        job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_placement_option'
        )

        def bad_distribution(_api, _instance, _token, _plan_id, shipment_id,
                             _page_size=20, _pagination_token=None):
            items = [{
                'asin': 'B000000001', 'fnsku': 'X000000001',
                'labelOwner': 'SELLER', 'msku': 'SKU-A',
                'prepInstructions': [],
                'quantity': 11 if shipment_id == SHIPMENT_ID_2 else 10,
            }]
            if shipment_id == SHIPMENT_ID_1:
                items.append({
                    'asin': 'B000000002', 'fnsku': 'X000000002',
                    'labelOwner': 'SELLER', 'msku': 'SKU-B',
                    'prepInstructions': [], 'quantity': 10,
                })
            return {'items': items}

        self.shipment_items_api_mock.side_effect = bad_distribution
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=self._success_operation(PLACEMENT_CONFIRM_OPERATION),
        ), patch.object(
            AmazonAPI, 'list_placement_options', autospec=True,
            return_value=self._placement_response(accepted=True),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'in_progress')
        self.assertIn('do not match the inbound plan', job.last_error)
        self.assertNotEqual(self.shipment.placement_confirmation_status, 'success')
        self.assertEqual(self.shipment.state, 'placement_generated')
        self.assertFalse(self.shipment.physical_shipment_ids)

    def test_06j_confirmation_jobs_resume_after_restart_without_resubmit(self):
        self._generate_packing()
        selected = self.shipment.packing_option_ids[0]
        selected.write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_packing_option', autospec=True,
            return_value={'operationId': PACKING_CONFIRM_OPERATION},
        ) as packing_confirm_mock:
            self.shipment.action_confirm_packing_option()
        packing_job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_packing_option'
        )
        packing_job_id = packing_job.id
        self.env.flush_all()
        self.env.invalidate_all()
        reloaded_env = api.Environment(self.env.cr, self.env.uid, dict(self.env.context))
        reloaded_job = reloaded_env['amazon.inbound.operation.job'].sudo().browse(packing_job_id)
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=self._success_operation(PACKING_CONFIRM_OPERATION),
        ), patch.object(
            AmazonAPI, 'list_packing_options', autospec=True,
            return_value=self._packing_response(accepted=True),
        ):
            reloaded_job._process_operation()
        self.assertEqual(packing_confirm_mock.call_count, 1)

        self._generate_placement()
        placement = self.shipment.placement_option_ids.filtered(
            lambda option: option.amazon_placement_option_id == PLACEMENT_OPTION_1
        )
        placement.write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_placement_option', autospec=True,
            return_value={'operationId': PLACEMENT_CONFIRM_OPERATION},
        ) as placement_confirm_mock:
            self.shipment.action_confirm_placement_option()
        placement_job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_placement_option'
        )
        placement_job_id = placement_job.id
        self.env.flush_all()
        self.env.invalidate_all()
        reloaded_env = api.Environment(self.env.cr, self.env.uid, dict(self.env.context))
        reloaded_job = reloaded_env['amazon.inbound.operation.job'].sudo().browse(placement_job_id)
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=self._success_operation(PLACEMENT_CONFIRM_OPERATION),
        ), patch.object(
            AmazonAPI, 'list_placement_options', autospec=True,
            return_value=self._placement_response(accepted=True),
        ):
            reloaded_job._process_operation()
        self.assertEqual(placement_confirm_mock.call_count, 1)
        self.assertEqual(self.shipment.state, 'placement_confirmed')

    def test_06k_transient_poll_failures_preserve_confirmation_operations(self):
        self._generate_packing()
        self.shipment.packing_option_ids[0].write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_packing_option', autospec=True,
            return_value={'operationId': PACKING_CONFIRM_OPERATION},
        ):
            self.shipment.action_confirm_packing_option()
        packing_job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_packing_option'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            side_effect=UserError('HTTP Status: 429; Retry-After: 60'),
        ):
            packing_job._process_operation()
        self.assertEqual(packing_job.state, 'in_progress')
        self.assertEqual(self.shipment.packing_confirmation_operation_id, PACKING_CONFIRM_OPERATION)

        # Resume the same stored operation, then exercise the placement poll.
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=self._success_operation(PACKING_CONFIRM_OPERATION),
        ), patch.object(
            AmazonAPI, 'list_packing_options', autospec=True,
            return_value=self._packing_response(accepted=True),
        ):
            packing_job._process_operation()
        self._generate_placement()
        self.shipment.placement_option_ids[0].write({'selected': True})
        with patch.object(
            AmazonAPI, 'confirm_placement_option', autospec=True,
            return_value={'operationId': PLACEMENT_CONFIRM_OPERATION},
        ):
            self.shipment.action_confirm_placement_option()
        placement_job = self.shipment.operation_job_ids.filtered(
            lambda item: item.operation_type == 'confirm_placement_option'
        )
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            side_effect=UserError('HTTP Status: 503'),
        ):
            placement_job._process_operation()
        self.assertEqual(placement_job.state, 'in_progress')
        self.assertEqual(self.shipment.placement_confirmation_operation_id, PLACEMENT_CONFIRM_OPERATION)
        self.assertFalse(self.shipment.physical_shipment_ids)

    def test_06l_http_retry_after_is_respected_for_read_polling(self):
        throttled = requests.Response()
        throttled.status_code = 429
        throttled.headers['Retry-After'] = '2'
        throttled._content = b'{"errors": [{"code": "QuotaExceeded", "message": "slow down"}]}'
        success = requests.Response()
        success.status_code = 200
        success._content = b'{"operationStatus": "IN_PROGRESS"}'
        with patch('requests.request', side_effect=[throttled, success]) as request_mock, patch.object(
            AmazonAPI, '_log_amazon_request', autospec=True,
        ), patch('time.sleep') as sleep_mock, patch('random.uniform', return_value=0):
            response = AmazonAPI()._amazon_request(
                self.instance, 'test-token', 'GET',
                'https://sellingpartnerapi-na.amazon.com/inbound/fba/2024-03-20/operations/test',
                max_retries=1,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2.0)

    def test_07_box_models_store_local_packing_information(self):
        self._generate_packing()
        option = self.shipment.packing_option_ids[0]
        box = self.env['amazon.fba.box'].sudo().create({
            'packing_option_id': option.id,
            'amazon_packing_group_id': 'pg1234abcd-1234-abcd-5678-1234abcd5678',
            'length': 30,
            'width': 20,
            'height': 10,
            'dimension_unit': 'CM',
            'weight': 5.5,
            'weight_unit': 'KG',
        })
        line = self.env['amazon.fba.box.line'].sudo().create({
            'box_id': box.id,
            'amazon_product_id': self.amazon_product.id,
            'msku': self.amazon_product.sku,
            'quantity': 3,
        })
        self.assertEqual(line.inbound_shipment_id, self.shipment)
        self.assertEqual(line.company_id, self.company)

    def test_07a_packing_information_is_required_and_uses_official_box_schema(self):
        self._generate_packing()
        self._confirm_packing()
        with self.assertRaisesRegex(UserError, 'box-level packing information'):
            self.shipment.action_generate_placement_options()
        body = self._set_packing_information()
        grouping = body['packageGroupings'][0]
        self.assertEqual(grouping['packingGroupId'], PACKING_GROUP_1)
        self.assertNotIn('shipmentId', grouping)
        box = grouping['boxes'][0]
        self.assertEqual(box['contentInformationSource'], 'BOX_CONTENT_PROVIDED')
        self.assertEqual(box['quantity'], 1)
        self.assertEqual(box['dimensions']['unitOfMeasurement'], 'CM')
        self.assertEqual(box['weight'], {'unit': 'KG', 'value': 5.5})
        self.assertEqual(
            {(item['msku'], item['quantity'], item['prepOwner'], item['labelOwner'])
             for item in box['items']},
            {('SKU-A', 20, 'SELLER', 'SELLER'), ('SKU-B', 10, 'SELLER', 'SELLER')},
        )
        self.assertEqual(self.shipment.packing_information_status, 'success')

    def test_08_full_phase_does_not_change_stock(self):
        Picking = self.env['stock.picking'].sudo()
        Move = self.env['stock.move'].sudo()
        MoveLine = self.env['stock.move.line'].sudo()
        Quant = self.env['stock.quant'].sudo()
        before_pickings = Picking.search_count([])
        before_moves = Move.search_count([])
        before_move_lines = MoveLine.search_count([])
        before_quantities = {quant.id: quant.quantity for quant in Quant.search([])}

        self._generate_packing()
        self._confirm_packing()
        self._generate_placement()
        self._confirm_placement()

        self.assertEqual(Picking.search_count([]), before_pickings)
        self.assertEqual(Move.search_count([]), before_moves)
        self.assertEqual(MoveLine.search_count([]), before_move_lines)
        self.assertEqual(
            {quant.id: quant.quantity for quant in Quant.search([])},
            before_quantities,
        )

    def test_09_repeated_packing_information_click_does_not_rebuild_payload(self):
        self._generate_packing()
        self._confirm_packing()
        self._set_packing_information()
        self.shipment.packing_option_ids.filtered('selected').box_ids.unlink()

        with patch.object(AmazonAPI, 'set_packing_information', autospec=True) as set_mock:
            action = self.shipment.action_set_packing_information()

        self.assertEqual(action['params']['type'], 'warning')
        set_mock.assert_not_called()

    def test_api_wrappers_use_official_v2024_paths(self):
        class Response:
            headers = {'x-amzn-RequestId': 'phase3-wrapper-request'}

            @staticmethod
            def json():
                return {'operationId': PACKING_GENERATE_OPERATION}

        api_client = AmazonAPI()
        with patch.object(api_client, '_amazon_request', return_value=Response()) as request_mock:
            api_client.generate_packing_options(self.instance, 'test-token', PLAN_ID)
            self.assertEqual(request_mock.call_args.args[2], 'POST')
            self.assertTrue(request_mock.call_args.args[3].endswith('/packingOptions'))
            self.assertEqual(request_mock.call_args.kwargs['max_retries'], 0)

            api_client.list_packing_options(
                self.instance, 'test-token', PLAN_ID, 20, 'packing-next',
            )
            self.assertEqual(request_mock.call_args.args[2], 'GET')
            self.assertEqual(request_mock.call_args.kwargs['params']['paginationToken'], 'packing-next')

            api_client.confirm_packing_option(
                self.instance, 'test-token', PLAN_ID, PACKING_OPTION_1,
            )
            self.assertTrue(request_mock.call_args.args[3].endswith(
                '/packingOptions/%s/confirmation' % PACKING_OPTION_1
            ))
            self.assertEqual(request_mock.call_args.kwargs['max_retries'], 0)

            api_client.set_packing_information(
                self.instance, 'test-token', PLAN_ID, {'packageGroupings': []},
            )
            self.assertTrue(request_mock.call_args.args[3].endswith('/packingInformation'))
            self.assertEqual(request_mock.call_args.kwargs['max_retries'], 0)

            self.real_list_packing_group_items(
                api_client, self.instance, 'test-token', PLAN_ID,
                PACKING_GROUP_1, 20, 'group-next',
            )
            self.assertTrue(request_mock.call_args.args[3].endswith(
                '/packingGroups/%s/items' % PACKING_GROUP_1
            ))
            self.assertEqual(request_mock.call_args.kwargs['params']['paginationToken'], 'group-next')

            api_client.generate_placement_options(self.instance, 'test-token', PLAN_ID)
            self.assertEqual(request_mock.call_args.kwargs['body'], {})
            self.assertEqual(request_mock.call_args.kwargs['max_retries'], 0)
            self.assertTrue(request_mock.call_args.args[3].endswith('/placementOptions'))

            api_client.list_placement_options(
                self.instance, 'test-token', PLAN_ID, 20, 'placement-next',
            )
            self.assertEqual(request_mock.call_args.kwargs['params']['paginationToken'], 'placement-next')

            api_client.confirm_placement_option(
                self.instance, 'test-token', PLAN_ID, PLACEMENT_OPTION_1,
            )
            self.assertTrue(request_mock.call_args.args[3].endswith(
                '/placementOptions/%s/confirmation' % PLACEMENT_OPTION_1
            ))
            self.assertEqual(request_mock.call_args.kwargs['max_retries'], 0)

            self.real_get_shipment(
                api_client, self.instance, 'test-token', PLAN_ID, SHIPMENT_ID_1,
            )
            self.assertTrue(request_mock.call_args.args[3].endswith(
                '/shipments/%s' % SHIPMENT_ID_1
            ))

            self.real_list_shipment_items(
                api_client, self.instance, 'test-token', PLAN_ID,
                SHIPMENT_ID_1, 20, 'shipment-next',
            )
            self.assertTrue(request_mock.call_args.args[3].endswith(
                '/shipments/%s/items' % SHIPMENT_ID_1
            ))
            self.assertEqual(request_mock.call_args.kwargs['params']['paginationToken'], 'shipment-next')
