"""Pydantic schemas for the public HTTP API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.services.reply_urls import MAX_REPLY_URL_LENGTH, normalize_reply_url

MAX_EVENT_ID_LENGTH = 128
MAX_TICKET_ID_LENGTH = 128
MAX_EMAIL_LENGTH = 320
MAX_MESSAGE_LENGTH = 20_000

# Deliberately lenient: the helpdesk owns address validation. We only reject
# values that cannot be an email at all (no single ``@`` or no domain part).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")

EventId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EVENT_ID_LENGTH),
]
TicketExternalId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TICKET_ID_LENGTH),
]
CustomerEmail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=MAX_EMAIL_LENGTH),
]
CustomerMessage = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_LENGTH)]
ReplyUrl = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_REPLY_URL_LENGTH)]


class HelpdeskWebhookPayload(BaseModel):
    """Body of ``POST /webhooks/helpdesk``, as sent by the helpdesk.

    Unknown fields are accepted and preserved in ``model_dump()`` so the audit
    row in ``helpdesk_events`` holds the complete delivery, but they do not
    affect processing.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=False)

    event_id: EventId
    ticket_id: TicketExternalId
    customer_email: CustomerEmail
    message: CustomerMessage
    reply_url: ReplyUrl

    @field_validator("customer_email")
    @classmethod
    def _validate_email_shape(cls, value: str) -> str:
        if not _EMAIL_PATTERN.match(value):
            raise ValueError("customer_email must be a single email address")
        return value

    @field_validator("message")
    @classmethod
    def _reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @field_validator("reply_url")
    @classmethod
    def _validate_reply_url(cls, value: str) -> str:
        return normalize_reply_url(value)


class WebhookAccepted(BaseModel):
    """Successful webhook acknowledgement. Duplicates get the same answer."""

    status: Literal["accepted"] = "accepted"
    duplicate: bool


class DraftRead(BaseModel):
    """A generated reply draft as exposed to the operator UI."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    content: str
    citations: list[dict[str, str]]
    source: str
    model: str | None = None
    latency_ms: int | None = None
    created_at: datetime


class OutboundRead(BaseModel):
    """Delivery state of the single reply owned by a ticket."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    idempotency_key: str
    attempts: int
    last_error: str | None = None
    sent_at: datetime | None = None
    created_at: datetime


class TicketRead(BaseModel):
    """Ticket row plus its current draft and delivery state."""

    id: uuid.UUID
    external_id: str
    customer_email: str
    message: str
    reply_url: str
    status: str
    current_draft_id: uuid.UUID | None = None
    current_draft: DraftRead | None = None
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    outbound: OutboundRead | None = None


class TicketDetail(TicketRead):
    """Ticket with every draft version for the detail panel."""

    drafts: list[DraftRead]


class TicketList(BaseModel):
    items: list[TicketRead]
    total: int


class MockReplyPayload(BaseModel):
    """Body the service sends to the mock helpdesk's ``reply_url``."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=MAX_TICKET_ID_LENGTH)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)

    @field_validator("message")
    @classmethod
    def _reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class MockReplyRead(BaseModel):
    """One reply accepted by the mock helpdesk."""

    id: uuid.UUID
    ticket_external_id: str
    payload: dict[str, Any]
    idempotency_key: str
    received_at: datetime
    duplicate: bool = False


class MockReplyList(BaseModel):
    items: list[MockReplyRead]
    total: int
