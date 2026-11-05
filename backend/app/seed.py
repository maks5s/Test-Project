"""Knowledge base seed.

Run with ``python -m app.seed``. The upsert is idempotent: re-running refreshes
the article text without creating duplicates or changing slugs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from app.db import get_engine, get_sessionmaker
from app.models import Article

logger = logging.getLogger(__name__)

ARTICLES: list[dict[str, Any]] = [
    {
        "slug": "exporting-reports-to-pdf",
        "title": "Exporting reports to PDF",
        "tags": ["export", "pdf", "reports"],
        "body": (
            "Open the report, click Share in the top-right corner, and choose Export as PDF. "
            "Northstar renders the PDF on the server, which usually takes 10-40 seconds for "
            "reports with more than 50 pages. If the button is disabled, the report has not "
            "finished loading: wait for the progress bar to disappear and try again. Large "
            "exports are emailed to you as a download link when the browser window is closed. "
            "Scheduled PDF exports are limited to 200 pages per run on the Team plan."
        ),
    },
    {
        "slug": "scheduled-reports",
        "title": "Scheduling recurring report deliveries",
        "tags": ["reports", "schedules", "email"],
        "body": (
            "Schedules live under Reports > Schedules. Each schedule picks a report, a "
            "frequency (hourly, daily, weekly, monthly), a timezone, and one or more "
            "recipients. Northstar evaluates schedules in the workspace timezone, not the "
            "viewer's timezone. Paused schedules keep their history but send nothing. If a "
            "scheduled email does not arrive, check that the workspace has at least one "
            "verified sender domain and that the schedule is not paused."
        ),
    },
    {
        "slug": "connecting-data-sources",
        "title": "Connecting a data source",
        "tags": ["data sources", "postgres", "csv", "setup"],
        "body": (
            "Add a data source from Settings > Data sources. Supported connectors are "
            "PostgreSQL, MySQL, BigQuery, Snowflake, and CSV upload. The first sync copies "
            "the full table; later syncs are incremental when the source has a timestamp "
            "column you mark as the cursor. Credentials are encrypted at rest and are never "
            "exposed in query results or logs. A failed sync keeps the last successful "
            "snapshot available, so dashboards stay readable while you fix the connection."
        ),
    },
    {
        "slug": "sso-and-login",
        "title": "Single sign-on and login problems",
        "tags": ["sso", "login", "saml", "security"],
        "body": (
            "Single sign-on is available on the Business plan with SAML 2.0 or OpenID "
            "Connect. Configure it in Settings > Authentication: Northstar provides the "
            "service provider metadata, and you supply the identity provider URL and "
            "certificate. When SSO is enforced, password login is disabled for everyone "
            "except workspace owners, who keep a break-glass password. If users see "
            "'Invalid SAML response', check that the clock on the identity provider is "
            "synchronised and that the audience URI matches exactly."
        ),
    },
    {
        "slug": "api-keys-and-rate-limits",
        "title": "API keys and rate limits",
        "tags": ["api", "keys", "rate limits"],
        "body": (
            "Create API keys in Settings > API. A key is shown once at creation time; store "
            "it in a secret manager. Each key inherits the permissions of the member who "
            "created it, and revoking a key takes effect within 60 seconds. The public API "
            "allows 600 requests per minute per workspace and returns 429 with a Retry-After "
            "header when the limit is exceeded. Bulk export endpoints are limited to 20 "
            "concurrent jobs per workspace."
        ),
    },
    {
        "slug": "billing-and-seats",
        "title": "Billing, plans, and seats",
        "tags": ["billing", "seats", "plans"],
        "body": (
            "Billing is per active seat per month, billed on the first day of the cycle. "
            "Inviting a member reserves a seat immediately; removing a member frees it at "
            "the next billing cycle. Plan changes are prorated. Invoices and receipts are "
            "available under Settings > Billing, and the workspace owner can update the "
            "payment method there. Failed payments keep the workspace readable for 14 days "
            "before write access is suspended."
        ),
    },
    {
        "slug": "sharing-dashboards",
        "title": "Sharing dashboards with people outside the workspace",
        "tags": ["sharing", "dashboards", "permissions"],
        "body": (
            "A dashboard can be shared as a read-only public link or with specific email "
            "addresses. Public links can be protected with a password and given an expiry "
            "date. Shared viewers always see the latest data but cannot change filters "
            "unless you enable interactive filters for the link. Workspace administrators "
            "can disable public links for the whole workspace under Settings > Security."
        ),
    },
    {
        "slug": "data-retention",
        "title": "Data retention and deletion",
        "tags": ["data", "retention", "gdpr"],
        "body": (
            "Raw data snapshots are kept for 90 days on the Team plan and 24 months on the "
            "Business plan; aggregated metrics are kept until the data source is deleted. "
            "Deleting a data source removes its snapshots within 24 hours. Workspace owners "
            "can request full export or deletion of workspace data under Settings > Data "
            "policy; deletion requests are completed within 30 days and cannot be undone. "
            "Deletion removes backups according to the standard 30-day backup rotation."
        ),
    },
]


async def seed_articles() -> int:
    """Upsert the knowledge base and return the number of articles written."""
    statement = insert(Article).values(ARTICLES)
    statement = statement.on_conflict_do_update(
        index_elements=[Article.slug],
        set_={
            "title": statement.excluded.title,
            "body": statement.excluded.body,
            "tags": statement.excluded.tags,
        },
    )

    async with get_sessionmaker()() as session:
        await session.execute(statement)
        await session.commit()

    return len(ARTICLES)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    count = await seed_articles()
    logger.info("knowledge base seeded with %s articles", count)
    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
