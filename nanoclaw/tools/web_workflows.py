"""Default web workflows built on top of atomic web and paper helpers."""

from __future__ import annotations

import asyncio
from typing import Any

from nanoclaw.core.logger import get_logger
from nanoclaw.core.rss_sources import RssSource, load_rss_sources
from nanoclaw.tools import web as web_tools
from nanoclaw.tools.registry import tool

logger = get_logger(__name__)


def _get_web_search_config() -> Any:
    """Return web search config with a safe fallback."""
    from nanoclaw.core.config import Config, get_config

    try:
        return get_config().tools.web_search
    except Exception:
        return Config().tools.web_search


def _select_channel_filters(topic: str, channels: str) -> set[str] | None:
    """Resolve explicit or inferred RSS channel filters."""
    explicit_channels = web_tools._parse_channel_filters(channels)
    inferred_channels = web_tools._infer_channels(topic) if topic.strip() else None
    return explicit_channels or inferred_channels


def _load_filtered_rss_sources(
    workflow_label: str,
    topic: str,
    channels: str,
) -> tuple[Any, set[str] | None, list[RssSource] | None, str, str]:
    """Load RSS sources and a source-scope summary for a workflow."""
    web_cfg = _get_web_search_config()
    sources_path = web_tools._resolve_sources_path(web_cfg.rss_sources_path)
    if not sources_path.exists():
        return (
            web_cfg,
            None,
            None,
            "",
            f"{workflow_label} unavailable: source registry not found at "
            f"`{web_cfg.rss_sources_path}`.",
        )

    channel_filters = _select_channel_filters(topic, channels)
    try:
        sources = load_rss_sources(
            sources_path,
            channel_filters=channel_filters,
            prefer_mainland=web_cfg.prefer_mainland,
            mainland_only=web_cfg.mainland_only,
        )
    except Exception as exc:
        logger.error("Failed to load RSS sources for %s: %s", workflow_label, exc)
        return (
            web_cfg,
            channel_filters,
            None,
            "",
            f"{workflow_label} unavailable: failed to load source registry ({exc}).",
        )

    if not sources:
        return (
            web_cfg,
            channel_filters,
            None,
            "",
            f"{workflow_label} unavailable: no RSS sources matched current filters.",
        )

    source_scope = web_tools._build_source_scope_summary(sources, channel_filters)
    return web_cfg, channel_filters, sources, source_scope, ""


@tool(
    name="hotspot_brief",
    description=(
        "Create a hotspot digest for a topic using RSS sources. "
        "Use this for trend briefs. The tool automatically searches configured "
        "RSS channels from assets/rss-sources.json and applies bilingual keyword "
        "expansion (original phrase + English aliases). Returns source scope, "
        "query plan, key signals, and ranked evidence links."
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "Topic to monitor, e.g. 'AI agents', 'quantum computing'.",
        },
        "channels": {
            "type": "string",
            "description": (
                "Optional comma-separated channel ids, e.g. "
                "'ai,tech,quantum_mechanics'."
            ),
        },
        "max_items": {
            "type": "integer",
            "description": "Maximum items in digest (3-12). Default 8.",
        },
    },
    required=[],
    catalog_summary="Create a ranked hotspot brief with RSS evidence and URLs.",
    catalog_entry_points=["hotspot brief", "trend brief", "AI hotspot summary"],
    risk_level="low",
)
async def hotspot_brief(
    topic: str = "",
    channels: str = "",
    max_items: int = 8,
) -> str:
    """Build a hotspot digest from RSS sources."""
    web_cfg, channel_filters, sources, source_scope, error = _load_filtered_rss_sources(
        workflow_label="Hotspot brief",
        topic=topic,
        channels=channels,
    )
    if error:
        return error
    if not sources:
        return "Hotspot brief unavailable: no RSS sources matched current filters."

    query_plan, strategy_notes = web_tools._build_query_plan(topic, channel_filters)
    max_feeds = max(1, min(int(web_cfg.rss_max_feeds), 50))
    items_per_feed = max(5, min(int(web_cfg.rss_items_per_feed), 50))
    timeout_seconds = max(3, min(int(web_cfg.rss_timeout), 30))
    concurrency = max(1, min(int(getattr(web_cfg, "rss_concurrency", 8)), 16))
    retries = max(1, min(int(getattr(web_cfg, "rss_retries", 1)), 3))
    top_k = max(3, min(int(max_items), 12))
    recency_days = web_tools._infer_recency_days(topic or "latest trend")

    entries, status_lines, fallback_used = await web_tools._collect_ranked_entries(
        queries=query_plan,
        sources=sources,
        prefer_mainland=bool(web_cfg.prefer_mainland),
        mainland_only=bool(web_cfg.mainland_only),
        recency_days=recency_days,
        max_feeds=max_feeds,
        items_per_feed=items_per_feed,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        retries=retries,
        top_k=top_k,
    )

    return web_tools._format_hotspot_brief(
        topic=topic,
        entries=entries,
        status_lines=status_lines,
        fallback_used=fallback_used,
        query_plan=query_plan,
        source_scope=source_scope,
        strategy_notes=strategy_notes,
    )


@tool(
    name="daily_digest",
    description=(
        "Generate a daily digest from all available RSS sources. "
        "Returns hot keywords, hot news with URLs, and per-item hotness scores."
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "Optional focus topic, e.g. 'AI' or 'middle east'.",
        },
        "channels": {
            "type": "string",
            "description": "Optional comma-separated channels, e.g. 'ai,tech,politics'.",
        },
        "max_items": {
            "type": "integer",
            "description": "Maximum news items in output (3-12). Default 8.",
        },
        "window_days": {
            "type": "integer",
            "description": "Recency window in days (1-7). Default 1.",
        },
    },
    required=[],
    catalog_summary="Build a daily brief from RSS feeds and time-windowed keywords.",
    catalog_entry_points=["daily digest", "morning brief", "daily news"],
    risk_level="low",
)
async def daily_digest(
    topic: str = "",
    channels: str = "",
    max_items: int = 8,
    window_days: int = 1,
) -> str:
    """Build a daily digest from RSS sources."""
    web_cfg, _, sources, source_scope, error = _load_filtered_rss_sources(
        workflow_label="Daily digest",
        topic=topic,
        channels=channels,
    )
    if error:
        return error
    if not sources:
        return "Daily digest unavailable: no RSS sources matched current filters."

    recency_days = max(1, min(int(window_days), 7))
    max_feeds = min(len(sources), 80)
    items_per_feed = max(5, min(int(web_cfg.rss_items_per_feed), 30))
    timeout_seconds = max(3, min(int(web_cfg.rss_timeout), 8))
    concurrency = max(8, min(int(getattr(web_cfg, "rss_concurrency", 8)), 16))
    retries = 1

    entries, status_lines = await web_tools._collect_daily_entries(
        sources=sources,
        recency_days=recency_days,
        max_feeds=max_feeds,
        items_per_feed=items_per_feed,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        retries=retries,
    )

    top_k = max(3, min(int(max_items), 12))
    hot_keywords = web_tools._compute_hot_keywords(entries, limit=8)
    ranked_entries = web_tools._sort_daily_items(entries, hot_keywords, top_k=top_k)

    return web_tools._format_daily_digest(
        topic=topic,
        entries=ranked_entries,
        status_lines=status_lines,
        source_scope=source_scope,
        hot_keywords=hot_keywords,
        recency_days=recency_days,
    )


@tool(
    name="paper_search",
    description=(
        "Search latest papers from arXiv/OpenAlex/Semantic Scholar, dedupe preprint vs journal, "
        "and return trend observations with confidence."
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "Paper topic, e.g. 'multimodal agents', 'quantum error correction'.",
        },
        "categories": {
            "type": "string",
            "description": "Optional comma-separated arXiv categories, e.g. 'cs.AI,cs.LG'.",
        },
        "max_items": {
            "type": "integer",
            "description": "Maximum papers in output (3-12). Default 8.",
        },
        "window_days": {
            "type": "integer",
            "description": "Recency window in days (1-180). Default inferred or 30.",
        },
        "sort_by": {
            "type": "string",
            "description": (
                "Sort mode: recent/citation/impact/balanced/author/institution. "
                "Default recent."
            ),
        },
        "author": {
            "type": "string",
            "description": "Optional author name hint for author-priority sorting.",
        },
        "institution": {
            "type": "string",
            "description": "Optional institution hint for institution-priority sorting.",
        },
        "providers": {
            "type": "string",
            "description": (
                "Comma-separated providers: arxiv,openalex,semantic_scholar. "
                "Default uses all."
            ),
        },
    },
    required=[],
    catalog_summary="Search recent papers across arXiv, OpenAlex, and Semantic Scholar.",
    catalog_entry_points=["latest papers", "arXiv search", "paper trends"],
    risk_level="low",
)
async def paper_search(
    topic: str = "",
    categories: str = "",
    max_items: int = 8,
    window_days: int = 0,
    sort_by: str = "recent",
    author: str = "",
    institution: str = "",
    providers: str = "",
) -> str:
    """Search multi-source papers with trend, quality tiers, and dedup summary."""
    topic_text = topic.strip() or "latest ai research"
    category_list = web_tools._parse_arxiv_categories(categories)
    if not category_list:
        category_list = web_tools._infer_arxiv_categories(topic_text)

    inferred_days = web_tools._infer_recency_days(topic_text) or 30
    recency_days = int(window_days) if window_days > 0 else inferred_days
    recency_days = max(1, min(recency_days, 180))
    limit = max(3, min(int(max_items), 12))

    query_text, terms = web_tools._build_arxiv_query(topic_text, category_list)
    provider_list = web_tools._parse_paper_providers(providers)
    query_plan, _ = web_tools._build_query_plan(
        topic_text,
        web_tools._infer_channels(topic_text),
    )
    semantic_query = query_plan[1] if len(query_plan) > 1 else query_plan[0]
    semantic_query = semantic_query or topic_text

    fetch_size = max(limit * 3, 24)
    provider_tasks: list[asyncio.Task[list[dict[str, Any]]]] = []
    provider_order: list[str] = []

    for provider in provider_list:
        if provider == "arxiv":
            provider_order.append(provider)
            provider_tasks.append(
                asyncio.create_task(web_tools._fetch_arxiv_papers(query_text, fetch_size))
            )
        elif provider == "openalex":
            provider_order.append(provider)
            provider_tasks.append(
                asyncio.create_task(
                    web_tools._fetch_openalex_papers(
                        query=semantic_query,
                        max_results=fetch_size,
                        recency_days=recency_days,
                    )
                )
            )
        elif provider == "semantic_scholar":
            provider_order.append(provider)
            provider_tasks.append(
                asyncio.create_task(
                    web_tools._fetch_semantic_scholar_papers(
                        query=semantic_query,
                        max_results=fetch_size,
                    )
                )
            )

    if not provider_tasks:
        return "Paper search unavailable: no valid providers configured."

    gathered = await asyncio.gather(*provider_tasks, return_exceptions=True)
    provider_counts: dict[str, int] = {}
    all_papers: list[dict[str, Any]] = []
    for provider_name, result in zip(provider_order, gathered):
        if isinstance(result, Exception):
            logger.warning("paper_search provider `%s` failed: %s", provider_name, result)
            provider_counts[provider_name] = 0
            continue
        provider_counts[provider_name] = len(result)
        all_papers.extend(result)

    if not all_papers:
        return (
            f"Paper search for `{topic_text}` found no provider results.\n"
            f"Providers: {', '.join(web_tools._paper_source_label(p) for p in provider_list)}\n"
            f"Query: {query_text}"
        )

    raw_count = len(all_papers)
    deduped_entries = web_tools._dedupe_papers(all_papers)
    for entry in deduped_entries:
        entry["quality_tier"] = web_tools._assign_quality_tier(entry)

    window_entries, in_window = web_tools._select_arxiv_entries(
        entries=deduped_entries,
        recency_days=recency_days,
        max_items=max(1, len(deduped_entries)),
    )
    sorted_entries = web_tools._sort_papers(
        papers=window_entries,
        sort_by=sort_by,
        author=author,
        institution=institution,
    )
    selected_entries = sorted_entries[:limit]

    return web_tools._format_paper_search_result(
        topic=topic_text,
        query_text=query_text,
        terms=terms,
        selected_entries=selected_entries,
        recency_days=recency_days,
        in_window=in_window,
        sort_by=sort_by,
        author=author,
        institution=institution,
        provider_counts=provider_counts,
        raw_count=raw_count,
        deduped_count=len(deduped_entries),
    )


@tool(
    name="wechat_article_assist",
    description=(
        "Assist WeChat article writing workflow with evidence grounding. "
        "Returns topic, outline, draft, fact-check, polish, and auto export outputs."
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "Article topic.",
        },
        "audience": {
            "type": "string",
            "description": "Target audience description.",
        },
        "goal": {
            "type": "string",
            "description": "Article goal, e.g. education, decision support.",
        },
        "style": {
            "type": "string",
            "description": "Writing style, e.g. professional concise.",
        },
        "length": {
            "type": "string",
            "description": "short/medium/long. Default medium.",
        },
        "evidence": {
            "type": "string",
            "description": "Optional notes or source URLs; tool also auto-collects evidence.",
        },
        "stage": {
            "type": "string",
            "description": (
                "all/roles/planner/researcher/drafter/critic/editor/"
                "topic/outline/draft/factcheck/polish/export. Default all."
            ),
        },
    },
    required=[],
    catalog_summary=(
        "Run the WeChat article workflow with evidence, article-role stages, "
        "drafting, and checks."
    ),
    catalog_entry_points=["WeChat article draft", "article outline", "fact-check article"],
    risk_level="medium",
)
async def wechat_article_assist(
    topic: str = "",
    audience: str = "",
    goal: str = "",
    style: str = "",
    length: str = "medium",
    evidence: str = "",
    stage: str = "all",
) -> str:
    """Generate WeChat article writing assistant output."""
    if not topic.strip():
        return "公众号写作辅助需要 `topic` 参数。"

    evidence_items, evidence_status = await web_tools._collect_wechat_evidence(
        topic=topic,
        evidence_text=evidence,
        max_items=8,
    )
    if not evidence_items:
        return (
            "公众号写作辅助未检索到可核验证据，当前不输出正文草稿，避免编造。\n"
            f"{evidence_status}\n"
            "请补充 3-6 条来源 URL 后重试，或把问题改成“过去30天”。"
        )

    verified_evidence_items = await web_tools._verify_evidence_urls(
        evidence_items=evidence_items,
        max_checks=8,
        timeout_seconds=8,
    )
    verification_status = web_tools._summarize_evidence_verification(verified_evidence_items)
    combined_status = f"{evidence_status}; {verification_status}"

    sections = web_tools._build_wechat_article_sections(
        topic=topic,
        audience=audience,
        goal=goal,
        style=style,
        length=length,
        evidence_items=verified_evidence_items,
        evidence_status=combined_status,
    )
    sections.update(
        web_tools._build_wechat_article_role_sections(
            topic=topic,
            audience=audience,
            goal=goal,
            style=style,
            length=length,
            evidence_items=verified_evidence_items,
            evidence_status=combined_status,
            sections=sections,
        )
    )

    key = stage.strip().lower() or "all"
    stage_aliases = {
        "fact_check": "factcheck",
        "fact-check": "factcheck",
        "plan": "planner",
        "research": "researcher",
        "writer": "drafter",
        "write": "drafter",
        "edit": "editor",
        "roles": "role_chain",
    }
    normalized = stage_aliases.get(key, key)
    should_export = key == "all" or normalized == "export"
    if should_export:
        export_result = web_tools._export_wechat_article_bundle(
            topic=topic,
            sections=sections,
            status_line=combined_status,
        )
        export_lines = [sections["export"], ""]
        if export_result.get("ok") == "true":
            export_lines.extend(
                [
                    "Auto export files:",
                    f"- Markdown: {export_result.get('md_path', export_result.get('md_rel', ''))}",
                    f"- HTML: {export_result.get('html_path', export_result.get('html_rel', ''))}",
                    "",
                    "This HTML can be pasted into WeChat editor directly.",
                ]
            )
        else:
            export_lines.extend(
                [
                    "Auto export failed:",
                    f"- {export_result.get('message', 'unknown error')}",
                    f"- Markdown target: {export_result.get('md_rel', '')}",
                    f"- HTML target: {export_result.get('html_rel', '')}",
                ]
            )
        sections["export"] = "\n".join(export_lines).strip()

    if key != "all":
        if normalized not in sections:
            supported = ", ".join(["all"] + sorted(sections.keys()))
            return f"Unknown stage `{stage}`. Supported: {supported}."
        return f"{combined_status}\n\n## {normalized}\n\n{sections[normalized]}"

    order = [
        "role_chain",
        "planner",
        "researcher",
        "drafter",
        "critic",
        "editor",
        "topic",
        "outline",
        "draft",
        "factcheck",
        "polish",
        "export",
    ]
    lines = [f"公众号写作辅助包：`{web_tools._normalize_text(topic)}`", combined_status, ""]
    for name in order:
        if name not in sections:
            continue
        lines.append(f"## {name}")
        lines.append(sections[name])
        lines.append("")
    return "\n".join(lines).strip()
