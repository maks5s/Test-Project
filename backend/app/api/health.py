from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_class=PlainTextResponse, summary="Liveness probe")
async def health() -> str:
    """Answer without touching the database: the helpdesk must always get a verdict."""
    return "OK"
