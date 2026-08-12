import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from ..models.amazon_api import AmazonAPI


@tagged('post_install', '-at_install', 'amazon_phase65')
class TestAmazonOperationsPhase65(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].sudo().create({
            'name': 'Amazon Phase 6.5 Company',
        })
        self.other_company = self.env['res.company'].sudo().create({
            'name': 'Amazon Phase 6.5 Other Company',
        })
        self.instance = self._instance(self.company, 'P65')
        self.other_instance = self._instance(self.other_company, 'P65-OTHER')
        self.env.user.group_ids |= self.env.ref(
            'sdlc_amazon_connector.group_amazon_technical_admin'
        )

    def _instance(self, company, suffix):
        return self.env['amazon.instance'].sudo().create({
            'name': 'Amazon Operations %s' % suffix,
            'company_id': company.id,
            'seller_id': '%s-SELLER' % suffix,
            'marketplace_id': 'ARBP9OOSHTCHU',
            'refresh_token': '%s-refresh-secret' % suffix,
            'client_id': '%s-client' % suffix,
            'client_secret': '%s-client-secret' % suffix,
            'region': 'eu',
            'maximum_automatic_retries': 3,
            'retry_backoff_base_seconds': 1,
            'stuck_job_threshold_minutes': 10,
        })

    @staticmethod
    def _response(status, payload, headers=None):
        response = MagicMock()
        response.status_code = status
        response.ok = 200 <= status < 300
        response.headers = headers or {}
        response.text = json.dumps(payload)
        response.json.return_value = payload
        response.request = MagicMock(method='GET', headers={}, url='https://example.test')
        response.url = 'https://example.test'
        return response

    def _failed_status_job(self, message, code='Error'):
        job = self.env['amazon.order.status.sync.job'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'draft',
        })
        job.write({
            'state': 'failed',
            'last_error_code': code,
            'last_error_message': message,
            'error_message': message,
        })
        return job, self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', job._name), ('source_id', '=', job.id),
        ])

    def test_01_healthy_instance_uses_lightweight_read_only_call(self):
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token') as token_call,
            patch.object(AmazonAPI, 'get_marketplace_participations', return_value={
                'payload': [{'marketplace': {'id': 'ARBP9OOSHTCHU'}}],
            }) as marketplace_call,
        ):
            self.assertTrue(self.instance._run_health_check())
        self.assertEqual(self.instance.connection_health, 'healthy')
        self.assertEqual(self.instance.token_health, 'healthy')
        self.assertEqual(self.instance.authorization_health, 'healthy')
        token_call.assert_called_once()
        marketplace_call.assert_called_once()

    def test_02_invalid_or_revoked_authorization(self):
        response = self._response(
            400,
            {'error': 'invalid_grant', 'error_description': 'Authorization revoked'},
            {'x-amzn-RequestId': 'oauth-revoked-request'},
        )
        with patch('requests.post', return_value=response):
            self.assertFalse(self.instance._run_health_check())
        self.assertEqual(self.instance.token_health, 'error')
        self.assertEqual(self.instance.external_failure_kind, 'authorization')
        self.assertEqual(self.instance.last_amazon_request_id, 'oauth-revoked-request')

    def test_03_central_error_classification(self):
        classifier = self.env['amazon.operation.control']
        rate = classifier.classify_error(http_status=429, message='Quota exceeded')
        service = classifier.classify_error(http_status=500, message='Internal failure')
        validation = classifier.classify_error(
            http_status=400, error_code='InvalidInput', message='Invalid address',
        )
        authorization = classifier.classify_error(
            http_status=403, error_code='Unauthorized', message='Role is not authorized',
        )
        self.assertEqual(rate['category'], 'rate_limit')
        self.assertTrue(rate['retry_safe'])
        self.assertEqual(service['category'], 'amazon_service')
        self.assertTrue(service['transient'])
        self.assertEqual(validation['category'], 'validation')
        self.assertFalse(validation['retry_safe'])
        self.assertEqual(authorization['category'], 'authorization')

    def test_04_safe_retry_and_duplicate_retry_prevention(self):
        job, control = self._failed_status_job('Temporary network timeout')
        self.assertEqual(control.state, 'retry_pending')
        self.assertTrue(control.retry_safe)
        self.assertTrue(control.action_retry())
        self.assertEqual(job.state, 'pending')
        self.assertEqual(control.state, 'active')
        self.assertEqual(control.retry_count, 1)
        with self.assertRaises(UserError):
            control.action_retry()
        self.assertEqual(self.env['amazon.order.status.sync.job'].search_count([
            ('id', '=', job.id),
        ]), 1)

    def test_05_permanent_validation_and_data_failures_are_blocked(self):
        _job, control = self._failed_status_job(
            'Invalid SKU: missing product mapping', code='InvalidInput',
        )
        self.assertEqual(control.state, 'manual_review')
        self.assertIn(control.error_category, ('data', 'validation'))
        self.assertFalse(control.retry_safe)
        with self.assertRaises(UserError):
            control.action_retry()

    def test_06_stuck_job_detection_resumes_same_record(self):
        job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id, 'state': 'draft',
        })
        job.write({'state': 'running'})
        old = fields.Datetime.now() - timedelta(minutes=30)
        job.with_context(skip_amazon_operation_tracking=True).write({
            'last_activity_at': old,
        })
        self.env.invalidate_all()
        count = self.env['amazon.operation.control'].sudo().cron_detect_stuck_jobs()
        control = self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', job._name), ('source_id', '=', job.id),
        ])
        self.assertGreaterEqual(count, 1)
        self.assertEqual(control.waiting_reason, 'abandoned')
        self.assertEqual(control.state, 'retry_pending')
        self.assertTrue(control.retry_safe)
        self.assertEqual(control.source_id, job.id)

    def test_07_waiting_for_amazon_is_not_falsely_stuck(self):
        shipment = self.env['amazon.inbound.shipment'].sudo().create({
            'name': 'P65-WAITING',
            'shipment_name': 'P65-WAITING',
            'instance_id': self.instance.id,
            'state': 'plan_created',
        })
        job = self.env['amazon.inbound.operation.job'].sudo().create({
            'inbound_shipment_id': shipment.id,
            'operation_type': 'generate_packing_options',
            'operation_id': '12345678-1234-1234-1234-123456789012',
            'state': 'pending',
        })
        job.write({
            'state': 'in_progress',
            'next_run_at': fields.Datetime.now() + timedelta(minutes=20),
        })
        job.with_context(skip_amazon_operation_tracking=True).write({
            'last_activity_at': fields.Datetime.now() - timedelta(minutes=30),
        })
        self.env.invalidate_all()
        self.env['amazon.operation.control'].sudo().cron_detect_stuck_jobs()
        control = self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', job._name), ('source_id', '=', job.id),
        ])
        self.assertEqual(control.waiting_reason, 'waiting_amazon')
        self.assertNotEqual(control.state, 'retry_pending')

    def test_08_retry_after_and_rate_limit_headers_are_persisted(self):
        response = self._response(
            429, {'errors': [{'code': 'QuotaExceeded', 'message': 'Throttled'}]},
            {
                'Retry-After': '120',
                'x-amzn-RateLimit-Limit': '2.5',
                'x-amzn-RequestId': 'rate-request-id',
            },
        )
        with patch('requests.request', return_value=response):
            # Odoo's ``assertRaises`` wraps the body in a database savepoint
            # and rolls that savepoint back with the exception.  Catch the
            # expected transport error explicitly so the request-attempt log
            # remains available for the assertions below.
            try:
                AmazonAPI()._amazon_request(
                    self.instance.with_context(amazon_operation='testRateLimit'),
                    'token', 'GET', '/test/rate-limit',
                )
            except requests.exceptions.HTTPError:
                pass
            else:
                self.fail('The mocked HTTP 429 response did not raise HTTPError')
        log = self.env['amazon.sync.log'].sudo().search([
            ('amazon_request_id', '=', 'rate-request-id'),
        ], limit=1)
        self.assertTrue(log, self.env['amazon.sync.log'].sudo().search([
            ('operation_name', '=', 'testRateLimit'),
        ]).read(['amazon_request_id', 'http_status', 'is_throttled']))
        self.assertTrue(log.is_throttled)
        self.assertEqual(log.http_status, 429)
        self.assertEqual(log.rate_limit, 2.5)
        self.assertEqual(log.retry_after_seconds, 120)
        self.assertEqual(log.error_category, 'rate_limit')
        self.assertEqual(self.instance.last_rate_limit_at, log.finished_at)

        job = self.env['amazon.order.status.sync.job'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'draft',
        })
        scheduled_from = fields.Datetime.now()
        self.env['amazon.sync.log'].sudo().with_context(
            amazon_source_model=job._name,
            amazon_source_id=job.id,
            amazon_operation='getOrdersForRetry',
        ).log_api_request(
            self.instance,
            request_data={'method': 'GET', 'endpoint': 'https://example.test/orders'},
            response_data={
                'status_code': 429,
                'headers': {'Retry-After': '120'},
                'response_json': {'errors': [{'code': 'QuotaExceeded', 'message': 'Throttled'}]},
            },
            error_message='HTTP 429',
        )
        job.write({
            'state': 'failed',
            'last_error_code': 'QuotaExceeded',
            'last_error_message': 'Throttled',
            'error_message': 'Throttled',
        })
        control = self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', job._name), ('source_id', '=', job.id),
        ])
        self.assertGreaterEqual(
            control.next_retry_at,
            scheduled_from + timedelta(seconds=120),
        )

    def test_09_alert_deduplication_and_automatic_resolution(self):
        self.instance.write({
            'authorization_health': 'error',
            'token_health': 'error',
            'connection_health': 'error',
            'last_amazon_error_message': 'Authorization revoked',
        })
        alerts = self.env['amazon.smart.alert'].sudo()
        alerts.cron_evaluate_operational_alerts()
        alerts.cron_evaluate_operational_alerts()
        issue_key = 'authorization:%s' % self.instance.id
        alert = alerts.search([('issue_key', '=', issue_key)])
        self.assertEqual(len(alert), 1)
        self.assertIn(alert.state, ('new', 'acknowledged'))
        self.instance.write({
            'authorization_health': 'healthy',
            'token_health': 'healthy',
            'connection_health': 'healthy',
        })
        alerts.cron_evaluate_operational_alerts()
        self.assertEqual(alert.state, 'resolved')

    def test_10_multi_company_dashboard_isolation(self):
        Dashboard = self.env['amazon.operations.dashboard'].sudo()
        first = Dashboard.create({'instance_id': self.instance.id})
        Dashboard.create({'instance_id': self.other_instance.id})
        user = self.env['res.users'].sudo().create({
            'name': 'Amazon Phase 6.5 User',
            'login': 'amazon-phase65-user',
            'company_id': self.company.id,
            'company_ids': [Command.set([self.company.id])],
            'group_ids': [Command.set([
                self.env.ref('base.group_user').id,
                self.env.ref('sdlc_amazon_connector.group_amazon_user').id,
            ])],
        })
        visible = self.env['amazon.operations.dashboard'].with_user(user).search([])
        self.assertEqual(visible, first)

        own_product = self.env['amazon.product'].sudo().create({
            'name': 'Company Product', 'sku': 'P65-COMPANY-SKU',
            'instance_id': self.instance.id,
        })
        self.env['amazon.product'].sudo().create({
            'name': 'Other Company Product', 'sku': 'P65-OTHER-COMPANY-SKU',
            'instance_id': self.other_instance.id,
        })
        own_order = self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': 'P65-COMPANY-ORDER', 'instance_id': self.instance.id,
        })
        self.env['amazon.sale.order'].sudo().create({
            'amazon_order_ref': 'P65-OTHER-COMPANY-ORDER',
            'instance_id': self.other_instance.id,
        })
        self.assertEqual(
            self.env['amazon.product'].with_user(user).search([
                ('sku', 'like', 'P65-%COMPANY-SKU'),
            ]),
            own_product,
        )
        self.assertEqual(
            self.env['amazon.sale.order'].with_user(user).search([
                ('amazon_order_ref', 'like', 'P65-%COMPANY-ORDER'),
            ]),
            own_order,
        )
        with self.assertRaises(AccessError):
            own_product.with_user(user).write({'name': 'Unauthorized Change'})
        with self.assertRaises(AccessError):
            self.instance.with_user(user)._get_access_token_or_raise()

        internal_user = self.env['res.users'].sudo().create({
            'name': 'Internal Non-Amazon User',
            'login': 'internal-non-amazon-user',
            'company_id': self.company.id,
            'company_ids': [Command.set([self.company.id])],
            'group_ids': [Command.set([self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.env['amazon.product'].with_user(internal_user).search([])

        own_job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'draft',
            'responsible_user_id': user.id,
        })
        other_job = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id,
            'state': 'draft',
            'responsible_user_id': self.env.user.id,
        })
        own_control = self.env['amazon.operation.control'].sudo().get_or_create_for_source(
            own_job._name, own_job.id,
        )
        other_control = self.env['amazon.operation.control'].sudo().get_or_create_for_source(
            other_job._name, other_job.id,
        )
        visible_controls = self.env['amazon.operation.control'].with_user(user).search([
            ('id', 'in', (own_control | other_control).ids),
        ])
        self.assertEqual(visible_controls, own_control)

        own_log = self.env['amazon.sync.log'].sudo().create({
            'instance_id': self.instance.id,
            'operation': 'health_check',
            'state': 'success',
            'responsible_user_id': user.id,
        })
        other_log = self.env['amazon.sync.log'].sudo().create({
            'instance_id': self.instance.id,
            'operation': 'health_check',
            'state': 'success',
            'responsible_user_id': self.env.user.id,
        })
        visible_logs = self.env['amazon.sync.log'].with_user(user).search([
            ('id', 'in', (own_log | other_log).ids),
        ])
        self.assertEqual(visible_logs, own_log)
        with self.assertRaises(AccessError):
            self.instance.with_user(user).read(['refresh_token'])
        with self.assertRaises(AccessError):
            own_log.with_user(user).read(['request_data'])

    def test_11_dashboard_counts_are_stored_and_accurate(self):
        running = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id, 'state': 'draft',
        })
        running.write({'state': 'running'})
        self._failed_status_job('Invalid address', code='InvalidInput')
        legacy_failed = self.env['amazon.order.import.job'].sudo().create({
            'instance_id': self.instance.id, 'state': 'draft',
        })
        legacy_failed.with_context(skip_amazon_operation_tracking=True).write({
            'state': 'failed', 'error_message': 'Temporary network timeout',
        })
        self.assertFalse(self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', legacy_failed._name),
            ('source_id', '=', legacy_failed.id),
        ]))
        self.env['amazon.sync.log'].sudo().log_api_request(
            self.instance,
            request_data={'method': 'GET', 'endpoint': 'https://example.test/orders'},
            response_data={
                'status_code': 500,
                'amazon_request_id': 'dashboard-error-id',
                'headers': {},
                'response_json': {'errors': [{'code': 'InternalFailure', 'message': 'Error'}]},
            },
            error_message='HTTP 500',
            duration_seconds=0.2,
        )
        for request_number in range(3):
            self.env['amazon.sync.log'].sudo().with_context(
                amazon_operation='dashboardThrottle',
            ).log_api_request(
                self.instance,
                request_data={'method': 'GET', 'endpoint': 'https://example.test/throttle'},
                response_data={
                    'status_code': 429,
                    'amazon_request_id': 'dashboard-throttle-%s' % request_number,
                    'headers': {'Retry-After': '10'},
                    'response_json': {
                        'errors': [{'code': 'QuotaExceeded', 'message': 'Throttled'}],
                    },
                },
                error_message='HTTP 429',
            )
        dashboard = self.env['amazon.operations.dashboard'].sudo().create({
            'instance_id': self.instance.id,
        })
        monitor_states = self.env['amazon.operation.job.monitor'].sudo().search([
            ('instance_id', '=', self.instance.id),
        ]).mapped('state')
        self.assertIn('running', monitor_states, monitor_states)
        dashboard._refresh_snapshot()
        self.assertEqual(dashboard.running_jobs, 1)
        # The validation failure stays in Failed Jobs; the transient legacy
        # failure is separately exposed as Waiting Retry.
        self.assertEqual(dashboard.failed_jobs, 1)
        self.assertEqual(dashboard.waiting_retry, 1)
        self.assertTrue(self.env['amazon.operation.control'].sudo().search([
            ('source_model', '=', legacy_failed._name),
            ('source_id', '=', legacy_failed.id),
            ('state', 'in', ('retry_pending', 'manual_review', 'exhausted')),
        ]))
        self.assertGreaterEqual(dashboard.api_errors_24h, 4)
        self.assertGreaterEqual(dashboard.rate_limits_1h, 3)
        self.assertTrue(dashboard.generated_at)
        metric = self.env['amazon.api.operation.metric'].sudo().search([
            ('instance_id', '=', self.instance.id),
            ('operation_name', '=', 'dashboardThrottle'),
        ])
        self.assertEqual(metric.throttle_count_1h, 3)
        self.assertTrue(metric.repeated_throttling)
        self.assertEqual(metric.throttle_rate_24h, 100.0)

    def test_12_health_check_has_no_business_side_effects(self):
        models = (
            'amazon.sale.order', 'amazon.inbound.shipment', 'stock.picking',
            'stock.move', 'stock.quant',
        )
        before = {model: self.env[model].sudo().search_count([]) for model in models}
        with (
            patch.object(AmazonAPI, 'get_access_token', return_value='token'),
            patch.object(AmazonAPI, 'get_marketplace_participations', return_value={'payload': []}),
        ):
            self.instance._run_health_check()
        after = {model: self.env[model].sudo().search_count([]) for model in models}
        self.assertEqual(after, before)

    def test_13_restart_persistence_and_attempt_history(self):
        job, control = self._failed_status_job('Connection reset by peer')
        control_id = control.id
        log_ids = control.attempt_log_ids.ids
        self.env.invalidate_all()
        persisted = self.env['amazon.operation.control'].sudo().browse(control_id)
        self.assertTrue(persisted.exists())
        self.assertEqual(persisted.source_id, job.id)
        self.assertEqual(persisted.attempt_log_ids.ids, log_ids)
        self.assertTrue(persisted.first_failure_at)
        self.assertTrue(persisted.latest_failure_at)

    def test_14_log_sanitization_and_request_metadata(self):
        log = self.env['amazon.sync.log'].sudo().log_api_request(
            self.instance,
            request_data={
                'method': 'POST',
                'endpoint': 'https://example.test/operation',
                'payload': {
                    'client_secret': 'never-store-this',
                    'nested': {'refresh_token': 'never-store-token', 'safe': 'yes'},
                },
                'headers': {'Authorization': 'secret-authorization'},
            },
            response_data={
                'status_code': 200,
                'amazon_request_id': 'sanitized-request-id',
                'headers': {'x-amzn-RateLimit-Limit': '5'},
                'response_json': {'access_token': 'response-secret', 'ok': True},
            },
            duration_seconds=0.1,
        )
        combined = '%s\n%s' % (log.request_data, log.response_data)
        self.assertNotIn('never-store-this', combined)
        self.assertNotIn('never-store-token', combined)
        self.assertNotIn('secret-authorization', combined)
        self.assertNotIn('response-secret', combined)
        self.assertIn('***REDACTED***', combined)
        self.assertEqual(log.amazon_request_id, 'sanitized-request-id')
        self.assertEqual(log.http_method, 'POST')
        self.assertEqual(log.endpoint, '/operation')

    def test_15_dashboard_action_does_not_call_amazon(self):
        dashboard = self.env['amazon.operations.dashboard'].sudo().create({
            'instance_id': self.instance.id,
        })
        with (
            patch.object(AmazonAPI, 'get_access_token') as token_call,
            patch.object(AmazonAPI, 'get_marketplace_participations') as marketplace_call,
        ):
            values = dashboard.read([
                'connection_health', 'running_jobs', 'failed_jobs',
                'rate_limits_24h', 'generated_at',
            ])
        self.assertEqual(len(values), 1)
        token_call.assert_not_called()
        marketplace_call.assert_not_called()
