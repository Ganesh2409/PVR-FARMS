import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models import (CANCELLED, DeliveryDate, DeliverySlot, Inventory, MessageLog, Order, Product)
from app.utils import today_ist
from tests.conftest import Chat, drive_to_review, first_date_id


def orders(db):
    db.expire_all()
    return db.scalars(select(Order)).all()


def test_welcome_is_bilingual_with_language_buttons(chat):
    chat.say("Hi")
    assert "నమస్కారం! మా మామిడి తోటకు స్వాగతం." in chat.text
    assert "Welcome to our Mango Farm!" in chat.text
    assert chat.option_ids == ["lang_te", "lang_en"]


def test_english_menu_and_telugu_menu(chat):
    chat.say("Hello").pick("lang_en")
    assert "Welcome! What would you like to do?" in chat.text and len(chat.option_ids) == 3
    chat.say("change language").pick("lang_te")
    assert "నమస్కారం! మీకు ఏం కావాలి?" in chat.text


def test_full_english_journey_creates_order(chat, db):
    drive_to_review(chat, "en")
    assert "Total ₹150" in chat.last[0].body  # 2kg x 60 + 30 delivery
    assert chat.option_ids == ["confirm_order", "edit_order", "cancel_order"]
    chat.pick("confirm_order")
    o = orders(db)[0]
    assert re.fullmatch(r"MF-\d{8}-\d{4}", o.order_code)
    assert o.status == "PENDING" and o.total == Decimal("150.00") and o.payment_method == "COD"
    # COD is only an acknowledgement: the family must call and confirm first
    assert "Your PVR Farms order has been received successfully." in chat.text and o.order_code in chat.text
    assert "waiting for confirmation" in chat.text and "call you shortly" in chat.text
    assert "successfully placed" not in chat.text and o.confirmed_at is None
    inv = db.scalar(select(Inventory).where(Inventory.product_id == 1))
    assert inv.quantity_reserved == Decimal("2") and inv.quantity_on_hand == Decimal("200")


def test_full_telugu_journey(chat, db):
    drive_to_review(chat, "te")
    assert "మొత్తం ₹150" in chat.last[0].body
    chat.pick("confirm_order")
    assert "మీ PVR Farms ఆర్డర్ మాకు అందింది" in chat.text
    assert "ఇంకా నిర్ధారణ కాలేదు" in chat.text and "ఫోన్ చేస్తారు" in chat.text


def test_snapshot_survives_price_change(chat, db):
    drive_to_review(chat).pick("confirm_order")
    p = db.get(Product, 1)
    p.price_per_kg = Decimal("99")
    db.commit()
    o = orders(db)[0]
    assert o.items[0].price_per_kg == Decimal("60.00") and o.total == Decimal("150.00")


def test_slots_flow(chat, db):
    for dd in db.scalars(select(DeliveryDate)):
        dd.slots_enabled = True
    db.commit()
    drive_to_review(chat, slot=True)
    assert "Morning" in chat.last[0].body or "Evening" in chat.last[0].body
    chat.pick("confirm_order")
    assert orders(db)[0].slot_name


def test_invalid_quantities(chat, db):
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1")  # numbers can simply be typed
    for bad, expect in [("abc", "valid number"), ("0", "valid number"), ("-3", "valid number"), ("0.5", "minimum"),
                        ("51", "maximum"), ("500", "available")]:
        if bad == "500":
            db.get(Product, 1).max_qty = None
            db.commit()
        chat.say(bad)
        assert expect in chat.text.lower(), (bad, chat.text)
    chat.say("2.5")
    assert "2.5 kg × ₹60 = ₹150" in chat.text and "delivered" in chat.text


def test_pickle_variety_hides_one_kg_option(chat):
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_2")
    assert "qty_1" not in chat.option_ids and "qty_2" in chat.option_ids


def test_decimal_not_allowed_when_configured(chat, db):
    db.get(Product, 1).allow_decimal = False
    db.commit()
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").say("2.5")
    assert "whole kilograms" in chat.text


def test_insufficient_stock_detected_at_confirmation(chat, db):
    drive_to_review(chat, qty="qty_5")
    db.get(Product, 1).inventory.quantity_on_hand = Decimal("3")
    db.commit()
    chat.pick("confirm_order")
    assert "only 3 kg" in chat.text and not orders(db)
    assert "qty_1" in chat.option_ids  # sent back to quantity selection
    chat.pick("qty_2")  # fixes quantity -> returns straight to the summary
    assert "Please check your order" in chat.text
    chat.pick("confirm_order")
    assert len(orders(db)) == 1


def test_date_disabled_before_confirmation(chat, db):
    drive_to_review(chat)
    for dd in db.scalars(select(DeliveryDate).order_by(DeliveryDate.date).limit(1)):
        dd.is_active = False
    db.commit()
    chat.pick("confirm_order")
    assert "no longer available" in chat.text and not orders(db)
    assert any(i.startswith("date_") for i in chat.option_ids)


def test_past_and_unavailable_dates_never_offered(chat, db):
    db.add(DeliveryDate(date=today_ist() - timedelta(days=2)))
    db.add(DeliveryDate(date=today_ist() + timedelta(days=30), is_active=False))
    db.commit()
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_1")
    offered = [i[5:] for i in chat.option_ids]
    assert all(d >= today_ist().isoformat() for d in offered)
    assert (today_ist() + timedelta(days=30)).isoformat() not in offered
    chat.pick(f"date_{(today_ist() - timedelta(days=2)).isoformat()}")  # crafted old id
    assert "didn't understand" in chat.text


def test_full_day_is_not_offered(chat, db):
    first = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).first()
    first.max_orders = 1
    db.commit()
    drive_to_review(chat).pick("confirm_order")
    other = Chat(db, "919000000002")
    other.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_1")
    assert f"date_{first.date.isoformat()}" not in other.option_ids


def test_duplicate_confirmation_creates_single_order(chat, db):
    drive_to_review(chat)
    chat.pick("confirm_order")
    chat.pick("confirm_order")  # stale button pressed again
    assert "already been placed" in chat.text
    assert len(orders(db)) == 1
    assert db.scalar(select(Inventory.quantity_reserved).where(Inventory.product_id == 1)) == Decimal("2")


def test_customer_cancels_draft(chat, db):
    drive_to_review(chat).pick("cancel_order")
    assert "cancelled" in chat.text and not orders(db)
    assert "menu_order" in chat.option_ids


def test_customer_cancels_placed_pending_order_by_text(chat, db):
    drive_to_review(chat).pick("confirm_order")
    code = orders(db)[0].order_code
    chat.say(f"cancel {code}")
    assert "Do you want to cancel order" in chat.text  # asks first
    chat.pick("cancel_yes")
    assert f"Your order {code} has been cancelled as you asked" in chat.text
    o = orders(db)[0]
    assert o.status == CANCELLED
    assert db.scalar(select(Inventory.quantity_reserved).where(Inventory.product_id == 1)) == 0


def test_cannot_cancel_someone_elses_order(chat, db):
    drive_to_review(chat).pick("confirm_order")
    code = orders(db)[0].order_code
    other = Chat(db, "919000000009")
    other.say("Hi").pick("lang_en").say(f"cancel {code}")
    assert "could not find" in other.text and orders(db)[0].status == "PENDING"


def test_back_navigation_and_edit(chat, db):
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_2")
    chat.say("back")
    assert "How many kg" in chat.text
    chat.say("back")
    assert "Choose your mangoes" in chat.text
    chat.pick("prod_2").pick("qty_5").pick(first_date_id(chat))
    chat.say("Sita").say("Near temple, Main Street")
    assert "Sita" in chat.last[0].body and "+91 9000000001" in chat.last[0].body  # WhatsApp number used automatically
    chat.pick("edit_order").pick("edit_phone").say("98765 43210")
    assert "+91 9876543210" in chat.last[0].body
    chat.pick("edit_order").pick("edit_alt").say("9123456789")
    assert "+91 9123456789" in chat.last[0].body
    chat.pick("edit_order").pick("edit_qty").pick("qty_2")
    assert "2 kg" in chat.last[0].body
    chat.pick("edit_order").pick("edit_name").say("Sita Devi")
    assert "Sita Devi" in chat.last[0].body
    chat.pick("confirm_order")
    o = orders(db)[0]
    assert o.contact_phone == "919876543210" and o.alt_phone == "919123456789" and o.total == Decimal("120.00")


def test_invalid_phone_and_short_address_rejected(chat):
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_1").pick(first_date_id(chat))
    chat.say("Ravi").say("abc")
    assert "more detail" in chat.text
    chat.say("4-5 Main Road, near the temple")
    chat.pick("edit_order").pick("edit_phone").say("12345")
    assert "10-digit" in chat.text
    chat.say("9876543210")
    assert "+91 9876543210" in chat.last[0].body
    chat.pick("edit_order").pick("edit_alt").say("not-a-phone")
    assert "10-digit" in chat.text


def test_language_switch_mid_order_keeps_place(chat):
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1")
    chat.say("భాష మార్చండి").pick("lang_te")
    assert "భాష తెలుగుకు" in chat.text and "కిలోలు కావాలి" in chat.text


def test_unexpected_input_reprompts(chat):
    chat.say("Hi").pick("lang_en").say("banana")
    assert "didn't understand" in chat.text and len(chat.option_ids) == 3


def test_numeric_replies_work_like_buttons(chat):
    chat.say("Hi").say("2")  # 2 = English
    assert "Welcome! What would you like" in chat.text
    chat.say("2")  # menu: 1 = Order Now, 2 = Prices, 3 = More
    assert "Today's prices" in chat.text and "₹60" in chat.text


def test_varieties_prices_come_from_database(chat, db):
    db.get(Product, 1).price_per_kg = Decimal("77")
    db.commit()
    chat.say("Hi").pick("lang_en").pick("menu_order")  # the ordering cards show the live price
    assert "₹77 / kg" in chat.text


def test_contact_shows_settings_and_stays_quiet_afterwards(chat):
    chat.say("Hi").pick("lang_en").pick("menu_more").pick("menu_contact")
    assert "Contact us" in chat.text
    chat.say("I need help with my order")
    assert chat.last == []  # human support: bot does not interrupt
    chat.say("menu")
    assert len(chat.option_ids) == 3


def test_session_timeout_recovers(chat, db):
    from app.models import Conversation
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1")
    conv = db.scalar(select(Conversation))
    conv.last_inbound_at = conv.last_inbound_at - timedelta(hours=3)
    db.commit()
    chat.say("2")
    assert "start again" in chat.text and len(chat.option_ids) == 3


def test_not_accepting_orders(chat, db):
    from app.services.core import set_setting
    drive_to_review(chat)
    set_setting(db, "accepting_orders", "false")
    db.commit()
    chat.pick("confirm_order")
    assert "not taking new orders" in chat.text and not orders(db)


def test_unsupported_message_type(db):
    from app.services.conversation import handle_incoming
    from app.services.messaging import InMessage
    out = handle_incoming(db, InMessage(id="x1", phone="919000000003", kind="unsupported"), "simulator")
    assert "can't read" in out[0].body


def test_messages_are_logged(chat, db):
    chat.say("Hi")
    assert db.scalar(select(func.count(MessageLog.id)).where(MessageLog.direction == "IN")) == 1
    assert db.scalar(select(func.count(MessageLog.id)).where(MessageLog.direction == "OUT")) == 1
