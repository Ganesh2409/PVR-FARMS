"""Order creation, inventory reservation and status transitions (all transactional, PostgreSQL).

Concurrency model (READ COMMITTED): every check-then-write takes row locks first
  * product + inventory rows   -> SELECT ... FOR UPDATE   (no overselling)
  * delivery_dates row         -> SELECT ... FOR UPDATE   (no overbooking of a day or its slots)
  * order code sequence        -> pg_advisory_xact_lock   (no duplicate MF-YYYYMMDD-XXXX)
Locks are always taken in the order product -> inventory -> delivery date, so transactions cannot deadlock.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (ACCEPTED, AWAITING_PAYMENT, CANCELLED, DELIVERED, DISPATCHED, PENDING, REJECTED, Customer,
                        DeliveryArea, DeliveryDate, DeliverySlot, Inventory, InventoryReservation, Order, OrderItem,
                        Product)
from app.services import delivery
from app.services.catalog import validate_quantity
from app.services.core import accepting_orders, audit
from app.utils import IST, q2, today_ist, utcnow

VALID_TRANSITIONS: dict[str, set[str]] = {
    AWAITING_PAYMENT: {CANCELLED},  # becomes PENDING only through verified payment (services.payments)
    PENDING: {ACCEPTED, REJECTED, CANCELLED},
    ACCEPTED: {DISPATCHED, CANCELLED},
    DISPATCHED: {DELIVERED, CANCELLED},
    REJECTED: set(),
    DELIVERED: set(),
    CANCELLED: set(),
}


class OrderError(Exception):
    """Business-rule failure. `code` is machine readable; `ctx` carries details."""

    def __init__(self, code: str, **ctx):
        super().__init__(code)
        self.code = code
        self.ctx = ctx


def _next_order_code(db: Session) -> str:
    now = datetime.now(IST)
    prefix = f"MF-{now:%Y%m%d}-"
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": int(f"{now:%Y%m%d}")})
    last = db.scalar(select(func.max(Order.order_code)).where(Order.order_code.like(prefix + "%")))
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


def _lock_product_stock(db: Session, product_id: int) -> tuple[Product | None, Inventory | None]:
    product = db.scalar(select(Product).where(Product.id == product_id).with_for_update()
                        .execution_options(populate_existing=True))
    if product is None:
        return None, None
    inv = db.scalar(select(Inventory).where(Inventory.product_id == product_id).with_for_update()
                    .execution_options(populate_existing=True))
    return product, inv


def _lock_bookable_date(db: Session, d: date, exclude_order_id: int | None = None) -> DeliveryDate | None:
    """Lock the delivery-date row, then check it is active, not past and not full."""
    if d < today_ist():
        return None
    dd = db.scalar(select(DeliveryDate).where(DeliveryDate.date == d, DeliveryDate.is_active.is_(True))
                   .with_for_update().execution_options(populate_existing=True))
    if dd is None or delivery.date_remaining(db, dd, exclude_order_id) <= 0:
        return None
    return dd


def create_order(db: Session, customer: Customer, draft: dict, *, language: str, channel: str,
                 payment_method: str = "COD") -> tuple[Order, bool]:
    """Validate everything again and create the order. Returns (order, created).

    * payment_method "COD"    -> status PENDING (awaits admin acceptance)
    * payment_method "ONLINE" -> status AWAITING_PAYMENT; stock and capacity are held, and the order
      only becomes PENDING after services.payments has verified a successful payment.
    `created` is False when the idempotency key already produced an order (duplicate confirm).
    Commits on success, rolls back on failure.
    """
    key = draft.get("idempotency_key")
    if key:
        existing = db.scalar(select(Order).where(Order.idempotency_key == key))
        if existing:
            return existing, False
    try:
        if not accepting_orders(db):
            raise OrderError("not_accepting")

        product, inv = _lock_product_stock(db, draft["product_id"])
        if product is None or not product.is_active:
            raise OrderError("product_unavailable")
        qty = Decimal(str(draft["qty"]))
        err = validate_quantity(product, qty)
        if err:
            code, ctx = err
            raise OrderError("insufficient_stock" if code == "no_stock" else "invalid_quantity", **ctx, detail=code)

        d = date.fromisoformat(draft["date"])
        dd = _lock_bookable_date(db, d)
        if dd is None:
            raise OrderError("date_unavailable")
        slot = None
        slots = delivery.slots_for_date(db, dd)
        if dd.slots_enabled and slots:
            slot = next((s for s in slots if s.id == draft.get("slot_id")), None)
            if slot is None:
                raise OrderError("slot_unavailable")

        area = db.get(DeliveryArea, draft["area_id"])
        if area is None or not area.is_active:
            raise OrderError("area_unavailable")

        subtotal = q2(qty * Decimal(product.price_per_kg))
        charge = q2(area.delivery_charge)
        inv.quantity_reserved = Decimal(inv.quantity_reserved) + qty

        online = payment_method == "ONLINE"
        order = Order(
            order_code=_next_order_code(db), idempotency_key=key, customer_id=customer.id,
            status=AWAITING_PAYMENT if online else PENDING, customer_name=draft["name"],
            contact_phone=draft["phone"], alt_phone=draft.get("alt_phone"), area_id=area.id,
            village_name=area.name_en, address=draft["address"], landmark=draft.get("landmark"), delivery_date=d,
            slot_id=slot.id if slot else None, slot_name=slot.name_en if slot else None,
            subtotal=subtotal, delivery_charge=charge, total=subtotal + charge, payment_method=payment_method,
            payment_status="UNPAID", language=language, channel=channel,
        )
        order.items.append(OrderItem(
            product_id=product.id, product_name_en=product.name_en, product_name_te=product.name_te,
            price_per_kg=product.price_per_kg, quantity=qty, line_total=subtotal))
        db.add(order)
        db.flush()
        db.add(InventoryReservation(order_id=order.id, product_id=product.id, quantity=qty, status="ACTIVE"))
        customer.name = draft["name"]
        customer.alt_phone = draft.get("alt_phone")
        customer.village_id = area.id
        db.commit()
        return order, True
    except IntegrityError:
        db.rollback()
        if key:
            existing = db.scalar(select(Order).where(Order.idempotency_key == key))
            if existing:
                return existing, False
        raise
    except Exception:
        db.rollback()
        raise


def revive_cancelled_order(db: Session, order: Order) -> bool:
    """A verified payment arrived for an order that had been cancelled/expired. Re-reserve stock and
    capacity and restore it to PENDING. Returns False (changing nothing) if that is no longer possible.
    Does not commit."""
    item = order.items[0]
    product, inv = _lock_product_stock(db, item.product_id)
    if product is None or inv is None or inv.available < Decimal(item.quantity):
        return False
    if _lock_bookable_date(db, order.delivery_date, exclude_order_id=order.id) is None:
        return False
    inv.quantity_reserved = Decimal(inv.quantity_reserved) + Decimal(item.quantity)
    db.add(InventoryReservation(order_id=order.id, product_id=item.product_id, quantity=item.quantity,
                                status="ACTIVE"))
    order.status = PENDING
    order.cancel_reason = None
    return True


def _release_reservations(order: Order, db: Session, new_status: str) -> None:
    for r in order.reservations:
        if r.status != "ACTIVE":
            continue
        inv = db.scalar(select(Inventory).where(Inventory.product_id == r.product_id).with_for_update()
                        .execution_options(populate_existing=True))
        if new_status == "FULFILLED":
            inv.quantity_on_hand = Decimal(inv.quantity_on_hand) - Decimal(r.quantity)
        inv.quantity_reserved = max(Decimal(inv.quantity_reserved) - Decimal(r.quantity), Decimal("0"))
        r.status = "RELEASED" if new_status == "RELEASED" else "FULFILLED"


def change_status(db: Session, order: Order, new_status: str, reason: str | None = None, *,
                 commit: bool = True, actor: str = "system", cancelled_by: str | None = None) -> Order:
    """Move an order to `new_status` under a row lock, release/commit stock, and write the audit trail.

    actor        who did it: an admin username, "customer:<phone>" or "system" (stored in audit_logs)
    cancelled_by CUSTOMER | ADMIN | SYSTEM - recorded on the order when it ends up CANCELLED/REJECTED
    """
    # Re-read the order under a row lock so two simultaneous actions (e.g. admin and customer)
    # cannot both pass the transition check; the loser gets invalid_transition.
    db.execute(select(Order.id).where(Order.id == order.id).with_for_update())
    db.refresh(order)
    prev = order.status
    if new_status not in VALID_TRANSITIONS.get(prev, set()):
        if commit:
            db.rollback()
        raise OrderError("invalid_transition", current=prev, new=new_status)
    if new_status == REJECTED and not (reason or "").strip():
        if commit:
            db.rollback()
        raise OrderError("reason_required")
    try:
        if new_status in (REJECTED, CANCELLED):
            _release_reservations(order, db, "RELEASED")
            order.cancelled_by = cancelled_by or ("ADMIN" if not actor.startswith(("system", "customer:")) else
                                                  "CUSTOMER" if actor.startswith("customer:") else "SYSTEM")
        elif new_status == DELIVERED:
            _release_reservations(order, db, "FULFILLED")
            if order.payment_method == "COD":
                order.payment_status = "PAID"  # cash collected on delivery
        elif new_status == ACCEPTED:
            order.confirmed_at, order.confirmed_by = utcnow(), actor
        if new_status == REJECTED:
            order.reject_reason = reason.strip()
        if new_status == CANCELLED:
            order.cancel_reason = (reason or "").strip() or None
        order.status = new_status
        order.updated_at = utcnow()
        pay = order.payment
        pay_info = f"{order.payment_method}/{order.payment_status}" + (
            f" payment={pay.status}" + (f" refund={pay.refund_id}:{pay.refund_status}" if pay.refund_id else "")
            if pay else "")
        audit(db, actor, "order_status", "order", order.order_code,
              f"{prev} -> {new_status}" + (f" | reason: {reason.strip()}" if (reason or "").strip() else "") +
              f" | {pay_info}")
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    return order


def change_delivery_date(db: Session, order: Order, new_date: date, slot_id: int | None = None) -> Order:
    if order.status not in (AWAITING_PAYMENT, PENDING, ACCEPTED):
        raise OrderError("invalid_transition", current=order.status, new="date change")
    try:
        dd = _lock_bookable_date(db, new_date, exclude_order_id=order.id)
        if dd is None:
            raise OrderError("date_unavailable")
        slot = None
        if dd.slots_enabled:
            slot = db.get(DeliverySlot, slot_id) if slot_id else None
            if slot is None or not slot.is_active:
                raise OrderError("slot_unavailable")
        order.delivery_date = new_date
        order.slot_id = slot.id if slot else None
        order.slot_name = slot.name_en if slot else None
        db.commit()
    except Exception:
        db.rollback()
        raise
    return order
