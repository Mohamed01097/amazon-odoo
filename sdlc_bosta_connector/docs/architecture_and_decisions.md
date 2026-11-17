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

**First real calls were made on 2026-11-12** (staging, `wix_staging` DB,
client's real production API key, `shipment_booking_enabled` toggled on and
off around a single manual cron run — see the dropOffAddress fix below). All
automated tests still mock the HTTP layer. The module is otherwise built
against Bosta's published documentation and official SDK source, not against
observed live traffic — see Known Limitations below for exactly what
remains unverified.

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

**Confirmed against real `POST /api/v2/deliveries` responses (2026-11-12 and
2026-11-17), across five rejections in a row, all on the district field:**
1. `dropOffAddress.district` is rejected outright —
   `"dropOffAddress must contain at least one of [districtId, districtName]"`.
2. `districtId` using the cached `bosta.district.bosta_id` — **rejected**,
   `"District Not Found"`. For this account, that cached id does not resolve.
3. `districtName` (the district's `name`) alone — **rejected**,
   `"districtName missing required peer cityId"`.
4. `districtName` **plus** `cityId` (`bosta.city.bosta_id`) — **still
   rejected**, `"District Not Found"` again. The cached district *name* does
   not match Bosta's catalogue for this city either.
5. Sending no district field at all, with only `zone` as the leaf area —
   **rejected again**, the exact same message as (1). `zone` alone is
   *never* sufficient; a district of some kind is required on every real
   address tried (Bani Suif, Maadi, New Cairo), not just an edge case.

**The actual root cause, found by counting the cached catalogue: this
account's `bosta.district` table has exactly one row in the whole country**
(28 cities, 561 zones synced from `/cities/{id}/zones`). Bosta's own
`districts` sub-array on that endpoint is populated for essentially no
zones — it is not that our sync is broken, the live data Bosta serves from
that endpoint has almost nothing in it, while `POST /deliveries` demands a
district value almost everywhere.

**Current mitigation in `_build_drop_off_address`: when no district was
actually resolved (which is nearly always, given the above), the matched
*zone's own name* is sent as `districtName`, alongside `cityId`.** This is
explicitly a fallback, not a confirmed-correct value — it has not been
rejected yet, but it is a guess that the zone name happens to also be an
acceptable district name to Bosta. If it ever gets rejected too (e.g.
another "District Not Found"), that means this account's real district
values live somewhere this integration cannot reach via its own API, and
the next step is a real support answer from Bosta, not a sixth guess
against production.

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

### Matching had to move from exact equality to containment, and to the `city` field

The paragraph above ("nobody writes 'Maadi & Muqattam', they write 'Maadi'")
turned out to be only half right once real Wix orders were involved. The
first three real bookings attempted (2026-11-12, 2026-11-17) all failed to
match *any* zone or district, even though the address was real and
deliverable — because none of them had an exact catalogue name in `street`
or `street2` at all. A real customer writes a street name and number there
("شارع التسعين الجنوبى"), not an administrative area name.

What actually carried the area name, in every real case checked, was the
free-text **`city`** field (e.g. "New Cairo 1") — Wix storefronts commonly
collect the neighbourhood there rather than the governorate, and it is
tried as a candidate (first, before `street2`/`street`) in both
`_match_zone` and `_match_district_in_city` for exactly this reason.

Because that text essentially never equals a catalogue name *exactly*
("New Cairo 1" ≠ "New Cairo"), `_unique_match` gained a second pass:
whole-word containment (`\bname\b` found inside the normalized candidate
text, or vice versa), tried only after the exact pass finds nothing. Still
governed by the same rule as exact matching — more than one distinct
containment match is left unmatched, not guessed. Confirmed working
end-to-end: the fourth real booking attempt, after this change, used the
zone matched this way as the `districtName` fallback (see "The Bosta API
contract" above) and was accepted by Bosta with a real tracking number.

Known risk accepted here: a short zone/district name could in principle
contain-match against an unrelated address coincidentally. `_unique_match`
already requires 3+ normalized characters and still refuses to guess on
ambiguity, but this is a heuristic, not a verified identifier — a wrong
containment match is a real mis-shipment risk, not just a failed booking,
unlike the other failure modes in this file.

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

## Multi-company scoping on the address catalogue

`bosta.city`/`bosta.zone`/`bosta.district` initially shipped with access
rights (the CSV) but no `ir.rule`. In a multi-company setup that let a user
in one company read another company's cached Bosta catalogue — not a
credential leak, but a cross-company data leak. Fixed with company-scoped
rules following the chain down to each model's nearest `company_id`:
`bosta.city` via `instance_id.company_id`, `bosta.zone` via
`city_id.instance_id.company_id`, `bosta.district` via
`zone_id.city_id.instance_id.company_id`. Covered by
`test_bosta_multicompany.py`, which deliberately runs as a real non-superuser
user — `ir.rule` is not enforced for the superuser, so a test run as the
default admin user would not have caught this.

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
