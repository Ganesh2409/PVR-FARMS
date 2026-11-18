"""All customer-facing text, Telugu (te) and English (en)."""

WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "te": ["సోమ", "మంగళ", "బుధ", "గురు", "శుక్ర", "శని", "ఆది"],
}

WELCOME = (
    "నమస్కారం! మా మామిడి తోటకు స్వాగతం.\n\n"
    "Welcome to our Mango Farm!\n\n"
    "Fresh farm mangoes are available for home delivery.\n\n"
    "Please select your preferred language.\n"
    "దయచేసి మీ భాషను ఎంచుకోండి."
)

T: dict[str, dict[str, str]] = {
    "menu_title": {"te": "నమస్కారం! మీకు ఏం కావాలి?", "en": "Welcome! What would you like to do?"},
    "menu_varieties": {"te": "మామిడి రకాలు చూడండి", "en": "View Mango Varieties"},
    "menu_order": {"te": "ఆర్డర్ చేయండి", "en": "Place an Order"},
    "menu_prices": {"te": "ధరలు తెలుసుకోండి", "en": "Check Prices"},
    "menu_contact": {"te": "మమ్మల్ని సంప్రదించండి", "en": "Contact Us"},
    "menu_language": {"te": "భాష మార్చండి", "en": "Change Language"},
    "choose": {"te": "ఎంచుకోండి", "en": "Select"},
    "hint": {"te": "'వెనక్కి' = వెనక్కి వెళ్ళడానికి, 'రద్దు' = ఆర్డర్ రద్దు, 'మెనూ' = ప్రధాన మెనూ",
             "en": "Type 'back' to go back, 'cancel' to cancel, 'menu' for main menu"},
    "lang_changed": {"te": "భాష తెలుగుకు మార్చబడింది.", "en": "Language changed to English."},
    "stock_kg": {"te": "స్టాక్: {qty} కిలోలు", "en": "In stock: {qty} kg"},
    "out_of_stock": {"te": "ప్రస్తుతం స్టాక్ లేదు", "en": "Currently out of stock"},
    "product_card": {"te": "*{name}*\n{desc}\n\nధర: ₹{price} / కిలో\n{stock}",
                     "en": "*{name}*\n{desc}\n\nPrice: ₹{price} / kg\n{stock}"},
    "no_products": {"te": "క్షమించండి, ప్రస్తుతం ఏ రకం అందుబాటులో లేదు. తర్వాత ప్రయత్నించండి.",
                    "en": "Sorry, no varieties are available right now. Please try again later."},
    "prices_header": {"te": "ఈ రోజు ధరలు:", "en": "Today's prices:"},
    "price_line": {"te": "• {name}: ₹{price} / కిలో ({stock})", "en": "• {name}: ₹{price} / kg ({stock})"},
    "stock_short_in": {"te": "{qty} కిలోలు ఉన్నాయి", "en": "{qty} kg available"},
    "stock_short_out": {"te": "స్టాక్ లేదు", "en": "out of stock"},
    "contact": {"te": "మమ్మల్ని సంప్రదించండి:\n*{farm}*\n📞 {phone}\n📍 {address}\n🕘 {hours}\n\nమా బృందం త్వరలో మీకు సహాయం చేస్తుంది. మెనూకు వెళ్ళడానికి 'మెనూ' అని పంపండి.",
                "en": "Contact us:\n*{farm}*\n📞 {phone}\n📍 {address}\n🕘 {hours}\n\nOur team will help you soon. Send 'menu' to return to the main menu."},
    "choose_product": {"te": "ఏ రకం మామిడి కాయలు కావాలి?", "en": "Which mangoes would you like?"},
    "ask_quantity": {"te": "*{name}* ఎన్ని కిలోలు కావాలి?\n(ధర ₹{price} / కిలో)",
                     "en": "How many kilograms of *{name}* would you like?\n(Price ₹{price} / kg)"},
    "qty_1": {"te": "1 కిలో", "en": "1 kg"},
    "qty_2": {"te": "2 కిలోలు", "en": "2 kg"},
    "qty_5": {"te": "5 కిలోలు", "en": "5 kg"},
    "qty_custom": {"te": "ఇంకో పరిమాణం", "en": "Custom quantity"},
    "ask_custom_qty": {"te": "మీకు కావాల్సిన కిలోల సంఖ్య టైప్ చేయండి (ఉదా: 3). కనీసం {min} కిలోలు.",
                       "en": "Type the number of kilograms you need (e.g. 3). Minimum {min} kg."},
    "qty_invalid": {"te": "దయచేసి సరైన సంఖ్య టైప్ చేయండి. ఉదా: 3", "en": "Please type a valid number, e.g. 3"},
    "qty_no_decimal": {"te": "ఈ రకానికి పూర్తి కిలోలు మాత్రమే (1, 2, 3...) ఆర్డర్ చేయగలరు.",
                       "en": "This variety can only be ordered in whole kilograms (1, 2, 3...)."},
    "qty_too_low": {"te": "కనీసం {min} కిలోలు ఆర్డర్ చేయాలి.", "en": "The minimum order is {min} kg."},
    "qty_too_high": {"te": "ఒక్క ఆర్డర్‌లో గరిష్టంగా {max} కిలోలు మాత్రమే.", "en": "The maximum per order is {max} kg."},
    "qty_no_stock": {"te": "క్షమించండి, ప్రస్తుతం {avail} కిలోలు మాత్రమే ఉన్నాయి.",
                     "en": "Sorry, only {avail} kg is available right now."},
    "qty_ok": {"te": "{qty} కిలోలు × ₹{price} = ₹{sub}", "en": "{qty} kg × ₹{price} = ₹{sub}"},
    "ask_date": {"te": "మీకు మామిడి కాయలు ఎప్పుడు డెలివరీ కావాలి?", "en": "When would you like your mangoes delivered?"},
    "no_dates": {"te": "క్షమించండి, ప్రస్తుతం డెలివరీ తేదీలు అందుబాటులో లేవు. దయచేసి తర్వాత ప్రయత్నించండి.",
                 "en": "Sorry, no delivery dates are available right now. Please try again later."},
    "ask_slot": {"te": "డెలివరీ సమయం ఎంచుకోండి:", "en": "Please choose a delivery time:"},
    "ask_name": {"te": "మీ పేరు చెప్పండి.", "en": "Please tell us your name."},
    "name_invalid": {"te": "దయచేసి మీ పేరు సరిగ్గా టైప్ చేయండి.", "en": "Please type your name."},
    "confirm_phone": {"te": "మీ WhatsApp నంబర్ {phone} నే డెలివరీకి ఉపయోగించమంటారా?",
                      "en": "Shall we use your WhatsApp number {phone} for this delivery?"},
    "yes_use": {"te": "అవును, ఇదే", "en": "Yes, use this"},
    "other_number": {"te": "వేరే నంబర్", "en": "Different number"},
    "ask_phone": {"te": "మీ ఫోన్ నంబర్ టైప్ చేయండి (10 అంకెలు).", "en": "Please type your phone number (10 digits)."},
    "phone_invalid": {"te": "ఈ నంబర్ సరిగ్గా లేదు. 10 అంకెల మొబైల్ నంబర్ టైప్ చేయండి.",
                      "en": "That number doesn't look right. Please type a 10-digit mobile number."},
    "ask_alt": {"te": "ఇంకో ఫోన్ నంబర్ ఉంటే టైప్ చేయండి (ఐచ్ఛికం). లేకపోతే 'వద్దు' నొక్కండి.",
                "en": "Type an alternative phone number if you have one (optional), or tap Skip."},
    "skip": {"te": "వద్దు", "en": "Skip"},
    "ask_village": {"te": "మీ ఊరు ఎంచుకోండి:", "en": "Please select your village:"},
    "delivery_charge_desc": {"te": "డెలివరీ ₹{c}", "en": "Delivery ₹{c}"},
    "free_delivery": {"te": "ఉచిత డెలివరీ", "en": "Free delivery"},
    "ask_address": {"te": "మీ పూర్తి చిరునామా టైప్ చేయండి (ఇంటి నంబర్, వీధి పేరు).",
                    "en": "Please type your complete delivery address (house number, street)."},
    "address_short": {"te": "చిరునామా కొంచెం వివరంగా రాయండి.", "en": "Please write the address in a little more detail."},
    "ask_landmark": {"te": "దగ్గరలో ఉన్న గుర్తు (ల్యాండ్‌మార్క్) చెప్పండి. లేకపోతే 'వద్దు' నొక్కండి.",
                     "en": "Please tell us a nearby landmark, or tap Skip."},
    "review_title": {"te": "మీ ఆర్డర్ వివరాలు:", "en": "Your order summary:"},
    "review_body": {
        "te": ("👤 పేరు: {name}\n📞 ఫోన్: {phone}\n\n🥭 రకం: {product}\n⚖️ పరిమాణం: {qty} కిలోలు\n"
               "💰 కిలో ధర: ₹{price}\n🧮 మామిడి ధర: ₹{subtotal}\n🚚 డెలివరీ ఛార్జీ: ₹{delivery}\n"
               "✅ *మొత్తం చెల్లించాల్సింది: ₹{total}*\n\n📍 చిరునామా: {address}\n"
               "📅 డెలివరీ తేదీ: {date}\n🕘 డెలివరీ సమయం: {slot}\n💵 చెల్లింపు: Cash on Delivery"),
        "en": ("👤 Name: {name}\n📞 Phone: {phone}\n\n🥭 Variety: {product}\n⚖️ Quantity: {qty} kg\n"
               "💰 Price per kg: ₹{price}\n🧮 Mango subtotal: ₹{subtotal}\n🚚 Delivery charges: ₹{delivery}\n"
               "✅ *Total payable: ₹{total}*\n\n📍 Address: {address}\n"
               "📅 Delivery date: {date}\n🕘 Delivery time: {slot}\n💵 Payment: Cash on Delivery"),
    },
    "any_time": {"te": "రోజంతా", "en": "Any time"},
    "confirm_q": {"te": "మీ ఆర్డర్‌ను నిర్ధారించాలనుకుంటున్నారా?", "en": "Would you like to confirm your order?"},
    "btn_confirm": {"te": "ఆర్డర్ నిర్ధారించు", "en": "Confirm Order"},
    "btn_edit": {"te": "వివరాలు మార్చు", "en": "Edit Details"},
    "btn_cancel": {"te": "ఆర్డర్ రద్దు", "en": "Cancel Order"},
    "edit_q": {"te": "ఏది మార్చాలి?", "en": "What would you like to change?"},
    "edit_product": {"te": "మామిడి రకం", "en": "Mango variety"},
    "edit_qty": {"te": "పరిమాణం", "en": "Quantity"},
    "edit_date": {"te": "డెలివరీ తేదీ", "en": "Delivery date"},
    "edit_name": {"te": "పేరు", "en": "Name"},
    "edit_phone": {"te": "ఫోన్ నంబర్", "en": "Phone number"},
    "edit_village": {"te": "ఊరు", "en": "Village"},
    "edit_address": {"te": "చిరునామా", "en": "Address"},
    "edit_landmark": {"te": "ల్యాండ్‌మార్క్", "en": "Landmark"},
    "edit_back": {"te": "వెనక్కి (సమ్మరీ)", "en": "Back to summary"},
    "order_confirmed": {
        "te": ("నమస్కారం {name} గారు!\n\nమీ ఆర్డర్ విజయవంతంగా నమోదు చేయబడింది!\n\n"
               "ఆర్డర్ నంబర్: {order_id}\nమామిడి రకం: {product}\nపరిమాణం: {qty} కిలోలు\n"
               "మొత్తం ధర: ₹{total}\nడెలివరీ తేదీ: {date}\nచిరునామా: {address}\n"
               "{payment}\n\n"
               "మా మామిడి తోటను ఎంచుకున్నందుకు ధన్యవాదాలు!\n\n"
               "మీ ఇంటికి తాజా మామిడి కాయలను అందించడానికి మేము సిద్ధంగా ఉన్నాము!"),
        "en": ("Hello {name}!\n\nYour order has been successfully placed!\n\n"
               "Order ID: {order_id}\nProduct: {product}\nQuantity: {qty} kg\n"
               "Total Amount: ₹{total}\nDelivery Date: {date}\nDelivery Address: {address}\n"
               "{payment}\n\n"
               "Thank you for choosing our farm!\n\n"
               "We will contact you if there are any changes to your delivery. "
               "We look forward to delivering fresh mangoes to your doorstep!"),
    },
    "pay_line_cod": {"te": "చెల్లింపు విధానం: Cash on Delivery.", "en": "Payment Method: Cash on Delivery."},
    "pay_line_online": {"te": "చెల్లింపు విధానం: ఆన్‌లైన్ (చెల్లించబడింది ✅)\nచెల్లింపు రిఫరెన్స్: {ref}",
                        "en": "Payment Method: Online (paid ✅)\nPayment Reference: {ref}"},
    "pay_method_q": {"te": "ఎలా చెల్లిస్తారు?", "en": "How would you like to pay?"},
    "btn_pay_online": {"te": "ఆన్‌లైన్ (UPI/కార్డ్)", "en": "Pay online (UPI/card)"},
    "btn_pay_cod": {"te": "డెలివరీ సమయంలో నగదు", "en": "Cash on Delivery"},
    "review_pay_online": {"te": "ఆన్‌లైన్ చెల్లింపు (UPI / కార్డ్ మొదలైనవి)", "en": "Online payment (UPI / cards etc.)"},
    "review_pay_cod": {"te": "Cash on Delivery", "en": "Cash on Delivery"},
    "btn_confirm_pay": {"te": "నిర్ధారించి చెల్లించు", "en": "Confirm & Pay"},
    "pay_link": {"te": ("ఆర్డర్ {order_id} కోసం ₹{total} చెల్లించడానికి ఈ సురక్షిత లింక్ నొక్కండి (UPI, కార్డ్, నెట్‌బ్యాంకింగ్ మొదలైనవి):\n{url}\n\n"
                        "ఈ లింక్ {expiry} వరకు పనిచేస్తుంది. చెల్లింపు నిర్ధారణ అయిన తర్వాతే మీ ఆర్డర్ నమోదు అవుతుంది. అప్పటివరకు మామిడి కాయలు మీ కోసం ఉంచుతాము."),
                 "en": ("To pay ₹{total} for order {order_id}, tap this secure link (UPI, card, net banking and more):\n{url}\n\n"
                        "The link works until {expiry}. Your order is confirmed only after the payment is verified. We are holding your mangoes until then.")},
    "pay_pending_status": {"te": "ఆర్డర్ {order_id} చెల్లింపు కోసం ఎదురుచూస్తున్నాము.\n{url}\n(వరకు: {expiry})",
                           "en": "Order {order_id} is waiting for payment.\n{url}\n(valid until {expiry})"},
    "btn_cancel_payment": {"te": "ఆర్డర్ రద్దు చేయి", "en": "Cancel this order"},
    "btn_menu": {"te": "ప్రధాన మెనూ", "en": "Main menu"},
    "pay_unavailable": {"te": "క్షమించండి, ఇప్పుడు ఆన్‌లైన్ చెల్లింపు ప్రారంభించలేకపోయాము. మీ ఆర్డర్ నమోదు కాలేదు. కొద్దిసేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.",
                        "en": "Sorry, we could not start the online payment, so your order was not placed. Please try again in a little while."},
    "pay_failed_attempt": {"te": "ఆర్డర్ {order_id} చెల్లింపు విఫలమైంది. అదే లింక్‌తో మళ్ళీ ప్రయత్నించవచ్చు:\n{url}\n(వరకు: {expiry})",
                           "en": "The payment for order {order_id} did not go through. You can try again with the same link:\n{url}\n(valid until {expiry})"},
    "pay_expired": {"te": "ఆర్డర్ {order_id} చెల్లింపు సమయం ముగిసింది, కాబట్టి ఆర్డర్ రద్దయింది. మళ్ళీ ఆర్డర్ చేయాలంటే 'మెనూ' అని పంపండి. ఒకవేళ డబ్బు కట్ అయి ఉంటే అది వెనక్కి వస్తుంది.",
                    "en": "The payment time for order {order_id} ended, so the order was cancelled. Send 'menu' to order again. If any money was deducted it will be refunded."},
    "pay_cancelled": {"te": "ఆర్డర్ {order_id} రద్దు చేయబడింది. ఎలాంటి చెల్లింపు తీసుకోలేదు.", "en": "Order {order_id} has been cancelled. No payment was taken."},
    "pay_failed_final": {"te": "ఆర్డర్ {order_id} చెల్లింపు విఫలమైనందున ఆర్డర్ రద్దయింది. మళ్ళీ ప్రయత్నించడానికి 'మెనూ' అని పంపండి.",
                         "en": "Order {order_id} was cancelled because the payment failed. Send 'menu' to try again."},
    "pay_refunded": {"te": "ఆర్డర్ {order_id} కోసం ₹{amount} రీఫండ్ చేయబడింది. బ్యాంకును బట్టి మీ ఖాతాలో కనిపించడానికి కొన్ని రోజులు పట్టవచ్చు.",
                     "en": "A refund of ₹{amount} for order {order_id} has been processed. It may take a few days to show in your account."},
    "cod_received": {"te": "ధన్యవాదాలు! మీ {farm} ఆర్డర్ మాకు అందింది.\n\nఆర్డర్ నంబర్: {order_id}\n\nమీ ఆర్డర్ ఇంకా నిర్ధారణ కాలేదు, నిర్ధారణ కోసం వేచి ఉంది. మా వాళ్ళు మీ వివరాలు సరిచూసుకోవడానికి త్వరలో మీకు ఫోన్ చేస్తారు. ఆర్డర్ నిర్ధారణ అయిన వెంటనే మీకు మళ్ళీ మెసేజ్ పంపుతాము.",
                     "en": "Thank you! Your {farm} order has been received successfully.\n\nOrder ID: {order_id}\n\nYour order is currently waiting for confirmation. Our team will call you shortly to verify your details. We will send another message once your order is confirmed."},
    "cancel_pick": {"te": "ఏ ఆర్డర్ రద్దు చేయాలి?", "en": "Which order would you like to cancel?"},
    "cancel_confirm_q": {"te": "ఆర్డర్ {order_id} (₹{total}) రద్దు చేయాలా?{extra}", "en": "Do you want to cancel order {order_id} (₹{total})?{extra}"},
    "cancel_extra_paid": {"te": "\n\nమీరు చెల్లించిన ₹{amount} పూర్తిగా వెనక్కి ఇవ్వబడుతుంది.", "en": "\n\nThe ₹{amount} you paid will be refunded in full."},
    "btn_cancel_yes": {"te": "అవును, రద్దు చేయి", "en": "Yes, cancel order"},
    "btn_cancel_no": {"te": "వద్దు, ఉంచండి", "en": "No, keep it"},
    "cancel_kept": {"te": "సరే, ఆర్డర్ {order_id} అలాగే ఉంటుంది.", "en": "Okay, your order {order_id} stays as it is."},
    "cancel_done": {"te": "మీ ఆర్డర్ {order_id} మీరు కోరినట్లు రద్దు చేయబడింది. మళ్ళీ ఆర్డర్ చేయాలంటే 'మెనూ' అని పంపండి.",
                    "en": "Your order {order_id} has been cancelled as you asked. Send 'menu' whenever you want to order again."},
    "cancel_done_refund": {"te": "మీ ఆర్డర్ {order_id} రద్దు చేయబడింది. మీరు చెల్లించిన ₹{amount} వెనక్కి ఇవ్వడం మొదలుపెట్టాము. రీఫండ్ నిర్ధారణ అయ్యాక మీకు మళ్ళీ మెసేజ్ పంపుతాము (సాధారణంగా కొన్ని రోజులు పడుతుంది).",
                           "en": "Your order {order_id} has been cancelled. We have started refunding the ₹{amount} you paid. We will message you again when the refund is confirmed (usually a few days)."},
    "cancel_dispatched": {"te": "ఆర్డర్ {order_id} ఇప్పటికే బయలుదేరింది, కాబట్టి ఇక్కడ రద్దు చేయలేము. మీ అభ్యర్థనను మా వాళ్ళకు తెలియజేశాము, వాళ్ళు మిమ్మల్ని సంప్రదిస్తారు. మీరు నేరుగా కూడా ఫోన్ చేయవచ్చు: {phone}",
                          "en": "Order {order_id} has already been dispatched, so it cannot be cancelled here. We have told our team about your request and they will contact you. You can also call us: {phone}"},
    "cancel_dispatched_again": {"te": "ఆర్డర్ {order_id} రద్దు అభ్యర్థన ఇప్పటికే మా వాళ్ళకు చేరింది. వాళ్ళు మిమ్మల్ని సంప్రదిస్తారు. ఫోన్: {phone}",
                                "en": "Your cancellation request for order {order_id} has already reached our team. They will contact you. Phone: {phone}"},
    "cancel_delivered": {"te": "ఆర్డర్ {order_id} ఇప్పటికే డెలివరీ అయింది, కాబట్టి రద్దు చేయలేము. ఏదైనా సమస్య ఉంటే దయచేసి మమ్మల్ని సంప్రదించండి: {phone}",
                         "en": "Order {order_id} has already been delivered, so it cannot be cancelled. If there is any problem, please contact us: {phone}"},
    "cancel_none": {"te": "రద్దు చేయడానికి మీకు ప్రస్తుతం ఏ ఆర్డర్ లేదు.", "en": "You have no active order to cancel."},
    "cancel_already": {"te": "ఆర్డర్ {order_id} ఇప్పటికే రద్దయింది.", "en": "Order {order_id} is already cancelled."},
    "cancel_stale": {"te": "ఈ అభ్యర్థన ఇప్పటికే పూర్తయింది. కొత్తగా మొదలుపెట్టడానికి 'మెనూ' అని పంపండి.", "en": "That request is already finished. Send 'menu' to start again."},
    "cancel_gateway_wait": {"te": "మీ చెల్లింపును ఇప్పుడు సరిచూడలేకపోయాము. కొద్దిసేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.", "en": "We could not check your payment right now. Please try again in a little while."},
    "pay_refund_started": {"te": "ఆర్డర్ {order_id} కోసం మీరు చెల్లించిన ₹{amount} వెనక్కి ఇవ్వడం మొదలుపెట్టాము. రీఫండ్ నిర్ధారణ అయ్యాక మీకు తెలియజేస్తాము.",
                           "en": "We have started refunding the ₹{amount} you paid for order {order_id}. We will message you when the refund is confirmed."},
    "already_placed": {"te": "ఈ ఆర్డర్ ఇప్పటికే నమోదైంది. ఆర్డర్ నంబర్: {order_id}",
                       "en": "This order has already been placed. Order ID: {order_id}"},
    "draft_cancelled": {"te": "మీ ఆర్డర్ రద్దు చేయబడింది. మళ్ళీ ఆర్డర్ చేయాలంటే 'మెనూ' అని పంపండి.",
                        "en": "Your order has been cancelled. Send 'menu' whenever you want to order again."},
    "not_accepting": {"te": "క్షమించండి, ప్రస్తుతం కొత్త ఆర్డర్లు తీసుకోవడం లేదు. త్వరలో మళ్ళీ ప్రారంభిస్తాము.",
                      "en": "Sorry, we are not taking new orders right now. We will start again soon."},
    "err_product": {"te": "క్షమించండి, ఈ రకం ఇప్పుడు అందుబాటులో లేదు. దయచేసి వేరే రకం ఎంచుకోండి.",
                    "en": "Sorry, this variety is no longer available. Please choose another."},
    "err_date": {"te": "క్షమించండి, ఈ తేదీకి ఇప్పుడు డెలివరీ సాధ్యం కాదు. దయచేసి వేరే తేదీ ఎంచుకోండి.",
                 "en": "Sorry, that delivery date is no longer available. Please choose another date."},
    "err_slot": {"te": "క్షమించండి, ఈ సమయం నిండిపోయింది. దయచేసి వేరే సమయం ఎంచుకోండి.",
                 "en": "Sorry, that time slot is full. Please choose another."},
    "err_area": {"te": "క్షమించండి, ఈ ఊరికి ఇప్పుడు డెలివరీ లేదు. దయచేసి వేరే ఊరు ఎంచుకోండి.",
                 "en": "Sorry, we don't deliver to this village right now. Please choose another."},
    "err_generic": {"te": "క్షమించండి, ఏదో సమస్య వచ్చింది. దయచేసి కొంచెం సేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.",
                    "en": "Sorry, something went wrong. Please try again in a little while."},
    "unexpected": {"te": "క్షమించండి, అర్థం కాలేదు. కింద ఉన్న ఎంపికల్లో ఒకటి ఎంచుకోండి.",
                   "en": "Sorry, I didn't understand that. Please choose one of the options below."},
    "unsupported": {"te": "క్షమించండి, ఈ రకం సందేశాన్ని ఇప్పుడు చదవలేను. దయచేసి టెక్స్ట్ లేదా బటన్లు వాడండి.",
                    "en": "Sorry, I can't read this type of message yet. Please use text or the buttons."},
    "session_expired": {"te": "చాలా సేపు సమాధానం లేనందున ఆర్డర్ మొదటి నుండి మొదలుపెడదాం.",
                        "en": "It has been a while, so let's start again from the beginning."},
    "human_support": {"te": "మీ సందేశం మా బృందానికి చేరింది. వాళ్ళు త్వరలో మిమ్మల్ని సంప్రదిస్తారు. మెనూకు వెళ్ళడానికి 'మెనూ' అని పంపండి.",
                      "en": "Your message has reached our team. They will contact you soon. Send 'menu' to go back to the main menu."},
    "cancel_order_ok": {"te": "ఆర్డర్ {order_id} రద్దు చేయబడింది.", "en": "Order {order_id} has been cancelled."},
    "cancel_order_not_found": {"te": "ఈ నంబర్‌తో ఆర్డర్ కనబడలేదు.", "en": "We could not find that order for your number."},
    "cancel_order_too_late": {"te": "ఆర్డర్ {order_id} ఇప్పటికే మా వద్ద అంగీకరించబడింది, కాబట్టి ఇక్కడ రద్దు చేయలేము. దయచేసి మమ్మల్ని సంప్రదించండి.",
                              "en": "Order {order_id} has already moved ahead, so it can't be cancelled here. Please contact us."},
    # --- order notifications ---
    "n_ACCEPTED": {"te": "ధన్యవాదాలు! మీ {farm} ఆర్డర్ {order_id} నిర్ధారించబడింది. మా కుటుంబ సభ్యులు మీ ఆర్డర్‌ను సిద్ధం చేసి త్వరలో పంపుతారు. ఎప్పటికప్పుడు మీకు తెలియజేస్తాము.",
                   "en": "Thank you! Your {farm} order {order_id} has been confirmed successfully. Our family will prepare your order and dispatch it soon. We will keep you updated."},
    "n_REJECTED": {"te": "నమస్కారం {name} గారు! క్షమించండి, మీ ఆర్డర్ {order_id} తీసుకోలేకపోయాము. కారణం: {reason}",
                   "en": "Hello {name}! Sorry, we could not take your order {order_id}. Reason: {reason}"},
    "n_DISPATCHED": {"te": "నమస్కారం {name} గారు! మీ ఆర్డర్ {order_id} బయలుదేరింది. త్వరలో మీ ఇంటికి చేరుతుంది.",
                     "en": "Hello {name}! Your order {order_id} is on its way and will reach you soon."},
    "n_DELIVERED": {"te": "నమస్కారం {name} గారు! మీ ఆర్డర్ {order_id} డెలివరీ అయింది. మా తోట మామిడి కాయలు ఎలా ఉన్నాయో చెప్పండి. ధన్యవాదాలు!",
                    "en": "Hello {name}! Your order {order_id} has been delivered. Thank you for choosing our farm!"},
    "n_CANCELLED": {"te": "నమస్కారం {name} గారు! మీ ఆర్డర్ {order_id} రద్దు చేయబడింది. {reason}",
                    "en": "Hello {name}! Your order {order_id} has been cancelled. {reason}"},
    "n_DATE_CHANGED": {"te": "నమస్కారం {name} గారు! మీ ఆర్డర్ {order_id} డెలివరీ తేదీ {date} కి మార్చబడింది.",
                       "en": "Hello {name}! The delivery date of your order {order_id} has been changed to {date}."},
}

GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "helo", "namaste", "namaskaram", "నమస్కారం", "నమస్తే", "హాయ్", "హలో"}
CMD_BACK = {"back", "వెనక్కి", "వెనుకకు", "వెనకకి"}
CMD_CANCEL = {"cancel", "రద్దు", "రద్దు చేయి", "క్యాన్సెల్"}
CMD_MENU = {"menu", "మెనూ", "main menu", "home"}
CMD_LANG = {"change language", "language", "భాష మార్చండి", "భాష", "bhasha"}
CMD_RESTART = {"restart", "start over", "మళ్ళీ మొదలు"}


def t(key: str, lang: str | None, **kw) -> str:
    entry = T[key]
    text = entry.get(lang or "en") or entry["en"]
    return text.format(**kw) if kw else text
