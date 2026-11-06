"""At-least-once delivery of approved replies to the helpdesk.

Delivery is intentionally split into short database transactions around one
HTTP POST. The outbound row is the transactional outbox, and its deterministic
``Idempotency-Key`` lets the receiver collapse retries into one reply.

Failure policy:

* network errors, timeouts, 408/425/429 and 5xx are retried with exponential
  backoff and jitter, always with the same idempotency key;
* 3xx is never followed (SSRF protection) and 4xx is terminal;
* after ``DELIVERY_MAX_ATTEMPTS`` the outbound row becomes ``failed`` and the
  ticket ``send_failed``; an operator can requeue it once more.

The POST is sent without a database connection held. If the process dies after
the helpdesk accepted the reply but before we recorded it, the job is retried;
the receiver sees the same idempotency key and does not create a second reply.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs, status
from app.config import Settings
from app.db import SessionFactory
from app.models import Job, OutboundMessage, Ticket
from app.services.reply_urls import InvalidReplyUrl, validate_delivery_target

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class _Attempt:
    outbound_id: uuid.UUID
    ticket_id: uuid.UUID
    reply_url: str
    payload: dict[str, Any]
    idempotency_key: str
    attempt: int


async def process_send_job(
    job: Job,
    *,
    session_factory: SessionFactory,
    settings: Settings,
    worker_id: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Handle one claimed ``send_reply`` job."""
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=settings.helpdesk_timeout_seconds,
        follow_redirects=False,
    )
    try:
        attempt = await _begin_attempt(session_factory, job, worker_id)
        if attempt is None:
            return

        try:
            target = validate_delivery_target(attempt.reply_url, settings.allowed_reply_hosts)
        except InvalidReplyUrl as exc:
            await _finish_failure(
                session_factory,
                job,
                worker_id,
                attempt,
                settings,
                error=f"blocked reply_url: {exc}",
                retryable=False,
            )
            return

        headers = {
            "Idempotency-Key": attempt.idempotency_key,
            "User-Agent": "helpdesk-drafts/0.1",
        }
        started = time.monotonic()
        try:
            response = await client.post(
                target,
                json=attempt.payload,
                headers=headers,
                timeout=settings.helpdesk_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            await _finish_failure(
                session_factory,
                job,
                worker_id,
                attempt,
                settings,
                error=f"helpdesk request timed out after {time.monotonic() - started:.2f}s",
                retryable=True,
            )
            return
        except httpx.RequestError as exc:
            await _finish_failure(
                session_factory,
                job,
                worker_id,
                attempt,
                settings,
                error=f"helpdesk request failed: {type(exc).__name__}",
                retryable=True,
            )
            return

        if 200 <= response.status_code < 300:
            await _finish_success(session_factory, job, worker_id, attempt)
            return

        if 300 <= response.status_code < 400:
            error = (
                f"helpdesk returned {response.status_code} {response.reason_phrase}; "
                "redirects are not followed"
            )
            retryable = False
        else:
            error = f"helpdesk returned {response.status_code} {response.reason_phrase}"
            retryable = response.status_code in (408, 425, 429) or response.status_code >= 500

        await _finish_failure(
            session_factory,
            job,
            worker_id,
            attempt,
            settings,
            error=error,
            retryable=retryable,
        )
    except Exception as exc:
        logger.exception("send_reply job %s crashed", job.id)
        try:
            async with session_factory() as session:
                retrying = await jobs.fail_job(
                    session,
                    job.id,
                    worker_id=worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                    settings=settings,
                )
            if not retrying:
                await _mark_ticket_send_failed(session_factory, job.ticket_id)
        except Exception:
            logger.exception("could not record delivery failure for job %s", job.id)
    finally:
        if owns_client:
            await client.aclose()


async def _begin_attempt(
    session_factory: SessionFactory,
    job: Job,
    worker_id: str,
) -> _Attempt | None:
    async with session_factory() as session, session.begin():
        outbound = await session.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.ticket_id == job.ticket_id)
            .with_for_update()
        )
        ticket = await session.get(Ticket, job.ticket_id)

        if outbound is None or ticket is None:
            await _mark_job_failed_inline(
                session,
                job.id,
                worker_id,
                "outbound message or ticket is missing",
            )
            return None
        if outbound.status == status.OUTBOUND_SENT:
            await _mark_job_done_inline(session, job.id, worker_id)
            return None
        if outbound.status == status.OUTBOUND_FAILED:
            await _mark_job_failed_inline(
                session,
                job.id,
                worker_id,
                "outbound message is already in the failed state",
            )
            return None

        outbound.status = status.OUTBOUND_SENDING
        outbound.attempts += 1
        if ticket.status != status.TICKET_SENT:
            ticket.status = status.TICKET_SENDING

        return _Attempt(
            outbound_id=outbound.id,
            ticket_id=ticket.id,
            reply_url=ticket.reply_url,
            payload=dict(outbound.payload),
            idempotency_key=outbound.idempotency_key,
            attempt=outbound.attempts,
        )


async def _finish_success(
    session_factory: SessionFactory,
    job: Job,
    worker_id: str,
    attempt: _Attempt,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(OutboundMessage)
            .where(
                OutboundMessage.id == attempt.outbound_id,
                OutboundMessage.status.in_((status.OUTBOUND_PENDING, status.OUTBOUND_SENDING)),
            )
            .values(
                status=status.OUTBOUND_SENT,
                sent_at=func.now(),
                last_error=None,
                updated_at=func.now(),
            )
        )
        await session.execute(
            update(Ticket)
            .where(Ticket.id == attempt.ticket_id)
            .values(
                status=status.TICKET_SENT,
                sent_at=func.now(),
                updated_at=func.now(),
            )
        )
        await _mark_job_done_inline(session, job.id, worker_id)

    logger.info(
        "job %s delivered: ticket=%s attempt=%s idempotency_key=%s",
        job.id,
        attempt.ticket_id,
        attempt.attempt,
        attempt.idempotency_key,
    )


async def _finish_failure(
    session_factory: SessionFactory,
    job: Job,
    worker_id: str,
    attempt: _Attempt,
    settings: Settings,
    *,
    error: str,
    retryable: bool,
) -> None:
    error = error[:MAX_ERROR_CHARS]
    will_retry = False
    delay = 0.0

    async with session_factory() as session, session.begin():
        outbound = await session.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.id == attempt.outbound_id)
            .with_for_update()
        )
        if outbound is None:
            will_retry = False
        else:
            outbound.last_error = error
            if retryable and outbound.attempts < settings.delivery_max_attempts:
                outbound.status = status.OUTBOUND_PENDING
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == attempt.ticket_id)
                    .values(status=status.TICKET_APPROVED, updated_at=func.now())
                )
                will_retry = True
                delay = _retry_delay(outbound.attempts, settings)
            else:
                outbound.status = status.OUTBOUND_FAILED
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == attempt.ticket_id)
                    .values(status=status.TICKET_SEND_FAILED, updated_at=func.now())
                )

    if will_retry:
        logger.warning(
            "job %s delivery attempt %s failed (retry in %.2fs): %s",
            job.id,
            attempt.attempt,
            delay,
            error,
        )
        async with session_factory() as session:
            await jobs.requeue_job(
                session,
                job.id,
                worker_id=worker_id,
                delay_seconds=delay,
                reason=error,
                refund_attempt=False,
            )
    else:
        logger.error("job %s delivery failed terminally: %s", job.id, error)
        async with session_factory() as session:
            await jobs.mark_job_failed(
                session,
                job.id,
                worker_id=worker_id,
                error=error,
            )


def _retry_delay(attempts: int, settings: Settings) -> float:
    exponential = settings.delivery_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    base = min(settings.delivery_backoff_max_seconds, exponential)
    return float(base + random.uniform(0.0, settings.delivery_jitter_seconds))


async def _mark_ticket_send_failed(
    session_factory: SessionFactory,
    ticket_id: uuid.UUID,
) -> None:
    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(status=status.TICKET_SEND_FAILED, updated_at=func.now())
            )
            await session.execute(
                update(OutboundMessage)
                .where(OutboundMessage.ticket_id == ticket_id)
                .values(status=status.OUTBOUND_FAILED, updated_at=func.now())
            )
    except Exception:
        logger.exception("could not mark ticket %s as send_failed", ticket_id)


async def _mark_job_done_inline(
    session: AsyncSession,
    job_id: uuid.UUID,
    worker_id: str,
) -> None:
    await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
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


async def _mark_job_failed_inline(
    session: AsyncSession,
    job_id: uuid.UUID,
    worker_id: str,
    error: str,
) -> None:
    await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == status.JOB_RUNNING,
            Job.locked_by == worker_id,
        )
        .values(
            status=status.JOB_FAILED,
            locked_at=None,
            locked_by=None,
            lease_expires_at=None,
            last_error=error[:MAX_ERROR_CHARS],
            updated_at=func.now(),
        )
    )


__all__ = ["process_send_job"]
