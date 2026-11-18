"""Customer-initiated cancellation (WhatsApp). One entry point, safe against every race we know of.

Outcome by order state when the customer asks to cancel:
  AWAITING_PAYMENT          cancel; ask the gateway first, terminate the gateway order, release stock
                            (if the payment actually succeeded a moment ago it is treated as paid, below)
  PENDING / ACCEPTED, COD   cancel; release stock + delivery capacity; tell the admin
  PENDING / ACCEPTED, paid  verify payment with the gateway, cancel, release stock, request a FULL refund;
                            the payment becomes REFUNDED only when the gateway confirms (see payments.py)
  DISPATCHED                NOT cancelled: the request is recorded, the admin is alerted and decides
  DELIVERED                 NOT cancelled (complaint/return workflow is out of scope)
  CANCELLED / REJECTED      nothing to do (idempotent: duplicate messages are harmless)

Lock discipline: payment verification happens BEFORE the order row is locked, and the order lock is never
held while waiting for a payment lock, so this cannot deadlock with the Cashfree webhook (payment -> order).
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (AWAITING_PAYMENT, CANCELLED, DELIVERED, DISPATCHED, PAY_PENDING, PAY_FAILED, PAY_SUCCESS,
                        REJECTED, Order)
from app.services import notifications, payments
from app.services.core import audit
from app.services.orders import OrderError, change_status
from app.utils import utcnow

log = logging.getLogger("mango.cancellation")


@dataclass
class CancelResult:
    outcome: str  # cancelled | already_cancelled | delivered | dispatched_request | dispatched_repeat
    order: Order
    previous_status: str
    refund_amount: Decimal | None = None  # set when a collected online payment is being refunded
    refund_status: str | None = None  # gateway's status; PENDING means "requested, not yet confirmed"


def paid_amount(order: Order) -> Decimal | None:
    """Amount the customer has actually paid online (verified), else None."""
    p = order.payment
    return Decimal(p.amount) if p is not None and p.status == PAY_SUCCESS else None


def customer_cancel_order(db: Session, order_id: int, reason: str = "Cancelled by customer") -> CancelResult:
    order = db.get(Order, order_id)
    actor = f"customer:{order.customer.whatsapp_number}"

    # 1. Gateway is the source of truth: re-verify any online payment before deciding anything.
    pay = order.payment
    if pay is not None and pay.status in (PAY_PENDING, PAY_FAILED, PAY_SUCCESS):
        payments.sync_payment(db, pay.id)  # swallows gateway outages (logged); never raises for them
    db.rollback()
    order = db.get(Order, order_id)
    db.refresh(order)
    prev = order.status

    if prev in (CANCELLED, REJECTED):
        return CancelResult("already_cancelled", order, prev)
    if prev == DELIVERED:
        return CancelResult("delivered", order, prev)
    if prev == DISPATCHED:
        return _dispatched_request(db, order, actor, prev)

    # 2. Unpaid online order: close the checkout (terminates the gateway order, releases stock).
    if prev == AWAITING_PAYMENT:
        outcome = payments.cancel_checkout(db, order.id, actor=actor, cancelled_by="CUSTOMER")
        db.refresh(order)
        if outcome == "cancelled":
            return CancelResult("cancelled", order, prev)
        prev = order.status  # the payment landed a moment ago -> continue as a paid order

    # 3. COD or paid online order: cancel under the order lock (loser of an admin/customer race gets an error).
    try:
        change_status(db, order, CANCELLED, reason, actor=actor, cancelled_by="CUSTOMER")
    except OrderError:
        db.refresh(order)
        if order.status in (CANCELLED, REJECTED):
            return CancelResult("already_cancelled", order, prev)
        if order.status == DISPATCHED:
            return _dispatched_request(db, order, actor, prev)
        if order.status == DELIVERED:
            return CancelResult("delivered", order, prev)
        raise

    result = CancelResult("cancelled", order, prev)
    pay = order.payment
    if pay is not None and pay.status == PAY_SUCCESS:
        # Money was collected: refund in full. Failure is recorded on the payment and retried by the sweeper;
        # it never undoes the cancellation.
        payments.refund_if_paid(db, order, "Order cancelled by customer", actor)
        db.refresh(pay)
        result.refund_amount, result.refund_status = Decimal(pay.amount), pay.refund_status
        if pay.status == "REFUNDED":
            notifications.notify_refunded(db, order.id)
    notifications.notify_admin_customer_cancelled(db, order, prev, result.refund_amount)
    return result


def _dispatched_request(db: Session, order: Order, actor: str, prev: str) -> CancelResult:
    """Too late to cancel automatically. Record the request once and alert the family."""
    locked = db.scalar(select(Order).where(Order.id == order.id).with_for_update()
                       .execution_options(populate_existing=True))
    if locked.status != DISPATCHED:  # moved on while we waited for the lock
        db.rollback()
        db.refresh(order)
        if order.status == DELIVERED:
            return CancelResult("delivered", order, prev)
        return CancelResult("already_cancelled", order, prev)
    first = locked.cancel_requested_at is None
    if first:
        locked.cancel_requested_at = utcnow()
        audit(db, actor, "cancel_requested", "order", locked.order_code,
              f"{prev} -> (not cancelled: already dispatched) | customer asked to cancel; admin must decide")
    db.commit()
    if first:
        notifications.notify_admin_cancel_request(db, locked)
    return CancelResult("dispatched_request" if first else "dispatched_repeat", locked, prev)
