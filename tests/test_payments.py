import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (AWAITING_PAYMENT, CANCELLED, PENDING, REJECTED, Conversation, Customer,
                        Inventory, InventoryReservation, MessageLog, Order, Payment, Product, WebhookEvent)
from app.services import gateways, notifications, payments
from app.services.core import set_setting
from app.services.gateways import (MOCK_WEBHOOK_SECRET, CashfreeGateway, MockGateway, webhook_signature)
from app.utils import utcnow
from tests.conftest import Chat, allow_cod, drive_to_review

PHONE = "919000000001"
TS = "1700000000000"


# ----------------------------------------------------------------- helpers
def place_online(db, phone=PHONE, qty="qty_2"):
    chat = Chat(db, phone)
    drive_to_review(chat, pay="online", qty=qty)
    chat.pick("confirm_order")
    db.expire_all()
    order = db.scalar(select(Order).order_by(Order.id.desc()))
    return chat, order


def reload(db):
    db.rollback()
    db.expire_all()


def payment_of(db, order) -> Payment:
    reload(db)
    return db.scalar(select(Payment).where(Payment.order_id == order.id))


def reserved(db, product_id=1) -> Decimal:
    reload(db)
    return db.scalar(select(Inventory.quantity_reserved).where(Inventory.product_id == product_id))


def out_bodies(db, phone=PHONE):
    reload(db)
    return [m.content.get("body", "") for m in db.scalars(
        select(MessageLog).where(MessageLog.phone == phone, MessageLog.direction == "OUT").order_by(MessageLog.id))]


def cf_event(order_code, typ="PAYMENT_SUCCESS_WEBHOOK", status="SUCCESS", amount="150.00", payment_id="999"):
    return {"data": {"order": {"order_id": order_code, "order_amount": amount, "order_currency": "INR"},
                     "payment": {"cf_payment_id": payment_id, "payment_status": status, "payment_amount": amount}},
            "event_time": "2026-11-14T10:00:00+05:30", "type": typ}


def send_webhook(client, payload, *, secret=MOCK_WEBHOOK_SECRET, ts=TS, key=None, raw=None, sign=True):
    raw = raw if raw is not None else json.dumps(payload).encode()
    headers = {"content-type": "application/json", "x-webhook-timestamp": ts}
    if sign:
        headers["x-webhook-signature"] = webhook_signature(secret, ts, raw)
    if key:
        headers["x-idempotency-key"] = key
    return client.post("/webhooks/cashfree", content=raw, headers=headers)


# ----------------------------------------------------------------- COD default off, checkout creation
def test_cod_is_off_by_default_and_online_checkout_is_created(db):
    chat = Chat(db)
    drive_to_review(chat, pay="online")
    assert "Confirm & Pay" in " ".join(o.title for m in chat.last for o in m.options)
    assert "Online payment" in chat.last[0].body and "Cash on Delivery" not in chat.last[0].body
    chat.pick("confirm_order")
    order = db.scalar(select(Order))
    pay = payment_of(db, order)
    assert order.status == AWAITING_PAYMENT and order.payment_method == "ONLINE" and order.payment_status == "UNPAID"
    assert pay.status == "PENDING" and pay.provider_order_id == order.order_code
    assert pay.payment_session_id.startswith("mock_session_") and pay.cf_order_id
    assert pay.amount == order.total == Decimal("150.00")
    cta = next(m for m in chat.last if m.kind == "cta")  # a real "Pay ₹150" button, not a raw link in text
    assert cta.cta_url == f"https://shop.test/pay/{pay.checkout_token}" and cta.cta_text == "Pay ₹150"
    assert len(pay.checkout_token) >= 40  # unguessable
    assert pay.expires_at > utcnow() + timedelta(minutes=25)
    assert "Your order has been successfully placed" not in chat.text  # NOT confirmed yet


def test_each_checkout_gets_a_unique_session_and_token(db):
    _, o1 = place_online(db, "919000000001")
    _, o2 = place_online(db, "919000000002")
    p1, p2 = payment_of(db, o1), payment_of(db, o2)
    assert p1.payment_session_id != p2.payment_session_id and p1.checkout_token != p2.checkout_token
    assert p1.provider_order_id != p2.provider_order_id


@pytest.mark.parametrize("global_on,customer_on,offered", [(False, True, False), (True, False, False), (True, True, True)])
def test_cod_needs_global_switch_and_per_customer_flag(db, global_on, customer_on, offered):
    set_setting(db, "cod_enabled", "true" if global_on else "false")
    db.add(Customer(whatsapp_number=PHONE, channel="simulator", cod_allowed=customer_on))
    db.commit()
    chat = Chat(db)
    drive_to_review(chat, pay="online")
    reload(db)
    asked = any("pay_cod" in json.dumps(m.content) for m in db.scalars(
        select(MessageLog).where(MessageLog.direction == "OUT")))
    assert asked is offered  # the payment-method question appears only when COD is genuinely available
    assert "Online payment" in chat.last[0].body  # and online is what we chose


def test_cod_flow_still_works_when_allowed(db):
    chat = Chat(db)
    drive_to_review(chat, pay="cod")
    assert "Cash on Delivery" in chat.last[0].body and "Confirm Order" in " ".join(o.title for m in chat.last for o in m.options)
    chat.pick("confirm_order")
    o = db.scalar(select(Order))
    assert o.status == PENDING and o.payment_method == "COD" and "waiting for confirmation" in chat.text
    assert db.scalar(select(func.count(Payment.id))) == 0


# ----------------------------------------------------------------- an unpaid order never becomes confirmed
def test_unpaid_order_is_never_confirmed(db, client):
    chat, order = place_online(db)
    pay = payment_of(db, order)
    # 1. customer returns to the "success" page / spoofed query string
    r = client.get(f"/pay/{pay.checkout_token}/return?status=SUCCESS&order_status=PAID&txStatus=SUCCESS")
    assert r.status_code == 200 and "Payment received" not in r.text
    # 2. customer claims to have paid / sends a screenshot caption
    chat.say("I have paid, see screenshot")
    # 3. a correctly signed webhook CLAIMING success while the gateway says ACTIVE
    r = send_webhook(client, cf_event(order.order_code))
    assert r.status_code == 200
    reload(db)
    order = db.get(Order, order.id)
    assert order.status == AWAITING_PAYMENT and order.payment_status == "UNPAID"
    assert db.scalar(select(Payment.status)) == "PENDING"
    assert not any("successfully placed" in b for b in out_bodies(db))
    assert "Payment received" not in client.get(f"/pay/{pay.checkout_token}").text


# ----------------------------------------------------------------- success
def test_verified_success_confirms_order_and_notifies(db, client):
    chat, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")
    assert send_webhook(client, cf_event(order.order_code)).status_code == 200
    reload(db)
    order = db.get(Order, order.id)
    pay = db.scalar(select(Payment))
    assert order.status == PENDING and order.payment_status == "PAID"
    assert pay.status == "SUCCESS" and pay.cf_payment_id == "mockpay1" and pay.bank_reference == "BANKREFmockpay1"
    assert pay.payment_group == "upi" and pay.paid_at is not None
    bodies = out_bodies(db)
    assert any("Your order has been successfully placed" in b and "Payment Reference: mockpay1" in b for b in bodies)
    assert db.scalar(select(Conversation.state)) == "ORDER_CONFIRMED"
    assert reserved(db) == Decimal("2")  # stays reserved until delivery
    assert "Payment received" in client.get(f"/pay/{pay.checkout_token}").text


def test_success_message_in_telugu(db, client):
    chat = Chat(db)
    drive_to_review(chat, lang="te", pay="online").pick("confirm_order")
    order = db.scalar(select(Order))
    assert "కింద బటన్ నొక్కి" in chat.text and chat.last[-1].cta_text == "₹150 చెల్లించండి"
    MockGateway.simulate(order.order_code, "success")
    send_webhook(client, cf_event(order.order_code))
    assert any("మీ ఆర్డర్ విజయవంతంగా నమోదు చేయబడింది!" in b and "ఆన్‌లైన్ (చెల్లించబడింది" in b for b in out_bodies(db))


def test_return_page_verifies_with_gateway_not_query_string(db, client):
    _, order = place_online(db)
    pay = payment_of(db, order)
    MockGateway.simulate(order.order_code, "success")
    r = client.get(f"/pay/{pay.checkout_token}/return")  # no query params at all
    assert "Payment received" in r.text
    reload(db)
    assert db.get(Order, order.id).status == PENDING


def test_amount_mismatch_is_not_confirmed(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "success", amount=Decimal("1.00"))
    send_webhook(client, cf_event(order.order_code))
    reload(db)
    assert db.get(Order, order.id).status == AWAITING_PAYMENT
    pay = db.scalar(select(Payment))
    assert pay.status == "PENDING" and "AMOUNT_MISMATCH" in pay.needs_attention


# ----------------------------------------------------------------- duplicates / idempotency
def test_duplicate_webhooks_are_idempotent(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")
    ev = cf_event(order.order_code)
    assert send_webhook(client, ev, key="evt-1").json()["status"] == "received"
    assert send_webhook(client, ev, key="evt-1").json()["status"] == "duplicate"        # same delivery retried
    assert send_webhook(client, ev, key="evt-2").json()["status"] == "received"          # new event id, same facts
    assert send_webhook(client, ev, key="evt-3", ts="1700000000999").status_code == 200  # again, other timestamp
    reload(db)
    confirmations = [b for b in out_bodies(db) if "successfully placed" in b]
    assert len(confirmations) == 1
    assert reserved(db) == Decimal("2")
    assert db.scalar(select(func.count(Payment.id))) == 1
    assert db.scalar(select(Order.status)) == PENDING
    assert db.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.event_type == "payment")) == 3  # the exact retry was not stored again


def test_webhook_without_idempotency_header_dedupes_by_content(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")
    raw = json.dumps(cf_event(order.order_code)).encode()
    assert send_webhook(client, None, raw=raw).json()["status"] == "received"
    assert send_webhook(client, None, raw=raw).json()["status"] == "duplicate"


def test_concurrent_verifications_confirm_once(db):
    from concurrent.futures import ThreadPoolExecutor
    from app.database import SessionLocal
    _, order = place_online(db)
    pid = payment_of(db, order).id
    MockGateway.simulate(order.order_code, "success")

    def go(_):
        with SessionLocal() as s:
            payments.sync_payment(s, pid)

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(go, range(6)))
    assert len([b for b in out_bodies(db) if "successfully placed" in b]) == 1
    assert reserved(db) == Decimal("2")
    assert db.scalar(select(Order.status)) == PENDING


# ----------------------------------------------------------------- signature verification
def test_webhook_signature_verification(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")
    ev = cf_event(order.order_code)
    raw = json.dumps(ev).encode()
    assert send_webhook(client, ev, secret="wrong-secret").status_code == 401
    assert send_webhook(client, ev, sign=False).status_code == 401
    assert client.post("/webhooks/cashfree", content=raw, headers={"x-webhook-timestamp": TS}).status_code == 401
    # signature valid for another timestamp / another body
    sig = webhook_signature(MOCK_WEBHOOK_SECRET, TS, raw)
    assert client.post("/webhooks/cashfree", content=raw, headers={"x-webhook-timestamp": "1", "x-webhook-signature": sig}).status_code == 401
    assert client.post("/webhooks/cashfree", content=raw + b" ", headers={"x-webhook-timestamp": TS, "x-webhook-signature": sig}).status_code == 401
    reload(db)
    assert db.scalar(select(func.count(WebhookEvent.id))) == 0           # nothing stored for rejected calls
    assert db.get(Order, order.id).status == AWAITING_PAYMENT            # and nothing confirmed
    assert send_webhook(client, ev).status_code == 200


def test_signature_algorithm_matches_cashfree_spec():
    """Base64( HMAC-SHA256(secret, timestamp + rawBody) ), no separator - computed independently here."""
    import base64, hashlib, hmac
    raw = b'{"data":{"payment":{"payment_amount":170.00}},"type":"PAYMENT_SUCCESS_WEBHOOK"}'
    expected = base64.b64encode(hmac.new(b"sekret", b"1617695238078" + raw, hashlib.sha256).digest()).decode()
    assert webhook_signature("sekret", "1617695238078", raw) == expected


def test_cashfree_gateway_verifies_with_client_secret(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_client_id", "app_id")
    monkeypatch.setattr(s, "cashfree_client_secret", "cf_secret")
    gw = CashfreeGateway()
    raw = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
    assert gw.verify_webhook_signature(raw, webhook_signature("cf_secret", TS, raw), TS)
    assert not gw.verify_webhook_signature(raw, webhook_signature("other", TS, raw), TS)
    assert not gw.verify_webhook_signature(raw, None, TS) and not gw.verify_webhook_signature(raw, "x", None)


def test_webhook_for_unknown_order_and_other_event_types_are_acknowledged(db, client):
    assert send_webhook(client, cf_event("MF-00000000-0000")).status_code == 200
    assert send_webhook(client, {"type": "PAYMENT_CHARGES_WEBHOOK", "data": {}}).status_code == 200
    reload(db)
    assert {e.status for e in db.scalars(select(WebhookEvent))} <= {"IGNORED", "RECEIVED", "PROCESSED"}


# ----------------------------------------------------------------- failure handling
def test_failed_attempt_keeps_hold_and_allows_retry(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "failed")
    send_webhook(client, cf_event(order.order_code, "PAYMENT_FAILED_WEBHOOK", "FAILED"))
    reload(db)
    pay = db.scalar(select(Payment))
    assert pay.status == "FAILED" and "declined" in pay.failure_reason
    assert db.get(Order, order.id).status == AWAITING_PAYMENT and reserved(db) == Decimal("2")
    assert any("did not go through" in b and pay.checkout_token in b for b in out_bodies(db))
    # the same failure notified again does not spam the customer
    send_webhook(client, cf_event(order.order_code, "PAYMENT_FAILED_WEBHOOK", "FAILED"), key="again")
    assert len([b for b in out_bodies(db) if "did not go through" in b]) == 1
    # customer retries successfully
    MockGateway.simulate(order.order_code, "success")
    send_webhook(client, cf_event(order.order_code), key="ok")
    reload(db)
    assert db.get(Order, order.id).status == PENDING and db.scalar(select(Payment.status)) == "SUCCESS"


def test_failed_attempt_can_release_stock_immediately_when_configured(db, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "payment_release_on_failed_attempt", True)
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "failed")
    send_webhook(client, cf_event(order.order_code, "PAYMENT_FAILED_WEBHOOK", "FAILED"))
    reload(db)
    assert db.get(Order, order.id).status == CANCELLED and reserved(db) == 0
    assert db.scalar(select(Payment.status)) == "FAILED" and MockGateway.ORDERS[order.order_code]["terminated"]
    assert any("payment failed" in b for b in out_bodies(db))


def test_user_dropped_is_a_failed_attempt(db, client):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "dropped")
    send_webhook(client, cf_event(order.order_code, "PAYMENT_USER_DROPPED_WEBHOOK", "USER_DROPPED"))
    reload(db)
    assert db.scalar(select(Payment.status)) == "FAILED" and db.get(Order, order.id).status == AWAITING_PAYMENT


def test_gateway_down_at_checkout_leaves_nothing_stuck(db):
    MockGateway.fail_create = True
    chat = Chat(db)
    drive_to_review(chat, pay="online").pick("confirm_order")
    assert "could not start the online payment" in chat.text
    reload(db)
    o = db.scalar(select(Order))
    assert o.status == CANCELLED and reserved(db) == 0
    assert db.scalar(select(Payment.status)) == "FAILED"
    assert len(chat.option_ids) == 3  # back at the main menu


def test_missing_public_url_cannot_start_checkout(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    chat = Chat(db)
    drive_to_review(chat, pay="online").pick("confirm_order")
    reload(db)
    assert db.scalar(select(Order.status)) == CANCELLED and reserved(db) == 0


# ----------------------------------------------------------------- expiry, cancellation, stock hold
def test_stock_is_held_during_checkout_and_blocks_others(db):
    db.get(Product, 1).inventory.quantity_on_hand = Decimal("6")
    db.commit()
    _, order = place_online(db, qty="qty_5")
    assert reserved(db) == Decimal("5")
    other = Chat(db, "919000000002")
    other.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_custom").say("3")
    assert "only 1 kg" in other.text


def test_expiry_cancels_order_and_releases_stock(db):
    chat, order = place_online(db)
    pay = payment_of(db, order)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    assert payments.run_maintenance(db) >= 1
    reload(db)
    o, p = db.get(Order, order.id), db.scalar(select(Payment))
    assert p.status == "EXPIRED" and p.closed_reason == "EXPIRED"
    assert o.status == CANCELLED and o.cancel_reason == "Payment expired"
    assert reserved(db) == 0 and db.scalar(select(InventoryReservation.status)) == "RELEASED"
    assert MockGateway.ORDERS[order.order_code]["terminated"]
    assert any("payment time" in b for b in out_bodies(db))
    assert db.scalar(select(Conversation.state)) == "MAIN_MENU"
    # running the sweeper again changes nothing
    assert payments.run_maintenance(db) == 0


def test_gateway_reported_expiry(db):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "expired")
    payments.sync_payment(db, payment_of(db, order).id)
    reload(db)
    assert db.get(Order, order.id).status == CANCELLED and reserved(db) == 0


def test_sweeper_confirms_a_payment_whose_webhook_never_arrived(db):
    _, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")  # no webhook sent at all
    payments.run_maintenance(db)
    reload(db)
    assert db.get(Order, order.id).status == PENDING


def test_sweeper_does_not_expire_a_payment_that_actually_succeeded(db):
    _, order = place_online(db)
    pay = payment_of(db, order)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    MockGateway.simulate(order.order_code, "success")
    payments.run_maintenance(db)
    reload(db)
    assert db.get(Order, order.id).status == PENDING and db.scalar(select(Payment.status)) == "SUCCESS"


def test_customer_cancels_unpaid_order(db):
    chat, order = place_online(db)
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    assert db.get(Order, order.id).status == CANCELLED and reserved(db) == 0
    assert db.scalar(select(Payment.status)) == "EXPIRED" and db.scalar(select(Payment.closed_reason)) == "CANCELLED"
    assert "No payment was taken" in chat.text and MockGateway.ORDERS[order.order_code]["terminated"]


def test_customer_cancel_by_text_command(db):
    chat, order = place_online(db)
    chat.say("cancel")
    assert "Do you want to cancel order" in chat.text
    chat.pick("cancel_yes")
    reload(db)
    assert db.get(Order, order.id).status == CANCELLED
    chat.say(f"cancel {order.order_code}")  # already cancelled: harmless
    assert "already cancelled" in chat.text
    reload(db)
    assert db.get(Order, order.id).status == CANCELLED


def test_cancel_never_discards_a_payment_that_just_succeeded(db):
    """Paid a moment before the customer cancelled: the money is verified and REFUNDED, never lost."""
    chat, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")  # paid, webhook not yet processed
    chat.say("cancel").pick("cancel_yes")
    reload(db)
    p = db.scalar(select(Payment))
    assert db.get(Order, order.id).status == CANCELLED and reserved(db) == 0
    assert p.cf_payment_id == "mockpay1" and p.refund_id == f"RF-{order.order_code}" and p.status == "REFUNDED"
    assert "refunding the ₹150" in chat.text


def test_payment_pending_chat_reports_status_instead_of_trusting_claims(db):
    chat, order = place_online(db)
    chat.say("paid!!")
    cta = chat.last[0]
    assert cta.kind == "cta" and cta.cta_url.endswith(payment_of(db, order).checkout_token)
    reload(db)
    assert db.get(Order, order.id).status == AWAITING_PAYMENT


# ----------------------------------------------------------------- late payments
def test_late_success_after_expiry_revives_order_when_stock_allows(db):
    _, order = place_online(db)
    pay = payment_of(db, order)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    payments.run_maintenance(db)
    MockGateway.ORDERS[order.order_code]["status"] = "ACTIVE"
    MockGateway.simulate(order.order_code, "success")
    payments.sync_payment(db, pay.id)
    reload(db)
    o = db.get(Order, order.id)
    assert o.status == PENDING and o.payment_status == "PAID" and reserved(db) == Decimal("2")
    assert db.scalar(select(Payment.status)) == "SUCCESS" and db.scalar(select(Payment.closed_reason)) is None


def test_late_success_without_stock_is_refunded_automatically(db):
    _, order = place_online(db)
    pay = payment_of(db, order)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    payments.run_maintenance(db)
    reload(db)
    db.scalar(select(Inventory).where(Inventory.product_id == 1)).quantity_on_hand = Decimal("1")  # sold out meanwhile
    db.commit()
    MockGateway.ORDERS[order.order_code]["status"] = "ACTIVE"
    MockGateway.simulate(order.order_code, "success")
    payments.sync_payment(db, pay.id)
    reload(db)
    p = db.scalar(select(Payment))
    assert db.get(Order, order.id).status == CANCELLED
    assert p.status == "REFUNDED" and p.closed_reason == "LATE_PAYMENT" and p.refund_status == "SUCCESS"
    assert db.get(Order, order.id).payment_status == "REFUNDED"


# ----------------------------------------------------------------- refunds
def paid_order(db, client):
    chat, order = place_online(db)
    MockGateway.simulate(order.order_code, "success")
    send_webhook(client, cf_event(order.order_code))
    reload(db)
    return chat, db.get(Order, order.id)


def test_refund_is_idempotent_and_marks_refunded(db, client):
    _, order = paid_order(db, client)
    pid = payment_of(db, order).id
    p = payments.refund_payment(db, pid, "test")
    assert p.status == "REFUNDED" and p.refund_id == f"RF-{order.order_code}" and p.refund_amount == Decimal("150.00")
    with pytest.raises(payments.PaymentError):
        payments.refund_payment(db, pid, "again")  # already refunded -> refused, never double refund
    assert len(MockGateway.REFUNDS) == 1


def test_unpaid_payment_cannot_be_refunded(db):
    _, order = place_online(db)
    with pytest.raises(payments.PaymentError):
        payments.refund_payment(db, payment_of(db, order).id)


def test_pending_refund_is_tracked_until_gateway_completes_it(db, client):
    _, order = paid_order(db, client)
    pid = payment_of(db, order).id
    MockGateway.refund_outcome = "PENDING"
    p = payments.refund_payment(db, pid, "slow bank")
    assert p.status == "SUCCESS" and p.refund_status == "PENDING"
    payments.refund_payment(db, pid)  # repeat while pending: no second gateway call
    assert len(MockGateway.REFUNDS) == 1
    MockGateway.REFUNDS[p.refund_id]["status"] = "SUCCESS"
    payments.run_maintenance(db)
    reload(db)
    assert db.scalar(select(Payment.status)) == "REFUNDED"
    assert any("refund of ₹150" in b for b in out_bodies(db))


def test_admin_rejecting_paid_order_refunds_it(db, client):
    _, order = paid_order(db, client)
    admin_login(client)
    r = admin_post(client, f"/admin/orders/{order.id}/status", f"/admin/orders/{order.id}",
                   new_status="REJECTED", reason="Out of area")
    assert r.status_code == 303
    reload(db)
    o, p = db.get(Order, order.id), db.scalar(select(Payment))
    assert o.status == REJECTED and reserved(db) == 0
    assert p.status == "REFUNDED" and o.payment_status == "REFUNDED"
    assert any("refund" in b.lower() for b in out_bodies(db))


def test_admin_manual_refund_and_refresh(db, client):
    _, order = paid_order(db, client)
    admin_login(client)
    page = client.get(f"/admin/orders/{order.id}").text
    for needle in ("mockpay1", "BANKREFmockpay1", order.order_code, "SUCCESS", "Refund in full", "upi"):
        assert needle in page
    assert admin_post(client, f"/admin/orders/{order.id}/payment/refresh", f"/admin/orders/{order.id}").status_code == 303
    assert admin_post(client, f"/admin/orders/{order.id}/payment/refund", f"/admin/orders/{order.id}", note="goodwill").status_code == 303
    reload(db)
    assert db.scalar(select(Payment.status)) == "REFUNDED"
    assert "REFUNDED" in client.get(f"/admin/orders/{order.id}").text


def test_refund_gateway_failure_is_recorded_not_lost(db, client, monkeypatch):
    _, order = paid_order(db, client)
    monkeypatch.setattr(MockGateway, "create_refund", lambda *a, **k: (_ for _ in ()).throw(gateways.GatewayError("boom")))
    with pytest.raises(payments.PaymentError):
        payments.refund_payment(db, payment_of(db, order).id)
    reload(db)
    p = db.scalar(select(Payment))
    assert p.status == "SUCCESS" and p.refund_status == "FAILED" and "REFUND_FAILED" in p.needs_attention


# ----------------------------------------------------------------- admin visibility & COD toggle
def admin_login(client):
    import re
    tok = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin/login").text).group(1)
    client.post("/admin/login", data={"username": "admin", "password": "test-password-123", "csrf_token": tok})


def admin_post(client, url, page="/admin", **data):
    import re
    tok = re.search(r'name="csrf_token" value="([^"]+)"', client.get(page).text).group(1)
    return client.post(url, data={"csrf_token": tok, **data}, follow_redirects=False)


def test_admin_lists_show_payment_state(db, client):
    _, order = place_online(db)
    admin_login(client)
    html = client.get("/admin/orders?view=awaiting").text
    assert order.order_code in html and "ONLINE" in html and "AWAITING_PAYMENT" in html
    assert order.order_code not in client.get("/admin/orders?view=new").text  # unpaid orders are not "new"
    assert "Awaiting payment" in client.get("/admin").text
    detail = client.get(f"/admin/orders/{order.id}").text
    assert "PENDING" in detail and "mock_session_" in detail and "Refund in full" not in detail
    assert "mock" in client.get("/admin/settings").text and "/webhooks/cashfree" in client.get("/admin/settings").text


def test_admin_cannot_accept_unpaid_order(db, client):
    _, order = place_online(db)
    admin_login(client)
    admin_post(client, f"/admin/orders/{order.id}/status", f"/admin/orders/{order.id}", new_status="ACCEPTED")
    reload(db)
    assert db.get(Order, order.id).status == AWAITING_PAYMENT


def test_admin_toggles_cod_for_a_customer(db, client):
    chat = Chat(db)
    chat.say("Hi")
    cid = db.scalar(select(Customer.id))
    admin_login(client)
    admin_post(client, f"/admin/customers/{cid}/cod", f"/admin/customers/{cid}", cod_allowed="on")
    reload(db)
    assert db.get(Customer, cid).cod_allowed is True
    admin_post(client, f"/admin/customers/{cid}/cod", f"/admin/customers/{cid}")
    reload(db)
    assert db.get(Customer, cid).cod_allowed is False


# ----------------------------------------------------------------- pay page / secrets
def test_pay_page_never_leaks_credentials(db, client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_client_secret", "SUPER-SECRET-VALUE")
    monkeypatch.setattr(s, "cashfree_client_id", "CLIENT-ID-VALUE")
    _, order = place_online(db)
    pay = payment_of(db, order)
    html = client.get(f"/pay/{pay.checkout_token}").text
    assert "SUPER-SECRET-VALUE" not in html and "CLIENT-ID-VALUE" not in html
    assert order.order_code in html and "₹150" in html
    assert client.get("/pay/not-a-real-token").status_code == 404
    assert "Pay ₹150" in html or "MOCK GATEWAY" in html


def test_pay_page_for_expired_payment_offers_no_pay_button(db, client):
    _, order = place_online(db)
    pay = payment_of(db, order)
    pay.expires_at = utcnow() - timedelta(minutes=5)
    db.commit()
    html = client.get(f"/pay/{pay.checkout_token}").text
    assert "link closed" in html and "MOCK GATEWAY" not in html


def test_mock_provider_refused_in_production(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "environment", "production")
    monkeypatch.setattr(s, "payment_provider", "mock")
    with pytest.raises(gateways.GatewayError):
        gateways.get_gateway()


def test_mock_buttons_work_only_for_mock_payments(db, client):
    _, order = place_online(db)
    pay = payment_of(db, order)
    r = client.post(f"/pay/{pay.checkout_token}/mock/success", follow_redirects=True)
    assert "Payment received" in r.text
    reload(db)
    assert db.get(Order, order.id).status == PENDING


# ----------------------------------------------------------------- Cashfree HTTP client (request shape per official docs)
class FakeHTTP:
    def __init__(self, responses):
        self.calls, self.responses = [], responses

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body)


@pytest.fixture
def cashfree(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_client_id", "TEST_APP_ID")
    monkeypatch.setattr(s, "cashfree_client_secret", "TEST_SECRET")
    monkeypatch.setattr(s, "cashfree_env", "sandbox")
    monkeypatch.setattr(s, "cashfree_api_version", "")
    return s


def test_cashfree_create_order_request(cashfree, monkeypatch):
    http = FakeHTTP([(200, {"cf_order_id": 2149460581, "order_id": "MF-1", "order_status": "ACTIVE",
                            "payment_session_id": "session_abc"})])
    monkeypatch.setattr(httpx, "request", http)
    g = CashfreeGateway().create_order(
        provider_order_id="MF-20261114-0001", amount=Decimal("150.00"), customer_id="C7", customer_phone="9876543210",
        customer_name="Ravi", return_url="https://shop.test/pay/t/return", notify_url="https://shop.test/webhooks/cashfree",
        expires_at=utcnow() + timedelta(minutes=30), note="Mango Farm order")
    c = http.calls[0]
    assert c["method"] == "POST" and c["url"] == "https://sandbox.cashfree.com/pg/orders"
    assert c["headers"]["x-client-id"] == "TEST_APP_ID" and c["headers"]["x-client-secret"] == "TEST_SECRET"
    assert c["headers"]["x-api-version"] == "2026-01-01" and c["headers"]["x-idempotency-key"]
    b = c["json"]
    assert b["order_id"] == "MF-20261114-0001" and b["order_amount"] == 150.0 and b["order_currency"] == "INR"
    assert b["customer_details"] == {"customer_id": "C7", "customer_phone": "9876543210", "customer_name": "Ravi"}
    assert b["order_meta"] == {"return_url": "https://shop.test/pay/t/return", "notify_url": "https://shop.test/webhooks/cashfree"}
    assert b["order_expiry_time"].endswith("Z") and "TEST_SECRET" not in json.dumps(b)
    assert g.payment_session_id == "session_abc" and g.cf_order_id == "2149460581"


def test_cashfree_production_url_and_configured_api_version(cashfree, monkeypatch):
    monkeypatch.setattr(cashfree, "cashfree_env", "production")
    monkeypatch.setattr(cashfree, "cashfree_api_version", "2025-01-01")
    http = FakeHTTP([(200, {"order_status": "ACTIVE", "order_amount": 10}), (200, [])])
    monkeypatch.setattr(httpx, "request", http)
    CashfreeGateway().get_status("MF-1")
    assert http.calls[0]["url"] == "https://api.cashfree.com/pg/orders/MF-1"
    assert http.calls[0]["headers"]["x-api-version"] == "2025-01-01"
    assert http.calls[1]["url"].endswith("/orders/MF-1/payments")


def test_cashfree_get_status_parses_order_and_payments(cashfree, monkeypatch):
    monkeypatch.setattr(httpx, "request", FakeHTTP([
        (200, {"order_status": "PAID", "order_amount": 150.0}),
        (200, [{"cf_payment_id": 555, "payment_status": "FAILED", "payment_amount": 150.0},
               {"cf_payment_id": 556, "payment_status": "SUCCESS", "payment_amount": 150.0, "bank_reference": "B1",
                "payment_group": "upi", "payment_completion_time": "2026-11-14T10:00:00+05:30"}])]))
    gs = CashfreeGateway().get_status("MF-1")
    assert gs.order_status == "PAID" and gs.order_amount == Decimal("150.00")
    assert [p.status for p in gs.payments] == ["FAILED", "SUCCESS"]
    assert gs.payments[1].cf_payment_id == "556" and gs.payments[1].bank_reference == "B1" and gs.payments[1].payment_group == "upi"


def test_cashfree_refund_and_terminate_requests(cashfree, monkeypatch):
    http = FakeHTTP([(200, {"refund_id": "RF-1", "refund_status": "PENDING", "cf_refund_id": 9}),
                     (200, {"order_status": "TERMINATED"})])
    monkeypatch.setattr(httpx, "request", http)
    gw = CashfreeGateway()
    r = gw.create_refund("MF-1", "RF-1", Decimal("150.00"), "note")
    gw.terminate_order("MF-1")
    c = http.calls[0]
    assert c["url"].endswith("/pg/orders/MF-1/refunds") and c["json"] == {
        "refund_amount": 150.0, "refund_id": "RF-1", "refund_note": "note", "refund_speed": "STANDARD"}
    assert r.status == "PENDING" and r.cf_refund_id == "9"
    assert http.calls[1]["method"] == "PATCH" and http.calls[1]["json"] == {"order_status": "TERMINATED"}


def test_cashfree_errors_do_not_leak_secrets(cashfree, monkeypatch):
    monkeypatch.setattr(httpx, "request", FakeHTTP([(401, {"message": "authentication Failed", "code": "x"})]))
    with pytest.raises(gateways.GatewayError) as e:
        CashfreeGateway().get_status("MF-1")
    assert "401" in str(e.value) and "TEST_SECRET" not in str(e.value)


def test_cashfree_unconfigured_raises(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_client_id", "")
    monkeypatch.setattr(s, "cashfree_client_secret", "")
    with pytest.raises(gateways.GatewayError):
        CashfreeGateway()


def test_full_flow_through_cashfree_gateway_class(db, client, cashfree, monkeypatch):
    """Whole journey with the real CashfreeGateway code and a faked HTTP layer."""
    state = {"paid": False}

    def fake(method, url, json=None, headers=None, timeout=None):
        if method == "POST" and url.endswith("/orders"):
            state["order_id"] = json["order_id"]
            return httpx.Response(200, json={"cf_order_id": 1, "order_status": "ACTIVE", "payment_session_id": "session_real"})
        if method == "GET" and url.endswith("/payments"):
            return httpx.Response(200, json=[{"cf_payment_id": 77, "payment_status": "SUCCESS", "payment_amount": 150.0,
                                              "bank_reference": "UTR1", "payment_group": "upi"}] if state["paid"] else [])
        if method == "GET":
            return httpx.Response(200, json={"order_status": "PAID" if state["paid"] else "ACTIVE", "order_amount": 150.0})
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(get_settings(), "payment_provider", "cashfree")
    chat, order = place_online(db)
    pay = payment_of(db, order)
    assert pay.provider == "cashfree" and pay.payment_session_id == "session_real"
    html = client.get(f"/pay/{pay.checkout_token}").text
    assert "sdk.cashfree.com/js/v3/cashfree.js" in html and '"session_real"' in html and 'mode:"sandbox"' in html
    assert "TEST_SECRET" not in html
    # unsigned/forged webhook is rejected, order stays unpaid
    assert client.post("/webhooks/cashfree", content=b"{}", headers={"x-webhook-timestamp": TS, "x-webhook-signature": "bogus"}).status_code == 401
    state["paid"] = True
    ev = cf_event(order.order_code, payment_id="77")
    assert send_webhook(client, ev, secret="TEST_SECRET").status_code == 200
    reload(db)
    p = db.scalar(select(Payment))
    assert db.get(Order, order.id).status == PENDING and p.cf_payment_id == "77" and p.bank_reference == "UTR1"
