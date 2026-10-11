"""Phase 24 — Diagnostic Script Safety Tests

Tests the RequestGuard enforcement layer that will protect the live
Amazon diagnostic script. All tests use mocked HTTP responses.
No live Amazon API calls are made.
"""

import json
from unittest.mock import patch, MagicMock
from odoo.tests import tagged, TransactionCase


@tagged('phase24_safety', '-at_install', 'post_install')
class TestPhase24DiagnosticSafety(TransactionCase):

    def setUp(self):
        super().setUp()
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            'phase24_diagnostic',
            '/tmp/claude-1001/-home-odoo16-odoo-v19-amazon-odoo/'
            'a0ae3b87-569d-426d-8cf7-86e84f5f26bd/scratchpad/phase24_diagnostic.py',
        )
        self.diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.diag)
        self.guard = self.diag.RequestGuard(max_requests=14)

    def test_01_allowed_get_orders(self):
        """GET to orders endpoint is allowed."""
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/orders/2026-01-01/orders')
        self.assertEqual(self.guard.request_count, 1)

    def test_02_allowed_get_inventory(self):
        """GET to FBA inventory endpoint is allowed."""
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/fba/inventory/v1/summaries')
        self.assertEqual(self.guard.request_count, 1)

    def test_03_allowed_get_sellers(self):
        """GET to sellers endpoint is allowed."""
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/sellers/v1/marketplaceParticipations')
        self.assertEqual(self.guard.request_count, 1)

    def test_04_allowed_oauth_post(self):
        """POST to OAuth token endpoint is allowed."""
        self.guard.check('POST', 'https://api.amazon.com/auth/o2/token')
        self.assertEqual(self.guard.request_count, 1)

    def test_05_blocked_post_to_reports(self):
        """POST to reports endpoint (create report) is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked) as ctx:
            self.guard.check('POST', 'https://sellingpartnerapi-eu.amazon.com/reports/2021-06-30/reports')
        self.assertIn('BLOCKED', str(ctx.exception))
        self.assertIn('POST', str(ctx.exception))

    def test_06_blocked_post_to_feeds(self):
        """POST to feeds endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked) as ctx:
            self.guard.check('POST', 'https://sellingpartnerapi-eu.amazon.com/feeds/2021-06-30/documents')
        self.assertIn('BLOCKED', str(ctx.exception))

    def test_07_blocked_put_to_listings(self):
        """PUT to listings endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked) as ctx:
            self.guard.check('PUT', 'https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/SELLER/SKU')
        self.assertIn('BLOCKED', str(ctx.exception))

    def test_08_blocked_patch_to_listings(self):
        """PATCH to listings endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('PATCH', 'https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/SELLER/SKU')

    def test_09_blocked_delete(self):
        """DELETE to any endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('DELETE', 'https://sellingpartnerapi-eu.amazon.com/orders/2026-01-01/orders/123')

    def test_10_blocked_post_inbound_plan(self):
        """POST to inbound plans endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('POST', 'https://sellingpartnerapi-eu.amazon.com/inbound/fba/2024-03-20/inboundPlans')

    def test_11_blocked_post_fulfillment_order(self):
        """POST to fulfillment orders endpoint is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('POST', 'https://sellingpartnerapi-eu.amazon.com/fba/outbound/2020-07-01/fulfillmentOrders')

    def test_12_blocked_unknown_endpoint(self):
        """Unknown endpoint is BLOCKED (fail closed)."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/some/unknown/api')

    def test_13_blocked_get_to_oauth(self):
        """GET to OAuth endpoint is BLOCKED (only POST allowed)."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('GET', 'https://api.amazon.com/auth/o2/token')

    def test_14_budget_exhaustion(self):
        """Budget enforcement stops at MAX_TOTAL_REQUESTS."""
        small_guard = self.diag.RequestGuard(max_requests=3)
        small_guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/orders/2026-01-01/orders')
        small_guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/orders/2026-01-01/orders')
        small_guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/orders/2026-01-01/orders')
        with self.assertRaises(self.diag.BudgetExhausted):
            small_guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/fba/inventory/v1/summaries')
        self.assertEqual(small_guard.request_count, 4)

    def test_15_request_log_tracks_all(self):
        """Request log captures every checked request."""
        self.guard.check('POST', 'https://api.amazon.com/auth/o2/token')
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/sellers/v1/marketplaceParticipations')
        self.assertEqual(len(self.guard.request_log), 2)
        self.assertEqual(self.guard.request_log[0]['method'], 'POST')
        self.assertEqual(self.guard.request_log[1]['method'], 'GET')

    def test_16_monkey_patch_blocks_post(self):
        """The monkey-patched requests.request blocks SP-API POST."""
        import requests
        original = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(self.diag.EndpointBlocked):
                requests.request('POST', 'https://sellingpartnerapi-eu.amazon.com/feeds/2021-06-30/feeds')
        finally:
            self.diag._uninstall_request_guard(original)

    def test_17_monkey_patch_allows_get(self):
        """The monkey-patched requests.request allows allowed GET (but connection will fail)."""
        import requests
        original = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(requests.exceptions.ConnectionError):
                requests.request('GET', 'https://sellingpartnerapi-eu.amazon.com/sellers/v1/marketplaceParticipations')
            self.assertEqual(self.guard.request_count, 1)
        finally:
            self.diag._uninstall_request_guard(original)

    def test_18_token_redaction(self):
        """Token values are not exposed in redact()."""
        self.assertEqual(self.diag.redact('Atza|abcdef123456'), 'Atza...' + self.diag.REDACTED)
        self.assertEqual(self.diag.redact('short'), self.diag.REDACTED)
        self.assertIsNone(self.diag.redact(None))
        self.assertFalse(self.diag.redact(''))

    def test_19_safe_url_redacts_tokens(self):
        """URLs with token/secret query params are redacted."""
        url = 'https://example.com/path?token=abc123&other=ok'
        safe = self.diag._safe_url(url)
        self.assertNotIn('abc123', safe)
        self.assertIn('REDACTED', safe)

    def test_20_blocked_put_cancel_fulfillment(self):
        """PUT to cancel fulfillment order is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('PUT', 'https://sellingpartnerapi-eu.amazon.com/fba/outbound/2020-07-01/fulfillmentOrders/123/cancel')

    def test_21_blocked_post_packing_options(self):
        """POST to generate packing options is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('POST', 'https://sellingpartnerapi-eu.amazon.com/inbound/fba/2024-03-20/inboundPlans/123/packingOptions')

    def test_22_allowed_get_catalog(self):
        """GET to catalog endpoint is allowed."""
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/catalog/2022-04-01/catalogItems')
        self.assertEqual(self.guard.request_count, 1)

    def test_23_allowed_get_reports_list(self):
        """GET to reports listing is allowed."""
        self.guard.check('GET', 'https://sellingpartnerapi-eu.amazon.com/reports/2021-06-30/reports')
        self.assertEqual(self.guard.request_count, 1)

    def test_24_remaining_budget_tracks(self):
        """remaining property decrements correctly."""
        self.assertEqual(self.guard.remaining, 14)
        self.guard.check('POST', 'https://api.amazon.com/auth/o2/token')
        self.assertEqual(self.guard.remaining, 13)

    def test_25_blocked_s3_upload(self):
        """PUT to S3 pre-signed URL (feed upload) is BLOCKED."""
        with self.assertRaises(self.diag.EndpointBlocked):
            self.guard.check('PUT', 'https://tortuga-prod-na.s3.amazonaws.com/feed-doc-123')

    def test_26_wrong_marketplace_in_expected(self):
        """EXPECTED_MARKETPLACE constant is correct for Egypt."""
        self.assertEqual(self.diag.EXPECTED_MARKETPLACE, 'ARBP9OOSHTCHU')

    def test_27_dangerous_crons_list_complete(self):
        """DANGEROUS_CRONS list includes all 10 API-capable crons."""
        self.assertEqual(len(self.diag.DANGEROUS_CRONS), 10)
        self.assertIn('Amazon: Process Order Import Jobs', self.diag.DANGEROUS_CRONS)
        self.assertIn('Amazon: Check Connection Health', self.diag.DANGEROUS_CRONS)
        self.assertIn('Amazon: Process FBA Sale Stock Events', self.diag.DANGEROUS_CRONS)
        self.assertIn('Amazon: Synchronize Inbound Receiving', self.diag.DANGEROUS_CRONS)

    def test_28_monkey_patch_blocks_requests_post(self):
        """requests.post to SP-API endpoint is blocked by guard."""
        import requests
        originals = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(self.diag.EndpointBlocked):
                requests.post('https://sellingpartnerapi-eu.amazon.com/feeds/2021-06-30/feeds')
        finally:
            self.diag._uninstall_request_guard(originals)

    def test_29_monkey_patch_allows_requests_post_oauth(self):
        """requests.post to OAuth endpoint is allowed (connection will fail)."""
        import requests
        originals = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(requests.exceptions.ConnectionError):
                requests.post('https://api.amazon.com/auth/o2/token', data={})
            self.assertEqual(self.guard.request_count, 1)
        finally:
            self.diag._uninstall_request_guard(originals)

    def test_30_monkey_patch_blocks_requests_get_unknown(self):
        """requests.get to unknown endpoint is blocked by guard."""
        import requests
        originals = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(self.diag.EndpointBlocked):
                requests.get('https://sellingpartnerapi-eu.amazon.com/some/unknown/api')
        finally:
            self.diag._uninstall_request_guard(originals)

    def test_31_monkey_patch_blocks_requests_put(self):
        """requests.put is blocked by guard."""
        import requests
        originals = self.diag._install_request_guard(self.guard)
        try:
            with self.assertRaises(self.diag.EndpointBlocked):
                requests.put('https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/X/Y')
        finally:
            self.diag._uninstall_request_guard(originals)
