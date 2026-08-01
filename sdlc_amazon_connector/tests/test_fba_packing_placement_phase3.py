from unittest.mock import patch

from odoo import api
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_1 = 'po1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_2 = 'po5678abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_1 = 'pl1234abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_2 = 'pl5678abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID_1 = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID_2 = 'sh5678abcd-1234-abcd-5678-1234abcd5678'
PACKING_GENERATE_OPERATION = '11111111-1111-1111-1111-111111111111'
PACKING_CONFIRM_OPERATION = '22222222-2222-2222-2222-222222222222'
PLACEMENT_GENERATE_OPERATION = '33333333-3333-3333-3333-333333333333'
PLACEMENT_CONFIRM_OPERATION = '44444444-4444-4444-4444-444444444444'
PACKING_REGENERATE_OPERATION = '55555555-5555-5555-5555-555555555555'
PLACEMENT_REGENERATE_OPERATION = '66666666-6666-6666-6666-666666666666'


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
            'name': 'Phase 3 Product',
            'default_code': 'P3-MSKU-001',
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Phase 3 Amazon Product',
            'instance_id': self.instance.id,
            'sku': 'P3-MSKU-001',
            'odoo_product_id': self.odoo_product.id,
        })
        self.shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P3-PLAN-001',
            'shipment_name': 'Phase 3 Plan',
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID,
            'create_operation_status': 'success',
            'state': 'plan_created',
        })

    @staticmethod
    def _packing_response(accepted=False):
        return {
            'packingOptions': [
                {
                    'packingOptionId': PACKING_OPTION_1,
                    'status': 'ACCEPTED' if accepted else 'OFFERED',
                    'packingGroups': ['pg1234abcd-1234-abcd-5678-1234abcd5678'],
                    'fees': [{
                        'type': 'FEE',
                        'target': 'Placement Services',
                        'description': 'Packing fee',
                        'value': {'amount': 1.25, 'code': 'USD'},
                    }],
                    'discounts': [],
                    'expiration': '2030-01-01T00:00:00.000Z',
                    'supportedConfigurations': [],
                    'supportedShippingConfigurations': [],
                },
                {
                    'packingOptionId': PACKING_OPTION_2,
                    'status': 'OFFERED',
                    'packingGroups': ['pg5678abcd-1234-abcd-5678-1234abcd5678'],
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
                    'discounts': [],
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

    def _generate_placement(self):
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
        self.assertEqual(len(self.shipment.packing_option_ids), 2)
        first = self.shipment.packing_option_ids.filtered(
            lambda option: option.amazon_packing_option_id == PACKING_OPTION_1
        )
        self.assertEqual(first.fee_amount, 1.25)
        self.assertEqual(first.fee_currency, 'USD')
        self.assertIn('pg1234abcd', first.amazon_packing_group_ids)

    def test_01b_paginated_packing_refresh_is_idempotent(self):
        response = self._packing_response()
        first_page = {
            'packingOptions': [response['packingOptions'][0]],
            'pagination': {'nextToken': 'packing-next'},
        }
        second_page = {
            'packingOptions': [response['packingOptions'][1]],
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
        self.assertEqual(len(self.shipment.packing_option_ids), 2)

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'list_packing_options', autospec=True,
                return_value=response,
            ),
        ):
            self.shipment._refresh_packing_options()
        self.assertEqual(len(self.shipment.packing_option_ids), 2)

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
        with self.assertRaisesRegex(UserError, 'before packing confirmation'):
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
        self.assertEqual(len(self.shipment.packing_option_ids), 2)

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
        self.assertEqual(len(self.shipment.packing_option_ids), 2)

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

            api_client.generate_placement_options(self.instance, 'test-token', PLAN_ID)
            self.assertEqual(request_mock.call_args.kwargs['body'], {})
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
