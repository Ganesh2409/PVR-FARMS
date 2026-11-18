# Deploying the COD-verification + cancellation update to the existing Railway app

**Nothing has been deployed.** Do these steps yourself, in order, once you are happy.
Never put secrets in this file, in commits, or in chat.

## 0. Before you copy anything
The code was changed in a folder that is **not** a Git repository. Your real repository is on your own computer
(`https://github.com/Ganesh2409/PVR-FARMS`) and may contain edits that are not in this folder. So:

1. In your repo: `git status` and `git log --oneline -5` — commit or stash anything pending first.
2. Copy the files listed below over your repo's copies (compare each with a diff tool such as VS Code "Compare"
   if you changed it yourself; if a file differs in ways you don't recognise, ask before overwriting).
3. Never copy `.env` (it is in `.gitignore`).

### Files changed or added
```
alembic/versions/0002_order_cancellation_and_confirmation_.py   (NEW migration)
app/models.py                      app/i18n.py                     app/web.py
app/routers/admin.py
app/services/cancellation.py       (NEW)
app/services/conversation.py       app/services/orders.py          app/services/payments.py
app/services/notifications.py      app/services/messaging.py       app/services/core.py
app/templates/admin/orders.html    app/templates/admin/order_detail.html
app/templates/admin/overview.html  app/templates/admin/customer_detail.html
tests/test_cod_and_cancellation.py (NEW)   tests/test_admin.py   tests/test_conversation.py
tests/test_orders.py               tests/test_payments.py
README.md   DEVELOPMENT_PROGRESS.md   docs/DEPLOY_CHANGES_TO_RAILWAY.md
```

## 1. Test locally against the TEST database only
```powershell
.venv\Scripts\activate
python -m pytest -q
```
`TEST_DATABASE_URL` must point to `mango_farm_test` (the suite refuses any database whose name does not end in `_test`
and empties its tables). Expected on the development machine: **177 passed**. If your count differs, stop and report it.

## 2. Migration safety review (`0002`)
* Adds **four nullable columns** to `orders`: `cancelled_by`, `cancel_requested_at`, `confirmed_at`, `confirmed_by`.
* No table or column is dropped, renamed or rewritten; existing orders and payments are untouched (verified on a
  PostgreSQL database that held an order and payment: still there, new columns `NULL`; `downgrade` and re-`upgrade` work).
* It runs automatically when the app starts on Railway (`AUTO_MIGRATE=true`, the default). It is quick (metadata only).
* Optional safety net: take a backup first (Railway → your Postgres service → *Backups*, or
  `pg_dump` with the *public* connection string from Railway's Variables tab, run from your PC).

## 3. Environment variables
**No new variables are required** and none need to change. Please check these on Railway (names only; do not paste values anywhere):

| Variable | Why it matters now |
|---|---|
| `PUBLIC_BASE_URL` | must be `https://pvr-farms-production.up.railway.app` — online payment cannot start without it |
| `PAYMENT_PROVIDER` | keep `cashfree` with **sandbox** credentials, or `mock` is **refused** in production (the app will not start). Keep `CASHFREE_ENV=sandbox` until sandbox testing passes |
| `CASHFREE_CLIENT_ID`, `CASHFREE_CLIENT_SECRET` | needed for online payments |
| `ADMIN_PASSWORD`, `SECRET_KEY` | already set (production refuses to start otherwise) |
| `WHATSAPP_*` | unchanged |

> About "online payment could not be started": the old code hid the cause. Now the **Railway deploy logs** show a line
> `online payment could not be started for MF-…: <reason>` and the cancelled order's *cancel reason* in the dashboard
> repeats it. The usual causes are `PUBLIC_BASE_URL` empty/not `https://…`, missing Cashfree credentials, or an HTTP error
> from Cashfree (e.g. 401 wrong key / sandbox key used with `CASHFREE_ENV=production`). No secrets appear in those messages.

COD stays **off** until you switch it on in Admin → Settings *and* allow each customer on their Customer page.
The default farm name in messages is now **PVR Farms**; if you previously saved a different name in Admin → Settings,
change it there.

## 4. Commit and push (from your repo folder)
```powershell
git checkout -b cod-verification-and-cancellation
git add -A
git status          # confirm .env is NOT listed
git commit -m "COD admin verification workflow and customer cancellation with refunds"
git push -u origin cod-verification-and-cancellation
```
Open a pull request on GitHub, look through the file list, then merge into the branch Railway deploys from
(Railway → your service → *Settings → Source* shows the branch; usually `main`).

## 5. How Railway receives it
Railway is connected to the GitHub repository, so **merging into the deployed branch triggers a new build and deploy
automatically** (check *Deployments*). If the service has auto-deploy turned off, press *Redeploy* yourself. On start the app
runs `alembic upgrade head` (applies `0002`), then serves. Watch the deploy logs for `Running upgrade 0001 -> 0002` and
no errors; then open `/health` and the admin dashboard. **Do not approve this deploy until steps 1–3 are done.**
To roll back: Railway → *Deployments* → redeploy the previous one (the extra nullable columns are harmless to old code).

## 6. After deploying — smoke test on production (use a test phone number, small order)
1. `/admin/login` works; Orders shows the new tabs (*Awaiting Confirmation*, *Refunds pending*, …).
2. Enable COD for your own test customer; place a COD order from WhatsApp → you get the "waiting for confirmation" message.
3. Admin → *Awaiting Confirmation* → **Confirm Order** → you get the confirmation message → mark dispatched/delivered.
4. Place another COD order and send `cancel` → answer yes → it is cancelled and stock returns.
5. Do not test real payments/refunds until Cashfree **sandbox** testing is complete.

## 7. WhatsApp template (needed for messages sent more than 24 hours after the customer's last message)
The confirmation, dispatch, cancellation and refund messages are sent as normal messages while the customer's
24-hour window is open (it usually is right after ordering). If the family confirms the next day, Meta only allows an
**approved template**. The code uses one template for every status message (`WHATSAPP_TEMPLATE_ORDER_UPDATE`):

| Field | Value |
|---|---|
| Name | `order_update` (any name; put it in `WHATSAPP_TEMPLATE_ORDER_UPDATE`) |
| Category | **UTILITY** |
| Languages | create **English (`en`)** and **Telugu (`te`)** versions with the same name |
| Variables | 3 text variables |
| English body | `Hello {{1}}, here is an update about your PVR Farms order {{2}}: {{3}} Thank you for choosing PVR Farms.` |
| Telugu body | `నమస్కారం {{1}} గారు, మీ PVR Farms ఆర్డర్ {{2}} గురించిన సమాచారం: {{3}} PVR Farms ను ఎంచుకున్నందుకు ధన్యవాదాలు.` |
| Sample values | `Ravi`, `MF-20261120-0001`, `Your order has been confirmed. We will keep you updated.` |

`{{3}}` receives the full status sentence (line breaks are flattened automatically because Meta rejects them in variables).
Until the template is approved, such late messages are logged as **failed** in `message_logs` (order state is never affected);
the dashboard remains the source of truth. The family's own alert messages to the *admin notify number* also need the
admin to have messaged the business within 24 hours — treat them as a convenience, not a guarantee.
