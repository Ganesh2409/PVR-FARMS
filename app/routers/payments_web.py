"""Customer-facing payment pages and the signed Cashfree webhook.

None of these endpoints decides whether an order is paid. They only *trigger* payments.sync_payment(),
which asks the gateway server-to-server. Query-string flags on the return page are ignored.
"""
import hashlib
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import PAY_EXPIRED, PAY_FAILED, PAY_PENDING, PAY_REFUNDED, PAY_SUCCESS, WebhookEvent
from app.services import payments
from app.services.core import get_all_settings
from app.services.gateways import GatewayError, MockGateway, get_gateway
from app.utils import utcnow
from app.web import templates

log = logging.getLogger("mango.payments.web")
router = APIRouter()
HANDLED_TYPES = {"PAYMENT_SUCCESS_WEBHOOK", "PAYMENT_FAILED_WEBHOOK", "PAYMENT_USER_DROPPED_WEBHOOK"}


def _page(request: Request, db: Session, payment, *, message: str | None = None, mode: str = "pay"):
    s = get_settings()
    farm = get_all_settings(db)["farm_name"]
    order = payment.order
    openable = payment.status in (PAY_PENDING, PAY_FAILED) and order.status == "AWAITING_PAYMENT" \
        and payment.expires_at > utcnow()
    try:
        checkout_mode = get_gateway().checkout_mode
    except GatewayError:
        checkout_mode = "sandbox"
    resp = templates.TemplateResponse(request, "pay.html", {
        "farm": farm, "order": order, "payment": payment, "openable": openable, "message": message, "mode": mode,
        "checkout_mode": checkout_mode, "is_mock": payment.provider == "mock" and not s.is_production,
        "PAY_SUCCESS": PAY_SUCCESS, "PAY_EXPIRED": PAY_EXPIRED, "PAY_REFUNDED": PAY_REFUNDED})
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _load(db: Session, token: str):
    payment = payments.get_by_token(db, token)
    if payment is None:
        raise HTTPException(404, "Payment link not found")
    return payment


@router.get("/pay/{token}", response_class=HTMLResponse)
def pay_page(token: str, request: Request, db: Session = Depends(get_db)):
    payment = _load(db, token)
    if payment.status in (PAY_PENDING, PAY_FAILED):  # refresh truth before showing a pay button
        payment = payments.sync_payment(db, payment.id)
    return _page(request, db, payment)


@router.get("/pay/{token}/return", response_class=HTMLResponse)
def pay_return(token: str, request: Request, db: Session = Depends(get_db)):
    """Where Cashfree sends the customer afterwards. Whatever the URL says, we verify with the gateway."""
    payment = _load(db, token)
    payment = payments.sync_payment(db, payment.id)
    return _page(request, db, payment, mode="return")


@router.post("/pay/{token}/mock/{outcome}")
def pay_mock(token: str, outcome: str, db: Session = Depends(get_db)):
    """Development-only buttons for the mock provider."""
    payment = _load(db, token)
    if payment.provider != "mock" or get_settings().is_production:
        raise HTTPException(404, "Not found")
    if outcome not in ("success", "failed", "dropped", "expired"):
        raise HTTPException(422, "bad outcome")
    MockGateway.simulate(payment.provider_order_id, outcome)
    payments.sync_payment(db, payment.id)
    return RedirectResponse(f"/pay/{token}/return", status_code=303)


# ------------------------------------------------------------------ Cashfree webhook
def _process_payment_event(event_pk: int) -> None:
    with SessionLocal() as db:
        ev = db.get(WebhookEvent, event_pk)
        if ev is None or ev.status != "RECEIVED":
            return
        try:
            order_id = ((ev.payload.get("data") or {}).get("order") or {}).get("order_id")
            payment = payments.get_by_provider_order(db, order_id) if order_id else None
            if payment is not None:
                payments.sync_payment(db, payment.id)
            ev.status = "PROCESSED" if payment is not None else "IGNORED"
        except Exception as exc:
            db.rollback()
            log.exception("payment webhook %s failed", event_pk)
            ev = db.get(WebhookEvent, event_pk)
            ev.status, ev.error = "FAILED", str(exc)[:400]  # the expiry sweeper will still reconcile it
        ev.processed_at = utcnow()
        db.commit()


@router.post("/webhooks/cashfree")
async def cashfree_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    raw = await request.body()  # signature is computed over the exact raw bytes
    try:
        gw = get_gateway()
    except GatewayError:
        raise HTTPException(503, "Payments not configured")
    timestamp = request.headers.get("x-webhook-timestamp")
    if not gw.verify_webhook_signature(raw, request.headers.get("x-webhook-signature"), timestamp):
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    key = request.headers.get("x-idempotency-key") or hashlib.sha256((timestamp or "").encode() + raw).hexdigest()
    try:
        with db.begin_nested():
            ev = WebhookEvent(event_id=f"cf:{key}", event_type="payment", payload=payload)
            db.add(ev)
            db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}  # already received: acknowledge so Cashfree stops retrying
    if payload.get("type") in HANDLED_TYPES:
        background.add_task(_process_payment_event, ev.id)
    else:
        ev.status, ev.processed_at = "IGNORED", utcnow()
        db.commit()
    return {"status": "received"}
