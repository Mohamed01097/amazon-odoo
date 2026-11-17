"""Link an Odoo address to Bosta's address catalogue.

Bosta will not accept a free-text city: it needs a city from its own list, a
zone, and a district. These fields hold that resolution so it is visible and
correctable on the contact, rather than guessed afresh on every booking.
"""

import logging
import re

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    bosta_city_id = fields.Many2one(
        'bosta.city', string='Bosta City', index=True,
        help="City from Bosta's catalogue. Matched from the city on this "
             "address when possible; set it here when the names differ.",
    )
    bosta_zone_id = fields.Many2one(
        'bosta.zone', string='Bosta Zone', index=True,
        domain="[('city_id', '=', bosta_city_id)]",
    )
    bosta_district_id = fields.Many2one(
        'bosta.district', string='Bosta District', index=True,
        domain="[('zone_id', '=', bosta_zone_id)]",
    )

    @api.onchange('bosta_city_id')
    def _onchange_bosta_city_id(self):
        if self.bosta_zone_id.city_id != self.bosta_city_id:
            self.bosta_zone_id = False

    @api.onchange('bosta_zone_id')
    def _onchange_bosta_zone_id(self):
        if self.bosta_district_id.zone_id != self.bosta_zone_id:
            self.bosta_district_id = False

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_bosta_location(self, instance, persist=True):
        """Best-effort match of this address against Bosta's catalogue.

        Returns the ``(city, zone, district)`` triple, with empty recordsets
        for whatever could not be matched. An ambiguous name is deliberately
        left unmatched: picking one of several candidates would quietly ship a
        parcel to the wrong area.
        """
        self.ensure_one()
        city = self.bosta_city_id
        zone = self.bosta_zone_id
        district = self.bosta_district_id

        if not city:
            city = self._match_city(instance)
        if city and not district:
            # The district is matched first and the zone taken from it: an
            # address names a district ("Maadi"), while a zone is Bosta's
            # grouping of several ("Maadi & Muqattam"), which no address says.
            district = self._match_district_in_city(city)
        if district and not zone:
            zone = district.zone_id
        if city and not zone:
            zone = self._match_zone(city)

        if persist and (city, zone, district) != (
            self.bosta_city_id, self.bosta_zone_id, self.bosta_district_id,
        ):
            self.sudo().write({
                'bosta_city_id': city.id if city else False,
                'bosta_zone_id': zone.id if zone else False,
                'bosta_district_id': district.id if district else False,
            })
        return city, zone, district

    def _match_city(self, instance):
        self.ensure_one()
        return self._unique_match('bosta.city', [
            ('instance_id', '=', instance.id),
        ], (self.city, self.state_id.name))

    def _match_zone(self, city):
        """Match a zone directly, treating it as the leaf deliverable area.

        Confirmed against this account's real catalogue (2026-11-01): a
        "zone" here is already a neighbourhood (e.g. "ElMaadi"), not a
        grouping of several, and carries its own drop-off availability.

        ``city`` is tried first: a Wix storefront's free-text city field
        routinely carries the actual neighbourhood ("New Cairo 1", "Maadi"),
        while ``street``/``street2`` almost never do -- a customer types a
        street name and number there, not an official area name. Confirmed
        against real orders: zero of the first real Bosta bookings attempted
        had a zone name anywhere in the street fields, and the one that
        booked successfully only did because the zone name was put there by
        hand for a test.
        """
        self.ensure_one()
        return self._unique_match('bosta.zone', [
            ('city_id', '=', city.id),
            ('dropoff_available', '=', True),
        ], (self.city, self.street2, self.street))

    def _match_district_in_city(self, city):
        """Match a district anywhere in the city, not within one zone."""
        self.ensure_one()
        return self._unique_match('bosta.district', [
            ('zone_id.city_id', '=', city.id),
            ('dropoff_available', '=', True),
        ], (self.city, self.street2, self.street))

    def _unique_match(self, model_name, domain, candidates):
        """Return the single record matching one of ``candidates``, or empty.

        Two passes, cheapest first:

        1. Exact match on the normalized name (primary or secondary
           language), candidate by candidate -- unchanged from before.
        2. Whole-word containment: does a candidate's normalized text
           contain a catalogue name as a whole word/phrase, or the other
           way round? This is what makes "New Cairo 1" (a real Wix city
           field) match the catalogue's "New Cairo" zone without an exact
           match -- real addresses almost never equal a catalogue name
           exactly, but they often contain it. Only records at least 3
           normalized characters long are considered, to avoid a short,
           generic name (e.g. a single short word) matching almost anything.

        Both passes return empty, not a guess, the moment more than one
        distinct record matches: picking one of several candidates would
        quietly ship a parcel to the wrong area.
        """
        self.ensure_one()
        Model = self.env[model_name].sudo()
        from .bosta_location import normalize_name

        targets = [normalize_name(candidate) for candidate in candidates]
        targets = [target for target in targets if target]
        if not targets:
            return Model.browse()

        for target in targets:
            found = Model.search(domain + [
                '|',
                ('normalized_name', '=', target),
                ('normalized_other_name', '=', target),
            ])
            if len(found) == 1:
                return found
            if len(found) > 1:
                _logger.info(
                    "Ambiguous Bosta %s match for %r on partner %s; left unset.",
                    model_name, target, self.display_name,
                )
                return Model.browse()

        candidates_pool = Model.search(domain)
        matched = Model.browse()
        for record in candidates_pool:
            for name in (record.normalized_name, record.normalized_other_name):
                if not name or len(name) < 3:
                    continue
                pattern = r'\b' + re.escape(name) + r'\b'
                if any(
                    re.search(pattern, target) or re.search(
                        r'\b' + re.escape(target) + r'\b', name,
                    )
                    for target in targets if len(target) >= 3
                ):
                    matched |= record
                    break
        if len(matched) == 1:
            return matched
        if len(matched) > 1:
            _logger.info(
                "Ambiguous Bosta %s containment match for %r on partner %s; "
                "left unset.", model_name, candidates, self.display_name,
            )
        return Model.browse()

    def action_resolve_bosta_location(self):
        """Re-run matching from the contact form."""
        instance = self.env['bosta.instance'].sudo().search(
            [('active', '=', True)], limit=1,
        )
        for partner in self:
            if instance:
                partner._resolve_bosta_location(instance)
        return True
