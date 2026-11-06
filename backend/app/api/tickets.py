"""Operator-facing ticket API: list, detail, and moderation actions."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import status
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Draft, Ticket
from app.schemas import DraftRead, OutboundRead, TicketDetail, TicketList, TicketRead
from app.services.moderation import (
    ModerationConflictError,
    TicketNotFoundError,
    approve_ticket,
    regenerate_ticket,
    reject_ticket,
    retry_delivery,
)

router = APIRouter(prefix="/api", tags=["tickets"])


@router.get("/tickets", response_model=TicketList, summary="List tickets for the operator")
async def list_tickets(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketList:
    filters: list[Any] = []
    if status_filter is not None:
        if status_filter not in status.TICKET_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"unknown ticket status: {status_filter}",
            )
        filters.append(Ticket.status == status_filter)

    total = await session.scalar(select(func.count()).select_from(Ticket).where(*filters))
    tickets = list(
        await session.scalars(
            select(Ticket)
            .where(*filters)
            .order_by(Ticket.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return TicketList(items=[_ticket_read(ticket) for ticket in tickets], total=int(total or 0))


@router.get("/tickets/{ticket_id}", response_model=TicketDetail, summary="Ticket detail")
async def get_ticket(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketDetail:
    ticket = await _load_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="ticket not found",
        )
    return _ticket_detail(ticket)


@router.post(
    "/tickets/{ticket_id}/approve",
    response_model=TicketDetail,
    summary="Approve the current draft and queue delivery",
)
async def approve(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TicketDetail:
    try:
        await approve_ticket(session, ticket_id, settings=settings)
    except TicketNotFoundError as exc:
        raise _not_found() from exc
    except ModerationConflictError as exc:
        raise _conflict(exc) from exc
    return await _detail_after_action(session, ticket_id)


@router.post(
    "/tickets/{ticket_id}/reject",
    response_model=TicketDetail,
    summary="Reject the current draft",
)
async def reject(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketDetail:
    try:
        await reject_ticket(session, ticket_id)
    except TicketNotFoundError as exc:
        raise _not_found() from exc
    except ModerationConflictError as exc:
        raise _conflict(exc) from exc
    return await _detail_after_action(session, ticket_id)


@router.post(
    "/tickets/{ticket_id}/regenerate",
    response_model=TicketDetail,
    summary="Queue another draft generation",
)
async def regenerate(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TicketDetail:
    try:
        await regenerate_ticket(session, ticket_id, settings=settings)
    except TicketNotFoundError as exc:
        raise _not_found() from exc
    except ModerationConflictError as exc:
        raise _conflict(exc) from exc
    return await _detail_after_action(session, ticket_id)


@router.post(
    "/tickets/{ticket_id}/retry-delivery",
    response_model=TicketDetail,
    summary="Requeue a terminally failed reply delivery",
)
async def retry(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TicketDetail:
    try:
        await retry_delivery(session, ticket_id, settings=settings)
    except TicketNotFoundError as exc:
        raise _not_found() from exc
    except ModerationConflictError as exc:
        raise _conflict(exc) from exc
    return await _detail_after_action(session, ticket_id)


async def _detail_after_action(session: AsyncSession, ticket_id: uuid.UUID) -> TicketDetail:
    ticket = await _load_ticket(session, ticket_id)
    if ticket is None:  # pragma: no cover - the row was just locked and updated
        raise _not_found()
    return _ticket_detail(ticket)


async def _load_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket | None:
    statement = (
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(selectinload(Ticket.drafts), selectinload(Ticket.outbound))
        .execution_options(populate_existing=True)
    )
    return cast(Ticket | None, await session.scalar(statement))


def _ticket_read(ticket: Ticket) -> TicketRead:
    current = _current_draft(ticket)
    return TicketRead(
        id=ticket.id,
        external_id=ticket.external_id,
        customer_email=ticket.customer_email,
        message=ticket.message,
        reply_url=ticket.reply_url,
        status=ticket.status,
        current_draft_id=ticket.current_draft_id,
        current_draft=DraftRead.model_validate(current) if current is not None else None,
        approved_at=ticket.approved_at,
        rejected_at=ticket.rejected_at,
        sent_at=ticket.sent_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        outbound=(
            OutboundRead.model_validate(ticket.outbound) if ticket.outbound is not None else None
        ),
    )


def _ticket_detail(ticket: Ticket) -> TicketDetail:
    summary = _ticket_read(ticket)
    return TicketDetail(
        **summary.model_dump(),
        drafts=[DraftRead.model_validate(draft) for draft in ticket.drafts],
    )


def _current_draft(ticket: Ticket) -> Draft | None:
    if ticket.current_draft_id is None:
        return None
    return next(
        (draft for draft in ticket.drafts if draft.id == ticket.current_draft_id),
        None,
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_404_NOT_FOUND,
        detail="ticket not found",
    )


def _conflict(exc: ModerationConflictError) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=str(exc),
    )
