import base64
import json
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install', 'amazon_final_e2e')
class TestAmazonFinalEndToEnd(TransactionCase):
    """Rollback-only FBA and settlement scenario; no live Amazon calls."""

    def setUp(self):
        super().setUp()
        self.currency = self.env['res.currency'].sudo().with_context(
            active_test=False,
        ).search([('name', '=', 'EGP')], limit=1)
        self.currency.active = True
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Final E2E Company',
            'currency_id': self.currency.id,
        })
        Warehouse = self.env['stock.warehouse'].sudo().with_company(self.company)
        self.customer_warehouse = Warehouse.create({
            'name': 'Amazon E2E Customer Warehouse',
            'code': 'E2ECW',
            'company_id': self.company.id,
        })
        self.fba_warehouse = Warehouse.create({
            'name': 'Amazon E2E FBA Warehouse',
            'code': 'E2EFA',
            'company_id': self.company.id,
        })
        self.ship_from = self.env['res.partner'].sudo().create({
            'name': 'Amazon E2E Dispatch',
            'street': '1 Test Street',
            'city': 'Cairo',
            'zip': '11511',
            'phone': '+201000000000',
            'email': 'warehouse@example.test',
            'country_id': self.env.ref('base.eg').id,
            'company_id': self.company.id,
        })
        self.accounts = self._create_accounts()
        self.settlement_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'Amazon E2E Settlements',
            'code': 'E2EST',
            'type': 'general',
            'company_id': self.company.id,
        })
        self.bank_journal = self.env['account.journal'].sudo().with_company(
            self.company,
        ).create({
            'name': 'Amazon E2E Bank',
            'code': 'E2EBK',
            'type': 'bank',
            'company_id': self.company.id,
            'default_account_id': self.accounts['bank'].id,
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Egypt Final E2E',
            'company_id': self.company.id,
            'seller_id': 'E2E-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-e2e-refresh-token',
            'client_id': 'mock-e2e-client-id',
            'client_secret': 'mock-e2e-client-secret',
            'fba_warehouse_id': self.fba_warehouse.id,
            'fba_source_location_id': self.customer_warehouse.lot_stock_id.id,
            'fba_ship_from_partner_id': self.ship_from.id,
            'fba_removal_return_partner_id': self.ship_from.id,
            'settlement_journal_id': self.settlement_journal.id,
            'amazon_payout_bank_journal_id': self.bank_journal.id,
            'amazon_clearing_account_id': self.accounts['clearing'].id,
            'amazon_sales_account_id': self.accounts['sale'].id,
            'amazon_refund_account_id': self.accounts['refund'].id,
            'amazon_fee_account_id': self.accounts['fee'].id,
            'amazon_fba_fee_account_id': self.accounts['fee'].id,
            'amazon_reimbursement_account_id': self.accounts['reimbursement'].id,
            'amazon_adjustment_account_id': self.accounts['adjustment'].id,
            'amazon_suspense_account_id': self.accounts['suspense'].id,
        })
        self.instance.action_create_fba_stock_structure()
        first_location_ids = self._location_ids()
        self.instance.action_create_fba_stock_structure()
        self.assertEqual(self._location_ids(), first_location_ids)

        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'TEST-AMAZON-E2E',
            'default_code': 'TEST-AMAZON-E2E',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.fnsku = 'X00E2ETEST1'
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'TEST-AMAZON-E2E',
            'instance_id': self.instance.id,
            'sku': 'TEST-AMAZON-E2E',
            'asin': 'B0E2ETEST01',
            'fulfillment_channel': 'AFN',
            'odoo_product_id': self.product.id,
        })

    def _create_accounts(self):
        specs = {
            'clearing': ('790001', 'Amazon E2E Clearing', 'asset_current', True),
            'sale': ('790002', 'Amazon E2E Sales', 'income', False),
            'refund': ('790003', 'Amazon E2E Refunds', 'income', False),
            'fee': ('790004', 'Amazon E2E Fees', 'expense', False),
            'reimbursement': ('790005', 'Amazon E2E Reimbursements', 'income_other', False),
            'adjustment': ('790006', 'Amazon E2E Adjustments', 'expense_other', False),
            'suspense': ('790007', 'Amazon E2E Suspense', 'asset_current', False),
            'bank': ('790008', 'Amazon E2E Bank', 'asset_cash', False),
        }
        accounts = {}
        for key, (code, name, account_type, reconcile) in specs.items():
            accounts[key] = self.env['account.account'].sudo().with_company(
                self.company,
            ).create({
                'code': code,
                'name': name,
                'account_type': account_type,
                'reconcile': reconcile,
                'company_ids': [Command.set([self.company.id])],
            })
        return accounts

    def _location_ids(self):
        return tuple(self.instance[field].id for field in (
            'fba_transit_location_id',
            'fba_received_location_id',
            'fba_sellable_location_id',
            'fba_reserved_location_id',
            'fba_unsellable_location_id',
            'fba_removal_transit_location_id',
        ))

    def _quantity(self, location):
        self.product.invalidate_recordset()
        return self.product.sudo().with_company(self.company).with_context(
            location=location.id,
        ).qty_available

    def _seed_customer_stock(self, quantity):
        supplier = self.env.ref('stock.stock_location_suppliers')
        destination = self.instance.fba_source_location_id
        picking = self.env['stock.picking'].sudo().with_company(self.company).create({
            'picking_type_id': self.customer_warehouse.in_type_id.id,
            'location_id': supplier.id,
            'location_dest_id': destination.id,
            'company_id': self.company.id,
            'origin': 'AMAZON FINAL E2E OPENING STOCK',
            'move_ids': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': quantity,
                'product_uom': self.product.uom_id.id,
                'location_id': supplier.id,
                'location_dest_id': destination.id,
                'company_id': self.company.id,
            })],
        })
        picking.with_context(picking_ids_not_to_backorder=picking.ids).button_validate()
        self.assertEqual(picking.state, 'done')

    @staticmethod
    def _inventory_response(sellable):
        summary = {
            'asin': 'B0E2ETEST01',
            'fnSku': 'X00E2ETEST1',
            'sellerSku': 'TEST-AMAZON-E2E',
            'condition': 'NewItem',
            'lastUpdatedTime': '2026-08-12T12:00:00Z',
            'totalQuantity': sellable,
            'inventoryDetails': {
                'fulfillableQuantity': sellable,
                'inboundWorkingQuantity': 0,
                'inboundShippedQuantity': 0,
                'inboundReceivingQuantity': 0,
                'reservedQuantity': {'totalReservedQuantity': 0},
                'unfulfillableQuantity': {'totalUnfulfillableQuantity': 0},
            },
        }
        page = {'payload': {'inventorySummaries': [summary]}}
        return {
            'payload': page['payload'],
            '_amazon_request_ids': ['e2e-inventory-request'],
            '_pages': [page],
            '_snapshot_complete': True,
            '_page_count': 1,
        }

    def _apply_receiving(self, physical, cumulative):
        status = {
            'shipmentId': physical.amazon_shipment_id,
            'shipmentConfirmationId': physical.shipment_confirmation_id,
            'status': 'RECEIVING' if cumulative < 30 else 'CLOSED',
            '_amazon_request_id': 'e2e-receiving-status',
        }
        item = {
            'ShipmentId': physical.shipment_confirmation_id,
            'SellerSKU': self.amazon_product.sku,
            'FulfillmentNetworkSKU': self.fnsku,
            'QuantityShipped': 30,
            'QuantityReceived': cumulative,
        }
        return physical._apply_receiving_snapshot(
            status,
            {
                'payload': {'ItemData': [item]},
                '_amazon_request_ids': ['e2e-receiving-items'],
                '_pages': [{'payload': {'ItemData': [item]}}],
            },
        )

    def _create_settlement(self):
        components = [
            ('sale', 1000.0),
            ('amazon_fee', -100.0),
            ('refund', -200.0),
            ('reimbursement', 50.0),
            ('adjustment', -25.0),
        ]
        settlement = self.env['amazon.settlement.report'].sudo().create({
            'instance_id': self.instance.id,
            'settlement_id': 'E2E-SETTLEMENT-001',
            'settlement_start_date': fields.Datetime.from_string('2026-08-01 00:00:00'),
            'settlement_end_date': fields.Datetime.from_string('2026-08-12 23:59:59'),
            'deposit_date': fields.Datetime.from_string('2026-08-13 00:00:00'),
            'currency_id': self.currency.id,
            'currency_code': 'EGP',
            'reported_net_amount': 725.0,
            'state': 'imported',
            'reconciliation_state': 'pending',
        })
        for sequence, (category, amount) in enumerate(components, start=1):
            self.env['amazon.settlement.report.line'].sudo().create({
                'report_id': settlement.id,
                'line_key': 'E2E-SETTLEMENT-LINE-%s' % sequence,
                'normalized_category': category,
                'amount': amount,
                'currency_id': self.currency.id,
                'currency_code': 'EGP',
                'amount_description': category,
                'transaction_type': 'Order' if category in ('sale', 'refund') else 'Adjustment',
                'amazon_transaction_type_raw': category,
                'order_link_state': 'not_applicable',
            })
        settlement._recompute_reconciliation()
        return settlement

    def test_complete_controlled_fba_and_financial_chain(self):
        self._seed_customer_stock(100)
        source = self.instance.fba_source_location_id
        transit = self.instance.fba_transit_location_id
        received = self.instance.fba_received_location_id
        sellable = self.instance.fba_sellable_location_id
        reserved = self.instance.fba_reserved_location_id
        unsellable = self.instance.fba_unsellable_location_id
        removal_transit = self.instance.fba_removal_transit_location_id
        self.assertEqual(self._quantity(source), 100)

        inbound = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'TEST-AMAZON-E2E',
            'shipment_name': 'TEST-AMAZON-E2E',
            'instance_id': self.instance.id,
            'state': 'draft',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'odoo_product_id': self.product.id,
                'sku': self.amazon_product.sku,
                'fnsku': self.fnsku,
                'planned_quantity': 30,
                'prep_owner': 'SELLER',
                'label_owner': 'SELLER',
            })],
        })
        plan_payload = inbound._prepare_create_inbound_plan_payload()
        self.assertEqual(plan_payload['destinationMarketplaces'], ['ARBP9OOSHTCHU'])
        self.assertEqual(plan_payload['items'][0]['quantity'], 30)
        inbound.write({
            'inbound_plan_id': 'wf00000001-1234-abcd-5678-1234abcd5678',
            'create_operation_status': 'success',
            'packing_confirmation_status': 'success',
            'packing_information_status': 'success',
            'placement_confirmation_status': 'success',
            'state': 'placement_confirmed',
        })
        packing = self.env['amazon.fba.packing.option'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': inbound.id,
            'amazon_packing_option_id': 'pk00000001-1234-abcd-5678-1234abcd5678',
            'option_name': 'E2E Packing',
            'status': 'ACCEPTED',
            'selected': True,
            'amazon_packing_group_ids': json.dumps(['pg-e2e-1']),
        })
        self.env['amazon.fba.box'].sudo().create({
            'packing_option_id': packing.id,
            'amazon_box_id': 'box-e2e-1',
            'amazon_packing_group_id': 'pg-e2e-1',
            'length': 30,
            'width': 20,
            'height': 10,
            'weight': 5,
            'weight_unit': 'KG',
            'dimension_unit': 'CM',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'quantity': 30,
            })],
        })
        amazon_shipment_id = 'sh00000001-1234-abcd-5678-1234abcd5678'
        placement = self.env['amazon.fba.placement.option'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'amazon_placement_option_id': 'pl00000001-1234-abcd-5678-1234abcd5678',
            'status': 'ACCEPTED',
            'amazon_shipment_ids': json.dumps([amazon_shipment_id]),
            'selected': True,
        })
        physical = self.env['amazon.fba.physical.shipment'].sudo().create({
            'inbound_shipment_id': inbound.id,
            'placement_option_id': placement.id,
            'amazon_shipment_id': amazon_shipment_id,
            'shipment_confirmation_id': 'FBA19E2E001',
            'status': 'SHIPPED',
            'destination_fc': 'CAI1',
            'line_ids': [Command.create({
                'amazon_product_id': self.amazon_product.id,
                'msku': self.amazon_product.sku,
                'asin': self.amazon_product.asin,
                'fnsku': self.fnsku,
                'quantity': 30,
            })],
        })
        self.env['amazon.fba.shipment.box'].sudo().create({
            'instance_id': self.instance.id,
            'inbound_shipment_id': inbound.id,
            'physical_shipment_id': physical.id,
            'amazon_box_id': 'box-e2e-1',
            'quantity': 1,
        })
        self.assertNotEqual(inbound.inbound_plan_id, physical.amazon_shipment_id)

        dispatch, created = physical._create_dispatch_picking()
        self.assertTrue(created)

        option = physical._sync_transportation_options([{
            'shipmentId': amazon_shipment_id,
            'transportationOptionId': 'to-e2e-1',
            'shippingMode': 'GROUND_SMALL_PARCEL',
            'shippingSolution': 'USE_YOUR_OWN_CARRIER',
            'carrier': {'name': 'E2E Carrier', 'alphaCode': 'E2E'},
            'quote': {'cost': {'amount': 125, 'code': 'EGP'}},
        }])
        label_attachment = self.env['ir.attachment'].sudo().create({
            'name': 'e2e-box-labels.pdf',
            'type': 'binary',
            'datas': base64.b64encode(b'%PDF-1.4 final e2e labels'),
            'mimetype': 'application/pdf',
            'res_model': physical._name,
            'res_id': physical.id,
        })
        physical.write({
            'selected_transportation_option_id': option.id,
            'transportation_generation_status': 'success',
            'transportation_confirmation_status': 'success',
            'transportation_status': 'CONFIRMED',
            'carrier_type': 'non_partnered',
            'carrier_name': 'E2E Carrier',
            'shipping_mode': 'GROUND_SMALL_PARCEL',
            'tracking_number': 'E2E-TRACK-001',
            'tracking_status': 'success',
            'labels_status': 'success',
            'shipping_label_attachment_id': label_attachment.id,
            'product_labels_confirmed': True,
        })
        self.assertEqual(
            physical._prepare_transportation_confirmation_payload(),
            {'transportationSelections': [{
                'shipmentId': amazon_shipment_id,
                'transportationOptionId': 'to-e2e-1',
                'contactInformation': {
                    'email': self.ship_from.email,
                    'name': self.ship_from.name,
                    'phoneNumber': self.ship_from.phone,
                },
            }]},
        )
        tracking_payload = physical._prepare_physical_tracking_details_payload()
        self.assertEqual(
            tracking_payload['trackingDetails']['spdTrackingDetail']['spdTrackingItems'],
            [{'boxId': 'box-e2e-1', 'trackingId': 'E2E-TRACK-001'}],
        )

        dispatch.with_context(picking_ids_not_to_backorder=dispatch.ids).button_validate()
        self.assertEqual(dispatch.state, 'done')
        self.assertEqual(self._quantity(source), 70)
        self.assertEqual(self._quantity(transit), 30)
        self.assertEqual(self._quantity(sellable), 0)

        deltas = [
            self._apply_receiving(physical, cumulative)['deltaReceived']
            for cumulative in (10, 25, 30)
        ]
        self.assertEqual(deltas, [10, 15, 5])
        self.assertEqual(self._quantity(transit), 0)
        self.assertEqual(self._quantity(received), 30)
        self.assertEqual(sum(
            physical.receiving_picking_ids.move_ids.mapped('amazon_receiving_delta')
        ), 30)

        run = self.env['amazon.inventory.reconciliation.run'].sudo().create({
            'instance_id': self.instance.id,
            'trigger': 'manual',
        })
        with (
            patch.object(type(self.instance), '_get_access_token_or_raise', return_value='mock-token'),
            patch.object(
                AmazonAPI,
                'get_all_inventory_summaries',
                autospec=True,
                return_value=self._inventory_response(30),
            ),
        ):
            self.assertTrue(run._process_run(), run.last_error)
        reconciliation = run.reconciliation_ids.filtered(
            lambda line: line.sku == 'TEST-AMAZON-E2E'
        )
        self.assertEqual(reconciliation.status, 'mismatch')
        self.assertEqual(reconciliation.suggested_action, 'manual_review')
        reconciliation.write({
            'adjustment_action': 'received_to_sellable',
            'adjustment_quantity': 30,
            'adjustment_reason': 'Controlled E2E review of complete Amazon snapshot.',
            'large_adjustment_confirmed': True,
        })
        reconciliation.action_mark_adjustment_reviewed()
        reconciliation.action_apply_suggested_action()
        self.assertEqual(self._quantity(received), 0)
        self.assertEqual(self._quantity(sellable), 30)

        partner = self.env['res.partner'].sudo().create({
            'name': 'Amazon E2E Buyer',
            'company_id': self.company.id,
        })
        sale_order = self.env['sale.order'].sudo().with_company(self.company).create({
            'partner_id': partner.id,
            'company_id': self.company.id,
        })
        sale_line = self.env['sale.order.line'].sudo().create({
            'order_id': sale_order.id,
            'product_id': self.product.id,
            'product_uom_qty': 1,
        })
        amazon_order = self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': 'E2E-AMAZON-ORDER-001',
            'instance_id': self.instance.id,
            'sale_order_id': sale_order.id,
            'fulfillment_channel': 'AFN',
        })
        self.env['amazon.sale.order.line'].sudo().create({
            'order_id': amazon_order.id,
            'amazon_order_item_id': 'E2E-AMAZON-ITEM-001',
            'amazon_product_id': self.amazon_product.id,
            'odoo_product_id': self.product.id,
            'sku': self.amazon_product.sku,
            'asin': self.amazon_product.asin,
            'quantity': 1,
        })
        return_report = self.env['amazon.return.report'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'downloaded',
            'amazon_report_id': 'E2E-RETURN-REPORT',
        })
        stock_moves_before_return = self.env['stock.move'].sudo().search_count([])
        return_event = self.env['amazon.return.report.line'].sudo().import_row(
            return_report,
            {
                'return-date': '2026-08-12T10:00:00Z',
                'order-id': amazon_order.amazon_order_ref,
                'sku': self.amazon_product.sku,
                'asin': self.amazon_product.asin,
                'fnsku': self.fnsku,
                'product-name': self.product.name,
                'quantity': '1',
                'fulfillment-center-id': 'CAI1',
                'detailed-disposition': 'SELLABLE',
                'reason': 'CUSTOMER_RETURN',
                'status': 'Unit returned to inventory',
                'license-plate-number': 'E2E-LPN-001',
                'customer-comments': '',
            },
        )
        return_event._classify_and_apply()
        self.assertEqual(return_event.stock_action_state, 'audit_only')
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), stock_moves_before_return)
        self.assertEqual(return_event.linked_sale_order_line_id, sale_line)

        removal_order = self.env['amazon.removal.order'].sudo().import_detail_row(
            self.instance,
            {
                'request-date': '2026-08-12T10:00:00Z',
                'order-id': 'E2E-REMOVAL-001',
                'order-type': 'Return',
                'order-status': 'Processing',
                'last-updated-date': '2026-08-12T11:00:00Z',
                'sku': self.amazon_product.sku,
                'fnsku': self.fnsku,
                'disposition': 'Sellable',
                'requested-quantity': '1',
                'cancelled-quantity': '0',
                'disposed-quantity': '0',
                'shipped-quantity': '0',
                'in-process-quantity': '1',
            },
            'E2E-REMOVAL-DETAIL-REPORT',
        )
        removal_shipment = self.env['amazon.removal.shipment'].sudo().import_row(
            self.instance,
            {
                'request-date': '2026-08-12T10:00:00Z',
                'order-id': removal_order.removal_order_id,
                'shipment-date': '2026-08-12T12:00:00Z',
                'sku': self.amazon_product.sku,
                'fnsku': self.fnsku,
                'disposition': 'Sellable',
                'shipped-quantity': '1',
                'carrier': 'Amazon Logistics',
                'tracking-number': 'E2E-REMOVAL-TRACK-001',
                'removal-order-type': 'Return',
            },
            'E2E-REMOVAL-SHIPMENT-REPORT',
        )
        self.assertEqual(removal_shipment.stock_action_state, 'audit_only')
        removal_shipment.sudo().action_move_to_removal_transit()
        self.assertEqual(self._quantity(sellable), 29)
        self.assertEqual(self._quantity(removal_transit), 1)
        receipt_action = removal_shipment.sudo().action_create_receipt()
        receipt = self.env['stock.picking'].sudo().browse(receipt_action['res_id'])
        self.assertNotEqual(receipt.state, 'done')
        receipt.action_assign()
        receipt.with_context(picking_ids_not_to_backorder=receipt.ids).button_validate()
        self.assertEqual(receipt.state, 'done')
        self.assertEqual(self._quantity(source), 71)
        self.assertEqual(self._quantity(removal_transit), 0)

        account_moves_before_reimbursement = self.env['account.move'].sudo().search_count([])
        reimbursement = self.env['amazon.fba.reimbursement'].sudo().import_row(
            self.instance,
            {
                'approval-date': '2026-08-12T13:00:00Z',
                'reimbursement-id': 'E2E-REIMBURSEMENT-001',
                'case-id': 'E2E-CASE-001',
                'amazon-order-id': amazon_order.amazon_order_ref,
                'reason': 'Lost_Warehouse',
                'sku': self.amazon_product.sku,
                'fnsku': self.fnsku,
                'asin': self.amazon_product.asin,
                'product-name': self.product.name,
                'condition': 'SELLABLE',
                'currency-unit': 'EGP',
                'amount-per-unit': '50',
                'amount-total': '50',
                'quantity-reimbursed-cash': '1',
                'quantity-reimbursed-inventory': '0',
                'quantity-reimbursed-total': '1',
                'original-reimbursement-id': '',
                'original-reimbursement-type': '',
            },
            'E2E-REIMBURSEMENT-REPORT',
        )
        self.assertEqual(reimbursement.amount_total, 50)
        self.assertEqual(self.env['account.move'].sudo().search_count([]), account_moves_before_reimbursement)
        self.assertEqual(self._quantity(sellable), 29)

        settlement = self._create_settlement()
        self.assertEqual(settlement.reconciliation_state, 'matched')
        self.assertEqual(settlement.calculated_net_amount, 725)
        settlement.sudo().action_create_accounting_entry()
        move = settlement.account_move_id
        self.assertEqual(move.state, 'draft')
        self.assertEqual(sum(move.line_ids.mapped('debit')), sum(move.line_ids.mapped('credit')))
        clearing_line = move.line_ids.filtered(
            lambda line: line.account_id == self.accounts['clearing']
        )
        self.assertEqual(clearing_line.debit - clearing_line.credit, 725)
        move.action_post()

        payout = self.env['amazon.payout'].sudo().create({
            'instance_id': self.instance.id,
            'source': 'manual_confirmation',
            'source_reference': 'E2E-BANK-RECEIPT-001',
            'payout_date': fields.Date.from_string('2026-08-13'),
            'currency_id': self.currency.id,
            'actual_received_amount': 725,
            'bank_journal_id': self.bank_journal.id,
            'matching_state': 'manually_matched',
        })
        self.env['amazon.payout.allocation'].sudo().create({
            'payout_id': payout.id,
            'settlement_id': settlement.id,
            'expected_amount': 725,
            'allocated_amount': 725,
        })
        payout.sudo().action_create_draft_receipt()
        self.assertEqual(payout.receipt_move_id.state, 'draft')
        payout.receipt_move_id.action_post()
        payout.sudo().action_reconcile_clearing()
        settlement.invalidate_recordset()
        self.assertEqual(payout.state, 'paid')
        self.assertTrue(self.currency.is_zero(settlement.clearing_remaining_amount))
        self.assertEqual(self._quantity(source), 71)
        self.assertEqual(self._quantity(transit), 0)
        self.assertEqual(self._quantity(sellable), 29)
        self.assertEqual(self._quantity(reserved), 0)
        self.assertEqual(self._quantity(unsellable), 0)
        self.assertEqual(self._quantity(removal_transit), 0)
