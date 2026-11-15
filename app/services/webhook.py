"""WhatsApp webhook payload parsing, idempotent recording and processing."""
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import MessageLog, WebhookEvent
from app.services.conversation import handle_incoming
from app.services.messaging import InMessage
from app.utils import utcnow

log = logging.getLogger("mango.webhook")


def parse_message(m: dict, contact_name: str | None) -> InMessage:
    mtype = m.get("type")
    base = dict(id=m["id"], phone=m["from"], name=contact_name)
    if mtype == "text":
        return InMessage(**base, text=m["text"].get("body", ""))
    if mtype == "interactive":
        inter = m.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return InMessage(**base, text=reply.get("title", ""), reply_id=reply.get("id"), kind="reply")
    if mtype == "button":
        return InMessage(**base, text=m.get("button", {}).get("text", ""))
    return InMessage(**base, kind="unsupported")


def record_events(db: Session, payload: dict) -> list[int]:
    """Store each message/status once. Duplicates (Meta retries) are ignored. Returns new event ids."""
    new_ids: list[int] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            candidates = []
            for m in value.get("messages", []) or []:
                candidates.append((f"msg:{m['id']}", "message",
                                   {"message": m, "contact_name": names.get(m.get("from"))}))
            for s in value.get("statuses", []) or []:
                candidates.append((f"status:{s['id']}:{s['status']}", "status", {"status": s}))
            for event_id, etype, data in candidates:
                try:
                    with db.begin_nested():
                        ev = WebhookEvent(event_id=event_id, event_type=etype, payload=data)
                        db.add(ev)
                        db.flush()
                    new_ids.append(ev.id)
                except IntegrityError:
                    log.info("duplicate webhook event %s ignored", event_id)
    db.commit()
    return new_ids


def process_event(db: Session, event_id: int) -> None:
    ev = db.get(WebhookEvent, event_id)
    if ev is None or ev.status != "RECEIVED":
        return
    try:
        if ev.event_type == "message":
            data = ev.payload
            handle_incoming(db, parse_message(data["message"], data.get("contact_name")), "whatsapp")
        else:
            s = ev.payload["status"]
            row = db.scalar(select(MessageLog).where(MessageLog.wa_message_id == s["id"]))
            if row:
                row.status = s["status"]
                if s["status"] == "failed":
                    errs = s.get("errors") or [{}]
                    row.error = f"{errs[0].get('code', '')} {errs[0].get('title', '')}".strip()
        ev = db.get(WebhookEvent, event_id)
        ev.status, ev.processed_at = "PROCESSED", utcnow()
    except Exception as exc:
        db.rollback()
        log.exception("webhook event %s failed", event_id)
        ev = db.get(WebhookEvent, event_id)
        ev.status, ev.error, ev.processed_at = "FAILED", str(exc)[:500], utcnow()
    db.commit()
