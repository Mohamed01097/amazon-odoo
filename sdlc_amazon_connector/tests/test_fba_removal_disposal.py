import inspect
from datetime import timedelta
from types import SimpleNamespace

import requests

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_phase7 import AmazonPhase7StockService
from ..models.amazon_removal_order import AmazonRemovalOrder, AmazonRemovalShipment


@tagged('post_install', '-at_install', 'amazon_fba_removal_disposal')
class TestFBARemovalDisposal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].sudo().create({'name': 'FBA Removal Company'})
        warehouse_model = cls.env['stock.warehouse'].sudo().with_company(cls.company)
        cls.customer_warehouse = warehouse_model.create({
            'name': 'Removal Customer Warehouse',
            'code': 'RMCW',
            'company_id': cls.company.id,
        })
        cls.fba_warehouse = warehouse_model.create({
            'name': 'Removal Amazon Warehouse',
            'code': 'RMAW',
            'company_id': cls.company.id,
        })
        cls.removal_partner = cls.env['res.partner'].sudo().create({
            'name': 'Removal Destination',
            'street': '1 Warehouse Road',
            'city': 'Cairo',
            'zip': '11511',
            'country_id': cls.env.ref('base.eg').id,
            'phone': '+201000000000',
            'company_id': cls.company.id,
        })
        cls.instance = cls.env['amazon.instance'].sudo().create({
            'name': 'FBA Removal Egypt',
            'company_id': cls.company.id,
            'seller_id': 'REMOVAL-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
            'fba_warehouse_id': cls.fba_warehouse.id,
            'fba_source_location_id': cls.customer_warehouse.lot_stock_id.id,
            'fba_removal_return_partner_id': cls.removal_partner.id,
        })
        cls.instance.action_create_fba_stock_structure()
        cls.product = cls.env['product.product'].sudo().with_company(cls.company).create({
            'name': 'Removal Product',
            'default_code': 'REM-SKU',
            'type': 'consu',
            'is_storable': True,
            'company_id': cls.company.id,
        })
        cls.amazon_product = cls.env['amazon.product'].sudo().create({
            'name': 'Removal Amazon Product',
            'instance_id': cls.instance.id,
            'sku': 'REM-SKU',
            'asin': 'B0REMOVAL01',
            'odoo_product_id': cls.product.id,
        })

    def _detail_row(self, order_id='REM-001', **overrides):
        row = {
            'request-date': '2026-08-01T10:00:00Z',
            'order-id': order_id,
            'order-type': 'Return',
            'order-status': 'Processing',
            'last-updated-date': '2026-08-08T10:00:00Z',
            'sku': 'REM-SKU',
            'fnsku': 'REM-FNSKU',
            'disposition': 'Sellable',
            'requested-quantity': '10',
            'cancelled-quantity': '0',
            'disposed-quantity': '0',
            'shipped-quantity': '0',
            'in-process-quantity': '10',
        }
        row.update(overrides)
        return row

    def _shipment_row(self, order_id='REM-001', **overrides):
        row = {
            'request-date': '2026-08-01T10:00:00Z',
            'order-id': order_id,
            'shipment-date': '2026-08-08T12:00:00Z',
            'sku': 'REM-SKU',
            'fnsku': 'REM-FNSKU',
            'disposition': 'Sellable',
            'shipped-quantity': '10',
            'carrier': 'Amazon Logistics',
            'tracking-number': 'REM-TRACK-001',
            'removal-order-type': 'Return',
        }
        row.update(overrides)
        return row

    def _import_order(self, row=None):
        return self.env['amazon.removal.order'].sudo().import_detail_row(
            self.instance, row or self._detail_row(), 'REMOVAL-DETAIL-REPORT',
        )

    def _import_shipment(self, row=None):
        return self.env['amazon.removal.shipment'].sudo().import_row(
            self.instance, row or self._shipment_row(), 'REMOVAL-SHIPMENT-REPORT',
        )

    def _put_stock(self, location, quantity, product=None):
        product = product or self.product
        supplier = self.env.ref('stock.stock_location_suppliers')
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.fba_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': location.id,
            'company_id': self.company.id,
            'origin': 'REMOVAL TEST OPENING STOCK',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': location.id,
                'company_id': self.company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        self.assertEqual(picking.state, 'done')

    def _quantity(self, location, product=None):
        product = product or self.product
        return product.sudo().with_company(self.company).with_context(
            location=location.id,
        ).qty_available

    def _prepared_return_shipment(self, order_id='REM-PREPARED'):
        order = self._import_order(self._detail_row(order_id))
        shipment = self._import_shipment(self._shipment_row(order_id))
        return order, shipment

    def _receipt_with_transit(self, order_id='REM-RECEIPT'):
        order, shipment = self._prepared_return_shipment(order_id)
        self._put_stock(self.instance.fba_sellable_location_id, 10)
        shipment.action_move_to_removal_transit()
        action = shipment.action_create_receipt()
        return order, shipment, self.env['stock.picking'].sudo().browse(action['res_id'])

    def test_a_import_one_removal_order(self):
        order = self._import_order()
        self.assertEqual(order.removal_order_id, 'REM-001')
        self.assertEqual(order.marketplace_id, 'ARBP9OOSHTCHU')
        self.assertEqual(order.line_ids.requested_quantity, 10)
        self.assertEqual(order.line_ids.mapping_status, 'mapped')

    def test_b_repeated_removal_import_is_idempotent(self):
        first = self._import_order()
        second = self._import_order()
        self.assertEqual(first, second)
        self.assertEqual(self.env['amazon.removal.order'].search_count([
            ('instance_id', '=', self.instance.id), ('removal_order_id', '=', 'REM-001'),
        ]), 1)
        self.assertEqual(first.line_count, 1)

    def test_c_removal_with_multiple_skus(self):
        second_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Removal Product 2', 'default_code': 'REM-SKU-2',
            'type': 'consu', 'is_storable': True, 'company_id': self.company.id,
        })
        self.env['amazon.product'].sudo().create({
            'name': 'Removal Amazon Product 2', 'instance_id': self.instance.id,
            'sku': 'REM-SKU-2', 'asin': 'B0REMOVAL02', 'odoo_product_id': second_product.id,
        })
        order = self._import_order(self._detail_row('REM-MULTI'))
        self._import_order(self._detail_row(
            'REM-MULTI', sku='REM-SKU-2', fnsku='REM-FNSKU-2',
            **{'requested-quantity': '3', 'in-process-quantity': '3'},
        ))
        self.assertEqual(order.line_count, 2)
        self.assertEqual(set(order.line_ids.mapped('sku')), {'REM-SKU', 'REM-SKU-2'})

    def test_d_cumulative_partial_shipment_delta_is_idempotent(self):
        self._import_order(self._detail_row('REM-DELTA'))
        first = self._import_shipment(self._shipment_row(
            'REM-DELTA', **{'shipped-quantity': '4'}
        ))
        self.assertEqual(first.shipped_delta_quantity, 4)
        self._put_stock(self.instance.fba_sellable_location_id, 10)
        first.action_move_to_removal_transit()
        self.assertEqual(first.dispatched_stock_quantity, 4)
        self.assertEqual(self._quantity(self.instance.fba_removal_transit_location_id), 4)
        second = self._import_shipment(self._shipment_row(
            'REM-DELTA', **{'shipped-quantity': '10'}
        ))
        self.assertEqual(first, second)
        self.assertEqual(second.previous_shipped_quantity, 4)
        self.assertEqual(second.shipped_delta_quantity, 6)
        self.assertEqual(second.shipped_quantity, 10)
        self.assertEqual(second.carrier, 'Amazon Logistics')
        self.assertEqual(second.tracking_number, 'REM-TRACK-001')
        second.action_move_to_removal_transit()
        self.assertEqual(second.dispatched_stock_quantity, 10)
        self.assertEqual(len(second.dispatch_picking_ids), 2)
        self.assertEqual(self._quantity(self.instance.fba_removal_transit_location_id), 10)
        second.action_move_to_removal_transit()
        self.assertEqual(len(second.dispatch_picking_ids), 2)

    def test_e_create_customer_receipt_from_removal_transit(self):
        order, shipment, receipt = self._receipt_with_transit('REM-RECEIPT-E')
        self.assertEqual(receipt.location_id, self.instance.fba_removal_transit_location_id)
        self.assertEqual(receipt.location_dest_id, self.instance.fba_source_location_id)
        self.assertNotEqual(receipt.state, 'done')
        self.assertEqual(receipt.amazon_removal_order_id, order)
        self.assertEqual(receipt.amazon_removal_shipment_id, shipment)
        self.assertEqual(receipt.move_ids.product_uom_qty, 10)

    def test_f_customer_receipt_cannot_be_created_twice(self):
        _order, shipment, first = self._receipt_with_transit('REM-RECEIPT-F')
        shipment.action_move_to_removal_transit()
        self.assertEqual(len(shipment.dispatch_picking_ids), 1)
        second_action = shipment.action_create_receipt()
        self.assertEqual(second_action['res_id'], first.id)
        self.assertEqual(self.env['stock.picking'].search_count([
            ('amazon_removal_shipment_id', '=', shipment.id),
            ('amazon_fba_movement_type', '=', 'removal_receipt'),
        ]), 1)

    def test_g_partial_physical_receipt_does_not_fabricate_balance(self):
        order, shipment, receipt = self._receipt_with_transit('REM-RECEIPT-G')
        receipt.action_assign()
        receipt.move_ids.quantity = 8
        receipt.with_context(
            skip_backorder=True,
            picking_ids_not_to_backorder=receipt.ids,
        ).button_validate()
        self.assertEqual(receipt.state, 'done')
        self.assertEqual(shipment.received_quantity, 8)
        self.assertEqual(order.total_received_quantity, 8)
        self.assertEqual(self._quantity(self.instance.fba_removal_transit_location_id), 2)
        self.assertEqual(self._quantity(self.instance.fba_source_location_id), 8)

    def test_h_disposal_creates_no_customer_receipt(self):
        order = self._import_order(self._detail_row(
            'REM-DISPOSAL-H', **{
                'order-type': 'Disposal', 'order-status': 'Completed',
                'disposition': 'Unsellable', 'disposed-quantity': '3',
                'in-process-quantity': '0',
            }
        ))
        self.assertEqual(order.removal_type, 'disposal')
        self.assertFalse(order.picking_ids)
        self.assertFalse(order.shipment_ids)

    def test_i_disposal_does_not_double_reduce_inventory(self):
        self._put_stock(self.instance.fba_unsellable_location_id, 5)
        before = self._quantity(self.instance.fba_unsellable_location_id)
        order = self._import_order(self._detail_row(
            'REM-DISPOSAL-I', **{
                'order-type': 'Disposal', 'order-status': 'Completed',
                'disposition': 'Unsellable', 'requested-quantity': '5',
                'disposed-quantity': '5', 'in-process-quantity': '0',
            }
        ))
        self.assertEqual(self._quantity(self.instance.fba_unsellable_location_id), before)
        self.assertFalse(order.line_ids.disposal_move_id)
        self.assertFalse(order.picking_ids)
        self.assertEqual(order.stock_action_state, 'audit_only')

    def test_j_removal_import_does_not_double_reduce_reconciled_stock(self):
        self._put_stock(self.instance.fba_sellable_location_id, 40)
        before = self._quantity(self.instance.fba_sellable_location_id)
        self._prepared_return_shipment('REM-RECONCILED-J')
        self.assertEqual(self._quantity(self.instance.fba_sellable_location_id), before)
        self.assertEqual(self._quantity(self.instance.fba_removal_transit_location_id), 0)

    def test_k_unmapped_sku_is_retained_without_stock_move(self):
        order = self._import_order(self._detail_row(
            'REM-UNMAPPED-K', sku='UNKNOWN-REM-SKU', fnsku='UNKNOWN-REM-FNSKU',
        ))
        self.assertEqual(order.line_ids.mapping_status, 'unmapped')
        self.assertEqual(order.line_ids.discrepancy_code, 'UNMAPPED_SKU')
        self.assertTrue(order.manual_review_required)
        self.assertFalse(order.picking_ids)

    def test_l_quantity_above_tracked_stock_is_review_only(self):
        self._put_stock(self.instance.fba_sellable_location_id, 2)
        order, shipment = self._prepared_return_shipment('REM-EXCESS-L')
        before = self._quantity(self.instance.fba_sellable_location_id)
        result = shipment.action_move_to_removal_transit()
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(shipment.discrepancy_code, 'REMOVAL_EXCEEDS_TRACKED_STOCK')
        self.assertEqual(order.stock_action_state, 'manual_review')
        self.assertFalse(shipment.dispatch_picking_ids)
        self.assertEqual(self._quantity(self.instance.fba_sellable_location_id), before)

    def test_m_http_429_respects_retry_after(self):
        cursor = fields.Datetime.now() - timedelta(days=5)
        self.instance.write({'last_fba_removal_sync_at': cursor})
        self.instance.action_import_removal_orders()
        job = self.env['amazon.phase7.job'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('operation_type', '=', 'removal_status'),
            ('state', 'in', ('pending', 'running', 'waiting_amazon')),
        ], limit=1)
        self.assertTrue(job)
        self.assertEqual(job.date_from, fields.Date.to_date(cursor) - timedelta(days=2))
        job.state = 'running'
        response = SimpleNamespace(status_code=429, headers={'Retry-After': '120'})
        cause = requests.HTTPError('429 Too Many Requests')
        cause.response = response
        started = fields.Datetime.now()
        try:
            raise UserError('Amazon report request failed') from cause
        except UserError as exc:
            job._fail_or_retry(exc)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(job.last_error_code, '429')
        self.assertGreaterEqual(job.next_run_at, started + timedelta(seconds=119))

    def test_n_multiple_instances_are_isolated(self):
        second = self.env['amazon.instance'].sudo().create({
            'name': 'FBA Removal Egypt 2',
            'company_id': self.company.id,
            'seller_id': 'REMOVAL-SELLER-EG-2',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh-2',
            'client_id': 'mock-client-2',
            'client_secret': 'mock-secret-2',
        })
        self.env['amazon.product'].sudo().create({
            'name': 'Removal Amazon Product 2',
            'instance_id': second.id,
            'sku': 'REM-SKU',
            'asin': 'B0REMOVAL01',
            'odoo_product_id': self.product.id,
        })
        first_order = self._import_order(self._detail_row('REM-SHARED-ID'))
        second_order = self.env['amazon.removal.order'].sudo().import_detail_row(
            second, self._detail_row('REM-SHARED-ID'), 'REMOVAL-DETAIL-SECOND',
        )
        self.assertNotEqual(first_order, second_order)
        self.assertEqual(first_order.instance_id, self.instance)
        self.assertEqual(second_order.instance_id, second)

    def test_o_no_connector_direct_stock_quant_write(self):
        source = '\n'.join((
            inspect.getsource(AmazonPhase7StockService),
            inspect.getsource(AmazonRemovalOrder),
            inspect.getsource(AmazonRemovalShipment),
        ))
        self.assertNotIn("env['stock.quant']", source)
        self.assertNotIn('UPDATE STOCK_QUANT', source.upper())
        self.assertNotIn('quant.quantity =', source)
