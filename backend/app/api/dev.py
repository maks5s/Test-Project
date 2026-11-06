"""Local-only helpers that make the demo one click long.

``POST /api/dev/tickets`` builds the same payload the helpdesk would send and
runs it through the real ingestion path, including the mock ``reply_url``.
The endpoint is disabled unless ``ENABLE_DEV_ENDPOINTS=true``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas import DevTicketAccepted, DevTicketCreate, HelpdeskWebhookPayload
from app.services.ingestion import ingest_webhook

router = APIRouter(prefix="/api/dev", tags=["dev"])

DEFAULT_MESSAGE = "Hi! How can I export a report to PDF? The export button does nothing."
DEFAULT_EMAIL = "demo.customer@example.com"


@router.post(
    "/tickets",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=DevTicketAccepted,
    summary="Simulate an incoming helpdesk ticket",
    description=(
        "Builds a webhook payload with sensible defaults and stores it through the "
        "real ingestion path. Available only when ENABLE_DEV_ENDPOINTS is true."
    ),
)
async def simulate_ticket(
    body: DevTicketCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DevTicketAccepted:
    if not settings.enable_dev_endpoints:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="dev endpoints are disabled",
        )

    suffix = uuid.uuid4().hex[:8]
    external_id = body.ticket_id or f"T-{suffix.upper()}"
    reply_url = body.reply_url or _mock_reply_url(settings, external_id)
    payload = HelpdeskWebhookPayload(
        event_id=body.event_id or f"evt-{suffix}",
        ticket_id=external_id,
        customer_email=body.customer_email or DEFAULT_EMAIL,
        message=body.message or DEFAULT_MESSAGE,
        reply_url=reply_url,
    )

    try:
        result = await ingest_webhook(session, payload, settings)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc

    return DevTicketAccepted(
        duplicate=result.duplicate,
        ticket_id=result.ticket_id,
        external_id=payload.ticket_id,
        event_id=payload.event_id,
        reply_url=payload.reply_url,
    )


def _mock_reply_url(settings: Settings, external_id: str) -> str:
    base = settings.mock_helpdesk_internal_url.rstrip("/")
    return f"{base}/mock/helpdesk/tickets/{external_id}/replies"
