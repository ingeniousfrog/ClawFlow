"""Tests for RSS search behavior in web tool."""

from __future__ import annotations

import asyncio

import pytest

from nanoclaw.core.rss_sources import RssSource
from nanoclaw.tools import web


def test_search_with_rss_sources_falls_back_to_global_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mainland-first mode should use global fallback when primary results are empty."""
    async def _run() -> None:
        sources = [
            RssSource(
                channel_id="ai",
                channel_name="AI",
                title="Mainland Source",
                url="https://example.com/mainland.xml",
                tier=1,
                fmt="rss",
                tags=["mainland-friendly"],
            ),
            RssSource(
                channel_id="ai",
                channel_name="AI",
                title="Global Source",
                url="https://example.com/global.xml",
                tier=2,
                fmt="rss",
                tags=["global"],
            ),
        ]

        async def fake_collect_feed_results(
            session: object,
            sources: list[RssSource],
            timeout_seconds: int,
            items_per_feed: int,
            concurrency: int,
            retries: int,
            layer: str,
        ) -> tuple[list[dict[str, str]], dict[str, int]]:
            if layer == "primary":
                return [], {
                    "checked": len(sources),
                    "ok_sources": 0,
                    "failed_sources": len(sources),
                }
            return (
                [
                    {
                        "title": "AI fallback entry",
                        "url": "https://example.com/fallback",
                        "snippet": "latest ai fallback snippet",
                        "source": "Global Source",
                        "channel": "ai",
                        "layer": "fallback",
                        "tier": "2",
                    }
                ],
                {"checked": 1, "ok_sources": 1, "failed_sources": 0},
            )

        monkeypatch.setattr(web, "_collect_feed_results", fake_collect_feed_results)

        text, ok = await web._search_with_rss_sources(
            query="latest ai",
            sources=sources,
            prefer_mainland=True,
            mainland_only=False,
            max_feeds=4,
            items_per_feed=5,
            timeout_seconds=5,
            concurrency=2,
            retries=1,
        )

        assert ok is True
        assert "fallback triggered" in text.lower()
        assert "Layer: fallback" in text
        await web.ConnectionPool.close()

    asyncio.run(_run())


def test_dedupe_entries_keeps_first_url_occurrence() -> None:
    """URL de-duplication should preserve first occurrence order."""
    items = [
        {"url": "https://example.com/1", "title": "a"},
        {"url": "https://example.com/2", "title": "b"},
        {"url": "https://example.com/1", "title": "a-duplicate"},
    ]

    deduped = web._dedupe_entries(items)  # type: ignore[arg-type]

    assert [entry["url"] for entry in deduped] == [
        "https://example.com/1",
        "https://example.com/2",
    ]


def test_parse_channel_filters() -> None:
    """Channel filter parser should normalize comma-separated values."""
    parsed = web._parse_channel_filters(" ai, tech ,quantum_mechanics ")
    assert parsed == {"ai", "tech", "quantum_mechanics"}
    assert web._parse_channel_filters("   ") is None


def test_format_hotspot_brief_contains_signals_and_items() -> None:
    """Hotspot formatter should include signals, layers, and ranked items."""
    entries = [
        {
            "title": "AI policy update",
            "url": "https://example.com/1",
            "snippet": "Important update",
            "source": "Source A",
            "channel": "ai",
            "layer": "primary",
            "tier": "1",
        },
        {
            "title": "Quantum trend",
            "url": "https://example.com/2",
            "snippet": "Research momentum",
            "source": "Source B",
            "channel": "quantum_mechanics",
            "layer": "fallback",
            "tier": "2",
        },
    ]
    output = web._format_hotspot_brief(
        topic="AI",
        entries=entries,
        status_lines=["Phase primary(mainland): checked=2 ok=1 failed=1"],
        fallback_used=True,
        query_plan=["AI", "ai model release latest"],
        source_scope="Source scope: channels=ai; available_feeds=2",
        strategy_notes=["Bilingual keyword strategy."],
    )
    assert "Hotspot brief for `AI`" in output
    assert "Key signals:" in output
    assert "Layer: fallback" in output
    assert "https://example.com/1" in output
    assert "Query plan:" in output
    assert "Source scope:" in output


def test_build_query_plan_expands_cjk_topic() -> None:
    """CJK topics should produce bilingual query variants."""
    queries, notes = web._build_query_plan("伊朗七天局势趋势简报", {"politics"})
    merged = " | ".join(queries).lower()
    assert "iran" in merged
    assert "last 7 days" in merged
    assert any("bilingual" in item.lower() for item in notes)


def test_infer_recency_days() -> None:
    """Recency parser should respect explicit and implicit windows."""
    assert web._infer_recency_days("伊朗过去7天局势") == 7
    assert web._infer_recency_days("latest ai news") == 30
    assert web._infer_recency_days("show me architecture design") is None


def test_parse_feed_entries_extracts_published_at() -> None:
    """RSS parser should include normalized publication timestamps."""
    xml_text = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Example item</title>
          <link>https://example.com/item</link>
          <description>Snippet</description>
          <pubDate>Tue, 04 Mar 2026 10:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """
    entries = web._parse_feed_entries(xml_text)
    assert len(entries) == 1
    assert entries[0]["published_at"].startswith("2026-03-04T10:00:00")


def test_collect_ranked_entries_returns_empty_on_zero_relevance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ranking should fail fast when no entry matches query terms."""

    async def _run() -> None:
        async def fake_collect_feed_results(
            session: object,
            sources: list[RssSource],
            timeout_seconds: int,
            items_per_feed: int,
            concurrency: int,
            retries: int,
            layer: str,
        ) -> tuple[list[dict[str, str]], dict[str, int]]:
            return (
                [
                    {
                        "title": "Sports roundup",
                        "url": "https://example.com/sports",
                        "snippet": "football tournament update",
                        "published_at": "2026-03-03T12:00:00+00:00",
                        "source": "Global Source",
                        "channel": "sports",
                        "layer": layer,
                        "tier": "2",
                    }
                ],
                {"checked": 1, "ok_sources": 1, "failed_sources": 0},
            )

        monkeypatch.setattr(web, "_collect_feed_results", fake_collect_feed_results)

        entries, status_lines, _ = await web._collect_ranked_entries(
            queries=["quantum entanglement"],
            sources=[
                RssSource(
                    channel_id="sports",
                    channel_name="Sports",
                    title="Global Source",
                    url="https://example.com/global.xml",
                    tier=2,
                    fmt="rss",
                    tags=["global"],
                )
            ],
            prefer_mainland=False,
            mainland_only=False,
            recency_days=30,
            max_feeds=4,
            items_per_feed=5,
            timeout_seconds=5,
            concurrency=2,
            retries=1,
            top_k=3,
        )

        assert entries == []
        assert any("Relevance filter" in line for line in status_lines)
        await web.ConnectionPool.close()

    asyncio.run(_run())


def test_compute_hot_keywords_and_sort_daily_items() -> None:
    """Daily keyword and item hotness should be computed deterministically."""
    entries = [
        {
            "title": "AI model release in China",
            "url": "https://example.com/ai-1",
            "snippet": "AI model release gains traction",
            "published_at": "2026-03-04T10:00:00+00:00",
            "source": "Source A",
            "channel": "ai",
            "layer": "primary",
            "tier": "1",
        },
        {
            "title": "Chip market update",
            "url": "https://example.com/tech-1",
            "snippet": "chip supply and ai demand",
            "published_at": "2026-03-04T09:00:00+00:00",
            "source": "Source B",
            "channel": "tech",
            "layer": "primary",
            "tier": "2",
        },
    ]

    hot_keywords = web._compute_hot_keywords(entries, limit=5)
    assert hot_keywords
    terms = [term for term, _ in hot_keywords]
    assert "ai" in terms

    ranked = web._sort_daily_items(entries, hot_keywords, top_k=2)
    assert len(ranked) == 2
    assert "hotness" in ranked[0]


def test_format_daily_digest_contains_hot_keywords_and_urls() -> None:
    """Daily digest formatter should include hot keywords, URLs, and hotness."""
    entries = [
        {
            "title": "AI model release in China",
            "url": "https://example.com/ai-1",
            "snippet": "AI model release gains traction",
            "published_at": "2026-03-04T10:00:00+00:00",
            "source": "Source A",
            "channel": "ai",
            "layer": "primary",
            "tier": "1",
            "hotness": "68",
        }
    ]

    output = web._format_daily_digest(
        topic="AI",
        entries=entries,
        status_lines=["Phase daily: checked=10 ok=7 failed=3"],
        source_scope="Source scope: channels=ai; available_feeds=10",
        hot_keywords=[("ai", 12), ("model", 8)],
        recency_days=1,
    )
    assert "Daily digest for `AI`" in output
    assert "Hot keywords:" in output
    assert "hotness=68" in output
    assert "https://example.com/ai-1" in output
