"""Transport-level tests for the Bosta client. No real network calls."""

from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.sdlc_bosta_connector.models.bosta_api import (
    API_BASE_PATH,
    BostaAPI,
    BostaError,
)

REQUESTS_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.requests.request'
SLEEP_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.time.sleep'


def fake_response(status_code=200, json_body=None, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.headers = headers or {}
    response.json.return_value = json_body if json_body is not None else {}
    return response


@tagged('post_install', '-at_install')
class TestBostaAPI(TransactionCase):

    def setUp(self):
        super().setUp()
        self.api = BostaAPI('https://app.bosta.co', 'test-key')

    def test_create_delivery_uses_v2_path_and_api_version(self):
        body = {'data': {'_id': 'd1', 'trackingNumber': 'T1'}}
        with patch(REQUESTS_PATH, return_value=fake_response(200, body)) as mocked:
            result = self.api.create_delivery({'type': 10})

        self.assertEqual(result['trackingNumber'], 'T1')
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], 'post')
        self.assertEqual(args[1], f'https://app.bosta.co{API_BASE_PATH}/deliveries')
        self.assertEqual(kwargs['params'], {'apiVersion': '1'})

    def test_authorization_header_has_no_bearer_prefix(self):
        body = {'data': {'_id': 'd1', 'trackingNumber': 'T1'}}
        with patch(REQUESTS_PATH, return_value=fake_response(200, body)) as mocked:
            self.api.create_delivery({'type': 10})

        headers = mocked.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'test-key')
        self.assertNotIn('Bearer', headers['Authorization'])

    def test_missing_tracking_number_raises(self):
        with patch(REQUESTS_PATH, return_value=fake_response(200, {'data': {'_id': 'x'}})):
            with self.assertRaises(BostaError):
                self.api.create_delivery({'type': 10})

    def test_client_error_is_not_retryable(self):
        body = {'message': 'Invalid city'}
        with patch(REQUESTS_PATH, return_value=fake_response(400, body)) as mocked:
            with self.assertRaises(BostaError) as ctx:
                self.api.create_delivery({'type': 10})

        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('Invalid city', str(ctx.exception))
        # A malformed request must not be retried.
        self.assertEqual(mocked.call_count, 1)

    def test_rate_limit_is_retried_then_raises_retryable(self):
        response = fake_response(429, {'message': 'slow down'}, {'Retry-After': '0'})
        with patch(SLEEP_PATH), patch(REQUESTS_PATH, return_value=response) as mocked:
            with self.assertRaises(BostaError) as ctx:
                self.api.create_delivery({'type': 10})

        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertGreater(mocked.call_count, 1)

    def test_server_error_recovers_on_retry(self):
        responses = [
            fake_response(503, {'message': 'maintenance'}),
            fake_response(200, {'data': {'_id': 'd9', 'trackingNumber': 'T9'}}),
        ]
        with patch(SLEEP_PATH), patch(REQUESTS_PATH, side_effect=responses):
            result = self.api.create_delivery({'type': 10})
        self.assertEqual(result['trackingNumber'], 'T9')

    def test_connection_error_is_retryable(self):
        import requests

        with patch(SLEEP_PATH), patch(REQUESTS_PATH, side_effect=requests.ConnectionError('down')):
            with self.assertRaises(BostaError) as ctx:
                self.api.create_delivery({'type': 10})
        self.assertTrue(ctx.exception.retryable)

    def test_missing_configuration_raises_before_any_call(self):
        with patch(REQUESTS_PATH) as mocked:
            with self.assertRaises(BostaError):
                BostaAPI('', 'key').get_cities()
            with self.assertRaises(BostaError):
                BostaAPI('https://app.bosta.co', '').get_cities()
        mocked.assert_not_called()

    def test_build_specs_only_sends_documented_keys(self):
        specs = BostaAPI.build_specs(items_count=3, description='Order X', weight=2.345)
        self.assertEqual(set(specs), {'packageDetails'})
        details = specs['packageDetails']
        self.assertEqual(details['itemsCount'], 3)
        self.assertEqual(details['description'], 'Order X')
        self.assertEqual(details['weight'], 2.35)
        # size / packageType are not sent: the v2 schema for them is unconfirmed.
        self.assertNotIn('size', specs)
        self.assertNotIn('packageType', specs)

    def test_build_specs_defaults_item_count_to_at_least_one(self):
        self.assertEqual(
            BostaAPI.build_specs(items_count=0)['packageDetails']['itemsCount'], 1,
        )

    def test_non_json_response_does_not_crash(self):
        response = fake_response(200, None)
        response.json.side_effect = ValueError('not json')
        with patch(REQUESTS_PATH, return_value=response):
            with self.assertRaises(BostaError):
                self.api.create_delivery({'type': 10})

    def test_tracking_endpoint_path(self):
        with patch(REQUESTS_PATH, return_value=fake_response(200, {})) as mocked:
            self.api.get_delivery_tracking('TN123')
        self.assertEqual(
            mocked.call_args[0][1],
            f'https://app.bosta.co{API_BASE_PATH}/deliveries/TN123/tracking',
        )
