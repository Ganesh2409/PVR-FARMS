import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, get_settings
from app.database import Base, SessionLocal, engine
from app.routers import admin, payments_web, public, whatsapp
from app.seed import run_seed
from app.services import payments
from app.web import NotAuthenticated

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mango")


def migrate() -> None:
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def _sweep_once() -> None:
    with SessionLocal() as db:
        payments.run_maintenance(db)


async def _payment_sweeper() -> None:
    """Every minute: reconcile unpaid checkouts (missed webhooks, expiry) and pending refunds."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(_sweep_once)
        except Exception:
            log.exception("payment sweeper failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.is_production and s.secret_key.startswith("dev-insecure"):
        raise RuntimeError("Set a strong SECRET_KEY in production.")
    if s.is_production and s.payment_provider == "mock":
        raise RuntimeError("PAYMENT_PROVIDER=mock is not allowed in production.")
    if s.payment_provider == "cashfree" and not s.cashfree_configured:
        log.warning("Cashfree credentials are not set: online payments will fail until CASHFREE_CLIENT_ID/SECRET are configured.")
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    if s.auto_migrate:
        migrate()
    else:
        Base.metadata.create_all(engine)
    with SessionLocal() as db:
        run_seed(db)
    sweeper = asyncio.create_task(_payment_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Mango Farm Ordering", lifespan=lifespan,
                  docs_url=None if s.is_production else "/docs", redoc_url=None, openapi_url=None if s.is_production else "/openapi.json")
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="mango_session",
                       max_age=8 * 3600, same_site="lax", https_only=s.is_production)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/admin"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(NotAuthenticated)
    async def _not_auth(request: Request, exc: NotAuthenticated):
        return RedirectResponse("/admin/login", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=s.upload_dir), name="media")
    app.include_router(public.router)
    app.include_router(whatsapp.router)
    app.include_router(payments_web.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/simulator" if get_settings().enable_simulator else "/admin")

    return app


app = create_app()
