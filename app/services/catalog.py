from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Inventory, Product


def active_products(db: Session) -> list[Product]:
    return list(db.scalars(select(Product).where(Product.is_active.is_(True))
                           .options(selectinload(Product.inventory))
                           .order_by(Product.sort_order, Product.id)))


def orderable_products(db: Session) -> list[Product]:
    return [p for p in active_products(db) if p.available_qty >= Decimal(p.min_qty)]


def validate_quantity(product: Product, qty: Decimal) -> tuple[str, dict] | None:
    """Return (error_code, context) or None if the quantity is acceptable."""
    if qty <= 0:
        return "invalid", {}
    if not product.allow_decimal and qty != qty.to_integral_value():
        return "no_decimal", {}
    if qty < Decimal(product.min_qty):
        return "too_low", {"min": Decimal(product.min_qty)}
    if product.max_qty is not None and qty > Decimal(product.max_qty):
        return "too_high", {"max": Decimal(product.max_qty)}
    if qty > product.available_qty:
        return "no_stock", {"avail": max(product.available_qty, Decimal("0"))}
    return None


def create_product(db: Session, *, stock: Decimal = Decimal("0"), **fields) -> Product:
    p = Product(**fields)
    p.inventory = Inventory(quantity_on_hand=stock, quantity_reserved=Decimal("0"))
    db.add(p)
    return p


def set_stock(product: Product, on_hand: Decimal) -> None:
    if product.inventory is None:
        product.inventory = Inventory(quantity_on_hand=on_hand, quantity_reserved=Decimal("0"))
    else:
        if on_hand < Decimal(product.inventory.quantity_reserved):
            raise ValueError("Stock cannot be lower than the quantity already reserved by open orders.")
        product.inventory.quantity_on_hand = on_hand
