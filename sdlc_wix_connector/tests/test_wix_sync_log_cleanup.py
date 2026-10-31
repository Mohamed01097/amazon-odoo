from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWixSyncLogCleanup(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env['wix.instance'].create({
            'name': 'Cleanup Test Store',
            'order_import_enabled': False,
        })

    def _log(self, state, days_old=0, **extra):
        vals = {
            'instance_id': self.instance.id,
            'operation': 'order_import',
            'state': state,
        }
        vals.update(extra)
        log = self.env['wix.sync.log'].create(vals)
        if days_old:
            # create_date is set from the database clock, not Python's, so it
            # is backdated directly rather than through freezegun.
            self.env.cr.execute(
                "UPDATE wix_sync_log SET create_date = create_date - %s::interval"
                " WHERE id = %s",
                (f'{days_old} days', log.id),
            )
            log.invalidate_recordset(['create_date'])
        return log

    def test_old_successful_logs_are_removed(self):
        old_success = self._log('success', days_old=31)
        recent_success = self._log('success')

        self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)

        self.assertFalse(old_success.exists())
        self.assertTrue(recent_success.exists())

    def test_old_failures_are_kept(self):
        """Failures are audit evidence and must survive cleanup indefinitely."""
        old_failure = self._log('failed', days_old=365, error_message='boom')

        self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(old_failure.exists())

    def test_old_started_logs_are_kept(self):
        """A log stuck in 'started' may indicate a crash; never delete it silently."""
        stuck = self._log('started', days_old=365)

        self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(stuck.exists())

    def test_recent_successful_logs_are_kept(self):
        recent = self._log('success')
        self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(recent.exists())

    def test_cleanup_returns_the_deleted_count(self):
        self._log('success', days_old=31)
        self._log('success', days_old=40)
        count = self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertEqual(count, 2)

    def test_boundary_just_under_threshold_is_kept(self):
        kept = self._log('success', days_old=29)
        self.env['wix.sync.log'].cron_cleanup_old_logs(days=30)
        self.assertTrue(kept.exists())
