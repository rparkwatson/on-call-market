"""Database URL resolution, engine and session factory.  No Streamlit imports here:
streamlit_app.py reads the secret and passes it in, so the tests can use a temp SQLite file.

The URL is resolved in this order:
  1. the value the caller found in st.secrets["DATABASE_URL"] (Streamlit Community Cloud),
  2. the DATABASE_URL environment variable,
  3. sqlite:///oncall_local.db, a file in the working directory (gitignored by *.db).
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Base

DEFAULT_SQLITE_URL = "sqlite:///oncall_local.db"


def normalise_url(url):
    """Neon hands out postgresql:// (or postgres://) URLs; SQLAlchemy needs the driver named."""
    url = url.strip()
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def resolve_database_url(secret_value=None, environ=None):
    """The database URL to use: the Streamlit secret if the caller found one, else
    $DATABASE_URL, else the local SQLite file."""
    environ = os.environ if environ is None else environ
    for candidate in (secret_value, environ.get("DATABASE_URL")):
        if candidate and str(candidate).strip():
            return normalise_url(str(candidate))
    return DEFAULT_SQLITE_URL


def make_engine(url):
    """An engine for the URL.  SQLite gets foreign keys switched on (they are off by
    default) and may be shared between Streamlit's threads; Postgres gets a small pool
    that checks connections before use, because Neon's free tier closes idle ones."""
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_recycle=300, pool_size=2, max_overflow=3)


def init_db(engine):
    """Create any missing tables (there is no migration tool; the schema is additive)."""
    Base.metadata.create_all(engine)


def session_factory(engine):
    """A sessionmaker whose sessions keep their loaded values after commit, so the repo
    can return plain dicts built from them."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def connect(url):
    """Engine + tables + session factory in one call.  Returns (engine, SessionLocal)."""
    engine = make_engine(url)
    init_db(engine)
    return engine, session_factory(engine)
