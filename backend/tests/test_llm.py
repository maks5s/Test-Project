"""LLM client: real requests, retries, audit rows, and keyless fallback."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.llm as llm
from app import status
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import LlmCall
from app.services.retrieval import RetrievedArticle

ENDPOINT = "https://llm.test/v1/chat/completions"

ARTICLE = RetrievedArticle(
    slug="exporting-reports-to-pdf",
    title="Exporting reports to PDF",
    body="Open the report, click Share, and choose Export as PDF.",
    rank=0.9,
)


def _settings(**overrides: Any) -> Settings:
    updates: dict[str, Any] = {
        "llm_base_url": "https://llm.test/v1",
        "llm_api_key": "test-key",
        "llm_max_retries": 1,
        "llm_retry_backoff_seconds": 0.0,
        "llm_timeout_seconds": 2.0,
        "llm_simulated_delay_min": 0.0,
        "llm_simulated_delay_max": 0.0,
    }
    updates.update(overrides)
    return get_settings().model_copy(update=updates)


def _success_body(content: str = "Click Share and choose Export as PDF.") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 12},
    }


async def _call(settings: Settings) -> llm.LlmOutcome:
    return await llm.complete(
        settings=settings,
        ticket_id=None,
        message="Не можу експортувати звіт у PDF",
        articles=[ARTICLE],
        session_factory=get_sessionmaker(),
    )


async def _calls(session: AsyncSession) -> list[LlmCall]:
    return list(await session.scalars(select(LlmCall).order_by(LlmCall.id)))


@respx.mock
async def test_successful_call_uses_key_and_records_usage(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body()))

    outcome = await _call(_settings())

    assert outcome.text == "Click Share and choose Export as PDF."
    assert outcome.attempts == 1
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-key"

    payload = json.loads(request.content)
    assert payload["model"] == get_settings().llm_model
    assert payload["temperature"] == get_settings().llm_temperature
    assert payload["max_tokens"] == get_settings().llm_max_tokens
    assert "<<<" in payload["messages"][1]["content"]
    assert "exporting-reports-to-pdf" in payload["messages"][1]["content"]
    assert "untrusted" in payload["messages"][0]["content"]

    calls = await _calls(session)
    assert len(calls) == 1
    assert calls[0].status == status.LLM_CALL_OK
    assert calls[0].prompt_tokens == 42
    assert calls[0].completion_tokens == 12


@respx.mock
async def test_keyless_mode_still_attempts_a_real_request(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": {"message": "missing key"}})
    )

    outcome = await _call(_settings(llm_api_key=""))

    assert route.call_count == 1
    assert "authorization" not in route.calls[0].request.headers
    assert outcome.text is None
    assert outcome.used_fallback is True
    assert "401" in (outcome.error or "")

    calls = await _calls(session)
    assert [call.status for call in calls] == [status.LLM_CALL_ERROR, status.LLM_CALL_FALLBACK]


@respx.mock
async def test_retryable_provider_error_is_retried(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_success_body("Second attempt worked.")),
        ]
    )

    outcome = await _call(_settings(llm_max_retries=1))

    assert outcome.text == "Second attempt worked."
    assert outcome.attempts == 2
    assert route.call_count == 2
    calls = await _calls(session)
    assert [call.status for call in calls] == [status.LLM_CALL_ERROR, status.LLM_CALL_OK]


@respx.mock
async def test_retry_budget_is_bounded_and_then_falls_back(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(500))

    outcome = await _call(_settings(llm_max_retries=1))

    assert outcome.text is None
    assert route.call_count == 2
    calls = await _calls(session)
    assert [call.status for call in calls] == [
        status.LLM_CALL_ERROR,
        status.LLM_CALL_ERROR,
        status.LLM_CALL_FALLBACK,
    ]


@respx.mock
async def test_network_timeout_falls_back(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(side_effect=httpx.ConnectTimeout("boom"))

    outcome = await _call(_settings(llm_max_retries=0))

    assert outcome.text is None
    assert "timed out" in (outcome.error or "")
    assert route.call_count == 1
    calls = await _calls(session)
    assert [call.status for call in calls] == [status.LLM_CALL_ERROR, status.LLM_CALL_FALLBACK]


@respx.mock
async def test_empty_provider_reply_falls_back(session: AsyncSession) -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body("   ")))

    outcome = await _call(_settings(llm_max_retries=0))

    assert outcome.text is None
    assert route.call_count == 1
    assert "empty reply" in (outcome.error or "")


@respx.mock
async def test_rate_limit_refuses_before_any_request(session: AsyncSession) -> None:
    await session.execute(
        text("UPDATE llm_rate_limit SET tokens = 0, updated_at = now() WHERE id = 1")
    )
    await session.commit()
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body()))

    with pytest.raises(llm.LlmRateLimited) as excinfo:
        await _call(_settings())

    assert excinfo.value.wait_seconds > 0
    assert route.call_count == 0
    assert await _calls(session) == []
