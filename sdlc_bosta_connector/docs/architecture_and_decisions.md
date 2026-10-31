# Bosta Connector — Architecture and Decisions

Last updated: 2026-10-31. Covers `sdlc_bosta_connector` 19.0.2.0.0.

## What this module does

Registers Bosta (Egyptian last-mile courier) as a standard Odoo delivery
method (`delivery.carrier` with `delivery_type = 'bosta'`), so it is
selectable on any sale order exactly like any other carrier — this module
has no dependency on `sdlc_wix_connector` or any other sales channel. It
books a shipment after a delivery is validated, applies Bosta's status
callbacks back onto the delivery, and creates (but never auto-validates) a
return picking when Bosta reports a parcel coming back.

**No real call to Bosta has been made as of this writing.** Every one of the
95 automated tests mocks the HTTP layer. The module is built against Bosta's
published documentation and official SDK source, not against observed live
traffic — see Known Limitations below for exactly what remains unverified.

## Why booking is asynchronous, against Odoo's own default wiring

Odoo's `stock_delivery` module calls `send_to_shipper()` **synchronously,
inline**, from `_send_confirmation_email()` (itself called from
`_action_done()`/`button_validate()`), whenever a carrier's
`integration_level` is `'rate_and_ship'`. That is how every real Odoo
carrier module (UPS, FedEx, Mondial Relay) works, and it is literally what
the framework calls automatically.

This module does not use that path. `delivery_carrier.py` enforces
(`_check_bosta_integration_level`, a model constraint) that a Bosta carrier's
`integration_level` always stays `'rate'`, which suppresses Odoo's automatic
synchronous call entirely. Instead, `stock_picking.py::_action_done` enqueues
a `bosta.shipment.job` row, and a cron books the shipment with bounded
retries (`bosta_shipment_job.py`).

This is a deliberate deviation from Odoo's own intended wiring, chosen for
one reason: a Bosta outage must never block a warehouse worker from
validating a delivery, and a request that times out after Bosta had already
accepted it must not be lost — a synchronous call inside the validation
transaction cannot give you either property. The three standard
provider-contract methods (`bosta_send_shipping`, `bosta_get_tracking_link`,
`bosta_cancel_shipment`) are still implemented in full, for a manual
"Retry Bosta Booking" button on the picking and for any core code that calls
`carrier_id.send_shipping(...)` directly — they are simply not relied on for
the automatic path.

## The Bosta API contract, and how it was actually confirmed

Do not trust secondary sources on this. Multiple third-party Bosta
integration write-ups (and an outdated attempt earlier in this project's own
research) claimed the API lived at `/api/v0` with a `stg-app.bosta.co`
staging host. Both claims turned out to be wrong or unconfirmable:

- The correct, currently documented endpoint is
  **`POST /api/v2/deliveries?apiVersion=1`** — confirmed directly against
  `docs.bosta.co`.
- **No staging host is officially documented.** `stg-app.bosta.co` appears
  in Bosta's own SDK test fixtures and in third-party repositories, but
  never in `docs.bosta.co` itself. `bosta.instance.api_base_url` is
  therefore an explicit, required configuration field defaulting to
  `https://app.bosta.co` — never a hardcoded assumption of a safe test
  environment. If the client's account turns out to have a staging host,
  confirm it with Bosta support before trusting it, then set it here.
- Authentication is the raw API key in the `Authorization` header, with
  **no `Bearer` prefix** — confirmed against the official SDK source
  (`bostaapp/bosta-nodejs`) and covered by
  `test_bosta_api.py::test_authorization_header_has_no_bearer_prefix`.
- Status webhooks are officially supported
  (`docs.bosta.co/docs/how-to/get-delivery-status-via-webhook/`) and can be
  registered **per shipment**, in the create-delivery payload
  (`webhookUrl` + `webhookCustomHeaders`), which is what this module does
  (`bosta_shipment_job.py::_build_payload`) rather than relying on a single
  dashboard-wide webhook setting.
- Bosta does **not** send a webhook on shipment creation — only on
  subsequent state changes. `bosta.tracking.event.job` therefore has a
  separate polling fallback (`cron_reconcile_active_shipments`, inactive by
  default) for shipments that have gone quiet, rather than assuming the
  webhook alone is reliable.

### Still genuinely unconfirmed

- The exact `specs` sub-schema for a v2 create-delivery request. Published
  sources disagree: Bosta's own SDK test fixture sends `specs.size`
  (`"SMALL"`) alongside `packageDetails`; a third-party v2 integration write-up
  sends `packageDetails` alone. `bosta_api.py::BostaAPI.build_specs` sends
  only the consistently-documented `packageDetails` keys
  (`itemsCount`/`description`/`weight`) and deliberately omits `size` and
  `packageType`. This is isolated in one small method on purpose — correct
  it there, once a real response is seen, without touching the job queue or
  retry logic around it.
- The exact field names in the `/cities` and `/cities/{id}/zones` **shipping
  API** responses. The only published example of this shape
  (`cityId`/`cityName`/`districtId`/...) belongs to Bosta's separate
  Fulfillment product (`api-fulfillment.bosta.co`), not the shipping API
  this module calls. `bosta_location.py::BostaCity._parse_city` and
  `BostaZone._upsert`/`BostaDistrict._upsert` therefore accept several
  plausible key spellings (`_id`/`cityId`/`id`, `name`/`cityName`, etc.)
  rather than assuming one. `test_bosta_address.py` covers both spellings
  explicitly so a real response choosing either will not require new code,
  only possibly a field-name correction if a third spelling appears.

## Address resolution: why the district is matched first, not the zone

Bosta's location hierarchy is three levels: city → zone → district
(confirmed — `docs-fulfillment.bosta.co/api-reference/get-zones/`). A zone is
Bosta's own grouping of several districts (e.g. "Maadi & Muqattam" groups the
Maadi and Muqattam districts) — no Odoo customer address ever names a zone,
because nobody writes "Maadi & Muqattam" as their address. They write
"Maadi".

An earlier version of `res_partner.py::_resolve_bosta_location` matched the
zone first, directly against `street2`/`street`, and only then looked for a
district inside that zone. This can never work, because the zone's name is
never present in a real address. It is fixed to match the **district**
first (searched across the whole city, not scoped to a not-yet-known zone),
then derive the zone from `district.zone_id`. A district with
`dropoff_available = False` is never selected, even if its name matches —
Bosta does not deliver there, so silently picking it would book an
undeliverable shipment.

### Arabic/English matching

`bosta_location.py::normalize_name` folds Arabic letter variants that are
written interchangeably in real addresses (`ة`/`ه`, `أ`/`إ`/`آ`/`ا`,
`ى`/`ي`), strips diacritics, and removes `ال`/`al`/`el` prefixes, so
"القاهره" matches a catalogue entry named "Cairo" with `otherName`
"القاهرة". Both the primary name and `otherName` are indexed as separate
stored, searchable fields (`normalized_name` / `normalized_other_name`) —
an earlier version only indexed the primary name, which silently made
Arabic-address matching impossible; this was caught by a test rather than
inspection, since the bug produced no error, just a `False` result.

An ambiguous match (more than one catalogue entry with the same normalized
name) is deliberately left **unmatched** rather than guessed at
(`_unique_match`) — picking one of several candidates would risk shipping a
parcel to the wrong governorate. A manually-set `bosta_city_id` /
`bosta_zone_id` / `bosta_district_id` on the contact is never overwritten by
automatic resolution.

## Why booking fails loudly on an unresolved address

`bosta_shipment_job.py::_build_drop_off_address` raises `BostaError`
(non-retryable) if the customer's city does not resolve against the cached
catalogue, or if the resolved city has no cached `code`. Sending a guessed
or default city code would ship the parcel to the wrong place with no
indication anything went wrong until the customer complains. Refreshing the
catalogue (`bosta.instance.action_sync_address_catalogue`, read-only against
Bosta) or setting the city manually on the contact are the two ways to
resolve this.

## State mapping: unknown codes are recorded, never guessed

`bosta_state.py` holds the numeric state codes Bosta's webhook documentation
publishes (10 = pickup requested, 45 = delivered, 48 = terminated/returned,
49 = canceled, etc.) and their mapping onto this module's own
`bosta_shipment_state` field. An undocumented code is recorded on the
tracking-event job with `state = 'ignored'` and a chatter note naming the
unrecognised code — it is never folded into the nearest known state. Only
`STATE_TERMINATED` (48) is currently treated as "parcel coming back"; if
Bosta's real traffic uses a different code for refusals specifically, add it
to `RETURN_STATES` rather than widening an existing mapping.

## Returns use Odoo's standard wizard, unmodified

When a terminal "returned" state is reported,
`bosta_tracking_event_job.py::_create_return_picking` drives
`stock.return.picking` programmatically — the same wizard the warehouse UI
uses. `_create_return()` calls `action_confirm()` and `action_assign()` on
the reverse picking but **never** `button_validate()`, so the reverse
picking lands `confirmed`/`assigned`, not `done`. This is the intended
behaviour, not an oversight: the parcel has not physically arrived back at
the warehouse yet when the webhook fires, so receiving it stays a human
decision.

One correction worth noting for future maintainers: the wizard computes its
line quantities as `0` by default (the UI expects a person to type them in).
Calling `_create_return()` without setting a quantity raises `"Please
specify at least one non-zero quantity"` — not an API that can be called
blindly. The fix is to copy `move_id.quantity` onto each wizard line before
calling `_create_return()`, which is what `_create_return_picking` does.

## Job queue, logging, and data hygiene

`bosta.shipment.job` and `bosta.tracking.event.job` both follow the same `FOR
UPDATE SKIP LOCKED` + per-job `cr.savepoint()` + bounded-retry pattern as
`sdlc_wix_connector`, and both call `self.env.flush_all()` before their raw
SQL claim query for the same reason documented in the Wix module's doc
(pending ORM writes in the same transaction are otherwise invisible to a
raw-SQL `SELECT`). All three raw-SQL queries are fully parameterized — this
was explicitly checked in the 2026-10-30 security review.

`BostaAPI.sanitize` redacts credential-bearing keys (case-insensitive match)
wherever a request or response is serialized into `bosta.sync.log`,
including `webhookCustomHeaders` — the dict literally containing the
callback secret sent to Bosta — which is redacted as a whole value, not
recursed into. `cron_cleanup_old_logs` (inactive by default) removes only
`state = 'success'` logs older than 30 days; failures are kept indefinitely.

## Known limitations (as of 2026-10-31)

- No live validation against Bosta exists. `shipment_booking_enabled`
  defaults to `False` on every `bosta.instance` specifically so this cannot
  happen by accident even once a key is configured.
- The `specs` schema and the catalogue field names (see above) are the two
  concrete places most likely to need a one-line correction once a real
  Bosta response is seen — both are already isolated for that.
- `bosta_cancel_shipment` raises unconditionally (not implemented). Bosta's
  cancel endpoint and its required fields have not been documented or
  confirmed anywhere this project found.
- COD amount (`bosta_shipment_job.py::_compute_cod`) is computed from the
  *unpaid residual* on posted customer invoices, falling back to the order
  total if no invoice exists yet. This assumes invoices are created and
  posted in the normal course of fulfillment; if this client's invoicing
  timing differs, re-check this method before relying on it for real COD
  amounts.
