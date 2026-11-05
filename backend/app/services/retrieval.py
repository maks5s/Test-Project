"""Knowledge-base retrieval.

Citations are produced here, never by the language model: the model only writes
prose, and the draft stores the ``slug``/``title`` pairs of the articles that
were actually retrieved. This makes hallucinated links impossible.

The primary search is PostgreSQL full-text with the ``simple`` configuration,
which keeps working for non-English messages instead of stemming them with an
English dictionary. The query is built from the message lexemes joined with
``|`` so a mostly Ukrainian message still matches an English article that
contains one shared token such as ``PDF``.

When full-text search finds nothing, a trigram/``ILIKE`` fallback tries the
individual words against titles and bodies. If that also finds nothing the
caller produces an escalation draft; an empty result is a valid state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ARTICLES = 3

_FTS_SQL = text(
    """
    WITH q AS (
        SELECT to_tsquery(
            'simple',
            replace(plainto_tsquery('simple', :query)::text, ' & ', ' | ')
        ) AS ts_query
        WHERE plainto_tsquery('simple', :query)::text <> ''
    )
    SELECT a.slug, a.title, a.body, ts_rank_cd(a.search_vector, q.ts_query) AS rank
    FROM articles a, q
    WHERE a.search_vector @@ q.ts_query
    ORDER BY rank DESC, a.title
    LIMIT :limit
    """
)

_TRIGRAM_SQL = text(
    """
    WITH words AS (
        SELECT DISTINCT nullif(
            regexp_replace(lower(word), '[^[:alnum:]_-]+', '', 'g'),
            ''
        ) AS value
        FROM regexp_split_to_table(:query, '\\s+') AS word
    ),
    filtered AS (
        SELECT value FROM words
        WHERE value IS NOT NULL AND char_length(value) >= 3
    )
    SELECT a.slug, a.title, a.body,
           max(
               greatest(
                   similarity(a.title, f.value),
                   CASE WHEN a.title ILIKE '%' || f.value || '%' THEN 0.5 ELSE 0 END,
                   CASE WHEN a.body ILIKE '%' || f.value || '%' THEN 0.25 ELSE 0 END
               )
           ) AS rank
    FROM articles a
    CROSS JOIN filtered f
    WHERE a.title ILIKE '%' || f.value || '%'
       OR a.body ILIKE '%' || f.value || '%'
       OR a.title % f.value
    GROUP BY a.slug, a.title, a.body
    ORDER BY rank DESC, a.title
    LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class RetrievedArticle:
    """One knowledge-base hit and how strongly it matched."""

    slug: str
    title: str
    body: str
    rank: float

    def citation(self) -> dict[str, str]:
        return {"slug": self.slug, "title": self.title}


async def search_articles(
    session: AsyncSession,
    query: str,
    *,
    limit: int = MAX_ARTICLES,
) -> list[RetrievedArticle]:
    """Return the most relevant articles for a customer message."""
    cleaned = query.strip()
    if not cleaned or limit <= 0:
        return []

    rows = (await session.execute(_FTS_SQL, {"query": cleaned, "limit": limit})).mappings().all()
    if not rows:
        rows = (
            (await session.execute(_TRIGRAM_SQL, {"query": cleaned, "limit": limit}))
            .mappings()
            .all()
        )

    return [_to_article(row) for row in rows]


def _to_article(row: Any) -> RetrievedArticle:
    return RetrievedArticle(
        slug=str(row["slug"]),
        title=str(row["title"]),
        body=str(row["body"]),
        rank=float(row["rank"] or 0.0),
    )


def citations_for(articles: list[RetrievedArticle]) -> list[dict[str, str]]:
    """Return unique citation objects for storage in ``drafts.citations``."""
    seen: set[str] = set()
    citations: list[dict[str, str]] = []
    for article in articles:
        if article.slug in seen:
            continue
        seen.add(article.slug)
        citations.append(article.citation())
    return citations
