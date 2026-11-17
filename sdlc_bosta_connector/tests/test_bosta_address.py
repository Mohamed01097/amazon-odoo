"""Address catalogue and address-resolution tests. All Bosta calls are mocked."""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.sdlc_bosta_connector.models.bosta_api import BostaError
from odoo.addons.sdlc_bosta_connector.models.bosta_location import normalize_name

CITIES_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.get_cities'
ZONES_PATH = 'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.get_city_zones'

# Shaped like Bosta's SDK responses: a success wrapper around a data list.
CITIES_RESPONSE = {'success': True, 'data': [
    {'_id': 'c1', 'name': 'Cairo', 'otherName': 'القاهرة', 'code': 'EG-01'},
    {'_id': 'c2', 'name': 'Giza', 'otherName': 'الجيزة', 'code': 'EG-02'},
]}

ZONES_BY_CITY = {
    'c1': {'data': [
        {'_id': 'z1', 'name': 'Maadi & Muqattam', 'districts': [
            {'districtId': 'd1', 'districtName': 'Maadi',
             'dropOffAvailability': True, 'pickupAvailability': True},
            {'districtId': 'd2', 'districtName': 'Muqattam',
             'dropOffAvailability': False, 'pickupAvailability': True},
        ]},
        {'_id': 'z2', 'name': 'Nasr City', 'districts': []},
    ]},
    'c2': {'data': [{'_id': 'z3', 'name': 'Dokki', 'districts': []}]},
}


@tagged('post_install', '-at_install')
class TestBostaAddress(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env['bosta.instance'].create({
            'name': 'Bosta Address Test',
            'api_key': 'address-test-key',
            'shipment_booking_enabled': True,
        })

    def _sync(self):
        with patch(CITIES_PATH, return_value=CITIES_RESPONSE), \
             patch(ZONES_PATH, side_effect=lambda cid: ZONES_BY_CITY.get(cid, {})):
            self.instance.action_sync_address_catalogue()

    # ------------------------------------------------------------------
    # Name normalisation
    # ------------------------------------------------------------------

    def test_normalisation_folds_arabic_variants(self):
        self.assertEqual(normalize_name('القاهرة'), normalize_name('القاهره'))
        # Ta marbuta and ha are written interchangeably in addresses.
        self.assertEqual(normalize_name('الجيزة'), normalize_name('الجيزه'))
        # Alef maqsura folds to ya.
        self.assertEqual(normalize_name('المعادى'), normalize_name('المعادي'))
        self.assertEqual(normalize_name('  Cairo  '), 'cairo')
        self.assertEqual(normalize_name('El Maadi'), 'maadi')
        self.assertEqual(normalize_name('Nasr  City'), 'nasr')
        self.assertEqual(normalize_name(None), '')

    # ------------------------------------------------------------------
    # Catalogue sync
    # ------------------------------------------------------------------

    def test_sync_caches_cities_zones_and_districts(self):
        self._sync()

        cities = self.env['bosta.city'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(len(cities), 2)
        cairo = cities.filtered(lambda c: c.code == 'EG-01')
        self.assertEqual(cairo.name, 'Cairo')
        self.assertEqual(cairo.other_name, 'القاهرة')
        self.assertEqual(len(cairo.zone_ids), 2)

        maadi_zone = cairo.zone_ids.filtered(lambda z: z.name == 'Maadi & Muqattam')
        self.assertEqual(len(maadi_zone.district_ids), 2)
        muqattam = maadi_zone.district_ids.filtered(lambda d: d.name == 'Muqattam')
        self.assertFalse(muqattam.dropoff_available)
        self.assertTrue(self.instance.last_catalogue_sync_at)

    def test_sync_is_idempotent(self):
        self._sync()
        self._sync()
        self.assertEqual(
            self.env['bosta.city'].search_count([('instance_id', '=', self.instance.id)]),
            2,
        )
        self.assertEqual(self.env['bosta.zone'].search_count([]), 3)

    def test_sync_updates_a_renamed_city(self):
        self._sync()
        renamed = {'success': True, 'data': [
            {'_id': 'c1', 'name': 'Cairo Governorate', 'code': 'EG-01'},
        ]}
        with patch(CITIES_PATH, return_value=renamed), \
             patch(ZONES_PATH, return_value={}):
            self.instance.action_sync_address_catalogue()

        city = self.env['bosta.city'].search([('bosta_id', '=', 'c1')])
        self.assertEqual(city.name, 'Cairo Governorate')
        self.assertEqual(len(city), 1)

    def test_sync_tolerates_alternative_key_spellings(self):
        """Bosta's own products spell these keys differently."""
        payload = {'data': [
            {'cityId': 'cx', 'cityName': 'Alexandria',
             'cityCode': 'EG-03', 'cityOtherName': 'الإسكندرية'},
        ]}
        with patch(CITIES_PATH, return_value=payload), \
             patch(ZONES_PATH, return_value={}):
            self.instance.action_sync_address_catalogue()

        city = self.env['bosta.city'].search([('bosta_id', '=', 'cx')])
        self.assertEqual(city.name, 'Alexandria')
        self.assertEqual(city.code, 'EG-03')

    def test_sync_skips_unusable_records_without_failing(self):
        payload = {'data': [
            {'name': 'No Id City'},
            {'_id': 'ok1', 'name': 'Good City', 'code': 'EG-09'},
            'not-a-dict',
        ]}
        with patch(CITIES_PATH, return_value=payload), \
             patch(ZONES_PATH, return_value={}):
            self.instance.action_sync_address_catalogue()

        self.assertEqual(
            self.env['bosta.city'].search_count([('instance_id', '=', self.instance.id)]),
            1,
        )

    def test_zone_failure_does_not_lose_cities(self):
        """One city's zones failing must not abandon the whole sync."""
        with patch(CITIES_PATH, return_value=CITIES_RESPONSE), \
             patch(ZONES_PATH, side_effect=BostaError("zones down", retryable=True)):
            self.instance.action_sync_address_catalogue()

        self.assertEqual(
            self.env['bosta.city'].search_count([('instance_id', '=', self.instance.id)]),
            2,
        )

    def test_cities_failure_raises_a_readable_error(self):
        from odoo.exceptions import UserError
        with patch(CITIES_PATH, side_effect=BostaError("bad key", status_code=401)):
            with self.assertRaises(UserError):
                self.instance.action_sync_address_catalogue()

    # ------------------------------------------------------------------
    # Resolving an Odoo address
    # ------------------------------------------------------------------

    def _partner(self, **vals):
        base = {'name': 'Addr Customer', 'street': '5 Test St', 'phone': '01000000000'}
        base.update(vals)
        return self.env['res.partner'].create(base)

    def test_city_is_matched_by_english_name(self):
        self._sync()
        partner = self._partner(city='Cairo')
        city, zone, district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(city.code, 'EG-01')
        self.assertEqual(partner.bosta_city_id, city)

    def test_city_is_matched_by_arabic_name(self):
        self._sync()
        partner = self._partner(city='القاهره')
        city, _zone, _district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(city.code, 'EG-01')

    def test_district_is_matched_and_the_zone_derived_from_it(self):
        """An address names a district; the zone is Bosta's grouping of them."""
        self._sync()
        partner = self._partner(city='Cairo', street2='Maadi')
        city, zone, district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(city.code, 'EG-01')
        self.assertEqual(district.name, 'Maadi')
        self.assertEqual(zone.name, 'Maadi & Muqattam')

    def test_zone_is_matched_directly_when_named(self):
        self._sync()
        partner = self._partner(city='Cairo', street2='Nasr City')
        _city, zone, _district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(zone.name, 'Nasr City')

    def test_district_without_dropoff_is_not_matched(self):
        """Bosta does not deliver there, so it must never be selected."""
        self._sync()
        partner = self._partner(city='Cairo', street2='Muqattam')
        _city, _zone, district = partner._resolve_bosta_location(self.instance)
        self.assertFalse(district)

    def test_unknown_city_resolves_to_nothing(self):
        self._sync()
        partner = self._partner(city='Atlantis')
        city, _zone, _district = partner._resolve_bosta_location(self.instance)
        self.assertFalse(city)

    def test_manual_choice_is_never_overwritten(self):
        self._sync()
        giza = self.env['bosta.city'].search([('code', '=', 'EG-02')])
        partner = self._partner(city='Cairo', bosta_city_id=giza.id)
        city, _zone, _district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(city, giza)

    # ------------------------------------------------------------------
    # Booking uses the resolved address
    # ------------------------------------------------------------------

    def _booked_payload(self, partner):
        """Build a shipment payload for a picking going to ``partner``."""
        delivery_product = self.env['product.product'].create({
            'name': 'Addr Shipping', 'type': 'service', 'invoice_policy': 'order',
        })
        carrier = self.env['delivery.carrier'].create({
            'name': 'Bosta Addr', 'delivery_type': 'bosta',
            'bosta_instance_id': self.instance.id, 'integration_level': 'rate',
            'product_id': delivery_product.id,
        })
        product = self.env['product.product'].create({
            'name': 'Addr Item', 'is_storable': True, 'list_price': 50.0,
        })
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'carrier_id': carrier.id,
            'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1})],
        })
        order.action_confirm()
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.picking_type_id.sudo().write({'create_backorder': 'never'})
        picking.button_validate()

        job = self.env['bosta.shipment.job'].search([('picking_id', '=', picking.id)])
        return job

    def test_payload_carries_the_bosta_city_code(self):
        self._sync()
        partner = self._partner(city='Cairo', street2='Maadi')
        job = self._booked_payload(partner)

        payload = job._build_payload()
        self.assertEqual(payload['dropOffAddress']['city'], 'EG-01')
        self.assertEqual(payload['dropOffAddress']['zone'], 'Maadi & Muqattam')
        self.assertEqual(payload['dropOffAddress']['district'], 'Maadi')

    def test_booking_fails_clearly_on_an_unmatched_city(self):
        """A guessed city code would ship the parcel to the wrong governorate."""
        self._sync()
        partner = self._partner(city='Atlantis')
        job = self._booked_payload(partner)

        with patch(
            'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.create_delivery'
        ) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_not_called()

        self.assertEqual(job.state, 'failed')
        self.assertIn('Atlantis', job.last_error_message or '')

    def test_booking_fails_when_the_city_has_no_code(self):
        self._sync()
        city = self.env['bosta.city'].search([('code', '=', 'EG-01')])
        city.sudo().write({'code': False})
        partner = self._partner(city='Cairo')
        job = self._booked_payload(partner)

        with patch(
            'odoo.addons.sdlc_bosta_connector.models.bosta_api.BostaAPI.create_delivery'
        ) as mocked:
            self.env['bosta.shipment.job'].cron_process_shipment_jobs()
        mocked.assert_not_called()
        self.assertEqual(job.state, 'failed')

    # ------------------------------------------------------------------
    # The real shape, confirmed against a live call (2026-11-01)
    #
    # /cities/{id}/zones returns zones with no nested districts at all: each
    # zone IS the leaf deliverable area, named in nameAr (not otherName), and
    # carries its own dropOffAvailability/pickupAvailability directly.
    # ------------------------------------------------------------------

    def test_real_shape_city_uses_nameAr(self):
        payload = {'data': {'list': [
            {'_id': 'rc1', 'name': 'Alexandria', 'nameAr': 'الاسكندريه',
             'code': 'EG-02', 'pickupAvailability': True, 'dropOffAvailability': True},
        ]}}
        with patch(CITIES_PATH, return_value=payload), \
             patch(ZONES_PATH, return_value={'data': []}):
            self.instance.action_sync_address_catalogue()

        city = self.env['bosta.city'].search([('bosta_id', '=', 'rc1')])
        self.assertEqual(city.other_name, 'الاسكندريه')
        self.assertEqual(city.code, 'EG-02')

    def test_real_shape_zone_is_the_leaf_with_its_own_availability(self):
        """No nested districts; the zone itself carries dropOffAvailability."""
        cities = {'data': {'list': [
            {'_id': 'rc1', 'name': 'Cairo', 'nameAr': 'القاهرة', 'code': 'EG-01'},
        ]}}
        zones = {'data': [
            {'_id': 'rz1', 'name': 'ElMaadi', 'nameAr': 'المعادي',
             'pickupAvailability': True, 'dropOffAvailability': True},
            {'_id': 'rz2', 'name': 'SomeClosedArea', 'nameAr': 'منطقة مغلقة',
             'pickupAvailability': True, 'dropOffAvailability': False},
        ]}
        with patch(CITIES_PATH, return_value=cities), \
             patch(ZONES_PATH, return_value=zones):
            self.instance.action_sync_address_catalogue()

        maadi = self.env['bosta.zone'].search([('bosta_id', '=', 'rz1')])
        self.assertEqual(maadi.other_name, 'المعادي')
        self.assertTrue(maadi.dropoff_available)
        self.assertFalse(maadi.district_ids)

        closed = self.env['bosta.zone'].search([('bosta_id', '=', 'rz2')])
        self.assertFalse(closed.dropoff_available)

    def test_real_shape_address_resolves_directly_to_the_zone(self):
        """End-to-end: an address names the zone/neighbourhood directly."""
        cities = {'data': {'list': [
            {'_id': 'rc1', 'name': 'Cairo', 'nameAr': 'القاهرة', 'code': 'EG-01'},
        ]}}
        zones = {'data': [
            {'_id': 'rz1', 'name': 'ElMaadi', 'nameAr': 'المعادي',
             'pickupAvailability': True, 'dropOffAvailability': True},
        ]}
        with patch(CITIES_PATH, return_value=cities), \
             patch(ZONES_PATH, return_value=zones):
            self.instance.action_sync_address_catalogue()

        partner = self._partner(city='Cairo', street2='ElMaadi')
        city, zone, district = partner._resolve_bosta_location(self.instance)
        self.assertEqual(city.code, 'EG-01')
        self.assertEqual(zone.name, 'ElMaadi')
        self.assertFalse(district)

        # Arabic address text matches the same zone via nameAr.
        partner_ar = self._partner(city='القاهرة', street2='المعادي')
        city_ar, zone_ar, _d = partner_ar._resolve_bosta_location(self.instance)
        self.assertEqual(zone_ar, zone)
        self.assertEqual(city_ar, city)

    def test_real_shape_closed_zone_is_never_matched(self):
        cities = {'data': {'list': [
            {'_id': 'rc1', 'name': 'Cairo', 'code': 'EG-01'},
        ]}}
        zones = {'data': [
            {'_id': 'rz2', 'name': 'ClosedArea', 'dropOffAvailability': False},
        ]}
        with patch(CITIES_PATH, return_value=cities), \
             patch(ZONES_PATH, return_value=zones):
            self.instance.action_sync_address_catalogue()

        partner = self._partner(city='Cairo', street2='ClosedArea')
        _city, zone, _district = partner._resolve_bosta_location(self.instance)
        self.assertFalse(zone)
