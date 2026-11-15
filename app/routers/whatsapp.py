"""Meta WhatsApp Cloud API webhook + the browser simulator (same conversation engine)."""
import json
import logging
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import Conversation, Customer, MessageLog
from app.security import client_ip, simulator_limiter
from app.services import webhook as wh
from app.services.conversation import handle_incoming
from app.services.messaging import InMessage, verify_signature
from app.web import templates

log = logging.getLogger("mango.webhook")
router = APIRouter()


# ------------------------------------------------------------------ Meta webhook
@router.get("/webhook")
def verify(request: Request):
    q, s = request.query_params, get_settings()
    token = s.whatsapp_verify_token
    if token and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Webhook verification failed")


def _process_in_background(event_ids: list[int]) -> None:
    with SessionLocal() as db:
        for eid in event_ids:
            wh.process_event(db, eid)


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    s = get_settings()
    body = await request.body()
    signature_ok = verify_signature(s.whatsapp_app_secret, body, request.headers.get("x-hub-signature-256"))
    if not signature_ok and not (s.allow_unsigned_webhooks and not s.is_production):
        raise HTTPException(403, "Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    ids = wh.record_events(db, payload)
    if ids:
        background.add_task(_process_in_background, ids)  # acknowledge fast, process after responding
    return {"status": "received", "new_events": len(ids)}


# ------------------------------------------------------------------ simulator
def _enabled():
    if not get_settings().enable_simulator:
        raise HTTPException(404, "Not found")


class SimSend(BaseModel):
    phone: str = Field(pattern=r"^\d{8,15}$")
    text: str = Field(default="", max_length=1000)
    reply_id: str | None = Field(default=None, max_length=200)
    after: int = 0


class SimReset(BaseModel):
    phone: str = Field(pattern=r"^\d{8,15}$")


def _logs_after(db: Session, phone: str, after: int) -> dict:
    rows = db.scalars(select(MessageLog).where(MessageLog.phone == phone, MessageLog.channel == "simulator",
                                               MessageLog.id > after).order_by(MessageLog.id)).all()
    return {"messages": [{"id": r.id, "direction": r.direction, "type": r.message_type, "content": r.content,
                          "status": r.status} for r in rows],
            "last_id": rows[-1].id if rows else after}


@router.get("/simulator", response_class=HTMLResponse)
def simulator_page(request: Request):
    _enabled()
    return templates.TemplateResponse(request, "simulator/index.html", {})


@router.post("/simulator/api/send")
def simulator_send(body: SimSend, request: Request, db: Session = Depends(get_db)):
    _enabled()
    simulator_limiter.check(client_ip(request))
    if not body.text.strip() and not body.reply_id:
        raise HTTPException(422, "Empty message")
    existing = db.scalar(select(Customer).where(Customer.whatsapp_number == body.phone))
    if existing is not None and existing.channel != "simulator":
        raise HTTPException(403, "This number belongs to a real WhatsApp customer. Use a different test number.")
    inm = InMessage(id=f"sim-in-{uuid.uuid4().hex[:12]}", phone=body.phone, text=body.text,
                    reply_id=body.reply_id, kind="reply" if body.reply_id else "text")
    handle_incoming(db, inm, "simulator")
    return _logs_after(db, body.phone, body.after)


@router.get("/simulator/api/messages")
def simulator_messages(phone: str, after: int = 0, db: Session = Depends(get_db)):
    _enabled()
    if not re.fullmatch(r"\d{8,15}", phone):
        raise HTTPException(422, "Invalid phone")
    return _logs_after(db, phone, after)


@router.post("/simulator/api/reset")
def simulator_reset(body: SimReset, db: Session = Depends(get_db)):
    _enabled()
    cust = db.scalar(select(Customer).where(Customer.whatsapp_number == body.phone, Customer.channel == "simulator"))
    if cust:
        conv = db.scalar(select(Conversation).where(Conversation.customer_id == cust.id))
        if conv:
            conv.state, conv.data, conv.last_inbound_at = "LANGUAGE_SELECTION", {}, None
        cust.language = None
    db.execute(delete(MessageLog).where(MessageLog.phone == body.phone, MessageLog.channel == "simulator"))
    db.commit()
    return {"status": "reset"}
