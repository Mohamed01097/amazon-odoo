from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBostaSyncLogCleanup(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env['bosta.instance'].create({'name': 'Cleanup Test Account'})

    def _log(self, state, days_old=0, **extra):
        vals = {
            'instance_id': self.instance.id,
            'operation': 'create_shipment',
            'state': state,
        }
        vals.update(extra)
        log = self.env['bosta.sync.log'].create(vals)
        if days_old:
            # create_date is set from the database clock, not Python's, so it
            # is backdated directly rather than through freezegun.
            self.env.cr.execute(
                "UPDATE bosta_sync_log SET create_date = create_date - %s::interval"
                " WHERE id = %s",
                (f'{days_old} days', log.id),
            )
            log.invalidate_recordset(['create_date'])
        return log

    def test_old_successful_logs_are_removed(self):
        old_success = self._log('success', days_old=31)
        recent_success = self._log('success')

        self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)

        self.assertFalse(old_success.exists())
        self.assertTrue(recent_success.exists())

    def test_old_failures_are_kept(self):
        """Failures are audit evidence and must survive cleanup indefinitely."""
        old_failure = self._log('failed', days_old=365, error_message='boom')

        self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(old_failure.exists())

    def test_old_started_logs_are_kept(self):
        stuck = self._log('started', days_old=365)
        self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(stuck.exists())

    def test_recent_successful_logs_are_kept(self):
        recent = self._log('success')
        self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(recent.exists())

    def test_cleanup_returns_the_deleted_count(self):
        self._log('success', days_old=31)
        self._log('success', days_old=40)
        count = self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertEqual(count, 2)

    def test_boundary_just_under_threshold_is_kept(self):
        kept = self._log('success', days_old=29)
        self.env['bosta.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(kept.exists())
