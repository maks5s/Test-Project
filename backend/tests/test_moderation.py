"""Moderation API: idempotent approve/reject/regenerate and ticket views."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import status
from app.db import get_sessionmaker
from app.models import Draft, Job, OutboundMessage, Ticket

REPLY_URL = "https://helpdesk.example.com/tickets/T-5001/replies"


async def _create_ticket(
    *,
    external_id: str = "T-5001",
    ticket_status: str = status.TICKET_DRAFT_READY,
    with_draft: bool = True,
    message: str = "Need help with PDF export",
) -> tuple[uuid.UUID, uuid.UUID | None]:
    async with get_sessionmaker()() as session, session.begin():
        ticket = Ticket(
            external_id=external_id,
            customer_email="user@example.com",
            message=message,
            reply_url=REPLY_URL,
            status=ticket_status,
        )
        session.add(ticket)
        await session.flush()

        draft_id: uuid.UUID | None = None
        if with_draft:
            draft = Draft(
                ticket_id=ticket.id,
                version=1,
                content="Open the report and choose Export as PDF.",
                citations=[
                    {"slug": "exporting-reports-to-pdf", "title": "Exporting reports to PDF"}
                ],
                source=status.DRAFT_SOURCE_LLM,
                model="gpt-4o-mini",
                latency_ms=1200,
            )
            session.add(draft)
            await session.flush()
            draft_id = draft.id
            ticket.current_draft_id = draft.id

        return ticket.id, draft_id


async def _count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    return int(total or 0)


async def test_list_tickets_returns_total_and_filters(client: AsyncClient) -> None:
    await _create_ticket(external_id="T-1", ticket_status=status.TICKET_RECEIVED, with_draft=False)
    await _create_ticket(external_id="T-2", ticket_status=status.TICKET_DRAFT_READY)

    response = await client.get("/api/tickets")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["external_id"] for item in body["items"]} == {"T-1", "T-2"}
    assert all("current_draft" in item and "outbound" in item for item in body["items"])

    filtered = await client.get("/api/tickets", params={"status": status.TICKET_DRAFT_READY})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["external_id"] == "T-2"


async def test_list_tickets_rejects_an_unknown_status(client: AsyncClient) -> None:
    response = await client.get("/api/tickets", params={"status": "banana"})

    assert response.status_code == 422


async def test_ticket_detail_includes_all_drafts_and_delivery(client: AsyncClient) -> None:
    ticket_id, draft_id = await _create_ticket()

    response = await client.get(f"/api/tickets/{ticket_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(ticket_id)
    assert body["current_draft"]["id"] == str(draft_id)
    assert len(body["drafts"]) == 1
    assert body["drafts"][0]["citations"][0]["slug"] == "exporting-reports-to-pdf"
    assert body["outbound"] is None


async def test_unknown_ticket_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/tickets/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_approve_creates_outbox_and_send_job_once(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, draft_id = await _create_ticket()

    response = await client.post(f"/api/tickets/{ticket_id}/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status.TICKET_APPROVED
    assert body["approved_at"] is not None
    assert body["outbound"]["status"] == status.OUTBOUND_PENDING
    assert body["outbound"]["idempotency_key"] == f"ticket:T-5001:draft:{draft_id}"
    assert body["outbound"]["attempts"] == 0

    outbound = await session.scalar(select(OutboundMessage))
    assert outbound is not None
    assert outbound.ticket_id == ticket_id
    assert outbound.draft_id == draft_id
    assert outbound.payload == {
        "ticket_id": "T-5001",
        "message": "Open the report and choose Export as PDF.",
    }

    job = await session.scalar(select(Job))
    assert job is not None
    assert job.type == status.JOB_SEND_REPLY
    assert job.status == status.JOB_PENDING
    assert job.payload == {"outbound_id": str(outbound.id)}

    repeated = await client.post(f"/api/tickets/{ticket_id}/approve")
    assert repeated.status_code == 200
    assert repeated.json()["status"] == status.TICKET_APPROVED
    assert await _count(session, OutboundMessage) == 1
    assert await _count(session, Job) == 1


async def test_concurrent_approvals_create_one_outbound_and_one_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, _ = await _create_ticket()

    responses = await asyncio.gather(
        *(client.post(f"/api/tickets/{ticket_id}/approve") for _ in range(10))
    )

    assert [response.status_code for response in responses] == [200] * 10
    assert await _count(session, OutboundMessage) == 1
    assert await _count(session, Job) == 1


async def test_approve_requires_a_ready_draft(client: AsyncClient) -> None:
    ticket_id, _ = await _create_ticket(
        external_id="T-not-ready",
        ticket_status=status.TICKET_RECEIVED,
        with_draft=False,
    )

    response = await client.post(f"/api/tickets/{ticket_id}/approve")

    assert response.status_code == 409
    assert "not 'draft_ready'" in response.json()["detail"]


async def test_reject_is_idempotent_and_prevents_delivery(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, _ = await _create_ticket()

    first = await client.post(f"/api/tickets/{ticket_id}/reject")
    second = await client.post(f"/api/tickets/{ticket_id}/reject")

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == status.TICKET_REJECTED
    assert first.json()["rejected_at"] is not None
    assert await _count(session, OutboundMessage) == 0
    assert await _count(session, Job) == 0

    approve = await client.post(f"/api/tickets/{ticket_id}/approve")
    assert approve.status_code == 409


async def test_reject_requires_a_ready_draft(client: AsyncClient) -> None:
    ticket_id, _ = await _create_ticket(
        external_id="T-reject-received",
        ticket_status=status.TICKET_RECEIVED,
        with_draft=False,
    )

    response = await client.post(f"/api/tickets/{ticket_id}/reject")

    assert response.status_code == 409


async def test_regenerate_queues_one_generation_and_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, _ = await _create_ticket()

    first = await client.post(f"/api/tickets/{ticket_id}/regenerate")
    second = await client.post(f"/api/tickets/{ticket_id}/regenerate")

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == status.TICKET_GENERATING
    jobs = list(await session.scalars(select(Job)))
    assert len(jobs) == 1
    assert jobs[0].type == status.JOB_GENERATE_DRAFT
    assert jobs[0].status == status.JOB_PENDING
    assert jobs[0].payload == {"reason": "regenerate"}


async def test_regenerate_is_allowed_after_rejection_and_failure(
    client: AsyncClient, session: AsyncSession
) -> None:
    rejected_id, _ = await _create_ticket(
        external_id="T-rejected", ticket_status=status.TICKET_REJECTED
    )
    failed_id, _ = await _create_ticket(
        external_id="T-failed", ticket_status=status.TICKET_GENERATION_FAILED
    )

    rejected = await client.post(f"/api/tickets/{rejected_id}/regenerate")
    failed = await client.post(f"/api/tickets/{failed_id}/regenerate")

    assert rejected.status_code == failed.status_code == 200
    assert rejected.json()["status"] == status.TICKET_GENERATING
    assert failed.json()["status"] == status.TICKET_GENERATING
    assert await _count(session, Job) == 2


async def test_regenerate_is_rejected_after_approval(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, _ = await _create_ticket()
    approve = await client.post(f"/api/tickets/{ticket_id}/approve")
    assert approve.status_code == 200

    response = await client.post(f"/api/tickets/{ticket_id}/regenerate")

    assert response.status_code == 409
    jobs = list(await session.scalars(select(Job)))
    assert [job.type for job in jobs] == [status.JOB_SEND_REPLY]


async def test_retry_delivery_requeues_a_failed_send(
    client: AsyncClient, session: AsyncSession
) -> None:
    ticket_id, draft_id = await _create_ticket(external_id="T-retry")
    async with get_sessionmaker()() as setup, setup.begin():
        ticket = await setup.get(Ticket, ticket_id)
        assert ticket is not None
        ticket.status = status.TICKET_SEND_FAILED
        draft = await setup.get(Draft, draft_id)
        assert draft is not None
        outbound = OutboundMessage(
            ticket_id=ticket_id,
            draft_id=draft.id,
            payload={"ticket_id": ticket.external_id, "message": draft.content},
            status=status.OUTBOUND_FAILED,
            idempotency_key=f"ticket:{ticket.external_id}:draft:{draft.id}",
            attempts=5,
            last_error="helpdesk returned 500",
        )
        setup.add(outbound)
        await setup.flush()
        setup.add(
            Job(
                type=status.JOB_SEND_REPLY,
                ticket_id=ticket_id,
                payload={"outbound_id": str(outbound.id)},
                status=status.JOB_FAILED,
                attempts=5,
            )
        )

    response = await client.post(f"/api/tickets/{ticket_id}/retry-delivery")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status.TICKET_APPROVED
    assert body["outbound"]["status"] == status.OUTBOUND_PENDING
    assert body["outbound"]["attempts"] == 0
    active = list(
        await session.scalars(
            select(Job).where(
                Job.type == status.JOB_SEND_REPLY,
                Job.status == status.JOB_PENDING,
            )
        )
    )
    assert len(active) == 1
