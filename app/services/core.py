"""Runtime settings, audit logging and the payment-provider abstraction."""
from sqlalchemy.orm import Session

from app.models import AuditLog, Setting

DEFAULT_SETTINGS = {
    "farm_name": "Mango Farm",
    "contact_number": "",
    "business_address": "Govindaraopet, Telangana",
    "business_hours": "9:00 AM - 6:00 PM",
    "default_language": "te",
    "accepting_orders": "true",
    "cod_enabled": "false",
    "admin_notify_phone": "",
}


def get_setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


def get_all_settings(db: Session) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for row in db.query(Setting).all():
        values[row.key] = row.value
    return values


def set_setting(db: Session, key: str, value: str) -> None:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown setting {key}")
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def accepting_orders(db: Session) -> bool:
    return get_setting(db, "accepting_orders").lower() == "true"


def audit(db: Session, admin: str, action: str, entity: str = "", entity_id="", details: str = "",
          ip: str | None = None) -> None:
    db.add(AuditLog(admin_username=admin, action=action, entity=entity, entity_id=str(entity_id),
                    details=details[:2000], ip=ip))
