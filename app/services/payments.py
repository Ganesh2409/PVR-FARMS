"""Online payment lifecycle. Single rule: an order is confirmed ONLY by sync_payment(), which asks the
gateway itself (server-to-server, with our credentials) whether the payment succeeded and for what amount.
Webhooks, the browser return page, the expiry sweeper and the admin "refresh" button are all just
*triggers* for sync_payment; none of them can mark anything paid on its own, and each is idempotent.

Payment states: PENDING -> SUCCESS -> REFUNDED ; PENDING -> FAILED (an attempt failed; may still retry
until expiry) ; PENDING|FAILED -> EXPIRED (expired / cancelled / terminated) ; FAILED -> SUCCESS (retry paid).
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (AWAITING_PAYMENT, CANCELLED, PAY_EXPIRED, PAY_FAILED, PAY_PENDING, PAY_REFUNDED, PAY_SUCCESS,
                        PENDING, Order, Payment)
from app.services import notifications
from app.services.core import audit
from app.services.gateways import GatewayError, GatewayStatus, get_gateway
from app.services.orders import OrderError, change_status, revive_cancelled_order
from app.utils import utcnow

log = logging.getLogger("mango.payments")
EXPIRY_GRACE = timedelta(seconds=45)  # let Cashfree's own expiry / an in-flight payment land first
FAILED_ATTEMPT = {"FAILED", "USER_DROPPED", "CANCELLED", "VOID"}


class PaymentError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def pay_url(payment: Payment) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/pay/{payment.checkout_token}"


def _phone10(order: Order) -> str:
    p = order.contact_phone
    return p[-10:] if len(p) >= 10 else p


# ------------------------------------------------------------------ checkout
def start_checkout(db: Session, order: Order) -> Payment:
    """Create the payment record and the gateway order/session for an AWAITING_PAYMENT order.
    On failure the caller must cancel the order (releasing stock)."""
    s = get_settings()
    if not s.public_base_url.startswith("http"):
        log.error("Online payment cannot start for %s: PUBLIC_BASE_URL is not set", order.order_code)
        raise PaymentError("not_configured", "PUBLIC_BASE_URL is required for online payments")
    try:
        gw = get_gateway()
    except GatewayError as exc:
        log.error("Online payment cannot start for %s: %s", order.order_code, exc)
        raise PaymentError("not_configured", str(exc)) from exc
    base = s.public_base_url.rstrip("/")
    payment = Payment(
        order_id=order.id, provider=gw.name, provider_order_id=order.order_code,
        checkout_token=secrets.token_urlsafe(32), amount=order.total, status=PAY_PENDING,
        expires_at=utcnow() + timedelta(minutes=s.payment_expiry_minutes))
    db.add(payment)
    db.commit()
    try:
        g = gw.create_order(
            provider_order_id=order.order_code, amount=order.total, customer_id=f"C{order.customer_id}",
            customer_phone=_phone10(order), customer_name=order.customer_name,
            return_url=f"{base}/pay/{payment.checkout_token}/return", notify_url=f"{base}/webhooks/cashfree",
            expires_at=payment.expires_at, note=f"Mango Farm order {order.order_code}")
    except GatewayError as exc:
        log.error("create_order failed for %s: %s", order.order_code, exc)
        payment.status, payment.closed_reason, payment.failure_reason = PAY_FAILED, "FAILED", str(exc)[:300]
        db.commit()
        raise PaymentError("gateway_error", str(exc)) from exc
    payment.cf_order_id, payment.payment_session_id = g.cf_order_id, g.payment_session_id
    payment.gateway_order_status = g.order_status
    db.commit()
    return payment


def abort_unpaid_order(db: Session, order: Order, reason: str) -> None:
    """Checkout could not even start: cancel the order and release its stock."""
    try:
        change_status(db, order, CANCELLED, reason)
    except OrderError:
        pass


# ------------------------------------------------------------------ verification (the only place orders get confirmed)
def get_by_token(db: Session, token: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.checkout_token == token))


def get_by_provider_order(db: Session, provider_order_id: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.provider_order_id == provider_order_id))


def sync_payment(db: Session, payment_id: int) -> Payment:
    """Ask the gateway for the truth and apply it. Safe to call any number of times, from anywhere."""
    payment = db.get(Payment, payment_id)
    if payment.status in (PAY_REFUNDED,):
        return payment
    try:
        gs = get_gateway().get_status(payment.provider_order_id)
    except GatewayError as exc:
        log.warning("status check failed for %s: %s", payment.provider_order_id, exc)
        db.rollback()
        return db.get(Payment, payment_id)
    events = _apply_status(db, payment_id, gs)
    for fn, *args in events:  # side effects run after the transaction commits
        try:
            fn(db, *args)
        except Exception:
            log.exception("post-payment action failed")
            db.rollback()
    return db.get(Payment, payment_id)


def _apply_status(db: Session, payment_id: int, gs: GatewayStatus) -> list:
    """Locks payment then order, applies the verified gateway state, commits. Returns deferred side effects."""
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update()
                        .execution_options(populate_existing=True))
    order = db.scalar(select(Order).where(Order.id == payment.order_id).with_for_update()
                      .execution_options(populate_existing=True))
    payment.gateway_order_status = gs.order_status
    events: list = []
    paid = next((p for p in gs.payments if p.status == "SUCCESS"), None)

    if gs.order_status == "PAID" and paid is not None:
        if payment.status in (PAY_SUCCESS, PAY_REFUNDED):
            db.commit()
            return events  # duplicate notification: nothing to do
        if gs.order_amount != payment.amount or paid.amount != payment.amount:
            payment.needs_attention = f"AMOUNT_MISMATCH expected {payment.amount} got {gs.order_amount}/{paid.amount}"
            log.error("amount mismatch for %s: %s", payment.provider_order_id, payment.needs_attention)
            db.commit()
            events.append((notifications.notify_admin_text, f"⚠️ Payment amount mismatch on {order.order_code}. Check the gateway."))
            return events
        payment.status, payment.paid_at = PAY_SUCCESS, paid.completed_at or utcnow()
        payment.cf_payment_id, payment.bank_reference, payment.payment_group = (
            paid.cf_payment_id, paid.bank_reference, paid.payment_group)
        payment.failure_reason = None
        order.payment_status = "PAID"
        if order.status == AWAITING_PAYMENT:
            order.status = PENDING
            events.append((notifications.notify_order_confirmed, order.id))
        elif (order.status == CANCELLED and payment.closed_reason == "EXPIRED"
              and revive_cancelled_order(db, order)):  # never revive an order someone chose to cancel
            payment.closed_reason = None
            events.append((notifications.notify_order_confirmed, order.id))
        else:
            payment.closed_reason = "LATE_PAYMENT"
            events.append((_auto_refund, payment.id, "Payment received after the order was cancelled"))
        db.commit()
        return events

    if payment.status in (PAY_SUCCESS, PAY_REFUNDED):
        db.commit()
        return events

    if payment.closed_reason:  # already closed (expired / cancelled / failed): keep the original reason
        db.commit()
        return events

    if gs.order_status in ("EXPIRED", "TERMINATED", "TERMINATION_REQUESTED"):
        events += _close(db, payment, order, "EXPIRED" if gs.order_status == "EXPIRED" else "CANCELLED")
        return events
    if utcnow() > payment.expires_at + EXPIRY_GRACE:
        events += _close(db, payment, order, "EXPIRED", terminate=True)
        return events

    # still open: record the latest failed attempt (customer may retry until expiry)
    failed = [p for p in gs.payments if p.status in FAILED_ATTEMPT]
    if failed:
        last = failed[-1]
        marker = f"{last.cf_payment_id}: {last.message or last.status}"
        if payment.failure_reason != marker:
            payment.failure_reason, payment.status = marker, PAY_FAILED
            if get_settings().payment_release_on_failed_attempt:
                events += _close(db, payment, order, "FAILED", terminate=True)
                return events
            events.append((notifications.notify_payment_failed_attempt, order.id))
    db.commit()
    return events


def _close(db: Session, payment: Payment, order: Order, reason: str, terminate: bool = False, *,
           actor: str = "system", cancelled_by: str = "SYSTEM") -> list:
    """Payment will never succeed: mark it closed and cancel the unpaid order, releasing its stock."""
    events: list = []
    payment.status = PAY_EXPIRED if reason in ("EXPIRED", "CANCELLED") else PAY_FAILED
    payment.closed_reason = reason
    if order.status == AWAITING_PAYMENT:
        text = {"EXPIRED": "Payment expired", "CANCELLED": "Payment cancelled", "FAILED": "Payment failed"}[reason]
        change_status(db, order, CANCELLED, text, commit=False, actor=actor, cancelled_by=cancelled_by)
        events.append((notifications.notify_payment_closed, order.id, reason))
    db.commit()
    if terminate:
        events.append((_terminate, payment.id))
    return events


def _terminate(db: Session, payment_id: int) -> None:
    p = db.get(Payment, payment_id)
    try:
        get_gateway().terminate_order(p.provider_order_id)
    except GatewayError as exc:
        log.info("terminate_order not applied for %s: %s", p.provider_order_id, exc)  # best effort


def cancel_checkout(db: Session, order_id: int, *, actor: str = "system", cancelled_by: str = "SYSTEM") -> str:
    """Customer cancels an unpaid online order. Verifies with the gateway first so a payment that
    just went through is never discarded. Returns 'cancelled' or 'paid'."""
    order = db.get(Order, order_id)
    payment = order.payment
    if payment is None or order.status != AWAITING_PAYMENT:
        return "paid" if order.payment_status == "PAID" else "cancelled"
    payment = sync_payment(db, payment.id)
    db.refresh(order)
    if payment.status == PAY_SUCCESS:
        return "paid"
    if order.status == AWAITING_PAYMENT:
        events = []
        p = db.scalar(select(Payment).where(Payment.id == payment.id).with_for_update().execution_options(populate_existing=True))
        o = db.scalar(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))
        if o.status == AWAITING_PAYMENT and p.status != PAY_SUCCESS:
            events = _close(db, p, o, "CANCELLED", terminate=True, actor=actor, cancelled_by=cancelled_by)
        else:
            db.rollback()
        for fn, *args in events:
            if fn is not notifications.notify_payment_closed:  # customer asked for this; no extra message
                fn(db, *args)
    return "cancelled"


def _auto_refund(db: Session, payment_id: int, note: str) -> None:
    refund_payment(db, payment_id, note)


# ------------------------------------------------------------------ refunds
def refund_payment(db: Session, payment_id: int, note: str = "Refund", actor: str = "system") -> Payment:
    """Full refund of a verified payment. Idempotent: the refund_id is fixed per payment."""
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update()
                        .execution_options(populate_existing=True))
    if payment.status != PAY_SUCCESS:
        db.rollback()
        raise PaymentError("not_refundable", f"payment is {payment.status}")
    if payment.refund_id and payment.refund_status not in (None, "FAILED", "REJECTED", "CANCELLED"):
        db.commit()
        return payment  # already requested
    payment.refund_id = payment.refund_id or f"RF-{payment.provider_order_id}"
    payment.refund_amount, payment.refund_note = payment.amount, note[:100]
    try:
        r = get_gateway().create_refund(payment.provider_order_id, payment.refund_id, payment.amount, note)
    except GatewayError as exc:
        payment.refund_status = "FAILED"
        payment.needs_attention = f"REFUND_FAILED: {str(exc)[:150]}"
        audit(db, actor, "refund_failed", "order", payment.provider_order_id, str(exc)[:200])
        db.commit()
        raise PaymentError("gateway_error", str(exc)) from exc
    _set_refund_status(db, payment, r.status)
    audit(db, actor, "refund_requested", "order", payment.provider_order_id,
          f"{payment.refund_id} amount={payment.amount} gateway_status={r.status} | {note}")
    db.commit()
    return payment


def _set_refund_status(db: Session, payment: Payment, status: str) -> None:
    payment.refund_status = status
    if status == "SUCCESS":
        payment.status, payment.refunded_at, payment.needs_attention = PAY_REFUNDED, utcnow(), None
        payment.order.payment_status = "REFUNDED"


def sync_refund(db: Session, payment_id: int) -> Payment:
    payment = db.get(Payment, payment_id)
    if not payment.refund_id or payment.status == PAY_REFUNDED:
        return payment
    try:
        r = get_gateway().get_refund(payment.provider_order_id, payment.refund_id)
    except GatewayError as exc:
        log.warning("refund status check failed: %s", exc)
        return payment
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update().execution_options(populate_existing=True))
    if payment.status != PAY_REFUNDED:
        _set_refund_status(db, payment, r.status)
        if payment.status == PAY_REFUNDED:
            audit(db, "system", "refund_confirmed", "order", payment.provider_order_id,
                  f"{payment.refund_id} confirmed by gateway")
        db.commit()
        if payment.status == PAY_REFUNDED:
            notifications.notify_refunded(db, payment.order_id)
    return payment


def refund_if_paid(db: Session, order: Order, note: str, actor: str = "system") -> None:
    """After an order is rejected/cancelled: a verified online payment is refunded in full.
    A failure is recorded on the payment and retried by the sweeper; it never undoes the cancellation."""
    p = order.payment
    if p is not None and p.status == PAY_SUCCESS:
        try:
            refund_payment(db, p.id, note, actor)
        except PaymentError as exc:
            log.error("auto refund failed for %s: %s", order.order_code, exc)


# ------------------------------------------------------------------ background maintenance
def run_maintenance(db: Session, limit: int = 50) -> int:
    """Reconcile unpaid checkouts (missed webhooks, expiry) and pending refunds. Returns items touched."""
    open_ids = db.scalars(
        select(Payment.id).join(Order, Order.id == Payment.order_id)
        .where(Payment.status.in_([PAY_PENDING, PAY_FAILED]), Order.status == AWAITING_PAYMENT)
        .order_by(Payment.id).limit(limit)).all()
    refund_ids = db.scalars(select(Payment.id).where(Payment.status == PAY_SUCCESS, Payment.refund_status.in_(
        ["PENDING", "PENDING_APPROVAL", "ONHOLD"])).limit(limit)).all()
    unrefunded_ids = db.scalars(
        select(Payment.id).join(Order, Order.id == Payment.order_id)
        .where(Payment.status == PAY_SUCCESS, Order.status.in_(["CANCELLED", "REJECTED"]),
               (Payment.refund_id.is_(None)) | (Payment.refund_status.in_(["FAILED", "REJECTED", "CANCELLED"])))
        .order_by(Payment.id).limit(limit)).all()
    db.rollback()
    for pid in unrefunded_ids:
        try:
            refund_payment(db, pid, "Order cancelled - automatic refund retry")
        except PaymentError:
            pass  # recorded on the payment (needs_attention); shown in the admin "Refunds" view
        except Exception:
            log.exception("refund retry failed for payment %s", pid)
            db.rollback()
    for pid in open_ids:
        try:
            sync_payment(db, pid)
        except Exception:
            log.exception("sweep sync failed for payment %s", pid)
            db.rollback()
    for pid in refund_ids:
        try:
            sync_refund(db, pid)
        except Exception:
            log.exception("sweep refund sync failed for payment %s", pid)
            db.rollback()
    return len(open_ids) + len(refund_ids) + len(unrefunded_ids)
