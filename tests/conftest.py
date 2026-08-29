"""Shared pytest fixtures.

WeightsDB reads no configuration file and no environment variable of its own (spec §12), so unlike
an application's test suite there is no XDG tree to isolate here. ``WEIGHTSDB_POSTGRES_URL`` and
``WEIGHTSDB_REQUIRE_POSTGRES`` (read by :mod:`weightsdb.testing`) are deliberately real environment
variables — they configure the *test harness*, not the package under test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.fixture
def frozen_instant() -> datetime:
    """A fixed, timezone-aware UTC instant for deterministic timestamp assertions."""
    return datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
