"""Initial data. Everything seeded here is editable in the admin dashboard."""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AdminUser, DeliveryArea, DeliveryDate, DeliverySlot, Product
from app.security import hash_password
from app.services.catalog import create_product
from app.utils import today_ist

log = logging.getLogger("mango.seed")


def ensure_admin(db: Session) -> None:
    s = get_settings()
    if db.scalar(select(func.count(AdminUser.id))):
        return
    password = s.admin_password
    if not password:
        if s.is_production:
            raise RuntimeError("ADMIN_PASSWORD must be set in production to create the first admin account.")
        password = secrets.token_urlsafe(9)
        log.warning("No ADMIN_PASSWORD set. Generated development password for '%s': %s", s.admin_username, password)
        print(f"\n*** Development admin login -> username: {s.admin_username}  password: {password} ***\n")
    db.add(AdminUser(username=s.admin_username, password_hash=hash_password(password)))
    db.commit()


def seed_catalog(db: Session) -> None:
    if not db.scalar(select(func.count(Product.id))):
        create_product(
            db, stock=Decimal("200"), name_en="Raw Mangoes (for eating)", name_te="తినడానికి పచ్చి మామిడికాయలు",
            description_en="Fresh, firm raw mangoes straight from our farm. Tangy and crunchy, great for eating with salt and chilli.",
            description_te="మా తోట నుండి నేరుగా తెచ్చిన తాజా పచ్చి మామిడికాయలు. పుల్లగా, కరకరలాడుతూ ఉప్పు కారంతో తినడానికి బాగుంటాయి.",
            price_per_kg=Decimal("60"), min_qty=Decimal("1"), max_qty=Decimal("50"), allow_decimal=True, sort_order=1)
        create_product(
            db, stock=Decimal("300"), name_en="Raw Mangoes (for pickle)", name_te="ఊరగాయకు పచ్చి మామిడికాయలు",
            description_en="Hard, sour raw mangoes ideal for making avakaya and other pickles.",
            description_te="ఆవకాయ, ఇతర ఊరగాయలు పెట్టడానికి బాగా సరిపోయే గట్టి, పుల్లటి పచ్చి మామిడికాయలు.",
            price_per_kg=Decimal("45"), min_qty=Decimal("2"), max_qty=Decimal("100"), allow_decimal=True, sort_order=2)
    if not db.scalar(select(func.count(DeliveryArea.id))):
        db.add(DeliveryArea(name_en="Govindaraopet", name_te="గోవిందరావుపేట", delivery_charge=Decimal("30")))
    if not db.scalar(select(func.count(DeliverySlot.id))):
        db.add(DeliverySlot(name_en="Morning (8 AM - 12 PM)", name_te="ఉదయం (8 - 12)", sort_order=1))
        db.add(DeliverySlot(name_en="Evening (4 PM - 7 PM)", name_te="సాయంత్రం (4 - 7)", sort_order=2))
    db.commit()


def seed_demo_dates(db: Session) -> None:
    """Development convenience: open the next 7 days for delivery (capacity 20)."""
    if db.scalar(select(func.count(DeliveryDate.id))):
        return
    start = today_ist()
    for i in range(1, 8):
        db.add(DeliveryDate(date=start + timedelta(days=i), max_orders=20))
    db.commit()


def run_seed(db: Session) -> None:
    ensure_admin(db)
    s = get_settings()
    if s.seed_demo_data:
        seed_catalog(db)
        if not s.is_production:
            seed_demo_dates(db)
