import json
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI, REPORT_FBA_REIMBURSEMENTS


@tagged('post_install', '-at_install', 'amazon_fba_reimbursements')
class TestAmazonFBAReimbursements(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Reimbursement Test Company',
        })
        self.instance = self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Egypt Reimbursement Test',
            'company_id': self.company.id,
            'seller_id': 'REIMB-SELLER-EG',
            'marketplace_id': 'ARBP9OOSHTCHU',
            'region': 'eu',
            'refresh_token': 'mock-refresh',
            'client_id': 'mock-client',
            'client_secret': 'mock-secret',
            'adjustment_stock_policy': 'informational',
        })
        self.product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Reimbursement Product',
            'default_code': 'REIMB-SKU',
            'type': 'consu',
            'is_storable': True,
            'company_id': self.company.id,
        })
        self.amazon_product = self.env['amazon.product'].sudo().create({
            'name': 'Amazon Reimbursement Product',
            'instance_id': self.instance.id,
            'sku': 'REIMB-SKU',
            'asin': 'B0REIMB001',
            'odoo_product_id': self.product.id,
        })

    def _row(self, suffix, cash='0', inventory='0', total=None,
             amount='100', reason='Lost_Warehouse', **updates):
        if total is None:
            total = str(float(cash) + float(inventory))
        row = {
            'approval-date': '2026-08-02T10:00:00Z',
            'reimbursement-id': 'REIMB-%s' % suffix,
            'case-id': '',
            'amazon-order-id': '',
            'reason': reason,
            'sku': 'REIMB-SKU',
            'fnsku': 'REIMB-FNSKU',
            'asin': 'B0REIMB001',
            'product-name': 'Reimbursement Product',
            'condition': 'SELLABLE',
            'currency-unit': 'EGP',
            'amount-per-unit': '100',
            'amount-total': amount,
            'quantity-reimbursed-cash': cash,
            'quantity-reimbursed-inventory': inventory,
            'quantity-reimbursed-total': total,
            'original-reimbursement-id': '',
            'original-reimbursement-type': '',
        }
        row.update(updates)
        return row

    def _event(self, suffix, reason='Lost', quantity='-1', reference=None,
               date='2026-08-01T10:00:00Z'):
        return self.env['amazon.fba.inventory.adjustment'].import_row(self.instance, {
            'Date': date,
            'FNSKU': 'REIMB-FNSKU',
            'ASIN': 'B0REIMB001',
            'MSKU': 'REIMB-SKU',
            'EventType': 'Adjustments',
            'ReferenceID': reference or 'EVENT-%s' % suffix,
            'Quantity': quantity,
            'FulfillmentCenter': 'CAI1',
            'Disposition': 'SELLABLE',
            'Reason': reason,
            'Country': 'EG',
            'ReconciledQuantity': '0',
            'UnreconciledQuantity': quantity,
        }, 'LEDGER-%s' % suffix)

    def _import(self, row, report='REPORT-REIMB'):
        return self.env['amazon.fba.reimbursement'].import_row(
            self.instance, row, report,
        )

    def test_a_normal_cash_reimbursement_and_order_link(self):
        order = self.env['amazon.sale.order'].sudo().create({
            'instance_id': self.instance.id,
            'amazon_order_ref': 'ORDER-CASH',
        })
        reimbursement = self._import(self._row(
            'CASH', cash='2', total='2', amount='700',
            **{'amazon-order-id': order.amazon_order_ref},
        ))
        self.assertEqual(reimbursement.quantity_reimbursed_cash, 2)
        self.assertEqual(reimbursement.quantity_reimbursed_inventory, 0)
        self.assertEqual(reimbursement.quantity_reimbursed_total, 2)
        self.assertEqual(reimbursement.amount_total, 700)
        self.assertEqual(reimbursement.currency_code, 'EGP')
        self.assertEqual(reimbursement.amazon_order_record_id, order)
        self.assertEqual(reimbursement.order_link_state, 'linked')

    def test_b_inventory_reimbursement_is_distinct_from_cash(self):
        reimbursement = self._import(self._row(
            'INVENTORY', inventory='1', total='1', amount='0',
        ))
        self.assertEqual(reimbursement.quantity_reimbursed_cash, 0)
        self.assertEqual(reimbursement.quantity_reimbursed_inventory, 1)
        self.assertEqual(reimbursement.amount_total, 0)

    def test_c_cash_and_inventory_quantities_are_preserved(self):
        reimbursement = self._import(self._row(
            'BOTH', cash='2', inventory='1', total='3', amount='700',
        ))
        self.assertEqual(reimbursement.quantity_reimbursed_cash, 2)
        self.assertEqual(reimbursement.quantity_reimbursed_inventory, 1)
        self.assertEqual(reimbursement.quantity_reimbursed_total, 3)
        self.assertFalse(reimbursement.quantity_anomaly)

        event = self._event('QUANTITY-ANOMALY', quantity='-1')
        anomaly = self._import(self._row(
            'QUANTITY-ANOMALY', cash='1', inventory='1', total='1',
        ))
        anomaly._match_one()
        self.assertTrue(anomaly.quantity_anomaly)
        self.assertEqual(anomaly.matching_state, 'unmatched')
        self.assertFalse(anomaly.linked_inventory_event_id)
        anomaly.linked_inventory_event_id = event
        anomaly.action_confirm_manual_match()
        self.assertEqual(anomaly.matching_state, 'manually_matched')

    def test_d_idempotent_upsert_and_multi_item_reimbursement(self):
        row = self._row('MULTI', cash='1', total='1')
        first = self._import(row)
        changed = dict(row, **{'amount-total': '125'})
        second = self._import(changed)
        self.assertEqual(first, second)
        self.assertEqual(second.amount_total, 125)

        other_product = self.env['product.product'].sudo().with_company(self.company).create({
            'name': 'Other Reimbursement Product', 'default_code': 'REIMB-SKU-2',
            'type': 'consu', 'is_storable': True, 'company_id': self.company.id,
        })
        self.env['amazon.product'].sudo().create({
            'name': 'Other Amazon Product', 'instance_id': self.instance.id,
            'sku': 'REIMB-SKU-2', 'asin': 'B0REIMB002',
            'odoo_product_id': other_product.id,
        })
        third = self._import(self._row(
            'MULTI', cash='1', total='1',
            **{'sku': 'REIMB-SKU-2', 'fnsku': 'REIMB-FNSKU-2',
               'asin': 'B0REIMB002', 'product-name': 'Other Reimbursement Product'},
        ))
        self.assertNotEqual(first, third)
        self.assertEqual(self.env['amazon.fba.reimbursement'].search_count([
            ('instance_id', '=', self.instance.id), ('reimbursement_id', '=', 'REIMB-MULTI'),
        ]), 2)

    def test_e_lost_event_matches_without_creating_another_loss(self):
        loss = self._event('LOST', quantity='-2', reference='CASE-LOST')
        reimbursement = self._import(self._row(
            'LOST', cash='2', total='2', amount='700',
            **{'case-id': 'CASE-LOST'},
        ))
        reimbursement._match_one()
        self.assertEqual(reimbursement.linked_inventory_event_id, loss)
        self.assertEqual(reimbursement.matching_state, 'matched')
        self.assertIn('ReferenceID', reimbursement.matching_explanation)

    def test_f_damaged_event_matches_only_damaged_reason(self):
        damaged = self._event('DAMAGED', reason='Warehouse Damaged', quantity='-1')
        reimbursement = self._import(self._row(
            'DAMAGED', cash='1', total='1', reason='Damaged_Warehouse',
        ))
        reimbursement._match_one()
        self.assertEqual(reimbursement.linked_inventory_event_id, damaged)
        self.assertEqual(reimbursement.matching_state, 'matched')

        disposed = self._event('DISPOSED', reason='Disposed', quantity='-1')
        disposal_reimbursement = self._import(self._row(
            'DISPOSED', cash='1', total='1', reason='Disposed',
        ))
        disposal_reimbursement._match_one()
        self.assertEqual(disposal_reimbursement.linked_inventory_event_id, disposed)
        self.assertEqual(disposal_reimbursement.matching_state, 'matched')

    def test_g_lost_event_without_reimbursement_remains_operational_only(self):
        loss = self._event('NO-REIMB', quantity='-1')
        self.assertFalse(loss.linked_reimbursement_id)
        self.assertEqual(loss.state, 'imported')

    def test_h_reimbursement_without_inventory_event_is_retained_unmatched(self):
        reimbursement = self._import(self._row('NO-EVENT', cash='1', total='1'))
        reimbursement._match_one()
        self.assertTrue(reimbursement.exists())
        self.assertEqual(reimbursement.matching_state, 'unmatched')
        self.assertFalse(reimbursement.linked_inventory_event_id)

    def test_i_found_event_matches_reimbursement_reversal_history(self):
        loss = self._event('REVERSAL-LOSS', quantity='-2')
        original = self._import(self._row(
            'ORIGINAL', cash='2', total='2', amount='200',
        ))
        original._match_one()
        self.assertEqual(original.linked_inventory_event_id, loss)
        found = self._event(
            'REVERSAL-FOUND', reason='Found', quantity='2',
            date='2026-08-03T10:00:00Z',
        )
        reversal = self._import(self._row(
            'REVERSAL', cash='-2', total='-2', amount='-200', reason='Reversal',
            **{'approval-date': '2026-08-04T10:00:00Z',
               'original-reimbursement-id': original.reimbursement_id,
               'original-reimbursement-type': 'REVERSAL'},
        ))
        reversal._match_one()
        self.assertEqual(found.reversal_of_adjustment_id, loss)
        self.assertEqual(reversal.original_reimbursement_record_id, original)
        self.assertEqual(reversal.linked_inventory_event_id, found)
        self.assertEqual(reversal.amount_total, -200)

    def test_j_original_reimbursement_relationship_is_separate_from_event_match(self):
        original = self._import(self._row('REL-ORIGINAL', cash='1', total='1'))
        reversal = self._import(self._row(
            'REL-REVERSAL', cash='-1', total='-1', amount='-100', reason='Reversal',
            **{'original-reimbursement-id': original.reimbursement_id,
               'original-reimbursement-type': 'REVERSAL'},
        ))
        self.assertEqual(reversal.original_reimbursement_record_id, original)
        self.assertEqual(reversal.original_link_state, 'linked')
        self.assertEqual(reversal.matching_state, 'unmatched')

    def test_k_unmapped_sku_and_fnsku_mapping_evidence(self):
        unmapped = self._import(self._row(
            'UNMAPPED', cash='1', total='1',
            **{'sku': 'UNKNOWN', 'fnsku': 'UNKNOWN-FNSKU', 'asin': 'UNKNOWN-ASIN'},
        ))
        self.assertEqual(unmapped.product_mapping_state, 'unmapped')
        self.assertIn('UNMAPPED PRODUCT', unmapped.review_note)

        self._event('FNSKU-BRIDGE', quantity='-1')
        mapped = self._import(self._row(
            'FNSKU-BRIDGE', cash='1', total='1',
            **{'sku': '', 'asin': '', 'fnsku': 'REIMB-FNSKU'},
        ))
        self.assertEqual(mapped.amazon_product_id, self.amazon_product)
        self.assertEqual(mapped.odoo_product_id, self.product)

    def test_l_order_not_found_is_retained_for_review(self):
        reimbursement = self._import(self._row(
            'MISSING-ORDER', cash='1', total='1',
            **{'amazon-order-id': 'ORDER-DOES-NOT-EXIST'},
        ))
        self.assertEqual(reimbursement.order_link_state, 'order_not_found')
        self.assertIn('ORDER NOT FOUND', reimbursement.review_note)

    def test_m_ambiguous_match_is_never_auto_linked(self):
        self._event('AMBIGUOUS-A', quantity='-1', date='2026-08-01T09:00:00Z')
        self._event('AMBIGUOUS-B', quantity='-1', date='2026-08-01T11:00:00Z')
        reimbursement = self._import(self._row('AMBIGUOUS', cash='1', total='1'))
        reimbursement._match_one()
        self.assertEqual(reimbursement.matching_state, 'ambiguous')
        self.assertFalse(reimbursement.linked_inventory_event_id)

    def test_n_negative_reversal_values_are_not_resigned(self):
        reimbursement = self._import(self._row(
            'NEGATIVE', cash='-1', inventory='0', total='-1', amount='-325',
            reason='Reversal',
            **{'amount-per-unit': '-325', 'original-reimbursement-id': 'MISSING-ORIGINAL',
               'original-reimbursement-type': 'REVERSAL'},
        ))
        self.assertEqual(reimbursement.quantity_reimbursed_cash, -1)
        self.assertEqual(reimbursement.quantity_reimbursed_total, -1)
        self.assertEqual(reimbursement.amount_per_unit, -325)
        self.assertEqual(reimbursement.amount_total, -325)
        self.assertEqual(reimbursement.original_link_state, 'not_found')

    def test_o_async_report_flow_row_isolation_matching_queue_and_retry(self):
        headers = list(self._row('ASYNC').keys()) + ['future-column']
        valid = self._row('ASYNC', cash='1', total='1')
        malformed = dict(valid, **{'reimbursement-id': ''})
        raw = '\ufeff' + '\t'.join(headers) + '\n' + '\n'.join(
            '\t'.join(str(row.get(header, '')) for header in headers)
            for row in (dict(valid, **{'future-column': 'future-value'}), malformed)
        )
        job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id,
            'operation_type': 'reimbursements',
            'date_from': fields.Date.from_string('2026-08-01'),
            'date_to': fields.Date.from_string('2026-08-03'),
        })
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'create_report', return_value={'reportId': 'REPORT-ASYNC'}) as create,
        ):
            job._run_one_turn()
        self.assertEqual(create.call_args.args[2], REPORT_FBA_REIMBURSEMENTS)
        self.assertEqual(job.stage, 'poll')
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_report', return_value={
                'processingStatus': 'DONE', 'reportDocumentId': 'DOC-ASYNC',
            }),
        ):
            job._run_one_turn()
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_report_document', return_value={
                'url': 'https://example.test/report',
            }),
            patch.object(AmazonAPI, 'download_report_document', return_value=raw),
        ):
            job._run_one_turn()
        job._run_one_turn()
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_processed, 1)
        self.assertEqual(job.total_failed, 1)
        imported = self.env['amazon.fba.reimbursement'].search([
            ('reimbursement_id', '=', 'REIMB-ASYNC'),
        ])
        self.assertEqual(len(imported), 1)
        self.assertIn('future-column', json.loads(imported.raw_report_row))
        self.assertTrue(self.env['amazon.phase7.job'].search([
            ('instance_id', '=', self.instance.id),
            ('operation_type', '=', 'reimbursement_matching'),
        ], limit=1))

        retry_job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'reimbursements',
        })
        retry_job._fail_or_retry(Exception('HTTP 429 throttled'))
        self.assertEqual(retry_job.state, 'pending')
        self.assertGreater(retry_job.next_run_at, fields.Datetime.now())

        role_job = self.env['amazon.phase7.job'].sudo().create({
            'instance_id': self.instance.id, 'operation_type': 'reimbursements',
        })
        role_job._fail_or_retry(Exception('HTTP 403 Forbidden'))
        self.assertEqual(role_job.state, 'failed')
        self.assertEqual(role_job.last_error_code, 'REPORT_ROLE_MISSING')
        self.assertIn('Pricing or Amazon Fulfillment', role_job.last_error_message)

    def test_p_reimbursement_import_and_match_have_no_stock_side_effect(self):
        loss = self._event('NO-STOCK', quantity='-1')
        before_moves = self.env['stock.move'].sudo().search_count([])
        before_pickings = self.env['stock.picking'].sudo().search_count([])
        reimbursement = self._import(self._row('NO-STOCK', cash='1', total='1'))
        reimbursement._match_one()
        self.assertEqual(reimbursement.linked_inventory_event_id, loss)
        self.assertEqual(self.env['stock.move'].sudo().search_count([]), before_moves)
        self.assertEqual(self.env['stock.picking'].sudo().search_count([]), before_pickings)

    def test_q_reimbursement_import_and_match_create_no_account_move(self):
        loss = self._event('NO-ACCOUNT', quantity='-1')
        before = self.env['account.move'].sudo().search_count([])
        reimbursement = self._import(self._row('NO-ACCOUNT', cash='1', total='1'))
        reimbursement._match_one()
        self.assertEqual(reimbursement.linked_inventory_event_id, loss)
        self.assertEqual(self.env['account.move'].sudo().search_count([]), before)
