"""Draft generation: retrieval, LLM/fallback persistence, and rate-limit deferral."""

from __future__ import annotations

from typing import Any

import httpx
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs, status
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import Draft, Job, LlmCall, Ticket
from app.seed import seed_articles
from app.services.generation import process_generation_job

settings = get_settings()
ENDPOINT = "https://llm.test/v1/chat/completions"


def _settings(**overrides: Any) -> Settings:
    updates: dict[str, Any] = {
        "llm_base_url": "https://llm.test/v1",
        "llm_api_key": "test-key",
        "llm_max_retries": 0,
        "llm_retry_backoff_seconds": 0.0,
        "llm_timeout_seconds": 2.0,
        "llm_simulated_delay_min": 0.0,
        "llm_simulated_delay_max": 0.0,
    }
    updates.update(overrides)
    return get_settings().model_copy(update=updates)


def _success_body(content: str = "Open the report and choose Export as PDF.") -> dict[str, Any]:
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def _prepare_job(
    *,
    message: str = "Не можу експортувати звіт у PDF",
    external_id: str = "T-3001",
    status_override: str | None = None,
) -> Job:
    factory = get_sessionmaker()
    async with factory() as session, session.begin():
        ticket = Ticket(
            external_id=external_id,
            customer_email="user@example.com",
            message=message,
            reply_url="https://helpdesk.example.com/tickets/T-3001/replies",
            status=status_override or status.TICKET_RECEIVED,
        )
        session.add(ticket)
        await session.flush()
        session.add(Job(type=status.JOB_GENERATE_DRAFT, ticket_id=ticket.id, max_attempts=3))

    async with factory() as session:
        claimed = await jobs.claim_jobs(
            session,
            worker_id="test-worker",
            limit=1,
            settings=settings,
        )
    assert len(claimed) == 1
    return claimed[0]


async def _run(job: Job, *, generation_settings: Settings | None = None) -> None:
    await process_generation_job(
        job,
        session_factory=get_sessionmaker(),
        settings=generation_settings or settings,
        worker_id="test-worker",
    )


@respx.mock
async def test_llm_draft_is_saved_with_retrieval_citations(session: AsyncSession) -> None:
    await seed_articles()
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body()))
    job = await _prepare_job()

    await _run(job, generation_settings=_settings())

    draft = await session.scalar(select(Draft))
    assert draft is not None
    assert draft.version == 1
    assert draft.source == status.DRAFT_SOURCE_LLM
    assert draft.model == settings.llm_model
    assert draft.content == "Open the report and choose Export as PDF."
    assert draft.citations
    assert draft.citations[0]["slug"] == "exporting-reports-to-pdf"
    assert draft.latency_ms is not None

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    assert ticket.status == status.TICKET_DRAFT_READY
    assert ticket.current_draft_id == draft.id

    stored_job = await session.get(Job, job.id)
    assert stored_job is not None
    assert stored_job.status == status.JOB_DONE
    assert stored_job.locked_by is None
    assert stored_job.lease_expires_at is None


@respx.mock
async def test_keyless_fallback_produces_a_usable_draft(session: AsyncSession) -> None:
    await seed_articles()
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "missing key"})
    )
    job = await _prepare_job()

    await _run(job, generation_settings=_settings(llm_api_key=""))

    assert route.call_count == 1
    draft = await session.scalar(select(Draft))
    assert draft is not None
    assert draft.source == status.DRAFT_SOURCE_FALLBACK
    assert draft.model is None
    assert "Northstar Analytics" in draft.content
    assert "Exporting reports to PDF" in draft.content
    assert draft.citations and draft.citations[0]["slug"] == "exporting-reports-to-pdf"

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None and ticket.status == status.TICKET_DRAFT_READY

    calls = list(await session.scalars(select(LlmCall).order_by(LlmCall.id)))
    assert [call.status for call in calls] == [status.LLM_CALL_ERROR, status.LLM_CALL_FALLBACK]


@respx.mock
async def test_fallback_without_a_matching_article_escalates(session: AsyncSession) -> None:
    await seed_articles()
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401))
    job = await _prepare_job(message="zzzz qqqq wwww")

    await _run(job, generation_settings=_settings(llm_api_key=""))

    draft = await session.scalar(select(Draft))
    assert draft is not None
    assert draft.source == status.DRAFT_SOURCE_FALLBACK
    assert draft.citations == []
    assert "could not find a knowledge base article" in draft.content


@respx.mock
async def test_rate_limit_defers_the_job_without_calling_the_provider(
    session: AsyncSession,
) -> None:
    await session.execute(
        text("UPDATE llm_rate_limit SET tokens = 0, updated_at = now() WHERE id = 1")
    )
    await session.commit()
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body()))
    job = await _prepare_job()

    await _run(job, generation_settings=_settings())

    assert route.call_count == 0
    stored_job = await session.get(Job, job.id)
    assert stored_job is not None
    assert stored_job.status == status.JOB_PENDING
    assert stored_job.attempts == 0
    assert stored_job.locked_by is None
    assert stored_job.run_at > stored_job.created_at
    assert stored_job.last_error == "llm rate limit"

    # No draft is produced by a deferred attempt.
    assert await session.scalar(select(Draft)) is None
    ticket = await session.scalar(select(Ticket))
    assert ticket is not None and ticket.status == status.TICKET_GENERATING


@respx.mock
async def test_provider_failure_after_retries_still_produces_a_fallback(
    session: AsyncSession,
) -> None:
    await seed_articles()
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(500))
    job = await _prepare_job()

    await _run(job, generation_settings=_settings(llm_max_retries=1))

    assert route.call_count == 2
    draft = await session.scalar(select(Draft))
    assert draft is not None
    assert draft.source == status.DRAFT_SOURCE_FALLBACK
    stored_job = await session.get(Job, job.id)
    assert stored_job is not None and stored_job.status == status.JOB_DONE


@respx.mock
async def test_regeneration_increments_the_draft_version(session: AsyncSession) -> None:
    await seed_articles()
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body("Second draft.")))
    job = await _prepare_job()

    # First version exists from an earlier generation.
    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    ticket_id = ticket.id
    previous = Draft(
        ticket_id=ticket_id,
        version=1,
        content="First draft.",
        citations=[],
        source=status.DRAFT_SOURCE_FALLBACK,
    )
    session.add(previous)
    await session.flush()
    await session.commit()

    await _run(job, generation_settings=_settings())

    session.expire_all()
    drafts = list(await session.scalars(select(Draft).order_by(Draft.version)))
    assert [draft.version for draft in drafts] == [1, 2]
    assert drafts[1].content == "Second draft."
    refreshed = await session.get(Ticket, ticket_id)
    assert refreshed is not None
    assert refreshed.current_draft_id == drafts[1].id


@respx.mock
async def test_work_for_an_already_sent_ticket_is_completed_without_a_draft(
    session: AsyncSession,
) -> None:
    await seed_articles()
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_success_body()))
    job = await _prepare_job(status_override=status.TICKET_SENT)

    await _run(job, generation_settings=_settings())

    assert route.call_count == 0
    assert await session.scalar(select(Draft)) is None
    stored_job = await session.get(Job, job.id)
    assert stored_job is not None and stored_job.status == status.JOB_DONE
