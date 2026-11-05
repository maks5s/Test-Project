"""Helpdesk-facing webhook endpoint."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas import HelpdeskWebhookPayload, WebhookAccepted
from app.services.ingestion import ingest_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DATABASE_UNAVAILABLE_DETAIL = "database unavailable"


@router.post(
    "/helpdesk",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
    summary="Accept a helpdesk ticket",
    description=(
        "Persist the event, the ticket, and one generation job, then acknowledge "
        "immediately. Safe to call repeatedly with the same or concurrent deliveries."
    ),
)
async def receive_helpdesk_event(
    payload: HelpdeskWebhookPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAccepted:
    try:
        result = await ingest_webhook(session, payload, settings)
    except (SQLAlchemyError, OSError):
        # The helpdesk treats a non-2xx response as "retry later". Never claim
        # success when nothing was stored.
        logger.warning(
            "Webhook ingestion failed; database unavailable (event_id=%s)",
            payload.event_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DATABASE_UNAVAILABLE_DETAIL,
            headers={"Retry-After": "5"},
        ) from None

    return WebhookAccepted(duplicate=result.duplicate)
