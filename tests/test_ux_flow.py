"""The shorter, friendlier ordering flow: fewer steps, saved details, photo cards, Pay button, WhatsApp limits."""
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import DeliveryArea, DeliveryDate, Order, Product
from app.services.catalog import create_product
from app.services.messaging import OutMessage, build_payload
from tests.conftest import Chat, allow_cod, drive_to_review, first_date_id

PHONE = "919000000001"


class RecordingChat(Chat):
    """Keeps every message the bot sent, so limits can be checked over a whole journey."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.all: list[OutMessage] = []

    def _send(self, **kw):
        super()._send(**kw)
        self.all.extend(self.last)
        return self


def titles(chat):
    return [o.title for m in chat.last for o in m.options]


def check_whatsapp_limits(msgs):
    """Meta's documented limits: <=3 reply buttons (title<=20), list <=10 rows (title<=24, desc<=72),
    body<=1024, footer<=60, list/cta button label<=20."""
    for m in msgs:
        assert len(m.body) <= 1024 or m.kind == "text", m.body[:60]
        assert len(m.footer) <= 60, m.footer
        if m.kind == "buttons":
            assert 1 <= len(m.options) <= 3, [o.title for o in m.options]
            assert all(len(o.title) <= 20 for o in m.options), [o.title for o in m.options if len(o.title) > 20]
        if m.kind == "list":
            assert 1 <= len(m.options) <= 10 and len(m.button_label) <= 20
            assert all(len(o.title) <= 24 and len(o.desc) <= 72 for o in m.options), \
                [(o.title, o.desc) for o in m.options if len(o.title) > 24 or len(o.desc) > 72]
        if m.kind == "cta":
            assert len(m.cta_text) <= 20 and m.cta_url.startswith("http")


# ------------------------------------------------------------------ fewer steps
def test_new_customer_orders_in_nine_messages_not_fourteen(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_2")
    chat.pick(first_date_id(chat)).say("Ravi Kumar").say("4-5 Main Road, near the temple").pick("confirm_order")
    assert chat.n == 9
    assert db.scalar(select(Order)).status == "AWAITING_PAYMENT"


def test_returning_customer_orders_in_seven_messages(db):
    first = Chat(db)
    drive_to_review(first, pay="online").pick("confirm_order")
    chat = Chat(db)  # same customer, new conversation turn
    chat.say("Hi").pick("menu_order").pick("prod_1").pick("qty_2").pick(first_date_id(chat))
    assert "use_saved" in chat.option_ids and "Deliver to your saved address?" in chat.text
    chat.pick("use_saved")
    assert "Please check your order" in chat.last[0].body
    chat.pick("confirm_order")
    assert chat.n == 7 and len(db.scalars(select(Order)).all()) == 2


def test_first_order_has_no_phone_alternative_phone_landmark_or_village_questions(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_2").pick(first_date_id(chat))
    assert "What is your name?" in chat.text
    chat.say("Ravi Kumar")
    assert "delivery address with a nearby landmark" in chat.text and not [i for i in chat.option_ids if i.startswith("area_")]
    chat.say("4-5 Main Road, near the temple")
    body = chat.last[0].body
    assert "+91 9000000001" in body and "Govindaraopet" in body and "near the temple" in body  # phone + village automatic
    chat.pick("confirm_order")
    o = db.scalar(select(Order))
    assert o.contact_phone == PHONE and o.village_name == "Govindaraopet" and o.landmark is None
    assert "near the temple" in o.address and o.alt_phone is None


def test_village_is_asked_only_when_more_than_one_is_served(db):
    db.add(DeliveryArea(name_en="Mulugu", name_te="ములుగు", delivery_charge=Decimal("50")))
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_2").pick(first_date_id(chat)).say("Ravi")
    areas = [i for i in chat.option_ids if i.startswith("area_")]
    assert len(areas) == 2 and "village" in chat.text.lower()
    chat.pick(areas[1]).say("4-5 Main Road, near the temple")
    assert "Mulugu" in chat.last[0].body and "Delivery ₹50" in chat.last[0].body


# ------------------------------------------------------------------ saved details
def test_saved_details_use_most_recent_order_and_can_be_changed(db):
    drive_to_review(Chat(db), pay="online").pick("confirm_order")
    chat = Chat(db)
    chat.say("Hi").pick("menu_order").pick("prod_1").pick("qty_1").pick(first_date_id(chat))
    assert chat.option_ids == ["use_saved", "change_saved"]
    assert "Ravi Kumar" in chat.text and "+91 9000000001" in chat.text and "near the temple" in chat.text
    chat.pick("change_saved")
    assert "What is your name?" in chat.text
    chat.say("Sita").say("9-9 New Colony, near the school")
    o_text = chat.last[0].body
    assert "Sita" in o_text and "New Colony" in o_text


def test_saved_details_skipped_when_saved_village_is_no_longer_served(db):
    drive_to_review(Chat(db), pay="online").pick("confirm_order")
    db.add(DeliveryArea(name_en="Mulugu", name_te="ములుగు", delivery_charge=Decimal("50")))
    db.get(DeliveryArea, 1).is_active = False
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("menu_order").pick("prod_1").pick("qty_1").pick(first_date_id(chat))
    assert "use_saved" not in chat.option_ids and "What is your name?" in chat.text


def test_main_menu_greets_returning_customer_by_name(db):
    chat = Chat(db)
    chat.say("Hi")
    assert "Welcome back" not in chat.text
    drive_to_review(Chat(db), pay="online").pick("confirm_order")
    chat.say("menu")
    assert "Welcome back, Ravi!" in chat.text


# ------------------------------------------------------------------ menu
def test_menu_is_three_buttons_and_more_holds_the_rest(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en")
    m = chat.last[0]
    assert m.kind == "buttons" and titles(chat) == ["🛒 Order Now", "💰 Prices", "⋯ More"]
    chat.pick("menu_more")
    assert chat.last[0].kind == "list" and chat.option_ids == ["menu_contact", "menu_language"]
    chat.pick("menu_language").pick("lang_te")
    assert titles(chat) == ["🛒 ఆర్డర్ చేయండి", "💰 ధరలు", "⋯ మరిన్ని"]


# ------------------------------------------------------------------ product cards
def test_products_are_photo_cards_each_with_an_order_button(db):
    db.get(Product, 1).image_path = "mango1.jpg"
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order")
    assert "Choose your mangoes" in chat.last[0].body
    cards = chat.last[1:]
    assert [c.options[0].id for c in cards] == ["prod_1", "prod_2"] and all(c.kind == "buttons" for c in cards)
    assert cards[0].header_image == "https://shop.test/media/mango1.jpg" and cards[1].header_image is None
    assert "₹60 / kg" in cards[0].body and "in stock" in cards[0].body and cards[0].options[0].title == "🛒 Order this"
    payload = build_payload("91x", cards[0])["interactive"]
    assert payload["type"] == "button" and payload["header"] == {"type": "image", "image": {"link": cards[0].header_image}}
    chat.pick("prod_2")  # every card's button works, not only the last one
    assert "Raw Mangoes (for pickle)" in chat.text


def test_out_of_stock_variety_is_shown_but_cannot_be_ordered(db):
    db.get(Product, 2).inventory.quantity_on_hand = Decimal("0")
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order")
    assert [c.options for c in chat.last[1:]] == [chat.last[1].options, []]
    assert "Out of stock" in chat.last[2].body
    chat.pick("prod_2")
    assert "didn't understand" in chat.text


def test_variety_can_be_chosen_by_typing_its_name(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").say("pickle")
    assert "How many kg of *Raw Mangoes (for pickle)*" in chat.text


def test_many_varieties_fall_back_to_one_list(db):
    for i in range(4):
        create_product(db, stock=Decimal("50"), name_en=f"Variety {i}", name_te=f"రకం {i}", description_en="", description_te="",
                       price_per_kg=Decimal("70"), min_qty=Decimal("1"), allow_decimal=False, sort_order=10 + i)
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order")
    assert len(chat.last) == 1 and chat.last[0].kind == "list" and len(chat.last[0].options) == 6


# ------------------------------------------------------------------ quantity
def test_quantity_buttons_show_the_price_and_typed_numbers_mean_kg(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1")
    assert chat.last[0].kind == "buttons" and titles(chat) == ["1 kg · ₹60", "2 kg · ₹120", "5 kg · ₹300"]
    chat.say("3")  # NOT the third button (5 kg)
    assert "✅ 3 kg × ₹60 = ₹180" in chat.text and "When would you like your mangoes delivered?" in chat.text
    assert len(chat.last) == 1  # the total and the date question are one message


def test_quantity_buttons_follow_each_products_rules(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_2")  # pickle: minimum 2 kg
    assert titles(chat) == ["2 kg · ₹90", "5 kg · ₹225", "10 kg · ₹450"]
    chat.say("2.5")
    assert "2.5 kg × ₹45" in chat.text


def test_quantity_in_telugu(db):
    chat = Chat(db)
    chat.say("Hi").pick("lang_te").pick("menu_order").pick("prod_1")
    assert titles(chat) == ["1 కిలో · ₹60", "2 కిలో · ₹120", "5 కిలో · ₹300"]


# ------------------------------------------------------------------ dates
def test_dates_use_tomorrow_and_dd_mm_yyyy_and_flag_scarcity(db):
    first = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).first()
    first.max_orders = 3
    db.commit()
    chat = Chat(db)
    chat.say("Hi").pick("lang_en").pick("menu_order").pick("prod_1").pick("qty_2")
    opts = chat.last[0].options
    assert opts[0].title.startswith("Tomorrow · ") and re.fullmatch(r"Tomorrow · \d{2}-\d{2}-\d{4}", opts[0].title)
    assert opts[0].desc == "Only 3 left" and opts[1].desc == "" and re.fullmatch(r"[A-Z][a-z]{2} · \d{2}-\d{2}-\d{4}", opts[1].title)
    chat2 = Chat(db, "919000000002")
    chat2.say("Hi").pick("lang_te").pick("menu_order").pick("prod_1").pick("qty_2")
    assert chat2.last[0].options[0].title.startswith("రేపు · ") and chat2.last[0].options[0].desc == "3 మాత్రమే మిగిలాయి"


# ------------------------------------------------------------------ summary and payment
@pytest.mark.parametrize("lang", ["en", "te"])
def test_summary_is_one_message_with_the_buttons(db, lang):
    chat = Chat(db)
    drive_to_review(chat, lang=lang, pay="online")
    assert len(chat.last) == 1 and chat.last[0].kind == "buttons" and len(chat.last[0].body) <= 1024
    assert chat.option_ids == ["confirm_order", "edit_order", "cancel_order"]
    assert ("Please check your order" if lang == "en" else "మీ ఆర్డర్ ఒకసారి చూడండి") in chat.last[0].body


def test_summary_stays_one_message_even_with_a_very_long_address(db):
    chat = Chat(db)
    drive_to_review(chat, pay="online")
    chat.pick("edit_order").pick("edit_address").say("Street number " + "very long address text " * 14)
    assert len(chat.last) == 1 and len(chat.last[0].body) <= 1024


def test_payment_link_is_a_pay_button_with_a_cancel_hint(db):
    chat = Chat(db)
    drive_to_review(chat, pay="online").pick("confirm_order")
    assert len(chat.last) == 1
    m = chat.last[0]
    assert m.kind == "cta" and m.cta_text == "Pay ₹150" and m.cta_url.startswith("https://shop.test/pay/")
    assert "https://" not in m.body and "Send 'cancel'" in m.footer
    p = build_payload("91x", m)["interactive"]
    assert p["type"] == "cta_url" and p["action"]["name"] == "cta_url"
    assert p["action"]["parameters"] == {"display_text": "Pay ₹150", "url": m.cta_url} and p["footer"]["text"] == m.footer


def test_cta_falls_back_to_plain_text_for_non_https_links():
    p = build_payload("91x", OutMessage("cta", "Pay now", cta_text="Pay", cta_url="http://localhost:8000/pay/abc"))
    assert p["type"] == "text" and "http://localhost:8000/pay/abc" in p["text"]["body"]


# ------------------------------------------------------------------ WhatsApp limits on every screen
@pytest.mark.parametrize("lang", ["en", "te"])
def test_every_message_in_a_full_journey_respects_whatsapp_limits(db, lang):
    allow_cod(db)
    chat = RecordingChat(db)
    drive_to_review(chat, lang=lang, pay="cod", slot=False)
    chat.pick("edit_order")  # edit list
    chat.pick("edit_back").pick("confirm_order")
    chat.say("menu").pick("menu_more").pick("menu_language").pick("lang_en" if lang == "te" else "lang_te")
    chat.say("cancel").pick("cancel_no")  # cancel question
    chat.say("menu").pick("menu_prices")
    assert len(chat.all) > 20
    check_whatsapp_limits(chat.all)
    for m in chat.all:  # and every one of them can be turned into a valid Cloud API request
        build_payload("91x", m)


def test_slots_screen_respects_limits(db):
    for dd in db.scalars(select(DeliveryDate)):
        dd.slots_enabled = True
    db.commit()
    chat = RecordingChat(db)
    drive_to_review(chat, lang="te", pay="online", slot=True)
    check_whatsapp_limits(chat.all)

