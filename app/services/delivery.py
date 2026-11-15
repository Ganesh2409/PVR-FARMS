from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ACTIVE_STATUSES, DeliveryArea, DeliveryDate, DeliverySlot, Order
from app.utils import today_ist


@dataclass
class DateOption:
    row: DeliveryDate
    remaining: int


def orders_on_date(db: Session, d: date, slot_id: int | None = None) -> int:
    q = select(func.count(Order.id)).where(Order.delivery_date == d, Order.status.in_(ACTIVE_STATUSES))
    if slot_id is not None:
        q = q.where(Order.slot_id == slot_id)
    return db.scalar(q) or 0


def date_remaining(db: Session, dd: DeliveryDate, exclude_order_id: int | None = None) -> int:
    q = select(func.count(Order.id)).where(Order.delivery_date == dd.date, Order.status.in_(ACTIVE_STATUSES))
    if exclude_order_id:
        q = q.where(Order.id != exclude_order_id)
    return dd.max_orders - (db.scalar(q) or 0)


def get_bookable_date(db: Session, d: date, exclude_order_id: int | None = None) -> DeliveryDate | None:
    """The delivery_date row if it is active, not in the past and has capacity."""
    if d < today_ist():
        return None
    dd = db.scalar(select(DeliveryDate).where(DeliveryDate.date == d, DeliveryDate.is_active.is_(True)))
    if dd is None or date_remaining(db, dd, exclude_order_id) <= 0:
        return None
    return dd


def available_dates(db: Session, limit: int = 8) -> list[DateOption]:
    rows = db.scalars(select(DeliveryDate).where(DeliveryDate.is_active.is_(True),
                                                 DeliveryDate.date >= today_ist())
                      .order_by(DeliveryDate.date)).all()
    out = []
    for r in rows:
        rem = date_remaining(db, r)
        if rem > 0:
            out.append(DateOption(r, rem))
        if len(out) >= limit:
            break
    return out


def slots_for_date(db: Session, dd: DeliveryDate) -> list[DeliverySlot]:
    """Active slots with free capacity; empty list when slots are disabled for the date."""
    if not dd.slots_enabled:
        return []
    slots = db.scalars(select(DeliverySlot).where(DeliverySlot.is_active.is_(True))
                       .order_by(DeliverySlot.sort_order, DeliverySlot.id)).all()
    return [s for s in slots if s.max_orders is None or orders_on_date(db, dd.date, s.id) < s.max_orders]


def active_areas(db: Session) -> list[DeliveryArea]:
    return list(db.scalars(select(DeliveryArea).where(DeliveryArea.is_active.is_(True))
                           .order_by(DeliveryArea.name_en)))
