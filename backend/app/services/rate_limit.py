"""Service-wide LLM rate limiter.

The token bucket lives in PostgreSQL so the limit holds across every API and
worker container. One singleton row is updated in a single statement guarded by
``FOR UPDATE``; concurrent workers therefore serialize on the row and can never
spend more than the configured capacity.

Capacity is ``LLM_RATE_LIMIT_PER_MINUTE`` (60 by default) and refill is one
token per second for that capacity. A refused request does not consume a token.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from app.config import Settings
from app.db import SessionFactory

_ACQUIRE_SQL = text(
    """
    WITH current AS (
        SELECT id,
               least(
                   :capacity,
                   tokens + extract(epoch FROM (now() - updated_at)) * :refill_per_second
               ) AS refilled
        FROM llm_rate_limit
        WHERE id = 1
        FOR UPDATE
    )
    UPDATE llm_rate_limit l
    SET tokens = CASE
            WHEN current.refilled >= 1 THEN current.refilled - 1
            ELSE current.refilled
        END,
        updated_at = now()
    FROM current
    WHERE l.id = current.id
    RETURNING l.tokens, current.refilled
    """
)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one token request."""

    allowed: bool
    wait_seconds: float
    tokens_remaining: float


async def acquire_llm_token(
    session_factory: SessionFactory,
    settings: Settings,
) -> RateLimitResult:
    """Take one token or report how long the caller should wait."""
    capacity = float(settings.llm_rate_limit_per_minute)
    refill_per_second = capacity / 60.0

    async with session_factory() as session, session.begin():
        row = (
            await session.execute(
                _ACQUIRE_SQL,
                {
                    "capacity": capacity,
                    "refill_per_second": refill_per_second,
                },
            )
        ).one()

    tokens_remaining = float(row[0])
    refilled = float(row[1])
    allowed = refilled >= 1.0 - 1e-9
    wait_seconds = 0.0 if allowed else max(0.0, (1.0 - refilled) / refill_per_second)
    return RateLimitResult(
        allowed=allowed,
        wait_seconds=wait_seconds,
        tokens_remaining=tokens_remaining,
    )
