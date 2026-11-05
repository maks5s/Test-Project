"""Durable, idempotent webhook ingestion.

The helpdesk waits at most two seconds and retries whatever it does not hear
back, so this module only writes rows in one short transaction. It never calls
the knowledge base, an LLM, or the helpdesk API: generation and delivery are
the worker's job.

At-least-once delivery means duplicates are the normal case. Three database
constraints make repeated (including concurrent) deliveries safe:

* ``helpdesk_events.event_id`` is unique - one audit row per delivery id;
* ``tickets.external_id`` is unique - one ticket per helpdesk ticket id;
* ``uq_jobs_active`` is a partial unique index - at most one pending/running
  generation job per ticket.

All three writes are ``INSERT ... ON CONFLICT``, so the loser of a race does
not fail and does not create a second row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import status
from app.config import Settings
from app.models import HelpdeskEvent, Job, Ticket
from app.schemas import HelpdeskWebhookPayload

JOB_ACTIVE_PREDICATE = "status IN ('pending', 'running')"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one webhook delivery."""

    ticket_id: uuid.UUID
    duplicate: bool
    job_id: uuid.UUID | None


async def ingest_webhook(
    session: AsyncSession,
    payload: HelpdeskWebhookPayload,
    settings: Settings,
) -> IngestResult:
    """Persist event, ticket, and generation job atomically, then commit."""
    async with session.begin():
        first_delivery = await _insert_event(session, payload)
        ticket_id = await _upsert_ticket(session, payload)
        # A duplicate event means the first delivery already queued generation
        # in the same committed transaction. Do not enqueue work again: an
        # identical redelivery must be a no-op even after the job finished.
        job_id = (
            await _enqueue_generation(session, ticket_id, payload, settings)
            if first_delivery
            else None
        )

    return IngestResult(ticket_id=ticket_id, duplicate=not first_delivery, job_id=job_id)


async def _insert_event(session: AsyncSession, payload: HelpdeskWebhookPayload) -> bool:
    """Store the raw delivery; return ``False`` when the event is a duplicate."""
    statement = (
        pg_insert(HelpdeskEvent)
        .values(
            event_id=payload.event_id,
            ticket_external_id=payload.ticket_id,
            payload=payload.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=[HelpdeskEvent.event_id])
        .returning(HelpdeskEvent.id)
    )
    inserted = await session.scalar(statement)
    return inserted is not None


async def _upsert_ticket(session: AsyncSession, payload: HelpdeskWebhookPayload) -> uuid.UUID:
    """Create the ticket, or keep the existing one untouched.

    A later event for an already known ticket must not overwrite the original
    message or reply target; the worker still has one generation job queued.
    """
    statement = (
        pg_insert(Ticket)
        .values(
            external_id=payload.ticket_id,
            customer_email=payload.customer_email,
            message=payload.message,
            reply_url=payload.reply_url,
            status=status.TICKET_RECEIVED,
        )
        .on_conflict_do_update(
            index_elements=[Ticket.external_id],
            set_={"updated_at": Ticket.updated_at},
        )
        .returning(Ticket.id)
    )
    ticket_id: uuid.UUID | None = await session.scalar(statement)
    if ticket_id is None:  # pragma: no cover - RETURNING always yields a row here
        raise RuntimeError("ticket upsert returned no row")
    return ticket_id


async def _enqueue_generation(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    payload: HelpdeskWebhookPayload,
    settings: Settings,
) -> uuid.UUID | None:
    """Queue draft generation unless an active job already exists."""
    statement = (
        pg_insert(Job)
        .values(
            type=status.JOB_GENERATE_DRAFT,
            ticket_id=ticket_id,
            payload={"event_id": payload.event_id},
            status=status.JOB_PENDING,
            max_attempts=settings.job_max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=[Job.type, Job.ticket_id],
            index_where=text(JOB_ACTIVE_PREDICATE),
        )
        .returning(Job.id)
    )
    job_id: uuid.UUID | None = await session.scalar(statement)
    return job_id
