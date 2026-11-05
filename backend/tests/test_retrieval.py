"""Knowledge-base retrieval and citation generation."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.seed import seed_articles
from app.services.retrieval import citations_for, search_articles


async def test_fts_finds_the_pdf_article(session: AsyncSession) -> None:
    await seed_articles()

    results = await search_articles(session, "how do I export a report to PDF?")

    assert results
    assert results[0].slug == "exporting-reports-to-pdf"
    assert results[0].title == "Exporting reports to PDF"
    assert results[0].body


async def test_fts_matches_a_ukrainian_message_by_shared_token(session: AsyncSession) -> None:
    await seed_articles()

    results = await search_articles(session, "Не можу експортувати звіт у PDF")

    assert results
    assert results[0].slug == "exporting-reports-to-pdf"


async def test_trigram_fallback_matches_a_word_form_not_found_by_fts(
    session: AsyncSession,
) -> None:
    await seed_articles()

    # "Invoices" appears in the body; `simple` does not stem it to "invoice".
    results = await search_articles(session, "invoice")

    assert results
    assert results[0].slug == "billing-and-seats"


async def test_results_are_limited_to_three_articles(session: AsyncSession) -> None:
    await seed_articles()

    results = await search_articles(session, "report data workspace plan", limit=3)

    assert len(results) <= 3


async def test_blank_or_unmatched_query_returns_nothing(session: AsyncSession) -> None:
    await seed_articles()

    assert await search_articles(session, "   ") == []
    assert await search_articles(session, "?!") == []
    assert await search_articles(session, "zzzz qqqq wwww") == []


async def test_citations_are_built_from_retrieved_articles(session: AsyncSession) -> None:
    await seed_articles()

    results = await search_articles(session, "api key rate limit")
    citations = citations_for(results)

    assert citations
    assert citations[0]["slug"] == "api-keys-and-rate-limits"
    assert citations[0]["title"] == "API keys and rate limits"
    assert all(set(citation) == {"slug", "title"} for citation in citations)
    assert len({citation["slug"] for citation in citations}) == len(citations)
