import base64
import json
from pathlib import Path
from unittest.mock import patch

import requests

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install')
class TestFbaReceivingPhase5(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon FBA Receiving Test Company',
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.source_warehouse = Warehouse.create({
            'name': 'Receiving Source Warehouse',
            'code': 'RCSRC',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Receiving FBA Warehouse',
            'code': 'RCFBA',
            'company_id': self.company.id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Receiving Test Instance',
            'company_id': self.company.id,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.source_warehouse.lot_stock_id.id,
        })
        self.instance.action_create_fba_stock_structure()
        self.product_a, self.amazon_product_a = self._create_product('RC-SKU-A')
        self.product_b, self.amazon_product_b = self._create_product('RC-SKU-B')
        self.shipment, physical = self._create_plan(
            1,
            [[(self.amazon_product_a, 100, 'FN-RC-A')]],
        )
        self.physical = physical[0]
        self._dispatch_physical_shipments(physical)

    def _create_product(self, sku):
        product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': sku,
            'default_code': sku,
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        amazon_product = self.env['amazon.product'].sudo().create({
            'name': sku,
            'instance_id': self.instance.id,
            'sku': sku,
            'odoo_product_id': product.id,
        })
        return product, amazon_product

    def _create_plan(self, sequence, physical_item_groups):
        plan_id = 'wf%08d-1234-abcd-5678-1234abcd5678' % sequence
        placement_id = 'pl%08d-1234-abcd-5678-1234abcd5678' % sequence
        shipment_ids = [
            'sh%08d-1234-abcd-5678-1234abcd%04d' % (sequence * 100 + index, index)
            for index in range(1, len(physical_item_groups) + 1)
        ]
        planned = {}
        products = {}
        fnskus = {}
        for item_group in physical_item_groups:
            for amazon_product, quantity, fnsku in item_group:
                planned[amazon_product.sku] = planned.get(amazon_product.sku, 0) + quantity
                products[amazon_product.sku] = amazon_product
                fnskus[amazon_product.sku] = fnsku
        shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'RC-PLAN-%s' % sequence,
            'shipment_name': 'RC-PLAN-%s' % sequence,
            'instance_id': self.instance.id,
            'inbound_plan_id': plan_id,
            'shipment_id': shipment_ids[0] if len(shipment_ids) == 1 else False,
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'packing_information_status': 'success',
            'placement_confirmation_status': 'success',
            'state': 'placement_confirmed',
            'line_ids': [Command.create({
                'amazon_product_id': products[sku].id,
                'odoo_product_id': products[sku].odoo_product_id.id,
                'sku': sku,
                'fnsku': fnskus[sku],
                'planned_quantity': quantity,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            }) for sku, quantity in planned.items()],
        })
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'amazon_placement_option_id': placement_id,
            'status': 'ACCEPTED',
            'amazon_shipment_ids': json.dumps(shipment_ids),
            'selected': True,
        })
        physical_shipments = self.env['amazon.fba.physical.shipment']
        for index, item_group in enumerate(physical_item_groups, start=1):
            physical = self.env['amazon.fba.physical.shipment'].sudo().create({
                'inbound_shipment_id': shipment.id,
                'placement_option_id': placement.id,
                'amazon_shipment_id': shipment_ids[index - 1],
                'shipment_confirmation_id': 'FBA19RC%04d%02d' % (sequence, index),
                'status': 'SHIPPED',
                'transportation_confirmation_status': 'success',
                'labels_status': 'success',
                'product_labels_confirmed': True,
                'destination_fc': 'CAI1',
                'line_ids': [Command.create({
                    'amazon_product_id': amazon_product.id,
                    'msku': amazon_product.sku,
                    'fnsku': fnsku,
                    'quantity': quantity,
                }) for amazon_product, quantity, fnsku in item_group],
            })
            attachment = self.env['ir.attachment'].sudo().create({
                'name': 'receiving-test-labels.pdf',
                'type': 'binary',
                'datas': base64.b64encode(b'%PDF-1.4 receiving test labels'),
                'mimetype': 'application/pdf',
                'res_model': physical._name,
                'res_id': physical.id,
            })
            physical.shipping_label_attachment_id = attachment
            physical_shipments |= physical
        return shipment, physical_shipments

    def _put_in_source(self, product, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        source = self.instance.fba_source_location_id
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.source_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': source.id,
            'company_id': self.company.id,
            'origin': 'RECEIVING-TEST-SEED',
            'move_ids': [Command.create({
                'product_id': product.id,
                'product_uom_qty': quantity,
                'product_uom': product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': source.id,
                'company_id': self.company.id,
            })],
        })
        result = picking.with_context(
            picking_ids_not_to_backorder=picking.ids,
        ).button_validate()
        self.assertNotIsInstance(result, dict)
        self.assertEqual(picking.state, 'done')

    def _dispatch_physical_shipments(self, physical_shipments):
        requirements = {}
        for line in physical_shipments.mapped('line_ids'):
            product = line.amazon_product_id.odoo_product_id
            requirements[product] = requirements.get(product, 0) + line.quantity
        for product, quantity in requirements.items():
            self._put_in_source(product, quantity)
        for physical in physical_shipments:
            picking, created = physical._create_dispatch_picking()
            self.assertTrue(created)
            result = picking.with_context(
                picking_ids_not_to_backorder=picking.ids,
            ).button_validate()
            self.assertNotIsInstance(result, dict)
            self.assertEqual(picking.state, 'done')
            self.assertEqual(physical.dispatch_state, 'dispatched')
        physical_shipments.mapped('inbound_shipment_id').write({'state': 'waiting_receiving'})

    def _quantity_at(self, product, location):
        product.invalidate_recordset()
        return product.with_context(location=location.id).qty_available

    @staticmethod
    def _status_response(physical, status='RECEIVING'):
        return {
            'placementOptionId': physical.placement_option_id.amazon_placement_option_id,
            'shipmentId': physical.amazon_shipment_id,
            'shipmentConfirmationId': physical.shipment_confirmation_id,
            'destination': {
                'destinationType': 'AMAZON_WAREHOUSE',
                'warehouseId': physical.destination_fc,
            },
            'status': status,
            '_amazon_request_id': 'receiving-status-request',
        }

    @staticmethod
    def _items_response(physical, received_by_sku, shipped_by_sku=None, extra_items=None):
        shipped_by_sku = shipped_by_sku or {}
        items = [{
            'ShipmentId': physical.shipment_confirmation_id,
            'SellerSKU': line.msku,
            'FulfillmentNetworkSKU': line.fnsku,
            'QuantityShipped': shipped_by_sku.get(line.msku, line.quantity),
            'QuantityReceived': received_by_sku.get(line.msku, 0),
            'QuantityInCase': 0,
        } for line in physical.line_ids]
        items.extend(extra_items or [])
        return {
            'payload': {'ItemData': items},
            '_amazon_request_ids': ['receiving-items-request'],
            '_pages': [{'payload': {'ItemData': items}}],
        }

    def _apply(self, physical, received_by_sku, status='RECEIVING',
               shipped_by_sku=None, extra_items=None):
        return physical._apply_receiving_snapshot(
            self._status_response(physical, status),
            self._items_response(
                physical, received_by_sku,
                shipped_by_sku=shipped_by_sku,
                extra_items=extra_items,
            ),
        )

    @staticmethod
    def _receiving_pickings(physical):
        return physical.picking_ids.filtered(
            lambda picking: picking.amazon_fba_movement_type == 'receiving_staging'
        )

    def test_00_receiving_requires_done_physical_dispatch(self):
        _shipment, physical_shipments = self._create_plan(
            99,
            [[(self.amazon_product_a, 1, 'FN-RC-A')]],
        )
        physical = physical_shipments[0]
        with self.assertRaises(UserError):
            self._apply(physical, {'RC-SKU-A': 1})

    def test_a_dispatch_100_amazon_received_30_moves_delta_30(self):
        result = self._apply(self.physical, {'RC-SKU-A': 30})
        line = self.physical.line_ids
        self.assertEqual(result['deltaReceived'], 30)
        self.assertEqual(line.dispatched_quantity, 100)
        self.assertEqual(line.amazon_received_quantity, 30)
        self.assertEqual(line.processed_received_quantity, 30)
        self.assertEqual(line.remaining_in_transit_quantity, 70)
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id), 70,
        )
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_received_location_id), 30,
        )
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_sellable_location_id), 0,
        )

    def test_b_amazon_increases_30_to_80_moves_delta_50(self):
        self._apply(self.physical, {'RC-SKU-A': 30})
        result = self._apply(self.physical, {'RC-SKU-A': 80})
        self.assertEqual(result['deltaReceived'], 50)
        self.assertEqual(self.physical.line_ids.processed_received_quantity, 80)
        self.assertEqual(
            sorted(self._receiving_pickings(self.physical).move_ids.mapped('amazon_receiving_delta')),
            [30, 50],
        )

    def test_c_amazon_remains_80_creates_no_stock_move(self):
        self._apply(self.physical, {'RC-SKU-A': 80})
        picking_ids = self._receiving_pickings(self.physical).ids
        result = self._apply(self.physical, {'RC-SKU-A': 80})
        self.assertEqual(result['deltaReceived'], 0)
        self.assertEqual(self._receiving_pickings(self.physical).ids, picking_ids)

    def test_d_amazon_reaches_100_moves_final_delta_20(self):
        self._apply(self.physical, {'RC-SKU-A': 30})
        self._apply(self.physical, {'RC-SKU-A': 80})
        result = self._apply(self.physical, {'RC-SKU-A': 100})
        self.assertEqual(result['deltaReceived'], 20)
        self.assertEqual(self.physical.line_ids.processed_received_quantity, 100)
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id), 0,
        )
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_received_location_id), 100,
        )

    def test_d2_cumulative_10_25_30_moves_only_deltas_10_15_5(self):
        _shipment, physicals = self._create_plan(
            30,
            [[(self.amazon_product_a, 30, 'FN-RC-A')]],
        )
        physical = physicals[0]
        self._dispatch_physical_shipments(physical)

        deltas = [
            self._apply(physical, {'RC-SKU-A': cumulative})['deltaReceived']
            for cumulative in (10, 25, 30)
        ]

        self.assertEqual(deltas, [10, 15, 5])
        self.assertEqual(physical.amazon_received_quantity, 30)
        self.assertEqual(physical.processed_received_quantity, 30)
        self.assertCountEqual(
            self._receiving_pickings(physical).move_ids.mapped('amazon_receiving_delta'),
            [10, 15, 5],
        )

    def test_e_over_receipt_105_creates_no_move_and_discrepancy(self):
        result = self._apply(self.physical, {'RC-SKU-A': 105})
        self.assertEqual(result['deltaReceived'], 0)
        self.assertFalse(self._receiving_pickings(self.physical))
        self.assertEqual(self.physical.line_ids.amazon_received_quantity, 105)
        self.assertEqual(self.physical.line_ids.processed_received_quantity, 0)
        discrepancy = self.physical.receiving_discrepancy_ids.filtered(
            lambda item: item.discrepancy_type == 'received_overage'
        )
        self.assertEqual(discrepancy.quantity, 5)
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id), 100,
        )

    def test_f_received_quantity_decrease_never_reverses_or_moves(self):
        self._apply(self.physical, {'RC-SKU-A': 80})
        picking_ids = self._receiving_pickings(self.physical).ids
        transit_before = self._quantity_at(
            self.product_a, self.instance.fba_transit_location_id,
        )
        result = self._apply(self.physical, {'RC-SKU-A': 78})
        self.assertEqual(result['deltaReceived'], 0)
        self.assertEqual(self.physical.line_ids.amazon_received_quantity, 78)
        self.assertEqual(self.physical.line_ids.processed_received_quantity, 80)
        self.assertEqual(self._receiving_pickings(self.physical).ids, picking_ids)
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id),
            transit_before,
        )
        discrepancy = self.physical.receiving_discrepancy_ids.filtered(
            lambda item: item.discrepancy_type == 'received_quantity_decrease'
        )
        self.assertEqual(discrepancy.quantity, 2)

    def test_g_two_skus_receive_independent_partial_quantities(self):
        _shipment, physical = self._create_plan(2, [[
            (self.amazon_product_a, 20, 'FN-RC-A'),
            (self.amazon_product_b, 10, 'FN-RC-B'),
        ]])
        physical = physical[0]
        self._dispatch_physical_shipments(physical)
        self._apply(physical, {'RC-SKU-A': 15, 'RC-SKU-B': 10})
        lines = {line.msku: line for line in physical.line_ids}
        self.assertEqual(lines['RC-SKU-A'].processed_received_quantity, 15)
        self.assertEqual(lines['RC-SKU-A'].remaining_in_transit_quantity, 5)
        self.assertEqual(lines['RC-SKU-B'].processed_received_quantity, 10)
        self.assertEqual(lines['RC-SKU-B'].remaining_in_transit_quantity, 0)

    def test_h_same_sku_two_physical_shipments_remains_traceable(self):
        _shipment, physical = self._create_plan(3, [
            [(self.amazon_product_a, 10, 'FN-RC-A')],
            [(self.amazon_product_a, 20, 'FN-RC-A')],
        ])
        self._dispatch_physical_shipments(physical)
        self._apply(physical[0], {'RC-SKU-A': 10})
        self._apply(physical[1], {'RC-SKU-A': 5})
        self.assertEqual(physical[0].processed_received_quantity, 10)
        self.assertEqual(physical[1].processed_received_quantity, 5)
        pickings = self._receiving_pickings(physical[0]) | self._receiving_pickings(physical[1])
        self.assertEqual(len(pickings), 2)
        self.assertEqual(set(pickings.mapped('amazon_fba_physical_shipment_id')), set(physical))

    def test_i_unmapped_amazon_sku_creates_discrepancy_and_no_move(self):
        unknown = {
            'ShipmentId': self.physical.shipment_confirmation_id,
            'SellerSKU': 'UNKNOWN-AMAZON-SKU',
            'FulfillmentNetworkSKU': 'UNKNOWN-FNSKU',
            'QuantityShipped': 7,
            'QuantityReceived': 4,
        }
        response = self._items_response(self.physical, {}, extra_items=[unknown])
        # Remove the expected item to model an Amazon response containing only an unknown SKU.
        response['payload']['ItemData'] = [unknown]
        self.physical._apply_receiving_snapshot(
            self._status_response(self.physical), response,
        )
        self.assertFalse(self._receiving_pickings(self.physical))
        discrepancy = self.physical.receiving_discrepancy_ids.filtered(
            lambda item: item.discrepancy_type == 'unmapped_amazon_sku'
        )
        self.assertEqual(discrepancy.sku, 'UNKNOWN-AMAZON-SKU')
        self.assertEqual(discrepancy.amazon_quantity, 4)

    def test_j_http_429_retries_without_stock_change(self):
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '120'
        error = requests.exceptions.HTTPError('rate limited', response=response)
        transit_before = self._quantity_at(
            self.product_a, self.instance.fba_transit_location_id,
        )
        job, created = self.physical._enqueue_receiving_job()
        self.assertTrue(created)
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(AmazonAPI, 'get_shipment', autospec=True, side_effect=error),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'in_progress')
        self.assertEqual(job.retry_count, 1)
        self.assertTrue(job.next_run_at)
        self.assertEqual(self.physical.receiving_error_code, 'HTTP_429')
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id),
            transit_before,
        )

    def test_k_network_timeout_retries_without_stock_change(self):
        transit_before = self._quantity_at(
            self.product_a, self.instance.fba_transit_location_id,
        )
        job, _created = self.physical._enqueue_receiving_job()
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='token'),
            patch.object(
                AmazonAPI, 'get_shipment', autospec=True,
                side_effect=requests.exceptions.Timeout('timeout'),
            ),
        ):
            job._process_operation()
        self.assertEqual(job.state, 'in_progress')
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(self.physical.receiving_error_code, 'NETWORK_ERROR')
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id),
            transit_before,
        )

    def test_l_repeated_cron_enqueues_only_one_active_job(self):
        Physical = self.env['amazon.fba.physical.shipment']
        self.assertEqual(Physical.cron_enqueue_receiving_sync(), 1)
        self.assertEqual(Physical.cron_enqueue_receiving_sync(), 0)
        jobs = self.physical.inbound_shipment_id.operation_job_ids.filtered(
            lambda item: item.operation_type == 'sync_receiving'
            and item.physical_shipment_id == self.physical
            and item.state in ('pending', 'in_progress')
        )
        self.assertEqual(len(jobs), 1)

    def test_m_receiving_job_enqueue_is_concurrency_safe_and_idempotent(self):
        first, first_created = self.physical._enqueue_receiving_job()
        second, second_created = self.physical._enqueue_receiving_job()
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first, second)

    def test_n_closed_shipment_with_mismatch_keeps_transit_shortage(self):
        self._apply(self.physical, {'RC-SKU-A': 98}, status='CLOSED')
        self.assertEqual(self.physical.status, 'CLOSED')
        self.assertEqual(self.physical.receiving_state, 'discrepancy')
        self.assertEqual(self.physical.line_ids.processed_received_quantity, 98)
        self.assertEqual(
            self._quantity_at(self.product_a, self.instance.fba_transit_location_id), 2,
        )
        discrepancy = self.physical.receiving_discrepancy_ids.filtered(
            lambda item: item.discrepancy_type == 'closed_shortage'
        )
        self.assertEqual(discrepancy.quantity, 2)

    def test_o_receiving_code_has_no_direct_stock_quant_write(self):
        source = Path(__file__).parents[1] / 'models' / 'amazon_inbound_receiving.py'
        code = source.read_text(encoding='utf-8')
        self.assertNotIn("env['stock.quant']", code)
        self.assertNotIn('stock.quant', code)
        self.assertNotIn('quant.quantity', code)

    def test_p_api_wrapper_uses_supported_shipment_item_operation(self):
        response = requests.Response()
        response.status_code = 200
        response.headers['x-amzn-RequestId'] = 'receiving-wrapper-request'
        response._content = json.dumps({
            'payload': {
                'ItemData': [{
                    'ShipmentId': self.physical.shipment_confirmation_id,
                    'SellerSKU': 'RC-SKU-A',
                    'QuantityShipped': 100,
                    'QuantityReceived': 30,
                }],
            },
        }).encode()
        with patch.object(AmazonAPI, '_amazon_request', return_value=response) as request:
            result = AmazonAPI().get_inbound_shipment_items_v0(
                self.instance, 'token', self.physical.shipment_confirmation_id,
            )
        self.assertEqual(result['payload']['ItemData'][0]['QuantityReceived'], 30)
        self.assertIn(
            '/fba/inbound/v0/shipments/%s/items' % self.physical.shipment_confirmation_id,
            request.call_args.args[3],
        )

    def test_q_terminal_status_gets_one_receiving_sync_then_stops_polling(self):
        Physical = self.env['amazon.fba.physical.shipment']
        self.physical.sudo().write({
            'status': 'CLOSED',
            'receiving_state': 'not_started',
            'receiving_sync_status': False,
        })
        self.assertEqual(Physical.cron_enqueue_receiving_sync(), 1)
        job = self.physical.inbound_shipment_id.operation_job_ids.filtered(
            lambda item: item.operation_type == 'sync_receiving'
            and item.physical_shipment_id == self.physical
            and item.state in ('pending', 'in_progress')
        )
        self.assertEqual(len(job), 1)
        job.sudo().write({'state': 'done'})
        self.physical.sudo().write({
            'receiving_state': 'discrepancy',
            'receiving_sync_status': 'success',
        })
        self.assertEqual(Physical.cron_enqueue_receiving_sync(), 0)
