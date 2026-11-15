"""Deterministic, state-machine conversation engine.

The simulator and the real WhatsApp webhook both call `handle_incoming`, so there is exactly one
implementation of the ordering journey. No LLM is involved: prices, stock, dates and totals all
come from the database through the services layer.
"""
import copy
import logging
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.i18n import CMD_BACK, CMD_CANCEL, CMD_LANG, CMD_MENU, CMD_RESTART, GREETINGS, WEEKDAYS, WELCOME, t
from app.models import Conversation, Customer, DeliveryArea, DeliveryDate, MessageLog, Order, Product
from app.services import delivery, notifications, payments
from app.services.catalog import active_products, orderable_products, validate_quantity
from app.services.core import get_all_settings, get_setting
from app.services.gateways import GatewayError
from app.services.messaging import InMessage, Option, OutMessage, send_messages
from app.services.orders import OrderError, create_order, customer_cancel
from app.models import AWAITING_PAYMENT
from app.utils import (TELUGU_DIGITS, display_phone, fmt_date, fmt_datetime, money, normalize_indian_mobile, parse_decimal,
                       q2, qty_str, utcnow)

log = logging.getLogger("mango.conversation")

LANGUAGE_SELECTION, MAIN_MENU, PRODUCT_SELECTION = "LANGUAGE_SELECTION", "MAIN_MENU", "PRODUCT_SELECTION"
QUANTITY_SELECTION, QUANTITY_CUSTOM = "QUANTITY_SELECTION", "QUANTITY_CUSTOM"
DELIVERY_DATE_SELECTION, DELIVERY_SLOT_SELECTION = "DELIVERY_DATE_SELECTION", "DELIVERY_SLOT_SELECTION"
CUSTOMER_NAME, PHONE_CONFIRMATION, PHONE_ENTRY = "CUSTOMER_NAME", "PHONE_CONFIRMATION", "PHONE_ENTRY"
ALTERNATIVE_PHONE, VILLAGE_SELECTION = "ALTERNATIVE_PHONE", "VILLAGE_SELECTION"
DELIVERY_ADDRESS, LANDMARK = "DELIVERY_ADDRESS", "LANDMARK"
ORDER_REVIEW, EDIT_SELECTION = "ORDER_REVIEW", "EDIT_SELECTION"
ORDER_CONFIRMED, HUMAN_SUPPORT = "ORDER_CONFIRMED", "HUMAN_SUPPORT"
PAYMENT_METHOD, PAYMENT_PENDING = "PAYMENT_METHOD", "PAYMENT_PENDING"

ORDERING_STATES = {PRODUCT_SELECTION, QUANTITY_SELECTION, QUANTITY_CUSTOM, DELIVERY_DATE_SELECTION,
                   DELIVERY_SLOT_SELECTION, CUSTOMER_NAME, PHONE_CONFIRMATION, PHONE_ENTRY,
                   ALTERNATIVE_PHONE, VILLAGE_SELECTION, DELIVERY_ADDRESS, LANDMARK, PAYMENT_METHOD,
                   ORDER_REVIEW, EDIT_SELECTION}
OPTION_STATES = {LANGUAGE_SELECTION, MAIN_MENU, PRODUCT_SELECTION, QUANTITY_SELECTION,
                 DELIVERY_DATE_SELECTION, DELIVERY_SLOT_SELECTION, PHONE_CONFIRMATION,
                 VILLAGE_SELECTION, PAYMENT_METHOD, PAYMENT_PENDING, ORDER_REVIEW, EDIT_SELECTION}
NO_HISTORY = {LANGUAGE_SELECTION, ORDER_CONFIRMED, HUMAN_SUPPORT, PAYMENT_PENDING}
DRAFT_KEYS = ["product_id", "qty", "date", "slot_id", "name", "phone", "alt_phone", "area_id",
              "address", "landmark", "draft_id", "editing", "payment_method"]
STALE = object()


class Ctx:
    def __init__(self, db: Session, customer: Customer, conv: Conversation, data: dict, prev_inbound):
        self.db, self.customer, self.conv, self.data, self.prev_inbound = db, customer, conv, data, prev_inbound

    @property
    def lang(self) -> str:
        return self.customer.language or "en"

    @property
    def state(self) -> str:
        return self.conv.state


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def media_url(customer: Customer, path: str | None) -> str | None:
    if not path:
        return None
    base = get_settings().public_base_url.rstrip("/")
    if customer.channel == "whatsapp":
        return f"{base}/media/{path}" if base else None
    return f"{base}/media/{path}"


# ======================================================================= entry point
def handle_incoming(db: Session, inm: InMessage, channel: str) -> list[OutMessage]:
    """Process one inbound message and deliver the replies. Returns the replies."""
    customer = db.scalar(select(Customer).where(Customer.whatsapp_number == inm.phone))
    if customer is None:
        customer = Customer(whatsapp_number=inm.phone, channel=channel, name=None)
        db.add(customer)
        db.flush()
    customer.channel = channel
    conv = db.scalar(select(Conversation).where(Conversation.customer_id == customer.id))
    if conv is None:
        conv = Conversation(customer_id=customer.id, state=LANGUAGE_SELECTION, data={})
        db.add(conv)
        db.flush()
    prev_inbound = conv.last_inbound_at
    conv.last_inbound_at = utcnow()
    db.add(MessageLog(customer_id=customer.id, phone=inm.phone, direction="IN", channel=channel,
                      message_type=inm.kind, wa_message_id=inm.id, status="received",
                      content={"text": inm.text, "reply_id": inm.reply_id}))
    db.commit()

    ctx = Ctx(db, customer, conv, copy.deepcopy(conv.data or {}), prev_inbound)
    try:
        out = Engine().process(ctx, inm)
        _finish(ctx, out)
    except Exception:
        db.rollback()
        log.exception("conversation engine failure for %s", inm.phone)
        out = [OutMessage("text", t("err_generic", customer.language))]
    send_messages(db, customer, out)
    return out


def _finish(ctx: Ctx, out: list[OutMessage]) -> None:
    last = next((m for m in reversed(out) if m.options), None)
    ctx.data["last_options"] = [[o.id, o.title] for o in last.options] if last else []
    ctx.conv.data = ctx.data
    ctx.db.commit()


# ======================================================================= engine
class Engine:
    # ---- helpers -------------------------------------------------------------
    def txt(self, ctx, key, **kw) -> OutMessage:
        return OutMessage("text", t(key, ctx.lang, **kw))

    def free_text(self, ctx, body: str) -> OutMessage:
        return OutMessage("text", f"{body}\n\n{t('hint', ctx.lang)}")

    def clear_draft(self, ctx):
        for k in DRAFT_KEYS:
            ctx.data.pop(k, None)
        ctx.data["history"] = []

    def goto(self, ctx, state: str, push: bool = True, prefix: list[OutMessage] | None = None):
        if push and ctx.conv.state != state and ctx.conv.state not in NO_HISTORY:
            hist = ctx.data.setdefault("history", [])
            hist.append(ctx.conv.state)
            del hist[:-15]
        ctx.conv.state = state
        return (prefix or []) + self.prompt(ctx, state)

    def after_field(self, ctx, next_state: str):
        """Move on; when editing from the summary, return to the summary instead."""
        if ctx.data.pop("editing", None):
            return self.goto(ctx, ORDER_REVIEW)
        return self.goto(ctx, next_state)

    # ---- main dispatcher -----------------------------------------------------
    def process(self, ctx: Ctx, inm: InMessage) -> list[OutMessage]:
        text = (inm.text or "").strip()
        low = _norm(text)
        state = ctx.state

        if inm.kind == "unsupported":
            return [self.txt(ctx, "unsupported")] + (self.prompt(ctx, state) if ctx.customer.language else [])

        m = re.fullmatch(r"(?:cancel|రద్దు)\s+(mf-\d{8}-\d{4,})", low)
        if m:
            return self.cancel_placed_order(ctx, m.group(1).upper())

        if ctx.customer.language is None and state != LANGUAGE_SELECTION:
            ctx.conv.state = LANGUAGE_SELECTION
            state = LANGUAGE_SELECTION
        if (state in ORDERING_STATES and ctx.prev_inbound is not None and
                utcnow() - ctx.prev_inbound > timedelta(minutes=get_settings().conversation_timeout_minutes)
                and not inm.reply_id):
            self.clear_draft(ctx)
            return [self.txt(ctx, "session_expired")] + self.goto(ctx, MAIN_MENU, push=False)

        if not inm.reply_id:
            if low in GREETINGS:
                if ctx.customer.language is None:
                    return self.welcome(ctx)
                self.clear_draft(ctx)
                return self.goto(ctx, MAIN_MENU, push=False)
            if low in CMD_LANG:
                return self.start_language_change(ctx)
            if low in CMD_MENU:
                self.clear_draft(ctx)
                return self.goto(ctx, MAIN_MENU, push=False)
            if low in CMD_RESTART:
                self.clear_draft(ctx)
                return self.start_order(ctx)
            if low in CMD_CANCEL:
                if state == PAYMENT_PENDING:
                    return self.cancel_payment(ctx)
                if state in ORDERING_STATES:
                    return self.cancel_draft(ctx)
                self.clear_draft(ctx)
                return self.goto(ctx, MAIN_MENU, push=False)
            if low in CMD_BACK:
                return self.go_back(ctx)

        if state == ORDER_CONFIRMED and inm.reply_id == "confirm_order":
            return [self.txt(ctx, "already_placed", order_id=ctx.data.get("last_order", ""))]
        if state == HUMAN_SUPPORT and not inm.reply_id:
            return []  # human takes over; message is already logged for the admin
        choice = self.resolve_choice(ctx, inm, low)
        if choice is STALE and state != ORDER_CONFIRMED:
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, state)
        handler = getattr(self, "on_" + state.lower(), None)
        if handler is None:
            return self.goto(ctx, MAIN_MENU, push=False)
        return handler(ctx, None if choice is STALE else choice, text)

    def resolve_choice(self, ctx: Ctx, inm: InMessage, low: str):
        last = ctx.data.get("last_options") or []
        ids = [o[0] for o in last]
        if inm.reply_id:
            return inm.reply_id if inm.reply_id in ids else STALE
        digits = low.translate(TELUGU_DIGITS)
        if ctx.state in OPTION_STATES and digits.isdigit() and 1 <= int(digits) <= len(ids):
            return ids[int(digits) - 1]
        for oid, title in last:
            if _norm(title) == low:
                return oid
        if ctx.state == LANGUAGE_SELECTION:
            if low in ("telugu", "తెలుగు"):
                return "lang_te"
            if low in ("english", "ఇంగ్లీష్"):
                return "lang_en"
        return None

    # ---- global commands -----------------------------------------------------
    def welcome(self, ctx):
        ctx.conv.state = LANGUAGE_SELECTION
        return [OutMessage("buttons", WELCOME, [Option("lang_te", "తెలుగు"), Option("lang_en", "English")])]

    def start_language_change(self, ctx):
        if ctx.state in ORDERING_STATES:
            ctx.data["lang_return"] = ctx.state
        ctx.conv.state = LANGUAGE_SELECTION
        return self.welcome(ctx)

    def go_back(self, ctx):
        hist = ctx.data.get("history") or []
        while hist:
            prev = hist.pop()
            if prev != ctx.state:
                ctx.data.pop("editing", None)
                return self.goto(ctx, prev, push=False)
        return self.goto(ctx, MAIN_MENU, push=False)

    def cancel_draft(self, ctx):
        self.clear_draft(ctx)
        return [self.txt(ctx, "draft_cancelled")] + self.goto(ctx, MAIN_MENU, push=False)

    def cancel_placed_order(self, ctx, code: str):
        order = ctx.db.scalar(select(Order).where(Order.order_code == code,
                                                  Order.customer_id == ctx.customer.id))
        if order is None:
            return [self.txt(ctx, "cancel_order_not_found")]
        if order.status == AWAITING_PAYMENT:
            return self.cancel_payment(ctx, order)
        try:
            customer_cancel(ctx.db, order)
        except OrderError:
            return [self.txt(ctx, "cancel_order_too_late", order_id=code)]
        notifications.notify_admin_cancelled(ctx.db, order)
        return [self.txt(ctx, "cancel_order_ok", order_id=code)]

    def start_order(self, ctx):
        self.clear_draft(ctx)
        ctx.data["draft_id"] = uuid.uuid4().hex
        ctx.conv.state = MAIN_MENU
        return self.goto(ctx, PRODUCT_SELECTION)

    # ---- prompts ---------------------------------------------------------------
    def prompt(self, ctx: Ctx, state: str) -> list[OutMessage]:
        fn = getattr(self, "p_" + state.lower(), None)
        return fn(ctx) if fn else self.p_main_menu(ctx)

    def p_language_selection(self, ctx):
        return self.welcome(ctx)

    def p_main_menu(self, ctx):
        ctx.conv.state = MAIN_MENU
        opts = [Option(k, t(k, ctx.lang)) for k in
                ("menu_varieties", "menu_order", "menu_prices", "menu_contact", "menu_language")]
        return [OutMessage("list", t("menu_title", ctx.lang), opts, button_label=t("choose", ctx.lang))]

    def p_product_selection(self, ctx):
        products = orderable_products(ctx.db)
        if not products:
            ctx.conv.state = MAIN_MENU
            return [self.txt(ctx, "no_products")] + self.p_main_menu(ctx)
        opts = [Option(f"prod_{p.id}", p.name(ctx.lang), f"₹{money(p.price_per_kg)} / kg") for p in products]
        return [OutMessage("list", t("choose_product", ctx.lang), opts, button_label=t("choose", ctx.lang),
                           footer=t("hint", ctx.lang)[:60])]

    def _product(self, ctx):
        return ctx.db.get(Product, ctx.data["product_id"]) if ctx.data.get("product_id") else None

    def p_quantity_selection(self, ctx):
        p = self._product(ctx)
        opts = []
        for q, key in ((1, "qty_1"), (2, "qty_2"), (5, "qty_5")):
            if validate_quantity(p, Decimal(q)) is None:
                opts.append(Option(f"qty_{q}", t(key, ctx.lang)))
        opts.append(Option("qty_custom", t("qty_custom", ctx.lang)))
        return [OutMessage("buttons" if len(opts) <= 3 else "list",
                           t("ask_quantity", ctx.lang, name=p.name(ctx.lang), price=money(p.price_per_kg)),
                           opts, button_label=t("choose", ctx.lang), footer=t("hint", ctx.lang)[:60])]

    def p_quantity_custom(self, ctx):
        p = self._product(ctx)
        return [self.free_text(ctx, t("ask_custom_qty", ctx.lang, min=qty_str(p.min_qty)))]

    def p_delivery_date_selection(self, ctx):
        dates = delivery.available_dates(ctx.db, limit=9)
        if not dates:
            self.clear_draft(ctx)
            ctx.conv.state = MAIN_MENU
            return [self.txt(ctx, "no_dates")] + self.p_main_menu(ctx)
        opts = [Option(f"date_{o.row.date.isoformat()}", self.date_label(ctx, o.row.date)) for o in dates]
        return [OutMessage("list", t("ask_date", ctx.lang), opts, button_label=t("choose", ctx.lang),
                           footer=t("hint", ctx.lang)[:60])]

    def date_label(self, ctx, d: date) -> str:
        return f"{fmt_date(d)} ({WEEKDAYS[ctx.lang][d.weekday()]})"

    def p_delivery_slot_selection(self, ctx):
        dd = ctx.db.scalar(select(DeliveryDate).where(DeliveryDate.date == date.fromisoformat(ctx.data["date"])))
        slots = delivery.slots_for_date(ctx.db, dd) if dd else []
        opts = [Option(f"slot_{s.id}", s.name(ctx.lang)) for s in slots]
        return [OutMessage("buttons" if len(opts) <= 3 else "list", t("ask_slot", ctx.lang), opts,
                           button_label=t("choose", ctx.lang))]

    def p_customer_name(self, ctx):
        return [self.free_text(ctx, t("ask_name", ctx.lang))]

    def p_phone_confirmation(self, ctx):
        opts = [Option("phone_yes", t("yes_use", ctx.lang)), Option("phone_other", t("other_number", ctx.lang))]
        return [OutMessage("buttons", t("confirm_phone", ctx.lang, phone=display_phone(ctx.customer.whatsapp_number)),
                           opts)]

    def p_phone_entry(self, ctx):
        return [self.free_text(ctx, t("ask_phone", ctx.lang))]

    def p_alternative_phone(self, ctx):
        return [OutMessage("buttons", t("ask_alt", ctx.lang), [Option("skip", t("skip", ctx.lang))])]

    def p_village_selection(self, ctx):
        areas = delivery.active_areas(ctx.db)
        opts = [Option(f"area_{a.id}", a.name(ctx.lang),
                       t("delivery_charge_desc", ctx.lang, c=money(a.delivery_charge))
                       if a.delivery_charge else t("free_delivery", ctx.lang)) for a in areas[:10]]
        return [OutMessage("list", t("ask_village", ctx.lang), opts, button_label=t("choose", ctx.lang))]

    def p_delivery_address(self, ctx):
        return [self.free_text(ctx, t("ask_address", ctx.lang))]

    def p_landmark(self, ctx):
        return [OutMessage("buttons", t("ask_landmark", ctx.lang), [Option("skip", t("skip", ctx.lang))])]

    def cod_available(self, ctx) -> bool:
        return get_setting(ctx.db, "cod_enabled").lower() == "true" and bool(ctx.customer.cod_allowed)

    def p_payment_method(self, ctx):
        opts = [Option("pay_online", t("btn_pay_online", ctx.lang)), Option("pay_cod", t("btn_pay_cod", ctx.lang))]
        return [OutMessage("buttons", t("pay_method_q", ctx.lang), opts)]

    def p_payment_pending(self, ctx):
        order = ctx.db.scalar(select(Order).where(Order.order_code == ctx.data.get("last_order", "")))
        if order is None or order.payment is None:
            return self.goto(ctx, MAIN_MENU, push=False)
        p = order.payment
        opts = [Option("cancel_payment", t("btn_cancel_payment", ctx.lang)), Option("to_menu", t("btn_menu", ctx.lang))]
        return [OutMessage("buttons", t("pay_pending_status", ctx.lang, order_id=order.order_code,
                                        url=payments.pay_url(p), expiry=fmt_datetime(p.expires_at)), opts)]

    def p_order_review(self, ctx):
        d = ctx.data
        p, area = self._product(ctx), ctx.db.get(DeliveryArea, d["area_id"])
        qty = Decimal(str(d["qty"]))
        sub = q2(qty * Decimal(p.price_per_kg))
        charge = q2(area.delivery_charge)
        slot = t("any_time", ctx.lang)
        if d.get("slot_id"):
            from app.models import DeliverySlot
            slot = ctx.db.get(DeliverySlot, d["slot_id"]).name(ctx.lang)
        address = d["address"] + (f", {d['landmark']}" if d.get("landmark") else "") + f", {area.name(ctx.lang)}"
        body = t("review_body", ctx.lang, name=d["name"], phone=display_phone(d["phone"]),
                 product=p.name(ctx.lang), qty=qty_str(qty), price=money(p.price_per_kg), subtotal=money(sub),
                 delivery=money(charge), total=money(sub + charge), address=address,
                 date=fmt_date(date.fromisoformat(d["date"])), slot=slot)
        method = d.get("payment_method", "ONLINE")
        body = body.replace("💵 చెల్లింపు: Cash on Delivery", "💵 చెల్లింపు: " + t("review_pay_" + method.lower(), "te")) \
                   .replace("💵 Payment: Cash on Delivery", "💵 Payment: " + t("review_pay_" + method.lower(), "en"))
        confirm_label = t("btn_confirm_pay" if method == "ONLINE" else "btn_confirm", ctx.lang)
        opts = [Option("confirm_order", confirm_label), Option("edit_order", t("btn_edit", ctx.lang)),
                Option("cancel_order", t("btn_cancel", ctx.lang))]
        return [OutMessage("text", f"{t('review_title', ctx.lang)}\n\n{body}"),
                OutMessage("buttons", t("confirm_q", ctx.lang), opts)]

    def p_edit_selection(self, ctx):
        keys = ["edit_product", "edit_qty", "edit_date", "edit_name", "edit_phone", "edit_village",
                "edit_address", "edit_landmark", "edit_back"]
        return [OutMessage("list", t("edit_q", ctx.lang), [Option(k, t(k, ctx.lang)) for k in keys],
                           button_label=t("choose", ctx.lang))]

    def p_order_confirmed(self, ctx):
        return self.p_main_menu(ctx)

    def p_human_support(self, ctx):
        return [self.txt(ctx, "human_support")]

    # ---- state handlers ------------------------------------------------------
    def on_language_selection(self, ctx, choice, text):
        if choice not in ("lang_te", "lang_en"):
            return self.welcome(ctx)
        ctx.customer.language = choice[-2:]
        back = ctx.data.pop("lang_return", None)
        if back and back in ORDERING_STATES:
            return [self.txt(ctx, "lang_changed")] + self.goto(ctx, back, push=False)
        self.clear_draft(ctx)
        return self.goto(ctx, MAIN_MENU, push=False)

    def on_main_menu(self, ctx, choice, text):
        if choice == "menu_order":
            return self.start_order(ctx)
        if choice == "menu_varieties":
            return self.show_varieties(ctx)
        if choice == "menu_prices":
            return self.show_prices(ctx)
        if choice == "menu_contact":
            return self.show_contact(ctx)
        if choice == "menu_language":
            return self.start_language_change(ctx)
        return [self.txt(ctx, "unexpected")] + self.p_main_menu(ctx)

    def show_varieties(self, ctx):
        products = active_products(ctx.db)
        if not products:
            return [self.txt(ctx, "no_products")] + self.p_main_menu(ctx)
        out = []
        for p in products:
            av = p.available_qty
            stock = t("stock_kg", ctx.lang, qty=qty_str(av)) if av > 0 else t("out_of_stock", ctx.lang)
            body = t("product_card", ctx.lang, name=p.name(ctx.lang), desc=p.description(ctx.lang),
                     price=money(p.price_per_kg), stock=stock)
            url = media_url(ctx.customer, p.image_path)
            out.append(OutMessage("image", body, image_url=url) if url else OutMessage("text", body))
        return out + self.p_main_menu(ctx)

    def show_prices(self, ctx):
        lines = [t("prices_header", ctx.lang)]
        for p in active_products(ctx.db):
            av = p.available_qty
            stock = t("stock_short_in", ctx.lang, qty=qty_str(av)) if av > 0 else t("stock_short_out", ctx.lang)
            lines.append(t("price_line", ctx.lang, name=p.name(ctx.lang), price=money(p.price_per_kg), stock=stock))
        if len(lines) == 1:
            lines.append(t("no_products", ctx.lang))
        return [OutMessage("text", "\n".join(lines))] + self.p_main_menu(ctx)

    def show_contact(self, ctx):
        s = get_all_settings(ctx.db)
        ctx.conv.state = HUMAN_SUPPORT
        return [self.txt(ctx, "contact", farm=s["farm_name"], phone=s["contact_number"] or "-",
                         address=s["business_address"], hours=s["business_hours"])]

    def on_human_support(self, ctx, choice, text):
        return self.goto(ctx, MAIN_MENU, push=False)

    def on_product_selection(self, ctx, choice, text):
        if not (choice or "").startswith("prod_"):
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, PRODUCT_SELECTION)
        p = ctx.db.get(Product, int(choice[5:]))
        if p is None or not p.is_active:
            return [self.txt(ctx, "err_product")] + self.prompt(ctx, PRODUCT_SELECTION)
        ctx.data["product_id"] = p.id
        ctx.data.pop("qty", None)
        # product change always needs a fresh quantity; keep `editing` so we return to the summary after.
        return self.goto(ctx, QUANTITY_SELECTION)

    def accept_quantity(self, ctx, qty: Decimal):
        p = self._product(ctx)
        err = validate_quantity(p, qty)
        if err:
            code, c = err
            key = {"invalid": "qty_invalid", "no_decimal": "qty_no_decimal", "too_low": "qty_too_low",
                   "too_high": "qty_too_high", "no_stock": "qty_no_stock"}[code]
            kw = {k: qty_str(v) for k, v in c.items()}
            return [self.txt(ctx, key, **kw)] + self.prompt(ctx, ctx.state)
        ctx.data["qty"] = str(qty)
        sub = q2(qty * Decimal(p.price_per_kg))
        note = self.txt(ctx, "qty_ok", qty=qty_str(qty), price=money(p.price_per_kg), sub=money(sub))
        return [note] + self.after_field(ctx, DELIVERY_DATE_SELECTION)

    def on_quantity_selection(self, ctx, choice, text):
        if choice == "qty_custom":
            return self.goto(ctx, QUANTITY_CUSTOM)
        if choice in ("qty_1", "qty_2", "qty_5"):
            return self.accept_quantity(ctx, Decimal(choice[4:]))
        return [self.txt(ctx, "unexpected")] + self.prompt(ctx, QUANTITY_SELECTION)

    def on_quantity_custom(self, ctx, choice, text):
        qty = parse_decimal(text)
        if qty is None:
            return [self.txt(ctx, "qty_invalid")] + self.prompt(ctx, QUANTITY_CUSTOM)
        return self.accept_quantity(ctx, qty)

    def on_delivery_date_selection(self, ctx, choice, text):
        if not (choice or "").startswith("date_"):
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, DELIVERY_DATE_SELECTION)
        d = date.fromisoformat(choice[5:])
        dd = delivery.get_bookable_date(ctx.db, d)
        if dd is None:
            return [self.txt(ctx, "err_date")] + self.prompt(ctx, DELIVERY_DATE_SELECTION)
        ctx.data["date"] = d.isoformat()
        ctx.data.pop("slot_id", None)
        if delivery.slots_for_date(ctx.db, dd):
            return self.goto(ctx, DELIVERY_SLOT_SELECTION)
        return self.after_field(ctx, CUSTOMER_NAME)

    def on_delivery_slot_selection(self, ctx, choice, text):
        if not (choice or "").startswith("slot_"):
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, DELIVERY_SLOT_SELECTION)
        dd = ctx.db.scalar(select(DeliveryDate).where(DeliveryDate.date == date.fromisoformat(ctx.data["date"])))
        slot = next((s for s in delivery.slots_for_date(ctx.db, dd) if f"slot_{s.id}" == choice), None)
        if slot is None:
            return [self.txt(ctx, "err_slot")] + self.prompt(ctx, DELIVERY_SLOT_SELECTION)
        ctx.data["slot_id"] = slot.id
        return self.after_field(ctx, CUSTOMER_NAME)

    def on_customer_name(self, ctx, choice, text):
        name = " ".join(text.split())
        if len(name) < 2 or len(name) > 80 or name.isdigit():
            return [self.txt(ctx, "name_invalid")]
        ctx.data["name"] = name
        return self.after_field(ctx, PHONE_CONFIRMATION)

    def on_phone_confirmation(self, ctx, choice, text):
        if choice == "phone_yes":
            ctx.data["phone"] = ctx.customer.whatsapp_number
            return self.after_field(ctx, ALTERNATIVE_PHONE)
        if choice == "phone_other":
            return self.goto(ctx, PHONE_ENTRY)
        return [self.txt(ctx, "unexpected")] + self.prompt(ctx, PHONE_CONFIRMATION)

    def on_phone_entry(self, ctx, choice, text):
        phone = normalize_indian_mobile(text)
        if not phone:
            return [self.txt(ctx, "phone_invalid")]
        ctx.data["phone"] = phone
        return self.after_field(ctx, ALTERNATIVE_PHONE)

    def on_alternative_phone(self, ctx, choice, text):
        if choice == "skip":
            ctx.data["alt_phone"] = None
        else:
            phone = normalize_indian_mobile(text)
            if not phone:
                return [self.txt(ctx, "phone_invalid")] + self.prompt(ctx, ALTERNATIVE_PHONE)
            ctx.data["alt_phone"] = phone
        return self.goto(ctx, VILLAGE_SELECTION)

    def on_village_selection(self, ctx, choice, text):
        if not (choice or "").startswith("area_"):
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, VILLAGE_SELECTION)
        area = ctx.db.get(DeliveryArea, int(choice[5:]))
        if area is None or not area.is_active:
            return [self.txt(ctx, "err_area")] + self.prompt(ctx, VILLAGE_SELECTION)
        ctx.data["area_id"] = area.id
        return self.after_field(ctx, DELIVERY_ADDRESS)

    def on_delivery_address(self, ctx, choice, text):
        addr = " ".join(text.split())
        if len(addr) < 8:
            return [self.txt(ctx, "address_short")]
        ctx.data["address"] = addr[:300]
        return self.after_field(ctx, LANDMARK)

    def on_landmark(self, ctx, choice, text):
        ctx.data["landmark"] = None if choice == "skip" else " ".join(text.split())[:150] or None
        was_editing = ctx.data.pop("editing", None)
        if not was_editing and self.cod_available(ctx):
            return self.goto(ctx, PAYMENT_METHOD)
        ctx.data.setdefault("payment_method", "ONLINE")
        return self.goto(ctx, ORDER_REVIEW)

    def on_payment_method(self, ctx, choice, text):
        if choice not in ("pay_online", "pay_cod"):
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, PAYMENT_METHOD)
        ctx.data["payment_method"] = "COD" if choice == "pay_cod" else "ONLINE"
        return self.goto(ctx, ORDER_REVIEW)

    def on_payment_pending(self, ctx, choice, text):
        if choice == "cancel_payment":
            return self.cancel_payment(ctx)
        if choice == "to_menu":
            return self.goto(ctx, MAIN_MENU, push=False)
        # Anything else ("I have paid", a screenshot caption ...) is never believed: ask the gateway.
        order = ctx.db.scalar(select(Order).where(Order.order_code == ctx.data.get("last_order", "")))
        if order is not None and order.payment is not None:
            payments.sync_payment(ctx.db, order.payment.id)
            ctx.db.refresh(ctx.conv)
            if ctx.conv.state != PAYMENT_PENDING:  # verified paid / closed: the notification was already sent
                ctx.data = copy.deepcopy(ctx.conv.data or {})
                return []
        return self.prompt(ctx, PAYMENT_PENDING)

    def cancel_payment(self, ctx, order=None):
        order = order or ctx.db.scalar(select(Order).where(Order.order_code == ctx.data.get("last_order", "")))
        if order is None:
            return self.goto(ctx, MAIN_MENU, push=False)
        result = payments.cancel_checkout(ctx.db, order.id)
        ctx.db.refresh(ctx.conv)
        if result == "paid":  # the payment had actually gone through; the verifier sent the confirmation
            ctx.data = copy.deepcopy(ctx.conv.data or {})
            return []
        self.clear_draft(ctx)
        return [OutMessage("text", t("pay_cancelled", ctx.lang, order_id=order.order_code))] + \
            self.goto(ctx, MAIN_MENU, push=False)

    def on_order_review(self, ctx, choice, text):
        if choice == "edit_order":
            return self.goto(ctx, EDIT_SELECTION)
        if choice == "cancel_order":
            return self.cancel_draft(ctx)
        if choice == "confirm_order":
            return self.confirm(ctx)
        return [self.txt(ctx, "unexpected")] + self.prompt(ctx, ORDER_REVIEW)

    EDIT_TARGETS = {"edit_product": PRODUCT_SELECTION, "edit_qty": QUANTITY_SELECTION,
                    "edit_date": DELIVERY_DATE_SELECTION, "edit_name": CUSTOMER_NAME,
                    "edit_phone": PHONE_ENTRY, "edit_village": VILLAGE_SELECTION,
                    "edit_address": DELIVERY_ADDRESS, "edit_landmark": LANDMARK}

    def on_edit_selection(self, ctx, choice, text):
        if choice == "edit_back":
            return self.goto(ctx, ORDER_REVIEW)
        target = self.EDIT_TARGETS.get(choice)
        if not target:
            return [self.txt(ctx, "unexpected")] + self.prompt(ctx, EDIT_SELECTION)
        ctx.data["editing"] = True
        return self.goto(ctx, target)

    def on_order_confirmed(self, ctx, choice, text):
        return self.goto(ctx, MAIN_MENU, push=False)

    # ---- confirmation ----------------------------------------------------------
    def confirm(self, ctx):
        d = ctx.data
        method = d.get("payment_method", "ONLINE")
        if method == "COD" and not self.cod_available(ctx):
            d["payment_method"] = method = "ONLINE"  # COD was switched off meanwhile
        draft = {k: d.get(k) for k in ("product_id", "qty", "date", "slot_id", "name", "phone", "alt_phone",
                                       "area_id", "address", "landmark")}
        draft["idempotency_key"] = f"{ctx.customer.id}:{d.get('draft_id') or uuid.uuid4().hex}"
        try:
            order, created = create_order(ctx.db, ctx.customer, draft, language=ctx.lang,
                                          channel=ctx.customer.channel, payment_method=method)
        except OrderError as e:
            return self.confirm_failed(ctx, e)
        if method == "COD":
            if created:
                notifications.notify_admin_new_order(ctx.db, order)
            msg = OutMessage("text", notifications.confirmation_text(order))
            self.clear_draft(ctx)
            ctx.data["last_order"] = order.order_code
            ctx.conv.state = ORDER_CONFIRMED
            return [msg]
        return self.begin_payment(ctx, order, created)

    def begin_payment(self, ctx, order, created: bool):
        """Online order: stock is held; create the gateway checkout and send the secure payment link."""
        if created or order.payment is None:
            try:
                payments.start_checkout(ctx.db, order)
            except (payments.PaymentError, GatewayError):
                payments.abort_unpaid_order(ctx.db, order, "Could not start online payment")
                self.clear_draft(ctx)
                return [self.txt(ctx, "pay_unavailable")] + self.goto(ctx, MAIN_MENU, push=False)
        ctx.db.refresh(order)
        p = order.payment
        self.clear_draft(ctx)
        ctx.data["last_order"] = order.order_code
        ctx.conv.state = PAYMENT_PENDING
        opts = [Option("cancel_payment", t("btn_cancel_payment", ctx.lang)), Option("to_menu", t("btn_menu", ctx.lang))]
        return [OutMessage("text", t("pay_link", ctx.lang, order_id=order.order_code, total=money(order.total),
                                     url=payments.pay_url(p), expiry=fmt_datetime(p.expires_at))),
                OutMessage("buttons", t("btn_cancel_payment", ctx.lang) + "?", opts)]

    def confirm_failed(self, ctx, e: OrderError):
        ctx.data["editing"] = True
        if e.code == "not_accepting":
            self.clear_draft(ctx)
            return [self.txt(ctx, "not_accepting")] + self.goto(ctx, MAIN_MENU, push=False)
        if e.code == "insufficient_stock":
            return [self.txt(ctx, "qty_no_stock", avail=qty_str(e.ctx["avail"]))] + self.goto(ctx, QUANTITY_SELECTION)
        if e.code == "invalid_quantity":
            return self.goto(ctx, QUANTITY_SELECTION)
        if e.code == "product_unavailable":
            return [self.txt(ctx, "err_product")] + self.goto(ctx, PRODUCT_SELECTION)
        if e.code == "date_unavailable":
            return [self.txt(ctx, "err_date")] + self.goto(ctx, DELIVERY_DATE_SELECTION)
        if e.code == "slot_unavailable":
            return [self.txt(ctx, "err_slot")] + self.goto(ctx, DELIVERY_DATE_SELECTION)
        if e.code == "area_unavailable":
            return [self.txt(ctx, "err_area")] + self.goto(ctx, VILLAGE_SELECTION)
        return [self.txt(ctx, "err_generic")] + self.goto(ctx, ORDER_REVIEW)

