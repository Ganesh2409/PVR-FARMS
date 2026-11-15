"""Small shared helpers: time, money, quantity and phone handling."""
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
TELUGU_DIGITS = str.maketrans("౦౧౨౩౪౫౬౭౮౯", "0123456789")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_ist() -> date:
    return datetime.now(IST).date()


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def fmt_date(d: date | None) -> str:
    return d.strftime("%d-%m-%Y") if d else ""


def fmt_datetime(dt: datetime | None) -> str:
    local = to_ist(dt)
    return local.strftime("%d-%m-%Y %I:%M %p") if local else ""


def money(value) -> str:
    d = Decimal(value or 0).quantize(Decimal("0.01"))
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


def qty_str(value) -> str:
    d = Decimal(value or 0).quantize(Decimal("0.001")).normalize()
    return f"{d:f}"


def q2(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def parse_decimal(text: str) -> Decimal | None:
    """Parse a customer-typed number such as '3', '2.5', '3 kg', '౩'."""
    t = (text or "").translate(TELUGU_DIGITS).strip().lower().replace(",", ".")
    t = re.sub(r"(kgs?|kilos?|కిలోలు|కిలో|కేజీలు|కేజీ)\s*$", "", t).strip()
    if not re.fullmatch(r"\d+(\.\d{1,3})?", t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def normalize_indian_mobile(raw: str) -> str | None:
    """Return '91XXXXXXXXXX' for a valid Indian mobile number, else None."""
    digits = re.sub(r"\D", "", (raw or "").translate(TELUGU_DIGITS))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if re.fullmatch(r"[6-9]\d{9}", digits):
        return "91" + digits
    return None


def display_phone(p: str | None) -> str:
    if not p:
        return ""
    if len(p) == 12 and p.startswith("91"):
        return "+91 " + p[2:]
    return p
