"""Payment gateway abstraction: Cashfree Payment Gateway (sandbox/production) and a dev-only mock.

Cashfree specifics follow the official docs (https://www.cashfree.com/docs):
  * Base URL  sandbox https://sandbox.cashfree.com/pg  |  production https://api.cashfree.com/pg
  * Headers   x-client-id, x-client-secret, x-api-version (default 2026-01-01), x-idempotency-key (optional UUID)
  * Create order  POST /orders  -> cf_order_id, payment_session_id, order_status
  * Verify        GET  /orders/{order_id} (order_status ACTIVE|PAID|EXPIRED|TERMINATED|TERMINATION_REQUESTED)
                  GET  /orders/{order_id}/payments (array; payment_status SUCCESS|FAILED|USER_DROPPED|...)
  * Terminate     PATCH /orders/{order_id} {"order_status": "TERMINATED"}
  * Refund        POST /orders/{order_id}/refunds {refund_amount, refund_id, refund_note, refund_speed}
                  GET  /orders/{order_id}/refunds/{refund_id}
  * Webhook signature  Base64(HMAC_SHA256(client_secret, x-webhook-timestamp + rawBody)) in x-webhook-signature
Secrets are only ever sent in request headers from this server and are never logged.
"""
import base64
import hashlib
import hmac
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.config import get_settings

log = logging.getLogger("mango.gateway")
DEFAULT_CASHFREE_API_VERSION = "2026-01-01"
MOCK_WEBHOOK_SECRET = "mock-webhook-secret-dev-only"


class GatewayError(Exception):
    """Gateway call failed. The message is safe to log (never contains credentials)."""


@dataclass
class GatewayOrder:
    cf_order_id: str
    payment_session_id: str
    order_status: str


@dataclass
class GatewayPayment:
    cf_payment_id: str
    status: str  # SUCCESS FAILED USER_DROPPED PENDING NOT_ATTEMPTED CANCELLED VOID
    amount: Decimal
    bank_reference: str | None = None
    payment_group: str | None = None
    message: str | None = None
    completed_at: datetime | None = None


@dataclass
class GatewayStatus:
    order_status: str  # ACTIVE PAID EXPIRED TERMINATED TERMINATION_REQUESTED
    order_amount: Decimal
    payments: list[GatewayPayment] = field(default_factory=list)


@dataclass
class GatewayRefund:
    refund_id: str
    status: str  # SUCCESS PENDING PENDING_APPROVAL CANCELLED ONHOLD REJECTED
    cf_refund_id: str | None = None


class PaymentGateway(ABC):
    name: str

    @abstractmethod
    def create_order(self, *, provider_order_id: str, amount: Decimal, customer_id: str, customer_phone: str,
                     customer_name: str | None, return_url: str, notify_url: str, expires_at: datetime,
                     note: str) -> GatewayOrder: ...

    @abstractmethod
    def get_status(self, provider_order_id: str) -> GatewayStatus: ...

    @abstractmethod
    def terminate_order(self, provider_order_id: str) -> None: ...

    @abstractmethod
    def create_refund(self, provider_order_id: str, refund_id: str, amount: Decimal, note: str) -> GatewayRefund: ...

    @abstractmethod
    def get_refund(self, provider_order_id: str, refund_id: str) -> GatewayRefund: ...

    @abstractmethod
    def verify_webhook_signature(self, raw_body: bytes, signature: str | None, timestamp: str | None) -> bool: ...

    @property
    def checkout_mode(self) -> str:
        return "sandbox"


def webhook_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Cashfree: Base64(HMAC-SHA256(secret, timestamp + rawBody)) with no separator."""
    return base64.b64encode(hmac.new(secret.encode(), timestamp.encode() + raw_body, hashlib.sha256).digest()).decode()


def _dec(v) -> Decimal:
    return Decimal(str(v if v is not None else 0)).quantize(Decimal("0.01"))


def _parse_time(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ===================================================================== Cashfree
class CashfreeGateway(PaymentGateway):
    name = "cashfree"

    def __init__(self):
        s = get_settings()
        if not s.cashfree_configured:
            raise GatewayError("Cashfree is not configured (set CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET)")
        self.s = s
        self.base = s.cashfree_base_url
        self.version = s.cashfree_api_version or DEFAULT_CASHFREE_API_VERSION

    @property
    def checkout_mode(self) -> str:
        return "production" if self.s.cashfree_env.lower() == "production" else "sandbox"

    def _request(self, method: str, path: str, *, json: dict | None = None, idempotent: bool = False):
        headers = {"x-client-id": self.s.cashfree_client_id, "x-client-secret": self.s.cashfree_client_secret,
                   "x-api-version": self.version, "accept": "application/json"}
        if idempotent:
            headers["x-idempotency-key"] = str(uuid.uuid4())
        try:
            r = httpx.request(method, self.base + path, json=json, headers=headers, timeout=20)
        except httpx.HTTPError as exc:
            raise GatewayError(f"Cashfree network error: {type(exc).__name__}") from exc
        if r.status_code >= 300:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = ""
            raise GatewayError(f"Cashfree HTTP {r.status_code} on {method} {path}: {msg[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise GatewayError("Cashfree returned a non-JSON response") from exc

    def create_order(self, *, provider_order_id, amount, customer_id, customer_phone, customer_name, return_url,
                     notify_url, expires_at, note) -> GatewayOrder:
        body = {
            "order_id": provider_order_id,
            "order_amount": float(amount),  # API takes a number with up to two decimals
            "order_currency": "INR",
            "customer_details": {"customer_id": customer_id, "customer_phone": customer_phone,
                                 **({"customer_name": customer_name} if customer_name else {})},
            "order_meta": {"return_url": return_url, "notify_url": notify_url},
            "order_expiry_time": expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order_note": note[:200],
        }
        d = self._request("POST", "/orders", json=body, idempotent=True)
        if not d.get("payment_session_id"):
            raise GatewayError("Cashfree response had no payment_session_id")
        return GatewayOrder(str(d.get("cf_order_id", "")), d["payment_session_id"], d.get("order_status", "ACTIVE"))

    def get_status(self, provider_order_id: str) -> GatewayStatus:
        o = self._request("GET", f"/orders/{provider_order_id}")
        pays = self._request("GET", f"/orders/{provider_order_id}/payments")
        payments = [GatewayPayment(
            cf_payment_id=str(p.get("cf_payment_id", "")), status=p.get("payment_status", ""),
            amount=_dec(p.get("payment_amount")), bank_reference=p.get("bank_reference"),
            payment_group=p.get("payment_group"), message=p.get("payment_message"),
            completed_at=_parse_time(p.get("payment_completion_time") or p.get("payment_time")))
            for p in (pays if isinstance(pays, list) else [])]
        return GatewayStatus(o.get("order_status", ""), _dec(o.get("order_amount")), payments)

    def terminate_order(self, provider_order_id: str) -> None:
        self._request("PATCH", f"/orders/{provider_order_id}", json={"order_status": "TERMINATED"}, idempotent=True)

    def create_refund(self, provider_order_id, refund_id, amount, note) -> GatewayRefund:
        d = self._request("POST", f"/orders/{provider_order_id}/refunds", idempotent=True, json={
            "refund_amount": float(amount), "refund_id": refund_id, "refund_note": note[:100],
            "refund_speed": "STANDARD"})
        return GatewayRefund(d.get("refund_id", refund_id), d.get("refund_status", "PENDING"),
                             str(d.get("cf_refund_id", "")) or None)

    def get_refund(self, provider_order_id, refund_id) -> GatewayRefund:
        d = self._request("GET", f"/orders/{provider_order_id}/refunds/{refund_id}")
        return GatewayRefund(d.get("refund_id", refund_id), d.get("refund_status", "PENDING"),
                             str(d.get("cf_refund_id", "")) or None)

    def verify_webhook_signature(self, raw_body, signature, timestamp) -> bool:
        if not signature or not timestamp:
            return False
        expected = webhook_signature(self.s.cashfree_client_secret, timestamp, raw_body)
        return hmac.compare_digest(expected, signature)


# ===================================================================== mock (development / tests only)
class MockGateway(PaymentGateway):
    """In-memory fake gateway. Refused in production. Outcomes are driven by simulate()."""
    name = "mock"
    ORDERS: dict[str, dict] = {}
    REFUNDS: dict[str, dict] = {}
    refund_outcome = "SUCCESS"  # tests may set PENDING
    fail_create = False

    @classmethod
    def reset(cls):
        cls.ORDERS.clear()
        cls.REFUNDS.clear()
        cls.refund_outcome, cls.fail_create = "SUCCESS", False

    def create_order(self, *, provider_order_id, amount, customer_id, customer_phone, customer_name, return_url,
                     notify_url, expires_at, note) -> GatewayOrder:
        if self.fail_create:
            raise GatewayError("mock gateway unavailable")
        self.ORDERS[provider_order_id] = {"amount": amount, "status": "ACTIVE", "payments": [], "terminated": False}
        return GatewayOrder(f"mock-{len(self.ORDERS)}", f"mock_session_{uuid.uuid4().hex}", "ACTIVE")

    @classmethod
    def simulate(cls, provider_order_id: str, outcome: str, amount: Decimal | None = None) -> None:
        o = cls.ORDERS[provider_order_id]
        pid = f"mockpay{len(o['payments']) + 1}"
        if outcome == "success":
            o["payments"].append(GatewayPayment(pid, "SUCCESS", amount if amount is not None else o["amount"],
                                                f"BANKREF{pid}", "upi", "Transaction success"))
            o["status"] = "PAID"
        elif outcome == "failed":
            o["payments"].append(GatewayPayment(pid, "FAILED", o["amount"], None, "upi", "Payment declined"))
        elif outcome == "dropped":
            o["payments"].append(GatewayPayment(pid, "USER_DROPPED", o["amount"], None, "upi", "User dropped"))
        elif outcome == "expired":
            o["status"] = "EXPIRED"
        else:
            raise ValueError(outcome)

    def get_status(self, provider_order_id: str) -> GatewayStatus:
        o = self.ORDERS.get(provider_order_id)
        if o is None:
            raise GatewayError("mock: unknown order")
        return GatewayStatus(o["status"], o["amount"], list(o["payments"]))

    def terminate_order(self, provider_order_id: str) -> None:
        o = self.ORDERS.get(provider_order_id)
        if o and o["status"] == "ACTIVE":
            o["status"], o["terminated"] = "TERMINATED", True

    def create_refund(self, provider_order_id, refund_id, amount, note) -> GatewayRefund:
        self.REFUNDS[refund_id] = {"status": self.refund_outcome, "amount": amount}
        return GatewayRefund(refund_id, self.refund_outcome, f"mockrf-{refund_id}")

    def get_refund(self, provider_order_id, refund_id) -> GatewayRefund:
        return GatewayRefund(refund_id, self.REFUNDS[refund_id]["status"])

    def verify_webhook_signature(self, raw_body, signature, timestamp) -> bool:
        if not signature or not timestamp:
            return False
        return hmac.compare_digest(webhook_signature(MOCK_WEBHOOK_SECRET, timestamp, raw_body), signature)


_override: PaymentGateway | None = None


def set_gateway_override(gw: PaymentGateway | None) -> None:
    global _override
    _override = gw


def get_gateway() -> PaymentGateway:
    if _override is not None:
        return _override
    s = get_settings()
    if s.payment_provider == "mock":
        if s.is_production:
            raise GatewayError("The mock payment provider is not allowed in production")
        return MockGateway()
    if s.payment_provider == "cashfree":
        return CashfreeGateway()
    raise GatewayError(f"Unknown PAYMENT_PROVIDER '{s.payment_provider}'")
