"""Webhook ingestion: durability, idempotency, latency, validation, and 503s."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import status
from app.db import get_session
from app.main import app
from app.models import HelpdeskEvent, Job, Ticket

VALID_PAYLOAD: dict[str, Any] = {
    "event_id": "evt_8f2a",
    "ticket_id": "T-1042",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
}


async def _count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    return int(total or 0)


async def test_valid_webhook_persists_event_ticket_and_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    payload = {**VALID_PAYLOAD, "priority": "high"}

    response = await client.post("/webhooks/helpdesk", json=payload)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "duplicate": False}

    event = await session.scalar(select(HelpdeskEvent))
    assert event is not None
    assert event.event_id == "evt_8f2a"
    assert event.ticket_external_id == "T-1042"
    # The whole delivery is kept for audit, including fields we do not use.
    assert event.payload == payload

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    assert ticket.external_id == "T-1042"
    assert ticket.customer_email == "user@example.com"
    assert ticket.message == "Не можу експортувати звіт у PDF"
    assert ticket.reply_url == "https://helpdesk.example.com/tickets/T-1042/replies"
    assert ticket.status == status.TICKET_RECEIVED
    assert ticket.current_draft_id is None
    assert ticket.approved_at is None and ticket.rejected_at is None and ticket.sent_at is None

    job = await session.scalar(select(Job))
    assert job is not None
    assert job.type == status.JOB_GENERATE_DRAFT
    assert job.ticket_id == ticket.id
    assert job.status == status.JOB_PENDING
    assert job.attempts == 0
    assert job.payload == {"event_id": "evt_8f2a"}


async def test_webhook_answers_well_inside_helpdesk_timeout(client: AsyncClient) -> None:
    # Warm up the pool and the validation path; the helpdesk measures the real
    # request, not our first-ever connection.
    warmup = await client.post("/webhooks/helpdesk", json={**VALID_PAYLOAD, "event_id": "warmup"})
    assert warmup.status_code == 202

    latencies: list[float] = []
    for index in range(10):
        payload = {**VALID_PAYLOAD, "event_id": f"evt_latency_{index}"}
        started = time.perf_counter()
        response = await client.post("/webhooks/helpdesk", json=payload)
        latencies.append(time.perf_counter() - started)
        assert response.status_code == 202

    assert max(latencies) < 0.5, latencies


async def test_duplicate_event_is_ignored(client: AsyncClient, session: AsyncSession) -> None:
    first = await client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)
    second = await client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)

    assert first.status_code == second.status_code == 202
    assert first.json() == {"status": "accepted", "duplicate": False}
    assert second.json() == {"status": "accepted", "duplicate": True}

    assert await _count(session, HelpdeskEvent) == 1
    assert await _count(session, Ticket) == 1
    assert await _count(session, Job) == 1

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    assert ticket.status == status.TICKET_RECEIVED


async def test_concurrent_duplicates_create_one_event_ticket_and_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=VALID_PAYLOAD) for _ in range(20))
    )

    assert [response.status_code for response in responses] == [202] * 20
    duplicates = [response.json()["duplicate"] for response in responses]
    assert duplicates.count(False) == 1
    assert duplicates.count(True) == 19

    assert await _count(session, HelpdeskEvent) == 1
    assert await _count(session, Ticket) == 1
    assert await _count(session, Job) == 1


async def test_redelivery_after_job_finished_does_not_queue_another_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    assert (await client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)).status_code == 202

    # Pretend the worker already generated the draft.
    await session.execute(update(Job).values(status=status.JOB_DONE))
    await session.execute(update(Ticket).values(status=status.TICKET_DRAFT_READY))
    await session.commit()

    redelivery = await client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)

    assert redelivery.status_code == 202
    assert redelivery.json() == {"status": "accepted", "duplicate": True}
    assert await _count(session, Job) == 1
    assert await _count(session, HelpdeskEvent) == 1
    job = await session.scalar(select(Job))
    ticket = await session.scalar(select(Ticket))
    assert job is not None and job.status == status.JOB_DONE
    assert ticket is not None and ticket.status == status.TICKET_DRAFT_READY


async def test_new_event_for_known_ticket_keeps_one_ticket_and_one_active_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    first = await client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)
    second = await client.post(
        "/webhooks/helpdesk",
        json={
            **VALID_PAYLOAD,
            "event_id": "evt_second",
            "customer_email": "other@example.com",
            "message": "Still broken after the weekend",
            "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies?attempt=2",
        },
    )

    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is False

    assert await _count(session, HelpdeskEvent) == 2
    assert await _count(session, Ticket) == 1
    assert await _count(session, Job) == 1

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    # The first delivery owns the content; a later event must not rewrite it.
    assert ticket.message == "Не можу експортувати звіт у PDF"
    assert ticket.customer_email == "user@example.com"
    assert ticket.reply_url == "https://helpdesk.example.com/tickets/T-1042/replies"


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": ""},
        {"event_id": "e" * 129},
        {"ticket_id": "t" * 129},
        {"customer_email": "not-an-email"},
        {"customer_email": "a@" + "b" * 320},
        {"message": ""},
        {"message": "   \n\t "},
        {"message": "x" * 20_001},
        {"reply_url": ""},
        {"reply_url": "ftp://helpdesk.example.com/replies"},
        {"reply_url": "helpdesk.example.com/replies"},
        {"reply_url": "https:///replies"},
        {"reply_url": "https://user:secret@helpdesk.example.com/replies"},
        {"reply_url": "https://helpdesk.example.com:99999/replies"},
        {"reply_url": "https://helpdesk.example.com/" + "a" * 2000},
    ],
)
async def test_invalid_payload_is_rejected_without_side_effects(
    client: AsyncClient, session: AsyncSession, overrides: dict[str, Any]
) -> None:
    response = await client.post("/webhooks/helpdesk", json={**VALID_PAYLOAD, **overrides})

    assert response.status_code == 422
    assert await _count(session, HelpdeskEvent) == 0
    assert await _count(session, Ticket) == 0
    assert await _count(session, Job) == 0


async def test_missing_fields_are_rejected(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.post("/webhooks/helpdesk", json={"event_id": "evt_only"})

    assert response.status_code == 422
    assert await _count(session, HelpdeskEvent) == 0


async def test_webhook_returns_503_when_database_is_unavailable() -> None:
    unreachable_engine = create_async_engine(
        "postgresql+asyncpg://app:app@127.0.0.1:1/app",
        connect_args={"timeout": 1},
        pool_timeout=1,
    )
    unreachable_sessions = async_sessionmaker(unreachable_engine, expire_on_commit=False)

    async def failing_session() -> AsyncIterator[AsyncSession]:
        async with unreachable_sessions() as session:
            yield session

    app.dependency_overrides[get_session] = failing_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.post("/webhooks/helpdesk", json=VALID_PAYLOAD)
    finally:
        app.dependency_overrides.clear()
        await unreachable_engine.dispose()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": "database unavailable"}
