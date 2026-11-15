import hashlib
import hmac
import json

from sqlalchemy import func, select

from app.models import Customer, MessageLog, Order, WebhookEvent
from app.services import messaging
from app.services.messaging import OutMessage, Option, build_payload, verify_signature

PHONE = "919876500001"


def sign(body: bytes, secret="app-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client, payload, secret="app-secret", signed=True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Hub-Signature-256"] = sign(body, secret)
    return client.post("/webhook", content=body, headers=headers)


def text_event(mid, text, phone=PHONE):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": phone, "profile": {"name": "Ravi"}}],
        "messages": [{"id": mid, "from": phone, "type": "text", "text": {"body": text}}]}}]}]}


def reply_event(mid, rid, title="x", kind="button_reply", phone=PHONE):
    return {"entry": [{"changes": [{"value": {"messages": [{"id": mid, "from": phone, "type": "interactive",
            "interactive": {"type": kind, kind: {"id": rid, "title": title}}}]}}]}]}


def capture_sends(monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_access_token", "tok")
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "999")
    sent = []
    monkeypatch.setattr(messaging, "_post", lambda payload: sent.append(payload) or f"wamid.OUT{len(sent)}")
    return sent


def test_verification_handshake(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"})
    assert ok.status_code == 200 and ok.text == "1234"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"}).status_code == 403


def test_missing_or_bad_signature_rejected(client, db):
    assert post(client, text_event("w1", "Hi"), signed=False).status_code == 403
    assert post(client, text_event("w1", "Hi"), secret="wrong").status_code == 403
    assert db.scalar(select(func.count(WebhookEvent.id))) == 0


def test_signature_function():
    body = b'{"a":1}'
    assert verify_signature("s", body, sign(body, "s"))
    assert not verify_signature("", body, sign(body, ""))
    assert not verify_signature("s", body, None)


def test_message_is_processed_and_reply_sent(client, db, monkeypatch):
    sent = capture_sends(monkeypatch)
    r = post(client, text_event("wamid.A1", "Hi"))
    assert r.status_code == 200 and r.json()["new_events"] == 1
    db.expire_all()
    assert db.scalar(select(WebhookEvent.status)) == "PROCESSED"
    assert db.scalar(select(Customer.channel)) == "whatsapp"
    msg = sent[0]
    assert msg["to"] == PHONE and msg["type"] == "interactive" and msg["interactive"]["type"] == "button"
    assert [b["reply"]["id"] for b in msg["interactive"]["action"]["buttons"]] == ["lang_te", "lang_en"]


def test_duplicate_webhook_delivery_processed_once(client, db, monkeypatch):
    sent = capture_sends(monkeypatch)
    post(client, text_event("wamid.D1", "Hi"))
    r = post(client, text_event("wamid.D1", "Hi"))
    assert r.json()["new_events"] == 0
    assert len(sent) == 1
    assert db.scalar(select(func.count(WebhookEvent.id))) == 1
    assert db.scalar(select(func.count(MessageLog.id)).where(MessageLog.direction == "IN")) == 1


def test_interactive_replies_and_full_order_over_webhook(client, db, monkeypatch):
    sent = capture_sends(monkeypatch)
    n = iter(range(1000))
    mid = lambda: f"wamid.F{next(n)}"
    post(client, text_event(mid(), "Hi"))
    post(client, reply_event(mid(), "lang_en"))
    post(client, reply_event(mid(), "menu_order", kind="list_reply"))
    post(client, reply_event(mid(), "prod_1", kind="list_reply"))
    post(client, reply_event(mid(), "qty_2"))
    dates = [r["id"] for r in sent[-1]["interactive"]["action"]["sections"][0]["rows"]]
    assert dates and all(len(r["title"]) <= 24 for r in sent[-1]["interactive"]["action"]["sections"][0]["rows"])
    post(client, reply_event(mid(), dates[0], kind="list_reply"))
    post(client, text_event(mid(), "Ravi Kumar"))
    post(client, reply_event(mid(), "phone_yes"))
    post(client, reply_event(mid(), "skip"))
    post(client, reply_event(mid(), "area_1", kind="list_reply"))
    post(client, text_event(mid(), "1-2-3 Main Road"))
    post(client, reply_event(mid(), "skip"))
    post(client, reply_event(mid(), "confirm_order"))
    db.expire_all()
    o = db.scalar(select(Order))
    assert o and o.channel == "whatsapp" and o.contact_phone == PHONE
    assert any(o.order_code in m.get("text", {}).get("body", "") for m in sent[-3:])  # payment link message


def test_unsupported_type_gets_polite_reply(client, db, monkeypatch):
    sent = capture_sends(monkeypatch)
    ev = {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.IMG", "from": PHONE, "type": "image",
                                                          "image": {"id": "1"}}]}}]}]}
    post(client, ev)
    assert "can't read" in sent[0]["text"]["body"]


def test_status_updates_update_message_log(client, db, monkeypatch):
    capture_sends(monkeypatch)
    post(client, text_event("wamid.S1", "Hi"))
    wa_id = db.scalar(select(MessageLog.wa_message_id).where(MessageLog.direction == "OUT"))
    status = lambda st, **kw: {"entry": [{"changes": [{"value": {"statuses": [{"id": wa_id, "status": st, **kw}]}}]}]}
    post(client, status("delivered"))
    db.expire_all()
    assert db.scalar(select(MessageLog.status).where(MessageLog.wa_message_id == wa_id)) == "delivered"
    post(client, status("failed", errors=[{"code": 131047, "title": "Re-engagement message"}]))
    db.expire_all()
    row = db.scalar(select(MessageLog).where(MessageLog.wa_message_id == wa_id))
    assert row.status == "failed" and "131047" in row.error
    assert post(client, status("delivered")).json()["new_events"] == 0  # replay ignored


def test_unconfigured_credentials_log_error_not_crash(client, db):
    post(client, text_event("wamid.U1", "Hi"))
    db.expire_all()
    row = db.scalar(select(MessageLog).where(MessageLog.direction == "OUT"))
    assert row.status == "failed" and "not configured" in row.error


def test_payload_builder_limits():
    opts = [Option(f"o{i}", "T" * 40, "D" * 100) for i in range(12)]
    p = build_payload("91x", OutMessage("list", "b" * 2000, opts, button_label="Select something long"))
    rows = p["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 10 and len(rows[0]["title"]) == 24 and len(rows[0]["description"]) == 72
    assert len(p["interactive"]["body"]["text"]) == 1024 and len(p["interactive"]["action"]["button"]) <= 20
    four = build_payload("91x", OutMessage("buttons", "q", opts[:4]))
    assert four["interactive"]["type"] == "list"  # >3 buttons fall back to a list
    img = build_payload("91x", OutMessage("image", "cap", image_url="https://x/y.jpg"))
    assert img["type"] == "image" and img["image"]["link"] == "https://x/y.jpg"


def test_simulator_and_webhook_share_engine(client, db):
    r = client.post("/simulator/api/send", json={"phone": "919000000077", "text": "Hi"})
    assert r.status_code == 200
    out = [m for m in r.json()["messages"] if m["direction"] == "OUT"]
    assert out and out[0]["content"]["options"][0]["id"] == "lang_te"
    r2 = client.post("/simulator/api/send", json={"phone": "919000000077", "text": "", "reply_id": "lang_en", "after": r.json()["last_id"]})
    assert "Welcome! What would you like" in r2.json()["messages"][-1]["content"]["body"]
    assert client.post("/simulator/api/reset", json={"phone": "919000000077"}).status_code == 200
    assert client.get("/simulator/api/messages", params={"phone": "919000000077"}).json()["messages"] == []


def test_simulator_cannot_impersonate_real_customer(client, db, monkeypatch):
    capture_sends(monkeypatch)
    post(client, text_event("wamid.R1", "Hi"))
    r = client.post("/simulator/api/send", json={"phone": PHONE, "text": "Hi"})
    assert r.status_code == 403
