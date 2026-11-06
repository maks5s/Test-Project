"""Dev simulator: one request should behave exactly like a helpdesk webhook."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import status
from app.config import get_settings
from app.main import app
from app.models import HelpdeskEvent, Job, Ticket


async def _count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    return int(total or 0)


async def test_simulated_ticket_creates_event_ticket_and_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post("/api/dev/tickets", json={})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["duplicate"] is False
    assert body["external_id"].startswith("T-")
    assert body["event_id"].startswith("evt-")
    assert body["reply_url"] == (
        f"http://api:8000/mock/helpdesk/tickets/{body['external_id']}/replies"
    )

    assert await _count(session, HelpdeskEvent) == 1
    assert await _count(session, Ticket) == 1
    job = await session.scalar(select(Job))
    assert job is not None
    assert job.type == status.JOB_GENERATE_DRAFT
    assert job.status == status.JOB_PENDING


async def test_simulated_ticket_accepts_explicit_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(
        "/api/dev/tickets",
        json={
            "event_id": "evt-demo-1",
            "ticket_id": "T-DEMO-1",
            "customer_email": "someone@example.com",
            "message": "Where do I download the invoice?",
        },
    )

    assert response.status_code == 202
    assert response.json()["external_id"] == "T-DEMO-1"
    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    assert ticket.customer_email == "someone@example.com"
    assert ticket.message == "Where do I download the invoice?"


async def test_repeating_the_generated_event_id_is_idempotent(
    client: AsyncClient, session: AsyncSession
) -> None:
    payload = {"event_id": "evt-repeat", "ticket_id": "T-REPEAT"}

    first = await client.post("/api/dev/tickets", json=payload)
    second = await client.post("/api/dev/tickets", json=payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert first.json()["ticket_id"] == second.json()["ticket_id"]
    assert await _count(session, Ticket) == 1
    assert await _count(session, Job) == 1


async def test_simulator_validates_overrides(client: AsyncClient) -> None:
    response = await client.post(
        "/api/dev/tickets",
        json={"customer_email": "not-an-email"},
    )

    assert response.status_code == 422

    unknown = await client.post("/api/dev/tickets", json={"unknown": "field"})
    assert unknown.status_code == 422


async def test_simulator_is_hidden_when_dev_endpoints_are_disabled(client: AsyncClient) -> None:
    disabled = get_settings().model_copy(update={"enable_dev_endpoints": False})
    app.dependency_overrides[get_settings] = lambda: disabled
    try:
        response = await client.post("/api/dev/tickets", json={})
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 404
    assert response.json()["detail"] == "dev endpoints are disabled"
