"""Canonical status strings shared by the API, the worker, and the tests."""

from typing import Final

# Ticket lifecycle
TICKET_RECEIVED: Final = "received"
TICKET_GENERATING: Final = "generating"
TICKET_DRAFT_READY: Final = "draft_ready"
TICKET_APPROVED: Final = "approved"
TICKET_SENDING: Final = "sending"
TICKET_SENT: Final = "sent"
TICKET_REJECTED: Final = "rejected"
TICKET_GENERATION_FAILED: Final = "generation_failed"
TICKET_SEND_FAILED: Final = "send_failed"

TICKET_STATUSES: Final = (
    TICKET_RECEIVED,
    TICKET_GENERATING,
    TICKET_DRAFT_READY,
    TICKET_APPROVED,
    TICKET_SENDING,
    TICKET_SENT,
    TICKET_REJECTED,
    TICKET_GENERATION_FAILED,
    TICKET_SEND_FAILED,
)

# Jobs
JOB_GENERATE_DRAFT: Final = "generate_draft"
JOB_SEND_REPLY: Final = "send_reply"

JOB_TYPES: Final = (JOB_GENERATE_DRAFT, JOB_SEND_REPLY)

JOB_PENDING: Final = "pending"
JOB_RUNNING: Final = "running"
JOB_DONE: Final = "done"
JOB_FAILED: Final = "failed"

JOB_STATUSES: Final = (JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_FAILED)

# Draft provenance
DRAFT_SOURCE_LLM: Final = "llm"
DRAFT_SOURCE_FALLBACK: Final = "fallback"

DRAFT_SOURCES: Final = (DRAFT_SOURCE_LLM, DRAFT_SOURCE_FALLBACK)

# Outbound delivery
OUTBOUND_PENDING: Final = "pending"
OUTBOUND_SENDING: Final = "sending"
OUTBOUND_SENT: Final = "sent"
OUTBOUND_FAILED: Final = "failed"

OUTBOUND_STATUSES: Final = (OUTBOUND_PENDING, OUTBOUND_SENDING, OUTBOUND_SENT, OUTBOUND_FAILED)

# LLM call audit
LLM_CALL_OK: Final = "ok"
LLM_CALL_ERROR: Final = "error"
LLM_CALL_FALLBACK: Final = "fallback"

LLM_CALL_STATUSES: Final = (LLM_CALL_OK, LLM_CALL_ERROR, LLM_CALL_FALLBACK)
