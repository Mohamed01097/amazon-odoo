"""Multi-company record-rule coverage for the address catalogue.

bosta.city/zone/district previously had access rights (the CSV) but no
``ir.rule``, so a user in one company could read another company's cached
Bosta catalogue. These tests run as a real internal user, not the superuser,
because ``ir.rule`` is enforced for non-superuser users only.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBostaMultiCompany(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_company = cls.env['res.company'].create({'name': 'Bosta Other Co'})

        cls.own_instance = cls.env['bosta.instance'].create({
            'name': 'Own Instance', 'company_id': cls.env.company.id,
        })
        cls.other_instance = cls.env['bosta.instance'].create({
            'name': 'Other Instance', 'company_id': cls.other_company.id,
        })

        cls.own_city = cls.env['bosta.city'].create({
            'name': 'Own City', 'bosta_id': 'own-c1',
            'instance_id': cls.own_instance.id,
        })
        cls.other_city = cls.env['bosta.city'].create({
            'name': 'Other City', 'bosta_id': 'other-c1',
            'instance_id': cls.other_instance.id,
        })
        cls.own_zone = cls.env['bosta.zone'].create({
            'name': 'Own Zone', 'city_id': cls.own_city.id,
        })
        cls.other_zone = cls.env['bosta.zone'].create({
            'name': 'Other Zone', 'city_id': cls.other_city.id,
        })
        cls.own_district = cls.env['bosta.district'].create({
            'name': 'Own District', 'zone_id': cls.own_zone.id,
        })
        cls.other_district = cls.env['bosta.district'].create({
            'name': 'Other District', 'zone_id': cls.other_zone.id,
        })

        cls.scoped_user = cls.env['res.users'].create({
            'name': 'Bosta Scoped User',
            'login': 'bosta_scoped_user_test',
            'group_ids': [(6, 0, [
                cls.env.ref('sdlc_bosta_connector.group_bosta_user').id,
            ])],
            'company_ids': [(6, 0, [cls.env.company.id])],
            'company_id': cls.env.company.id,
        })

    def test_city_is_scoped_by_company(self):
        visible = self.env['bosta.city'].with_user(self.scoped_user).search([])
        self.assertIn(self.own_city, visible)
        self.assertNotIn(self.other_city, visible)

    def test_zone_is_scoped_by_company(self):
        """Zone has no company_id of its own; scoping follows city->instance."""
        visible = self.env['bosta.zone'].with_user(self.scoped_user).search([])
        self.assertIn(self.own_zone, visible)
        self.assertNotIn(self.other_zone, visible)

    def test_district_is_scoped_by_company(self):
        visible = self.env['bosta.district'].with_user(self.scoped_user).search([])
        self.assertIn(self.own_district, visible)
        self.assertNotIn(self.other_district, visible)

    def test_scoped_user_cannot_read_other_companys_city_by_id(self):
        """Direct id access must be blocked too, not only search filtering."""
        with self.assertRaises(Exception):
            self.other_city.with_user(self.scoped_user).read(['name'])

    def test_scoped_user_can_still_read_own_company_city(self):
        record = self.own_city.with_user(self.scoped_user)
        self.assertEqual(record.name, 'Own City')
