"""Tests for weightsdb.redaction."""

from __future__ import annotations

from weightsdb.redaction import redact_url


def test_redact_url_masks_password() -> None:
    redacted = redact_url("postgresql://user:hunter2@host:5432/db")
    assert "hunter2" not in redacted
    assert redacted == "postgresql://user:***@host:5432/db"


def test_redact_url_preserves_user_host_and_database() -> None:
    redacted = redact_url("postgresql+psycopg://alice:s3cret@db.example.com:5433/loadcoach")
    assert "alice" in redacted
    assert "db.example.com" in redacted
    assert "5433" in redacted
    assert "loadcoach" in redacted
    assert "s3cret" not in redacted


def test_redact_url_no_password_present() -> None:
    redacted = redact_url("sqlite:////var/lib/loadcoach/loadcoach.sqlite3")
    assert redacted == "sqlite:////var/lib/loadcoach/loadcoach.sqlite3"


def test_redact_url_never_leaks_password_in_repr_or_str() -> None:
    for url in (
        "postgresql://user:topsecret@host/db",
        "postgresql+psycopg://user:topsecret@host:5432/db",
    ):
        redacted = redact_url(url)
        assert "topsecret" not in redacted
        assert "topsecret" not in repr(redacted)
