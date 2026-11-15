from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric,
                        String, Text, TypeDecorator, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils import utcnow


class UTCDateTime(TypeDecorator):
    """Always store UTC; always return timezone-aware UTC datetimes."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---- Order status constants -------------------------------------------------
AWAITING_PAYMENT = "AWAITING_PAYMENT"  # online order created, payment not yet verified (holds stock)
PENDING, ACCEPTED, REJECTED = "PENDING", "ACCEPTED", "REJECTED"
DISPATCHED, DELIVERED, CANCELLED = "DISPATCHED", "DELIVERED", "CANCELLED"
ALL_STATUSES = [AWAITING_PAYMENT, PENDING, ACCEPTED, REJECTED, DISPATCHED, DELIVERED, CANCELLED]
ACTIVE_STATUSES = [AWAITING_PAYMENT, PENDING, ACCEPTED, DISPATCHED]  # these hold capacity / stock

# Payment states
PAY_PENDING, PAY_SUCCESS, PAY_FAILED = "PENDING", "SUCCESS", "FAILED"
PAY_EXPIRED, PAY_REFUNDED = "EXPIRED", "REFUNDED"


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str | None] = mapped_column(String(2))  # 'te' | 'en'
    alt_phone: Mapped[str | None] = mapped_column(String(20))
    village_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_areas.id"))
    channel: Mapped[str] = mapped_column(String(12), default="whatsapp")  # whatsapp | simulator
    cod_allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # Cash on Delivery for selected customers
    orders: Mapped[list["Order"]] = relationship(back_populates="customer", order_by="Order.id.desc()")
    village: Mapped["DeliveryArea | None"] = relationship()


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_te: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str] = mapped_column(String(200))
    description_te: Mapped[str] = mapped_column(Text, default="")
    description_en: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(255))
    price_per_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    min_qty: Mapped[Decimal] = mapped_column(Numeric(8, 3), default=Decimal("1"))
    max_qty: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    allow_decimal: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    inventory: Mapped["Inventory | None"] = relationship(back_populates="product", uselist=False,
                                                         cascade="all, delete-orphan")

    def name(self, lang: str) -> str:
        return self.name_te if lang == "te" and self.name_te else self.name_en

    def description(self, lang: str) -> str:
        return self.description_te if lang == "te" and self.description_te else self.description_en

    @property
    def available_qty(self) -> Decimal:
        return self.inventory.available if self.inventory else Decimal("0")


class Inventory(Base):
    __tablename__ = "inventory"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    quantity_on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    quantity_reserved: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    product: Mapped[Product] = relationship(back_populates="inventory")

    @property
    def available(self) -> Decimal:
        return Decimal(self.quantity_on_hand) - Decimal(self.quantity_reserved)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="LANGUAGE_SELECTION")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    customer: Mapped[Customer] = relationship()


class DeliveryArea(TimestampMixin, Base):
    __tablename__ = "delivery_areas"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_en: Mapped[str] = mapped_column(String(120), unique=True)
    name_te: Mapped[str] = mapped_column(String(120), default="")
    delivery_charge: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    def name(self, lang: str) -> str:
        return self.name_te if lang == "te" and self.name_te else self.name_en


class DeliveryDate(TimestampMixin, Base):
    __tablename__ = "delivery_dates"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_orders: Mapped[int] = mapped_column(Integer, default=20)
    slots_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class DeliverySlot(TimestampMixin, Base):
    __tablename__ = "delivery_slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_en: Mapped[str] = mapped_column(String(80))
    name_te: Mapped[str] = mapped_column(String(80), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    max_orders: Mapped[int | None] = mapped_column(Integer)  # per date; None = unlimited

    def name(self, lang: str) -> str:
        return self.name_te if lang == "te" and self.name_te else self.name_en


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_date", "status", "delivery_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=PENDING, index=True)
    customer_name: Mapped[str] = mapped_column(String(120))
    contact_phone: Mapped[str] = mapped_column(String(20))
    alt_phone: Mapped[str | None] = mapped_column(String(20))
    area_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_areas.id"))
    village_name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(Text)
    landmark: Mapped[str | None] = mapped_column(String(200))
    delivery_date: Mapped[date] = mapped_column(Date, index=True)
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_slots.id"))
    slot_name: Mapped[str | None] = mapped_column(String(80))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    delivery_charge: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str] = mapped_column(String(20), default="ONLINE")  # ONLINE | COD
    payment_status: Mapped[str] = mapped_column(String(12), default="UNPAID")  # UNPAID | PAID | REFUNDED
    language: Mapped[str] = mapped_column(String(2), default="en")
    channel: Mapped[str] = mapped_column(String(12), default="whatsapp")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    reservations: Mapped[list["InventoryReservation"]] = relationship(back_populates="order")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order", order_by="Payment.id")

    @property
    def payment(self) -> "Payment | None":
        return self.payments[-1] if self.payments else None


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    product_name_en: Mapped[str] = mapped_column(String(200))  # snapshot
    product_name_te: Mapped[str] = mapped_column(String(200), default="")
    price_per_kg: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # snapshot
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    order: Mapped[Order] = relationship(back_populates="items")

    def name(self, lang: str) -> str:
        return self.product_name_te if lang == "te" and self.product_name_te else self.product_name_en


class InventoryReservation(TimestampMixin, Base):
    __tablename__ = "inventory_reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")  # ACTIVE | RELEASED | FULFILLED
    order: Mapped[Order] = relationship(back_populates="reservations")


class Payment(TimestampMixin, Base):
    """One payment checkout (gateway order) for an Order. Status is only ever changed by
    app.services.payments after the gateway itself has been asked (never from client input)."""
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(12))  # cashfree | mock
    provider_order_id: Mapped[str] = mapped_column(String(60), unique=True)  # our order_id sent to the gateway
    cf_order_id: Mapped[str | None] = mapped_column(String(40))
    payment_session_id: Mapped[str | None] = mapped_column(Text)
    checkout_token: Mapped[str] = mapped_column(String(64), unique=True)  # secret path of /pay/<token>
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(10), default=PAY_PENDING, index=True)
    gateway_order_status: Mapped[str | None] = mapped_column(String(30))  # ACTIVE/PAID/EXPIRED/...
    cf_payment_id: Mapped[str | None] = mapped_column(String(40))  # transaction reference
    bank_reference: Mapped[str | None] = mapped_column(String(80))
    payment_group: Mapped[str | None] = mapped_column(String(30))  # upi, credit_card, net_banking ...
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    closed_reason: Mapped[str | None] = mapped_column(String(30))  # EXPIRED | CANCELLED | FAILED | LATE_PAYMENT
    needs_attention: Mapped[str | None] = mapped_column(String(200))  # e.g. amount mismatch -> admin must look
    refund_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    refund_status: Mapped[str | None] = mapped_column(String(20))
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    refund_note: Mapped[str | None] = mapped_column(String(100))
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    order: Mapped[Order] = relationship(back_populates="payments")


class MessageLog(Base):
    __tablename__ = "message_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # IN | OUT
    channel: Mapped[str] = mapped_column(String(12))
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    wa_message_id: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_webhook_event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(20))  # message | status
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="RECEIVED")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_username: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(40), default="")
    details: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
