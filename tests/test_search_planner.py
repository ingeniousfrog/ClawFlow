"""Tests for deterministic search query planning."""

from __future__ import annotations

from nanoclaw.tools.search_planner import build_search_plan, select_search_provider


def test_build_search_plan_classifies_news_and_channels() -> None:
    """News-style queries should carry recency and RSS channel hints."""
    plan = build_search_plan("帮我查查伊朗最近七天的新闻", configured_provider="auto")
    assert plan.intent == "news"
    assert plan.category == "news"
    assert plan.provider_hint == "auto"
    assert plan.recency_days == 7
    assert "politics" in plan.rss_channels
    assert plan.query_variants
    assert any("latest news" in item.lower() or "last 7 days" in item.lower() for item in plan.query_variants)


def test_build_search_plan_classifies_paper_query() -> None:
    """Paper-style queries should prefer science intent and RSS auto hint."""
    plan = build_search_plan(
        "show latest arxiv papers about video generation",
        configured_provider="auto",
    )
    assert plan.intent == "paper"
    assert plan.category == "science"
    assert plan.provider_hint == "rss"
    assert select_search_provider("auto", plan) == "rss"
    assert any("arxiv preprint" in item.lower() for item in plan.query_variants)


def test_build_search_plan_classifies_site_query() -> None:
    """Site-filtered queries should preserve explicit domain constraints."""
    plan = build_search_plan(
        "site:openai.com latest operator update",
        configured_provider="auto",
    )
    assert plan.intent == "site"
    assert plan.site_filters == ["openai.com"]
    assert any("site:openai.com" in item.lower() for item in plan.query_variants)


def test_build_search_plan_classifies_chinese_web() -> None:
    """Chinese-web hints should stay distinct from generic web lookup."""
    plan = build_search_plan("找一些知乎上关于 MCP 的中文讨论", configured_provider="auto")
    assert plan.intent == "chinese_web"
    assert plan.language_hint == "zh"
    assert any("中文" in item for item in plan.query_variants)
