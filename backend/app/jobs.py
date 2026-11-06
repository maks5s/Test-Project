"""Durable job queue backed by PostgreSQL.

Jobs are claimed with ``FOR UPDATE SKIP LOCKED`` so any number of worker
processes can share the table without seeing the same row twice. A claim is
short: it commits before the expensive work starts, and the worker then holds a
lease instead of a database lock.

Every state change is conditional on the row still being owned by the worker
that claimed it. A slow worker whose lease expired (and whose job was reaped)
can finish its work but can no longer overwrite the job row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app import status
from app.config import Settings
from app.models import Job, Ticket


def _interval(seconds: float) -> ColumnElement[datetime]:
    """Render ``now() + seconds`` with the database clock."""
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, float(seconds))


def _claimable(*, job_types: tuple[str, ...], limit: int) -> Select[tuple[uuid.UUID]]:
    return (
        select(Job.id)
        .where(
            Job.status == status.JOB_PENDING,
            Job.type.in_(job_types),
            Job.run_at <= func.now(),
        )
        .order_by(Job.run_at, Job.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def claim_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    settings: Settings,
    job_types: tuple[str, ...] = status.JOB_TYPES,
) -> list[Job]:
    """Atomically mark up to ``limit`` pending jobs as running.

    The transaction commits before returning, so no row lock is held while the
    worker talks to the LLM or the helpdesk.
    """
    if limit <= 0:
        return []

    claimable = _claimable(job_types=job_types, limit=limit).cte("claimable")
    statement = (
        update(Job)
        .where(Job.id.in_(select(claimable.c.id)))
        .values(
            status=status.JOB_RUNNING,
            locked_by=worker_id,
            locked_at=func.now(),
            lease_expires_at=_interval(settings.job_lease_seconds),
            attempts=Job.attempts + 1,
            updated_at=func.now(),
        )
        .returning(Job)
        .execution_options(synchronize_session=False)
    )

    async with session.begin():
        claimed = list(await session.scalars(statement))
    return claimed


async def complete_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
) -> bool:
    """Mark a claimed job as done; return ``False`` if it was no longer ours."""
    statement = (
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
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    async with session.begin():
        completed = await session.scalar(statement)
    return completed is not None


async def requeue_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    delay_seconds: float,
    reason: str,
    refund_attempt: bool = False,
) -> bool:
    """Return a claimed job to the queue without treating it as a failure.

    Used when the LLM rate limiter refuses a token: the work is deferred, not
    failed, so the claim attempt is refunded by default.
    """
    delay = max(0.0, delay_seconds)
    async with session.begin():
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status != status.JOB_RUNNING or job.locked_by != worker_id:
            return False

        job.status = status.JOB_PENDING
        job.run_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.locked_at = None
        job.locked_by = None
        job.lease_expires_at = None
        job.last_error = reason[:2000]
        if refund_attempt:
            job.attempts = max(job.attempts - 1, 0)
    return True


async def fail_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    error: str,
    settings: Settings,
) -> bool:
    """Record a failed attempt; return ``True`` when the job will be retried.

    Backoff is exponential in the number of attempts already made. When the
    attempt budget is exhausted the job moves to the terminal ``failed`` state
    instead of silently disappearing.
    """
    async with session.begin():
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.status != status.JOB_RUNNING or job.locked_by != worker_id:
            return False

        job.last_error = error[:2000]
        job.locked_at = None
        job.locked_by = None
        job.lease_expires_at = None

        if job.attempts >= job.max_attempts:
            job.status = status.JOB_FAILED
            return False

        delay = min(
            settings.job_retry_max_seconds,
            settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0)),
        )
        job.status = status.JOB_PENDING
        job.run_at = datetime.now(UTC) + timedelta(seconds=delay)
    return True


async def mark_job_failed(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    error: str,
) -> bool:
    """Move a claimed job straight to the terminal failed state."""
    statement = (
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
            last_error=error[:2000],
            updated_at=func.now(),
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    async with session.begin():
        failed = await session.scalar(statement)
    return failed is not None


async def reap_expired_jobs(session: AsyncSession, *, settings: Settings) -> int:
    """Return jobs whose worker died to the queue, or fail them terminally."""
    statement = (
        select(Job)
        .where(
            Job.status == status.JOB_RUNNING,
            or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < func.now()),
        )
        .with_for_update(skip_locked=True)
    )

    reaped = 0
    async with session.begin():
        for job in await session.scalars(statement):
            reaped += 1
            job.last_error = "lease expired; worker did not finish"
            job.locked_at = None
            job.locked_by = None
            job.lease_expires_at = None

            if job.attempts >= job.max_attempts:
                job.status = status.JOB_FAILED
                if job.type == status.JOB_GENERATE_DRAFT:
                    await session.execute(
                        update(Ticket)
                        .where(
                            Ticket.id == job.ticket_id,
                            Ticket.status == status.TICKET_GENERATING,
                        )
                        .values(
                            status=status.TICKET_GENERATION_FAILED,
                            updated_at=func.now(),
                        )
                    )
            else:
                job.status = status.JOB_PENDING
                job.run_at = datetime.now(UTC) + timedelta(seconds=settings.job_retry_base_seconds)
    return reaped
