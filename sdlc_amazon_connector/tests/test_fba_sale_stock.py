import inspect
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI
from ..models.amazon_fba_sale_stock import AmazonFbaSaleStockEvent


@tagged('post_install', '-at_install', 'amazon_fba_sale_stock')
class TestAmazonFbaSaleStock(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({'name': 'FBA Sale Stock Test Company'})
        warehouse_model = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.customer_warehouse = warehouse_model.create({
            'name': 'FBA Sale Customer Warehouse', 'code': 'FSSCW',
            'company_id': self.company.id,
        })
        self.fba_warehouse = warehouse_model.create({
            'name': 'FBA Sale Amazon Warehouse', 'code': 'FSSAW',
            'company_id': self.company.id,
        })
        self.partner = self.env['res.partner'].sudo().create({
            'name': 'FBA Sale Customer', 'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'FBA Sale Egypt',
            'company_id': self.company.id,
            'seller_id': 'FBA-SALE-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.customer_warehouse.lot_stock_id.id,
            'fba_removal_return_partner_id': self.partner.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product, self.amazon_product = self._create_product('SKU-1')
        self._put_stock(self.product, self.instance.fba_source_location_id, 70)
        self._put_stock(self.product, self.instance.fba_sellable_location_id, 24)

    def _create_product(self, sku):
        product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku, 'default_code': sku, 'type': 'consu',
            'is_storable': True, 'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon %s' % sku, 'instance_id': self.instance.id,
            'sku': sku, 'asin': 'B0%s' % sku.replace('-', ''),
            'fulfillment_channel': 'AFN', 'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _put_stock(self, product, destination, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': destination.id,
            'company_id': self.company.id,
            'origin': 'FBA SALE TEST OPENING STOCK',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': destination.id,
                'company_id': self.company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
            skip_backorder=True,
        ).button_validate()
        self.assertEqual(picking.state, 'done')
        return picking

    def _quantity(self, product, location):
        product.invalidate_recordset()
        return product.sudo().with_company(self.company).with_context(location=location.id).qty_available

    def _order_line(self, order_ref='ORDER-1', sku='SKU-1', quantity=5, product=None, amazon_product=None):
        product = product or self.product
        amazon_product = amazon_product or self.amazon_product
        order = self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': order_ref,
            'instance_id': self.instance.id,
            'fulfillment_channel': 'AFN',
            'amazon_status': 'Unshipped',
        })
        line = self.env['amazon.sale.order.line'].sudo().create({
            'order_id': order.id,
            'amazon_order_item_id': '%s-ITEM-%s' % (order_ref, sku),
            'amazon_product_id': amazon_product.id,
            'odoo_product_id': product.id,
            'sku': sku,
            'quantity': quantity,
        })
        return order, line

    def _event(self, cumulative, order_ref='ORDER-1', sku='SKU-1', quantity=5,
               product=None, amazon_product=None):
        order, line = self._order_line(order_ref, sku, quantity, product, amazon_product)
        event = self.env['amazon.fba.sale.stock.event'].sudo().upsert_from_order_line(
            line, cumulative, fields.Datetime.now(),
        )
        return order, line, event

    def _item_payload(self, line, cumulative):
        return {
            'OrderItemId': line.amazon_order_item_id,
            'SellerSKU': line.sku,
            'ASIN': line.asin or 'B0TEST',
            'Title': line.title or line.sku,
            'QuantityOrdered': line.quantity,
            'QuantityShipped': cumulative,
        }

    @staticmethod
    def _inventory_summary(sku, sellable, reserved=0, unsellable=0):
        return {
            'asin': 'B0TEST', 'fnSku': 'FNSKU-%s' % sku, 'sellerSku': sku,
            'condition': 'NewItem', 'lastUpdatedTime': '2026-08-29T10:00:00Z',
            'totalQuantity': sellable + reserved + unsellable,
            'inventoryDetails': {
                'fulfillableQuantity': sellable,
                'inboundWorkingQuantity': 0,
                'inboundShippedQuantity': 0,
                'inboundReceivingQuantity': 0,
                'reservedQuantity': {
                    'totalReservedQuantity': reserved,
                    'pendingCustomerOrderQuantity': reserved,
                    'pendingTransshipmentQuantity': 0,
                    'fcProcessingQuantity': 0,
                },
                'unfulfillableQuantity': {'totalUnfulfillableQuantity': unsellable},
            },
        }

    def _audit(self, *summaries):
        page = {'payload': {'inventorySummaries': list(summaries)}}
        response = {
            'payload': {'inventorySummaries': list(summaries)},
            '_amazon_request_ids': ['MOCK-INVENTORY-REQUEST'],
            '_pages': [page], '_snapshot_complete': True, '_page_count': 1,
        }
        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id, 'trigger': 'manual',
        })
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='mock-token'),
            patch.object(AmazonAPI, 'get_all_inventory_summaries', autospec=True, return_value=response),
        ):
            self.assertTrue(run._process_run(), run.last_error)
        return run

    def test_01_sale_five_consumes_sellable_once(self):
        _order, _line, event = self._event(5)
        picking = event._process_one()
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)
        self.assertEqual(event.processed_fulfilled_qty, 5)
        self.assertEqual(picking.location_id, self.instance.fba_sellable_location_id)
        self.assertEqual(picking.location_dest_id, self.instance.fba_sold_customer_location_id)

    def test_02_customer_warehouse_remains_seventy(self):
        _order, _line, event = self._event(5)
        event._process_one()
        self.assertEqual(self._quantity(self.product, self.instance.fba_source_location_id), 70)

    def test_03_repeated_order_import_is_idempotent(self):
        order, line = self._order_line()
        importer = self.env['amazon.order.import.job'].new({'instance_id': self.instance.id})
        for _index in range(10):
            importer._upsert_order_items(order, [self._item_payload(line, 5)])
        event = self.env['amazon.fba.sale.stock.event'].search([('order_line_id', '=', line.id)])
        event._process_one()
        for _index in range(10):
            importer._upsert_order_items(order, [self._item_payload(line, 5)])
            event._process_one()
        self.assertEqual(len(event), 1)
        self.assertEqual(len(event.picking_ids), 1)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_04_repeated_status_sync_is_idempotent(self):
        order, line = self._order_line()
        job = self.env['amazon.order.status.sync.job'].new({'instance_id': self.instance.id})
        payload = {
            'AmazonOrderId': order.amazon_order_ref,
            'OrderStatus': 'Shipped', 'FulfillmentChannel': 'AFN',
            'LastUpdateDate': '2026-08-29T10:00:00Z',
            'OrderItems': [self._item_payload(line, 5)],
        }
        for _index in range(10):
            job._process_one_order(payload)
        event = self.env['amazon.fba.sale.stock.event'].search([('order_line_id', '=', line.id)])
        event._process_one()
        for _index in range(10):
            job._process_one_order(payload)
            event._process_one()
        self.assertEqual(len(event.picking_ids), 1)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_05_cumulative_two_then_five_moves_two_then_three(self):
        _order, line, event = self._event(2)
        event._process_one()
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 22)
        self.env['amazon.fba.sale.stock.event'].upsert_from_order_line(line, 5)
        event._process_one()
        self.assertEqual(sorted(event.picking_ids.mapped('move_ids').mapped('quantity')), [2, 3])
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_06_cancellation_before_fulfillment_moves_nothing(self):
        order, _line, event = self._event(0)
        order.amazon_status = 'Canceled'
        event._process_one()
        self.assertFalse(event.picking_ids)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 24)

    def test_07_partial_fulfillment_then_cancellation_moves_only_three(self):
        order, _line, event = self._event(3)
        event._process_one()
        order.amazon_status = 'Canceled'
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 3)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 21)

    def test_08_financial_refund_does_not_restore_stock(self):
        order, _line, event = self._event(5)
        event._process_one()
        order.write({'requires_status_review': True, 'status_review_reason': 'Financial refund only'})
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)
        self.assertEqual(len(event.picking_ids), 1)

    def _import_return(self, disposition, license_plate):
        report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id, 'state': 'downloaded',
            'amazon_report_id': 'RETURN-%s' % license_plate,
        })
        line = self.env['amazon.return.report.line'].sudo().import_row(report, {
            'return-date': '2026-08-29T12:00:00Z', 'order-id': 'ORDER-1',
            'sku': 'SKU-1', 'asin': 'B0SKU1', 'fnsku': 'FNSKU-SKU-1',
            'product-name': 'SKU-1', 'quantity': '1',
            'fulfillment-center-id': 'CAI1', 'detailed-disposition': disposition,
            'reason': 'CUSTOMER_RETURN', 'status': 'Unit returned to inventory',
            'license-plate-number': license_plate, 'customer-comments': '',
        })
        line._classify_and_apply()
        return line

    def test_09_sellable_return_does_not_reverse_sale_event(self):
        _order, _line, event = self._event(5)
        event._process_one()
        returned = self._import_return('SELLABLE', 'LPN-SELLABLE')
        self.assertFalse(returned.linked_stock_move_id)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_10_unsellable_return_does_not_reverse_sale_event(self):
        _order, _line, event = self._event(5)
        event._process_one()
        returned = self._import_return('UNSELLABLE', 'LPN-UNSELLABLE')
        self.assertFalse(returned.linked_stock_move_id)
        self.assertEqual(self._quantity(self.product, self.instance.fba_unsellable_location_id), 0)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_11_snapshot_after_sale_matches_without_move(self):
        _order, _line, event = self._event(5)
        event._process_one()
        before = self.env['stock.picking'].search_count([])
        line = self._audit(self._inventory_summary('SKU-1', 19)).reconciliation_ids
        self.assertEqual(line.status, 'matched')
        self.assertEqual(line.difference_sellable, 0)
        self.assertEqual(self.env['stock.picking'].search_count([]), before)

    def test_12_snapshot_before_event_race_cannot_double_deduct(self):
        line = self._audit(self._inventory_summary('SKU-1', 19)).reconciliation_ids
        self.assertEqual(line.sale_overlap_state, 'snapshot_outflow')
        _order, _order_line, event = self._event(5)
        event._process_one()
        line.invalidate_recordset()
        self.assertEqual(line.status, 'matched')
        self.assertEqual(line.sale_overlap_state, 'resolved')
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_13_multi_sku_processing_is_independent(self):
        product2, amazon_product2 = self._create_product('SKU-2')
        self._put_stock(product2, self.instance.fba_sellable_location_id, 10)
        order, line1 = self._order_line(order_ref='ORDER-A')
        line2 = self.env['amazon.sale.order.line'].sudo().create({
            'order_id': order.id,
            'amazon_order_item_id': 'ORDER-A-ITEM-SKU-2',
            'amazon_product_id': amazon_product2.id,
            'odoo_product_id': product2.id,
            'sku': 'SKU-2',
            'quantity': 3,
        })
        event1 = self.env['amazon.fba.sale.stock.event'].upsert_from_order_line(line1, 5)
        event2 = self.env['amazon.fba.sale.stock.event'].upsert_from_order_line(line2, 3)
        event1._process_one()
        with patch.object(
            type(event2), '_create_and_validate_delta_picking', autospec=True,
            side_effect=UserError('temporary failure in SKU-2'),
        ):
            event2._process_one()
        event1._process_one()
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)
        self.assertEqual(self._quantity(product2, self.instance.fba_sellable_location_id), 10)
        self.assertEqual(len(event1.picking_ids), 1)

    def test_14_multiple_orders_consume_independent_deltas(self):
        _order_a, _line_a, event_a = self._event(5, order_ref='ORDER-A')
        _order_b, _line_b, event_b = self._event(2, order_ref='ORDER-B', quantity=2)
        event_a._process_one()
        event_b._process_one()
        event_a._process_one()
        event_b._process_one()
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 17)
        self.assertEqual(len(event_a.picking_ids | event_b.picking_ids), 2)

    def test_15_concurrent_worker_contract_uses_row_and_skip_locked_locks(self):
        _order, _line, event = self._event(5)
        event._process_one()
        event.write({'state': 'pending', 'next_run_at': fields.Datetime.now()})
        self.env['amazon.fba.sale.stock.event'].cron_process_fba_sale_stock_events()
        self.env['amazon.fba.sale.stock.event'].cron_process_fba_sale_stock_events()
        source = inspect.getsource(AmazonFbaSaleStockEvent)
        self.assertIn('FOR UPDATE SKIP LOCKED', source)
        self.assertIn('pg_advisory_xact_lock', source)
        self.assertEqual(len(event.picking_ids), 1)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_16_restart_after_partial_processing_resumes_remaining_three(self):
        _order, line, event = self._event(2)
        event._process_one()
        self.env['amazon.fba.sale.stock.event'].upsert_from_order_line(line, 5)
        self.env.invalidate_all()
        restarted = self.env['amazon.fba.sale.stock.event'].browse(event.id)
        restarted._process_one()
        self.assertEqual(restarted.processed_fulfilled_qty, 5)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_17_failure_before_progress_update_retries_exact_delta(self):
        _order, _line, event = self._event(5)
        before = self.env['stock.picking'].search_count([])
        with patch.object(
            type(event), '_create_and_validate_delta_picking', autospec=True,
            side_effect=UserError('temporary failure before stock validation'),
        ):
            event._process_one()
        event.invalidate_recordset()
        self.assertEqual(event.processed_fulfilled_qty, 0)
        self.assertEqual(self.env['stock.picking'].search_count([]), before)
        event.write({'state': 'pending', 'next_run_at': fields.Datetime.now()})
        event._process_one()
        self.assertEqual(event.processed_fulfilled_qty, 5)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)

    def test_18_insufficient_sellable_creates_manual_review_without_negative(self):
        product2, amazon_product2 = self._create_product('SKU-LOW')
        self._put_stock(product2, self.instance.fba_sellable_location_id, 3)
        _order, _line, event = self._event(
            5, order_ref='ORDER-LOW', sku='SKU-LOW', quantity=5,
            product=product2, amazon_product=amazon_product2,
        )
        event._process_one()
        self.assertEqual(event.state, 'manual_review')
        self.assertEqual(event.processed_fulfilled_qty, 0)
        self.assertFalse(event.picking_ids)
        self.assertEqual(self._quantity(product2, self.instance.fba_sellable_location_id), 3)
        self.assertTrue(self.env['amazon.operation.control'].search([
            ('source_model', '=', event._name), ('source_id', '=', event.id),
            ('state', '=', 'manual_review'),
        ]))

    def test_19_removal_uses_post_sale_sellable_balance(self):
        _order, _line, event = self._event(5)
        event._process_one()
        removal = self.env['amazon.removal.order'].sudo().import_detail_row(self.instance, {
            'request-date': '2026-08-29T10:00:00Z', 'order-id': 'REM-AFTER-SALE',
            'order-type': 'Return', 'order-status': 'Processing',
            'last-updated-date': '2026-08-29T11:00:00Z', 'sku': 'SKU-1',
            'fnsku': 'FNSKU-SKU-1', 'disposition': 'Sellable',
            'requested-quantity': '3', 'cancelled-quantity': '0',
            'disposed-quantity': '0', 'shipped-quantity': '0', 'in-process-quantity': '3',
        }, 'MOCK-REMOVAL-DETAIL')
        shipment = self.env['amazon.removal.shipment'].sudo().import_row(self.instance, {
            'request-date': '2026-08-29T10:00:00Z', 'order-id': removal.removal_order_id,
            'shipment-date': '2026-08-29T12:00:00Z', 'sku': 'SKU-1',
            'fnsku': 'FNSKU-SKU-1', 'disposition': 'Sellable',
            'shipped-quantity': '3', 'carrier': 'Amazon Logistics',
            'tracking-number': 'REM-TRACK', 'removal-order-type': 'Return',
        }, 'MOCK-REMOVAL-SHIPMENT')
        shipment.action_move_to_removal_transit()
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 16)
        self.assertEqual(self._quantity(self.product, self.instance.fba_removal_transit_location_id), 3)

    def test_20_no_direct_quant_writes_and_generic_afn_delivery_suppressed(self):
        order, _line, event = self._event(5)
        sale_order = self.env['sale.order'].sudo().with_company(self.company).create({
            'partner_id': self.partner.id, 'company_id': self.company.id,
            'warehouse_id': self.fba_warehouse.id,
            'amazon_order_id': order.id, 'is_amazon_order': True,
            'amazon_instance_id': self.instance.id,
            'amazon_fulfillment_channel': 'AFN',
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_uom_qty': 5, 'price_unit': 180,
            })],
        })
        order.sale_order_id = sale_order.id
        sale_order.action_confirm()
        self.assertFalse(sale_order.picking_ids)
        event._process_one()
        source = inspect.getsource(AmazonFbaSaleStockEvent)
        self.assertNotIn("env['stock.quant']", source)
        self.assertNotIn('UPDATE STOCK_QUANT', source.upper())
        self.assertEqual(self._quantity(self.product, self.instance.fba_source_location_id), 70)
        self.assertEqual(self._quantity(self.product, self.instance.fba_sellable_location_id), 19)
