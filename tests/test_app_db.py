"""app.db: URL resolution and the page-safe description of a failed connection."""
from app import db


class Boom(Exception):
    pass


class Wrapped(Exception):
    """Mimics sqlalchemy.exc.DBAPIError, which carries the driver's exception as .orig."""

    def __init__(self, orig):
        super().__init__("(psycopg.OperationalError) " + str(orig))
        self.orig = orig


URL = "postgresql+psycopg://neondb_owner:s3cret-pw@ep-x-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require"


def test_describe_masks_the_password_and_keeps_the_reason():
    exc = Boom('connection failed: FATAL:  password authentication failed for user "neondb_owner" (s3cret-pw)')
    text = db.describe_connection_error(exc, URL)
    assert "s3cret-pw" not in text
    assert "password authentication failed" in text
    assert text.startswith("Boom: ")


def test_describe_unwraps_the_driver_exception():
    exc = Wrapped(Boom("connection timeout expired"))
    text = db.describe_connection_error(exc, URL)
    assert text == "Boom: connection timeout expired"


def test_describe_never_echoes_the_url():
    exc = Boom("could not open " + URL)
    text = db.describe_connection_error(exc, URL)
    assert URL not in text and "s3cret-pw" not in text and "<DATABASE_URL>" in text


def test_describe_copes_with_sqlite_and_empty_messages():
    assert db.describe_connection_error(Boom(""), db.DEFAULT_SQLITE_URL) == "Boom: Boom"
    assert db.describe_connection_error(Boom("locked"), "not a url at all") == "Boom: locked"


def test_resolve_database_url_order():
    assert db.resolve_database_url(None, {}) == db.DEFAULT_SQLITE_URL
    assert db.resolve_database_url(None, {"DATABASE_URL": "postgres://u:p@h/d"}) == "postgresql+psycopg://u:p@h/d"
    assert db.resolve_database_url(" postgresql://u:p@h/d ", {"DATABASE_URL": "x"}) == "postgresql+psycopg://u:p@h/d"
