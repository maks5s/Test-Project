from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.seed import ARTICLES, seed_articles


async def test_seed_is_idempotent_and_creates_eight_articles(session: AsyncSession) -> None:
    first_run = await seed_articles()
    second_run = await seed_articles()

    assert len(ARTICLES) == 8
    assert first_run == second_run == 8
    assert await session.scalar(text("SELECT count(*) FROM articles")) == 8


async def test_full_text_search_finds_the_pdf_export_article(session: AsyncSession) -> None:
    await seed_articles()

    rows = await session.execute(
        text(
            """
            SELECT slug
            FROM articles, websearch_to_tsquery('simple', 'export report pdf') AS query
            WHERE search_vector @@ query
            ORDER BY ts_rank_cd(search_vector, query) DESC
            LIMIT 3
            """
        )
    )

    assert "exporting-reports-to-pdf" in [row.slug for row in rows]


async def test_trigram_extension_is_available_for_fallback_search(session: AsyncSession) -> None:
    similarity = await session.scalar(
        text("SELECT similarity('scheduled reports', 'scheduled reports page')")
    )

    assert similarity is not None
    assert similarity > 0.5
