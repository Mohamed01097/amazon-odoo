import os
import re

from odoo.tests import TransactionCase, tagged

# Amazon fetch/write crons that must NEVER default to active on a fresh install
# (they contact Amazon or export data). Operators opt in explicitly.
SAFETY_INACTIVE_DEFAULTS = {
    'cron_amazon_import_orders', 'cron_amazon_import_fbm_orders',
    'cron_amazon_export_stock', 'cron_amazon_update_prices',
    'cron_amazon_import_settlement', 'cron_amazon_update_fbm_status',
    'cron_amazon_import_fba_customer_returns', 'cron_amazon_import_fba_reimbursements',
}


@tagged('post_install', '-at_install', 'amazon_cron_upgrade_safety')
class TestCronUpgradeSafety(TransactionCase):
    """Phase 17: module upgrades must not overwrite operator-controlled cron config."""

    def _module_cron_imd(self):
        return self.env['ir.model.data'].search([
            ('module', '=', 'sdlc_amazon_connector'), ('model', '=', 'ir.cron')])

    def _cron_xml_path(self):
        return os.path.join(os.path.dirname(__file__), '..', 'data', 'cron.xml')

    def _xml_records(self):
        """Return {xml_id: active_default_bool} parsed from data/cron.xml."""
        with open(self._cron_xml_path(), encoding='utf-8') as fh:
            text = fh.read()
        records = {}
        for block in re.findall(r'<record id="(cron_[a-z0-9_]+)" model="ir\.cron">(.*?)</record>',
                                text, re.DOTALL):
            xmlid, body = block
            m = re.search(r'<field name="active">(\w+)</field>', body)
            records[xmlid] = (m.group(1).lower() == 'true') if m else None
        return records

    # 1 — the fix invariant: every module cron is protected from upgrade overwrite
    def test_01_all_module_crons_are_noupdate(self):
        imd = self._module_cron_imd()
        self.assertTrue(imd, "module must define ir.cron records")
        not_protected = imd.filtered(lambda d: not d.noupdate)
        self.assertFalse(
            not_protected.mapped('name'),
            "these crons would be overwritten on upgrade (noupdate must be True)")

    # 2 — XML IDs present and unique, matching the DB
    def test_02_cron_xml_ids_unique_and_present(self):
        xml = self._xml_records()
        self.assertEqual(len(xml), 33, "expected 33 cron definitions in cron.xml")
        self.assertEqual(len(set(xml)), 33, "duplicate cron XML IDs in cron.xml")
        db_ids = set(self._module_cron_imd().mapped('name'))
        self.assertTrue(set(xml).issubset(db_ids),
                        "every cron.xml record must exist in ir.model.data")

    # 3 — every cron record is well-formed
    def test_03_each_cron_well_formed(self):
        for data in self._module_cron_imd():
            cron = self.env['ir.cron'].browse(data.res_id)
            self.assertTrue(cron.exists(), "%s points to a missing cron" % data.name)
            self.assertTrue(cron.ir_actions_server_id or cron.model_id,
                            "%s has no model/action" % data.name)
            self.assertIn(cron.interval_type,
                          ('minutes', 'hours', 'days', 'weeks', 'months'))
            self.assertGreater(cron.interval_number, 0, "%s bad interval" % data.name)

    # 4 — fresh-install defaults are explicit (no ambiguous active state)
    def test_04_fresh_install_active_defaults_explicit(self):
        xml = self._xml_records()
        missing = [x for x, active in xml.items() if active is None]
        self.assertFalse(missing, "cron.xml records without an explicit <active>: %s" % missing)

    # 5 — Amazon fetch/write crons default INACTIVE on fresh install (safety)
    def test_05_amazon_write_crons_default_inactive(self):
        xml = self._xml_records()
        wrong = [x for x in SAFETY_INACTIVE_DEFAULTS if xml.get(x) is True]
        self.assertFalse(
            wrong, "these Amazon-contacting crons must default inactive: %s" % wrong)

    # 6 — upgrade/registry load keeps Amazon automation off by default
    def test_06_no_auto_amazon_after_load(self):
        company = self.env.company
        inst = self.env['amazon.instance'].create({
            'name': 'CronSafetyInstance', 'company_id': company.id,
            'marketplace_id': 'ARBP9OOSHTCHU', 'region': 'eu',
            'refresh_token': 'm', 'client_id': 'm', 'client_secret': 'm'})
        self.assertFalse(inst.order_import_enabled, "order import must be off by default")
        self.assertFalse(inst.auto_sync_enabled, "auto-sync must be off by default")

    # 7 — the noupdate protection is wired in the XML source (not only migration)
    def test_07_cron_xml_declares_noupdate(self):
        with open(self._cron_xml_path(), encoding='utf-8') as fh:
            text = fh.read()
        self.assertRegex(text, r'<data\s+noupdate="1">',
                         "cron.xml must wrap records in <data noupdate=\"1\"> for fresh installs")
