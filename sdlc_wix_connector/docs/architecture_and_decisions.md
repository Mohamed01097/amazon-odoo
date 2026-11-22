# Wix Connector — Architecture and Decisions

Last updated: 2026-10-31. Covers `sdlc_wix_connector` 19.0.3.0.0.

## What this module does

Receives Wix eCommerce order webhooks (`created`, `updated`, `canceled`),
verifies each one cryptographically, and turns a `created` event into a
confirmed Odoo `sale.order`. `updated`/`canceled` events refresh status
fields on the already-imported order and never touch accounting. Delivery,
invoicing and payment all run through Odoo's standard sale/stock/account
flow, unmodified. Shipping is a normal `sale.order.carrier_id` selection,
assigned the same way for a Wix order as for any other — optionally set
automatically from a per-connection default (see "The delivery carrier is a
connection-level default" below). This module depends on core `delivery`
for that field, but on no specific carrier module (Bosta or otherwise).

## Why the webhook contract looks the way it does

### Signature verification is hand-rolled, not PyJWT

Wix signs every webhook as a single RS256 JWT and delivers it as the entire
request body — no `X-Wix-Signature` header, no JWKS endpoint, no shared
secret (verified against
[dev.wix.com](https://dev.wix.com/docs/build-apps/develop-your-app/access/authentication/verify-requests-received-from-wix)).
Verification lives in `models/wix_api.py::verify_wix_jwt`, built directly on
`cryptography` rather than PyJWT, so the module adds no new runtime
dependency.

Two choices there matter for anyone touching this code later:

- The algorithm is **pinned to RS256 in code**. The JWT's own `alg` header is
  never read. This is what closes the classic RS256→HS256
  algorithm-confusion attack class — there is no code path that would ever
  treat the public key as an HMAC secret.
- Verification runs against the **raw, unparsed request body**. Re-encoding
  the JSON before verifying (e.g. `json.dumps(request.get_json())`) would
  silently break every signature, because JSON re-serialization is not
  guaranteed byte-identical to the original.

A security review (2026-10-30) found no high-confidence vulnerabilities in
this implementation.

### The event envelope shape differs by event type

Wix wraps the order object differently depending on which event fired. All
three are handled in `wix_order_import_job.py::_extract_entity`:

| Event | Where the order object lives |
|---|---|
| `created` | `createdEvent.entity` |
| `updated` | `updatedEvent.currentEntity` (`modifiedFields` also present, unused) |
| `canceled` | `actionEvent.body.order` |

This was verified directly against dev.wix.com's per-event documentation
pages, not assumed. If Wix adds a new order event, add its shape to this
table and to `_extract_entity`'s candidate list — do not assume it matches
one of the three above.

### Deduplication is keyed on the event id, not the order id

An early version of this module keyed the idempotency constraint on
`(instance_id, wix_order_id)`. That is wrong: one order legitimately produces
several events over its life (created, then paid, then maybe cancelled), and
that constraint would silently drop every event after the first. The
constraint is now `UNIQUE (instance_id, wix_event_id)`
(`wix_order_import_job.py`), following Wix's own documented recommendation to
deduplicate on the event's `id` field. A payload with no event id falls back
to `(order_id, event_slug)`, which is weaker but still blocks the common case
of a retried delivery of the same transition.

## Order totals: why shipping/discount/fees/tax are explicit

Odoo's default behaviour is to apply each product's own sales tax when a
sale order line is created. For an imported Wix line this produces the wrong
total: Wix already quotes a final price, and adding Odoo's own tax on top
inflates it. A live end-to-end test during development caught this
concretely — Wix reported a 300 EGP order and Odoo produced 345 EGP.

The fix, in `wix_order_import_job.py`:

- Every imported line's `tax_ids` is explicit: the tax configured on
  `wix.instance.sales_tax_id`, or none at all. The default is **no tax**,
  which makes the Odoo total match Wix's total exactly.
- Shipping (`shippingInfo.cost.price`), the order-level discount
  (`priceSummary.discount` — Wix documents this as the sum of *every*
  discount on the order, so it is taken whole rather than reconstructed from
  individual discount records), and each additional fee become their own
  order lines, using auto-created service products (`WIX-SHIPPING`,
  `WIX-DISCOUNT`, `WIX-FEE`).
- After building the order, `_verify_total` compares `order.amount_total`
  against `priceSummary.total` as Wix reported it. A mismatch beyond
  `total_mismatch_tolerance` sets `wix_review_needed` and posts a chatter
  message explaining the likely cause, rather than silently importing a
  wrong total. `total_mismatch_policy` can be switched to `fail` to refuse
  the import outright instead.

This matters operationally: the order total is what a courier (e.g. Bosta)
collects as COD. A silently wrong total becomes a silently wrong cash
collection.

## The delivery carrier is a connection-level default, not read from Wix

Every imported order gets `wix.instance.default_carrier_id` as its
`carrier_id` (if one is configured) — the same pattern as
`default_warehouse_id`/`default_pricelist_id`/`default_sales_team_id`. This
is what lets a validated delivery already be eligible for the Bosta
connector's dispatch flow (`sdlc_bosta_connector`) without anyone picking a
shipping method by hand on every order.

Wix's own per-order shipping method name (`shippingInfo.title`) is *not*
mapped to a carrier automatically — it only labels the shipping fee line
(see "Order totals" above). A Wix store's named shipping options
("Standard", "Express", a courier's own name) can be renamed or
reconfigured on Wix's side at any time, with nothing here to notice; guessing
a `delivery.carrier` from that string would silently pick the wrong one
(or none) the moment the name changes. One configured default per
connection is simple, visible in one place, and never goes stale behind a
storefront setting no one remembered to keep in sync. If a store ever offers
more than one real shipping option that must route to different carriers,
that needs its own explicit mapping table — don't bolt a guess onto
`shippingInfo.title` instead.

## Cancellation handling is deliberately conservative

`order_cancel_policy` defaults to `flag_only`: a Wix cancellation always sets
`wix_review_needed` and posts a chatter note, but never changes the Odoo
order's state. Switching it to `cancel_when_safe` additionally calls
`order._action_cancel()`, but only when `_cancellation_blockers` finds
nothing in the way — specifically, no validated delivery (`picking.state ==
'done'`) and no posted invoice. Both blockers are checked unconditionally,
even under `cancel_when_safe`; there is no override. The reasoning: a Wix
cancellation that arrives after the warehouse has already shipped the
parcel, or after the customer has been invoiced, describes a real-world
situation Odoo's own order-cancel flow is not designed to unwind safely —
a human needs to look at it.

## Job queue pattern

`wix.order.import.job` mirrors the pattern already established in
`sdlc_amazon_connector/models/amazon_order_import_job.py`: `FOR UPDATE SKIP
LOCKED` row claiming so concurrent cron workers never process the same job
twice, one `cr.savepoint()` per job so one bad payload can't roll back a
whole batch, and bounded exponential-backoff retries
(`MAX_RETRY_COUNT = 5`). The raw SQL claim query is fully parameterized
(`%s` placeholders) — confirmed in the 2026-10-30 security review.

Both `cron_process_order_import_jobs` and the equivalent Bosta job-queue
crons call `self.env.flush_all()` immediately before the raw-SQL claim query.
This is required: because the claim is a bypass of the ORM, a job just
written by the ORM in the same transaction is otherwise invisible to that
SQL statement until flushed. This was caught by a test failure during
development (a job stayed `draft` forever) and is not optional — removing it
reintroduces the bug silently, since it only manifests when a write and a
claim happen in the same transaction.

## The buyer's typed address is written onto the contact

`_resolve_partner` maps `recipientInfo.address` (the address the buyer typed
into Wix at checkout) onto `res.partner.street`/`street2`/`city`/`zip`/
`country_id` — this was the one piece of order data the connector silently
dropped, even though the courier integration (`sdlc_bosta_connector`) reads
the delivery address straight off the contact and cannot book a shipment
without it.

Two deliberate choices:
- **An existing contact (matched by email) never has an address field
  overwritten — only a currently-blank field is filled.** A repeat Wix
  order must not clobber a manually-curated business contact; filling only
  blanks still gets a brand-new contact a usable address without touching
  one a human already fixed up.
- **Confirmed against a real order (2026-11-21, this client's store): the
  actual key is a single combined `addressLine` string**, not the
  documented `addressLine1`/`addressLine2` pair — the real payload had no
  second line at all, just one full address in Arabic. `addressLine1` and
  the alternate `streetAddress: {name, number}` form are kept as fallbacks
  (neither has been seen live), so whichever shape an order actually uses
  still works. The real order's `postalCode` was the literal string `"0"`
  — not a postal code, just what Wix sends when the buyer left it blank —
  so a bare `"0"` is treated as no value, never written to `zip`.
- **`subdivisionFullname` is now mapped to `state_id`**, confirmed by the
  same real order (`"subdivisionFullname": "Giza"` alongside
  `"country": "EG"`). It only sets the field when Odoo already has a
  matching `res.country.state` for that country and name — it never creates
  one or guesses from the `subdivision` ISO code, which is left unused.

### Bosta City/Zone/District are resolved here too, duck-typed

`_resolve_partner` calls `partner._resolve_bosta_location(instance)` right
after the address is set — on both a newly created contact and an existing
one whose blanks were just filled — rather than leaving that for the Bosta
connector's shipment job to do lazily on its own.

Two reasons this moved earlier:
- `bosta.shipment.job._process_job()` runs inside its own savepoint, which
  a failed booking rolls back — including the city/zone/district match it
  just made. A failed attempt used to leave no trace that resolution had
  even run; the only way to see the result was to read the job's error log
  after a real booking attempt.
- Resolving at import time means staff can see — and correct, on the
  contact form — Bosta City/Zone/District right after the order lands,
  before any delivery is validated, instead of only after a shipment
  booking has already failed.

This module still has no manifest dependency on `sdlc_bosta_connector` (see
"What this module does" above) — the call is duck-typed:
`_maybe_resolve_bosta_location` checks `'bosta.instance' not in self.env`
first and is a silent no-op if the Bosta connector isn't installed. A
resolution failure is logged and swallowed, never allowed to fail the order
import itself.

## Data hygiene

`wix.sync.log` grows with every webhook delivery and every import attempt.
`cron_cleanup_old_logs` (default: inactive, like every cron in this module)
deletes only logs in `state = 'success'` older than 30 days. A log in
`failed` or `started` is kept indefinitely — `started` specifically can mean
a crashed job, and deleting it would destroy the evidence needed to diagnose
that crash.

## Known limitations (as of 2026-10-31)

- **The field mapping is built from Wix's published documentation, not from
  live traffic from this client's store.** No real Wix order has been
  processed end-to-end yet (one test order was requested from the client
  specifically to close this gap — see the client-facing requirements doc).
  `_extract_order` parses defensively (`.get()` chains throughout) for this
  reason; a shape mismatch fails one job with a clear error rather than
  crashing the batch, and the raw payload is kept verbatim in
  `payload_json` so a fix can be followed by a retry (`action_retry`)
  without re-sending the webhook.
- Only `created`, `updated`, `canceled` are subscribed/handled. If the
  client's Wix store needs refund or payment-specific events later, they
  need their own envelope-shape entry and their own handling branch —
  do not assume they share `updated`'s shape.
- Tax handling is an accounting decision, not just a technical one. The
  current default (no tax, matching Wix's total exactly) is safe but is not
  necessarily final — see the client-facing requirements doc for the
  open question about Egyptian VAT treatment.
