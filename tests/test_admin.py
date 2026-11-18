import re

import pytest
from sqlalchemy import select

from app.models import AuditLog, Inventory, MessageLog, Order, Product
from tests.conftest import Chat, drive_to_review

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def token(client, url="/admin/login"):
    html = client.get(url).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, password="test-password-123"):
    return client.post("/admin/login", data={"username": "admin", "password": password, "csrf_token": token(client)},
                       follow_redirects=False)


@pytest.fixture
def admin(client):
    assert login(client).status_code == 303
    return client


def post(client, url, page="/admin", **data):
    return client.post(url, data={"csrf_token": token(client, page), **data}, follow_redirects=False)


@pytest.fixture
def placed(db):
    c = Chat(db)
    drive_to_review(c).pick("confirm_order")
    db.expire_all()
    return db.scalar(select(Order))


def test_pages_require_login(client):
    for url in ["/admin", "/admin/orders", "/admin/products", "/admin/customers", "/admin/settings",
                "/admin/delivery", "/admin/audit", "/admin/orders/export.csv"]:
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", url
    assert client.get("/api/admin/orders", follow_redirects=False).status_code == 303
    assert client.get("/api/admin/customers", follow_redirects=False).status_code == 303


def test_login_wrong_password_and_rate_limit(client):
    assert login(client, "bad").headers["location"] == "/admin/login"
    for _ in range(10):
        r = client.post("/admin/login", data={"username": "admin", "password": "x", "csrf_token": token(client)})
    assert r.status_code == 429


def test_login_requires_csrf(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "test-password-123"})
    assert r.status_code == 403


def test_post_without_csrf_rejected(admin):
    assert admin.post("/admin/settings", data={"farm_name": "x"}).status_code == 403


def test_password_is_hashed(db):
    from app.models import AdminUser
    h = db.scalar(select(AdminUser.password_hash))
    assert h.startswith("$2") and "test-password-123" not in h


def test_overview_counts(admin, placed):
    html = admin.get("/admin").text
    assert "Total orders" in html and "Awaiting Confirmation" in html and "Orders today" in html


def test_accept_and_dispatch_notify_customer(admin, db, placed):
    r = post(admin, f"/admin/orders/{placed.id}/status", f"/admin/orders/{placed.id}", new_status="ACCEPTED")
    assert r.status_code == 303
    post(admin, f"/admin/orders/{placed.id}/status", f"/admin/orders/{placed.id}", new_status="DISPATCHED")
    db.expire_all()
    assert db.scalar(select(Order.status)) == "DISPATCHED"
    bodies = [m.content["body"] for m in db.scalars(select(MessageLog).where(MessageLog.direction == "OUT"))]
    assert any("confirmed successfully" in b for b in bodies) and any("on its way" in b for b in bodies)
    assert db.scalar(select(AuditLog).where(AuditLog.action == "order_status"))


def test_reject_requires_reason_then_releases_stock(admin, db, placed):
    url, page = f"/admin/orders/{placed.id}/status", f"/admin/orders/{placed.id}"
    post(admin, url, page, new_status="REJECTED", reason="")
    db.expire_all()
    assert db.scalar(select(Order.status)) == "PENDING"
    post(admin, url, page, new_status="REJECTED", reason="Not enough stock today")
    db.expire_all()
    assert db.scalar(select(Order.status)) == "REJECTED"
    assert db.scalar(select(Inventory.quantity_reserved).where(Inventory.product_id == 1)) == 0
    assert any("Not enough stock today" in m.content["body"] for m in db.scalars(select(MessageLog)) if m.direction == "OUT")


def test_invalid_transition_blocked_via_http(admin, db, placed):
    post(admin, f"/admin/orders/{placed.id}/status", f"/admin/orders/{placed.id}", new_status="DELIVERED")
    db.expire_all()
    assert db.scalar(select(Order.status)) == "PENDING"


def test_search_filter_and_csv_export(admin, placed):
    assert placed.order_code in admin.get("/admin/orders", params={"q": placed.order_code}).text
    assert placed.order_code in admin.get("/admin/orders", params={"q": "9000000001"}).text
    assert placed.order_code not in admin.get("/admin/orders", params={"status": "DELIVERED"}).text
    assert placed.order_code in admin.get("/admin/orders", params={"view": "new"}).text
    csv = admin.get("/admin/orders/export.csv")
    assert csv.headers["content-type"].startswith("text/csv") and placed.order_code in csv.text


def test_csv_formula_injection_neutralised(admin, db, placed):
    placed.customer_name = "=HYPERLINK(\"http://evil\")"
    db.commit()
    assert "'=HYPERLINK" in admin.get("/admin/orders/export.csv").text


def test_change_delivery_date_from_admin(admin, db, placed):
    from app.models import DeliveryDate
    target = db.scalars(select(DeliveryDate).order_by(DeliveryDate.date)).all()[4]
    post(admin, f"/admin/orders/{placed.id}/date", f"/admin/orders/{placed.id}", date=target.date.isoformat())
    db.expire_all()
    assert db.scalar(select(Order.delivery_date)) == target.date


def test_product_create_edit_with_image(admin, db):
    page = "/admin/products/new"
    r = admin.post("/admin/products/save", data={
        "csrf_token": token(admin, page), "name_en": "Banginapalli", "name_te": "బంగినపల్లి", "price_per_kg": "80",
        "min_qty": "1", "max_qty": "20", "stock_on_hand": "40", "description_en": "Sweet", "description_te": "తీపి",
        "is_active": "on", "allow_decimal": "on"}, files={"image": ("m.png", PNG, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    p = db.scalar(select(Product).where(Product.name_en == "Banginapalli"))
    assert p.price_per_kg == 80 and p.inventory.quantity_on_hand == 40 and p.image_path.endswith(".png")
    assert admin.get(f"/media/{p.image_path}").status_code == 200
    # price change and stock update
    admin.post("/admin/products/save", data={"csrf_token": token(admin, f"/admin/products/{p.id}"), "id": p.id,
               "name_en": "Banginapalli", "name_te": "బంగినపల్లి", "price_per_kg": "95", "min_qty": "1",
               "stock_on_hand": "10", "is_active": "on"}, follow_redirects=False)
    db.expire_all()
    p = db.get(Product, p.id)
    assert p.price_per_kg == 95 and p.inventory.quantity_on_hand == 10 and p.max_qty is None


def test_non_image_upload_rejected(admin, db):
    admin.post("/admin/products/save", data={"csrf_token": token(admin, "/admin/products/new"), "name_en": "X",
               "name_te": "ఎక్స్", "price_per_kg": "5", "min_qty": "1", "stock_on_hand": "1"},
               files={"image": ("evil.png", b"<script>alert(1)</script>", "image/png")}, follow_redirects=False)
    assert db.scalar(select(Product).where(Product.name_en == "X")) is None


def test_stock_cannot_drop_below_reserved(admin, db, placed):
    r = admin.post("/admin/products/save", data={"csrf_token": token(admin, "/admin/products/1"), "id": 1,
                   "name_en": "A", "name_te": "ఎ", "price_per_kg": "60", "min_qty": "1", "stock_on_hand": "1"},
                   follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(Inventory.quantity_on_hand).where(Inventory.product_id == 1)) == 200


def test_delivery_management(admin, db):
    from app.models import DeliveryArea, DeliveryDate
    from datetime import timedelta
    from app.utils import today_ist
    start = today_ist() + timedelta(days=20)
    post(admin, "/admin/delivery/dates", "/admin/delivery", start=start.isoformat(),
         end=(start + timedelta(days=2)).isoformat(), max_orders="5")
    assert len(db.scalars(select(DeliveryDate).where(DeliveryDate.date >= start)).all()) == 3
    post(admin, "/admin/delivery/areas", "/admin/delivery", name_en="Mulugu", name_te="ములుగు", delivery_charge="50")
    a = db.scalar(select(DeliveryArea).where(DeliveryArea.name_en == "Mulugu"))
    assert a.delivery_charge == 50
    post(admin, "/admin/delivery/dates", "/admin/delivery", start="2000-01-01", max_orders="5")
    assert db.scalar(select(DeliveryDate).where(DeliveryDate.date.__lt__(today_ist()))) is None


def test_customers_and_settings(admin, db, placed):
    assert "Ravi Kumar" in admin.get("/admin/customers").text
    assert placed.order_code in admin.get(f"/admin/customers/{placed.customer_id}").text
    post(admin, "/admin/settings", "/admin/settings", farm_name="Our Farm", contact_number="9876543210",
         business_address="A", business_hours="B", default_language="te", admin_notify_phone="")
    assert "Our Farm" in admin.get("/admin/settings").text
    # accepting_orders checkbox was omitted -> orders disabled
    c = Chat(db, "919000000044")
    drive_to_review(c).pick("confirm_order")
    assert "not taking new orders" in c.text


def test_public_apis_hide_customer_data(client, placed):
    assert client.get("/api/products").json()[0]["price_per_kg"] == "60.00"
    assert client.get("/api/delivery-dates").status_code == 200
    assert "9000000001" not in client.get("/api/delivery-areas").text
    assert client.get("/api/admin/orders", follow_redirects=False).status_code == 303


def test_admin_api_after_login(admin, placed):
    rows = admin.get("/api/admin/orders").json()
    assert rows[0]["order_id"] == placed.order_code


def test_health_and_headers(client):
    r = client.get("/health")
    assert r.json()["status"] == "ok" and r.headers["x-content-type-options"] == "nosniff"
