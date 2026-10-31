import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class BostaInstance(models.Model):
    _name = 'bosta.instance'
    _description = 'Bosta Account Connection'
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
    )

    # Credentials. The exact authentication scheme is not yet verified against
    # Bosta's API documentation, so both a key and a token field are provided
    # and either may be the one actually used.
    api_key = fields.Char(
        'API Key',
        help="Bosta API credential used to authenticate outbound calls.",
    )
    api_token = fields.Char(
        'API Token',
        help="Bosta bearer token, if the account authenticates with a token "
             "rather than a static API key.",
    )
    webhook_secret = fields.Char(
        'Webhook Secret',
        help="Shared secret used to verify inbound Bosta tracking webhooks. "
             "Consumed by a later phase.",
    )

    # Bosta does not publish a staging base URL, so the host is explicit
    # configuration rather than something this module infers.
    api_base_url = fields.Char(
        'API Base URL',
        default='https://app.bosta.co',
        required=True,
        help="Host only, without the /api/v2 path. Bosta does not publish a "
             "staging host; if your account has one, enter it here and confirm "
             "it with Bosta support before booking real shipments.",
    )
    shipment_booking_enabled = fields.Boolean(
        'Enable Shipment Booking',
        default=False,
        help="Explicit gate for calling Bosta to create shipments. Keep this "
             "off until the credentials and address mapping are validated: "
             "booked shipments are real and are collected by a courier.",
    )

    webhook_url = fields.Char(
        'Status Webhook URL',
        help="Public HTTPS URL Bosta posts status changes to. Registered per "
             "shipment when a booking is created.",
    )
    webhook_auth_header_name = fields.Char(
        'Webhook Auth Header Name',
        default='Authorization',
        help="Header name Bosta sends back on each status callback.",
    )

    @property
    def _sensitive_fields(self):
        return ('api_key', 'api_token', 'webhook_secret')

    def _get_api_base_url(self):
        self.ensure_one()
        return (self.sudo().api_base_url or '').strip()

    def _get_api_key(self):
        """Return the credential used for the Authorization header."""
        self.ensure_one()
        secure = self.sudo()
        return (secure.api_key or secure.api_token or '').strip()

    def _get_webhook_url(self):
        self.ensure_one()
        return (self.sudo().webhook_url or '').strip() or False

    def _get_webhook_headers(self):
        """Headers Bosta must echo back, used to authenticate its callbacks."""
        self.ensure_one()
        secure = self.sudo()
        secret = (secure.webhook_secret or '').strip()
        header_name = (secure.webhook_auth_header_name or 'Authorization').strip()
        if not secret or not header_name:
            return False
        return {header_name: secret}

    pickup_location_code = fields.Char(
        'Bosta Pickup Location Code',
        help="Identifier Bosta assigns to the physical pickup location. Sent "
             "with every shipment request.",
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Odoo Warehouse',
        check_company=True,
        domain="[('active', '=', True), ('company_id', '=', company_id)]",
        help="Odoo warehouse this Bosta pickup location corresponds to.",
    )

    default_cod_enabled = fields.Boolean(
        'Cash on Delivery by Default',
        default=True,
        help="Whether shipments created for this account request COD "
             "collection unless overridden.",
    )

    carrier_ids = fields.One2many(
        'delivery.carrier', 'bosta_instance_id', string='Delivery Methods',
    )

    city_ids = fields.One2many('bosta.city', 'instance_id', string='Cities')
    city_count = fields.Integer(compute='_compute_catalogue_counts')
    zone_count = fields.Integer(compute='_compute_catalogue_counts')
    last_catalogue_sync_at = fields.Datetime(readonly=True)

    def _compute_catalogue_counts(self):
        for instance in self:
            instance.city_count = self.env['bosta.city'].search_count(
                [('instance_id', '=', instance.id)])
            instance.zone_count = self.env['bosta.zone'].search_count(
                [('instance_id', '=', instance.id)])

    # ------------------------------------------------------------------
    # Address catalogue
    # ------------------------------------------------------------------

    def action_sync_address_catalogue(self):
        """Refresh the cached Bosta city, zone and district lists.

        Read-only against Bosta: it books nothing and changes nothing on
        their side, so it is safe to run before shipment booking is enabled.
        """
        self.ensure_one()
        return self._sync_address_catalogue()

    def _sync_address_catalogue(self):
        from .bosta_api import BostaAPI, BostaError

        self.ensure_one()
        api = BostaAPI(self._get_api_base_url(), self._get_api_key())
        log = self.env['bosta.sync.log'].log_start(self, 'catalogue_sync')

        try:
            cities_payload = api.get_cities()
        except BostaError as exc:
            log.log_fail(str(exc), http_status=exc.status_code,
                         retryable=exc.retryable, response_data=exc.payload)
            raise UserError(_("Could not read Bosta cities: %s", exc)) from exc

        City = self.env['bosta.city']
        cities = 0
        zones = 0
        for raw in City._iter_records(cities_payload):
            city = City._upsert(self, raw)
            if not city:
                continue
            cities += 1
            # Zones are fetched per city; a failure on one city must not
            # abandon the cities already cached.
            try:
                zones_payload = api.get_city_zones(city.bosta_id)
            except BostaError as exc:
                _logger.warning(
                    "Could not read Bosta zones for city %s: %s", city.name, exc,
                )
                continue
            zones += city._upsert_zones(zones_payload)

        self.sudo().write({'last_catalogue_sync_at': fields.Datetime.now()})
        summary = _("Cached %(cities)s cities and %(zones)s zones.",
                    cities=cities, zones=zones)
        log.log_success(summary)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Bosta Address Catalogue"), 'message': summary,
                       'type': 'success'},
        }

    @api.model
    def cron_sync_address_catalogues(self):
        """Refresh every active account's catalogue.

        Bosta recommends refreshing roughly daily; a stale catalogue is the
        main way a previously valid address stops resolving.
        """
        for instance in self.search([('active', '=', True)]):
            if not instance._get_api_key():
                continue
            try:
                with self.env.cr.savepoint():
                    instance._sync_address_catalogue()
            except Exception:  # noqa: BLE001 - one account must not stop the rest
                _logger.exception(
                    "Bosta catalogue sync failed for account %s", instance.name,
                )
        return True

    @api.constrains('company_id', 'warehouse_id')
    def _check_warehouse_company(self):
        for instance in self:
            if (
                instance.warehouse_id
                and instance.warehouse_id.company_id != instance.company_id
            ):
                raise ValidationError(_(
                    "The Odoo Warehouse must belong to the Bosta connection company."
                ))
