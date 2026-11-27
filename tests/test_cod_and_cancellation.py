"""COD admin-verification workflow + customer cancellation (cases A-F) incl. payment/refund races.
All gateway and WhatsApp calls are mocked: these tests prove OUR logic, not Cashfree or Meta."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (ACCEPTED, CANCELLED, DELIVERED, DISPATCHED, PENDING, REJECTED, AuditLog, DeliveryDate,
                        Order, Payment)
from app.services import cancellation, delivery, messaging, payments
from app.services.core import set_setting
from app.services.gateways import GatewayError, MockGateway
from app.services.orders import OrderError, change_status
from app.utils import utcnow
from tests.conftest import Chat, allow_cod, drive_to_review
from tests.test_payments import (admin_login, admin_post, cf_event, out_bodies, payment_of, place_online, reload,
                                 reserved, send_webhook)

PHONE = "919000000001"


# ------------------------------------------------------------------ helpers
def place_cod(db, phone=PHONE, lang="en", qty="qty_2"):
    chat = Chat(db, phone)
    drive_to_review(chat, lang=lang, pay="cod", qty=qty)
    chat.pick("confirm_order")
    reload(db)
    return chat, db.scalar(select(Order).order_by(Order.id.desc()))


def paid_online(db, client, phone=PHONE, lang="en"):
    """An online order whose payment Cashfree (mock) has verified: status PENDING, payment SUCCESS."""
    chat = Chat(db, phone)
    drive_to_review(chat, lang=lang, pay="online").pick("confirm_order")
    reload(db)
    order = db.scalar(select(Order).order_by(Order.id.desc()))
    MockGateway.simulate(order.order_code, "success")
    assert send_webhook(client, cf_event(order.order_code), key=f"k-{order.order_code}").status_code == 200
    reload(db)
    return chat, db.get(Order, order.id)


def order_audit(db, order, action="order_status"):
    reload(db)
    return db.scalars(select(AuditLog).where(AuditLog.entity == "order", AuditLog.entity_id == order.order_code,
                                             AuditLog.action == action).order_by(AuditLog.id)).all()


def status_of(db, order) -> str:
    reload(db)
    return db.get(Order, order.id).status


def advance(db, order, *statuses):
    for s in statuses:
        change_status(db, db.get(Order, order.id), s, actor="admin", cancelled_by="ADMIN")
    reload(db)


@pytest.fixture
def admin_alerts(db, monkeypatch):
    """Captures WhatsApp messages sent to the family's alert number."""
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_access_token", "tok")
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "1")
    set_setting(db, "admin_notify_phone", "919888800001")
    db.commit()
    sent = []
    monkeypatch.setattr(messaging, "_post", lambda payload: sent.append(payload) or "wamid.ALERT")
    return sent


def alert_texts(sent):
    return [p["text"]["body"] for p in sent if p.get("to") == "919888800001"]


# ================================================================== CHANGE 1 - COD verification workflow
def test_cod_order_is_saved_and_acknowledged_but_not_confirmed(db):
    chat, o = place_cod(db)
    assert o.status == PENDING and o.payment_method == "COD" and o.confirmed_at is None and o.confirmed_by is None
    assert reserved(db) == Decimal("2")  # stock held while the family verifies
    ack = [b for b in out_bodies(db) if o.order_code in b]
    assert ack and "Your PVR Farms order has been received successfully." in ack[-1]
    assert f"Order ID: {o.order_code}" in ack[-1] and "waiting for confirmation" in ack[-1]
    assert "call you shortly to verify your details" in ack[-1]
    assert not any("confirmed successfully" in b or "successfully placed" in b for b in out_bodies(db))


def test_cod_acknowledgement_in_telugu(db):
    chat, o = place_cod(db, lang="te")
    text = chat.text
    for needle in ("PVR Farms", o.order_code, "ఇంకా నిర్ధారణ కాలేదు", "ఫోన్ చేస్తారు", "మళ్ళీ మెసేజ్ పంపుతాము"):
        assert needle in text


def test_cod_order_appears_in_admin_awaiting_confirmation_list(db, client):
    _, o = place_cod(db)
    admin_login(client)
    html = client.get("/admin/orders?view=new").text
    for needle in (o.order_code, "Ravi Kumar", "+91 9000000001", "H.No 4-5, Main Road", "Govindaraopet",
                   "Raw Mangoes (for eating)", "₹150", "COD", "Confirm Order", "Reject", "View Details"):
        assert needle in html, needle
    assert o.order_code not in client.get("/admin/orders?view=confirmed").text
    assert "Awaiting Confirmation" in client.get("/admin").text


def test_admin_confirms_cod_order_and_customer_is_told(db, client):
    _, o = place_cod(db)
    admin_login(client)
    r = admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="ACCEPTED")
    assert r.status_code == 303
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == ACCEPTED and o.confirmed_by == "admin" and o.confirmed_at is not None
    assert reserved(db) == Decimal("2")  # still reserved until delivery
    msg = [b for b in out_bodies(db) if "confirmed successfully" in b]
    assert len(msg) == 1 and f"order {o.order_code} has been confirmed successfully" in msg[0]
    assert "Our family will prepare your order and dispatch it soon" in msg[0]
    rows = order_audit(db, o)
    assert len(rows) == 1 and rows[0].admin_username == "admin" and rows[0].details.startswith("PENDING -> ACCEPTED")


def test_confirmation_message_uses_customers_language(db, client):
    _, o = place_cod(db, lang="te")
    admin_login(client)
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="ACCEPTED")
    assert any("నిర్ధారించబడింది" in b and o.order_code in b and "PVR Farms" in b for b in out_bodies(db))


def test_duplicate_admin_confirmation_does_nothing_twice(db, client):
    _, o = place_cod(db)
    admin_login(client)
    for _ in range(3):
        admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="ACCEPTED")
    reload(db)
    o = db.get(Order, o.id)
    first_time = o.confirmed_at
    assert o.status == ACCEPTED
    assert len([b for b in out_bodies(db) if "confirmed successfully" in b]) == 1
    assert len(order_audit(db, o)) == 1
    assert reserved(db) == Decimal("2")
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="ACCEPTED")
    reload(db)
    assert db.get(Order, o.id).confirmed_at == first_time


def test_simultaneous_admin_confirmations_only_one_wins(db):
    _, o = place_cod(db)
    barrier = Barrier(4)

    def go(_):
        with SessionLocal() as s:
            barrier.wait()
            try:
                change_status(s, s.get(Order, o.id), ACCEPTED, actor="admin")
                return "ok"
            except OrderError as e:
                return e.code

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(go, range(4)))
    assert results.count("ok") == 1 and results.count("invalid_transition") == 3
    assert len(order_audit(db, o)) == 1


def test_admin_rejects_unconfirmed_cod_order_and_releases_stock_and_capacity(db, client):
    first = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).first()
    first.max_orders = 1
    db.commit()
    _, o = place_cod(db)
    assert o.delivery_date == first.date
    assert first.date not in [x.row.date for x in delivery.available_dates(db, 20)]  # day is full
    admin_login(client)
    # a reason is mandatory
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="REJECTED", reason="")
    assert status_of(db, o) == PENDING
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="REJECTED",
               reason="Could not reach customer")
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == REJECTED and o.cancelled_by == "ADMIN" and o.reject_reason == "Could not reach customer"
    assert reserved(db) == 0
    assert first.date in [x.row.date for x in delivery.available_dates(db, 20)]  # capacity released
    assert any("Could not reach customer" in b for b in out_bodies(db))


def test_unreachable_customer_is_never_confirmed_automatically(db):
    _, o = place_cod(db)
    o.created_at = utcnow() - timedelta(days=3)
    db.commit()
    payments.run_maintenance(db)
    assert status_of(db, o) == PENDING and reserved(db) == Decimal("2")


def test_dispatch_and_delivery_notifications_after_confirmation(db, client):
    _, o = place_cod(db)
    admin_login(client)
    url, page = f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}"
    for new in ("ACCEPTED", "DISPATCHED", "DELIVERED"):
        admin_post(client, url, page, new_status=new)
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == DELIVERED and o.payment_status == "PAID"
    bodies = out_bodies(db)
    assert any("confirmed successfully" in b for b in bodies)
    assert any("on its way" in b for b in bodies) and any("has been delivered" in b for b in bodies)
    assert db.scalar(select(Order.payment_status)) == "PAID" and reserved(db) == 0
    assert db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == DELIVERED)) == Decimal("150")


def test_cod_cannot_skip_confirmation(db, client):
    _, o = place_cod(db)
    admin_login(client)
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="DISPATCHED")
    assert status_of(db, o) == PENDING


# ================================================================== CHANGE 2 - customer cancellation
# ---- Case A: COD awaiting confirmation
def test_case_a_customer_cancels_unconfirmed_cod_order(db, admin_alerts):
    chat, o = place_cod(db)
    chat.say("cancel")
    assert f"Do you want to cancel order {o.order_code}" in chat.text and chat.option_ids == ["cancel_yes", "cancel_no"]
    chat.pick("cancel_yes")
    assert f"Your order {o.order_code} has been cancelled as you asked" in chat.text
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == CANCELLED and o.cancelled_by == "CUSTOMER" and o.cancel_reason == "Cancelled by customer"
    assert reserved(db) == 0 and db.scalar(select(Payment.id)) is None
    rows = order_audit(db, o)
    assert len(rows) == 1 and rows[0].admin_username == f"customer:{PHONE}"
    assert "PENDING -> CANCELLED" in rows[0].details and "COD/UNPAID" in rows[0].details
    assert any(o.order_code in a and "cancelled" in a for a in alert_texts(admin_alerts))


def test_cancellation_asks_first_and_no_keeps_order(db):
    chat, o = place_cod(db)
    chat.say("రద్దు")  # Telugu 'cancel'
    chat.pick("cancel_no")
    assert "stays as it is" in chat.text
    assert status_of(db, o) == PENDING and reserved(db) == Decimal("2")


def test_cancel_confirmation_in_telugu(db):
    chat, o = place_cod(db, lang="te")
    chat.say("రద్దు")
    assert "రద్దు చేయాలా" in chat.text and o.order_code in chat.text
    chat.pick("cancel_yes")
    assert "రద్దు చేయబడింది" in chat.text and status_of(db, o) == CANCELLED


def test_cancel_releases_delivery_capacity(db):
    first = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).first()
    first.max_orders = 1
    db.commit()
    chat, o = place_cod(db)
    assert first.date not in [x.row.date for x in delivery.available_dates(db, 20)]
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    assert first.date in [x.row.date for x in delivery.available_dates(db, 20)]


def test_cancel_without_active_order(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").say("cancel")
    assert "no active order to cancel" in chat.text and len(chat.option_ids) == 3


def test_duplicate_cancel_confirmations_are_harmless(db):
    chat, o = place_cod(db)
    chat.say("cancel").pick("cancel_yes")
    chat.pick("cancel_yes")  # same button pressed again / duplicate delivery
    assert "already finished" in chat.text
    chat.say("cancel")
    assert "no active order" in chat.text
    chat.say(f"cancel {o.order_code}")
    assert "already cancelled" in chat.text
    assert len(order_audit(db, o)) == 1 and reserved(db) == 0


def test_cancel_with_several_active_orders_asks_which(db):
    chat, o1 = place_cod(db)
    drive_to_review(chat, pay="cod").pick("confirm_order")
    reload(db)
    o2 = db.scalar(select(Order).order_by(Order.id.desc()))
    assert o1.id != o2.id
    chat.say("cancel")
    assert "Which order would you like to cancel" in chat.text and f"co_{o1.order_code}" in chat.option_ids
    chat.pick(f"co_{o1.order_code}").pick("cancel_yes")
    assert status_of(db, o1) == CANCELLED and status_of(db, o2) == PENDING


def test_cancel_in_the_middle_of_ordering_still_cancels_the_draft(db):
    chat, o = place_cod(db)
    drive_to_review(chat, pay="cod")  # a second order in progress
    chat.say("cancel")
    assert "Your order has been cancelled" in chat.text  # the draft, as before
    assert status_of(db, o) == PENDING  # the placed order is untouched


def test_customer_cannot_cancel_someone_elses_order(db):
    _, o = place_cod(db)
    other = Chat(db, "919000000077")
    other.say("Hi").pick("lang_en").say(f"cancel {o.order_code}")
    assert "could not find" in other.text and status_of(db, o) == PENDING


# ---- Case B: COD confirmed, not dispatched
def test_case_b_customer_cancels_confirmed_cod_order(db, client, admin_alerts):
    chat, o = place_cod(db)
    advance(db, o, ACCEPTED)
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == CANCELLED and o.cancelled_by == "CUSTOMER" and reserved(db) == 0
    assert "PENDING -> ACCEPTED" in order_audit(db, o)[0].details and "ACCEPTED -> CANCELLED" in order_audit(db, o)[1].details
    assert any(o.order_code in a and "it was ACCEPTED" in a for a in alert_texts(admin_alerts))
    assert "has been cancelled as you asked" in chat.text


# ---- Case C: online order awaiting payment
def test_case_c_customer_cancels_unpaid_online_order(db):
    chat, o = place_online(db)
    chat.say("cancel")
    assert "Do you want to cancel" in chat.text and "refunded" not in chat.text  # nothing paid yet
    chat.pick("cancel_yes")
    reload(db)
    o, p = db.get(Order, o.id), db.scalar(select(Payment))
    assert o.status == CANCELLED and o.cancelled_by == "CUSTOMER" and reserved(db) == 0
    assert p.status == "EXPIRED" and p.closed_reason == "CANCELLED" and MockGateway.ORDERS[o.order_code]["terminated"]
    assert "No payment was taken" in chat.text


def test_payment_arriving_after_customer_cancelled_is_refunded_not_revived(db, client):
    chat, o = place_online(db)
    chat.say("cancel").pick("cancel_yes")
    MockGateway.simulate(o.order_code, "success")  # money moved just as/after we cancelled
    assert send_webhook(client, cf_event(o.order_code), key="late-1").status_code == 200
    reload(db)
    o, p = db.get(Order, o.id), db.scalar(select(Payment))
    assert o.status == CANCELLED and reserved(db) == 0  # the cancelled order is NOT brought back
    assert p.closed_reason == "LATE_PAYMENT" and p.cf_payment_id == "mockpay1"
    assert p.refund_id and p.status == "REFUNDED"  # and the money goes back
    assert send_webhook(client, cf_event(o.order_code), key="late-2").status_code == 200  # duplicate webhook
    assert len(MockGateway.REFUNDS) == 1


def test_late_payment_after_expiry_still_revives_when_stock_allows(db):
    chat, o = place_online(db)
    pay = payment_of(db, o)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    payments.run_maintenance(db)
    MockGateway.ORDERS[o.order_code]["status"] = "ACTIVE"
    MockGateway.simulate(o.order_code, "success")
    payments.sync_payment(db, pay.id)
    reload(db)
    assert db.get(Order, o.id).status == PENDING and db.scalar(select(Payment.status)) == "SUCCESS"


def test_admin_cancelling_unpaid_online_order_closes_the_payment(db, client):
    _, o = place_online(db)
    admin_login(client)
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="CANCELLED", reason="x")
    reload(db)
    o, p = db.get(Order, o.id), db.scalar(select(Payment))
    assert o.status == CANCELLED and o.cancelled_by == "ADMIN" and p.closed_reason == "CANCELLED"
    MockGateway.simulate(o.order_code, "success")
    send_webhook(client, cf_event(o.order_code))
    reload(db)
    assert db.get(Order, o.id).status == CANCELLED and db.scalar(select(Payment.status)) == "REFUNDED"


# ---- Case D: online order, payment succeeded, not dispatched
def test_case_d_paid_order_is_cancelled_and_refund_tracked_until_gateway_confirms(db, client):
    chat, o = paid_online(db, client)
    assert o.status == PENDING
    MockGateway.refund_outcome = "PENDING"  # gateway accepts the request but has not paid out yet
    chat.say("cancel")
    assert "refunded in full" in chat.text and "₹150" in chat.text
    chat.pick("cancel_yes")
    assert "We have started refunding the ₹150 you paid" in chat.text
    reload(db)
    o, p = db.get(Order, o.id), db.scalar(select(Payment))
    assert o.status == CANCELLED and reserved(db) == 0 and o.cancelled_by == "CUSTOMER"
    # CRITICAL: a submitted refund request is NOT a refund
    assert p.status == "SUCCESS" and p.refund_status == "PENDING" and p.refunded_at is None
    assert o.payment_status == "PAID" and p.refund_id == f"RF-{o.order_code}" and p.refund_amount == Decimal("150.00")
    assert not any("has been processed" in b for b in out_bodies(db))
    payments.run_maintenance(db)  # gateway still pending: stays as it is
    reload(db)
    assert db.scalar(select(Payment.status)) == "SUCCESS"
    # gateway confirms
    MockGateway.REFUNDS[p.refund_id]["status"] = "SUCCESS"
    payments.run_maintenance(db)
    reload(db)
    p = db.scalar(select(Payment))
    assert p.status == "REFUNDED" and p.refunded_at is not None and db.get(Order, o.id).payment_status == "REFUNDED"
    assert len([b for b in out_bodies(db) if "has been processed" in b]) == 1  # customer told once
    assert len(MockGateway.REFUNDS) == 1
    details = " | ".join(r.details for r in order_audit(db, o))
    assert "PENDING -> CANCELLED" in details and "ONLINE/PAID" in details and "payment=SUCCESS" in details
    assert [r.action for r in order_audit(db, o, "refund_requested")] == ["refund_requested"]
    assert len(order_audit(db, o, "refund_confirmed")) == 1


def test_case_d_instant_refund_is_marked_refunded_and_customer_told(db, client):
    chat, o = paid_online(db, client)
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    assert db.scalar(select(Payment.status)) == "REFUNDED"
    assert any("has been processed" in b for b in out_bodies(db))


def test_case_d_confirmed_and_paid_order_can_be_cancelled(db, client):
    chat, o = paid_online(db, client)
    advance(db, o, ACCEPTED)
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    assert db.get(Order, o.id).status == CANCELLED and db.scalar(select(Payment.status)) == "REFUNDED" and reserved(db) == 0


def test_case_d_refund_request_failure_never_undoes_cancellation_and_is_retried(db, client, monkeypatch):
    chat, o = paid_online(db, client)
    calls = {"n": 0}
    original = MockGateway.create_refund

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GatewayError("gateway timeout")
        return original(self, *a, **k)

    monkeypatch.setattr(MockGateway, "create_refund", flaky)
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    p = db.scalar(select(Payment))
    assert db.get(Order, o.id).status == CANCELLED
    assert p.status == "SUCCESS" and p.refund_status == "FAILED" and "REFUND_FAILED" in p.needs_attention
    assert "started refunding" in chat.text  # the customer is not told it is refunded
    payments.run_maintenance(db)  # sweeper retries with the same refund id
    reload(db)
    p = db.scalar(select(Payment))
    assert p.status == "REFUNDED" and calls["n"] == 2 and p.refund_id == f"RF-{o.order_code}"
    payments.run_maintenance(db)
    assert calls["n"] == 2  # nothing more to do


def test_case_d_gateway_down_during_cancel_still_cancels_and_refunds_later(db, client, monkeypatch):
    chat, o = paid_online(db, client)
    monkeypatch.setattr(MockGateway, "get_status", lambda self, oid: (_ for _ in ()).throw(GatewayError("down")))
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    assert db.get(Order, o.id).status == CANCELLED and db.scalar(select(Payment.status)) == "REFUNDED"


def test_admin_cancelling_a_paid_order_also_refunds(db, client):
    _, o = paid_online(db, client)
    MockGateway.refund_outcome = "PENDING"
    admin_login(client)
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="CANCELLED", reason="Sold out")
    reload(db)
    p = db.scalar(select(Payment))
    assert db.get(Order, o.id).cancelled_by == "ADMIN" and p.status == "SUCCESS" and p.refund_status == "PENDING"
    assert "refund" in client.get("/admin/orders?view=refund_pending").text.lower() and o.order_code in client.get("/admin/orders?view=refund_pending").text
    assert any("started refunding" in b for b in out_bodies(db))
    MockGateway.REFUNDS[p.refund_id]["status"] = "SUCCESS"
    payments.run_maintenance(db)
    assert o.order_code in client.get("/admin/orders?view=refunded").text


def test_refund_sweeper_picks_up_a_cancelled_paid_order_that_never_got_a_refund_request(db, client):
    """The app stopped between 'order cancelled' and 'refund requested'."""
    _, o = paid_online(db, client)
    change_status(db, db.get(Order, o.id), CANCELLED, "cancelled", actor="admin", cancelled_by="ADMIN")  # no refund call
    reload(db)
    assert db.scalar(select(Payment.refund_id)) is None
    payments.run_maintenance(db)
    reload(db)
    assert db.scalar(select(Payment.status)) == "REFUNDED"


# ---- Case E: already dispatched
def test_case_e_dispatched_order_is_not_cancelled_but_admin_is_alerted(db, client, admin_alerts):
    chat, o = place_cod(db)
    advance(db, o, ACCEPTED, DISPATCHED)
    chat.say("cancel")  # no confirmation question: nothing will be cancelled
    assert "already been dispatched" in chat.text and "cannot be cancelled here" in chat.text
    reload(db)
    o = db.get(Order, o.id)
    assert o.status == DISPATCHED and o.cancel_requested_at is not None and reserved(db) == Decimal("2")
    assert len([a for a in alert_texts(admin_alerts) if "asks to CANCEL" in a and o.order_code in a]) == 1
    rows = order_audit(db, o, "cancel_requested")
    assert len(rows) == 1 and rows[0].admin_username == f"customer:{PHONE}"
    chat.say("cancel")  # asking again does not spam the family
    assert "already reached our team" in chat.text
    assert len([a for a in alert_texts(admin_alerts) if "asks to CANCEL" in a]) == 1
    # the admin sees it and decides
    admin_login(client)
    assert o.order_code in client.get("/admin/orders?view=cancel_requests").text
    assert "asked to cancel this order" in client.get(f"/admin/orders/{o.id}").text
    admin_post(client, f"/admin/orders/{o.id}/status", f"/admin/orders/{o.id}", new_status="CANCELLED", reason="Customer asked")
    reload(db)
    assert db.get(Order, o.id).status == CANCELLED and reserved(db) == 0


def test_case_e_paid_dispatched_order_is_not_refunded_until_admin_decides(db, client):
    chat, o = paid_online(db, client)
    advance(db, o, ACCEPTED, DISPATCHED)
    chat.say(f"cancel {o.order_code}")
    assert "already been dispatched" in chat.text
    reload(db)
    assert db.get(Order, o.id).status == DISPATCHED and db.scalar(select(Payment.status)) == "SUCCESS"
    assert db.scalar(select(Payment.refund_id)) is None


# ---- Case F: delivered
def test_case_f_delivered_order_cannot_be_cancelled(db):
    chat, o = place_cod(db)
    advance(db, o, ACCEPTED, DISPATCHED, DELIVERED)
    chat.say(f"cancel {o.order_code}")
    assert "already been delivered" in chat.text and "cannot be cancelled" in chat.text
    chat.say("cancel")
    assert "no active order" in chat.text
    assert status_of(db, o) == DELIVERED


# ---- races
def test_race_admin_and_customer_cancel_at_the_same_time(db):
    _, o = place_cod(db)
    barrier = Barrier(2)

    def customer(_):
        with SessionLocal() as s:
            barrier.wait()
            return cancellation.customer_cancel_order(s, o.id).outcome

    def admin(_):
        with SessionLocal() as s:
            barrier.wait()
            try:
                change_status(s, s.get(Order, o.id), CANCELLED, "admin cancel", actor="admin", cancelled_by="ADMIN")
                return "ok"
            except OrderError as e:
                return e.code

    with ThreadPoolExecutor(2) as ex:
        f1, f2 = ex.submit(customer, 0), ex.submit(admin, 0)
        c, a = f1.result(), f2.result()
    assert {c, a} in ({"cancelled", "invalid_transition"}, {"already_cancelled", "ok"})
    assert status_of(db, o) == CANCELLED and reserved(db) == 0  # released exactly once, never negative
    assert len(order_audit(db, o)) == 1


def test_race_admin_confirm_vs_customer_cancel_ends_cancelled_with_stock_released(db):
    _, o = place_cod(db)
    barrier = Barrier(2)

    def customer(_):
        with SessionLocal() as s:
            barrier.wait()
            return cancellation.customer_cancel_order(s, o.id).outcome

    def admin(_):
        with SessionLocal() as s:
            barrier.wait()
            try:
                change_status(s, s.get(Order, o.id), ACCEPTED, actor="admin")
                return "ok"
            except OrderError as e:
                return e.code

    with ThreadPoolExecutor(2) as ex:
        f1, f2 = ex.submit(customer, 0), ex.submit(admin, 0)
        assert f1.result() == "cancelled"  # the customer's wish always wins in the end
        f2.result()
    assert status_of(db, o) == CANCELLED and reserved(db) == 0


def test_race_many_duplicate_cancellations_of_a_paid_order_refund_once(db, client):
    _, o = paid_online(db, client)
    barrier = Barrier(5)

    def go(_):
        with SessionLocal() as s:
            barrier.wait()
            return cancellation.customer_cancel_order(s, o.id).outcome

    with ThreadPoolExecutor(5) as ex:
        results = list(ex.map(go, range(5)))
    assert results.count("cancelled") == 1 and results.count("already_cancelled") == 4
    assert len(MockGateway.REFUNDS) == 1 and reserved(db) == 0
    assert len(order_audit(db, o)) == 1
    reload(db)
    assert db.scalar(select(Payment.status)) == "REFUNDED"


def test_race_payment_succeeds_while_customer_cancels(db, client):
    """Webhook-style verification and a cancellation at the same moment: exactly one consistent outcome."""
    chat, o = place_online(db)
    pid = payment_of(db, o).id
    MockGateway.simulate(o.order_code, "success")
    barrier = Barrier(2)

    def pay(_):
        with SessionLocal() as s:
            barrier.wait()
            payments.sync_payment(s, pid)

    def cancel(_):
        with SessionLocal() as s:
            barrier.wait()
            return cancellation.customer_cancel_order(s, o.id).outcome

    with ThreadPoolExecutor(2) as ex:
        f1, f2 = ex.submit(pay, 0), ex.submit(cancel, 0)
        f1.result(), f2.result()
    reload(db)
    o, p = db.get(Order, o.id), db.scalar(select(Payment))
    assert o.status == CANCELLED and reserved(db) == 0  # the customer asked to cancel; payment verified either way
    assert p.status == "REFUNDED" and len(MockGateway.REFUNDS) == 1  # collected money is never kept or lost


# ---- messaging compatibility
def test_template_parameters_have_no_line_breaks(db, monkeypatch):
    """Meta rejects template variables containing newlines: the fallback must flatten them."""
    from app.models import Customer
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_access_token", "tok")
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "1")
    monkeypatch.setattr(s, "whatsapp_template_order_update", "order_update")
    sent = []
    monkeypatch.setattr(messaging, "_post", lambda payload: sent.append(payload) or "wamid.T")
    c = Customer(whatsapp_number="919811122233", channel="whatsapp", language="en")
    db.add(c)
    db.commit()  # no conversation => outside the 24h window => template
    out = messaging.send_notification(db, c, "Line one\n\nLine two\twith tab", ["Ravi", "MF-1", "Line one\n\nLine two\twith tab"])
    assert out == "template"
    params = [p["text"] for p in sent[0]["template"]["components"][0]["parameters"]]
    assert params == ["Ravi", "MF-1", "Line one Line two with tab"]
    assert all("\n" not in p and "\t" not in p for p in params)
