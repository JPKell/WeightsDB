"""weightsdb.redaction — strip credentials from a database URL before it is logged or raised.

Used by every error and log path in this package (spec §14): a connection URL reaches
:class:`~weightsdb.errors.DatabaseUnavailable`, a health payload or a DEBUG log line only after
going through :func:`redact_url`, never in its raw form.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

__all__ = ["redact_url"]


def redact_url(url: str) -> str:
    """Return ``url`` with any password replaced by ``***``.

    Args:
        url: A SQLAlchemy database URL, e.g. ``postgresql://user:secret@host/db``.

    Returns:
        The same URL with its password component masked (``postgresql://user:***@host/db``), or
        unchanged if it carried no password. Every other component (user, host, database name) is
        preserved, since those are needed to identify *which* database a message is about.
    """
    return make_url(url).render_as_string(hide_password=True)
