"""Validation of helpdesk ``reply_url`` values.

Ingestion must accept any reasonable http/https endpoint, because the helpdesk
owns the URL. Hard SSRF controls (host allowlist, no redirects, no private
addresses) are applied when a reply is actually delivered, not at intake; see
``app.services.delivery`` in stage 5.

The checks here keep obviously unusable or dangerous URLs out of the database:
non-http(s) schemes, missing hosts, embedded credentials, whitespace in the
host, invalid ports, and values over the API limit.
"""

from __future__ import annotations

from urllib.parse import urlsplit

MAX_REPLY_URL_LENGTH = 2000
ALLOWED_REPLY_URL_SCHEMES = frozenset({"http", "https"})


class InvalidReplyUrl(ValueError):
    """Raised when a ``reply_url`` cannot be used as a delivery target."""


def normalize_reply_url(raw: str) -> str:
    """Return a stripped, validated absolute URL or raise ``InvalidReplyUrl``."""
    candidate = raw.strip()
    if not candidate:
        raise InvalidReplyUrl("reply_url must not be empty")
    if len(candidate) > MAX_REPLY_URL_LENGTH:
        raise InvalidReplyUrl(f"reply_url must be at most {MAX_REPLY_URL_LENGTH} characters")

    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidReplyUrl("reply_url is not a valid absolute URL") from exc

    if parts.scheme.lower() not in ALLOWED_REPLY_URL_SCHEMES:
        raise InvalidReplyUrl("reply_url must use http or https")
    if not hostname:
        raise InvalidReplyUrl("reply_url must include a host")
    if parts.username is not None or parts.password is not None:
        raise InvalidReplyUrl("reply_url must not contain credentials")
    if any(char.isspace() for char in hostname):
        raise InvalidReplyUrl("reply_url host must not contain whitespace")
    # ``parts.port`` above already rejects non-numeric and out-of-range ports.
    del port

    return candidate
