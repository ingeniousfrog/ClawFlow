"""Integration tests for planner-backed web search routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanoclaw.core import config as config_module
from nanoclaw.core.config import WebSearchConfig
from nanoclaw.core.rss_sources import RssSource
from nanoclaw.tools import search_providers, web
from nanoclaw.tools.search_planner import SearchQueryPlan
from nanoclaw.tools.search_providers import SearchProviderResult, run_search_provider


@pytest.mark.asyncio
async def test_web_search_uses_planner_selected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto mode should allow planner to redirect paper queries onto RSS."""

    async def fake_run(provider: str, query: str, web_cfg: object, plan=None):  # type: ignore[no-untyped-def]
        assert query == "show latest arxiv papers about video generation"
        assert plan is not None
        assert plan.intent == "paper"
        return SearchProviderResult(
            text="**Paper hit**\nhttps://example.com\nSource: RSS | Provider: rss",
            ok=True,
            provider=provider,
        )

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(
            tools=SimpleNamespace(web_search=SimpleNamespace(provider="auto"))
        ),
    )
    monkeypatch.setattr(search_providers, "run_search_provider", fake_run)

    result = await web.web_search("show latest arxiv papers about video generation")
    assert "Search planner: type=paper; provider=rss;" in result
    assert "Query variants:" in result
    assert "Normalized evidence set:" in result
    assert "https://example.com" in result


@pytest.mark.asyncio
async def test_serper_provider_uses_news_mode_and_variant_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serper provider should honor planner category and retry better variants."""

    calls: list[tuple[str, str, str | None, str]] = []

    async def fake_serper(  # type: ignore[no-untyped-def]
        query: str,
        api_key: str,
        *,
        gl: str = "world",
        hl: str = "en",
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        calls.append((query, mode, tbs, hl))
        if len(calls) == 1:
            return "No Serper results found."
        return "**Hit**\nhttps://example.com\nSource: Serper News | Provider: serper"

    async def fake_rss_plan(plan: SearchQueryPlan) -> tuple[str, bool]:
        return "No RSS results found for this query.", False

    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_rss_plan", fake_rss_plan)

    plan = SearchQueryPlan(
        query="帮我查查伊朗最近七天的新闻",
        intent="news",
        category="news",
        provider_hint="auto",
        time_range="7d",
        recency_days=7,
        language_hint="zh",
        rss_channels=["politics"],
        query_variants=[
            "帮我查查伊朗最近七天的新闻",
            "iran latest news",
        ],
        notes=[],
        engine_hints=["news", "web"],
        site_filters=[],
    )

    result = await run_search_provider(
        "serper",
        plan.query,
        SimpleNamespace(
            serper_api_key="serper-key",
            serper_gl="world",
            serper_hl="en",
            serper_max_calls=0,
        ),
        plan=plan,
    )
    assert result.ok is True
    assert "Query planner fallback variant: iran latest news" in result.text
    assert calls[0][1] == "news"
    assert calls[1][1] == "news"
    assert calls[0][3] == "zh-cn"


@pytest.mark.asyncio
async def test_outbound_host_policy_allows_matching_subdomain_rule() -> None:
    """Allowlist rules should match both the host and its subdomains."""
    allowed, hostname, reason = await web._check_outbound_url_policy(
        "https://news.example.com/story",
        WebSearchConfig(allowedHosts=["example.com"]),
    )
    assert allowed is True
    assert hostname == "news.example.com"
    assert reason == ""


@pytest.mark.asyncio
async def test_outbound_host_policy_blocks_private_host() -> None:
    """Outbound policy should still reject private or loopback targets."""
    allowed, hostname, reason = await web._check_outbound_url_policy(
        "http://127.0.0.1:8080/health",
        WebSearchConfig(),
    )
    assert allowed is False
    assert hostname == "127.0.0.1"
    assert "private/internal" in reason


@pytest.mark.asyncio
async def test_fetch_feed_entries_respects_allowed_host_policy() -> None:
    """RSS fetches should stop before network I/O when the host is not allowlisted."""

    class DummySession:
        def get(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("network call should not happen")

    source = RssSource(
        channel_id="tech",
        channel_name="Tech",
        title="Blocked feed",
        url="https://rss.example.com/feed.xml",
        tier=1,
        fmt="rss",
        tags=[],
    )
    entries = await web._fetch_feed_entries(
        session=DummySession(),  # type: ignore[arg-type]
        source=source,
        timeout_seconds=5,
        items_per_feed=5,
        retries=1,
        layer="primary",
        web_cfg=WebSearchConfig(allowedHosts=["allowed.com"]),
    )
    assert entries == []


@pytest.mark.asyncio
async def test_web_fetch_respects_outbound_host_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web_fetch should block disallowed hosts before trying to fetch the URL."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(
            tools=SimpleNamespace(
                web_search=WebSearchConfig(allowedHosts=["arxiv.org"])
            )
        ),
    )
    result = await web.web_fetch("https://example.com/article")
    assert "allowedHosts" in result
    assert "example.com" in result
