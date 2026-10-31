"""Bosta's address catalogue, mirrored locally.

Bosta addresses are not free text: a drop-off address carries a city, a zone
and a district drawn from Bosta's own catalogue. Odoo stores addresses as free
text, so the catalogue is cached here and each customer address is linked to
it, either automatically by name or by a human picking from the list.

What is verified and what is not:

* The hierarchy is three levels - city, then zone, then district. Confirmed
  across Bosta's own documentation.
* The shipping API's catalogue paths are ``GET /api/v2/cities`` and
  ``GET /api/v2/cities/{cityId}/zones``.
* The exact response field names for the **shipping** API are NOT confirmed.
  Bosta's published example of this shape belongs to their separate
  Fulfillment product (``api-fulfillment.bosta.co``), so parsing accepts
  several plausible key spellings instead of assuming one. Correct
  :meth:`_parse_city` and :meth:`_parse_zone` once a real response is seen.
"""

import logging
import re
import unicodedata

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Arabic letter forms that differ only cosmetically between how a customer
# types an address and how Bosta spells it.
_ARABIC_FOLD = str.maketrans({
    'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا',
    'ى': 'ي', 'ئ': 'ي',
    'ة': 'ه',
    'ؤ': 'و',
    'ـ': '',
})


def normalize_name(value):
    """Fold a place name so Arabic and English spellings compare equal.

    Matching is by name because Bosta exposes no shared key with Odoo. The
    result is only ever used to *suggest* a match; a wrong suggestion is
    corrected by setting the field on the address.
    """
    if not value:
        return ''
    text = unicodedata.normalize('NFKC', str(value)).strip().lower()
    text = text.translate(_ARABIC_FOLD)
    # Drop Arabic diacritics, which carry no meaning for matching.
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r'\b(al|el|governorate|city|district|zone)\b', ' ', text)
    text = re.sub(r'[^0-9a-z؀-ۿ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


class BostaCity(models.Model):
    _name = 'bosta.city'
    _description = 'Bosta City'
    _order = 'name'

    name = fields.Char(required=True, index=True)
    other_name = fields.Char(help="Name in Bosta's secondary language.")
    code = fields.Char(
        index=True,
        help="Bosta city code, such as EG-01. This is the value sent as "
             "`dropOffAddress.city`.",
    )
    bosta_id = fields.Char(required=True, index=True)
    instance_id = fields.Many2one(
        'bosta.instance', required=True, index=True, ondelete='cascade',
    )
    zone_ids = fields.One2many('bosta.zone', 'city_id', string='Zones')
    normalized_name = fields.Char(compute='_compute_normalized_name', store=True, index=True)
    normalized_other_name = fields.Char(
        compute='_compute_normalized_name', store=True, index=True,
        help="Secondary-language name, folded for matching. Indexed "
             "separately so an Arabic address can be matched too.",
    )

    _city_unique = models.Constraint(
        'UNIQUE (instance_id, bosta_id)',
        'This Bosta city is already cached for this account.',
    )

    @api.depends('name', 'other_name')
    def _compute_normalized_name(self):
        for city in self:
            city.normalized_name = normalize_name(city.name)
            city.normalized_other_name = normalize_name(city.other_name)

    def _matches(self, candidate):
        """Whether a free-text name refers to this city."""
        self.ensure_one()
        target = normalize_name(candidate)
        if not target:
            return False
        return target in {
            self.normalized_name,
            normalize_name(self.other_name),
            normalize_name(self.code),
        }

    # ------------------------------------------------------------------
    # Parsing Bosta's catalogue responses
    # ------------------------------------------------------------------

    @api.model
    def _iter_records(self, payload):
        """Yield the record dicts out of a Bosta list response.

        Bosta wraps lists as ``{"success":…, "data":[…]}``, but a bare list
        and a ``{"data":{"cities":[…]}}`` shape are both tolerated so an
        unexpected wrapper does not silently produce zero records.
        """
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            data = payload.get('data', payload)
            if isinstance(data, list):
                candidates = data
            elif isinstance(data, dict):
                candidates = next(
                    (value for value in data.values() if isinstance(value, list)),
                    [],
                )
            else:
                candidates = []
        else:
            candidates = []
        return [item for item in candidates if isinstance(item, dict)]

    @api.model
    def _parse_city(self, raw):
        """Map one raw city record onto this model's fields.

        Several key spellings are accepted: the shipping API's exact field
        names are unconfirmed, and Bosta's own Fulfillment product uses the
        ``cityId`` / ``cityName`` family while their SDKs use ``_id`` /
        ``name``.
        """
        bosta_id = raw.get('_id') or raw.get('cityId') or raw.get('id')
        name = raw.get('name') or raw.get('cityName')
        if not bosta_id or not name:
            return None
        return {
            'bosta_id': str(bosta_id),
            'name': str(name),
            'other_name': raw.get('otherName') or raw.get('cityOtherName') or False,
            'code': raw.get('code') or raw.get('cityCode') or False,
        }

    @api.model
    def _upsert(self, instance, raw):
        """Create or refresh one cached city."""
        vals = self._parse_city(raw)
        if not vals:
            _logger.warning(
                "Skipped a Bosta city record with no usable id or name: %s",
                sorted(raw) if isinstance(raw, dict) else type(raw).__name__,
            )
            return self.browse()
        city = self.search([
            ('instance_id', '=', instance.id),
            ('bosta_id', '=', vals['bosta_id']),
        ], limit=1)
        if city:
            city.write(vals)
            return city
        return self.create(dict(vals, instance_id=instance.id))

    def _upsert_zones(self, payload):
        """Cache the zones, and their districts, of this city.

        Returns how many zones were written. Handles both a flat zone list
        and the city-with-nested-districts shape.
        """
        self.ensure_one()
        Zone = self.env['bosta.zone']
        District = self.env['bosta.district']
        # Distinct zones, because the fallback shape repeats a zone once per
        # district and the count is reported back to the user.
        seen_zone_ids = set()

        for raw in self._iter_records(payload):
            districts = [d for d in (raw.get('districts') or []) if isinstance(d, dict)]

            # Preferred shape: the record is a zone, optionally carrying its
            # own districts.
            zone = Zone._upsert(self, raw)
            if zone:
                seen_zone_ids.add(zone.id)
                for district_raw in districts:
                    District._upsert(zone, district_raw)
                continue

            # Fallback shape: the record names no zone of its own and instead
            # carries district rows that each name their zone. This is how
            # Bosta's Fulfillment product returns the catalogue.
            for district_raw in districts:
                zone = Zone._upsert(self, {
                    '_id': district_raw.get('zoneId'),
                    'name': district_raw.get('zoneName'),
                    'otherName': district_raw.get('zoneOtherName'),
                })
                if not zone:
                    continue
                seen_zone_ids.add(zone.id)
                District._upsert(zone, district_raw)
        return len(seen_zone_ids)


class BostaZone(models.Model):
    _name = 'bosta.zone'
    _description = 'Bosta Zone'
    _order = 'city_id, name'

    name = fields.Char(required=True, index=True)
    other_name = fields.Char()
    bosta_id = fields.Char(index=True)
    city_id = fields.Many2one(
        'bosta.city', required=True, index=True, ondelete='cascade',
    )
    instance_id = fields.Many2one(
        related='city_id.instance_id', store=True, index=True,
    )
    district_ids = fields.One2many('bosta.district', 'zone_id', string='Districts')
    normalized_name = fields.Char(compute='_compute_normalized_name', store=True, index=True)
    normalized_other_name = fields.Char(
        compute='_compute_normalized_name', store=True, index=True,
    )
    display_name_full = fields.Char(compute='_compute_display_name_full')

    @api.depends('name', 'other_name')
    def _compute_normalized_name(self):
        for zone in self:
            zone.normalized_name = normalize_name(zone.name)
            zone.normalized_other_name = normalize_name(zone.other_name)

    @api.depends('name', 'city_id.name')
    def _compute_display_name_full(self):
        for zone in self:
            zone.display_name_full = f"{zone.city_id.name} / {zone.name}"

    def _matches(self, candidate):
        self.ensure_one()
        target = normalize_name(candidate)
        if not target:
            return False
        return target in {self.normalized_name, normalize_name(self.other_name)}

    @api.model
    def _upsert(self, city, raw):
        bosta_id = raw.get('_id') or raw.get('zoneId') or raw.get('id')
        name = raw.get('name') or raw.get('zoneName')
        if not name:
            return self.browse()
        vals = {
            'name': str(name),
            'bosta_id': str(bosta_id) if bosta_id else False,
            'other_name': raw.get('otherName') or raw.get('zoneOtherName') or False,
        }
        domain = [('city_id', '=', city.id)]
        domain += (
            [('bosta_id', '=', vals['bosta_id'])] if vals['bosta_id']
            else [('name', '=', vals['name'])]
        )
        zone = self.search(domain, limit=1)
        if zone:
            zone.write(vals)
            return zone
        return self.create(dict(vals, city_id=city.id))


class BostaDistrict(models.Model):
    _name = 'bosta.district'
    _description = 'Bosta District'
    _order = 'zone_id, name'

    name = fields.Char(required=True, index=True)
    other_name = fields.Char()
    bosta_id = fields.Char(index=True)
    zone_id = fields.Many2one(
        'bosta.zone', required=True, index=True, ondelete='cascade',
    )
    instance_id = fields.Many2one(
        related='zone_id.instance_id', store=True, index=True,
    )
    dropoff_available = fields.Boolean(
        default=True,
        help="Whether Bosta delivers to this district. A district without "
             "drop-off availability must not be used for a shipment.",
    )
    pickup_available = fields.Boolean(default=True)
    normalized_name = fields.Char(compute='_compute_normalized_name', store=True, index=True)
    normalized_other_name = fields.Char(
        compute='_compute_normalized_name', store=True, index=True,
    )

    @api.depends('name', 'other_name')
    def _compute_normalized_name(self):
        for district in self:
            district.normalized_name = normalize_name(district.name)
            district.normalized_other_name = normalize_name(district.other_name)

    def _matches(self, candidate):
        self.ensure_one()
        target = normalize_name(candidate)
        if not target:
            return False
        return target in {self.normalized_name, normalize_name(self.other_name)}

    @api.model
    def _upsert(self, zone, raw):
        bosta_id = raw.get('districtId') or raw.get('_id') or raw.get('id')
        name = raw.get('districtName') or raw.get('name')
        if not name:
            return self.browse()
        vals = {
            'name': str(name),
            'bosta_id': str(bosta_id) if bosta_id else False,
            'other_name': (
                raw.get('districtOtherName') or raw.get('otherName') or False
            ),
            'dropoff_available': bool(raw.get('dropOffAvailability', True)),
            'pickup_available': bool(raw.get('pickupAvailability', True)),
        }
        domain = [('zone_id', '=', zone.id)]
        domain += (
            [('bosta_id', '=', vals['bosta_id'])] if vals['bosta_id']
            else [('name', '=', vals['name'])]
        )
        district = self.search(domain, limit=1)
        if district:
            district.write(vals)
            return district
        return self.create(dict(vals, zone_id=zone.id))
