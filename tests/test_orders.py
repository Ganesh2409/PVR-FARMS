from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (ACCEPTED, CANCELLED, DELIVERED, DISPATCHED, PENDING, REJECTED, Customer, DeliveryDate,
                        Inventory, InventoryReservation, Order)
from app.services import delivery, notifications
from app.services.orders import OrderError, change_delivery_date, change_status, create_order
from app.utils import today_ist


def make_order(db, qty="2", phone="919000000001", key=None, day=1):
    cust = db.scalar(select(Customer).where(Customer.whatsapp_number == phone))
    if cust is None:
        cust = Customer(whatsapp_number=phone, language="en", channel="simulator")
        db.add(cust)
        db.commit()
    draft = dict(product_id=1, qty=qty, date=(today_ist() + timedelta(days=day)).isoformat(), name="Test",
                 phone=phone, area_id=1, address="Some address here", idempotency_key=key)
    return create_order(db, cust, draft, language="en", channel="simulator")


def inv(db):
    db.expire_all()
    return db.scalar(select(Inventory).where(Inventory.product_id == 1))


def test_decimal_money_has_no_float_error(db):
    order, _ = make_order(db, qty="2.333")
    assert order.subtotal == Decimal("139.98") and order.total == Decimal("169.98")  # 2.333*60 + 30


def test_idempotency_key_prevents_duplicates(db):
    a, created_a = make_order(db, key="k1")
    b, created_b = make_order(db, key="k1")
    assert created_a and not created_b and a.id == b.id
    assert inv(db).quantity_reserved == Decimal("2")


def test_cannot_oversell_stock(db):
    for i in range(4):
        make_order(db, qty="50", phone=f"91900000001{i}")
    assert inv(db).quantity_reserved == Decimal("200")
    with pytest.raises(OrderError) as e:
        make_order(db, qty="1", phone="919000000050")
    assert e.value.code == "insufficient_stock"


def test_capacity_limit_enforced_in_create(db):
    dd = db.scalar(select(DeliveryDate).order_by(DeliveryDate.date))
    dd.max_orders = 1
    db.commit()
    make_order(db, day=1)
    with pytest.raises(OrderError) as e:
        make_order(db, phone="919000000002", day=1)
    assert e.value.code == "date_unavailable"


def test_past_date_rejected(db):
    db.add(DeliveryDate(date=today_ist() - timedelta(days=1)))
    db.commit()
    with pytest.raises(OrderError) as e:
        make_order(db, day=-1)
    assert e.value.code == "date_unavailable"


def test_reject_releases_reservation(db):
    o, _ = make_order(db)
    assert inv(db).quantity_reserved == 2
    change_status(db, o, REJECTED, "Out of area")
    assert inv(db).quantity_reserved == 0 and o.reject_reason == "Out of area"
    assert db.scalar(select(InventoryReservation.status)) == "RELEASED"


def test_reject_requires_reason(db):
    o, _ = make_order(db)
    with pytest.raises(OrderError) as e:
        change_status(db, o, REJECTED, "  ")
    assert e.value.code == "reason_required" and o.status == PENDING


def test_delivery_commits_stock(db):
    o, _ = make_order(db, qty="3")
    for s in (ACCEPTED, DISPATCHED, DELIVERED):
        change_status(db, o, s)
    i = inv(db)
    assert i.quantity_on_hand == Decimal("197") and i.quantity_reserved == 0
    assert o.payment_status == "PAID"


def test_cancel_releases_after_accept(db):
    o, _ = make_order(db)
    change_status(db, o, ACCEPTED)
    change_status(db, o, CANCELLED, "Customer asked")
    assert inv(db).quantity_reserved == 0


@pytest.mark.parametrize("start,bad", [(PENDING, DISPATCHED), (PENDING, DELIVERED), (ACCEPTED, REJECTED),
                                       (ACCEPTED, PENDING), (DISPATCHED, ACCEPTED)])
def test_invalid_transitions(db, start, bad):
    o, _ = make_order(db)
    path = {PENDING: [], ACCEPTED: [ACCEPTED], DISPATCHED: [ACCEPTED, DISPATCHED]}[start]
    for s in path:
        change_status(db, o, s)
    with pytest.raises(OrderError) as e:
        change_status(db, o, bad, "x")
    assert e.value.code == "invalid_transition" and o.status == start


@pytest.mark.parametrize("terminal", [REJECTED, CANCELLED])
def test_terminal_states_are_final(db, terminal):
    o, _ = make_order(db)
    change_status(db, o, terminal, "reason")
    with pytest.raises(OrderError):
        change_status(db, o, ACCEPTED)


def test_admin_changes_delivery_date_respecting_capacity(db):
    o, _ = make_order(db, day=1)
    new_day = today_ist() + timedelta(days=3)
    change_delivery_date(db, o, new_day)
    assert o.delivery_date == new_day
    with pytest.raises(OrderError):
        change_delivery_date(db, o, today_ist() - timedelta(days=1))


def test_available_dates_exclude_past_inactive_full(db):
    rows = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).all()
    rows[0].is_active = False
    rows[1].max_orders = 0
    db.commit()
    dates = [o.row.date for o in delivery.available_dates(db, limit=20)]
    assert rows[0].date not in dates and rows[1].date not in dates and rows[2].date in dates


def test_customer_notification_logged_and_localised(db):
    o, _ = make_order(db)
    change_status(db, o, ACCEPTED)
    assert notifications.notify_customer(db, o, "ACCEPTED") == "sent"
    from app.models import MessageLog
    log = db.scalar(select(MessageLog).where(MessageLog.direction == "OUT"))
    assert o.order_code in log.content["body"] and "accepted" in log.content["body"]


def test_notification_outside_24h_window_uses_template(db, monkeypatch):
    from app.config import get_settings
    from app.models import Conversation, MessageLog
    from app.services import messaging
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_access_token", "tok")
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "123")
    monkeypatch.setattr(s, "whatsapp_template_order_update", "order_update")
    sent = []
    monkeypatch.setattr(messaging, "_post", lambda payload: sent.append(payload) or "wamid.T1")
    o, _ = make_order(db, phone="919811111111")
    o.customer.channel, o.customer.language = "whatsapp", "te"
    db.add(Conversation(customer_id=o.customer.id, state="MAIN_MENU", data={},
                        last_inbound_at=messaging.utcnow() - timedelta(hours=30)))
    db.commit()
    assert notifications.notify_customer(db, o, "DISPATCHED") == "template"
    assert sent[0]["type"] == "template" and sent[0]["template"]["name"] == "order_update"
    assert sent[0]["template"]["language"]["code"] == "te"
    assert db.scalar(select(MessageLog.message_type).where(MessageLog.direction == "OUT")) == "template"


def test_notification_outside_window_without_template_fails_safely(db, monkeypatch):
    from app.config import get_settings
    from app.models import MessageLog
    monkeypatch.setattr(get_settings(), "whatsapp_template_order_update", "")
    o, _ = make_order(db, phone="919822222222")
    o.customer.channel = "whatsapp"
    db.commit()
    assert notifications.notify_customer(db, o, "DELIVERED") == "failed"
    assert "24h window" in db.scalar(select(MessageLog.error))


def test_concurrent_orders_cannot_oversell_or_overbook(db):
    """Real threads, real PostgreSQL row locks: only as many orders succeed as stock and capacity allow."""
    from concurrent.futures import ThreadPoolExecutor
    from app.database import SessionLocal

    prod = db.get(__import__("app.models", fromlist=["Product"]).Product, 1)
    prod.inventory.quantity_on_hand = Decimal("3")
    db.commit()
    db.rollback()

    def attempt(i):
        with SessionLocal() as s:
            try:
                make_order(s, qty="1", phone=f"91970000{i:04d}")
                return "ok"
            except OrderError as e:
                return e.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("ok") == 3 and results.count("insufficient_stock") == 5
    assert inv(db).quantity_reserved == Decimal("3")


def test_concurrent_orders_respect_date_capacity(db):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import SessionLocal

    dd = db.scalar(select(DeliveryDate).order_by(DeliveryDate.date))
    dd.max_orders = 2
    db.commit()
    db.rollback()

    def attempt(i):
        with SessionLocal() as s:
            try:
                make_order(s, qty="1", phone=f"91960000{i:04d}", day=1)
                return "ok"
            except OrderError as e:
                return e.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count("ok") == 2 and results.count("date_unavailable") == 4
