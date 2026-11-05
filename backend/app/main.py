"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, webhooks
from app.db import get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    application = FastAPI(
        title="AI-first Helpdesk Draft Service",
        version="0.1.0",
        description=(
            "Accepts helpdesk tickets, drafts replies from a knowledge base, "
            "and delivers operator-approved answers."
        ),
        lifespan=lifespan,
    )
    application.include_router(health.router)
    application.include_router(webhooks.router)
    return application


app = create_app()
