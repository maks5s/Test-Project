"""Knowledge base API used by the operator console."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Article
from app.schemas import ArticleList, ArticleRead, ArticleSummary

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("", response_model=ArticleList, summary="List knowledge base articles")
async def list_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ArticleList:
    """Return every article title, newest changes first by title order.

    The body is omitted here: the console loads it only for the article the
    operator opens.
    """
    total = await session.scalar(select(func.count()).select_from(Article))
    articles = list(await session.scalars(select(Article).order_by(Article.title)))
    return ArticleList(
        items=[ArticleSummary.model_validate(article) for article in articles],
        total=int(total or 0),
    )


@router.get("/{slug}", response_model=ArticleRead, summary="Read one article")
async def get_article(
    slug: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ArticleRead:
    article = await session.scalar(select(Article).where(Article.slug == slug))
    if article is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="article not found",
        )
    return ArticleRead.model_validate(article)
