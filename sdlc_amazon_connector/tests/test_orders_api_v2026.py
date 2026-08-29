from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install')
class TestOrdersApiV2026(TransactionCase):

    def setUp(self):
        super().setUp()
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Orders 2026 Test Instance',
            'company_id': self.env.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
        })

    @staticmethod
    def _order_response(next_token='NEXT-2026'):
        return {
            'orders': [{
                'orderId': 'TEST-ORDER-2026-1',
                'createdTime': '2026-08-01T10:00:00Z',
                'lastUpdatedTime': '2026-08-01T11:00:00Z',
                'programs': ['PRIME', 'AMAZON_BUSINESS'],
                'salesChannel': {
                    'channelName': 'AMAZON',
                    'marketplaceId': 'ARBP9OOSHTCHU',
                    'marketplaceName': 'Amazon.eg',
                },
                'recipient': {'deliveryAddress': {
                    'name': 'Orders Test Customer',
                    'addressLine1': 'Test Street',
                    'city': 'Cairo',
                    'stateOrRegion': 'Cairo',
                    'postalCode': '11511',
                    'countryCode': 'EG',
                }},
                'proceeds': {'grandTotal': {'amount': '116.00', 'currencyCode': 'EGP'}},
                'fulfillment': {
                    'fulfillmentStatus': 'UNSHIPPED',
                    'fulfilledBy': 'AMAZON',
                    'fulfillmentServiceLevel': 'STANDARD',
                },
                'orderItems': [{
                    'orderItemId': 'TEST-ITEM-2026-1',
                    'quantityOrdered': 2,
                    'product': {
                        'sellerSku': 'TEST-AMAZON-E2E',
                        'asin': 'B000TESTE2E',
                        'title': 'Orders API v2026 Test Product',
                    },
                    'proceeds': {'breakdowns': [
                        {'type': 'ITEM', 'subtotal': {'amount': '100.00', 'currencyCode': 'EGP'}},
                        {'type': 'SHIPPING', 'subtotal': {'amount': '10.00', 'currencyCode': 'EGP'}},
                        {
                            'type': 'TAX',
                            'subtotal': {'amount': '10.00', 'currencyCode': 'EGP'},
                            'detailedBreakdowns': [{
                                'subtype': 'ITEM',
                                'value': {'amount': '10.00', 'currencyCode': 'EGP'},
                            }],
                        },
                        {'type': 'DISCOUNT', 'subtotal': {'amount': '4.00', 'currencyCode': 'EGP'}},
                    ]},
                }],
            }],
            'pagination': {'nextToken': next_token} if next_token else {},
        }

    def test_search_orders_uses_current_endpoint_and_normalizes_response(self):
        response = MagicMock()
        response.json.return_value = self._order_response()
        response.headers = {'x-amzn-RequestId': 'orders-2026-request'}
        api = AmazonAPI()

        with patch.object(api, '_amazon_request', return_value=response) as request:
            result = api.get_orders(
                self.instance,
                'test-token',
                created_after='2026-08-01T00:00:00Z',
                created_before='2026-08-02T00:00:00Z',
                order_statuses=('Unshipped', 'Canceled'),
                fulfillment_channels='AFN',
                next_token='PAGE-2',
                max_results_per_page=25,
            )

        url = request.call_args.args[3]
        params = request.call_args.kwargs['params']
        self.assertIn('/orders/2026-01-01/orders', url)
        self.assertNotIn('/orders/v0/', url)
        self.assertEqual(params['marketplaceIds'], 'ARBP9OOSHTCHU')
        self.assertEqual(params['createdAfter'], '2026-08-01T00:00:00Z')
        self.assertEqual(params['createdBefore'], '2026-08-02T00:00:00Z')
        self.assertEqual(params['paginationToken'], 'PAGE-2')
        self.assertEqual(params['fulfillmentStatuses'], 'UNSHIPPED,CANCELLED')
        self.assertEqual(params['fulfilledBy'], 'AMAZON')
        self.assertEqual(params['includedData'], 'RECIPIENT,FULFILLMENT,PROCEEDS')

        payload = result['payload']
        self.assertEqual(payload['NextToken'], 'NEXT-2026')
        order = payload['Orders'][0]
        self.assertEqual(order['AmazonOrderId'], 'TEST-ORDER-2026-1')
        self.assertEqual(order['OrderStatus'], 'Unshipped')
        self.assertEqual(order['FulfillmentChannel'], 'AFN')
        self.assertEqual(order['OrderTotal'], {'Amount': '116.00', 'CurrencyCode': 'EGP'})
        self.assertEqual(order['ShippingAddress']['CountryCode'], 'EG')
        item = order['OrderItems'][0]
        self.assertEqual(item['SellerSKU'], 'TEST-AMAZON-E2E')
        self.assertEqual(item['ItemPrice']['Amount'], '100.00')
        self.assertEqual(item['ItemTax']['Amount'], '10.00')
        self.assertEqual(item['PromotionDiscount']['Amount'], '4.00')
        self.assertEqual(result['_amazon_request_id'], 'orders-2026-request')

    def test_embedded_items_are_idempotent_without_legacy_item_request(self):
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 8, 1),
            'date_to': datetime(2026, 8, 2),
        })
        order_data = AmazonAPI._normalize_order_2026(self._order_response()['orders'][0])
        api = MagicMock(spec=AmazonAPI)

        first = job._import_one_order(api, 'test-token', order_data)
        second = job._import_one_order(api, 'test-token', order_data)

        api.get_order_items.assert_not_called()
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        orders = self.env['amazon.sale.order'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('amazon_order_ref', '=', 'TEST-ORDER-2026-1'),
        ])
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders.order_line_ids), 1)
        self.assertEqual(orders.order_line_ids.amazon_order_item_id, 'TEST-ITEM-2026-1')

    def test_continuation_job_reuses_original_filter_window(self):
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'date_from': datetime(2026, 8, 1, 0, 0, 0),
            'date_to': datetime(2026, 8, 2, 0, 0, 0),
            'effective_date_to': datetime(2026, 8, 2, 0, 0, 0),
            'next_token': 'PAGE-2',
        })
        with (
            patch.object(type(self.instance), '_check_required_fields', return_value=True),
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='test-token'),
            patch.object(
                AmazonAPI, 'get_orders', autospec=True,
                return_value={'payload': {'Orders': [], 'NextToken': False}},
            ) as get_orders,
        ):
            job._process_next_batch()

        kwargs = get_orders.call_args.kwargs
        self.assertEqual(kwargs['created_after'], '2026-08-01T00:00:00Z')
        self.assertEqual(kwargs['created_before'], '2026-08-02T00:00:00Z')
        self.assertEqual(kwargs['next_token'], 'PAGE-2')

