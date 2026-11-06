"""Mock helpdesk receiver used for local end-to-end demos.

It behaves like the real helpdesk contract: a reply arrives as a POST to the
``reply_url`` with a JSON body and an ``Idempotency-Key`` header, and returns
201 Created. A repeated POST with the same key returns 200 with the original
row, so an at-least-once sender cannot create two replies from the customer's
point of view.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import MockHelpdeskReply
from app.schemas import MockReplyList, MockReplyPayload, MockReplyRead

router = APIRouter(prefix="/mock/helpdesk", tags=["mock-helpdesk"])

MAX_IDEMPOTENCY_KEY_LENGTH = 256


@router.post(
    "/tickets/{ticket_external_id}/replies",
    status_code=http_status.HTTP_201_CREATED,
    response_model=MockReplyRead,
    summary="Receive one reply from the service",
)
async def receive_reply(
    ticket_external_id: str,
    payload: MockReplyPayload,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        ),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    response: Response,
) -> MockReplyRead:
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must not be blank",
        )
    if payload.ticket_id.strip() != ticket_external_id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="payload ticket_id does not match the URL",
        )

    stored_payload = payload.model_dump(mode="json")
    statement = (
        pg_insert(MockHelpdeskReply)
        .values(
            ticket_external_id=ticket_external_id,
            payload=stored_payload,
            idempotency_key=key,
        )
        .on_conflict_do_nothing(index_elements=[MockHelpdeskReply.idempotency_key])
        .returning(MockHelpdeskReply.id)
    )
    inserted = await session.scalar(statement)
    await session.commit()

    if inserted is None:
        existing = await session.scalar(
            select(MockHelpdeskReply).where(MockHelpdeskReply.idempotency_key == key)
        )
        if existing is None:  # pragma: no cover - the unique key conflict implies a row
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="could not read the stored reply",
            )
        response.status_code = http_status.HTTP_200_OK
        return _reply_read(existing, duplicate=True)

    reply = await session.get(MockHelpdeskReply, inserted)
    if reply is None:  # pragma: no cover - inserted id must exist
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not read the stored reply",
        )
    return _reply_read(reply, duplicate=False)


@router.get("/replies", response_model=MockReplyList, summary="List received replies")
async def list_replies(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MockReplyList:
    total = await session.scalar(select(func.count()).select_from(MockHelpdeskReply))
    replies = list(
        await session.scalars(
            select(MockHelpdeskReply)
            .order_by(MockHelpdeskReply.received_at.desc(), MockHelpdeskReply.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return MockReplyList(
        items=[_reply_read(reply, duplicate=False) for reply in replies],
        total=int(total or 0),
    )


def _reply_read(reply: MockHelpdeskReply, *, duplicate: bool) -> MockReplyRead:
    payload: dict[str, Any] = dict(reply.payload)
    return MockReplyRead(
        id=reply.id,
        ticket_external_id=reply.ticket_external_id,
        payload=payload,
        idempotency_key=reply.idempotency_key,
        received_at=reply.received_at,
        duplicate=duplicate,
    )


__all__ = ["router"]
