"""Draft generation: retrieval + LLM + persistence.

The worker claims a ``generate_draft`` job, then this module:

1. moves the ticket to ``generating``;
2. retrieves up to three knowledge-base articles (citations come from here);
3. calls the LLM without holding a database connection;
4. stores the draft, points the ticket at it, and completes the job in one
   short transaction.

Any provider problem degrades to a static fallback draft, so a ticket is never
lost because the model was unavailable. The only non-failure deferral is the
service-wide rate limit: the job returns to ``pending`` and the claim attempt is
refunded.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

import httpx
from sqlalchemy import func, select, update

from app import jobs, status
from app.config import Settings
from app.db import SessionFactory
from app.models import Draft, Job, Ticket
from app.services import llm, retrieval

logger = logging.getLogger(__name__)

_GENERATION_STATUSES = (
    status.TICKET_RECEIVED,
    status.TICKET_GENERATING,
    status.TICKET_DRAFT_READY,
    status.TICKET_REJECTED,
    status.TICKET_GENERATION_FAILED,
)

_FALLBACK_WITH_ARTICLE = (
    "Thanks for contacting Northstar Analytics support.\n\n"
    "This knowledge base article may help: {title}\n\n"
    "{excerpt}\n\n"
    "If this does not solve the problem, reply to this message and our team "
    "will follow up."
)

_FALLBACK_WITHOUT_ARTICLE = (
    "Thanks for contacting Northstar Analytics support.\n\n"
    "We could not find a knowledge base article that matches your question. "
    "A support specialist will follow up with you shortly."
)


async def process_generation_job(
    job: Job,
    *,
    session_factory: SessionFactory,
    settings: Settings,
    worker_id: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Handle one claimed ``generate_draft`` job."""
    try:
        await _generate(job, session_factory, settings, worker_id, http_client)
    except llm.LlmRateLimited as exc:
        logger.info(
            "job %s deferred for %.2fs: llm rate limit",
            job.id,
            exc.wait_seconds,
        )
        async with session_factory() as session:
            await jobs.requeue_job(
                session,
                job.id,
                worker_id=worker_id,
                delay_seconds=exc.wait_seconds,
                reason="llm rate limit",
                refund_attempt=True,
            )
    except Exception as exc:
        logger.exception("job %s failed", job.id)
        retrying = await _fail_job(session_factory, job.id, worker_id, exc, settings)
        if not retrying:
            await _mark_generation_failed(session_factory, job.ticket_id)


async def _generate(
    job: Job,
    session_factory: SessionFactory,
    settings: Settings,
    worker_id: str,
    http_client: httpx.AsyncClient | None,
) -> None:
    started = time.monotonic()

    async with session_factory() as session:
        ticket = await session.get(Ticket, job.ticket_id)
        if ticket is None:
            await session.commit()
            await jobs.complete_job(session, job.id, worker_id=worker_id)
            return
        if ticket.status in (status.TICKET_APPROVED, status.TICKET_SENDING, status.TICKET_SENT):
            # Work arrived after moderation started; do not overwrite the flow.
            await session.commit()
            await jobs.complete_job(session, job.id, worker_id=worker_id)
            return

        message = ticket.message
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id, Ticket.status.in_(_GENERATION_STATUSES))
            .values(status=status.TICKET_GENERATING, updated_at=func.now())
        )
        await session.commit()

    async with session_factory() as session:
        articles = await retrieval.search_articles(session, message)

    outcome = await llm.complete(
        settings=settings,
        ticket_id=job.ticket_id,
        message=message,
        articles=articles,
        session_factory=session_factory,
        client=http_client,
    )

    if outcome.text is not None:
        content = outcome.text
        source = status.DRAFT_SOURCE_LLM
        model: str | None = outcome.model
    else:
        await _wait_for_fallback_delay(started, settings)
        content = build_fallback_content(articles)
        source = status.DRAFT_SOURCE_FALLBACK
        model = None

    latency_ms = max(int((time.monotonic() - started) * 1000), 0)
    await _store_draft(
        session_factory,
        job=job,
        worker_id=worker_id,
        content=content,
        citations=retrieval.citations_for(articles),
        source=source,
        model=model,
        latency_ms=latency_ms,
    )
    logger.info(
        "job %s finished: ticket=%s source=%s citations=%s latency_ms=%s",
        job.id,
        job.ticket_id,
        source,
        len(articles),
        latency_ms,
    )


async def _store_draft(
    session_factory: SessionFactory,
    *,
    job: Job,
    worker_id: str,
    content: str,
    citations: list[dict[str, str]],
    source: str,
    model: str | None,
    latency_ms: int,
) -> None:
    async with session_factory() as session, session.begin():
        latest = await session.scalar(
            select(func.coalesce(func.max(Draft.version), 0)).where(
                Draft.ticket_id == job.ticket_id
            )
        )
        version = int(latest or 0) + 1

        draft = Draft(
            ticket_id=job.ticket_id,
            version=version,
            content=content,
            citations=citations,
            source=source,
            model=model,
            latency_ms=latency_ms,
        )
        session.add(draft)
        await session.flush()

        await session.execute(
            update(Ticket)
            .where(Ticket.id == job.ticket_id, Ticket.status.in_(_GENERATION_STATUSES))
            .values(
                current_draft_id=draft.id,
                status=status.TICKET_DRAFT_READY,
                updated_at=func.now(),
            )
        )
        await session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == status.JOB_RUNNING,
                Job.locked_by == worker_id,
            )
            .values(
                status=status.JOB_DONE,
                locked_at=None,
                locked_by=None,
                lease_expires_at=None,
                last_error=None,
                updated_at=func.now(),
            )
        )


def build_fallback_content(articles: list[retrieval.RetrievedArticle]) -> str:
    """Return the deterministic reply used when the provider is unavailable."""
    if not articles:
        return _FALLBACK_WITHOUT_ARTICLE

    top = articles[0]
    excerpt = " ".join(top.body.split())
    if len(excerpt) > 400:
        excerpt = excerpt[:397].rstrip() + "..."
    return _FALLBACK_WITH_ARTICLE.format(title=top.title, excerpt=excerpt)


async def _wait_for_fallback_delay(started: float, settings: Settings) -> None:
    """Keep the keyless fallback path inside the required 5-30 second window."""
    maximum = settings.llm_simulated_delay_max
    if maximum <= 0:
        return
    target = random.uniform(settings.llm_simulated_delay_min, maximum)
    remaining = target - (time.monotonic() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _fail_job(
    session_factory: SessionFactory,
    job_id: uuid.UUID,
    worker_id: str,
    exc: Exception,
    settings: Settings,
) -> bool:
    try:
        async with session_factory() as session:
            return await jobs.fail_job(
                session,
                job_id,
                worker_id=worker_id,
                error=f"{type(exc).__name__}: {exc}",
                settings=settings,
            )
    except Exception:
        logger.exception("could not record failure for job %s", job_id)
        return True


async def _mark_generation_failed(
    session_factory: SessionFactory,
    ticket_id: uuid.UUID,
) -> None:
    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.status == status.TICKET_GENERATING,
                )
                .values(
                    status=status.TICKET_GENERATION_FAILED,
                    updated_at=func.now(),
                )
            )
    except Exception:
        logger.exception("could not mark ticket %s as generation_failed", ticket_id)


# Re-exported for tests and for the worker's type checker.
__all__ = ["build_fallback_content", "process_generation_job"]
