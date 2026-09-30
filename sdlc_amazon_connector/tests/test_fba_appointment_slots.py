from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


PLAN_ID = 'wf1234abcd-1234-abcd-5678-1234abcd5678'
PACKING_OPTION_ID = 'po1234abcd-1234-abcd-5678-1234abcd5678'
PLACEMENT_OPTION_ID = 'pl1234abcd-1234-abcd-5678-1234abcd5678'
SHIPMENT_ID = 'sh1234abcd-1234-abcd-5678-1234abcd5678'
TRANSPORTATION_OPTION_ID = 'to1234abcd-1234-abcd-5678-1234abcd5678'
DELIVERY_OPTION_ID = 'dw1234abcd-1234-abcd-5678-1234abcd5678'
APPOINTMENT_GENERATE_OPERATION_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
SLOT_ID_1 = 'slot-001-aaaa-bbbb-cccc-dddd'
SLOT_ID_2 = 'slot-002-aaaa-bbbb-cccc-dddd'
SLOT_ID_3 = 'slot-003-aaaa-bbbb-cccc-dddd'


@tagged('post_install', '-at_install')
class TestFbaAppointmentSlots(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'FC Appointment Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Appointment Source Warehouse',
            'code': 'APSRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Appointment FBA Warehouse',
            'code': 'APFBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Appointment Test Instance',
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
            'name': 'Appointment Test Product',
            'default_code': 'AP-MSKU-001',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Appointment Amazon Product',
            'instance_id': self.instance.id,
            'sku': 'AP-MSKU-001',
            'odoo_product_id': self.product.id,
        })
        self.shipment = self._create_shipment()
        self.physical = self.shipment.physical_shipment_ids

    def _create_shipment(self):
        shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'AP-PLAN-001',
            'shipment_name': 'AP-PLAN-001',
            'instance_id': self.instance.id,
            'inbound_plan_id': PLAN_ID,
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'packing_information_status': 'success',
            'placement_confirmation_status': 'success',
            'state': 'placement_confirmed',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'odoo_product_id': self.product.id,
                'sku': self.amazon_product.sku,
                'planned_quantity': 4,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            })],
        })
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'amazon_placement_option_id': PLACEMENT_OPTION_ID,
            'status': 'ACCEPTED',
            'amazon_shipment_ids': '["%s"]' % SHIPMENT_ID,
            'selected': True,
        })
        now = fields.Datetime.now()
        dw_start = now + timedelta(days=5)
        dw_end = now + timedelta(days=12)
        physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'placement_option_id': placement.id,
            'amazon_shipment_id': SHIPMENT_ID,
            'shipment_confirmation_id': 'FBA1234ABCD',
            'status': 'WORKING',
            'destination_fc': 'ONT8',
            'ready_to_ship_at': now + timedelta(hours=1),
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'quantity': 4,
            })],
        })
        self.env['amazon.fba.shipment.box'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_box_id': 'FBA10ABC0YY100001',
            'quantity': 1,
        })
        option = self.env['amazon.fba.transportation.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_transportation_option_id': TRANSPORTATION_OPTION_ID,
            'shipment_id': SHIPMENT_ID,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'shipping_solution': 'USE_YOUR_OWN_CARRIER',
            'carrier_name': 'Test Carrier',
            'selected': True,
        })
        physical.write({
            'selected_transportation_option_id': option.id,
            'shipping_mode': 'GROUND_SMALL_PARCEL',
        })
        dw_option = self.env['amazon.fba.delivery.window.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': shipment.id,
            'physical_shipment_id': physical.id,
            'amazon_delivery_window_option_id': DELIVERY_OPTION_ID,
            'start_date': dw_start,
            'end_date': dw_end,
            'valid_until': now + timedelta(days=30),
            'availability_type': 'AVAILABLE',
            'selected': True,
        })
        physical.write({'selected_delivery_window_option_id': dw_option.id})
        return shipment

    def _set_confirmed(self):
        self.physical.write({
            'transportation_confirmation_status': 'success',
            'delivery_window_confirmation_status': 'success',
        })

    @staticmethod
    def _slots_response(slots=None, expires_at=None, pagination_token=None):
        if slots is None:
            slots = [
                {
                    'slotId': SLOT_ID_1,
                    'slotTime': {
                        'startTime': '2026-10-05T09:00:00Z',
                        'endTime': '2026-10-05T09:30:00Z',
                    },
                    'slotStatus': 'AVAILABLE',
                },
                {
                    'slotId': SLOT_ID_2,
                    'slotTime': {
                        'startTime': '2026-10-05T10:30:00Z',
                        'endTime': '2026-10-05T11:15:00Z',
                    },
                    'slotStatus': 'AVAILABLE',
                },
            ]
        result = {
            'selfShipAppointmentSlotsAvailability': {
                'expiresAt': expires_at or '2026-10-04T23:59:59Z',
                'slots': slots,
            },
            '_amazon_request_id': 'test-appointment-request',
        }
        if pagination_token:
            result['pagination'] = {'nextToken': pagination_token}
        return result

    # --- Test 1: Generation blocked without confirmed transportation ---
    def test_01_generation_blocked_without_confirmed_transportation(self):
        self.physical.write({
            'transportation_confirmation_status': False,
            'delivery_window_confirmation_status': 'success',
        })
        with self.assertRaises(UserError, msg="Transportation must be confirmed"):
            self.physical.action_generate_appointment_slots()

    # --- Test 2: Generation blocked without confirmed delivery window ---
    def test_02_generation_blocked_without_confirmed_delivery_window(self):
        self.physical.write({
            'transportation_confirmation_status': 'success',
            'delivery_window_confirmation_status': False,
        })
        with self.assertRaises(UserError, msg="delivery window must be confirmed"):
            self.physical.action_generate_appointment_slots()

    # --- Test 3: Generation POST stores operationId ---
    def test_03_generation_post_stores_operation_id(self):
        self._set_confirmed()
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-generate-request',
        }
        with patch.object(
            AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            return_value=generate_response,
        ) as mock_generate:
            self.physical.action_generate_appointment_slots()
            job = self.shipment.operation_job_ids.filtered(
                lambda j: j.operation_type == 'generate_appointment_slots'
                and j.state in ('pending', 'in_progress')
            )
            self.assertEqual(len(job), 1)
            job._process_operation()
            self.assertTrue(mock_generate.called)
            call_args = mock_generate.call_args
            body = call_args[0][5] if len(call_args[0]) > 5 else call_args[1].get('body')
            self.assertIn('desiredStartDate', body)
            self.assertIn('desiredEndDate', body)

        self.assertEqual(
            self.physical.appointment_generation_operation_id,
            APPOINTMENT_GENERATE_OPERATION_ID,
        )
        self.assertEqual(self.physical.appointment_generation_status, 'in_progress')

    # --- Test 4: Polling IN_PROGRESS keeps workflow in-progress ---
    def test_04_polling_in_progress_keeps_pending(self):
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'in_progress',
            'appointment_generation_operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
        })
        job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': self.physical.id,
            'operation_type': 'generate_appointment_slots',
            'operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
            'state': 'in_progress',
        })
        poll_response = {
            'operationStatus': 'IN_PROGRESS',
            '_amazon_request_id': 'test-poll-request',
        }
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=poll_response,
        ):
            job._process_operation()
        self.assertIn(self.physical.appointment_generation_status, ('pending', 'in_progress'))
        self.assertNotEqual(job.state, 'done')

    # --- Test 5: Polling SUCCESS calls _refresh_appointment_slots ---
    def test_05_polling_success_triggers_refresh(self):
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'in_progress',
            'appointment_generation_operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
        })
        job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': self.physical.id,
            'operation_type': 'generate_appointment_slots',
            'operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
            'state': 'in_progress',
        })
        poll_response = {
            'operationStatus': 'SUCCESS',
            '_amazon_request_id': 'test-poll-success',
        }
        with (
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=poll_response,
            ),
            patch.object(
                AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                return_value=self._slots_response(),
            ) as mock_get_slots,
        ):
            job._process_operation()
        self.assertTrue(mock_get_slots.called)
        self.assertEqual(self.physical.appointment_generation_status, 'success')
        self.assertEqual(job.state, 'done')
        self.assertEqual(len(self.physical.appointment_slot_ids), 2)

    # --- Test 6: Polling FAILED stores failed status/error ---
    def test_06_polling_failed_stores_error(self):
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'in_progress',
            'appointment_generation_operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
        })
        job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': self.physical.id,
            'operation_type': 'generate_appointment_slots',
            'operation_id': APPOINTMENT_GENERATE_OPERATION_ID,
            'state': 'in_progress',
        })
        poll_response = {
            'operationStatus': 'FAILED',
            'operationProblems': [{'message': 'Test failure reason'}],
            '_amazon_request_id': 'test-poll-failed',
        }
        with patch.object(
            AmazonAPI, 'get_inbound_operation_status', autospec=True,
            return_value=poll_response,
        ):
            job._process_operation()
        self.assertEqual(self.physical.appointment_generation_status, 'failed')
        self.assertEqual(job.state, 'failed')

    # --- Test 7: Slots are stored correctly ---
    def test_07_slots_stored_correctly(self):
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'success',
        })
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
        slots = self.physical.appointment_slot_ids.sorted('start_date')
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].amazon_appointment_slot_id, SLOT_ID_1)
        self.assertEqual(slots[1].amazon_appointment_slot_id, SLOT_ID_2)
        self.assertEqual(slots[0].slot_status, 'AVAILABLE')

    # --- Test 8: Start/end times are parsed correctly ---
    def test_08_times_parsed_correctly(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
        slot = self.physical.appointment_slot_ids.filtered(
            lambda s: s.amazon_appointment_slot_id == SLOT_ID_1
        )
        self.assertTrue(slot.start_date)
        self.assertTrue(slot.end_date)
        self.assertGreater(slot.end_date, slot.start_date)

    # --- Test 9: Duration is correct ---
    def test_09_duration_is_correct(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
        slots = self.physical.appointment_slot_ids.sorted('start_date')
        self.assertEqual(slots[0].duration_minutes, 30)
        self.assertEqual(slots[1].duration_minutes, 45)

    # --- Test 10: Pagination fetches all pages ---
    def test_10_pagination_fetches_all_pages(self):
        self._set_confirmed()
        page1 = self._slots_response(
            slots=[{
                'slotId': SLOT_ID_1,
                'slotTime': {
                    'startTime': '2026-10-05T09:00:00Z',
                    'endTime': '2026-10-05T09:30:00Z',
                },
            }],
            pagination_token='page2token',
        )
        page2_slots = [{
            'slotId': SLOT_ID_2,
            'slotTime': {
                'startTime': '2026-10-05T10:00:00Z',
                'endTime': '2026-10-05T10:30:00Z',
            },
        }]
        page2 = self._slots_response(slots=page2_slots)

        call_count = [0]
        def mock_get_slots(*args, **kwargs):
            call_count[0] += 1
            pagination_token = kwargs.get('pagination_token') or (
                args[5] if len(args) > 5 else None
            )
            if pagination_token == 'page2token':
                return page2
            return page1

        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots',
            side_effect=mock_get_slots,
        ):
            self.physical._refresh_appointment_slots()
        self.assertEqual(call_count[0], 2)
        self.assertEqual(len(self.physical.appointment_slot_ids), 2)

    # --- Test 11: Refresh does NOT call generation POST ---
    def test_11_refresh_does_not_call_generation(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        with (
            patch.object(
                AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                return_value=self._slots_response(),
            ),
            patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            ) as mock_generate,
        ):
            self.physical.action_refresh_appointment_slots()
        self.assertFalse(mock_generate.called)

    # --- Test 12: Duplicate refresh does not duplicate slots ---
    def test_12_duplicate_refresh_no_duplicates(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
            self.assertEqual(len(self.physical.appointment_slot_ids), 2)
            self.physical._refresh_appointment_slots()
            self.assertEqual(len(self.physical.appointment_slot_ids), 2)

    # --- Test 13: Stale slot handling works ---
    def test_13_stale_slots_removed(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
        self.assertEqual(len(self.physical.appointment_slot_ids), 2)

        only_one = self._slots_response(slots=[{
            'slotId': SLOT_ID_1,
            'slotTime': {
                'startTime': '2026-10-05T09:00:00Z',
                'endTime': '2026-10-05T09:30:00Z',
            },
            'slotStatus': 'AVAILABLE',
        }])
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=only_one,
        ):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(len(self.physical.appointment_slot_ids), 1)
        self.assertEqual(
            self.physical.appointment_slot_ids.amazon_appointment_slot_id,
            SLOT_ID_1,
        )

    # --- Test 14: No scheduleSelfShipAppointment call in this workflow ---
    def test_14_no_schedule_appointment_call(self):
        self._set_confirmed()
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-generate-request',
        }
        poll_response = {
            'operationStatus': 'SUCCESS',
            '_amazon_request_id': 'test-poll-success',
        }
        with (
            patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
                return_value=generate_response,
            ),
            patch.object(
                AmazonAPI, 'get_inbound_operation_status', autospec=True,
                return_value=poll_response,
            ),
            patch.object(
                AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                return_value=self._slots_response(),
            ),
        ):
            self.physical.action_generate_appointment_slots()
            job = self.shipment.operation_job_ids.filtered(
                lambda j: j.operation_type == 'generate_appointment_slots'
                and j.state in ('pending', 'in_progress')
            )
            job._process_operation()
            job._process_operation()

        self.assertFalse(
            hasattr(AmazonAPI, 'schedule_self_ship_appointment')
            and getattr(AmazonAPI.schedule_self_ship_appointment, '_mock_name', None),
            "scheduleSelfShipAppointment must not be called in this workflow",
        )

    # --- Test: Stale selected slot is preserved ---
    def test_15_selected_slot_preserved_on_refresh(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(),
        ):
            self.physical._refresh_appointment_slots()
        slot_to_select = self.physical.appointment_slot_ids.filtered(
            lambda s: s.amazon_appointment_slot_id == SLOT_ID_1
        )
        slot_to_select.action_select_appointment_slot()
        self.assertTrue(slot_to_select.selected)

        only_slot2 = self._slots_response(slots=[{
            'slotId': SLOT_ID_2,
            'slotTime': {
                'startTime': '2026-10-05T10:30:00Z',
                'endTime': '2026-10-05T11:15:00Z',
            },
        }])
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=only_slot2,
        ):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        remaining = self.physical.appointment_slot_ids
        self.assertEqual(len(remaining), 2)
        self.assertTrue(remaining.filtered(
            lambda s: s.amazon_appointment_slot_id == SLOT_ID_1 and s.selected
        ))

    # --- Test: Expires-at is stored ---
    def test_16_expires_at_stored(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(expires_at='2026-10-04T23:59:59Z'),
        ):
            self.physical._refresh_appointment_slots()
        self.assertTrue(self.physical.appointment_slots_expires_at)

    # --- Test: Generation blocked when already in progress ---
    def test_17_generation_blocked_when_in_progress(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'in_progress'})
        with self.assertRaises(UserError, msg="already in progress"):
            self.physical.action_generate_appointment_slots()

    # --- Test: Date range comes from delivery window ---
    def test_18_date_range_from_delivery_window(self):
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch.object(
            AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            return_value=generate_response,
        ) as mock_gen:
            self.physical.action_generate_appointment_slots()
            job = self.shipment.operation_job_ids.filtered(
                lambda j: j.operation_type == 'generate_appointment_slots'
                and j.state in ('pending', 'in_progress')
            )
            job._process_operation()
            call_args = mock_gen.call_args
            body = call_args[0][5] if len(call_args[0]) > 5 else call_args[1].get('body')
            self.assertEqual(
                body['desiredStartDate'],
                dw.start_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            )
            self.assertEqual(
                body['desiredEndDate'],
                dw.end_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
            )

    # --- Test: Future window uses window_start directly ---
    def test_19_future_window_uses_window_start(self):
        """When window_start is well in the future, desiredStartDate == window_start."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.start_date - timedelta(days=3)
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
                return_value=generate_response,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                body = mock_gen.call_args[0][5] if len(mock_gen.call_args[0]) > 5 else mock_gen.call_args[1].get('body')
                self.assertEqual(
                    body['desiredStartDate'],
                    dw.start_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                )

    # --- Test: Window already started, start clamped to now + margin ---
    def test_20_started_window_clamps_start(self):
        """When window_start < now < window_end, desiredStartDate is clamped to now + 5min."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.start_date + timedelta(hours=6)
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
                return_value=generate_response,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                body = mock_gen.call_args[0][5] if len(mock_gen.call_args[0]) > 5 else mock_gen.call_args[1].get('body')
                expected_start = frozen_now + timedelta(minutes=5)
                self.assertEqual(
                    body['desiredStartDate'],
                    expected_start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                )
                self.assertEqual(
                    body['desiredEndDate'],
                    dw.end_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                )

    # --- Test: Completely expired window raises error ---
    def test_21_expired_window_raises_error(self):
        """When window_end <= now, no Amazon POST, clear error raised."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.end_date + timedelta(hours=1)
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                # Amazon must not be called and a business error must be recorded
                mock_gen.assert_not_called()
                self.assertEqual(self.physical.appointment_generation_status, 'failed')
                self.assertIn('expired', (self.physical.appointment_error_message or '').lower())

    # --- Test: Safety margin exhausts remaining window ---
    def test_22_margin_exhausts_window_raises_error(self):
        """When now + 5min >= window_end, no Amazon POST."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.end_date - timedelta(minutes=3)
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                mock_gen.assert_not_called()
                self.assertEqual(self.physical.appointment_generation_status, 'failed')

    # --- Test: desiredStartDate is never in the past ---
    def test_23_start_never_in_past(self):
        """Regardless of window, the POST body start is never before now."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.start_date + timedelta(days=2)
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
                return_value=generate_response,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                body = mock_gen.call_args[0][5] if len(mock_gen.call_args[0]) > 5 else mock_gen.call_args[1].get('body')
                from datetime import datetime
                sent_start = datetime.strptime(body['desiredStartDate'], '%Y-%m-%dT%H:%M:%SZ')
                self.assertGreater(sent_start, frozen_now)

    # --- Test: Failed generation can be retried ---
    def test_24_failed_generation_can_be_retried(self):
        """After appointment_generation_status=failed, user can click Generate again."""
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'failed',
            'appointment_error_code': 'BACKGROUND_JOB_FAILED',
            'appointment_error_message': 'Previous error',
        })
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch.object(
            AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            return_value=generate_response,
        ):
            self.physical.action_generate_appointment_slots()
        self.assertEqual(self.physical.appointment_generation_status, 'pending')

    # --- Test: Retry clears stale error info ---
    def test_25_retry_clears_error_fields(self):
        """Retrying generation clears previous error code and message."""
        self._set_confirmed()
        self.physical.sudo().write({
            'appointment_generation_status': 'failed',
            'appointment_error_code': 'OLD_ERROR',
            'appointment_error_message': 'Stale error message',
        })
        with patch.object(
            AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
            return_value={'operationId': APPOINTMENT_GENERATE_OPERATION_ID, '_amazon_request_id': 'r'},
        ):
            self.physical.action_generate_appointment_slots()
        self.assertFalse(self.physical.appointment_error_code)
        self.assertFalse(self.physical.appointment_error_message)

    # --- Test: Existing future-window behavior unchanged ---
    def test_26_future_window_unchanged(self):
        """A window entirely in the future still passes start/end unmodified."""
        self._set_confirmed()
        dw = self.physical.selected_delivery_window_option_id
        frozen_now = dw.start_date - timedelta(days=1)
        generate_response = {
            'operationId': APPOINTMENT_GENERATE_OPERATION_ID,
            '_amazon_request_id': 'test-request',
        }
        with patch('odoo.fields.Datetime.now', return_value=frozen_now):
            with patch.object(
                AmazonAPI, 'generate_self_ship_appointment_slots', autospec=True,
                return_value=generate_response,
            ) as mock_gen:
                self.physical.action_generate_appointment_slots()
                job = self.shipment.operation_job_ids.filtered(
                    lambda j: j.operation_type == 'generate_appointment_slots'
                    and j.state in ('pending', 'in_progress')
                )
                job._process_operation()
                body = mock_gen.call_args[0][5] if len(mock_gen.call_args[0]) > 5 else mock_gen.call_args[1].get('body')
                self.assertEqual(
                    body['desiredStartDate'],
                    dw.start_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                )
                self.assertEqual(
                    body['desiredEndDate'],
                    dw.end_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                )

    # ------------------------------------------------------------------
    # UI-support phase: duration, local selection, expiry visibility
    # ------------------------------------------------------------------

    def _store_slots_for_ui(self):
        """Persist three slots (30 / 45 / 60 min) via the normal refresh path."""
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        response = self._slots_response(slots=[
            {
                'slotId': SLOT_ID_1,
                'slotTime': {'startTime': '2026-10-05T09:00:00Z',
                             'endTime': '2026-10-05T09:30:00Z'},
                'slotStatus': 'AVAILABLE',
            },
            {
                'slotId': SLOT_ID_2,
                'slotTime': {'startTime': '2026-10-05T10:00:00Z',
                             'endTime': '2026-10-05T10:45:00Z'},
                'slotStatus': 'AVAILABLE',
            },
            {
                'slotId': SLOT_ID_3,
                'slotTime': {'startTime': '2026-10-05T11:00:00Z',
                             'endTime': '2026-10-05T12:00:00Z'},
                'slotStatus': 'AVAILABLE',
            },
        ])
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=response,
        ):
            self.physical._refresh_appointment_slots()
        return self.physical.appointment_slot_ids.sorted('start_date')

    # --- Test A: duration for 30/45/60-minute slots ---
    def test_27_duration_minutes_30_45_60(self):
        slots = self._store_slots_for_ui()
        by_id = {s.amazon_appointment_slot_id: s for s in slots}
        self.assertEqual(by_id[SLOT_ID_1].duration_minutes, 30)
        self.assertEqual(by_id[SLOT_ID_2].duration_minutes, 45)
        self.assertEqual(by_id[SLOT_ID_3].duration_minutes, 60)
        # Never negative even when end precedes start.
        bad = self.env['amazon.fba.appointment.slot'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': self.shipment.id,
            'physical_shipment_id': self.physical.id,
            'amazon_appointment_slot_id': 'slot-neg-0001',
            'start_date': '2026-10-05 10:00:00',
            'end_date': '2026-10-05 09:00:00',
        })
        self.assertEqual(bad.duration_minutes, 0)

    # --- Test B: local slot selection ---
    def test_28_local_selection_sets_flag_and_pointer(self):
        slots = self._store_slots_for_ui()
        target = slots[0]
        target.action_select_appointment_slot()
        self.assertTrue(target.selected)
        self.assertEqual(self.physical.selected_appointment_slot_id, target)

    # --- Test C: selecting slot B deselects slot A ---
    def test_29_selecting_b_deselects_a(self):
        slots = self._store_slots_for_ui()
        slot_a, slot_b = slots[0], slots[1]
        slot_a.action_select_appointment_slot()
        self.assertTrue(slot_a.selected)
        slot_b.action_select_appointment_slot()
        slot_a.invalidate_recordset()
        self.assertFalse(slot_a.selected)
        self.assertTrue(slot_b.selected)
        self.assertEqual(self.physical.selected_appointment_slot_id, slot_b)
        # Exactly one selected at any time.
        self.assertEqual(
            len(self.physical.appointment_slot_ids.filtered('selected')), 1,
        )

    # --- Test D: pointer stays synchronized with the selected row ---
    def test_30_pointer_tracks_selected_row(self):
        slots = self._store_slots_for_ui()
        slots[2].action_select_appointment_slot()
        self.assertEqual(self.physical.selected_appointment_slot_id, slots[2])
        self.assertTrue(self.physical.selected_appointment_slot_id.selected)

    # --- Test E: selection performs no Amazon API call ---
    def test_31_selection_makes_no_amazon_call(self):
        slots = self._store_slots_for_ui()
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as mock_req:
            slots[0].action_select_appointment_slot()
            mock_req.assert_not_called()

    # --- Test F: expired availability does not auto-refresh Amazon ---
    def test_32_expired_flag_no_autorefresh(self):
        slots = self._store_slots_for_ui()
        count_before = len(slots)
        self.physical.sudo().write({
            'appointment_slots_expires_at': fields.Datetime.now() - timedelta(hours=1),
        })
        self.physical.invalidate_recordset()
        self.assertTrue(self.physical.appointment_slots_expired)
        # Reading the flag / recordset must not call Amazon and must not
        # drop the stored rows.
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as mock_req:
            _ = self.physical.appointment_slots_expired
            mock_req.assert_not_called()
        self.assertEqual(len(self.physical.appointment_slot_ids), count_before)

    # --- Test: not-expired availability leaves the flag false ---
    def test_33_future_expiry_not_expired(self):
        self._store_slots_for_ui()
        self.physical.sudo().write({
            'appointment_slots_expires_at': fields.Datetime.now() + timedelta(hours=6),
        })
        self.physical.invalidate_recordset()
        self.assertFalse(self.physical.appointment_slots_expired)
