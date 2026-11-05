"""Job queue: SKIP LOCKED claiming, leases, retries, and the reaper."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs, status
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Job, Ticket

settings = get_settings()


async def _create_ticket_with_job(
    *,
    external_id: str = "T-2001",
    max_attempts: int = 3,
    job_type: str = status.JOB_GENERATE_DRAFT,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with get_sessionmaker()() as session, session.begin():
        ticket = Ticket(
            external_id=external_id,
            customer_email="user@example.com",
            message="How do I export a report to PDF?",
            reply_url="https://helpdesk.example.com/tickets/T-2001/replies",
        )
        session.add(ticket)
        await session.flush()
        job = Job(
            type=job_type,
            ticket_id=ticket.id,
            max_attempts=max_attempts,
        )
        session.add(job)
        await session.flush()
        return ticket.id, job.id


async def _claim(*, worker_id: str = "worker-1", limit: int = 1) -> list[Job]:
    async with get_sessionmaker()() as session:
        return await jobs.claim_jobs(
            session,
            worker_id=worker_id,
            limit=limit,
            settings=settings,
        )


async def test_claim_marks_job_running_with_a_lease(session: AsyncSession) -> None:
    _, job_id = await _create_ticket_with_job()

    claimed = await _claim(worker_id="worker-1")

    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert claimed[0].status == status.JOB_RUNNING
    assert claimed[0].attempts == 1
    assert claimed[0].locked_by == "worker-1"
    assert claimed[0].locked_at is not None
    assert claimed[0].lease_expires_at is not None

    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_RUNNING


async def test_two_workers_never_claim_the_same_job(session: AsyncSession) -> None:
    _, job_id = await _create_ticket_with_job()

    async def claim(worker_id: str) -> list[Job]:
        async with get_sessionmaker()() as claim_session:
            return await jobs.claim_jobs(
                claim_session,
                worker_id=worker_id,
                limit=1,
                settings=settings,
            )

    results = await asyncio.gather(claim("worker-a"), claim("worker-b"))

    assert sum(len(result) for result in results) == 1
    winners = [result[0] for result in results if result]
    assert winners[0].id == job_id
    assert winners[0].locked_by in {"worker-a", "worker-b"}


async def test_two_workers_claim_different_jobs(session: AsyncSession) -> None:
    await _create_ticket_with_job(external_id="T-1")
    await _create_ticket_with_job(external_id="T-2")

    first = await _claim(worker_id="worker-a", limit=1)
    second = await _claim(worker_id="worker-b", limit=1)

    assert len(first) == 1 and len(second) == 1
    assert first[0].id != second[0].id
    assert {first[0].locked_by, second[0].locked_by} == {"worker-a", "worker-b"}


async def test_claim_only_picks_jobs_whose_run_at_has_arrived(session: AsyncSession) -> None:
    _, future_job = await _create_ticket_with_job(external_id="T-future")
    await session.execute(
        update(Job)
        .where(Job.id == future_job)
        .values(run_at=datetime.now(UTC) + timedelta(minutes=5))
    )
    await session.commit()

    claimed = await _claim()

    assert claimed == []


async def test_fail_job_requeues_with_backoff_then_fails_terminal(
    session: AsyncSession,
) -> None:
    _, job_id = await _create_ticket_with_job(max_attempts=2)
    await _claim()

    async with get_sessionmaker()() as fail_session:
        retrying = await jobs.fail_job(
            fail_session,
            job_id,
            worker_id="worker-1",
            error="provider exploded",
            settings=settings,
        )

    assert retrying is True
    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_PENDING
    assert stored.run_at > datetime.now(UTC)
    assert stored.last_error == "provider exploded"
    assert stored.locked_by is None

    # Second claim and second failure exhausts max_attempts=2.
    await session.execute(
        update(Job).where(Job.id == job_id).values(run_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    await _claim()
    async with get_sessionmaker()() as fail_session:
        retrying = await jobs.fail_job(
            fail_session,
            job_id,
            worker_id="worker-1",
            error="provider exploded again",
            settings=settings,
        )

    assert retrying is False
    session.expire_all()
    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_FAILED
    assert stored.locked_by is None


async def test_requeue_refunds_the_claim_attempt(session: AsyncSession) -> None:
    _, job_id = await _create_ticket_with_job()
    await _claim()

    async with get_sessionmaker()() as requeue_session:
        requeued = await jobs.requeue_job(
            requeue_session,
            job_id,
            worker_id="worker-1",
            delay_seconds=1.5,
            reason="llm rate limit",
            refund_attempt=True,
        )

    assert requeued is True
    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_PENDING
    assert stored.attempts == 0
    assert stored.run_at > datetime.now(UTC)
    assert stored.last_error == "llm rate limit"


async def test_reaper_returns_an_expired_lease_to_pending(session: AsyncSession) -> None:
    _, job_id = await _create_ticket_with_job()
    await _claim()
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()

    async with get_sessionmaker()() as reap_session:
        reaped = await jobs.reap_expired_jobs(reap_session, settings=settings)

    assert reaped == 1
    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_PENDING
    assert stored.locked_by is None
    assert "lease expired" in (stored.last_error or "")


async def test_reaper_fails_a_job_that_exhausted_its_attempts(session: AsyncSession) -> None:
    ticket_id, job_id = await _create_ticket_with_job(max_attempts=1)
    await _claim()
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.execute(
        update(Ticket).where(Ticket.id == ticket_id).values(status=status.TICKET_GENERATING)
    )
    await session.commit()

    async with get_sessionmaker()() as reap_session:
        reaped = await jobs.reap_expired_jobs(reap_session, settings=settings)

    assert reaped == 1
    stored = await session.get(Job, job_id)
    assert stored is not None
    assert stored.status == status.JOB_FAILED
    ticket = await session.get(Ticket, ticket_id)
    assert ticket is not None
    assert ticket.status == status.TICKET_GENERATION_FAILED
