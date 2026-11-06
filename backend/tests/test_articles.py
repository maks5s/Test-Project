"""Knowledge base endpoints: article list and detail."""

from __future__ import annotations

from httpx import AsyncClient

from app.seed import ARTICLES, seed_articles


async def test_articles_are_listed_without_bodies(client: AsyncClient) -> None:
    await seed_articles()

    response = await client.get("/api/articles")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(ARTICLES) == 8
    assert len(body["items"]) == 8
    titles = [item["title"] for item in body["items"]]
    assert titles == sorted(titles)
    assert all("body" not in item for item in body["items"])
    assert {"slug", "title", "tags", "updated_at"} <= set(body["items"][0])


async def test_article_detail_returns_the_body_and_tags(client: AsyncClient) -> None:
    await seed_articles()

    response = await client.get("/api/articles/exporting-reports-to-pdf")

    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "exporting-reports-to-pdf"
    assert "Export as PDF" in body["body"]
    assert "pdf" in body["tags"]


async def test_unknown_article_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/articles/no-such-article")

    assert response.status_code == 404
    assert response.json()["detail"] == "article not found"


async def test_empty_knowledge_base_is_a_valid_empty_list(client: AsyncClient) -> None:
    response = await client.get("/api/articles")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}
