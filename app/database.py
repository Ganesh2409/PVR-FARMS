from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    """PostgreSQL engine (psycopg 3). Default isolation READ COMMITTED; the services take explicit
    row locks (SELECT ... FOR UPDATE) wherever a check-then-write must be atomic."""
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_recycle=1800)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
