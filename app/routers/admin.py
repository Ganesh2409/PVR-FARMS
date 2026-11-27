"""Server-rendered administrator dashboard (session auth, CSRF on every POST, audit logging)."""
import csv
import io
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (ACCEPTED, ACTIVE_STATUSES, ALL_STATUSES, AWAITING_PAYMENT, CANCELLED, DELIVERED, DISPATCHED, PENDING, REJECTED,
                        AdminUser, AuditLog, Customer, DeliveryArea, DeliveryDate, DeliverySlot, Order, Payment, Product)
from app.security import (client_ip, csrf_token, hash_password, login_limiter, verify_csrf, verify_password)
from app.services import catalog, delivery, notifications, payments
from app.services.core import DEFAULT_SETTINGS, audit, get_all_settings, set_setting
from app.services.orders import VALID_TRANSITIONS, OrderError, change_delivery_date, change_status
from app.utils import fmt_date, normalize_indian_mobile, today_ist, utcnow
from app.web import get_admin, templates

router = APIRouter(prefix="/admin")
Admin = Depends(get_admin)
Csrf = Depends(verify_csrf)

ERRORS = {
    "invalid_transition": "That status change is not allowed from the current status.",
    "reason_required": "Please give a reason for rejecting the order.",
    "paid_meanwhile": "The customer has just paid for this order, so it was not cancelled. Re-open it and cancel again if you want to refund.",
    "date_unavailable": "That delivery date is not available (inactive, past, or full).",
    "slot_unavailable": "Please choose a valid delivery slot for that date.",
}
PAY_ERRORS = {
    "not_refundable": "Only a successful, not-yet-refunded payment can be refunded.",
    "gateway_error": "The payment gateway rejected or could not process that request. See the payment details below.",
}


# ------------------------------------------------------------------ helpers
def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, admin: AdminUser | None = None, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, f"admin/{name}", {
        "admin": admin, "csrf": csrf_token(request), "flashes": flashes, "statuses": ALL_STATUSES, **ctx})


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def dec(value: str, field: str, *, required=True, minimum: Decimal | None = Decimal("0")) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field} must be a number")
    if not d.is_finite() or (minimum is not None and d < minimum):
        raise ValueError(f"{field} must be a valid number (min {minimum})")
    return d


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Please choose a valid date")


MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


async def save_image(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    s = get_settings()
    data = await upload.read(s.max_upload_bytes + 1)
    if len(data) > s.max_upload_bytes:
        raise ValueError("Image is too large (max 3 MB)")
    ext = next((e for m, e in MAGIC.items() if data.startswith(m)), None)
    if ext is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    if ext is None:
        raise ValueError("Only JPG, PNG or WEBP images are allowed")
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (s.upload_dir / name).write_bytes(data)
    return name


# ------------------------------------------------------------------ auth
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    await verify_csrf(request)
    ip = client_ip(request)
    login_limiter.check(ip)
    form = await request.form()
    username, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
    admin = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if not admin or not admin.is_active or not verify_password(password, admin.password_hash):
        audit(db, username or "?", "login_failed", ip=ip)
        db.commit()
        flash(request, "Wrong username or password.", "err")
        return back("/admin/login")
    request.session.clear()
    request.session["admin_id"] = admin.id
    admin.last_login_at = utcnow()
    audit(db, admin.username, "login", ip=ip)
    db.commit()
    return back("/admin")


@router.post("/logout")
async def logout(request: Request, _=Csrf):
    request.session.clear()
    return back("/admin/login")


# ------------------------------------------------------------------ overview
@router.get("", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    counts = dict(db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status)).all())
    total_sales = db.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == DELIVERED))
    today = today_ist()
    from app.utils import IST
    start_utc = datetime.combine(today, datetime.min.time(), tzinfo=IST)
    orders_today = db.scalar(select(func.count(Order.id)).where(Order.created_at >= start_utc))
    low_stock = [p for p in catalog.active_products(db) if p.available_qty < Decimal(p.min_qty)]
    recent = db.scalars(select(Order).order_by(Order.id.desc()).limit(8)).all()
    stats = {
        "total": sum(counts.values()), "pending": counts.get(PENDING, 0), "accepted": counts.get(ACCEPTED, 0),
        "awaiting": counts.get(AWAITING_PAYMENT, 0),
        "refunds_pending": db.scalar(select(func.count(Payment.id)).join(Order, Order.id == Payment.order_id).where(
            Payment.status == "SUCCESS", Order.status.in_([CANCELLED, REJECTED]))) or 0,
        "cancel_requests": db.scalar(select(func.count(Order.id)).where(
            Order.status == DISPATCHED, Order.cancel_requested_at.is_not(None))) or 0,
        "delivered": counts.get(DELIVERED, 0), "cancelled": counts.get(CANCELLED, 0) + counts.get(REJECTED, 0),
        "sales": total_sales, "today": orders_today,
        "pending_deliveries": counts.get(ACCEPTED, 0) + counts.get(DISPATCHED, 0)}
    return render(request, "overview.html", admin, stats=stats, recent=recent, low_stock=low_stock, nav="overview")


# ------------------------------------------------------------------ products
@router.get("/products", response_class=HTMLResponse)
def products(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    rows = db.scalars(select(Product).options(selectinload(Product.inventory)).order_by(Product.sort_order, Product.id))
    return render(request, "products.html", admin, products=rows.all(), nav="products")


@router.get("/products/new", response_class=HTMLResponse)
def product_new(request: Request, admin: AdminUser = Admin):
    return render(request, "product_form.html", admin, product=None, nav="products")


@router.get("/products/{pid}", response_class=HTMLResponse)
def product_edit(pid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    p = db.get(Product, pid)
    if not p:
        flash(request, "Product not found.", "err")
        return back("/admin/products")
    return render(request, "product_form.html", admin, product=p, nav="products")


@router.post("/products/save")
async def product_save(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    pid = form.get("id")
    try:
        name_en, name_te = str(form.get("name_en", "")).strip(), str(form.get("name_te", "")).strip()
        if not name_en or not name_te:
            raise ValueError("Both English and Telugu names are required")
        fields = dict(
            name_en=name_en, name_te=name_te,
            description_en=str(form.get("description_en", "")).strip(),
            description_te=str(form.get("description_te", "")).strip(),
            price_per_kg=dec(form.get("price_per_kg"), "Price", minimum=Decimal("0.01")),
            min_qty=dec(form.get("min_qty"), "Minimum quantity", minimum=Decimal("0.001")),
            max_qty=dec(form.get("max_qty"), "Maximum quantity", required=False, minimum=Decimal("0.001")),
            allow_decimal=form.get("allow_decimal") == "on", is_active=form.get("is_active") == "on",
            sort_order=int(form.get("sort_order") or 0))
        if fields["max_qty"] is not None and fields["max_qty"] < fields["min_qty"]:
            raise ValueError("Maximum quantity cannot be below the minimum")
        stock = dec(form.get("stock_on_hand"), "Stock")
        image = await save_image(form.get("image"))
        if pid:
            p = db.get(Product, int(pid))
            if p is None:
                raise ValueError("Product not found")
            for k, v in fields.items():
                setattr(p, k, v)
            catalog.set_stock(p, stock)
            action = "product_update"
        else:
            p = catalog.create_product(db, stock=stock, **fields)
            action = "product_create"
        if image:
            p.image_path = image
        db.flush()
        audit(db, admin.username, action, "product", p.id, f"{p.name_en} price={p.price_per_kg} stock={stock}",
              client_ip(request))
        db.commit()
        flash(request, "Product saved.")
        return back("/admin/products")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
        return back(f"/admin/products/{pid}" if pid else "/admin/products/new")


@router.post("/products/{pid}/toggle")
def product_toggle(pid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    p = db.get(Product, pid)
    if p:
        p.is_active = not p.is_active
        audit(db, admin.username, "product_toggle", "product", pid, f"active={p.is_active}", client_ip(request))
        db.commit()
    return back("/admin/products")


# ------------------------------------------------------------------ orders
def _order_query(q: str, status: str, date_from: str, date_to: str, view: str):
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id.desc())
    if view == "new":  # "Awaiting Confirmation": COD orders to verify by phone + paid online orders to accept
        status = PENDING
    elif view == "confirmed":
        status = ACCEPTED
    elif view == "dispatched":
        status = DISPATCHED
    elif view in ("completed", "delivered"):
        status = DELIVERED
    elif view == "awaiting":  # online orders waiting for the customer's payment
        status = AWAITING_PAYMENT
    elif view == "pending_delivery":
        stmt = stmt.where(Order.status.in_([ACCEPTED, DISPATCHED]))
    elif view == "cancelled":
        stmt = stmt.where(Order.status.in_([CANCELLED, REJECTED]))
    elif view == "cancel_requests":  # customer asked to cancel an order that was already dispatched
        stmt = stmt.where(Order.status == DISPATCHED, Order.cancel_requested_at.is_not(None))
    elif view == "refund_pending":  # money collected and not (yet) confirmed refunded
        stmt = stmt.join(Payment, Payment.order_id == Order.id).where(
            Payment.status == "SUCCESS",
            (Payment.refund_status.in_(["PENDING", "PENDING_APPROVAL", "ONHOLD", "FAILED"]))
            | (Order.status.in_([CANCELLED, REJECTED])))
    elif view == "refunded":
        stmt = stmt.join(Payment, Payment.order_id == Order.id).where(Payment.status == "REFUNDED")
    if status in ALL_STATUSES:
        stmt = stmt.where(Order.status == status)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Order.order_code.ilike(like), Order.contact_phone.ilike(like),
                              Order.customer_name.ilike(like)))
    for val, op in ((date_from, "ge"), (date_to, "le")):
        try:
            d = date.fromisoformat(val)
            stmt = stmt.where(Order.delivery_date >= d if op == "ge" else Order.delivery_date <= d)
        except ValueError:
            pass
    return stmt


@router.get("/orders", response_class=HTMLResponse)
def orders(request: Request, q: str = "", status: str = "", date_from: str = "", date_to: str = "",
           view: str = "", db: Session = Depends(get_db), admin: AdminUser = Admin):
    rows = db.scalars(_order_query(q, status, date_from, date_to, view).limit(300)).all()
    return render(request, "orders.html", admin, orders=rows, q=q, status=status, date_from=date_from,
                  date_to=date_to, view=view, nav="orders")


def _safe_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/orders/export.csv")
def orders_export(request: Request, q: str = "", status: str = "", date_from: str = "", date_to: str = "",
                  view: str = "", db: Session = Depends(get_db), admin: AdminUser = Admin):
    rows = db.scalars(_order_query(q, status, date_from, date_to, view)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order ID", "Status", "Customer", "Phone", "Village", "Address", "Landmark", "Product", "Qty (kg)",
                "Subtotal", "Delivery", "Total", "Delivery date", "Slot", "Payment", "Created (UTC)"])
    for o in rows:
        it = o.items[0] if o.items else None
        w.writerow([_safe_cell(x) for x in (
            o.order_code, o.status, o.customer_name, o.contact_phone, o.village_name, o.address, o.landmark,
            it.product_name_en if it else "", it.quantity.normalize() if it else "", o.subtotal, o.delivery_charge,
            o.total, fmt_date(o.delivery_date), o.slot_name, o.payment_method, o.created_at.isoformat())])
    audit(db, admin.username, "orders_export", details=f"{len(rows)} rows", ip=client_ip(request))
    db.commit()
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="mango-farm-orders.csv"'})


def _get_order(db: Session, oid: int) -> Order | None:
    return db.scalar(select(Order).where(Order.id == oid).options(selectinload(Order.items),
                                                                  selectinload(Order.customer)))


@router.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail(oid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    o = _get_order(db, oid)
    if not o:
        flash(request, "Order not found.", "err")
        return back("/admin/orders")
    dates = delivery.available_dates(db, limit=60)
    slots = db.scalars(select(DeliverySlot).where(DeliverySlot.is_active.is_(True))).all()
    history = db.scalars(select(AuditLog).where(AuditLog.entity == "order", AuditLog.entity_id == o.order_code)
                         .order_by(AuditLog.id.desc()).limit(50)).all()
    return render(request, "order_detail.html", admin, o=o, next_statuses=sorted(VALID_TRANSITIONS[o.status]),
                  dates=dates, slots=slots, history=history, nav="orders")


@router.post("/orders/{oid}/status")
async def order_status(oid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    o = _get_order(db, oid)
    new, reason = str(form.get("new_status", "")), str(form.get("reason", "")).strip()
    try:
        if o is None:
            raise OrderError("not_found")
        if o.status == AWAITING_PAYMENT and new == CANCELLED:
            # unpaid online order: verify with the gateway, terminate the gateway order, release the stock
            if payments.cancel_checkout(db, o.id, actor=admin.username, cancelled_by="ADMIN") == "paid":
                raise OrderError("paid_meanwhile")
            db.refresh(o)
        else:
            change_status(db, o, new, reason, actor=admin.username, cancelled_by="ADMIN")  # audits + row-locks
        result = notifications.notify_customer(db, o, new, reason or None)
        refund_msg = ""
        if new in (REJECTED, CANCELLED) and o.payment is not None and o.payment.status == "SUCCESS":
            payments.refund_if_paid(db, o, f"Order {new.lower()}: {reason or 'by admin'}"[:100], admin.username)
            db.refresh(o.payment)
            refund_msg = f" Refund requested (gateway status: {o.payment.refund_status})."
            if o.payment.status == "REFUNDED":
                notifications.notify_refunded(db, o.id)
            else:
                notifications.notify_refund_started(db, o.id)
        flash(request, f"Order {o.order_code} is now {new}. Customer notification: {result}.{refund_msg}")
    except OrderError as e:
        flash(request, ERRORS.get(e.code, "Could not update the order."), "err")
    return back(f"/admin/orders/{oid}")


@router.post("/orders/{oid}/date")
async def order_date(oid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    o = _get_order(db, oid)
    try:
        d = parse_date(str(form.get("date", "")))
        slot_id = int(form["slot_id"]) if form.get("slot_id") else None
        change_delivery_date(db, o, d, slot_id)
        audit(db, admin.username, "order_date", "order", o.order_code, f"-> {d}", client_ip(request))
        db.commit()
        result = notifications.notify_customer(db, o, "DATE_CHANGED")
        flash(request, f"Delivery date changed. Customer notification: {result}.")
    except OrderError as e:
        flash(request, ERRORS.get(e.code, "Could not change the date."), "err")
    except ValueError as e:
        flash(request, str(e), "err")
    return back(f"/admin/orders/{oid}")


@router.post("/orders/{oid}/payment/refresh")
async def payment_refresh(oid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    o = _get_order(db, oid)
    if o is None or o.payment is None:
        flash(request, "No online payment on this order.", "err")
        return back(f"/admin/orders/{oid}")
    p = payments.sync_payment(db, o.payment.id)
    payments.sync_refund(db, p.id)
    audit(db, admin.username, "payment_refresh", "order", o.order_code, "", client_ip(request))
    db.commit()
    flash(request, "Payment status re-checked with the gateway.")
    return back(f"/admin/orders/{oid}")


@router.post("/orders/{oid}/payment/refund")
async def payment_refund(oid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    o = _get_order(db, oid)
    note = str(form.get("note", "")).strip() or "Refund requested by admin"
    try:
        if o is None or o.payment is None:
            raise payments.PaymentError("not_refundable", "no payment")
        p = payments.refund_payment(db, o.payment.id, note, admin.username)
        if p.status == "REFUNDED":
            notifications.notify_refunded(db, o.id)
        flash(request, f"Refund of ₹{p.refund_amount} requested: {p.refund_status}.")
    except payments.PaymentError as e:
        flash(request, PAY_ERRORS.get(e.code, "Refund failed.") + (f" ({e.detail[:120]})" if e.detail else ""), "err")
    return back(f"/admin/orders/{oid}")


# ------------------------------------------------------------------ delivery management
@router.get("/delivery", response_class=HTMLResponse)
def delivery_page(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    booked = dict(db.execute(select(Order.delivery_date, func.count(Order.id))
                             .where(Order.status.in_(ACTIVE_STATUSES)).group_by(Order.delivery_date)).all())
    dates = db.scalars(select(DeliveryDate).where(DeliveryDate.date >= today_ist() - timedelta(days=1))
                       .order_by(DeliveryDate.date)).all()
    return render(request, "delivery.html", admin, dates=dates, booked=booked, nav="delivery",
                  slots=db.scalars(select(DeliverySlot).order_by(DeliverySlot.sort_order)).all(),
                  areas=db.scalars(select(DeliveryArea).order_by(DeliveryArea.name_en)).all())


@router.post("/delivery/dates")
async def dates_add(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    try:
        start = parse_date(str(form.get("start", "")))
        end = parse_date(str(form.get("end") or form.get("start", "")))
        cap = int(form.get("max_orders") or 20)
        if end < start or (end - start).days > 120 or cap < 1:
            raise ValueError("Check the date range (max 120 days) and capacity")
        if start < today_ist():
            raise ValueError("Dates in the past cannot be added")
        added, d = 0, start
        while d <= end:
            if not db.scalar(select(DeliveryDate).where(DeliveryDate.date == d)):
                db.add(DeliveryDate(date=d, max_orders=cap, slots_enabled=form.get("slots_enabled") == "on"))
                added += 1
            d += timedelta(days=1)
        audit(db, admin.username, "dates_add", "delivery_date", "", f"{start}..{end} cap={cap}", client_ip(request))
        db.commit()
        flash(request, f"Added {added} delivery date(s).")
    except ValueError as e:
        flash(request, str(e), "err")
    return back("/admin/delivery")


@router.post("/delivery/dates/{did}")
async def date_update(did: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    dd = db.get(DeliveryDate, did)
    try:
        cap = int(form.get("max_orders") or 0)
        if dd is None or cap < 0:
            raise ValueError("Invalid capacity")
        dd.max_orders, dd.is_active = cap, form.get("is_active") == "on"
        dd.slots_enabled = form.get("slots_enabled") == "on"
        audit(db, admin.username, "date_update", "delivery_date", did, f"{dd.date} cap={cap} active={dd.is_active}",
              client_ip(request))
        db.commit()
        flash(request, "Delivery date updated.")
    except ValueError as e:
        flash(request, str(e), "err")
    return back("/admin/delivery")


@router.post("/delivery/slots")
async def slot_save(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    try:
        name_en, name_te = str(form.get("name_en", "")).strip(), str(form.get("name_te", "")).strip()
        if not name_en:
            raise ValueError("Slot name (English) is required")
        mx = int(form["max_orders"]) if form.get("max_orders") else None
        slot = db.get(DeliverySlot, int(form["id"])) if form.get("id") else DeliverySlot()
        slot.name_en, slot.name_te, slot.max_orders = name_en, name_te, mx
        slot.is_active = form.get("is_active") == "on" if form.get("id") else True
        slot.sort_order = int(form.get("sort_order") or 0)
        db.add(slot)
        audit(db, admin.username, "slot_save", "delivery_slot", form.get("id", "new"), name_en, client_ip(request))
        db.commit()
        flash(request, "Delivery slot saved.")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "err")
    return back("/admin/delivery")


@router.post("/delivery/areas")
async def area_save(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    try:
        name_en, name_te = str(form.get("name_en", "")).strip(), str(form.get("name_te", "")).strip()
        if not name_en:
            raise ValueError("Village name (English) is required")
        charge = dec(form.get("delivery_charge") or "0", "Delivery charge")
        area = db.get(DeliveryArea, int(form["id"])) if form.get("id") else DeliveryArea()
        area.name_en, area.name_te, area.delivery_charge = name_en, name_te, charge
        area.is_active = form.get("is_active") == "on" if form.get("id") else True
        db.add(area)
        db.flush()
        audit(db, admin.username, "area_save", "delivery_area", area.id, f"{name_en} charge={charge}",
              client_ip(request))
        db.commit()
        flash(request, "Village saved.")
    except Exception as e:
        db.rollback()
        flash(request, "That village name already exists." if "UNIQUE" in str(e).upper() else str(e), "err")
    return back("/admin/delivery")


# ------------------------------------------------------------------ customers
@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, q: str = "", db: Session = Depends(get_db), admin: AdminUser = Admin):
    stmt = select(Customer).options(selectinload(Customer.orders), selectinload(Customer.village)).order_by(Customer.id.desc())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.whatsapp_number.ilike(like)))
    return render(request, "customers.html", admin, customers=db.scalars(stmt.limit(300)).all(), q=q, nav="customers")


@router.post("/customers/{cid}/cod")
async def customer_cod(cid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    c = db.get(Customer, cid)
    if c:
        c.cod_allowed = form.get("cod_allowed") == "on"
        audit(db, admin.username, "customer_cod", "customer", cid, f"cod_allowed={c.cod_allowed}", client_ip(request))
        db.commit()
        flash(request, "Cash on Delivery permission updated. (It also needs the global switch in Settings.)")
    return back(f"/admin/customers/{cid}")


@router.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(cid: int, request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    c = db.scalar(select(Customer).where(Customer.id == cid).options(selectinload(Customer.orders)))
    if not c:
        return back("/admin/customers")
    return render(request, "customer_detail.html", admin, c=c, nav="customers")


# ------------------------------------------------------------------ settings, audit
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    s = get_settings()
    return render(request, "settings.html", admin, values=get_all_settings(db), nav="settings", pay={
        "provider": s.payment_provider, "env": s.cashfree_env, "configured": s.cashfree_configured,
        "version": s.cashfree_api_version or "2026-01-01 (default)", "expiry": s.payment_expiry_minutes,
        "webhook": f"{s.public_base_url or 'https://YOUR-DOMAIN'}/webhooks/cashfree"},
                  wa={"configured": s.whatsapp_configured, "verify_token": bool(s.whatsapp_verify_token),
                      "app_secret": bool(s.whatsapp_app_secret), "template": s.whatsapp_template_order_update,
                      "version": s.whatsapp_api_version, "public_url": s.public_base_url})


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    for key in DEFAULT_SETTINGS:
        if key in ("accepting_orders", "cod_enabled"):
            set_setting(db, key, "true" if form.get(key) == "on" else "false")
        elif key == "admin_notify_phone":
            raw = str(form.get(key, "")).strip()
            set_setting(db, key, (normalize_indian_mobile(raw) or raw) if raw else "")
        elif key == "default_language":
            set_setting(db, key, "en" if form.get(key) == "en" else "te")
        else:
            set_setting(db, key, str(form.get(key, "")).strip()[:300])
    audit(db, admin.username, "settings_update", "settings", "", ip=client_ip(request))
    db.commit()
    flash(request, "Settings saved.")
    return back("/admin/settings")


@router.post("/password")
async def change_password(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin, _=Csrf):
    form = await request.form()
    old, new = str(form.get("old_password", "")), str(form.get("new_password", ""))
    if not verify_password(old, admin.password_hash):
        flash(request, "Current password is wrong.", "err")
    elif len(new) < 10:
        flash(request, "New password must be at least 10 characters.", "err")
    else:
        admin.password_hash = hash_password(new)
        audit(db, admin.username, "password_change", ip=client_ip(request))
        db.commit()
        flash(request, "Password changed.")
    return back("/admin/settings")


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db), admin: AdminUser = Admin):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200)).all()
    return render(request, "audit.html", admin, rows=rows, nav="audit")

