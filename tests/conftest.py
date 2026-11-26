"""Test harness. Tests run against a REAL PostgreSQL database (never SQLite).

Set TEST_DATABASE_URL to a database whose name ends in "_test" (it is TRUNCATEd between tests!), e.g.
  postgresql+psycopg://mango_app:PASSWORD@localhost:5432/mango_farm_test
The schema is built by running the actual Alembic migrations, so migrations are tested too.
"""
import os
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import pytest

_TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
if not _TEST_URL:
    pytest.exit("Set TEST_DATABASE_URL to a PostgreSQL test database (see README, 'Running the tests').", 2)
if not _TEST_URL.split("?")[0].rsplit("/", 1)[-1].endswith("_test"):
    pytest.exit("Refusing to run: the TEST_DATABASE_URL database name must end in '_test' (it gets truncated).", 2)

_TMP = Path(tempfile.mkdtemp(prefix="mango-tests-"))
os.environ.update({
    "ENVIRONMENT": "development", "DATABASE_URL": _TEST_URL, "AUTO_MIGRATE": "true",
    "SECRET_KEY": "test-secret-key-for-sessions", "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "test-password-123",
    "UPLOAD_DIR": str(_TMP / "uploads"), "WHATSAPP_VERIFY_TOKEN": "verify-me", "WHATSAPP_APP_SECRET": "app-secret",
    "WHATSAPP_ACCESS_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": "", "PUBLIC_BASE_URL": "https://shop.test",
    "ALLOW_UNSIGNED_WEBHOOKS": "false", "WHATSAPP_TEMPLATE_ORDER_UPDATE": "", "PAYMENT_PROVIDER": "mock",
    "CASHFREE_CLIENT_ID": "", "CASHFREE_CLIENT_SECRET": "", "PAYMENT_EXPIRY_MINUTES": "30",
    "PAYMENT_RELEASE_ON_FAILED_ATTEMPT": "false", "BCRYPT_ROUNDS": "4",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app, migrate  # noqa: E402
from app.models import Customer  # noqa: E402
from app.security import login_limiter, simulator_limiter  # noqa: E402
from app.seed import run_seed  # noqa: E402
from app.services.conversation import handle_incoming  # noqa: E402
from app.services.core import set_setting  # noqa: E402
from app.services.gateways import MockGateway  # noqa: E402
from app.services.messaging import InMessage  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema with the real migrations (downgrade first to prove they are reversible)."""
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    migrate()
    yield


@pytest.fixture(autouse=True)
def fresh_db(_schema):
    """Empty every table (children first) and restart id sequences so tests see ids 1, 2, ...
    (DELETE rather than TRUNCATE: TRUNCATE is very slow on some Windows machines.)"""
    with engine.begin() as c:
        for table in reversed(Base.metadata.sorted_tables):
            c.execute(table.delete())
            if "id" in table.c and table.c.id.primary_key and table.c.id.type.python_type is int:
                c.execute(text("SELECT setval(pg_get_serial_sequence(:t, 'id'), 1, false)"), {"t": table.name})
    with SessionLocal() as db:
        run_seed(db)
    login_limiter.reset()
    simulator_limiter.reset()
    MockGateway.reset()
    yield


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def client(db):
    """HTTP client. The shared test session is rolled back before each request so that it holds no
    locks while the request runs (production sessions are per-request)."""
    c = TestClient(app)
    original = c.request

    def request(*args, **kwargs):
        db.rollback()
        return original(*args, **kwargs)

    c.request = request
    return c


def allow_cod(db, phone="919000000001"):
    """Switch Cash on Delivery on globally and for this customer (it is off by default)."""
    set_setting(db, "cod_enabled", "true")
    c = db.query(Customer).filter_by(whatsapp_number=phone).first()
    if c is None:
        c = Customer(whatsapp_number=phone, channel="simulator")
        db.add(c)
    c.cod_allowed = True
    db.commit()


class Chat:
    """Drives the real conversation engine like a customer would."""

    def __init__(self, db, phone="919000000001", channel="simulator"):
        self.db, self.phone, self.channel, self.n = db, phone, channel, 0
        self.last: list = []

    def _send(self, **kw):
        self.n += 1
        self.last = handle_incoming(self.db, InMessage(id=f"m{self.phone}-{self.n}", phone=self.phone, **kw), self.channel)
        return self

    def say(self, text):
        return self._send(text=text)

    def pick(self, reply_id, title=""):
        return self._send(text=title, reply_id=reply_id, kind="reply")

    @property
    def text(self) -> str:
        return "\n".join(m.body for m in self.last)

    @property
    def option_ids(self) -> list[str]:
        return [o.id for m in self.last for o in m.options]


@pytest.fixture
def chat(db):
    return Chat(db)


def first_date_id(chat) -> str:
    return next(i for i in chat.option_ids if i.startswith("date_"))


def drive_to_review(chat: Chat, lang="en", product="prod_1", qty="qty_2", slot=False, pay="cod"):
    """Full happy path up to the order summary. pay='cod' enables COD for the customer first
    (as the family would); pay='online' leaves COD off, the default.
    First-time customers type their name and one address message; returning customers tap 'Deliver here'."""
    if pay == "cod":
        allow_cod(chat.db, chat.phone)
    chat.say("Hi").pick("lang_te" if lang == "te" else "lang_en")
    chat.pick("menu_order").pick(product).pick(qty)
    chat.pick(first_date_id(chat))
    if slot:
        chat.pick(next(i for i in chat.option_ids if i.startswith("slot_")))
    if "use_saved" in chat.option_ids:
        chat.pick("use_saved")
    else:
        chat.say("Ravi Kumar")
        area = next((i for i in chat.option_ids if i.startswith("area_")), None)
        if area:  # only asked when more than one village is served
            chat.pick(area)
        chat.say("H.No 4-5, Main Road, near the temple")
    if "pay_cod" in chat.option_ids:
        chat.pick("pay_cod" if pay == "cod" else "pay_online")
    return chat
