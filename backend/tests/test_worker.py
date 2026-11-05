"""Worker loop: end-to-end claim, generate, and stop conditions."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import status
from app.config import Settings, get_settings
from app.models import Draft, Job, Ticket
from app.seed import seed_articles
from app.worker import Worker

ENDPOINT = "https://llm.test/v1/chat/completions"

WEBHOOK = {
    "event_id": "evt_worker_1",
    "ticket_id": "T-4001",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "https://helpdesk.example.com/tickets/T-4001/replies",
}


def _settings() -> Settings:
    return get_settings().model_copy(
        update={
            "llm_base_url": "https://llm.test/v1",
            "llm_api_key": "test-key",
            "llm_max_retries": 0,
            "llm_retry_backoff_seconds": 0.0,
            "llm_simulated_delay_min": 0.0,
            "llm_simulated_delay_max": 0.0,
            "worker_concurrency": 4,
            "worker_poll_interval_seconds": 0.05,
            "job_reaper_interval_seconds": 3600.0,
        }
    )


@respx.mock
async def test_worker_processes_one_webhook_ticket(
    client: AsyncClient, session: AsyncSession
) -> None:
    await seed_articles()
    response = await client.post("/webhooks/helpdesk", json=WEBHOOK)
    assert response.status_code == 202

    success = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Use Share > Export as PDF."},
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 8},
    }
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=success))

    worker = Worker(settings=_settings())
    try:
        processed = await worker.run_once()
    finally:
        await worker.aclose()

    assert processed == 1
    assert route.call_count == 1

    ticket = await session.scalar(select(Ticket))
    assert ticket is not None
    assert ticket.status == status.TICKET_DRAFT_READY

    draft = await session.scalar(select(Draft))
    assert draft is not None
    assert draft.source == status.DRAFT_SOURCE_LLM
    assert draft.citations

    job = await session.scalar(select(Job))
    assert job is not None
    assert job.status == status.JOB_DONE


async def test_run_once_returns_zero_when_the_queue_is_empty() -> None:
    worker = Worker(settings=_settings())
    try:
        assert await worker.run_once() == 0
    finally:
        await worker.aclose()
