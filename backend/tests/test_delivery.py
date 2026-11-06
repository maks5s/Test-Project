"""Reply delivery: outbox, idempotency, retries, SSRF limits, and mock inbox."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs, status
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import Draft, Job, MockHelpdeskReply, OutboundMessage, Ticket
from app.services.delivery import process_send_job

settings = get_settings()
MOCK_URL = "http://api/mock/helpdesk/tickets/{ticket}/replies"


def _settings(**overrides: Any) -> Settings:
    updates: dict[str, Any] = {
        "helpdesk_timeout_seconds": 2.0,
        "delivery_max_attempts": 5,
        "delivery_backoff_base_seconds": 0.0,
        "delivery_backoff_max_seconds": 0.0,
        "delivery_jitter_seconds": 0.0,
        "reply_url_allowed_hosts": "api,localhost,127.0.0.1",
    }
    updates.update(overrides)
    return get_settings().model_copy(update=updates)


async def _prepare(
    *,
    external_id: str = "T-6001",
    reply_url: str | None = None,
    ticket_status: str = status.TICKET_APPROVED,
) -> tuple[Job, uuid.UUID, uuid.UUID]:
    target = reply_url or MOCK_URL.format(ticket=external_id)
    async with get_sessionmaker()() as session, session.begin():
        ticket = Ticket(
            external_id=external_id,
            customer_email="user@example.com",
            message="Need help with PDF export",
            reply_url=target,
            status=ticket_status,
        )
        session.add(ticket)
        await session.flush()
        draft = Draft(
            ticket_id=ticket.id,
            version=1,
            content="Open the report and choose Export as PDF.",
            citations=[{"slug": "exporting-reports-to-pdf", "title": "Exporting reports to PDF"}],
            source=status.DRAFT_SOURCE_LLM,
        )
        session.add(draft)
        await session.flush()
        ticket.current_draft_id = draft.id
        outbound = OutboundMessage(
            ticket_id=ticket.id,
            draft_id=draft.id,
            payload={"ticket_id": ticket.external_id, "message": draft.content},
            status=status.OUTBOUND_PENDING,
            idempotency_key=f"ticket:{ticket.external_id}:draft:{draft.id}",
        )
        session.add(outbound)
        await session.flush()
        session.add(
            Job(
                type=status.JOB_SEND_REPLY,
                ticket_id=ticket.id,
                payload={"outbound_id": str(outbound.id)},
                max_attempts=5,
            )
        )
        ticket_id, draft_id = ticket.id, draft.id

    async with get_sessionmaker()() as session:
        claimed = await jobs.claim_jobs(
            session,
            worker_id="delivery-worker",
            limit=1,
            settings=settings,
            job_types=(status.JOB_SEND_REPLY,),
        )
    assert len(claimed) == 1
    return claimed[0], ticket_id, draft_id


async def _run(job: Job, *, delivery_settings: Settings | None = None, client: Any = None) -> None:
    await process_send_job(
        job,
        session_factory=get_sessionmaker(),
        settings=delivery_settings or settings,
        worker_id="delivery-worker",
        http_client=client,
    )


async def _reclaim(job_id: uuid.UUID) -> Job:
    """Make a requeued job immediately claimable again."""
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(update(Job).where(Job.id == job_id).values(run_at=func.now()))

    async with get_sessionmaker()() as session:
        claimed = await jobs.claim_jobs(
            session,
            worker_id="delivery-worker",
            limit=1,
            settings=settings,
            job_types=(status.JOB_SEND_REPLY,),
        )
    assert len(claimed) == 1
    return claimed[0]


async def _count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    return int(total or 0)


async def test_successful_delivery_marks_everything_sent_and_reaches_the_mock(
    session: AsyncSession,
) -> None:
    job, ticket_id, _ = await _prepare(external_id="T-6101")
    http_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://api")
    try:
        await _run(job, delivery_settings=_settings(), client=http_client)
    finally:
        await http_client.aclose()

    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_SENT
    assert outbound.attempts == 1
    assert outbound.sent_at is not None

    ticket = await session.get(Ticket, ticket_id)
    assert ticket is not None
    assert ticket.status == status.TICKET_SENT
    assert ticket.sent_at is not None

    stored_job = await session.get(Job, job.id)
    assert stored_job is not None
    assert stored_job.status == status.JOB_DONE

    reply = await session.scalar(select(MockHelpdeskReply))
    assert reply is not None
    assert reply.ticket_external_id == "T-6101"
    assert reply.idempotency_key == outbound.idempotency_key
    assert reply.payload == {
        "ticket_id": "T-6101",
        "message": "Open the report and choose Export as PDF.",
    }


@respx.mock
async def test_5xx_is_retried_with_the_same_idempotency_key(
    session: AsyncSession,
) -> None:
    job, ticket_id, _ = await _prepare(external_id="T-6102")
    route = respx.post(MOCK_URL.format(ticket="T-6102")).mock(
        side_effect=[httpx.Response(503), httpx.Response(201, json={"ok": True})]
    )

    await _run(job, delivery_settings=_settings())

    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_PENDING
    assert outbound.attempts == 1
    assert outbound.last_error is not None and "503" in outbound.last_error
    stored_job = await session.get(Job, job.id)
    assert stored_job is not None and stored_job.status == status.JOB_PENDING
    ticket = await session.get(Ticket, ticket_id)
    assert ticket is not None and ticket.status == status.TICKET_APPROVED

    second = await _reclaim(job.id)
    await _run(second, delivery_settings=_settings())

    assert route.call_count == 2
    first_key = route.calls[0].request.headers["idempotency-key"]
    second_key = route.calls[1].request.headers["idempotency-key"]
    assert first_key == second_key
    assert first_key == outbound.idempotency_key

    await session.refresh(outbound)
    assert outbound.status == status.OUTBOUND_SENT
    assert outbound.attempts == 2
    await session.refresh(stored_job)
    assert stored_job.status == status.JOB_DONE

    await session.refresh(ticket)
    assert ticket.status == status.TICKET_SENT


@respx.mock
async def test_timeout_is_retried_with_the_same_key(session: AsyncSession) -> None:
    job, _, _ = await _prepare(external_id="T-6103")
    route = respx.post(MOCK_URL.format(ticket="T-6103")).mock(
        side_effect=[httpx.ReadTimeout("slow helpdesk"), httpx.Response(201)]
    )

    await _run(job, delivery_settings=_settings())
    second = await _reclaim(job.id)
    await _run(second, delivery_settings=_settings())

    assert route.call_count == 2
    assert (
        route.calls[0].request.headers["idempotency-key"]
        == route.calls[1].request.headers["idempotency-key"]
    )
    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None and outbound.status == status.OUTBOUND_SENT


@respx.mock
async def test_delivery_is_failed_after_the_attempt_budget(session: AsyncSession) -> None:
    job, ticket_id, _ = await _prepare(external_id="T-6104")
    route = respx.post(MOCK_URL.format(ticket="T-6104")).mock(return_value=httpx.Response(500))

    await _run(job, delivery_settings=_settings(delivery_max_attempts=1))

    assert route.call_count == 1
    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_FAILED
    assert outbound.attempts == 1
    assert "500" in (outbound.last_error or "")
    ticket = await session.get(Ticket, ticket_id)
    assert ticket is not None and ticket.status == status.TICKET_SEND_FAILED
    stored_job = await session.get(Job, job.id)
    assert stored_job is not None and stored_job.status == status.JOB_FAILED


@respx.mock
async def test_client_error_is_terminal_without_retry(session: AsyncSession) -> None:
    job, _, _ = await _prepare(external_id="T-6105")
    route = respx.post(MOCK_URL.format(ticket="T-6105")).mock(
        return_value=httpx.Response(400, json={"error": "bad request"})
    )

    await _run(job, delivery_settings=_settings())

    assert route.call_count == 1
    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_FAILED
    assert "400" in (outbound.last_error or "")


@respx.mock
async def test_redirects_are_not_followed(session: AsyncSession) -> None:
    job, _, _ = await _prepare(external_id="T-6106")
    redirect = respx.post(MOCK_URL.format(ticket="T-6106")).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.test/replies"})
    )
    evil = respx.post("https://evil.test/replies").mock(return_value=httpx.Response(201))

    await _run(job, delivery_settings=_settings())

    assert redirect.call_count == 1
    assert evil.call_count == 0
    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_FAILED
    assert "redirects are not followed" in (outbound.last_error or "")


@respx.mock
async def test_host_outside_the_allowlist_is_blocked_before_http(
    session: AsyncSession,
) -> None:
    job, _, _ = await _prepare(
        external_id="T-6107",
        reply_url="https://evil.test/replies",
    )
    route = respx.post("https://evil.test/replies").mock(return_value=httpx.Response(201))

    await _run(job, delivery_settings=_settings())

    assert route.call_count == 0
    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.status == status.OUTBOUND_FAILED
    assert "not allowed" in (outbound.last_error or "")


async def test_mock_helpdesk_deduplicates_by_idempotency_key(
    client: AsyncClient, session: AsyncSession
) -> None:
    payload = {"ticket_id": "T-7001", "message": "Reply from the service"}

    first = await client.post(
        "/mock/helpdesk/tickets/T-7001/replies",
        json=payload,
        headers={"Idempotency-Key": "key-7001"},
    )
    second = await client.post(
        "/mock/helpdesk/tickets/T-7001/replies",
        json=payload,
        headers={"Idempotency-Key": "key-7001"},
    )
    other = await client.post(
        "/mock/helpdesk/tickets/T-7001/replies",
        json=payload,
        headers={"Idempotency-Key": "key-7002"},
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["id"] == second.json()["id"]
    assert other.status_code == 201
    assert await _count(session, MockHelpdeskReply) == 2

    listing = await client.get("/mock/helpdesk/replies")
    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    assert {item["idempotency_key"] for item in listing.json()["items"]} == {
        "key-7001",
        "key-7002",
    }


async def test_mock_helpdesk_requires_an_idempotency_key(client: AsyncClient) -> None:
    response = await client.post(
        "/mock/helpdesk/tickets/T-7002/replies",
        json={"ticket_id": "T-7002", "message": "hello"},
    )

    assert response.status_code == 422


async def test_mock_helpdesk_rejects_a_ticket_mismatch(client: AsyncClient) -> None:
    response = await client.post(
        "/mock/helpdesk/tickets/T-7003/replies",
        json={"ticket_id": "T-other", "message": "hello"},
        headers={"Idempotency-Key": "key-7003"},
    )

    assert response.status_code == 409
