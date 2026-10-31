"""Link an Odoo address to Bosta's address catalogue.

Bosta will not accept a free-text city: it needs a city from its own list, a
zone, and a district. These fields hold that resolution so it is visible and
correctable on the contact, rather than guessed afresh on every booking.
"""

import logging

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
        self.ensure_one()
        return self._unique_match('bosta.zone', [
            ('city_id', '=', city.id),
        ], (self.street2, self.street))

    def _match_district_in_city(self, city):
        """Match a district anywhere in the city, not within one zone."""
        self.ensure_one()
        return self._unique_match('bosta.district', [
            ('zone_id.city_id', '=', city.id),
            ('dropoff_available', '=', True),
        ], (self.street2, self.street))

    def _unique_match(self, model_name, domain, candidates):
        """Return the single record matching one of ``candidates``, or empty.

        Both the primary and the secondary-language name are searched, so an
        address written in Arabic matches a catalogue entry named in English
        and the other way round.
        """
        self.ensure_one()
        Model = self.env[model_name].sudo()
        from .bosta_location import normalize_name

        for candidate in candidates:
            target = normalize_name(candidate)
            if not target:
                continue
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
                    model_name, candidate, self.display_name,
                )
                return Model.browse()
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
