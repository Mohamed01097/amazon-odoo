import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install', 'amazon_phase7')
class TestAmazonPhase7(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({'name': 'Amazon Phase 7 Company'})
        self.other_company = self.env['res.company'].sudo().create({'name': 'Amazon Phase 7 Other'})
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.customer_warehouse = Warehouse.create({
            'name': 'P7 Customer Warehouse', 'code': 'P7CW', 'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'P7 Amazon Warehouse', 'code': 'P7AW', 'company_id': self.company.id,
        })
        country = self.env.ref('base.eg')
        self.removal_partner = self.env['res.partner'].sudo().create({
            'name': 'P7 Removal Destination', 'street': '1 Test Street',
            'city': 'Cairo', 'zip': '11511', 'country_id': country.id,
            'phone': '+201000000000', 'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'P7 Instance', 'company_id': self.company.id,
            'seller_id': 'P7-SELLER', 'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu', 'refresh_token': 'mock-refresh',
            'client_id': 'mock-client', 'client_secret': 'mock-secret',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.customer_warehouse.lot_stock_id.id,
            'fba_removal_return_partner_id': self.removal_partner.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'P7 Product', 'default_code': 'P7-SKU', 'type': 'consu',
            'is_storable': True, 'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'P7 Amazon Product', 'instance_id': self.instance.id,
            'sku': 'P7-SKU', 'asin': 'B000P70001', 'odoo_product_id': self.product.id,
        })
        self.report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id, 'state': 'downloaded',
        })

    def _return_row(self, disposition='SELLABLE', suffix='1'):
        return {
            'return-date': '2026-08-01T10:00:00Z', 'order-id': 'P7-ORDER-%s' % suffix,
            'sku': 'P7-SKU', 'asin': 'B000P70001', 'fnsku': 'P7-FNSKU',
            'product-name': 'P7 Product', 'quantity': '1',
            'fulfillment-center-id': 'CAI1', 'detailed-disposition': disposition,
            'reason': 'CUSTOMER_RETURN', 'status': 'Unit returned to inventory',
            'license-plate-number': 'LPN-%s' % suffix,
        }

    def _removal(self, removal_type='return_to_address', suffix='1'):
        return self.env['amazon.removal.order'].sudo().create({
            'instance_id': self.instance.id, 'name': 'P7-REM-%s' % suffix,
            'removal_order_id': 'P7-REM-%s' % suffix, 'removal_type': removal_type,
            'ship_to_partner_id': self.removal_partner.id if removal_type == 'return_to_address' else False,
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'odoo_product_id': self.product.id, 'sku': 'P7-SKU',
                'fnsku': 'P7-FNSKU', 'disposition': 'Sellable',
                'requested_quantity': 3,
            })],
        })

    def _put_stock(self, location, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_warehouse.in_type_id.id,
            'location_id': supplier.id, 'location_dest_id': location.id,
            'company_id': self.company.id, 'origin': 'P7 TEST OPENING STOCK',
            'move_ids': [Command.create({
                'product_id': self.product.id, 'product_uom_qty': quantity,
                'product_uom': self.product.uom_id.id, 'location_id': supplier.id,
                'location_dest_id': location.id, 'company_id': self.company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        self.assertEqual(picking.state, 'done')

    def _submit_feed_mocked(self, order, result_xml=None, processing_status='DONE'):
        submit = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'removal_submit',
            'source_model': order._name, 'source_id': order.id,
        })
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'create_feed_document', return_value={
                'feedDocumentId': 'DOC-1', 'url': 'https://upload.example.test',
            }),
            patch.object(AmazonAPI, 'upload_feed_document', return_value=None),
            patch.object(AmazonAPI, 'create_feed', return_value={'feedId': 'FEED-1'}),
        ):
            submit._run_one_turn()
        poll = self.env['amazon.phase7.job'].sudo().search([
            ('operation_type', '=', 'removal_feed_poll'), ('source_id', '=', order.id),
        ], order='id desc', limit=1)
        response = {'feedId': 'FEED-1', 'processingStatus': processing_status}
        if result_xml is not None:
            response['resultFeedDocumentId'] = 'RESULT-1'
        patches = [
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_feed', return_value=response),
        ]
        if result_xml is not None:
            patches += [
                patch.object(AmazonAPI, 'get_feed_document', return_value={'url': 'https://result.example.test'}),
                patch.object(AmazonAPI, 'download_report_document', return_value=result_xml),
            ]
        with patches[0], patches[1]:
            if result_xml is None:
                poll._run_one_turn()
            else:
                with patches[2], patches[3]:
                    poll._run_one_turn()
        return submit, poll

    def test_01_sellable_customer_return(self):
        event = self.env['amazon.return.report.line'].import_row(self.report, self._return_row())
        event._classify_and_apply()
        self.assertEqual(event.operational_disposition, 'sellable')
        self.assertEqual(event.stock_action_state, 'informational')

    def test_02_unsellable_customer_return(self):
        event = self.env['amazon.return.report.line'].import_row(
            self.report, self._return_row('CUSTOMER_DAMAGED', '2')
        )
        event._classify_and_apply()
        self.assertEqual(event.operational_disposition, 'unsellable')
        self.assertFalse(event.linked_stock_move_id)

    def test_03_unknown_return_disposition(self):
        event = self.env['amazon.return.report.line'].import_row(
            self.report, self._return_row('FUTURE_AMAZON_VALUE', '3')
        )
        event._classify_and_apply()
        self.assertTrue(event.manual_review_required)
        self.assertEqual(event.detailed_disposition, 'FUTURE_AMAZON_VALUE')

    def test_04_duplicate_return_import(self):
        first = self.env['amazon.return.report.line'].import_row(self.report, self._return_row())
        second = self.env['amazon.return.report.line'].import_row(self.report, self._return_row())
        self.assertEqual(first, second)
        self.assertEqual(self.env['amazon.return.report.line'].search_count([('event_key', '=', first.event_key)]), 1)

    def test_05_create_return_to_address_removal_request(self):
        order = self._removal()
        feed = AmazonAPI.build_fba_removal_flat_file(order)
        self.assertIn('AddressCountryCode', feed)
        self.assertIn('\tEG\t', feed)
        order.action_submit_to_amazon()
        self.assertEqual(order.state, 'queued')

    def test_06_create_disposal_request(self):
        order = self._removal('disposal', '6')
        feed = AmazonAPI.build_fba_removal_flat_file(order)
        self.assertIn('\tDisposal\t', feed)
        order.action_submit_to_amazon()
        self.assertFalse(order.picking_ids)

    def test_07_removal_feed_accepted(self):
        order = self._removal(suffix='7')
        success = '<AmazonEnvelope><Message><ProcessingReport><Result><ResultCode>Success</ResultCode></Result></ProcessingReport></Message></AmazonEnvelope>'
        self._submit_feed_mocked(order, success)
        self.assertEqual(order.state, 'submitted')
        self.assertEqual(order.feed_processing_status, 'DONE')

    def test_08_removal_feed_rejected(self):
        order = self._removal(suffix='8')
        error = '<AmazonEnvelope><Message><ProcessingReport><Result><ResultCode>Error</ResultCode><ResultMessageCode>InvalidSKU</ResultMessageCode><ResultDescription>Bad SKU</ResultDescription></Result></ProcessingReport></Message></AmazonEnvelope>'
        _submit, poll = self._submit_feed_mocked(order, error)
        self.assertEqual(poll.state, 'failed')
        self.assertEqual(order.state, 'failed')
        self.assertIn('InvalidSKU', order.error_message)

    def test_09_removal_partially_shipped(self):
        order = self.env['amazon.removal.order'].import_detail_row(self.instance, {
            'order-id': 'P7-R9', 'order-type': 'Return', 'order-status': 'Processing',
            'request-date': '2026-08-01', 'sku': 'P7-SKU', 'fnsku': 'P7-FNSKU',
            'disposition': 'Sellable', 'requested-quantity': '3', 'shipped-quantity': '1',
            'cancelled-quantity': '0', 'disposed-quantity': '0', 'in-process-quantity': '2',
        })
        self.assertEqual(order.line_ids.shipped_quantity, 1)
        self.assertEqual(order.line_ids.in_process_quantity, 2)

    def test_10_removal_fully_shipped(self):
        order = self.env['amazon.removal.order'].import_detail_row(self.instance, {
            'order-id': 'P7-R10', 'order-type': 'Return', 'order-status': 'Completed',
            'request-date': '2026-08-01', 'sku': 'P7-SKU', 'fnsku': 'P7-FNSKU',
            'disposition': 'Sellable', 'requested-quantity': '3', 'shipped-quantity': '3',
            'cancelled-quantity': '0', 'disposed-quantity': '0', 'in-process-quantity': '0',
        })
        self.assertEqual(order.total_shipped_quantity, 3)
        self.assertEqual(order.state, 'completed')

    def test_11_customer_warehouse_partial_receipt(self):
        order = self._removal(suffix='11')
        order.state = 'processing'
        order.line_ids.shipped_quantity = 3
        self._put_stock(self.instance.fba_sellable_location_id, 3)
        shipment = self.env['amazon.removal.shipment'].create({
            'shipment_key': 'P7-SHIP-11', 'order_id': order.id,
            'shipment_date': fields.Datetime.now(), 'sku': 'P7-SKU',
            'fnsku': 'P7-FNSKU', 'disposition': 'Sellable',
            'shipped_quantity': 3, 'line_id': order.line_ids.id,
        })
        self.env['amazon.phase7.stock.service'].apply_removal_shipment(shipment)
        self.assertEqual(shipment.dispatch_move_id.state, 'done')
        self.assertNotEqual(shipment.receipt_picking_id.state, 'done')
        self.assertEqual(order.total_received_quantity, 0)

        receipt = shipment.receipt_picking_id
        receipt.action_assign()
        receipt.move_ids.quantity = 1
        receipt.with_context(
            skip_backorder=True,
            picking_ids_not_to_backorder=receipt.ids,
        ).button_validate()
        self.assertEqual(receipt.state, 'done')
        self.assertEqual(order.line_ids.received_quantity, 1)
        self.assertEqual(order.total_received_quantity, 1)
        self.assertEqual(order.stock_action_state, 'partially_received')
        self.assertEqual(order.state, 'awaiting_receipt')

    def test_12_disposal_confirmed(self):
        order = self._removal('disposal', '12')
        self.instance.adjustment_stock_policy = 'event_moves'
        self._put_stock(self.instance.fba_sellable_location_id, 3)
        self.env['amazon.removal.order'].import_detail_row(self.instance, {
            'order-id': order.removal_order_id, 'order-type': 'Disposal', 'order-status': 'Completed',
            'request-date': '2026-08-01', 'sku': 'P7-SKU', 'fnsku': 'P7-FNSKU',
            'disposition': 'Sellable', 'requested-quantity': '3', 'shipped-quantity': '0',
            'cancelled-quantity': '0', 'disposed-quantity': '3', 'in-process-quantity': '0',
        })
        self.assertEqual(order.line_ids.disposal_move_id.state, 'done')
        self.assertEqual(order.stock_action_state, 'disposed')

    def _adjustment_row(self, reason, quantity, reference):
        return {
            'Date': '2026-08-01T10:00:00Z', 'FNSKU': 'P7-FNSKU',
            'ASIN': 'B000P70001', 'MSKU': 'P7-SKU', 'EventType': 'Adjustments',
            'ReferenceID': reference, 'Quantity': str(quantity), 'FulfillmentCenter': 'CAI1',
            'Disposition': 'SELLABLE', 'Reason': reason, 'Country': 'EG',
            'ReconciledQuantity': '0', 'UnreconciledQuantity': str(quantity),
        }

    def test_13_lost_inventory_event(self):
        event = self.env['amazon.fba.inventory.adjustment'].import_row(
            self.instance, self._adjustment_row('Lost', -1, 'LOSS-13')
        )
        self.assertEqual(event.event_category, 'lost')
        self.assertEqual(event.stock_action_state, 'informational')

    def test_14_found_inventory_linked_to_prior_loss(self):
        loss = self.env['amazon.fba.inventory.adjustment'].import_row(
            self.instance, self._adjustment_row('Lost', -1, 'LOSS-14')
        )
        found = self.env['amazon.fba.inventory.adjustment'].import_row(
            self.instance, self._adjustment_row('Found', 1, 'FOUND-14')
        )
        self.assertEqual(found.reversal_of_adjustment_id, loss)

    def test_15_damaged_inventory_event(self):
        event = self.env['amazon.fba.inventory.adjustment'].import_row(
            self.instance, self._adjustment_row('Warehouse Damaged', -1, 'DAMAGE-15')
        )
        self.assertEqual(event.event_category, 'damaged')

    def test_16_duplicate_adjustment_import(self):
        row = self._adjustment_row('Lost', -1, 'LOSS-16')
        first = self.env['amazon.fba.inventory.adjustment'].import_row(self.instance, row)
        second = self.env['amazon.fba.inventory.adjustment'].import_row(self.instance, row)
        self.assertEqual(first, second)

    def _reimbursement_row(self, suffix, cash='0', inventory='0', reason='Lost_Warehouse'):
        return {
            'approval-date': '2026-08-02T10:00:00Z', 'reimbursement-id': 'REIM-%s' % suffix,
            'case-id': '', 'amazon-order-id': '', 'reason': reason, 'sku': 'P7-SKU',
            'fnsku': 'P7-FNSKU', 'asin': 'B000P70001', 'condition': 'SELLABLE',
            'currency-unit': 'EGP', 'amount-per-unit': '100', 'amount-total': '100',
            'quantity-reimbursed-cash': cash, 'quantity-reimbursed-inventory': inventory,
            'original-reimbursement-id': '', 'original-reimbursement-type': '',
        }

    def test_17_cash_reimbursement(self):
        rec = self.env['amazon.fba.reimbursement'].import_row(
            self.instance, self._reimbursement_row('17', cash='1')
        )
        self.assertEqual(rec.quantity_reimbursed_cash, 1)
        self.assertEqual(rec.quantity_reimbursed_inventory, 0)

    def test_18_inventory_reimbursement(self):
        rec = self.env['amazon.fba.reimbursement'].import_row(
            self.instance, self._reimbursement_row('18', inventory='1')
        )
        self.assertEqual(rec.quantity_reimbursed_inventory, 1)
        self.assertFalse(rec._fields.get('move_id'))

    def test_19_reimbursement_reversal(self):
        row = self._reimbursement_row('19', cash='-1', reason='Reversal')
        row['original-reimbursement-id'] = 'REIM-OLD'
        row['original-reimbursement-type'] = 'REVERSAL'
        rec = self.env['amazon.fba.reimbursement'].import_row(self.instance, row)
        self.assertEqual(rec.reimbursement_classification, 'reversal')

    def test_20_ambiguous_reimbursement_matching(self):
        for suffix in ('20A', '20B'):
            event = self.env['amazon.return.report.line'].import_row(
                self.report, self._return_row('SELLABLE', suffix)
            )
            event.amazon_order_id = 'AMBIGUOUS-ORDER'
        row = self._reimbursement_row('20', cash='1', reason='Customer_Return')
        row['amazon-order-id'] = 'AMBIGUOUS-ORDER'
        rec = self.env['amazon.fba.reimbursement'].import_row(self.instance, row)
        rec._match_one()
        self.assertEqual(rec.review_state, 'manual_review')
        self.assertFalse(rec.linked_return_id)

    def test_21_duplicate_reimbursement_import(self):
        row = self._reimbursement_row('21', cash='1')
        first = self.env['amazon.fba.reimbursement'].import_row(self.instance, row)
        second = self.env['amazon.fba.reimbursement'].import_row(self.instance, row)
        self.assertEqual(first, second)

    def test_22_restart_resume_batch_cursor(self):
        rows = [self._return_row('SELLABLE', '22A'), self._return_row('SELLABLE', '22B')]
        job = self.env['amazon.phase7.job'].create({
            'instance_id': self.instance.id, 'operation_type': 'customer_returns',
            'source_model': self.report._name, 'source_id': self.report.id,
            'stage': 'process', 'state': 'pending', 'report_kind': 'returns',
            'raw_document': json.dumps(rows), 'total_found': 2, 'batch_size': 1,
        })
        job._run_one_turn()
        self.assertEqual(job.cursor_index, 1)
        self.assertEqual(job.state, 'pending')
        job._run_one_turn()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_processed, 2)

    def test_23_http_429_job_backoff(self):
        job = self.env['amazon.phase7.job'].create({
            'instance_id': self.instance.id, 'operation_type': 'reimbursements',
        })
        before = fields.Datetime.now()
        job._fail_or_retry(Exception('HTTP 429 throttled; Retry-After'))
        self.assertEqual(job.state, 'pending')
        self.assertGreater(job.next_run_at, before)

    def test_24_multi_company_isolation(self):
        other_instance = self.env['amazon.instance'].sudo().create({
            'name': 'P7 Other Instance', 'company_id': self.other_company.id,
        })
        other_event = self.env['amazon.fba.inventory.adjustment'].sudo().create({
            'instance_id': other_instance.id, 'event_key': 'OTHER-EVENT',
            'event_date': fields.Datetime.now(), 'quantity': -1,
        })
        rec = self.env['amazon.fba.reimbursement'].import_row(
            self.instance, self._reimbursement_row('24', cash='1')
        )
        with self.assertRaises(ValidationError):
            rec._set_match('linked_adjustment_id', other_event, 'forbidden')

    def test_25_existing_instance_menu_remains_visible(self):
        menu = self.env.ref('sdlc_amazon_connector.amazon_instance_menu')
        self.assertTrue(menu.active)
        self.assertEqual(menu.action.res_model, 'amazon.instance')

    def test_26_existing_products_orders_dashboard_unchanged(self):
        self.assertEqual(self.env.ref('sdlc_amazon_connector.amazon_product_menu').action.res_model, 'amazon.product')
        self.assertEqual(self.env.ref('sdlc_amazon_connector.amazon_all_orders_menu').action.res_model, 'amazon.sale.order')
        self.assertEqual(self.env.ref('sdlc_amazon_connector.amazon_dashboard_menu').action.tag, 'amazon_dashboard')

    def test_27_no_accounting_move_created(self):
        before = self.env['account.move'].sudo().search_count([])
        self.env['amazon.fba.reimbursement'].import_row(
            self.instance, self._reimbursement_row('27', cash='1')
        )
        event = self.env['amazon.return.report.line'].import_row(self.report, self._return_row('SELLABLE', '27'))
        event._classify_and_apply()
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)

    def test_28_no_direct_stock_quant_write(self):
        Quant = type(self.env['stock.quant'])
        with (
            patch.object(Quant, 'create', autospec=True, side_effect=AssertionError('direct quant create')) as create,
            patch.object(Quant, 'write', autospec=True, side_effect=AssertionError('direct quant write')) as write,
        ):
            event = self.env['amazon.return.report.line'].import_row(
                self.report, self._return_row('SELLABLE', '28')
            )
            event._classify_and_apply()
        create.assert_not_called()
        write.assert_not_called()
