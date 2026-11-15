"""Health check and read-only public catalogue APIs (no customer data)."""
from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminUser, Customer, Order
from app.services import catalog, delivery
from app.web import get_admin

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}


@router.get("/api/products")
def products(db: Session = Depends(get_db)):
    return [{"id": p.id, "name_en": p.name_en, "name_te": p.name_te, "description_en": p.description_en,
             "description_te": p.description_te, "image": f"/media/{p.image_path}" if p.image_path else None,
             "price_per_kg": str(p.price_per_kg), "min_qty": str(p.min_qty),
             "max_qty": str(p.max_qty) if p.max_qty is not None else None,
             "available_qty": str(p.available_qty), "allow_decimal": p.allow_decimal}
            for p in catalog.active_products(db)]


@router.get("/api/delivery-dates")
def delivery_dates(db: Session = Depends(get_db)):
    return [{"date": o.row.date.isoformat(), "remaining": o.remaining, "slots_enabled": o.row.slots_enabled}
            for o in delivery.available_dates(db, limit=60)]


@router.get("/api/delivery-areas")
def delivery_areas(db: Session = Depends(get_db)):
    return [{"id": a.id, "name_en": a.name_en, "name_te": a.name_te, "delivery_charge": str(a.delivery_charge)}
            for a in delivery.active_areas(db)]


# ---- administrator-only JSON (session cookie required) --------------------------------
@router.get("/api/admin/orders")
def admin_orders(status: str | None = None, limit: int = 100, db: Session = Depends(get_db),
                 _: AdminUser = Depends(get_admin)):
    q = select(Order).order_by(Order.id.desc()).limit(min(limit, 500))
    if status:
        q = q.where(Order.status == status.upper())
    return [{"order_id": o.order_code, "status": o.status, "customer": o.customer_name, "phone": o.contact_phone,
             "total": str(o.total), "delivery_date": o.delivery_date.isoformat(),
             "items": [{"name": i.product_name_en, "qty": str(i.quantity)} for i in o.items]}
            for o in db.scalars(q)]


@router.get("/api/admin/customers")
def admin_customers(db: Session = Depends(get_db), _: AdminUser = Depends(get_admin)):
    return [{"id": c.id, "phone": c.whatsapp_number, "name": c.name, "language": c.language,
             "orders": len(c.orders)} for c in db.scalars(select(Customer).order_by(Customer.id.desc()))]
