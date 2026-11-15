# 🥭 Mango Farm — WhatsApp Ordering System

Bilingual (Telugu / English) WhatsApp ordering for a family mango farm in Govindaraopet, Telangana.
Customers order and pay on WhatsApp (no app, no account); the family manages products, stock, deliveries, payments and orders in a web dashboard.

**Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 + psycopg 3, Alembic, **PostgreSQL**, Jinja2, **Cashfree Payments**, Meta WhatsApp Cloud API.

> **Status:** works end-to-end with the development mock gateway and is tested against PostgreSQL 16 (137 automated tests).
> **Not yet verified:** the real Cashfree Sandbox, real WhatsApp, Docker, and a second computer — see [Untested](#untested--not-production-ready).

* New computer? → **[docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)** (complete step-by-step).
* Progress and limitations → [DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md).

## Quick start (Windows PowerShell, PostgreSQL already set up)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # set DATABASE_URL, SECRET_KEY, ADMIN_PASSWORD; PAYMENT_PROVIDER=mock for a first try
alembic upgrade head          # (also runs automatically at start-up)
uvicorn app.main:app --reload
```

| URL | What |
|---|---|
| http://localhost:8000/simulator | Browser WhatsApp simulator (same engine as real WhatsApp) |
| http://localhost:8000/admin | Admin dashboard |
| http://localhost:8000/health | Health check (also checks the database) |
| http://localhost:8000/docs | API docs (development only) |

## PostgreSQL setup (Windows)

PostgreSQL is the only supported database (development, tests and production). The app connects with a **dedicated, non-superuser role** (`mango_app`); the `postgres` superuser is used once for setup.

1. Install PostgreSQL 16 from https://www.postgresql.org/download/windows/ (remember the `postgres` password; keep port 5432).
2. Edit `scripts\create_database.sql` (choose a password for `mango_app`), then run:
   ```powershell
   psql -U postgres -h localhost -v ON_ERROR_STOP=1 -f scripts\create_database.sql
   ```
3. Put it in `.env` (URL-encode special characters in the password):
   ```
   DATABASE_URL=postgresql+psycopg://mango_app:PASSWORD@localhost:5432/mango_farm
   TEST_DATABASE_URL=postgresql+psycopg://mango_app:PASSWORD@localhost:5432/mango_farm_test
   ```
4. `alembic upgrade head`. Future schema changes: `alembic revision --autogenerate -m "message"` then `alembic upgrade head`.

**Concurrency design (READ COMMITTED + explicit locks):** `create_order` takes `SELECT … FOR UPDATE` locks on the product, inventory and delivery-date rows (always in that order) before checking stock/capacity, and a per-day `pg_advisory_xact_lock` before allocating `MF-YYYYMMDD-XXXX`. Status changes and payment confirmation lock the order/payment rows. Multi-threaded tests prove no overselling, overbooking or double confirmation.

### Running the tests
```powershell
python -m pytest -q
```
Needs `TEST_DATABASE_URL` (database name must end in `_test`; its tables are emptied between tests). The schema is created by the real Alembic migration. No internet, Cashfree or WhatsApp account is needed.

## How ordering and payment work

```
WhatsApp/Simulator ─► conversation engine ─► create_order (stock + date held, status AWAITING_PAYMENT)
        ▲                                         │
        │ confirmation (only after verification)   ▼
   notifications ◄── payments.sync_payment ◄── Cashfree  ◄── customer pays on  https://<your-domain>/pay/<secret-token>
                          ▲  ▲  ▲  ▲                          (our page loads Cashfree's JS checkout with the payment_session_id)
                webhook ──┘  │  │  └── expiry/reconcile sweeper (every 60 s)
          return page ───────┘  └───── admin "re-check with gateway"
```

* **Online payment is the default.** After the summary the customer taps *Confirm & Pay*. The backend creates the order, **holds the stock and delivery capacity**, creates a unique Cashfree order + `payment_session_id` for that checkout (`POST /pg/orders`, API version `2026-01-01` unless `CASHFREE_API_VERSION` is set) and sends a secure link `…/pay/<random token>` on WhatsApp. UPI, cards, net banking and everything else enabled on your Cashfree account appear in Cashfree's checkout.
* **Only the gateway can confirm an order.** The webhook, the browser *return* page, the sweeper and the admin refresh button are only *triggers*; each calls `payments.sync_payment()`, which asks Cashfree server-to-server (`GET /pg/orders/{id}` + `/payments`, using your secret) and requires `order_status = PAID`, a `SUCCESS` payment and the **exact amount**. Query strings, screenshots and chat messages like "I paid" are ignored (the bot just re-checks). Webhook signatures (`x-webhook-signature` = Base64 HMAC-SHA256 of `x-webhook-timestamp + rawBody`, key = client secret) are verified first; bad signatures get HTTP 401 and are not stored.
* **Idempotent.** Duplicate/retried webhooks are de-duplicated by `x-idempotency-key` (or a content hash) in `webhook_events`, and confirmation itself is idempotent under row locks (tested with concurrent threads).
* **Order status** `AWAITING_PAYMENT` → (verified payment) `PENDING` → `ACCEPTED` → `DISPATCHED` → `DELIVERED`. Unpaid orders never appear as new orders and cannot be accepted by the admin.
* **Payment states:** `PENDING`, `SUCCESS`, `FAILED`, `EXPIRED`, `REFUNDED`. Stored per payment: Cashfree order ID, `payment_session_id`, transaction reference (`cf_payment_id`), bank reference, payment method group, amount, paid time, expiry, failure reason, refund id/status.
* **Failures:** a failed attempt marks the payment `FAILED`, tells the customer, and **keeps the stock held until the link expires** so they can retry on the same link (set `PAYMENT_RELEASE_ON_FAILED_ATTEMPT=true` to cancel and release immediately instead).
* **Expiry/cancel:** after `PAYMENT_EXPIRY_MINUTES` (default 30) the sweeper re-checks with Cashfree, and if still unpaid marks the payment `EXPIRED`, cancels the order, **releases the stock/capacity**, terminates the Cashfree order and tells the customer. The customer can also cancel from the chat; a payment that went through a moment earlier is never discarded.
* **Late payment** (money arrives after expiry): the order is revived if stock and date still allow, otherwise the payment is **refunded automatically**.
* **Refunds:** rejecting/cancelling a paid order refunds it in full automatically; admins can also refund manually. Uses `POST /pg/orders/{id}/refunds` with a fixed `refund_id` per order (cannot double-refund) and polls the refund status until the gateway reports success → `REFUNDED`.
* **Cash on Delivery is off by default.** It is offered only when the global switch (Admin → Settings) **and** the per-customer flag (Admin → Customers → customer) are both on.
* **Secrets:** `CASHFREE_CLIENT_SECRET` lives only in the server environment and request headers; the pay page contains only the (non-secret) `payment_session_id`.
* **Development mock:** `PAYMENT_PROVIDER=mock` gives a fake gateway with *Simulate success / failure / expiry* buttons on the pay page. The server refuses to start with it in production.

Other customer behaviour (commands, language switching, 60-minute idle timeout, human-support mode) is unchanged — see the commands: `Hi`/`నమస్కారం`, `back`/`వెనక్కి`, `cancel`/`రద్దు`, `menu`/`మెనూ`, `change language`/`భాష మార్చండి`, `restart`.

## Cashfree Sandbox

You need a Cashfree merchant account (free to create at https://www.cashfree.com; sandbox needs no KYC). **Credentials are never in the repo.**

1. **Get sandbox keys:** Cashfree dashboard → *Developers → API Keys* (Test/Sandbox mode) → generate **App ID** and **Secret Key**. ⚠ Menu names may differ slightly; Cashfree's docs: https://www.cashfree.com/docs/api-reference/authentication
2. **Expose your PC** (Cashfree must reach your webhook; `notify_url` must be HTTPS):
   ```powershell
   cloudflared tunnel --url http://localhost:8000      # prints https://something.trycloudflare.com
   ```
3. **.env:**
   ```
   PAYMENT_PROVIDER=cashfree
   CASHFREE_ENV=sandbox
   CASHFREE_CLIENT_ID=<sandbox App ID>
   CASHFREE_CLIENT_SECRET=<sandbox Secret Key>
   CASHFREE_API_VERSION=            # empty = 2026-01-01
   PUBLIC_BASE_URL=https://something.trycloudflare.com
   ```
   Restart `uvicorn`. Admin → Settings → *Payment gateway* should show provider `cashfree`, mode `sandbox`, credentials ✅.
4. **Register the webhook:** Dashboard → *Developers → Webhooks → Add Webhook URL* → `https://something.trycloudflare.com/webhooks/cashfree` → *Test & Add Webhook*. Enable the payment events (success, failed, user-dropped). Use the newest webhook version offered (docs list `2026-01-01`; the handler only relies on `type`, `data.order.order_id` and verifies everything with the API anyway). Docs: https://www.cashfree.com/docs/api-reference/webhooks/configure-endpoints
5. **Order** in the simulator (`/simulator`) → *Confirm & Pay* → open the link and pay with Cashfree's **official sandbox test data** (https://www.cashfree.com/docs/payments/online/resources/sandbox-environment):

   | Method | Test value | Result |
   |---|---|---|
   | UPI | `testsuccess@gocash` | success |
   | UPI | `testfailure@gocash` | failure |
   | UPI | `testinvalid@gocash` | invalid VPA |
   | Cards | Visa credit `4576238912771450`, expiry `03/2028`, CVV `123`, name `Test`, OTP `111000` | success (more cards on the page above) |
   | Net banking | “TEST Bank” | simulated bank page |

   Note: `success@upi` / `failure@upi` are **not** in Cashfree's official sandbox list. Sandbox does not support PayPal or bank transfer.
6. **What to check:** success → WhatsApp (simulator) shows the confirmation with *Payment Reference*, admin order shows `SUCCESS` with transaction/bank reference; failure → retry message, stock still held, Admin → order → *Re-check with gateway*; wait past expiry (set `PAYMENT_EXPIRY_MINUTES=15` and don't pay) → order auto-cancels and stock is released; admin *Reject* on a paid order → refund (sandbox refunds can be simulated by Cashfree via the refund note, see their refund docs).
7. **Going to production later:** `CASHFREE_ENV=production`, production keys, your real HTTPS domain; Cashfree requires KYC/activation and domain whitelisting for JS checkout (https://www.cashfree.com/docs/payments/online/go-live/whitelist). Re-register the webhook in the *production* dashboard.

## Connecting real WhatsApp (Meta Cloud API)

Unchanged from before: a Meta developer app, WhatsApp Business number, system-user token, public HTTPS URL. Set `WHATSAPP_*` in `.env`, callback `https://your-domain/webhook` (subscribe to `messages`), verify token = `WHATSAPP_VERIFY_TOKEN`. Webhooks without a valid `X-Hub-Signature-256` are rejected. Status messages sent outside the 24-hour window (including payment notifications such as *payment received* when the customer hasn't written recently) need an approved template in `WHATSAPP_TEMPLATE_ORDER_UPDATE` (3 variables: name, order id, text); otherwise they are logged as failed in `message_logs`. Payment links are plain `https://…/pay/<token>` URLs sent as text.

## Deployment (not yet tested)

`docker-compose.yml` runs PostgreSQL (with a dedicated `mango_app` role created by `scripts/docker-init-db.sh`), the app and Caddy (automatic HTTPS). Fill `.env` (see the header of the compose file), then `docker compose up -d --build`. Production refuses to start with the default `SECRET_KEY` or `PAYMENT_PROVIDER=mock`, requires `ADMIN_PASSWORD`, serves `Secure` cookies and hides `/docs` and the simulator. Add delivery dates in Admin → Delivery before opening for orders. Back up the PostgreSQL volume (`pg_dump`) and the uploads volume.

## Security notes
bcrypt passwords · signed HttpOnly SameSite cookies (`Secure` in production) · CSRF on every admin POST · login rate limiting · ORM parametrised queries · WhatsApp and Cashfree webhook HMAC verification + idempotent event table · unguessable pay-link tokens · server-side payment verification with amount check · gateway secrets only in environment/headers · image uploads checked by signature · CSV formula-injection guard · admin audit log (incl. refunds) · non-superuser DB role.

## Untested / not production-ready
* **Cashfree Sandbox and Production:** no credentials were available; the integration follows Cashfree's current docs (API `2026-01-01`) and is tested against a faked HTTP layer and the mock gateway, **not** against Cashfree's servers. Details I could not confirm in the docs: required fields of `customer_details`/`order_meta` (implemented per the API reference summary), minimum order-expiry lead time, behaviour of a webhook "Test" ping, and whether Cashfree sends a refund webhook (the code does not depend on one — it polls `GET /pg/orders/{id}/refunds/{refund_id}`).
* The Cashfree JS checkout page in a real browser; UPI/card flows; refunds with real money.
* Real WhatsApp delivery, templates, and Telugu copy review by a native speaker.
* Docker/`docker-compose`; any machine other than the development PC; PostgreSQL versions other than 16.4.
* Do not enable production payments until the Sandbox checklist above passes on your own account.

## Project layout
```
app/main.py              app factory, migrations, seeding, payment sweeper
app/models.py            SQLAlchemy models (incl. payments)
app/services/            conversation, orders, payments, gateways (Cashfree + mock), catalog, delivery, messaging, notifications, webhook
app/routers/             public APIs, whatsapp (+simulator), payments_web (/pay, /webhooks/cashfree), admin
app/templates/pay.html   customer payment page; app/templates/admin/*  dashboard
alembic/                 migrations          tests/   pytest suite (PostgreSQL)
scripts/                 create_database.sql, docker-init-db.sh      docs/WINDOWS_SETUP.md
```
