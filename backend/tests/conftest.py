"""Test configuration.

The suite requires a disposable PostgreSQL database: full-text search, JSONB,
and ``FOR UPDATE SKIP LOCKED`` are PostgreSQL features and SQLite is not an
option. Point ``TEST_DATABASE_URL`` at it, for example::

    docker compose up -d db
    createdb -h localhost -U app app_test        # once
    TEST_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/app_test pytest -q

Migrations are applied once per session; every test starts with truncated
tables. LLM delays are disabled and no test may reach the network.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL is not set. Tests need a disposable PostgreSQL database, "
        "e.g. postgresql+asyncpg://app:app@localhost:5432/app_test"
    )

# Must be set before the application modules read their settings.
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["LLM_SIMULATED_DELAY_MIN"] = "0"
os.environ["LLM_SIMULATED_DELAY_MAX"] = "0"
os.environ["ENABLE_DEV_ENDPOINTS"] = "true"

from app.db import get_sessionmaker  # noqa: E402
from app.main import app  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parents[1]

TRUNCATE_TABLES = (
    "mock_helpdesk_replies, llm_calls, outbound_messages, jobs, drafts, "
    "tickets, helpdesk_events, articles"
)


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
async def clean_database(apply_migrations: None) -> AsyncIterator[None]:
    """Leave a pristine database behind, whatever the test did."""
    yield
    async with get_sessionmaker()() as session:
        await session.execute(text(f"TRUNCATE TABLE {TRUNCATE_TABLES} RESTART IDENTITY CASCADE"))
        await session.execute(
            text(
                "INSERT INTO llm_rate_limit (id, tokens, updated_at) VALUES (1, 60, now()) "
                "ON CONFLICT (id) DO UPDATE SET tokens = 60, updated_at = now()"
            )
        )
        await session.commit()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
