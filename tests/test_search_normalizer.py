"""Tests for search result normalization and reranking."""

from __future__ import annotations

from nanoclaw.tools.search_normalizer import normalize_search_result, render_normalized_result
from nanoclaw.tools.search_planner import SearchQueryPlan
from nanoclaw.tools.search_providers import SearchProviderResult


def _news_plan() -> SearchQueryPlan:
    """Return a compact reusable news query plan."""
    return SearchQueryPlan(
        query="latest ai news",
        intent="news",
        category="news",
        provider_hint="auto",
        time_range="7d",
        recency_days=7,
        language_hint="en",
        rss_channels=["ai"],
        site_filters=[],
        engine_hints=["news", "web"],
        query_variants=["latest ai news", "ai latest news"],
        notes=[],
    )


def test_normalizer_dedupes_tracking_urls_and_reranks_recent_hit() -> None:
    """Duplicate URLs should collapse to one normalized evidence item."""
    result = SearchProviderResult(
        text="",
        ok=True,
        provider="serper+rss",
        evidence_items=[
            {
                "title": "AI headline",
                "url": "https://example.com/story?utm_source=x",
                "snippet": "Recent AI launch",
                "source": "Serper",
                "published_at": "2026-03-08T08:00:00+00:00",
            },
            {
                "title": "AI headline duplicate",
                "url": "https://example.com/story?utm_campaign=y",
                "snippet": "Duplicate copy",
                "source": "RSS",
                "published_at": "2026-03-06T08:00:00+00:00",
            },
            {
                "title": "Older AI note",
                "url": "https://older.example.com/post",
                "snippet": "Older AI update",
                "source": "Serper",
                "published_at": "2026-02-01T08:00:00+00:00",
            },
        ],
    )
    normalized = normalize_search_result(result, _news_plan())
    assert len(normalized.evidences) == 2
    assert normalized.evidences[0].normalized_url == "https://example.com/story"
    assert normalized.evidences[0].title == "AI headline"
    rendered = render_normalized_result(normalized)
    assert "unique_items=2" in rendered
    assert "Score:" in rendered


def test_normalizer_supports_future_structured_provider_hook() -> None:
    """Future providers can bypass text parsing by populating structured hits directly."""
    result = SearchProviderResult(
        text="raw fallback text",
        ok=True,
        provider="searxng",
        evidence_items=[
            {
                "title": "Self-hosted search hit",
                "url": "https://search.example.com/post",
                "snippet": "Normalized directly from provider payload.",
                "source": "SearXNG",
                "metadata": {"engine": "google", "category": "news"},
            }
        ],
    )
    normalized = normalize_search_result(result, _news_plan())
    assert len(normalized.evidences) == 1
    assert normalized.evidences[0].provider == "searxng"
    assert normalized.evidences[0].metadata["engine"] == "google"
