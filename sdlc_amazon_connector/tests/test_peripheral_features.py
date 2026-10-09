from unittest.mock import ANY, patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


EG = 'ARBP9OOSHTCHU'


@tagged('post_install', '-at_install', 'amazon_peripheral_features')
class TestAmazonPeripheralFeatures(TransactionCase):
    """Peripheral AI, VCS, rating, and MCF behavior with all providers mocked."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company.id)], limit=1)
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Peripheral Egypt',
            'company_id': self.company.id,
            'marketplace_id': EG,
            'region': 'eu',
            'seller_id': 'PERIPHERAL-SELLER',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
            'fba_warehouse_id': warehouse.id,
            'fbm_warehouse_id': warehouse.id,
        })
        self.product = self.env['product.product'].sudo().create({
            'name': 'Peripheral Towel',
            'default_code': 'PER-SKU',
            'type': 'consu',
            'is_storable': True,
            'list_price': 120.0,
            'standard_price': 70.0,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Peripheral Towel',
            'instance_id': self.instance.id,
            'sku': 'PER-SKU',
            'asin': 'B0PER',
            'status': 'Active',
            'amazon_price': 120.0,
            'amazon_qty': 12,
            'brand': 'Test Brand',
            'product_type': 'HOME_BED_AND_BATH',
            'description': 'Soft cotton towel.',
            'odoo_product_id': self.product.id,
        })
        self.partner = self.env['res.partner'].sudo().create({
            'name': 'Peripheral Customer',
            'street': '1 Test Street',
            'city': 'Cairo',
            'zip': '11711',
            'country_id': self.env.ref('base.eg').id,
        })

    def _amazon_order(self, ref='P-ORDER', instance=None):
        return self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': ref,
            'instance_id': (instance or self.instance).id,
            'fulfillment_channel': 'AFN',
            'amazon_status': 'Shipped',
        })

    def _sale_order(self):
        return self.env['sale.order'].sudo().create({
            'partner_id': self.partner.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'name': self.product.display_name,
                'product_uom_qty': 2,
                'price_unit': 120.0,
            })],
        })

    def _outbound(self, **vals):
        base = {
            'instance_id': self.instance.id,
            'dest_name': 'Peripheral Customer',
            'dest_address_line1': '1 Test Street',
            'dest_city': 'Cairo',
            'dest_country_code': 'EG',
            'line_ids': [Command.create({'sku': 'PER-SKU', 'quantity': 2})],
        }
        base.update(vals)
        return self.env['amazon.outbound.order'].sudo().create(base)

    # AI
    def test_01_ai_listing_missing_key_fails_without_external_call_or_mutation(self):
        listing = self.env['amazon.ai.listing'].sudo().create({'product_id': self.amazon_product.id})
        before = (listing.optimised_title, self.amazon_product.name, self.amazon_product.search_terms)
        with patch('odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService._call_and_parse') as call:
            with self.assertRaisesRegex(UserError, 'AI API Key'):
                listing.action_optimise_with_ai()
        call.assert_not_called()
        self.assertEqual((listing.optimised_title, self.amazon_product.name, self.amazon_product.search_terms), before)

    def test_02_ai_listing_provider_failure_does_not_mutate_listing(self):
        self.instance.ai_api_key = 'mock-key'
        listing = self.env['amazon.ai.listing'].sudo().create({'product_id': self.amazon_product.id})
        with patch(
            'odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService._call_and_parse',
            side_effect=Exception('provider down'),
        ):
            with self.assertRaisesRegex(UserError, 'provider down'):
                listing.action_optimise_with_ai()
        self.assertFalse(listing.optimised_title)
        self.assertFalse(listing.applied)

    def test_03_ai_listing_valid_response_populates_review_record_only(self):
        self.instance.ai_api_key = 'mock-key'
        listing = self.env['amazon.ai.listing'].sudo().create({'product_id': self.amazon_product.id})
        result = {
            'optimised_title': 'Optimised Peripheral Towel',
            'optimised_description': 'Better copy.',
            'bullet_points': ['FAST DRYING', 'SOFT COTTON'],
            'backend_keywords': 'towel,cotton',
            'search_terms': 'bath towel',
            'a_plus_content_idea': 'Comparison layout',
            'seo_score': 88,
        }
        with patch(
            'odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService._call_and_parse',
            return_value=result,
        ) as call:
            listing.action_optimise_with_ai()
        call.assert_called_once()
        self.assertEqual(listing.optimised_title, 'Optimised Peripheral Towel')
        self.assertEqual(listing._get_bullet_points_list(), ['FAST DRYING', 'SOFT COTTON'])
        self.assertEqual(self.amazon_product.name, 'Peripheral Towel')
        self.assertFalse(listing.applied)

    def test_04_ai_pricing_missing_key_and_provider_failure_create_no_suggestions(self):
        wizard = self.env['amazon.ai.pricing.wizard'].sudo().create({'instance_id': self.instance.id})
        with patch('odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService.optimize_price') as call:
            with self.assertRaisesRegex(UserError, 'AI API Key'):
                wizard.action_generate_suggestions()
        call.assert_not_called()

        self.instance.ai_api_key = 'mock-key'
        before = self.env['amazon.ai.pricing'].search_count([('product_id', '=', self.amazon_product.id)])
        with patch(
            'odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService.optimize_price',
            side_effect=Exception('bad response'),
        ):
            action = wizard.action_generate_suggestions()
        self.assertEqual(action['domain'], [('id', 'in', [])])
        self.assertEqual(
            self.env['amazon.ai.pricing'].search_count([('product_id', '=', self.amazon_product.id)]),
            before,
        )

    def test_05_ai_forecast_and_review_missing_key_fail_without_mutation(self):
        forecast = self.env['amazon.demand.forecast'].sudo().create({'product_id': self.amazon_product.id})
        review = self.env['amazon.review.analysis'].sudo().create({'product_id': self.amazon_product.id})
        with self.assertRaisesRegex(UserError, 'AI API Key'):
            forecast.action_generate_forecast()
        with self.assertRaisesRegex(UserError, 'AI API Key'):
            review.action_analyse_reviews()
        self.assertFalse(forecast.forecast_30d)
        self.assertFalse(review.total_reviews)

    def test_06_ai_chat_provider_failure_is_captured_in_history(self):
        self.instance.ai_api_key = 'mock-key'
        chat = self.env['amazon.ai.chat'].sudo().create({'instance_id': self.instance.id})
        with patch(
            'odoo.addons.sdlc_amazon_connector.services.ai_service.AmazonAIService._call_provider',
            side_effect=Exception('provider timeout'),
        ):
            response = chat.action_send_message('Summarise sales')
        self.assertIn('provider timeout', response)
        history = chat._get_history()
        self.assertEqual(history[-2]['role'], 'user')
        self.assertEqual(history[-1]['role'], 'assistant')

    # VCS
    def test_07_vcs_requires_download_before_processing(self):
        report = self.env['amazon.vcs.tax.report'].sudo().create({'instance_id': self.instance.id})
        with self.assertRaisesRegex(UserError, 'Download'):
            report.action_process_report()

    def test_08_vcs_download_replaces_lines_and_calculates_tax_amounts(self):
        report = self.env['amazon.vcs.tax.report'].sudo().create({
            'instance_id': self.instance.id,
            'line_ids': [Command.create({'amazon_order_id': 'OLD', 'vat_amount': 99.0})],
        })
        rows = [{
            'order-id': 'VCS-1',
            'invoice-number': 'INV-1',
            'vat-number': 'VAT-1',
            'tax-amount': '14.25',
            'invoice-amount': '114.25',
            'currency': 'EGP',
        }]
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'fetch_vcs_tax_report', return_value=rows) as fetch,
        ):
            report.action_download_report()
        fetch.assert_called_once_with(self.instance, 'token')
        self.assertEqual(report.state, 'downloaded')
        self.assertEqual(len(report.line_ids), 1)
        self.assertEqual(report.line_ids.vat_amount, 14.25)

    def test_09_vcs_process_is_company_instance_scoped_and_not_repeatable(self):
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'Other Peripheral',
            'company_id': self.company.id,
            'marketplace_id': EG,
            'region': 'eu',
        })
        self._amazon_order('VCS-2', instance=other_instance)
        order = self._amazon_order('VCS-2')
        report = self.env['amazon.vcs.tax.report'].sudo().create({
            'instance_id': self.instance.id,
            'line_ids': [Command.create({
                'amazon_order_id': 'VCS-2',
                'amazon_invoice_number': 'A-INV-2',
                'vat_amount': 12.28,
                'invoice_amount': 100.0,
                'currency_code': 'EGP',
            })],
        })
        report.write({'state': 'downloaded'})
        report.action_process_report()
        self.assertEqual(report.state, 'processed')
        self.assertEqual(order.amazon_invoice_number, 'A-INV-2')
        self.assertFalse(self.env['amazon.sale.order'].search([
            ('instance_id', '=', other_instance.id),
            ('amazon_order_ref', '=', 'VCS-2'),
        ]).amazon_invoice_number)
        with self.assertRaisesRegex(UserError, 'Download'):
            report.action_process_report()

    def test_10_vcs_empty_report_processes_safely(self):
        report = self.env['amazon.vcs.tax.report'].sudo().create({
            'instance_id': self.instance.id,
        })
        report.write({'state': 'downloaded'})
        report.action_process_report()
        self.assertEqual(report.state, 'processed')
        self.assertEqual(report.line_count, 0)

    # Rating
    def test_11_rating_requires_download_before_processing(self):
        report = self.env['amazon.rating.report'].sudo().create({'instance_id': self.instance.id})
        with self.assertRaisesRegex(UserError, 'Download'):
            report.action_process_report()

    def test_12_rating_download_generates_lines_and_summary(self):
        report = self.env['amazon.rating.report'].sudo().create({'instance_id': self.instance.id})
        rows = [
            {'order-id': 'R-1', 'rating': '5', 'comments': 'Great', 'date': '2026-10-01 10:00:00'},
            {'order-id': 'R-2', 'rating': '3', 'comments': 'Ok', 'date': '2026-10-02 10:00:00'},
        ]
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'fetch_seller_feedback_report', return_value=rows),
        ):
            report.action_download_report()
        self.assertEqual(report.state, 'downloaded')
        self.assertEqual(report.line_count, 2)
        self.assertEqual(report.total_ratings, 2)
        self.assertEqual(report.average_rating, 4.0)

    def test_13_rating_process_links_same_instance_only_and_is_not_repeatable(self):
        order = self._amazon_order('R-3')
        report = self.env['amazon.rating.report'].sudo().create({
            'instance_id': self.instance.id,
            'line_ids': [Command.create({'amazon_order_id': 'R-3', 'rating': 4})],
        })
        report.write({'state': 'downloaded'})
        report.action_process_report()
        self.assertEqual(report.state, 'processed')
        self.assertEqual(report.line_ids.order_id, order)
        with self.assertRaisesRegex(UserError, 'Download'):
            report.action_process_report()

    # Outbound / MCF
    def test_14_outbound_create_from_sale_order_maps_destination_and_sku_idempotently(self):
        outbound = self.env['amazon.outbound.order'].sudo().create({
            'instance_id': self.instance.id,
            'sale_order_id': self._sale_order().id,
        })
        outbound.action_create_from_sale_order()
        outbound.action_create_from_sale_order()
        self.assertEqual(outbound.dest_name, self.partner.name)
        self.assertEqual(outbound.dest_country_code, 'EG')
        self.assertEqual(len(outbound.line_ids), 1)
        self.assertEqual(outbound.line_ids.sku, 'PER-SKU')
        self.assertEqual(outbound.line_ids.quantity, 2)

    def test_15_outbound_submit_validates_draft_lines_and_destination(self):
        outbound = self._outbound(line_ids=[Command.clear()])
        with self.assertRaisesRegex(UserError, 'Add items'):
            outbound.action_submit_to_amazon()
        outbound = self._outbound(dest_name=False)
        with self.assertRaisesRegex(UserError, 'Destination address'):
            outbound.action_submit_to_amazon()
        outbound = self._outbound(state='submitted')
        with self.assertRaisesRegex(UserError, 'Only draft'):
            outbound.action_submit_to_amazon()

    def test_16_outbound_submit_uses_mocked_api_and_persists_local_state(self):
        outbound = self._outbound(displayable_order_id='SO-MCF-1')
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'create_fulfillment_order', return_value={'payload': {}}) as create,
        ):
            outbound.action_submit_to_amazon()
        create.assert_called_once_with(self.instance, 'token', ANY)
        body = create.call_args.args[2]
        self.assertEqual(body['sellerFulfillmentOrderId'], outbound.name)
        self.assertEqual(body['items'][0]['sellerSku'], 'PER-SKU')
        self.assertEqual(body['items'][0]['quantity'], 2)
        self.assertEqual(outbound.fulfillment_order_id, outbound.name)
        self.assertEqual(outbound.state, 'submitted')

    def test_17_outbound_api_failure_leaves_order_draft(self):
        outbound = self._outbound()
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'create_fulfillment_order', side_effect=Exception('mcf down')),
        ):
            with self.assertRaisesRegex(Exception, 'mcf down'):
                outbound.action_submit_to_amazon()
        self.assertEqual(outbound.state, 'draft')
        self.assertFalse(outbound.fulfillment_order_id)

    def test_18_outbound_status_and_cancel_are_mocked_and_idempotent(self):
        outbound = self._outbound(fulfillment_order_id='MCF-1', state='submitted')
        status_payload = {
            'payload': {'fulfillmentOrder': {
                'fulfillmentOrderStatus': 'PROCESSING',
                'fulfillmentShipment': {'trackingNumber': 'TRK-MCF'},
            }}
        }
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'get_fulfillment_order', return_value=status_payload),
        ):
            outbound.action_check_status()
        self.assertEqual(outbound.state, 'processing')
        self.assertEqual(outbound.tracking_number, 'TRK-MCF')

        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'cancel_fulfillment_order', return_value={'payload': {}}),
        ):
            outbound.action_cancel()
        self.assertEqual(outbound.state, 'cancelled')

    def test_19_outbound_check_status_requires_submission(self):
        outbound = self._outbound(line_ids=[Command.create({'sku': 'PER-SKU', 'quantity': 1})])
        with self.assertRaisesRegex(UserError, 'not yet submitted'):
            outbound.action_check_status()
