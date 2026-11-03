import logging

from sqlalchemy import create_engine, inspect

from shopping.logging_setup import RedactingFilter, redact
from shopping.models import Base


def test_redaction():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
    assert jwt not in redact(f"authorization: Bearer {jwt}")
    assert "s3cret" not in redact('{"password":"s3cret","email":"a@b"}')
    assert "abc123" not in redact("x-apikey: abc123")
    assert "rt-value" not in redact("grant_type=refresh_token&refresh_token=rt-value")
    record = logging.LogRecord("t", logging.DEBUG, __file__, 1, "token=%s", (jwt,), None)
    RedactingFilter().filter(record)
    assert jwt not in record.getMessage()


def test_migration_matches_models(tmp_path):
    from shopping.database import run_migrations

    url = f"sqlite:///{tmp_path / 'm.db'}"
    run_migrations(url)
    migrated = inspect(create_engine(url))
    for table in Base.metadata.sorted_tables:
        cols = {c["name"] for c in migrated.get_columns(table.name)}
        assert cols == {c.name for c in table.columns}, table.name


def test_mcp_surface_has_no_purchase_tools():
    from shopping.mcp_server.server import tool_names

    names = tool_names()
    assert "shopping_search_products" in names and "shopping_apply_approved" in names
    for banned in ("checkout", "order", "payment", "purchase", "timeslot", "http", "request"):
        assert not any(banned in n for n in names), banned
