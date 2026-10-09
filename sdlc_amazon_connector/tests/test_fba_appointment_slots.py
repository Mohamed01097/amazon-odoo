from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


class _FakeResponse:
    """Minimal object mimicking a requests.Response for schedule-failure tests."""

    def __init__(self, status_code=400, error_code='InvalidInput',
                 message='Slot is no longer available', request_id='req-err-1'):
        self.status_code = status_code
        self._error_code = error_code
        self._message = message
        self.headers = {'x-amzn-RequestId': request_id}
        self.text = '{"errors": [{"code": "%s", "message": "%s"}]}' % (
            error_code, message,
        )

    def json(self):
        return {'errors': [{'code': self._error_code, 'message': self._message}]}


def _http_error(status_code=400):
    resp = _FakeResponse(status_code=status_code)
    exc = requests.exceptions.HTTPError('boom', response=resp)
    # Set a pre-built diagnostic so format_exception is not invoked on the fake.
    exc.amazon_diagnostic = (
        'HTTP Status: %s\nAmazon Error Code: InvalidInput' % status_code
    )
    return exc


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
    def _amazon_timestamp(dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def _odoo_timestamp(dt):
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def _future_datetime(self, days=30, hour=9, minute=0, second=0):
        return (
            fields.Datetime.now() + timedelta(days=days)
        ).replace(hour=hour, minute=minute, second=second, microsecond=0)

    def _future_slot_time(self, hour, minute=0, duration_minutes=30, days=30):
        start = self._future_datetime(days=days, hour=hour, minute=minute)
        end = start + timedelta(minutes=duration_minutes)
        return {
            'startTime': self._amazon_timestamp(start),
            'endTime': self._amazon_timestamp(end),
        }

    def _future_expiry(self, days=29):
        return self._amazon_timestamp(
            self._future_datetime(days=days, hour=23, minute=59, second=59)
        )

    def _slots_response(self, slots=None, expires_at=None, pagination_token=None):
        if slots is None:
            slots = [
                {
                    'slotId': SLOT_ID_1,
                    'slotTime': self._future_slot_time(9, duration_minutes=30),
                    'slotStatus': 'AVAILABLE',
                },
                {
                    'slotId': SLOT_ID_2,
                    'slotTime': self._future_slot_time(10, 30, duration_minutes=45),
                    'slotStatus': 'AVAILABLE',
                },
            ]
        result = {
            'selfShipAppointmentSlotsAvailability': {
                'expiresAt': expires_at or self._future_expiry(),
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
                'slotTime': self._future_slot_time(9, duration_minutes=30),
            }],
            pagination_token='page2token',
        )
        page2_slots = [{
            'slotId': SLOT_ID_2,
            'slotTime': self._future_slot_time(10, duration_minutes=30),
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
            'slotTime': self._future_slot_time(9, duration_minutes=30),
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

    # --- Test: A refresh is authoritative — a selected slot Amazon no longer
    #     returns is removed and the selection pointer is cleared, so a stale
    #     slot can never be scheduled (PART 2 safety, items 14 & 23). ---
    def test_15_stale_selected_slot_cleared_on_refresh(self):
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
        self.assertEqual(self.physical.selected_appointment_slot_id, slot_to_select)

        only_slot2 = self._slots_response(slots=[{
            'slotId': SLOT_ID_2,
            'slotTime': self._future_slot_time(10, 30, duration_minutes=45),
        }])
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=only_slot2,
        ):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        remaining = self.physical.appointment_slot_ids
        # SLOT_ID_1 is gone; only the still-offered slot remains, unselected.
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining.amazon_appointment_slot_id, SLOT_ID_2)
        self.assertFalse(self.physical.selected_appointment_slot_id)

    # --- Test: Expires-at is stored ---
    def test_16_expires_at_stored(self):
        self._set_confirmed()
        with patch.object(
            AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
            return_value=self._slots_response(expires_at=self._future_expiry()),
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
                'slotTime': self._future_slot_time(9, duration_minutes=30),
                'slotStatus': 'AVAILABLE',
            },
            {
                'slotId': SLOT_ID_2,
                'slotTime': self._future_slot_time(10, duration_minutes=45),
                'slotStatus': 'AVAILABLE',
            },
            {
                'slotId': SLOT_ID_3,
                'slotTime': self._future_slot_time(11, duration_minutes=60),
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
            'start_date': self._odoo_timestamp(self._future_datetime(hour=10)),
            'end_date': self._odoo_timestamp(self._future_datetime(hour=9)),
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

    # ==================================================================
    # PART 2 — Schedule / confirm one self-ship FC appointment
    # ==================================================================

    @staticmethod
    def _schedule_success_response(appointment_id=1000,
                                   start=False,
                                   end=False,
                                   status='ARRIVAL_SCHEDULED'):
        if not start or not end:
            base = (
                fields.Datetime.now() + timedelta(days=30)
            ).replace(hour=9, minute=0, second=0, microsecond=0)
            start = start or TestFbaAppointmentSlots._amazon_timestamp(base)
            end = end or TestFbaAppointmentSlots._amazon_timestamp(
                base + timedelta(minutes=30)
            )
        return {
            'selfShipAppointmentDetails': {
                'appointmentId': appointment_id,
                'appointmentSlotTime': {'startTime': start, 'endTime': end},
                'appointmentStatus': status,
            },
            '_amazon_request_id': 'sched-req-1',
        }

    def _prepare_selected_future_slot(self, expires_future=True):
        """Store fresh future slots, select SLOT_ID_1, return the selected slot."""
        slots = self._store_slots_for_ui()
        if expires_future:
            self.physical.sudo().write({
                'appointment_slots_expires_at': fields.Datetime.now() + timedelta(hours=6),
            })
        slot = slots.filtered(lambda s: s.amazon_appointment_slot_id == SLOT_ID_1)
        slot.action_select_appointment_slot()
        return slot

    # --- 1. Schedule blocked with no selected slot ---
    def test_34_schedule_blocked_without_selected_slot(self):
        self._store_slots_for_ui()  # generation success but nothing selected
        with self.assertRaises(UserError, msg="select a slot"):
            self.physical.action_schedule_appointment()

    # --- 2. Schedule blocked when generation != success ---
    def test_35_schedule_blocked_when_generation_not_success(self):
        slot = self._prepare_selected_future_slot()
        self.physical.sudo().write({'appointment_generation_status': 'failed'})
        with self.assertRaises(UserError, msg="generated successfully"):
            self.physical.action_schedule_appointment()

    # --- 3. Schedule blocked when slots expired ---
    def test_36_schedule_blocked_when_expired(self):
        self._prepare_selected_future_slot()
        self.physical.sudo().write({
            'appointment_slots_expires_at': fields.Datetime.now() - timedelta(hours=1),
        })
        self.physical.invalidate_recordset()
        with self.assertRaises(UserError, msg="expired"):
            self.physical.action_schedule_appointment()

    # --- 4. Schedule blocked when selected slot start is in the past ---
    def test_37_schedule_blocked_when_start_in_past(self):
        slot = self._prepare_selected_future_slot()
        slot.sudo().write({
            'start_date': fields.Datetime.now() - timedelta(hours=2),
            'end_date': fields.Datetime.now() - timedelta(hours=1),
        })
        with self.assertRaises(UserError, msg="already passed"):
            self.physical.action_schedule_appointment()

    # --- 5. Schedule blocked when selected slot belongs to another shipment ---
    def test_38_schedule_blocked_foreign_slot(self):
        self._prepare_selected_future_slot()
        other_shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'AP-PLAN-002',
            'shipment_name': 'AP-PLAN-002',
            'instance_id': self.instance.id,
            'inbound_plan_id': 'wf0000abcd-0000-abcd-0000-0000abcdffff',
            'create_operation_status': 'success',
            'state': 'placement_confirmed',
        })
        other_placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': other_shipment.id,
            'amazon_placement_option_id': 'pl0000abcd-0000-abcd-0000-0000abcdffff',
            'status': 'ACCEPTED',
            'amazon_shipment_ids': '["sh-other-0000"]',
            'selected': True,
        })
        other_physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': other_shipment.id,
            'placement_option_id': other_placement.id,
            'amazon_shipment_id': 'sh-other-0000',
            'shipment_confirmation_id': 'FBA-OTHER',
            'status': 'WORKING',
            'destination_fc': 'ONT8',
        })
        foreign = self.env['amazon.fba.appointment.slot'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': other_shipment.id,
            'physical_shipment_id': other_physical.id,
            'amazon_appointment_slot_id': 'slot-foreign-00000000000000000000001',
            'start_date': self._odoo_timestamp(self._future_datetime(days=31, hour=9)),
            'end_date': self._odoo_timestamp(
                self._future_datetime(days=31, hour=9) + timedelta(minutes=30)
            ),
        })
        # Force the pointer to a foreign slot (never trust only the pointer).
        self.physical.sudo().write({'selected_appointment_slot_id': foreign.id})
        with self.assertRaises(UserError, msg="does not belong"):
            self.physical.action_schedule_appointment()

    # --- 6. Schedule blocked when amazon_slot_id missing ---
    def test_39_schedule_blocked_missing_amazon_slot_id(self):
        slot = self._prepare_selected_future_slot()
        # amazon_appointment_slot_id is a required column; blank it with raw SQL
        # (empty string satisfies NOT NULL) to exercise the defensive guard.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE amazon_fba_appointment_slot SET amazon_appointment_slot_id = '' "
            "WHERE id = %s", [slot.id],
        )
        slot.invalidate_recordset()
        with self.assertRaises(UserError, msg="no Amazon slot ID"):
            self.physical.action_schedule_appointment()

    # --- 7. Opening confirmation wizard makes NO Amazon call ---
    def test_40_opening_wizard_makes_no_amazon_call(self):
        self._prepare_selected_future_slot()
        with patch.object(AmazonAPI, 'schedule_self_ship_appointment',
                          autospec=True) as mock_sched, \
             patch.object(AmazonAPI, '_amazon_request', autospec=True) as mock_req:
            action = self.physical.action_schedule_appointment()
            mock_sched.assert_not_called()
            mock_req.assert_not_called()
        self.assertEqual(action['res_model'],
                         'amazon.fba.schedule.appointment.wizard')
        self.assertEqual(action['context']['default_physical_shipment_id'],
                         self.physical.id)

    def _open_and_confirm(self, response=None, side_effect=None):
        """Open the wizard and press Confirm Schedule with a mocked API."""
        action = self.physical.action_schedule_appointment()
        wizard = self.env['amazon.fba.schedule.appointment.wizard'].with_context(
            action['context']
        ).create({})
        kwargs = {'autospec': True}
        if side_effect is not None:
            kwargs['side_effect'] = side_effect
        else:
            kwargs['return_value'] = response or self._schedule_success_response()
        with patch.object(AmazonAPI, 'schedule_self_ship_appointment', **kwargs) as mock_sched:
            result = wizard.action_confirm_schedule()
        return wizard, mock_sched, result

    # --- 8 & 9 & 10. Confirm calls endpoint once with correct ids + empty body ---
    def test_41_confirm_calls_endpoint_once_with_correct_args(self):
        slot = self._prepare_selected_future_slot()
        _wizard, mock_sched, _res = self._open_and_confirm()
        self.assertEqual(mock_sched.call_count, 1)
        args = mock_sched.call_args
        # (api_self, instance, access_token, plan_id, shipment_id, slot_id)
        self.assertEqual(args[0][3], PLAN_ID)
        self.assertEqual(args[0][4], SHIPMENT_ID)
        self.assertEqual(args[0][5], SLOT_ID_1)
        # Initial schedule must send an empty body (no reasonComment).
        self.assertEqual(args[1].get('body'), {})

    # --- 11 & 12 & 13. Success stores appointment id/start/end/status + status ---
    def test_42_success_stores_confirmed_appointment(self):
        self._prepare_selected_future_slot()
        self._open_and_confirm(response=self._schedule_success_response(
            appointment_id=4242, status='ARRIVAL_SCHEDULED',
        ))
        self.physical.invalidate_recordset()
        self.assertEqual(self.physical.appointment_confirmation_status, 'success')
        self.assertEqual(self.physical.confirmed_appointment_id, '4242')
        self.assertEqual(self.physical.confirmed_appointment_amazon_status,
                         'ARRIVAL_SCHEDULED')
        self.assertEqual(self.physical.confirmed_appointment_slot_id, SLOT_ID_1)
        self.assertTrue(self.physical.confirmed_appointment_start)
        self.assertTrue(self.physical.confirmed_appointment_end)
        self.assertEqual(self.physical.confirmed_appointment_duration_minutes, 30)
        self.assertTrue(self.physical.appointment_confirmed_at)

    # --- 14 & 15 & 16 & 17. Failure marks failed, keeps everything intact ---
    def test_43_failure_marks_failed_and_preserves_state(self):
        slot = self._prepare_selected_future_slot()
        slot_count = len(self.physical.appointment_slot_ids)
        _wizard, _mock, result = self._open_and_confirm(side_effect=_http_error(400))
        self.physical.invalidate_recordset()
        self.assertEqual(self.physical.appointment_confirmation_status, 'failed')
        # slots intact
        self.assertEqual(len(self.physical.appointment_slot_ids), slot_count)
        # selected slot intact
        self.assertEqual(self.physical.selected_appointment_slot_id, slot)
        # generation still success
        self.assertEqual(self.physical.appointment_generation_status, 'success')
        # no confirmed appointment recorded
        self.assertFalse(self.physical.confirmed_appointment_id)
        # returned a (danger) notification rather than raising
        self.assertEqual(result.get('tag'), 'display_notification')

    # --- 18. Double click / in-progress blocks a new schedule ---
    def test_44_in_progress_blocks_schedule(self):
        self._prepare_selected_future_slot()
        self.physical.sudo().write({'appointment_confirmation_status': 'in_progress'})
        with self.assertRaises(UserError, msg="already in progress"):
            self.physical.action_schedule_appointment()

    # --- 19. Same confirmed slot cannot be scheduled twice ---
    def test_45_same_confirmed_slot_cannot_reschedule(self):
        self._prepare_selected_future_slot()
        self._open_and_confirm()
        self.physical.invalidate_recordset()
        self.assertEqual(self.physical.appointment_confirmation_status, 'success')
        # Selecting/scheduling the same slot again is blocked.
        with self.assertRaises(UserError, msg="already confirmed for the selected slot"):
            self.physical.action_schedule_appointment()

    # --- 20. Existing confirmed appointment blocks implicit reschedule ---
    def test_46_confirmed_blocks_implicit_reschedule(self):
        self._prepare_selected_future_slot()
        self._open_and_confirm()
        self.physical.invalidate_recordset()
        # Point selection at a different slot; must not silently reschedule.
        other = self.physical.appointment_slot_ids.filtered(
            lambda s: s.amazon_appointment_slot_id == SLOT_ID_2
        )
        self.physical.sudo().write({
            'selected_appointment_slot_id': other.id,
            'confirmed_appointment_slot_id': SLOT_ID_1,
        })
        with self.assertRaises(UserError, msg="Rescheduling must be performed explicitly"):
            self.physical.action_schedule_appointment()

    # --- 21. Local Select makes ZERO Amazon calls ---
    def test_47_local_select_makes_no_amazon_call(self):
        slots = self._store_slots_for_ui()
        with patch.object(AmazonAPI, '_amazon_request', autospec=True) as mock_req:
            slots[0].action_select_appointment_slot()
            mock_req.assert_not_called()

    # --- 22. Expired slots remain visible but cannot be scheduled ---
    def test_48_expired_slots_visible_but_not_schedulable(self):
        self._prepare_selected_future_slot()
        count = len(self.physical.appointment_slot_ids)
        self.physical.sudo().write({
            'appointment_slots_expires_at': fields.Datetime.now() - timedelta(minutes=1),
        })
        self.physical.invalidate_recordset()
        # rows still present
        self.assertEqual(len(self.physical.appointment_slot_ids), count)
        self.assertTrue(self.physical.appointment_slots_expired)
        with self.assertRaises(UserError, msg="expired"):
            self.physical.action_schedule_appointment()

    # --- 23. Refresh cannot leave a stale selected slot schedulable ---
    def test_49_refresh_clears_stale_selection_for_scheduling(self):
        slot = self._prepare_selected_future_slot()
        # A refresh that no longer returns SLOT_ID_1 must drop the selection.
        only_slot2 = self._slots_response(slots=[{
            'slotId': SLOT_ID_2,
            'slotTime': self._future_slot_time(10, duration_minutes=45),
        }], expires_at=self._future_expiry())
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=only_slot2):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertFalse(self.physical.selected_appointment_slot_id)
        # Scheduling is now blocked because nothing is selected.
        with self.assertRaises(UserError, msg="select a slot"):
            self.physical.action_schedule_appointment()

    # --- 24. No credentials/tokens stored in error fields ---
    def test_50_no_secrets_in_error_fields(self):
        self._prepare_selected_future_slot()
        self._open_and_confirm(side_effect=_http_error(403))
        self.physical.invalidate_recordset()
        blob = ' '.join(filter(None, [
            self.physical.appointment_schedule_error_code or '',
            self.physical.appointment_schedule_error_message or '',
            self.physical.appointment_confirmation_response or '',
        ])).lower()
        for secret_marker in ('x-amz-access-token', 'access_token', 'refresh_token',
                              'client_secret', 'authorization', 'aws_secret'):
            self.assertNotIn(secret_marker, blob)
        # Diagnostics still captured something useful.
        self.assertTrue(self.physical.appointment_schedule_error_code)

    # --- Wizard confirm returns a close action on success ---
    def test_51_wizard_success_returns_close_next(self):
        self._prepare_selected_future_slot()
        _wizard, _mock, result = self._open_and_confirm()
        self.assertEqual(result.get('tag'), 'display_notification')
        self.assertEqual(result['params']['next']['type'], 'ir.actions.act_window_close')

    # ==================================================================
    # Refresh / expiry persistence (getSelfShipAppointmentSlots is GET-only:
    # it retrieves the last generated snapshot and never regenerates).
    # ==================================================================

    # --- 1. GET returns future expiresAt -> stored correctly ---
    def test_52_refresh_stores_future_expiry(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        future_expiry_dt = self._future_datetime(days=85, hour=23, minute=59, second=59)
        future_expiry = self._amazon_timestamp(future_expiry_dt)
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response(
                              expires_at=future_expiry)):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(
            fields.Datetime.to_string(self.physical.appointment_slots_expires_at),
            self._odoo_timestamp(future_expiry_dt),
        )
        self.assertFalse(self.physical.appointment_slots_expired)

    # --- 2. GET returns old/expired expiresAt -> stored exactly as returned ---
    #     (This is the real amazon24sep case: Amazon keeps returning the old
    #     snapshot with a past expiresAt; Odoo must not mask it.)
    def test_53_refresh_stores_expired_expiry_verbatim(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        past = fields.Datetime.now() - timedelta(hours=6)
        past_iso = past.strftime('%Y-%m-%dT%H:%M:%SZ')
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response(expires_at=past_iso)):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(
            fields.Datetime.to_string(self.physical.appointment_slots_expires_at),
            past.strftime('%Y-%m-%d %H:%M:%S'),
        )
        # Flag correctly reflects Amazon's real (expired) state.
        self.assertTrue(self.physical.appointment_slots_expired)

    # --- 3. GET missing expiresAt -> safe deterministic behavior ---
    def test_54_refresh_missing_expiry_clears_field(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        resp = self._slots_response()
        # Remove expiresAt entirely from the availability envelope.
        resp['selfShipAppointmentSlotsAvailability'].pop('expiresAt', None)
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=resp):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        # Deterministic: no expiry stored, so not flagged expired.
        self.assertFalse(self.physical.appointment_slots_expires_at)
        self.assertFalse(self.physical.appointment_slots_expired)
        # Slots themselves are still stored.
        self.assertEqual(len(self.physical.appointment_slot_ids), 2)

    # --- 4. Pagination does not overwrite expiry incorrectly ---
    def test_55_pagination_keeps_first_page_expiry(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        page1_expiry_dt = self._future_datetime(days=85, hour=23, minute=59, second=59)
        page1 = self._slots_response(
            slots=[{'slotId': SLOT_ID_1,
                    'slotTime': self._future_slot_time(9, duration_minutes=30)}],
            expires_at=self._amazon_timestamp(page1_expiry_dt), pagination_token='p2',
        )
        # Second page carries a DIFFERENT expiresAt that must be ignored.
        page2 = self._slots_response(
            slots=[{'slotId': SLOT_ID_2,
                    'slotTime': self._future_slot_time(
                        9, duration_minutes=30, days=31,
                    )}],
            expires_at=self._amazon_timestamp(
                self._future_datetime(days=80, hour=0, minute=0, second=0)
            ),
        )

        def mock_get(*args, **kwargs):
            # autospec call: (api_self, instance, access_token, plan_id,
            # shipment_id, page_size, pagination_token) -> token is args[6].
            token = kwargs.get('pagination_token') or (args[6] if len(args) > 6 else None)
            return page2 if token == 'p2' else page1

        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          side_effect=mock_get):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(len(self.physical.appointment_slot_ids), 2)
        # First page's expiry wins; the later page never overwrites it.
        self.assertEqual(
            fields.Datetime.to_string(self.physical.appointment_slots_expires_at),
            self._odoo_timestamp(page1_expiry_dt),
        )

    # --- 5. Existing slots are not deleted on a failed refresh ---
    def test_56_failed_refresh_keeps_existing_slots(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response()):
            self.physical._refresh_appointment_slots()
        count = len(self.physical.appointment_slot_ids)
        self.assertEqual(count, 2)
        # A subsequent refresh that errors must not touch the stored slots.
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          side_effect=_http_error(500)):
            with self.assertRaises(UserError):
                self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(len(self.physical.appointment_slot_ids), count)

    # --- 6. Selected slot handling remains correct across refresh ---
    def test_57_refresh_keeps_still_offered_selection(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response()):
            self.physical._refresh_appointment_slots()
        slot = self.physical.appointment_slot_ids.filtered(
            lambda s: s.amazon_appointment_slot_id == SLOT_ID_1)
        slot.action_select_appointment_slot()
        # A refresh that still returns SLOT_ID_1 keeps the selection.
        with patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response()):
            self.physical._refresh_appointment_slots()
        self.physical.invalidate_recordset()
        self.assertEqual(self.physical.selected_appointment_slot_id.amazon_appointment_slot_id,
                         SLOT_ID_1)

    # --- 7, 8, 9. Refresh calls only GET — never generate/schedule/cancel ---
    def test_58_refresh_calls_only_get(self):
        self._set_confirmed()
        self.physical.sudo().write({'appointment_generation_status': 'success'})
        # AmazonAPI must not even define a cancel operation.
        self.assertFalse(hasattr(AmazonAPI, 'cancel_self_ship_appointment'))
        with patch.object(AmazonAPI, 'generate_self_ship_appointment_slots',
                          autospec=True) as mock_gen, \
             patch.object(AmazonAPI, 'schedule_self_ship_appointment',
                          autospec=True) as mock_sched, \
             patch.object(AmazonAPI, 'get_self_ship_appointment_slots', autospec=True,
                          return_value=self._slots_response()) as mock_get:
            self.physical._refresh_appointment_slots()
            self.assertEqual(mock_get.call_count, 1)
            mock_gen.assert_not_called()
            mock_sched.assert_not_called()
