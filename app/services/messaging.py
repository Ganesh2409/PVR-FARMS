"""Outbound message model, providers (simulated / WhatsApp Cloud API) and dispatching."""
import hashlib
import hmac
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Customer, MessageLog
from app.utils import utcnow

log = logging.getLogger("mango.messaging")
WINDOW = timedelta(hours=24)


@dataclass
class Option:
    id: str
    title: str
    desc: str = ""


@dataclass
class OutMessage:
    kind: str  # text | buttons | list | image | cta
    body: str
    options: list[Option] = field(default_factory=list)
    image_url: str | None = None  # kind "image": the picture itself
    button_label: str = "Select"
    footer: str = ""
    header_image: str | None = None  # picture shown on top of a "buttons" / "cta" message (photo cards)
    cta_text: str = ""  # kind "cta": label of the single link button, e.g. "Pay ₹150"
    cta_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InMessage:
    id: str
    phone: str
    text: str = ""
    reply_id: str | None = None
    name: str | None = None
    kind: str = "text"  # text | reply | unsupported


@dataclass
class SendResult:
    wa_message_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------- WhatsApp Cloud API
class WhatsAppAPIError(Exception):
    pass


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def build_payload(to: str, msg: OutMessage) -> dict:
    """Translate an OutMessage to a Cloud API request body, honouring Meta's field limits."""
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    opts = msg.options
    if msg.kind == "image" and msg.image_url:
        return {**base, "type": "image", "image": {"link": msg.image_url, "caption": msg.body[:1024]}}
    header = {"header": {"type": "image", "image": {"link": msg.header_image}}} if msg.header_image else {}
    if msg.kind == "cta":
        if not msg.cta_url.startswith("https://"):  # WhatsApp only accepts https links: fall back to plain text
            return {**base, "type": "text", "text": {"preview_url": True, "body": f"{msg.body}\n\n{msg.cta_url}"[:4096]}}
        inter = {"type": "cta_url", **header, "body": {"text": msg.body[:1024]},
                 "action": {"name": "cta_url", "parameters": {"display_text": msg.cta_text[:20], "url": msg.cta_url}}}
    elif msg.kind == "buttons" and 0 < len(opts) <= 3:
        inter = {"type": "button", **header, "body": {"text": msg.body[:1024]},
                 "action": {"buttons": [{"type": "reply", "reply": {"id": o.id[:256], "title": o.title[:20]}}
                                        for o in opts]}}
    elif msg.kind in ("buttons", "list") and opts:
        inter = {"type": "list", "body": {"text": msg.body[:1024]},
                 "action": {"button": (msg.button_label or "Select")[:20],
                            "sections": [{"title": "Options"[:24], "rows": [
                                {k: v for k, v in {"id": o.id[:200], "title": o.title[:24],
                                                   "description": o.desc[:72]}.items() if v}
                                for o in opts[:10]]}]}}
    else:
        return {**base, "type": "text", "text": {"preview_url": False, "body": msg.body[:4096]}}
    if msg.footer:
        inter["footer"] = {"text": msg.footer[:60]}
    return {**base, "type": "interactive", "interactive": inter}


def build_template_payload(to: str, template: str, lang: str, params: list[str]) -> dict:
    return {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": template, "language": {"code": lang},
                         "components": [{"type": "body",
                                         "parameters": [{"type": "text", "text": p} for p in params]}]}}


def _post(payload: dict) -> str:
    s = get_settings()
    url = f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"
    try:
        r = httpx.post(url, json=payload, timeout=15,
                       headers={"Authorization": f"Bearer {s.whatsapp_access_token}"})
    except httpx.HTTPError as exc:
        raise WhatsAppAPIError(f"network error: {exc}") from exc
    if r.status_code >= 300:
        raise WhatsAppAPIError(f"HTTP {r.status_code}: {r.text[:400]}")
    msgs = r.json().get("messages") or []
    return msgs[0]["id"] if msgs else ""


# ---------------------------------------------------------------- dispatching
def _log_out(db: Session, customer: Customer, msg_type: str, content: dict, res: SendResult) -> MessageLog:
    row = MessageLog(customer_id=customer.id, phone=customer.whatsapp_number, direction="OUT",
                     channel=customer.channel, message_type=msg_type, content=content,
                     wa_message_id=res.wa_message_id, status="failed" if res.error else "sent",
                     error=res.error)
    db.add(row)
    return row


def window_open(db: Session, customer: Customer) -> bool:
    from app.models import Conversation
    conv = db.query(Conversation).filter_by(customer_id=customer.id).first()
    return bool(conv and conv.last_inbound_at and utcnow() - conv.last_inbound_at < WINDOW)


def send_messages(db: Session, customer: Customer, msgs: list[OutMessage]) -> None:
    """Log and deliver messages to a customer (free-form; caller ensures the 24h window)."""
    s = get_settings()
    for m in msgs:
        res = SendResult()
        if customer.channel == "whatsapp":
            if not s.whatsapp_configured:
                res.error = "WhatsApp credentials not configured"
            else:
                try:
                    res.wa_message_id = _post(build_payload(customer.whatsapp_number, m))
                except WhatsAppAPIError as exc:
                    res.error = str(exc)
                    log.error("WhatsApp send failed: %s", exc)
        else:
            res.wa_message_id = f"sim-{uuid.uuid4().hex[:16]}"
        _log_out(db, customer, m.kind, m.to_dict(), res)
    db.commit()


def send_notification(db: Session, customer: Customer, text: str, template_params: list[str]) -> str:
    """Business-initiated message (e.g. order status). Uses the approved template outside the
    24-hour customer-service window. Returns 'sent', 'template' or 'failed'."""
    s = get_settings()
    if customer.channel != "whatsapp" or window_open(db, customer):
        send_messages(db, customer, [OutMessage("text", text)])
        return "sent"
    # Meta rejects template variables containing line breaks, tabs or long runs of spaces: flatten them.
    template_params = [" ".join(str(p).split())[:1000] for p in template_params]
    res = SendResult()
    if not s.whatsapp_template_order_update:
        res.error = "Outside 24h window and WHATSAPP_TEMPLATE_ORDER_UPDATE is not configured"
    elif not s.whatsapp_configured:
        res.error = "WhatsApp credentials not configured"
    else:
        try:
            res.wa_message_id = _post(build_template_payload(
                customer.whatsapp_number, s.whatsapp_template_order_update,
                customer.language or "en", template_params))
        except WhatsAppAPIError as exc:
            res.error = str(exc)
    _log_out(db, customer, "template", {"kind": "template", "body": text, "params": template_params}, res)
    db.commit()
    return "failed" if res.error else "template"
