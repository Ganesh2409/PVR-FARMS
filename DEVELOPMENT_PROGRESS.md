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
