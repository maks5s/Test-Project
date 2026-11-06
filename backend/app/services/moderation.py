"""Operator moderation: approve, reject, regenerate, retry delivery.

Every transition is a conditional update of one ticket row locked with
``FOR UPDATE``. Repeated clicks and concurrent requests therefore converge on
the same state instead of creating a second outbound message, a second send job,
or a second generation job.

Approval writes the transactional outbox row and the send job in the same
transaction that flips the ticket to ``approved``, so an accepted reply can
never be lost between those steps.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import status
from app.config import Settings
from app.models import Draft, Job, OutboundMessage, Ticket
from app.services.ingestion import JOB_ACTIVE_PREDICATE


class TicketNotFoundError(Exception):
    """No ticket exists for the requested id."""


class ModerationConflictError(Exception):
    """The ticket is not in a state that allows the requested action."""


_APPROVED_STATUSES = (status.TICKET_APPROVED, status.TICKET_SENDING, status.TICKET_SENT)
_REGENERATABLE_STATUSES = (
    status.TICKET_DRAFT_READY,
    status.TICKET_GENERATION_FAILED,
    status.TICKET_REJECTED,
    status.TICKET_GENERATING,
)


async def approve_ticket(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    *,
    settings: Settings,
) -> None:
    """Queue the current draft for delivery exactly once."""
    async with session.begin():
        ticket = await _lock_ticket(session, ticket_id)

        if ticket.status in _APPROVED_STATUSES:
            # Double click, retry after a timeout, or a concurrent request that
            # lost the race: the work is already queued or done.
            return
        if ticket.status != status.TICKET_DRAFT_READY:
            raise ModerationConflictError(
                f"ticket is {ticket.status!r}, not {status.TICKET_DRAFT_READY!r}"
            )
        if ticket.current_draft_id is None:
            raise ModerationConflictError("ticket has no current draft")

        draft = await session.get(Draft, ticket.current_draft_id)
        if draft is None or draft.ticket_id != ticket.id:
            raise ModerationConflictError("ticket current draft is missing")

        idempotency_key = _idempotency_key(ticket.external_id, draft.id)
        outbound_id = await _insert_outbound(session, ticket, draft, idempotency_key)
        await _insert_send_job(session, ticket.id, outbound_id, settings)

        ticket.status = status.TICKET_APPROVED
        if ticket.approved_at is None:
            ticket.approved_at = datetime.now(UTC)


async def reject_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> None:
    """Reject a ready draft; repeated rejection is a no-op."""
    async with session.begin():
        ticket = await _lock_ticket(session, ticket_id)

        if ticket.status == status.TICKET_REJECTED:
            return
        if ticket.status != status.TICKET_DRAFT_READY:
            raise ModerationConflictError(
                f"ticket is {ticket.status!r}, not {status.TICKET_DRAFT_READY!r}"
            )

        ticket.status = status.TICKET_REJECTED
        ticket.rejected_at = datetime.now(UTC)


async def regenerate_ticket(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    *,
    settings: Settings,
) -> None:
    """Queue another draft generation for a ticket the operator may edit."""
    async with session.begin():
        ticket = await _lock_ticket(session, ticket_id)

        if ticket.status == status.TICKET_GENERATING:
            # A generation job is already active; this call is idempotent.
            return
        if ticket.status not in _REGENERATABLE_STATUSES:
            raise ModerationConflictError(f"ticket is {ticket.status!r} and cannot be regenerated")

        ticket.status = status.TICKET_GENERATING
        ticket.rejected_at = None
        await _enqueue_generation(session, ticket.id, settings)


async def retry_delivery(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    *,
    settings: Settings,
) -> None:
    """Put a terminally failed delivery back on the queue once more."""
    async with session.begin():
        ticket = await _lock_ticket(session, ticket_id)
        outbound = await session.scalar(
            select(OutboundMessage).where(OutboundMessage.ticket_id == ticket.id).with_for_update()
        )

        if ticket.status == status.TICKET_SENT or (
            outbound is not None and outbound.status == status.OUTBOUND_SENT
        ):
            return
        if (
            ticket.status != status.TICKET_SEND_FAILED
            or outbound is None
            or outbound.status != status.OUTBOUND_FAILED
        ):
            raise ModerationConflictError("ticket has no failed delivery to retry")

        outbound.status = status.OUTBOUND_PENDING
        outbound.attempts = 0
        outbound.last_error = None
        ticket.status = status.TICKET_APPROVED
        await _insert_send_job(session, ticket.id, outbound.id, settings)


async def _lock_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket:
    ticket = await session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if ticket is None:
        raise TicketNotFoundError(str(ticket_id))
    return ticket


def _idempotency_key(ticket_external_id: str, draft_id: uuid.UUID) -> str:
    return f"ticket:{ticket_external_id}:draft:{draft_id}"


async def _insert_outbound(
    session: AsyncSession,
    ticket: Ticket,
    draft: Draft,
    idempotency_key: str,
) -> uuid.UUID:
    statement = (
        pg_insert(OutboundMessage)
        .values(
            ticket_id=ticket.id,
            draft_id=draft.id,
            payload={"ticket_id": ticket.external_id, "message": draft.content},
            status=status.OUTBOUND_PENDING,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[OutboundMessage.ticket_id])
        .returning(OutboundMessage.id)
    )
    inserted = await session.scalar(statement)
    if inserted is not None:
        return inserted

    existing = await session.scalar(
        select(OutboundMessage.id).where(OutboundMessage.ticket_id == ticket.id)
    )
    if existing is None:  # pragma: no cover - conflict implies an existing row
        raise ModerationConflictError("could not create the outbound message")
    return existing


async def _insert_send_job(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    outbound_id: uuid.UUID,
    settings: Settings,
) -> None:
    statement = (
        pg_insert(Job)
        .values(
            type=status.JOB_SEND_REPLY,
            ticket_id=ticket_id,
            payload={"outbound_id": str(outbound_id)},
            status=status.JOB_PENDING,
            max_attempts=settings.delivery_max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=[Job.type, Job.ticket_id],
            index_where=text(JOB_ACTIVE_PREDICATE),
        )
    )
    await session.execute(statement)


async def _enqueue_generation(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    settings: Settings,
) -> None:
    statement = (
        pg_insert(Job)
        .values(
            type=status.JOB_GENERATE_DRAFT,
            ticket_id=ticket_id,
            payload={"reason": "regenerate"},
            status=status.JOB_PENDING,
            max_attempts=settings.job_max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=[Job.type, Job.ticket_id],
            index_where=text(JOB_ACTIVE_PREDICATE),
        )
    )
    await session.execute(statement)


__all__ = [
    "ModerationConflictError",
    "TicketNotFoundError",
    "approve_ticket",
    "regenerate_ticket",
    "reject_ticket",
    "retry_delivery",
]
