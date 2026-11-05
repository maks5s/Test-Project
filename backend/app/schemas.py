"""Pydantic schemas for the public HTTP API."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

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
