# Development progress

## Session 1 — Phases 1–6 (code complete locally), Phase 7 prepared

### Completed
* **Phase 1** – architecture: layered app (routers → services → models), single conversation engine shared by simulator and webhook.
* **Phase 2** – all 13 required tables (+ `settings`, `audit_logs`), Alembic migration `0001`, SQLite dev / PostgreSQL prod config, transactional order creation, inventory reservation/release/fulfilment, public read APIs, admin JSON APIs.
* **Phase 3** – deterministic Telugu/English state machine (welcome, language, menu, catalogue with images, quantity, dates, slots, customer details, edit, review, confirm, cancel, back, restart, timeout, human support); browser simulator at `/simulator`.
* **Phase 4** – WhatsApp Cloud API: webhook verification, HMAC signature check, idempotent `webhook_events`, background processing, text/button/list/image/template payloads (Meta limits enforced), delivery-status updates, 24-hour-window logic with template fallback.
* **Phase 5** – admin dashboard: login/CSRF/rate limit, overview, products + image upload + stock, orders (search, filters, CSV, status actions with customer notifications, date change), delivery dates/slots/villages, customers, settings, password change, audit log.
* **Phase 6** – 83 automated tests.
* Phase 7 files only: `Dockerfile`, `docker-compose.yml` (PostgreSQL + Caddy HTTPS), README deployment guide.

### Files
`app/` (main, config, database, models, security, seed, i18n, utils, web, `services/*`, `routers/*`, `templates/*`, `static/style.css`), `alembic/` (+`versions/0001_initial_schema.py`), `tests/` (conftest, test_conversation, test_orders, test_webhook, test_admin), `requirements.txt`, `.env.example`, `.gitignore`, `Dockerfile`, `docker-compose.yml`, `README.md`.

### Tests executed
`python -m pytest -q` → **83 passed** (≈30 s). Covers: Telugu & English journeys, language switching, invalid/decimal quantities, insufficient stock (at selection and at confirmation), unavailable/past/full dates, slots, duplicate webhook delivery, duplicate confirmation, customer cancel (draft and placed), admin reject/cancel with stock release, delivery stock deduction, invalid status transitions, price-snapshot immutability, two real multi-threaded overbooking tests (stock and daily capacity), webhook handshake/signature/idempotency/status updates, payload limits, template fallback outside 24 h, auth/CSRF/rate-limit/access control, CSV export + formula-injection guard, image upload validation.
Manually verified in the browser: simulator Telugu order through to confirmation, then the order appearing in the admin overview.

### Known limitations
* **Not tested against live Meta servers** – no credentials exist yet. Payload shapes follow Meta's documented Cloud API; expect to verify with a real test number (Phase 7).
* **Not tested on PostgreSQL / Docker** – the code uses `SELECT … FOR UPDATE` for it, but only SQLite was exercised here (Docker is not available in this session).
* WhatsApp message templates must be created/approved in Meta Business Manager by you; until then out-of-window status messages are logged as failed.
* Telugu copy is written in conversational Telangana style but should be read by a native speaker (family) before launch.
* Rate limiter is in-memory/per-process. No online payments (COD only; `PaymentProvider` abstraction is in place).
* One product per order (the schema supports multiple `order_items`; the conversation does not).
* Admin UI is English-only; one admin role (no permissions tiers); product photos are stored on local disk.
* Deprecation warning from Starlette's TestClient (`httpx2`) is harmless.

### Remaining tasks (Phase 7)
1. Create Meta developer app, WhatsApp number, system-user token; fill `.env`.
2. Deploy with `docker-compose.yml` on a small VPS/domain; add delivery dates in the dashboard.
3. Register webhook URL, subscribe to `messages`; create/approve the order-update template (te + en).
4. Pilot with a few customers; have family review Telugu wording and add real product photos/prices/stock.
5. Write the short operating guide for the family (accept orders daily, update stock, add dates).

### Exact next step
Run `uvicorn app.main:app --reload`, open `/simulator` and `/admin` (login from your `.env`), and try a few orders; then tell me to proceed with Phase 7 (PostgreSQL/Docker verification and WhatsApp go-live checklist) or request changes to the flow or wording.

---

## Session 2 — PostgreSQL primary database + Cashfree payments

### What changed
* **PostgreSQL only** (`postgresql+psycopg://`, validated at start-up). SQLite code (`BEGIN IMMEDIATE`, batch migrations) removed. Migration `0001` regenerated against PostgreSQL (nothing was deployed, so the old SQLite-generated one was replaced rather than stacked). Explicit locking added: `FOR UPDATE` on product/inventory/delivery-date rows, `pg_advisory_xact_lock` for order codes, row lock on order/payment during transitions. Dedicated non-superuser role `mango_app` (`scripts/create_database.sql`).
* **Cashfree integration** (`app/services/gateways.py`, `payments.py`, `routers/payments_web.py`, `templates/pay.html`): create order, server-side verification, signed webhooks, idempotency, expiry sweeper, failed attempts, cancellation, late payments, refunds, terminate order. Dev-only `MockGateway`.
* **Order model:** new status `AWAITING_PAYMENT`; `payments` table; `customers.cod_allowed`; COD off by default (global setting + per-customer flag). Bot flow gained *Payment method* (only when COD available) and *Payment pending* states; confirmation message now carries the payment line + reference.
* **Admin:** payment block on the order page (Cashfree order id, session, transaction & bank reference, method, paid time, expiry, failures, refund) with *Re-check with gateway* and *Refund*; payment column and *Awaiting payment* filter; COD toggle per customer; payment-gateway panel in Settings.
* **Docs:** README rewritten, `docs/WINDOWS_SETUP.md` (fresh computer guide), `.env.example` (Cashfree variables empty as requested), compose with non-superuser DB role.

### Tests executed
`python -m pytest -q` on **PostgreSQL 16.4** (throwaway local server, non-superuser role, schema built by Alembic): **137 passed**, including 55 payment tests: success, failed attempt + retry, user-dropped, expiry, gateway-reported expiry, sweeper reconciliation, late payment (revive / auto-refund), duplicate & replayed webhooks, concurrent verification (6 threads → one confirmation), signature (valid/invalid/missing/tampered/timestamp), unpaid order never confirmed (spoofed return URL, "I paid" chat, signed-but-false webhook), amount mismatch, stock hold & release, customer cancel (incl. cancel-vs-paid race), refunds (idempotent, pending → complete, gateway failure recorded), COD gating, admin visibility, no secrets in pay page, Cashfree request/response shapes (URL, headers, body, production URL, version override) with faked HTTP.
Manually: mock online order in the simulator → pay page → *Simulate success* → confirmed (app running on PostgreSQL).

### Known limitations / NOT verified
* **Real Cashfree Sandbox never contacted** (no credentials). Unconfirmed from the docs: full `customer_details`/`order_meta` rules, minimum `order_expiry_time`, `Test & Add Webhook` ping format, refund-status endpoint details (taken from search results), whether a refund webhook exists (not used).
* Cashfree JS checkout in a real browser; real UPI/card; real refunds.
* **Second Windows computer:** guide written but not followed by anyone yet.
* Docker/compose untested. Only PostgreSQL 16.4 tested.
* The payment sweeper runs inside the web process (every 60 s); with several workers it runs in each, which is safe (row locks + idempotency) but redundant.
* Webhook timestamps are not age-checked (Cashfree documents no tolerance); replay is harmless because every webhook is re-verified through the API.
* Mock gateway state is in memory (dev only; lost on restart).
* A customer's failed attempt does not release stock until expiry unless `PAYMENT_RELEASE_ON_FAILED_ATTEMPT=true` (deliberate, documented).

### Exact next step
On the other computer follow `docs/WINDOWS_SETUP.md` steps 1–7 (mock provider, `pytest`), then step 8 with your Cashfree **sandbox** keys and a tunnel, and report what differs from the README (especially the first real `POST /pg/orders` response and the first real webhook).


---

## Session 3 - COD admin verification + customer cancellation

### Audit of the code before changing it
* COD already existed but confirmed instantly ("successfully placed") and a placed order could only be cancelled by `cancel MF-...` while COD+`PENDING`. Bare `cancel` only reset the menu. Prepaid-paid, confirmed and dispatched orders could not be cancelled by the customer; customer actions were not audited; there was no cancelled-by / cancel-request record.
* Refund logic itself was sound (`REFUNDED` only after gateway `SUCCESS`) but was triggered only from the admin route after commit (no retry if the app stopped in between).
* Bugs found and fixed: a late payment revived an order the customer/admin had **cancelled** (now refunded; only an *expired* order can be revived); admin-cancelling an unpaid online order left the payment open; checkout failures were not logged ("online payment could not be started" had no visible cause); WhatsApp template variables could contain line breaks (Meta rejects them).

### Changes
* Reused statuses: `PENDING` = *Awaiting Confirmation*, `ACCEPTED` = *Confirmed* (no duplicate `PENDING_CONFIRMATION`). Migration `0002` adds 4 nullable columns to `orders` (`cancelled_by`, `cancel_requested_at`, `confirmed_at`, `confirmed_by`).
* COD: acknowledgement message (EN/TE) instead of confirmation; admin *Awaiting Confirmation* list with Confirm / Reject / View Details; confirmation message on admin confirm; audit trail; idempotent under row locks.
* Cancellation service (`services/cancellation.py`) for cases A-F with confirmation step, gateway re-verification, full refund request for paid orders, dispatched-request flow with admin alert, delivered refusal, duplicate/stale-button handling.
* Central audit in `change_status` (actor, previous -> new status, payment/refund state); refund request/confirm/failure audited; sweeper retries refunds for cancelled paid orders.
* Admin: status labels, new filters (Confirmed, Dispatched, Delivered, Refunds pending, Refunded, Cancel requests), order History, cancel-request banner, overview counters.
* Deployment guide: `docs/DEPLOY_CHANGES_TO_RAILWAY.md` (includes the exact Meta template to create).

### Tests executed
`python -m pytest -q` on PostgreSQL 16.4 (throwaway local server, role `mango_app`, test database only): **177 passed** (137 earlier + 40 new in `tests/test_cod_and_cancellation.py`; 9 older assertions updated for the new wording/flow). Mutation check: three deliberate breakages (revive cancelled order, mark REFUNDED too early, auto-cancel a dispatched order) were each caught by a test. Migration `0002` verified on a database containing an order and payment (data preserved, downgrade/upgrade OK).

### Known limitations / not verified
* **Not verified against real Cashfree, real WhatsApp, Railway, or PostgreSQL 17.** Mocked gateway/messaging only. Cancellation/refund against Cashfree is untested live (refund request/status shapes come from docs).
* The workspace is not a Git repository; the files must be copied into your real repo (see the deployment doc). I could not see local edits you may have made elsewhere.
* Admin alerts to the "notify number" use a plain WhatsApp text and so only arrive inside the admin's 24-hour window; customer messages after 24 h need the approved template (not yet created).
* The customer chat state after an admin confirms later is not changed (customer can always send `cancel`).
* No complaint/return workflow after delivery (by design, later).
* Visual check of the new admin pages was done through rendered-HTML assertions, not in a browser.

### Exact next step
Read `docs/DEPLOY_CHANGES_TO_RAILWAY.md`, copy the listed files into your repo, run `python -m pytest -q` against `mango_farm_test`, then tell me to proceed (or what differs) before anything is pushed or deployed.


---

## Session 4 - friendlier WhatsApp ordering experience

### Why
Testing on real WhatsApp felt tedious and badly arranged: ~14 customer messages / ~16 bot messages per order, repeat customers retyped everything, long text-wall summaries, raw payment URL, a 5-item menu list, a two-step "custom quantity".

### Decisions (confirmed with the family)
Remember returning customers' details with one-tap reuse; trim to essentials (name + one address message, WhatsApp number automatic, village automatic when only one); photo cards with an Order button.

### Changes
* `conversation.py`: new flow (see README "The ordering conversation"); states `SAVED_DETAILS` added; `QUANTITY_CUSTOM`, `PHONE_CONFIRMATION`, `LANDMARK` and the separate alternative-phone step are no longer on the main path (legacy handlers kept so in-flight chats still work); typed kg in the quantity step; typed variety names; every option shown in one turn stays tappable (several product cards).
* `messaging.py`: `OutMessage` gains `header_image` (photo on top of a button message) and the `cta` kind -> WhatsApp `interactive.type = cta_url` (falls back to text for non-HTTPS links).
* `i18n.py`: ~35 strings rewritten/added (EN + TE): step counters 1/5-5/5, emoji structure, shorter wording; button titles verified <= 20 characters.
* Simulator renders photo headers, the Pay button, and numbers options as typed shortcuts.
* `pytest.ini` pins `testpaths = tests`.

### Tests executed
`python -m pytest -q` on PostgreSQL 16.4 (throwaway server, non-superuser role, test database only): **201 passed** = 177 earlier (updated for the new flow; every behavioural assertion kept) + 24 new in `tests/test_ux_flow.py` (message counts, saved details, village auto/ask, photo cards, out-of-stock, typed variety, priced quantity buttons, typed kg, date labels, one-message summary incl. very long address, Pay button payload, **WhatsApp length limits over full EN and TE journeys**). The limits test caught a real defect before release: the payment-method button "Pay online (UPI/card)" was 21 characters (WhatsApp maximum 20), also in Telugu; now shortened.
Visually checked in the browser simulator (English and Telugu): menu, photo cards, quantity buttons, dates, summary card, Pay button.

### Known limitations / not verified
* **Not tried on real WhatsApp yet.** Photo headers on button messages and the `cta_url` button follow Meta's documented Cloud API format, but have only been verified through our payload builder and the simulator. Photos need a public HTTPS `PUBLIC_BASE_URL`.
* Saved details come from the customer's most recent order (any status). A customer who only ever had a rejected order still gets the saved-address shortcut.
* The payment message no longer has a Cancel button (WhatsApp allows only one button type per message); customers send `cancel` and the footer says so.
* Meta's 24-hour rule still applies to messages sent later (needs the approved template).
* WhatsApp "Flows" (a proper address form) were considered and left for later: they need extra Meta setup.

### Exact next step
Try the new flow on your real WhatsApp number after deploying (or in the simulator), tell me what still feels off, and we iterate.
