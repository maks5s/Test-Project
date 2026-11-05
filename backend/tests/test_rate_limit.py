"""PostgreSQL token bucket: capacity, refill, refusal, and concurrency."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.services.rate_limit import RateLimitResult, acquire_llm_token

settings = get_settings()


async def _set_bucket(session: AsyncSession, tokens: float, age_seconds: float = 0.0) -> None:
    await session.execute(
        text(
            "UPDATE llm_rate_limit "
            "SET tokens = :tokens, updated_at = now() - make_interval(secs => :age) "
            "WHERE id = 1"
        ),
        {"tokens": tokens, "age": age_seconds},
    )
    await session.commit()


async def _acquire() -> RateLimitResult:
    return await acquire_llm_token(get_sessionmaker(), settings)


async def test_token_is_spent_on_each_request(session: AsyncSession) -> None:
    await _set_bucket(session, 60.0)

    first = await _acquire()
    second = await _acquire()

    assert first.allowed is True
    assert second.allowed is True
    stored = await session.scalar(text("SELECT tokens FROM llm_rate_limit WHERE id = 1"))
    assert float(stored) < 59.0


async def test_empty_bucket_refuses_and_reports_wait(session: AsyncSession) -> None:
    await _set_bucket(session, 1.0)

    allowed = await _acquire()
    refused = await _acquire()

    assert allowed.allowed is True
    assert refused.allowed is False
    assert 0.5 <= refused.wait_seconds <= 1.05


async def test_bucket_refills_over_time(session: AsyncSession) -> None:
    await _set_bucket(session, 0.0, age_seconds=2.0)

    result = await _acquire()

    assert result.allowed is True
    assert result.tokens_remaining >= 0.9


async def test_capacity_is_capped_after_a_long_idle_period(session: AsyncSession) -> None:
    await _set_bucket(session, 0.0, age_seconds=3600.0)

    result = await _acquire()

    assert result.allowed is True
    assert result.tokens_remaining == 59.0


async def test_the_sixty_first_call_in_a_minute_is_refused(session: AsyncSession) -> None:
    await _set_bucket(session, 60.0)

    results = [await _acquire() for _ in range(61)]

    assert sum(1 for result in results if result.allowed) == 60
    assert results[-1].allowed is False
    assert results[-1].wait_seconds > 0


async def test_concurrent_requests_cannot_spend_the_last_token_twice(
    session: AsyncSession,
) -> None:
    await _set_bucket(session, 1.0)

    async def acquire() -> RateLimitResult:
        return await acquire_llm_token(get_sessionmaker(), settings)

    results = await asyncio.gather(acquire(), acquire())

    assert [result.allowed for result in results].count(True) == 1
    assert [result.allowed for result in results].count(False) == 1
