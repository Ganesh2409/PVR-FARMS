# Fresh Windows computer — complete setup guide

Follow this top to bottom on a clean Windows 10/11 PC. Commands are for **PowerShell**. Everything marked ⚠ is something
that has **not** been verified on a second computer yet (see "Untested" at the end).

## 1. Install the prerequisites

| Need | How | Check |
|---|---|---|
| **Python 3.12 or newer** | https://www.python.org/downloads/windows/ → tick **"Add python.exe to PATH"** | `python --version` |
| **PostgreSQL 16** | https://www.postgresql.org/download/windows/ (EnterpriseDB installer). Keep port **5432**, remember the password you set for the `postgres` superuser. Untick Stack Builder. | `psql --version` (if not found, add `C:\Program Files\PostgreSQL\16\bin` to PATH, reopen PowerShell) |
| **Git** (optional, to copy the project) | https://git-scm.com | `git --version` |
| **cloudflared** (only for Cashfree/WhatsApp tests from your PC) | `winget install Cloudflare.cloudflared` | `cloudflared --version` |

Copy the project folder to the new PC (zip/USB/git), e.g. to `C:\MangoFarm`, and open PowerShell there:

```powershell
cd C:\MangoFarm
```

## 2. Create the PostgreSQL role and databases (once)

The application never uses the `postgres` superuser. Edit **`scripts\create_database.sql`** and replace
`CHANGE_ME_STRONG_PASSWORD` with a password you invent (use letters and digits only to avoid URL-encoding trouble). Then:

```powershell
psql -U postgres -h localhost -v ON_ERROR_STOP=1 -f scripts\create_database.sql
```

It asks for the `postgres` password from the installer. It creates:
* role `mango_app` (login only: no superuser, no create-database, no create-role),
* database `mango_farm` (the app) and `mango_farm_test` (the test-suite; its tables are emptied on every run), both owned by `mango_app`.

Verify (should print `mango_app | f`, the `f` = not superuser):

```powershell
psql -U mango_app -h localhost -d mango_farm -tA -c "select current_user, (select rolsuper from pg_roles where rolname = current_user)"
```

## 3. Python environment and dependencies

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If activation is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then retry.

## 4. Configure environment variables

```powershell
copy .env.example .env
notepad .env
```

Set at least (everything else can stay as is for now):

```
SECRET_KEY=<long random string>
DATABASE_URL=postgresql+psycopg://mango_app:<your db password>@localhost:5432/mango_farm
TEST_DATABASE_URL=postgresql+psycopg://mango_app:<your db password>@localhost:5432/mango_farm_test
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<choose one, 10+ chars>
PAYMENT_PROVIDER=mock          # for the first run; switch to cashfree in step 8
PUBLIC_BASE_URL=http://localhost:8000
```

Generate a secret key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Never commit or share `.env`.

## 5. Create the tables (Alembic migrations)

```powershell
alembic upgrade head
```

(The app also does this automatically on start-up when `AUTO_MIGRATE=true`.) Check: `alembic current` prints `0001 (head)`.

## 6. Run the automated tests

```powershell
python -m pytest -q
```

Expected: all tests pass (137 passed on the development machine). They use `TEST_DATABASE_URL`, run the real migrations, and need **no internet or Cashfree account**.
If it says *"Set TEST_DATABASE_URL"* or *"must end in _test"*, fix `.env` / the variable (`$env:TEST_DATABASE_URL="..."`).

## 7. Start the application

```powershell
uvicorn app.main:app --reload
```

* Customer simulator: http://localhost:8000/simulator — say "Hi", order, and on the payment link click *Simulate successful payment* (mock provider).
* Admin: http://localhost:8000/admin (login from `.env`) → Orders → open the order to see the payment block.
* Health: http://localhost:8000/health

Cash on Delivery is **off**. To try it: Admin → Settings → tick COD, then Admin → Customers → open the customer → allow COD.

## 8. Cashfree Sandbox (real gateway, test money)

See the *Cashfree Sandbox* section in README.md for the exact steps and the official test payment details. In short:
1. Create a free Cashfree merchant account and get the **sandbox/test** App ID and Secret Key from the dashboard.
2. `cloudflared tunnel --url http://localhost:8000` → copy the `https://….trycloudflare.com` URL.
3. In `.env`: `PAYMENT_PROVIDER=cashfree`, `CASHFREE_ENV=sandbox`, `CASHFREE_CLIENT_ID=…`, `CASHFREE_CLIENT_SECRET=…`, `PUBLIC_BASE_URL=<tunnel URL>`; restart uvicorn.
4. In the Cashfree dashboard add the webhook `<tunnel URL>/webhooks/cashfree`.
5. Place an order in the simulator (http://localhost:8000/simulator). The payment link it sends starts with your tunnel URL; open it, pay with a Cashfree **test** method, and watch the order turn confirmed after Cashfree's webhook arrives.

## 9. Things that go wrong

| Symptom | Fix |
|---|---|
| `password authentication failed for user "mango_app"` | wrong password in `DATABASE_URL`; URL-encode special characters (`@` → `%40`) |
| `connection refused` on 5432 | PostgreSQL service not running: `Get-Service postgresql*`, `Start-Service <name>` |
| `permission denied for schema public` | run `psql -U postgres -d mango_farm -c "GRANT ALL ON SCHEMA public TO mango_app"` |
| `ModuleNotFoundError` | venv not activated, or `pip install -r requirements.txt` not run |
| Pay link says *not configured* / order cancelled right away | `PUBLIC_BASE_URL` empty, or Cashfree credentials missing (see admin → Settings → Payment gateway) |
| Webhook returns 401 in Cashfree dashboard test | `CASHFREE_CLIENT_SECRET` not the one for the same (sandbox) environment |

## Untested (be honest in your own notes)

* Everything in this guide on a **second computer** — it has only been exercised on the development machine.
* The **real Cashfree Sandbox**: no sandbox credentials were available while building, so request/response handling was verified only against Cashfree's published documentation and a faked HTTP layer.
* The Cashfree JS checkout page in a real browser, UPI/card test payments, real webhook delivery, real refunds.
* Docker / `docker-compose.yml`.
