"""Deterministic query planning for web search providers."""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

RSS_CHANNEL_HINTS = {
    "ai": ["ai", "ml", "llm", "agent", "人工智能", "大模型", "智能体", "论文"],
    "quantum_mechanics": ["quantum", "qubit", "quant-ph", "量子", "量子力学"],
    "finance": ["finance", "crypto", "bitcoin", "经济", "油价", "汇率"],
    "investing": ["invest", "market", "trading", "fund", "投资", "股市", "指数"],
    "politics": [
        "politics",
        "policy",
        "world",
        "geopolitics",
        "iran",
        "israel",
        "middle east",
        "政治",
        "局势",
        "中东",
        "伊朗",
        "以色列",
        "冲突",
    ],
    "real_estate": ["real estate", "housing", "property", "房地产", "楼市"],
    "sports": ["sports", "nba", "football", "体育", "比赛"],
    "tech": ["tech", "technology", "startup", "chip", "科技", "芯片"],
}

ENTITY_TRANSLATIONS = {
    "伊朗": "iran",
    "以色列": "israel",
    "美国": "united states",
    "中国": "china",
    "俄罗斯": "russia",
    "乌克兰": "ukraine",
    "中东": "middle east",
    "欧盟": "european union",
}

TOPIC_TRANSLATIONS = {
    "核谈判": "nuclear talks",
    "核问题": "nuclear issue",
    "制裁": "sanctions",
    "冲突": "conflict",
    "代理人": "proxy",
    "油价": "oil price",
    "航运": "shipping",
    "霍尔木兹": "strait of hormuz",
    "外交": "diplomacy",
    "抗议": "protest",
    "经济": "economy",
    "选举": "election",
    "关税": "tariff",
    "论文": "paper arxiv preprint",
    "预印本": "preprint arxiv",
    "科技": "technology",
    "趋势": "trend",
    "视频生成": "video generation",
    "视频模型": "video model",
    "扩散模型": "diffusion model",
    "加速": "acceleration inference optimization",
    "推理加速": "inference acceleration",
    "蒸馏": "distillation",
    "缓存": "attention cache kv cache",
    "实时": "real-time",
}

CHANNEL_QUERY_HINTS = {
    "politics": ["diplomacy", "conflict", "sanctions", "middle east", "iran", "israel"],
    "investing": ["oil price", "shipping", "market", "risk sentiment"],
    "finance": ["economy", "policy", "sanctions", "capital flow"],
    "ai": ["agent", "model", "release", "arxiv"],
    "tech": ["industry", "platform", "launch", "chip"],
    "sports": ["match", "league", "tournament"],
    "quantum_mechanics": ["quantum", "qubit", "arxiv quant-ph"],
}

RECENCY_KEYWORDS = {
    "latest",
    "recent",
    "news",
    "trend",
    "trends",
    "today",
    "this week",
    "newest",
    "hotspot",
    "paper",
    "papers",
    "arxiv",
    "最新",
    "最近",
    "今日",
    "今天",
    "本周",
    "近一周",
    "近7天",
    "趋势",
    "热点",
    "论文",
}

TOPIC_NOISE_PATTERNS = [
    r"做一份",
    r"给我",
    r"请",
    r"简报",
    r"趋势",
    r"热点",
    r"最多\s*\d+\s*条",
    r"过去\s*\d+\s*天",
    r"近\s*\d+\s*天",
]

PAPER_HINTS = (
    "paper",
    "papers",
    "arxiv",
    "preprint",
    "citation",
    "论文",
    "文献",
    "预印本",
    "期刊",
)

NEWS_HINTS = (
    "news",
    "latest",
    "recent",
    "today",
    "current",
    "breaking",
    "headline",
    "brief",
    "日报",
    "简报",
    "热点",
    "最新",
    "最近",
    "今日",
    "今天",
)

CHINESE_WEB_HINTS = (
    "中文",
    "国内",
    "公众号",
    "微信公众号",
    "知乎",
    "微博",
    "小红书",
    "b站",
    "bilibili",
    "百度",
    "搜狗",
    "头条",
)

LONG_TAIL_HINTS = (
    "forum",
    "forums",
    "reddit",
    "hacker news",
    "hn",
    "discussion",
    "discussions",
    "issue",
    "issues",
    "github",
    "stack overflow",
    "review",
    "reviews",
    "experience",
    "踩坑",
    "讨论",
    "经验",
    "评测",
    "评价",
)

SITE_PATTERN = re.compile(r"\bsite:([a-z0-9.-]+\.[a-z]{2,})\b", re.IGNORECASE)


class SearchQueryPlan(BaseModel):
    """Normalized query plan shared by web search providers."""

    query: str
    intent: str
    category: str
    provider_hint: str
    time_range: str
    recency_days: Optional[int] = None
    language_hint: str = "auto"
    rss_channels: list[str] = Field(default_factory=list)
    site_filters: list[str] = Field(default_factory=list)
    engine_hints: list[str] = Field(default_factory=list)
    query_variants: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def primary_query(self) -> str:
        """Return the primary query variant."""
        if self.query_variants:
            return self.query_variants[0]
        return self.query


def _normalize_text(value: str) -> str:
    """Collapse whitespace and trim text."""
    return re.sub(r"\s+", " ", value).strip()


def _contains_cjk(text: str) -> bool:
    """Return True when text contains CJK characters."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _extract_day_window(text: str) -> Optional[int]:
    """Extract Chinese day-window hints."""
    match = re.search(r"(\d+)\s*天", text)
    if match:
        try:
            return max(1, min(int(match.group(1)), 90))
        except ValueError:
            return None

    cjk_days = {
        "一天": 1,
        "三天": 3,
        "七天": 7,
        "十天": 10,
        "半月": 15,
        "三十天": 30,
        "一周": 7,
        "两周": 14,
    }
    for token, value in cjk_days.items():
        if token in text:
            return value
    return None


def _extract_day_window_english(text: str) -> Optional[int]:
    """Extract English day-window hints."""
    lowered = text.lower()
    match = re.search(r"\b(last|past)\s+(\d{1,3})\s+days?\b", lowered)
    if match:
        try:
            return max(1, min(int(match.group(2)), 180))
        except ValueError:
            return None

    if "today" in lowered:
        return 1
    if "this week" in lowered or "past week" in lowered or "last week" in lowered:
        return 7
    if "this month" in lowered or "past month" in lowered or "last month" in lowered:
        return 30
    return None


def infer_recency_days(text: str) -> Optional[int]:
    """Infer recency days from query text."""
    explicit = _extract_day_window(text)
    if explicit:
        return explicit

    english_window = _extract_day_window_english(text)
    if english_window:
        return english_window

    lowered = text.lower()
    if any(keyword in lowered for keyword in RECENCY_KEYWORDS):
        return 30

    if any(keyword in text for keyword in RECENCY_KEYWORDS):
        return 30
    return None


def infer_channels(query: str) -> set[str] | None:
    """Infer RSS channels from query keywords."""
    query_lower = query.lower()
    selected = {
        channel_id
        for channel_id, hints in RSS_CHANNEL_HINTS.items()
        if any(hint in query_lower for hint in hints)
    }
    return selected or None


def _normalize_topic(topic: str) -> str:
    """Normalize topic by removing prompt noise phrases."""
    text = topic.strip().lower()
    for pattern in TOPIC_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    return _normalize_text(text)


def _extract_english_terms(text: str) -> list[str]:
    """Extract ordered English-like terms from text."""
    terms = [token for token in re.findall(r"[a-z0-9][a-z0-9-]{1,30}", text) if len(token) > 2]
    seen: set[str] = set()
    ordered: list[str] = []
    for token in terms:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _extract_site_filters(query: str) -> list[str]:
    """Extract explicit site filters such as site:example.com."""
    sites: list[str] = []
    seen: set[str] = set()
    for match in SITE_PATTERN.findall(query):
        site = match.strip().lower()
        if site in seen:
            continue
        seen.add(site)
        sites.append(site)
    return sites[:3]


def _infer_intent(
    query: str,
    *,
    recency_days: Optional[int],
    site_filters: list[str],
) -> str:
    """Classify the search request into one of the v1 planner intents."""
    lowered = query.lower()
    if site_filters:
        return "site"
    if any(hint in lowered for hint in PAPER_HINTS) or any(hint in query for hint in PAPER_HINTS):
        return "paper"
    if any(hint in lowered for hint in CHINESE_WEB_HINTS) or any(hint in query for hint in CHINESE_WEB_HINTS):
        return "chinese_web"
    if any(hint in lowered for hint in LONG_TAIL_HINTS) or any(hint in query for hint in LONG_TAIL_HINTS):
        return "long_tail"
    if recency_days is not None:
        return "news"
    if any(hint in lowered for hint in NEWS_HINTS) or any(hint in query for hint in NEWS_HINTS):
        return "news"
    return "web"


def _infer_provider_hint(intent: str) -> str:
    """Infer the preferred provider when config is in auto mode."""
    if intent == "paper":
        return "rss"
    return "auto"


def _infer_category(intent: str) -> str:
    """Infer provider category from planner intent."""
    if intent == "news":
        return "news"
    if intent == "paper":
        return "science"
    return "web"


def _infer_engine_hints(intent: str) -> list[str]:
    """Infer future engine hints for heterogeneous providers."""
    mapping = {
        "news": ["news", "web"],
        "paper": ["arxiv", "openalex", "semantic_scholar"],
        "site": ["web"],
        "chinese_web": ["web"],
        "long_tail": ["forum", "web"],
        "web": ["web"],
    }
    return list(mapping[intent])


def _infer_time_range(recency_days: Optional[int]) -> str:
    """Map recency days to a coarse time-range label."""
    if recency_days is None:
        return "all"
    if recency_days <= 1:
        return "1d"
    if recency_days <= 7:
        return "7d"
    if recency_days <= 30:
        return "30d"
    return f"{recency_days}d"


def build_query_variants(
    topic: str,
    channel_filters: set[str] | None,
    *,
    intent: str | None = None,
    recency_days: Optional[int] = None,
    site_filters: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Build bounded query variants and planning notes."""
    raw_topic = _normalize_text(topic)
    normalized = _normalize_topic(raw_topic)
    effective_channels = channel_filters or set()
    effective_sites = site_filters or []
    effective_intent = intent or "web"

    english_terms: list[str] = []
    english_terms.extend(_extract_english_terms(normalized))
    for zh, en in ENTITY_TRANSLATIONS.items():
        if zh in raw_topic:
            english_terms.append(en)
    for zh, en in TOPIC_TRANSLATIONS.items():
        if zh in raw_topic:
            english_terms.append(en)
    for channel in effective_channels:
        english_terms.extend(CHANNEL_QUERY_HINTS.get(channel, [])[:2])

    seen_terms: set[str] = set()
    deduped_terms: list[str] = []
    for term in english_terms:
        normalized_term = _normalize_text(term.lower())
        if not normalized_term or normalized_term in seen_terms:
            continue
        seen_terms.add(normalized_term)
        deduped_terms.append(normalized_term)

    queries: list[str] = []
    if raw_topic:
        queries.append(raw_topic)

    if effective_sites:
        queries.append(
            _normalize_text(
                raw_topic + " " + " ".join(f"site:{site}" for site in effective_sites[:2])
            )
        )

    base_alias = " ".join(deduped_terms[:6]).strip()
    if base_alias:
        queries.append(base_alias)
        if recency_days is not None:
            queries.append(_normalize_text(f"{base_alias} last {recency_days} days"))

    if effective_intent == "news":
        base_news = base_alias or raw_topic
        if base_news:
            queries.append(_normalize_text(f"{base_news} latest news"))
            if recency_days:
                queries.append(_normalize_text(f"{base_news} last {recency_days} days news"))
    elif effective_intent == "paper":
        base_paper = base_alias or raw_topic
        if base_paper:
            queries.append(_normalize_text(f"{base_paper} arxiv preprint"))
            queries.append(_normalize_text(f"{base_paper} research paper"))
    elif effective_intent == "chinese_web":
        queries.append(_normalize_text(f"{raw_topic} 中文"))
        if base_alias:
            queries.append(_normalize_text(f"{raw_topic} {base_alias.split(' ', 3)[0]}"))
    elif effective_intent == "long_tail":
        base_long_tail = base_alias or raw_topic
        if base_long_tail:
            queries.append(_normalize_text(f"{base_long_tail} discussion forum"))
            queries.append(_normalize_text(f"{base_long_tail} github issue reddit"))

    if _contains_cjk(raw_topic) and deduped_terms:
        queries.append(_normalize_text(f"{raw_topic} {' '.join(deduped_terms[:3])}"))

    if not queries:
        queries = ["hotspot latest"]

    compact_queries: list[str] = []
    seen_queries: set[str] = set()
    for item in queries:
        normalized_query = _normalize_text(item)
        if not normalized_query:
            continue
        key = normalized_query.lower()
        if key in seen_queries:
            continue
        seen_queries.add(key)
        compact_queries.append(normalized_query)
        if len(compact_queries) >= 4:
            break

    notes = [f"Intent: {effective_intent}."]
    if _contains_cjk(raw_topic):
        notes.append("Language hint: zh.")
    if recency_days is not None:
        notes.append(f"Time window hint detected: last {recency_days} days.")
    if effective_channels:
        notes.append("RSS channels: " + ", ".join(sorted(effective_channels)))
    if effective_sites:
        notes.append("Site filters: " + ", ".join(effective_sites))
    if deduped_terms and _contains_cjk(raw_topic):
        notes.append("Bilingual query variants enabled.")

    return compact_queries, notes


def build_search_plan(
    query: str,
    *,
    configured_provider: str = "rss",
) -> SearchQueryPlan:
    """Build a deterministic v1 search plan for one user query."""
    normalized_query = _normalize_text(query)
    site_filters = _extract_site_filters(normalized_query)
    recency_days = infer_recency_days(normalized_query)
    rss_channels = sorted(infer_channels(normalized_query) or [])
    intent = _infer_intent(
        normalized_query,
        recency_days=recency_days,
        site_filters=site_filters,
    )
    query_variants, notes = build_query_variants(
        normalized_query,
        set(rss_channels) if rss_channels else None,
        intent=intent,
        recency_days=recency_days,
        site_filters=site_filters,
    )
    configured = configured_provider.strip().lower() or "rss"
    notes.append(f"Configured provider: {configured}.")

    return SearchQueryPlan(
        query=normalized_query,
        intent=intent,
        category=_infer_category(intent),
        provider_hint=_infer_provider_hint(intent),
        time_range=_infer_time_range(recency_days),
        recency_days=recency_days,
        language_hint="zh" if _contains_cjk(normalized_query) else "en",
        rss_channels=rss_channels,
        site_filters=site_filters,
        engine_hints=_infer_engine_hints(intent),
        query_variants=query_variants,
        notes=notes,
    )


def select_search_provider(
    configured_provider: str,
    plan: SearchQueryPlan,
) -> str:
    """Select the effective provider after applying planner hints."""
    normalized = configured_provider.strip().lower() or "rss"
    if normalized in {"disabled", "off", "none"}:
        return "disabled"
    if normalized != "auto":
        return normalized
    if plan.provider_hint:
        return plan.provider_hint
    return "auto"


def format_search_plan(plan: SearchQueryPlan, provider: str) -> str:
    """Return a concise, user-visible planner summary."""
    parts = [
        "Search planner: "
        f"type={plan.intent}; provider={provider}; category={plan.category}; "
        f"time_range={plan.time_range}"
    ]
    if plan.rss_channels:
        parts.append("RSS channels: " + ", ".join(plan.rss_channels))
    if plan.site_filters:
        parts.append("Site filters: " + ", ".join(plan.site_filters))
    parts.append("Query variants: " + " | ".join(plan.query_variants[:3]))
    return "\n".join(parts)
