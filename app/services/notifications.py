"""Customer and administrator notifications for order events."""
import logging

from sqlalchemy.orm import Session

from app.i18n import t
from app.models import Conversation, MessageLog, Order
from app.services import messaging
from app.services.core import get_setting
from app.utils import fmt_date, fmt_datetime, money, qty_str

log = logging.getLogger("mango.notifications")


def notify_customer(db: Session, order: Order, event: str, reason: str | None = None) -> str:
    """event is an order status (ACCEPTED, REJECTED, ...) or DATE_CHANGED."""
    lang = order.language
    text = t(f"n_{event}", lang, name=order.customer_name, order_id=order.order_code,
             date=fmt_date(order.delivery_date), reason=reason or order.reject_reason or order.cancel_reason or "",
             farm=get_setting(db, "farm_name"))
    if event == "CANCELLED" and not reason:
        text = text.strip()
    customer = order.customer
    if customer.language is None:
        customer.language = lang
    try:
        return messaging.send_notification(db, customer, text, [order.customer_name, order.order_code, text])
    except Exception:  # never let a notification failure break an admin action
        log.exception("customer notification failed for %s", order.order_code)
        db.rollback()
        return "failed"


def _admin_text(db: Session, text: str) -> None:
    phone = get_setting(db, "admin_notify_phone").strip()
    if not phone:
        return
    s = messaging.get_settings()
    error, wa_id = None, None
    if not s.whatsapp_configured:
        error = "WhatsApp credentials not configured"
    else:
        try:
            wa_id = messaging._post(messaging.build_payload(phone, messaging.OutMessage("text", text)))
        except messaging.WhatsAppAPIError as exc:
            error = str(exc)
    db.add(MessageLog(phone=phone, direction="OUT", channel="whatsapp", message_type="admin_alert",
                      content={"body": text}, wa_message_id=wa_id, status="failed" if error else "sent",
                      error=error))
    db.commit()


def notify_admin_new_order(db: Session, order: Order) -> None:
    item = order.items[0]
    kind = ("CASH ON DELIVERY - please CALL the customer to verify, then confirm in the dashboard"
            if order.payment_method == "COD" else "PAID online - please confirm in the dashboard")
    text = (f"🥭 New order {order.order_code}: {kind}\n{order.customer_name} ({order.contact_phone})\n"
            f"{item.product_name_en} x {item.quantity.normalize():f} kg = ₹{money(order.total)}\n"
            f"Delivery {fmt_date(order.delivery_date)} - {order.village_name}")
    try:
        _admin_text(db, text)
    except Exception:
        log.exception("admin notification failed")
        db.rollback()


def notify_admin_cancelled(db: Session, order: Order) -> None:
    try:
        _admin_text(db, f"Order {order.order_code} was cancelled by the customer.")
    except Exception:
        log.exception("admin notification failed")
        db.rollback()


def notify_admin_customer_cancelled(db: Session, order: Order, prev_status: str, refund_amount=None) -> None:
    refund = f" Refund of ₹{money(refund_amount)} requested - check the Refunds view." if refund_amount else ""
    notify_admin_text(db, f"Customer cancelled order {order.order_code} (it was {prev_status}).{refund}")


def notify_admin_cancel_request(db: Session, order: Order) -> None:
    notify_admin_text(db, f"⚠️ {order.customer_name} ({order.contact_phone}) asks to CANCEL order "
                          f"{order.order_code}, which is already DISPATCHED. Please call them and decide in the dashboard.")


def notify_refund_started(db: Session, order_id: int) -> None:
    order = db.get(Order, order_id)
    _send(db, order, t("pay_refund_started", order.language, order_id=order.order_code, amount=money(order.total)))


def notify_admin_text(db: Session, text: str) -> None:
    try:
        _admin_text(db, text)
    except Exception:
        log.exception("admin notification failed")
        db.rollback()


def confirmation_text(order: Order) -> str:
    """The order-confirmation message (same wording for COD and for a verified online payment)."""
    lang = order.language
    item = order.items[0]
    if order.payment_method == "COD":
        payment_line = t("pay_line_cod", lang)
    else:
        ref = order.payment.cf_payment_id if order.payment and order.payment.cf_payment_id else "-"
        payment_line = t("pay_line_online", lang, ref=ref)
    return t("order_confirmed", lang, name=order.customer_name, order_id=order.order_code,
             product=item.name(lang), qty=qty_str(item.quantity), total=money(order.total),
             date=fmt_date(order.delivery_date), address=f"{order.address}, {order.village_name}",
             payment=payment_line)


def _send(db: Session, order: Order, text: str) -> None:
    customer = order.customer
    if customer.language is None:
        customer.language = order.language
    try:
        messaging.send_notification(db, customer, text, [order.customer_name, order.order_code, text])
    except Exception:
        log.exception("notification failed for %s", order.order_code)
        db.rollback()


def _reset_chat(db: Session, order: Order, from_state: str, to_state: str, **data) -> None:
    conv = db.query(Conversation).filter_by(customer_id=order.customer_id).first()
    if conv is not None and conv.state == from_state:
        conv.state = to_state
        conv.data = {**(conv.data or {}), "last_options": [], "history": [], **data}
        db.commit()


def notify_order_confirmed(db: Session, order_id: int) -> None:
    """Verified online payment received: tell the customer, move the chat on, alert the family."""
    order = db.get(Order, order_id)
    _send(db, order, confirmation_text(order))
    _reset_chat(db, order, "PAYMENT_PENDING", "ORDER_CONFIRMED", last_order=order.order_code)
    notify_admin_new_order(db, order)


def notify_payment_failed_attempt(db: Session, order_id: int) -> None:
    from app.services.payments import pay_url
    order = db.get(Order, order_id)
    p = order.payment
    _send(db, order, t("pay_failed_attempt", order.language, order_id=order.order_code, url=pay_url(p),
                       expiry=fmt_datetime(p.expires_at)))


def notify_payment_closed(db: Session, order_id: int, reason: str) -> None:
    order = db.get(Order, order_id)
    key = {"EXPIRED": "pay_expired", "CANCELLED": "pay_cancelled", "FAILED": "pay_failed_final"}[reason]
    _send(db, order, t(key, order.language, order_id=order.order_code))
    _reset_chat(db, order, "PAYMENT_PENDING", "MAIN_MENU")


def notify_refunded(db: Session, order_id: int) -> None:
    order = db.get(Order, order_id)
    _send(db, order, t("pay_refunded", order.language, order_id=order.order_code, amount=money(order.total)))
