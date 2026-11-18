from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminUser
from app.utils import display_phone, fmt_date, fmt_datetime, money, qty_str

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
STATUS_LABELS = {"PENDING": "Awaiting Confirmation", "ACCEPTED": "Confirmed", "AWAITING_PAYMENT": "Awaiting Payment",
                 "DISPATCHED": "Dispatched", "DELIVERED": "Delivered", "CANCELLED": "Cancelled", "REJECTED": "Rejected"}
ACTION_LABELS = {"ACCEPTED": "Confirm Order", "REJECTED": "Reject Order", "CANCELLED": "Cancel Order",
                 "DISPATCHED": "Mark Dispatched", "DELIVERED": "Mark Delivered"}
templates.env.filters.update(status_label=lambda s: STATUS_LABELS.get(s, s), action_label=lambda s: ACTION_LABELS.get(s, s),
                             money=money, qty=qty_str, fdate=fmt_date, fdt=fmt_datetime, phone=display_phone)


class NotAuthenticated(Exception):
    pass


def get_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    admin_id = request.session.get("admin_id")
    admin = db.get(AdminUser, admin_id) if admin_id else None
    if admin is None or not admin.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return admin
