"""Atomic web search/fetch tools and helper functions with SSRF protections."""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import aiohttp

from nanoclaw.core.llm import ConnectionPool
from nanoclaw.core.logger import get_logger
from nanoclaw.core.rss_sources import RssSource, is_mainland_source, load_rss_sources
from nanoclaw.security.boundary import get_tool_boundary_policy
from nanoclaw.tools import search_normalizer
from nanoclaw.tools import search_planner
from nanoclaw.tools.registry import tool

logger = get_logger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

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

# Query planning hints for multilingual hotspot search.
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

HOTNESS_STOPWORDS_EN = {
    "with",
    "from",
    "that",
    "this",
    "will",
    "have",
    "into",
    "after",
    "about",
    "more",
    "their",
    "than",
    "new",
    "news",
    "latest",
    "update",
    "today",
    "world",
    "brief",
    "daily",
}

HOTNESS_STOPWORDS_ZH = {
    "今日",
    "今天",
    "最新",
    "新闻",
    "报道",
    "消息",
    "发布",
    "更新",
    "简报",
    "日报",
    "热点",
    "趋势",
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

ARXIV_API_URL = "https://export.arxiv.org/api/query"
OPENALEX_API_URL = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

ARXIV_TOPIC_CATEGORIES = {
    "ai": ["cs.AI", "cs.LG", "cs.CL", "stat.ML"],
    "agent": ["cs.AI", "cs.LG"],
    "llm": ["cs.CL", "cs.AI", "cs.LG"],
    "nlp": ["cs.CL"],
    "vision": ["cs.CV"],
    "robot": ["cs.RO"],
    "quantum": ["quant-ph"],
    "qubit": ["quant-ph"],
    "quant-ph": ["quant-ph"],
}

PAPER_TOKEN_STOPWORDS = {
    "with",
    "from",
    "this",
    "that",
    "using",
    "towards",
    "through",
    "learning",
    "model",
    "models",
    "paper",
    "analysis",
    "study",
    "approach",
    "based",
    "arxiv",
}

PAPER_SOURCE_LABELS = {
    "arxiv": "arXiv",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
}

# Shared search API rate limiter
_last_search_time: float = 0.0
_search_lock: asyncio.Lock | None = None


def _get_search_lock() -> asyncio.Lock:
    """Get the shared search-provider rate-limit lock lazily."""
    global _search_lock
    if _search_lock is None:
        _search_lock = asyncio.Lock()
    return _search_lock


def _get_web_search_config() -> Any:
    """Load web-search config, falling back to defaults when local config is unavailable."""
    from nanoclaw.core.config import Config, get_config

    try:
        return get_config().tools.web_search
    except Exception:
        return Config().tools.web_search


def _local_name(tag: str) -> str:
    """Return local XML tag name without namespace prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _normalize_text(value: str) -> str:
    """Collapse whitespace and trim text."""
    return re.sub(r"\s+", " ", value).strip()


def _strip_html(value: str) -> str:
    """Remove HTML tags from feed snippets."""
    text = re.sub(r"<[^>]+>", " ", value)
    return _normalize_text(text)

async def _check_outbound_url_policy(
    url: str,
    web_cfg: Any | None = None,
    *,
    operation: str = "",
) -> tuple[bool, str, str]:
    """Validate one outbound URL through the shared tool boundary policy."""
    policy = get_tool_boundary_policy()
    return await policy.validate_outbound_url(
        url,
        web_cfg=web_cfg,
        operation=operation,
    )


def _infer_channels(query: str) -> set[str] | None:
    """Infer channel filters from query keywords."""
    return search_planner.infer_channels(query)


def _contains_cjk(text: str) -> bool:
    """Return True if text contains CJK characters."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _extract_day_window(text: str) -> int | None:
    """Extract a day-window hint from query text."""
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


def _extract_day_window_english(text: str) -> int | None:
    """Extract a day-window hint from English query text."""
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


def _infer_recency_days(text: str) -> int | None:
    """Infer recency filter days from query text."""
    return search_planner.infer_recency_days(text)


def _normalize_topic(topic: str) -> str:
    """Normalize topic text by removing command noise phrases."""
    text = topic.strip().lower()
    for pattern in TOPIC_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    return _normalize_text(text)


def _extract_english_terms(text: str) -> list[str]:
    """Extract english tokens from text for query planning."""
    terms = [token for token in re.findall(r"[a-z0-9][a-z0-9-]{1,30}", text) if len(token) > 2]
    seen: set[str] = set()
    ordered: list[str] = []
    for token in terms:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _build_query_plan(
    topic: str,
    channel_filters: set[str] | None,
) -> tuple[list[str], list[str]]:
    """
    Build deterministic search queries and strategy notes.

    Goal: avoid relying on LLM-only translation for non-English topics.
    """
    return search_planner.build_query_variants(
        topic,
        channel_filters,
        recency_days=search_planner.infer_recency_days(topic),
    )


def _parse_channel_filters(channels: str) -> set[str] | None:
    """Parse comma-separated channel ids from user input."""
    if not channels.strip():
        return None
    values = {
        chunk.strip().lower()
        for chunk in channels.split(",")
        if chunk.strip()
    }
    return values or None


def _parse_arxiv_categories(categories: str) -> list[str]:
    """Parse comma-separated arXiv categories and keep valid items."""
    if not categories.strip():
        return []

    values: list[str] = []
    seen: set[str] = set()
    for chunk in categories.split(","):
        value = chunk.strip()
        if not value:
            continue
        if not re.fullmatch(r"[a-z\-]+(\.[A-Za-z\-]+)?", value):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
        if len(values) >= 8:
            break
    return values


def _parse_paper_providers(providers: str) -> list[str]:
    """Parse provider list for paper search."""
    normalized = providers.strip().lower()
    if not normalized:
        return ["arxiv", "openalex", "semantic_scholar"]

    mapping = {
        "arxiv": "arxiv",
        "openalex": "openalex",
        "semantic_scholar": "semantic_scholar",
        "semantic-scholar": "semantic_scholar",
        "semanticscholar": "semantic_scholar",
        "s2": "semantic_scholar",
    }
    selected: list[str] = []
    seen: set[str] = set()
    for token in normalized.split(","):
        key = token.strip()
        if not key:
            continue
        provider = mapping.get(key)
        if not provider or provider in seen:
            continue
        seen.add(provider)
        selected.append(provider)

    if not selected:
        return ["arxiv", "openalex", "semantic_scholar"]
    return selected


def _infer_arxiv_categories(topic: str) -> list[str]:
    """Infer likely arXiv categories from topic text."""
    normalized = _normalize_topic(topic or "")
    inferred: list[str] = []
    for token, categories in ARXIV_TOPIC_CATEGORIES.items():
        if token in normalized:
            inferred.extend(categories)

    if not inferred:
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for category in inferred:
        key = category.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(category)
    return deduped[:6]


def _build_arxiv_query(topic: str, categories: list[str]) -> tuple[str, list[str]]:
    """Build arXiv query text from topic and category constraints."""
    raw_topic = topic.strip() or "latest ai research"
    channel_filters = _infer_channels(raw_topic)
    query_plan, _ = _build_query_plan(raw_topic, channel_filters)
    seed = query_plan[1] if len(query_plan) > 1 else query_plan[0]
    terms = _extract_english_terms(seed.lower())
    if not terms:
        terms = _extract_english_terms(raw_topic.lower())
    if not terms:
        terms = ["ai", "research"]
    terms = terms[:5]

    text_clause = " AND ".join(f'all:"{term}"' for term in terms)
    category_clause = " OR ".join(f"cat:{category}" for category in categories)
    if text_clause and category_clause:
        return f"({text_clause}) AND ({category_clause})", terms
    if category_clause:
        return category_clause, terms
    return text_clause, terms


def _extract_arxiv_id_from_url(url: str) -> str:
    """Extract arXiv identifier from abs URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    match = re.search(r"/abs/([^/?#]+)", parsed.path)
    if not match:
        return ""
    return match.group(1).strip()


def _parse_arxiv_datetime(value: str) -> datetime | None:
    """Parse arXiv ISO datetime into timezone-aware UTC datetime."""
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_doi(value: str) -> str:
    """Normalize DOI text to canonical lowercase value."""
    text = _normalize_text(value).lower()
    if not text:
        return ""
    text = text.replace("https://doi.org/", "")
    text = text.replace("http://doi.org/", "")
    text = text.replace("doi:", "")
    return text.strip().strip("/")


def _normalize_arxiv_id(value: str) -> str:
    """Normalize arXiv id from plain id or URL and drop version suffix."""
    text = _normalize_text(value)
    if not text:
        return ""

    lowered = text.lower()
    if "arxiv.org/" in lowered:
        text = _extract_arxiv_id_from_url(text)
    elif lowered.startswith("arxiv:"):
        text = text.split(":", 1)[1]

    text = text.strip()
    text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
    return text.lower()


def _normalize_paper_title(title: str) -> str:
    """Normalize paper title for cross-provider deduplication."""
    lowered = title.lower()
    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", lowered)
    return _normalize_text(cleaned)


def _decode_openalex_abstract(index: Any) -> str:
    """Decode OpenAlex inverted-index abstract into plain text."""
    if not isinstance(index, dict):
        return ""

    tokens: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                tokens.append((pos, word))

    if not tokens:
        return ""

    tokens.sort(key=lambda item: item[0])
    text = " ".join(word for _, word in tokens)
    return _normalize_text(text)


def _paper_source_label(source: str) -> str:
    """Return user-friendly source label for one provider id."""
    return PAPER_SOURCE_LABELS.get(source, source)


def _safe_int(value: Any) -> int:
    """Parse integer safely."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _string_list(values: Any, limit: int = 8) -> list[str]:
    """Normalize mixed values into a unique list of strings."""
    if not isinstance(values, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _normalize_text(str(value))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _parse_arxiv_atom(xml_text: str) -> list[dict[str, Any]]:
    """Parse arXiv Atom response into structured entries."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    atom_ns = "{http://www.w3.org/2005/Atom}"
    entries: list[dict[str, Any]] = []
    for node in root.findall(f"{atom_ns}entry"):
        title = _normalize_text(node.findtext(f"{atom_ns}title", default="").strip())
        summary = _normalize_text(node.findtext(f"{atom_ns}summary", default="").strip())
        link = _normalize_text(node.findtext(f"{atom_ns}id", default="").strip())
        published = _normalize_text(
            node.findtext(f"{atom_ns}published", default="").strip()
        )
        updated = _normalize_text(node.findtext(f"{atom_ns}updated", default="").strip())

        for link_node in node.findall(f"{atom_ns}link"):
            href = (link_node.attrib.get("href") or "").strip()
            rel = (link_node.attrib.get("rel") or "").strip().lower()
            if href and rel in {"", "alternate"}:
                link = href
                break

        authors: list[str] = []
        for author_node in node.findall(f"{atom_ns}author"):
            name = _normalize_text(author_node.findtext(f"{atom_ns}name", default="").strip())
            if name:
                authors.append(name)

        categories: list[str] = []
        for category_node in node.findall(f"{atom_ns}category"):
            category = (category_node.attrib.get("term") or "").strip()
            if category:
                categories.append(category)

        if not title or not link:
            continue

        entries.append(
            {
                "title": title,
                "summary": summary,
                "url": link,
                "published": published,
                "updated": updated,
                "published_dt": _parse_arxiv_datetime(published),
                "authors": authors,
                "categories": categories,
            }
        )
    return entries


def _merge_unique_strings(first: list[str], second: list[str], limit: int = 12) -> list[str]:
    """Merge two string lists while keeping order and uniqueness."""
    merged: list[str] = []
    seen: set[str] = set()
    for value in first + second:
        text = _normalize_text(value)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
        if len(merged) >= limit:
            break
    return merged


def _is_preprint_paper(paper: dict[str, Any]) -> bool:
    """Infer whether a paper record is likely a preprint version."""
    source = str(paper.get("source", "")).lower()
    if source == "arxiv":
        return True

    source_type = str(paper.get("source_type", "")).lower()
    if source_type == "preprint":
        return True

    venue = str(paper.get("venue", "")).lower()
    return any(token in venue for token in ("arxiv", "preprint", "biorxiv", "medrxiv"))


def _paper_priority_key(paper: dict[str, Any]) -> tuple[int, int, int, int, int, int, int]:
    """Return ranking key for deduplication winner selection."""
    preprint_penalty = 0 if _is_preprint_paper(paper) else 1
    citations = max(0, _safe_int(paper.get("citations", 0)))
    has_doi = 1 if str(paper.get("doi", "")).strip() else 0
    has_org = 1 if paper.get("institutions") else 0
    has_abstract = 1 if str(paper.get("summary", "")).strip() else 0
    source_rank = {
        "openalex": 3,
        "semantic_scholar": 2,
        "arxiv": 1,
    }.get(str(paper.get("source", "")).lower(), 0)
    published_dt = paper.get("published_dt")
    published_ts = int(published_dt.timestamp()) if isinstance(published_dt, datetime) else 0
    return (
        preprint_penalty,
        citations,
        has_doi,
        has_org,
        source_rank,
        has_abstract,
        published_ts,
    )


def _paper_identity_keys(paper: dict[str, Any]) -> set[str]:
    """Build robust identity keys for cross-provider paper dedup."""
    keys: set[str] = set()
    doi = _normalize_doi(str(paper.get("doi", "")))
    if doi:
        keys.add(f"doi:{doi}")

    arxiv_id = _normalize_arxiv_id(str(paper.get("arxiv_id", "")))
    if not arxiv_id:
        arxiv_id = _normalize_arxiv_id(str(paper.get("url", "")))
    if arxiv_id:
        keys.add(f"arxiv:{arxiv_id}")

    title_key = _normalize_paper_title(str(paper.get("title", "")))
    if title_key:
        keys.add(f"title:{title_key}")

    return keys


def _merge_two_papers(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicated paper records and keep richer metadata."""
    if _paper_priority_key(right) > _paper_priority_key(left):
        left, right = right, left

    merged = dict(left)
    for key in ("title", "url", "summary", "published", "venue", "doi", "arxiv_id", "source_type"):
        if not str(merged.get(key, "")).strip():
            merged[key] = right.get(key, "")

    if not merged.get("published_dt") and right.get("published_dt"):
        merged["published_dt"] = right.get("published_dt")
    elif merged.get("published_dt") and right.get("published_dt"):
        if right["published_dt"] > merged["published_dt"]:
            merged["published_dt"] = right["published_dt"]

    merged["citations"] = max(
        _safe_int(merged.get("citations", 0)),
        _safe_int(right.get("citations", 0)),
    )
    merged["authors"] = _merge_unique_strings(
        _string_list(merged.get("authors", [])),
        _string_list(right.get("authors", [])),
    )
    merged["institutions"] = _merge_unique_strings(
        _string_list(merged.get("institutions", [])),
        _string_list(right.get("institutions", [])),
    )
    merged["categories"] = _merge_unique_strings(
        _string_list(merged.get("categories", [])),
        _string_list(right.get("categories", [])),
    )
    merged["sources"] = _merge_unique_strings(
        _string_list(merged.get("sources", [])),
        _string_list(right.get("sources", [])),
        limit=6,
    )
    if not merged.get("sources"):
        merged["sources"] = [_paper_source_label(str(merged.get("source", "")))]
    return merged


def _dedupe_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate paper list across providers."""
    sorted_papers = sorted(papers, key=_paper_priority_key, reverse=True)
    merged: list[dict[str, Any]] = []
    merged_keys: list[set[str]] = []

    for paper in sorted_papers:
        keys = _paper_identity_keys(paper)
        if not keys:
            merged.append(dict(paper))
            merged_keys.append(set())
            continue

        target_index = -1
        for idx, existing_keys in enumerate(merged_keys):
            if existing_keys & keys:
                target_index = idx
                break

        if target_index < 0:
            merged.append(dict(paper))
            merged_keys.append(set(keys))
            continue

        merged[target_index] = _merge_two_papers(merged[target_index], paper)
        merged_keys[target_index] |= keys | _paper_identity_keys(merged[target_index])

    return merged


def _assign_quality_tier(paper: dict[str, Any]) -> str:
    """Assign quality tier using citation count and publication signal."""
    citations = max(0, _safe_int(paper.get("citations", 0)))
    has_doi = bool(_normalize_doi(str(paper.get("doi", ""))))
    is_preprint = _is_preprint_paper(paper)

    if not is_preprint and citations >= 200:
        return "A"
    if not is_preprint and citations >= 50:
        return "B"
    if citations >= 15 or (has_doi and not is_preprint):
        return "C"
    return "D"


def _quality_rank(tier: str) -> int:
    """Map quality tier to sortable rank."""
    return {"A": 4, "B": 3, "C": 2, "D": 1}.get(tier, 0)


def _paper_sort_score(
    paper: dict[str, Any],
    sort_mode: str,
    author_hint: str,
    institution_hint: str,
    now_utc: datetime,
) -> tuple[float, float, float, float, float]:
    """Build sort score tuple according to selected mode."""
    published_dt = paper.get("published_dt")
    age_days = 3650.0
    if isinstance(published_dt, datetime):
        age_days = max(0.0, (now_utc - published_dt).total_seconds() / 86400.0)
    recency_score = -age_days
    citations = float(max(0, _safe_int(paper.get("citations", 0))))
    quality_score = float(_quality_rank(str(paper.get("quality_tier", ""))))

    author_text = " ".join(_string_list(paper.get("authors", []))).lower()
    institution_text = " ".join(_string_list(paper.get("institutions", []))).lower()
    author_match = 1.0 if author_hint and author_hint in author_text else 0.0
    institution_match = 1.0 if institution_hint and institution_hint in institution_text else 0.0

    if sort_mode in {"citation", "citations", "impact"}:
        return (citations, quality_score, recency_score, author_match, institution_match)
    if sort_mode == "author":
        return (author_match, citations, quality_score, recency_score, institution_match)
    if sort_mode == "institution":
        return (institution_match, citations, quality_score, recency_score, author_match)
    if sort_mode == "balanced":
        balance = (citations * 0.6) + (max(-30.0, recency_score) * 0.4) + quality_score
        return (balance, citations, recency_score, author_match, institution_match)
    return (recency_score, citations, quality_score, author_match, institution_match)


def _sort_papers(
    papers: list[dict[str, Any]],
    sort_by: str,
    author: str,
    institution: str,
) -> list[dict[str, Any]]:
    """Sort papers with optional author/institution weighting."""
    mode = sort_by.strip().lower() or "recent"
    if mode == "default":
        mode = "recent"
    author_hint = author.strip().lower()
    institution_hint = institution.strip().lower()
    now_utc = datetime.now(timezone.utc)
    return sorted(
        papers,
        key=lambda paper: _paper_sort_score(
            paper=paper,
            sort_mode=mode,
            author_hint=author_hint,
            institution_hint=institution_hint,
            now_utc=now_utc,
        ),
        reverse=True,
    )


def _paper_from_arxiv_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert arXiv Atom entry into normalized paper record."""
    url = str(entry.get("url", "")).strip()
    arxiv_id = _normalize_arxiv_id(url or str(entry.get("id", "")))
    return {
        "title": str(entry.get("title", "")).strip(),
        "summary": str(entry.get("summary", "")).strip(),
        "url": url,
        "published": str(entry.get("published", "")).strip(),
        "published_dt": entry.get("published_dt"),
        "authors": _string_list(entry.get("authors", [])),
        "institutions": [],
        "categories": _string_list(entry.get("categories", [])),
        "venue": "arXiv",
        "citations": 0,
        "doi": "",
        "arxiv_id": arxiv_id,
        "source": "arxiv",
        "source_type": "preprint",
        "sources": [_paper_source_label("arxiv")],
        "quality_tier": "",
    }


def _paper_from_openalex_work(work: dict[str, Any]) -> dict[str, Any] | None:
    """Convert OpenAlex work item into normalized paper record."""
    title = _normalize_text(str(work.get("display_name", "")))
    if not title:
        return None

    ids = work.get("ids", {}) if isinstance(work.get("ids"), dict) else {}
    doi = _normalize_doi(str(work.get("doi", "") or ids.get("doi", "")))
    arxiv_id = _normalize_arxiv_id(str(ids.get("arxiv", "")))
    publication_date = _normalize_text(str(work.get("publication_date", "")))
    published = publication_date
    if not published and work.get("publication_year"):
        published = f"{work.get('publication_year')}-01-01"

    primary_location = (
        work.get("primary_location", {})
        if isinstance(work.get("primary_location"), dict)
        else {}
    )
    source_obj = (
        primary_location.get("source", {})
        if isinstance(primary_location.get("source"), dict)
        else {}
    )
    venue = _normalize_text(str(source_obj.get("display_name", "")))
    if not venue:
        host_venue = work.get("host_venue", {})
        if isinstance(host_venue, dict):
            venue = _normalize_text(str(host_venue.get("display_name", "")))

    url = _normalize_text(str(primary_location.get("landing_page_url", "")))
    if not url and doi:
        url = f"https://doi.org/{doi}"
    if not url:
        url = _normalize_text(str(work.get("id", "")))

    authorships = work.get("authorships", [])
    authors: list[str] = []
    institutions: list[str] = []
    if isinstance(authorships, list):
        for node in authorships:
            if not isinstance(node, dict):
                continue
            author_obj = node.get("author", {}) if isinstance(node.get("author"), dict) else {}
            author_name = _normalize_text(str(author_obj.get("display_name", "")))
            if author_name:
                authors.append(author_name)
            inst_values = node.get("institutions", [])
            if isinstance(inst_values, list):
                for inst in inst_values:
                    if not isinstance(inst, dict):
                        continue
                    inst_name = _normalize_text(str(inst.get("display_name", "")))
                    if inst_name:
                        institutions.append(inst_name)

    abstract = _decode_openalex_abstract(work.get("abstract_inverted_index"))
    source_type = _normalize_text(str(work.get("type", ""))).lower()
    if not source_type:
        source_type = "journal"
    if _is_preprint_paper({"source": "openalex", "venue": venue, "source_type": source_type}):
        source_type = "preprint"

    return {
        "title": title,
        "summary": abstract,
        "url": url,
        "published": published,
        "published_dt": _parse_datetime_value(published),
        "authors": _string_list(authors),
        "institutions": _string_list(institutions),
        "categories": [],
        "venue": venue,
        "citations": max(0, _safe_int(work.get("cited_by_count", 0))),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source": "openalex",
        "source_type": source_type,
        "sources": [_paper_source_label("openalex")],
        "quality_tier": "",
    }


def _paper_from_semantic_scholar(item: dict[str, Any]) -> dict[str, Any] | None:
    """Convert Semantic Scholar result into normalized paper record."""
    title = _normalize_text(str(item.get("title", "")))
    if not title:
        return None

    external_ids = item.get("externalIds", {})
    if not isinstance(external_ids, dict):
        external_ids = {}
    doi = _normalize_doi(str(external_ids.get("DOI", "")))
    arxiv_id = _normalize_arxiv_id(str(external_ids.get("ArXiv", "")))

    publication_date = _normalize_text(str(item.get("publicationDate", "")))
    published = publication_date
    if not published and item.get("year"):
        published = f"{item.get('year')}-01-01"

    open_access = item.get("openAccessPdf", {})
    if not isinstance(open_access, dict):
        open_access = {}
    url = _normalize_text(str(open_access.get("url", "")))
    if not url:
        url = _normalize_text(str(item.get("url", "")))
    if not url and doi:
        url = f"https://doi.org/{doi}"

    authors: list[str] = []
    institutions: list[str] = []
    author_values = item.get("authors", [])
    if isinstance(author_values, list):
        for author in author_values:
            if not isinstance(author, dict):
                continue
            name = _normalize_text(str(author.get("name", "")))
            if name:
                authors.append(name)
            affiliations = author.get("affiliations", [])
            if isinstance(affiliations, list):
                for affiliation in affiliations:
                    text = _normalize_text(str(affiliation))
                    if text:
                        institutions.append(text)

    venue = _normalize_text(str(item.get("venue", "")))
    source_type = "preprint" if arxiv_id and not doi else "journal"
    if _is_preprint_paper(
        {"source": "semantic_scholar", "venue": venue, "source_type": source_type}
    ):
        source_type = "preprint"

    return {
        "title": title,
        "summary": _normalize_text(str(item.get("abstract", ""))),
        "url": url,
        "published": published,
        "published_dt": _parse_datetime_value(published),
        "authors": _string_list(authors),
        "institutions": _string_list(institutions),
        "categories": [],
        "venue": venue,
        "citations": max(0, _safe_int(item.get("citationCount", 0))),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source": "semantic_scholar",
        "source_type": source_type,
        "sources": [_paper_source_label("semantic_scholar")],
        "quality_tier": "",
    }


async def _fetch_arxiv_papers(query_text: str, max_results: int) -> list[dict[str, Any]]:
    """Fetch papers from arXiv API and normalize records."""
    encoded_query = quote_plus(query_text)
    request_url = (
        f"{ARXIV_API_URL}?search_query={encoded_query}"
        f"&start=0&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    allowed, _, reason = await _check_outbound_url_policy(request_url)
    if not allowed:
        logger.warning("Blocked arXiv paper fetch: %s", reason)
        return []
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            request_url,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            },
        ) as resp:
            if resp.status != 200:
                logger.warning("arXiv request failed: HTTP %s", resp.status)
                return []
            xml_text = await resp.text()
    except Exception as exc:
        logger.warning("arXiv paper fetch failed: %s", exc)
        return []

    parsed_entries = _parse_arxiv_atom(xml_text)
    return [_paper_from_arxiv_entry(entry) for entry in parsed_entries]


async def _fetch_openalex_papers(
    query: str,
    max_results: int,
    recency_days: int,
) -> list[dict[str, Any]]:
    """Fetch papers from OpenAlex API and normalize records."""
    since_date = (datetime.now(timezone.utc) - timedelta(days=max(1, recency_days))).date()
    params = {
        "search": query,
        "per-page": str(max_results),
        "sort": "publication_date:desc",
        "filter": f"from_publication_date:{since_date.isoformat()}",
    }
    allowed, _, reason = await _check_outbound_url_policy(OPENALEX_API_URL)
    if not allowed:
        logger.warning("Blocked OpenAlex paper fetch: %s", reason)
        return []
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            OPENALEX_API_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as resp:
            if resp.status != 200:
                logger.warning("OpenAlex request failed: HTTP %s", resp.status)
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("OpenAlex paper fetch failed: %s", exc)
        return []

    results = payload.get("results", []) if isinstance(payload, dict) else []
    papers: list[dict[str, Any]] = []
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            parsed = _paper_from_openalex_work(item)
            if parsed:
                papers.append(parsed)
    return papers


async def _fetch_semantic_scholar_papers(
    query: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fetch papers from Semantic Scholar API and normalize records."""
    params = {
        "query": query,
        "limit": str(max_results),
        "fields": (
            "title,url,abstract,publicationDate,year,venue,citationCount,"
            "authors,externalIds,openAccessPdf"
        ),
    }
    allowed, _, reason = await _check_outbound_url_policy(SEMANTIC_SCHOLAR_API_URL)
    if not allowed:
        logger.warning("Blocked Semantic Scholar paper fetch: %s", reason)
        return []
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            SEMANTIC_SCHOLAR_API_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as resp:
            if resp.status != 200:
                logger.warning("Semantic Scholar request failed: HTTP %s", resp.status)
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Semantic Scholar paper fetch failed: %s", exc)
        return []

    data = payload.get("data", []) if isinstance(payload, dict) else []
    papers: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            parsed = _paper_from_semantic_scholar(item)
            if parsed:
                papers.append(parsed)
    return papers


def _select_arxiv_entries(
    entries: list[dict[str, Any]],
    recency_days: int,
    max_items: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Select entries by recency; if no hits in window, fallback to latest entries."""
    now_utc = datetime.now(timezone.utc)
    threshold = now_utc - timedelta(days=recency_days)

    within_window = [
        entry for entry in entries
        if entry.get("published_dt") and entry["published_dt"] >= threshold
    ]
    if within_window:
        return within_window[:max_items], True
    return entries[:max_items], False


def _compute_paper_trend_signals(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute trend observations and confidence for paper entries."""
    category_counts: dict[str, int] = {}
    keyword_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    recency_known = 0
    citation_known = 0

    for entry in entries:
        categories = entry.get("categories", [])
        for category in categories:
            category_counts[category] = category_counts.get(category, 0) + 1

        sources = _string_list(entry.get("sources", []), limit=6)
        if not sources:
            source = str(entry.get("source", "")).strip()
            if source:
                sources = [_paper_source_label(source)]
        for source_name in sources:
            source_counts[source_name] = source_counts.get(source_name, 0) + 1

        title = str(entry.get("title", "")).lower()
        for token in re.findall(r"[a-z][a-z0-9-]{2,30}", title):
            if token in PAPER_TOKEN_STOPWORDS:
                continue
            keyword_counts[token] = keyword_counts.get(token, 0) + 1

        if entry.get("published_dt"):
            recency_known += 1
        if _safe_int(entry.get("citations", 0)) > 0:
            citation_known += 1

    dominant_categories = sorted(
        category_counts.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]
    top_keywords = sorted(
        keyword_counts.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:5]

    score = 0
    if len(entries) >= 8:
        score += 2
    elif len(entries) >= 4:
        score += 1
    if recency_known >= max(3, len(entries) // 2):
        score += 1
    if len(dominant_categories) >= 2:
        score += 1
    if len(source_counts) >= 2:
        score += 1
    if citation_known >= max(2, len(entries) // 3):
        score += 1

    if score >= 5:
        confidence = "high"
    elif score >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "dominant_categories": dominant_categories,
        "top_keywords": top_keywords,
        "source_counts": source_counts,
        "citation_known": citation_known,
        "confidence": confidence,
    }


def _format_paper_search_result(
    topic: str,
    query_text: str,
    terms: list[str],
    selected_entries: list[dict[str, Any]],
    recency_days: int,
    in_window: bool,
    sort_by: str,
    author: str,
    institution: str,
    provider_counts: dict[str, int],
    raw_count: int,
    deduped_count: int,
) -> str:
    """Format multi-source paper search result with trend observations."""
    topic_display = topic.strip() or "latest research"
    source_coverage = ", ".join(
        f"{_paper_source_label(name)}:{count}"
        for name, count in provider_counts.items()
    ) or "n/a"

    if not selected_entries:
        return (
            f"Paper search for `{topic_display}` found no usable entries.\n"
            f"Window: last {recency_days} day(s)\n"
            f"Query: {query_text}\n"
            f"Source coverage: {source_coverage}"
        )

    trend = _compute_paper_trend_signals(selected_entries)
    categories_text = ", ".join(
        f"{name}:{count}" for name, count in trend["dominant_categories"]
    ) or "n/a"
    keywords_text = ", ".join(
        f"{name}:{count}" for name, count in trend["top_keywords"]
    ) or "n/a"
    velocity = round(len(selected_entries) / max(recency_days, 1), 2)
    source_mix = ", ".join(
        f"{name}:{count}" for name, count in sorted(trend["source_counts"].items())
    ) or "n/a"

    lines = [f"Paper search (`multi-source`) for `{topic_display}`", ""]
    lines.append(f"Window: last {recency_days} day(s)")
    if not in_window:
        lines.append("Note: no entries strictly in window; showing latest available papers.")
    lines.append(f"Query: {query_text}")
    lines.append("Query terms: " + (", ".join(terms) if terms else "n/a"))
    lines.append(f"Sort mode: {sort_by or 'recent'}")
    if author.strip():
        lines.append(f"Author hint: {author.strip()}")
    if institution.strip():
        lines.append(f"Institution hint: {institution.strip()}")
    lines.append(f"Provider coverage: {source_coverage}")
    lines.append(
        "Deduplication: "
        f"raw={raw_count}, deduped={deduped_count}, selected={len(selected_entries)}"
    )
    lines.append("")
    lines.append("Trend signals:")
    lines.append(f"- Dominant categories: {categories_text}")
    lines.append(f"- Emerging keywords (title): {keywords_text}")
    lines.append(f"- Source mix: {source_mix}")
    lines.append(
        "- Velocity: "
        f"{len(selected_entries)} papers / {recency_days} day(s) ({velocity}/day)"
    )
    lines.append(f"- Confidence: {trend['confidence']}")
    lines.append("")
    lines.append("Top papers:")

    for idx, entry in enumerate(selected_entries, start=1):
        title = str(entry.get("title", "Untitled"))
        url = str(entry.get("url", ""))
        published = str(entry.get("published", "") or "").replace("T", " ").replace("Z", " UTC")
        authors = ", ".join(entry.get("authors", [])[:4]) or "n/a"
        institutions = ", ".join(entry.get("institutions", [])[:3]) or "n/a"
        categories = ", ".join(entry.get("categories", [])[:4]) or "n/a"
        venue = str(entry.get("venue", "")).strip() or "n/a"
        citations = max(0, _safe_int(entry.get("citations", 0)))
        quality_tier = str(entry.get("quality_tier", "")).strip() or "D"
        sources = ", ".join(_string_list(entry.get("sources", []), limit=5)) or "n/a"
        doi = str(entry.get("doi", "")).strip()
        summary = str(entry.get("summary", ""))[:220]

        lines.append(f"{idx}. {title}")
        lines.append(f"   {url}")
        if published:
            lines.append(f"   Published: {published}")
        lines.append(f"   Venue: {venue}")
        lines.append(f"   Quality tier: {quality_tier} | Citations: {citations}")
        lines.append(f"   Sources: {sources}")
        lines.append(f"   Authors: {authors}")
        lines.append(f"   Institutions: {institutions}")
        if categories != "n/a":
            lines.append(f"   Categories: {categories}")
        if doi:
            lines.append(f"   DOI: {doi}")
        if summary:
            lines.append(f"   {summary}")

    return "\n".join(lines)


def _resolve_article_length(length: str) -> tuple[int, str]:
    """Map article length hint to target word range."""
    lowered = length.strip().lower()
    if lowered in {"short", "简短"}:
        return 900, "800-1000 words"
    if lowered in {"long", "长文"}:
        return 2200, "1800-2500 words"
    return 1500, "1200-1800 words"


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract URLs from free-form text."""
    found = re.findall(r"https?://[^\s)>\]\"']+", text)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


async def _collect_topic_paper_evidence(
    topic: str,
    max_items: int,
    window_days: int,
) -> list[dict[str, str]]:
    """Collect paper evidence entries from arXiv for writing assistance."""
    topic_text = topic.strip() or "latest ai research"
    categories = _infer_arxiv_categories(topic_text)
    query_text, _ = _build_arxiv_query(topic_text, categories)
    encoded_query = quote_plus(query_text)
    request_url = (
        f"{ARXIV_API_URL}?search_query={encoded_query}"
        f"&start=0&max_results={max(max_items * 2, 16)}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    allowed, _, reason = await _check_outbound_url_policy(request_url)
    if not allowed:
        logger.warning("Blocked topic paper evidence fetch: %s", reason)
        return []

    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            request_url,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            },
        ) as resp:
            if resp.status != 200:
                return []
            xml_text = await resp.text()
    except Exception:
        return []

    parsed_entries = _parse_arxiv_atom(xml_text)
    selected_entries, _ = _select_arxiv_entries(
        entries=parsed_entries,
        recency_days=window_days,
        max_items=max_items,
    )
    output: list[dict[str, str]] = []
    for entry in selected_entries:
        output.append(
            {
                "kind": "paper",
                "source": "arXiv",
                "title": str(entry.get("title", "")).strip(),
                "url": str(entry.get("url", "")).strip(),
                "snippet": str(entry.get("summary", "")).strip()[:220],
                "published": str(entry.get("published", "")).strip(),
            }
        )
    return [item for item in output if item.get("title") and item.get("url")]


async def _collect_topic_rss_evidence(topic: str, max_items: int) -> list[dict[str, str]]:
    """Collect RSS evidence entries for writing assistance."""
    web_cfg = _get_web_search_config()

    sources_path = _resolve_sources_path(web_cfg.rss_sources_path)
    if not sources_path.exists():
        return []

    channel_filters = _infer_channels(topic)
    query_plan, _ = _build_query_plan(topic, channel_filters)
    recency_days = _infer_recency_days(topic) or 7

    try:
        sources = load_rss_sources(
            sources_path,
            channel_filters=channel_filters,
            prefer_mainland=web_cfg.prefer_mainland,
            mainland_only=web_cfg.mainland_only,
        )
    except Exception:
        return []

    if not sources:
        return []

    try:
        entries, _, _ = await _collect_ranked_entries(
            queries=query_plan,
            sources=sources,
            prefer_mainland=bool(web_cfg.prefer_mainland),
            mainland_only=bool(web_cfg.mainland_only),
            recency_days=recency_days,
            max_feeds=max(1, min(int(web_cfg.rss_max_feeds), 8)),
            items_per_feed=max(5, min(int(web_cfg.rss_items_per_feed), 10)),
            timeout_seconds=max(3, min(int(web_cfg.rss_timeout), 6)),
            concurrency=max(1, min(int(getattr(web_cfg, "rss_concurrency", 4)), 4)),
            retries=1,
            top_k=max_items,
            web_cfg=web_cfg,
        )
    except Exception:
        return []

    output: list[dict[str, str]] = []
    for entry in entries[:max_items]:
        output.append(
            {
                "kind": "news",
                "source": str(entry.get("source", "rss")),
                "title": str(entry.get("title", "")).strip(),
                "url": str(entry.get("url", "")).strip(),
                "snippet": str(entry.get("snippet", "")).strip()[:220],
                "published": str(entry.get("published_at", "")).strip(),
            }
        )
    return [item for item in output if item.get("title") and item.get("url")]


async def _collect_wechat_evidence(
    topic: str,
    evidence_text: str,
    max_items: int = 8,
) -> tuple[list[dict[str, str]], str]:
    """Merge user-provided links, RSS entries, and arXiv papers."""
    user_urls = _extract_urls_from_text(evidence_text)
    user_items: list[dict[str, str]] = []
    for url in user_urls[:max_items]:
        host = urlparse(url).netloc or "user"
        user_items.append(
            {
                "kind": "user",
                "source": f"user:{host}",
                "title": f"User provided source ({host})",
                "url": url,
                "snippet": "",
                "published": "",
            }
        )

    rss_task = asyncio.create_task(_collect_topic_rss_evidence(topic, max_items=max_items))
    paper_task = asyncio.create_task(
        _collect_topic_paper_evidence(topic, max_items=max_items, window_days=14)
    )
    rss_result, paper_result = await asyncio.gather(
        rss_task,
        paper_task,
        return_exceptions=True,
    )
    rss_items = rss_result if isinstance(rss_result, list) else []
    paper_items = paper_result if isinstance(paper_result, list) else []

    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in user_items + rss_items + paper_items:
        url = item.get("url", "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)
        if len(merged) >= max_items:
            break

    status = (
        "Evidence scope: "
        f"user={len(user_items)}, rss={len(rss_items)}, paper={len(paper_items)}, "
        f"selected={len(merged)}"
    )
    return merged, status


def _verification_status_label(status: str) -> str:
    """Map verification status to human-readable Chinese label."""
    mapping = {
        "ok": "可达",
        "failed": "失败",
        "blocked": "已拦截",
        "invalid": "无效URL",
        "skipped": "未检测",
    }
    return mapping.get(status, "未知")


def _summarize_evidence_verification(evidence_items: list[dict[str, str]]) -> str:
    """Build compact verification summary string."""
    counts = {"ok": 0, "failed": 0, "blocked": 0, "invalid": 0, "skipped": 0}
    for item in evidence_items:
        status = str(item.get("verify_status", "")).strip().lower()
        if status in counts:
            counts[status] += 1
    return (
        "Verification: "
        f"ok={counts['ok']}, failed={counts['failed']}, blocked={counts['blocked']}, "
        f"invalid={counts['invalid']}, skipped={counts['skipped']}"
    )


async def _verify_single_evidence_url(
    item: dict[str, str],
    timeout_seconds: int,
) -> dict[str, str]:
    """Verify one evidence URL reachability."""
    output = dict(item)
    url = str(item.get("url", "")).strip()
    output["verify_checked_at"] = datetime.now(timezone.utc).isoformat()
    output["verify_http"] = "-"
    output["verify_final_url"] = url

    if not url:
        output["verify_status"] = "invalid"
        output["verify_note"] = "empty_url"
        return output

    allowed, _, reason = await _check_outbound_url_policy(url)
    if not allowed:
        output["verify_status"] = "invalid" if reason.startswith("Invalid URL:") else "blocked"
        output["verify_note"] = (
            "unsupported_scheme_or_host"
            if reason.startswith("Invalid URL:")
            else "blocked_by_outbound_policy"
        )
        return output

    started_at = time.monotonic()
    try:
        session = await ConnectionPool.get_session()
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            },
            allow_redirects=True,
        ) as resp:
            await resp.content.read(256)
            latency_ms = int((time.monotonic() - started_at) * 1000)
            output["verify_http"] = str(resp.status)
            output["verify_latency_ms"] = str(latency_ms)
            output["verify_final_url"] = str(resp.url)
            if 200 <= resp.status < 400:
                output["verify_status"] = "ok"
                output["verify_note"] = "reachable"
            else:
                output["verify_status"] = "failed"
                output["verify_note"] = f"http_{resp.status}"
            return output
    except asyncio.TimeoutError:
        output["verify_status"] = "failed"
        output["verify_note"] = "timeout"
        output["verify_latency_ms"] = str(int((time.monotonic() - started_at) * 1000))
        return output
    except aiohttp.ClientError as exc:
        output["verify_status"] = "failed"
        output["verify_note"] = f"network_error:{type(exc).__name__}"
        output["verify_latency_ms"] = str(int((time.monotonic() - started_at) * 1000))
        return output
    except Exception as exc:
        output["verify_status"] = "failed"
        output["verify_note"] = f"unexpected_error:{type(exc).__name__}"
        output["verify_latency_ms"] = str(int((time.monotonic() - started_at) * 1000))
        return output


async def _verify_evidence_urls(
    evidence_items: list[dict[str, str]],
    max_checks: int = 8,
    timeout_seconds: int = 8,
) -> list[dict[str, str]]:
    """Verify evidence URLs in parallel and return enriched items."""
    if not evidence_items:
        return []

    limit = max(1, min(int(max_checks), len(evidence_items)))
    tasks = [
        asyncio.create_task(
            _verify_single_evidence_url(item, timeout_seconds=timeout_seconds)
        )
        for item in evidence_items[:limit]
    ]
    checked_results = await asyncio.gather(*tasks, return_exceptions=True)

    verified: list[dict[str, str]] = []
    for item, result in zip(evidence_items[:limit], checked_results):
        if isinstance(result, Exception):
            fallback = dict(item)
            fallback["verify_status"] = "failed"
            fallback["verify_http"] = "-"
            fallback["verify_note"] = "verification_internal_error"
            fallback["verify_final_url"] = str(item.get("url", ""))
            fallback["verify_checked_at"] = datetime.now(timezone.utc).isoformat()
            verified.append(fallback)
            continue
        verified.append(result)

    for item in evidence_items[limit:]:
        skipped = dict(item)
        skipped["verify_status"] = "skipped"
        skipped["verify_http"] = "-"
        skipped["verify_note"] = "check_limit_reached"
        skipped["verify_final_url"] = str(item.get("url", ""))
        skipped["verify_checked_at"] = datetime.now(timezone.utc).isoformat()
        verified.append(skipped)

    return verified


def _shorten_title(text: str, limit: int = 64) -> str:
    """Shorten long titles for compact article sections."""
    value = _normalize_text(text)
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _prioritize_evidence_for_writing(
    evidence_items: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Prioritize evidence entries by verification status for writing."""
    priority = {"ok": 3, "skipped": 2, "failed": 1, "blocked": 0, "invalid": 0}
    return sorted(
        evidence_items,
        key=lambda item: priority.get(str(item.get("verify_status", "")).lower(), 0),
        reverse=True,
    )


def _build_wechat_article_sections(
    topic: str,
    audience: str,
    goal: str,
    style: str,
    length: str,
    evidence_items: list[dict[str, str]],
    evidence_status: str,
) -> dict[str, str]:
    """Build WeChat article sections grounded in collected evidence."""
    normalized_topic = _normalize_text(topic) or "待定主题"
    audience_text = _normalize_text(audience) or "通用读者"
    goal_text = _normalize_text(goal) or "解释现象并给出可执行建议"
    style_text = _normalize_text(style) or "专业、清晰、克制"
    _, target_range = _resolve_article_length(length)

    if not evidence_items:
        topic_section = (
            f"未找到 `{normalized_topic}` 的可核验证据。请提供 3-6 条来源 URL 后再生成。"
        )
        outline_section = "暂无可用大纲（缺少证据）。"
        draft_section = "暂无初稿（缺少证据）。"
        fact_section = "暂无核查清单（缺少证据）。"
    else:
        prioritized_evidence = _prioritize_evidence_for_writing(evidence_items)
        usable_evidence = [
            item for item in prioritized_evidence
            if str(item.get("verify_status", "")).lower() in {"ok", "skipped"}
        ]
        if not usable_evidence:
            usable_evidence = prioritized_evidence

        verification_summary = _summarize_evidence_verification(evidence_items)
        verified_ok = sum(
            1 for item in evidence_items
            if str(item.get("verify_status", "")).lower() == "ok"
        )

        angles = ["核心技术突破", "工程加速与成本", "落地与风险边界"]
        topic_lines: list[str] = []
        for idx, angle in enumerate(angles, start=1):
            seed_a = usable_evidence[(idx - 1) % len(usable_evidence)]
            seed_b = usable_evidence[idx % len(usable_evidence)]
            topic_lines.append(f"{idx}) {normalized_topic} | {angle}")
            topic_lines.append(
                "   基本内容：围绕"
                f"《{_shorten_title(seed_a['title'])}》与《{_shorten_title(seed_b['title'])}》"
                "提炼本周关键变化与可执行决策。"
            )
            topic_lines.append(f"   - 证据A：{seed_a['url']}")
            topic_lines.append(f"   - 证据B：{seed_b['url']}")
        topic_lines.append("")
        topic_lines.append(evidence_status)
        topic_lines.append(verification_summary)
        topic_section = "\n".join(topic_lines)

        outline_lines = [
            "1. 开场：本周最关键的技术/产品信号（附 URL）",
            "2. 背景：为何“视频生成模型加速”成为当前约束点",
            "3. 核心变化A：算法侧加速（附证据）",
            "4. 核心变化B：系统侧加速（附证据）",
            "5. 核心变化C：工程落地与成本（附证据）",
            "6. 影响评估：对工程团队与技术管理的实际影响",
            "7. 行动清单：下周可执行事项",
            "8. 风险与边界：证据冲突点与待验证项",
        ]
        outline_section = "\n".join(outline_lines)

        draft_lines = [
            f"# {normalized_topic}",
            "",
            f"> 目标读者：{audience_text}",
            f"> 写作目标：{goal_text}",
            f"> 风格：{style_text}",
            f"> 目标篇幅：{target_range}",
            f"> 证据范围：{evidence_status}",
            f"> 核查摘要：{verification_summary}",
            "",
            "## 开场：本周值得关注的三条信号",
        ]
        for idx, item in enumerate(usable_evidence[:3], start=1):
            published = item.get("published", "")
            published_text = published[:10] if published else "日期待核"
            draft_lines.append(
                f"- 信号{idx}（{published_text}）：{_shorten_title(item['title'], 80)}"
            )
            draft_lines.append(f"  来源：{item['url']}")
            verify_label = _verification_status_label(str(item.get("verify_status", "")))
            verify_http = str(item.get("verify_http", "-"))
            draft_lines.append(f"  核查：{verify_label} (HTTP {verify_http})")

        draft_lines.append("")
        if verified_ok == 0:
            draft_lines.append(
                "## 核查提醒"
            )
            draft_lines.append(
                "当前证据未检测到可达 URL，以下内容仅基于摘要线索，请发布前逐条复核原文。"
            )
            draft_lines.append("")
        draft_lines.append("## 关键变化拆解")
        for idx, item in enumerate(usable_evidence[:4], start=1):
            snippet = item.get("snippet", "")
            if not snippet:
                snippet = "该来源需人工阅读全文以提取关键论据。"
            draft_lines.append(f"### 变化{idx}：{_shorten_title(item['title'], 80)}")
            draft_lines.append(
                "事实："
                + snippet[:180]
            )
            draft_lines.append(
                "解读：该信号提示团队需要同时评估模型质量、时延与成本三者的权衡。"
            )
            draft_lines.append(f"证据：{item['url']}")
            draft_lines.append("")

        draft_lines.extend(
            [
                "## 行动建议（下周）",
                "1. 建立统一加速基线：同一数据集比较不同加速方案的质量与吞吐。",
                "2. 先做低风险实验：将新方案放入离线评估，再逐步灰度上线。",
                "3. 维护证据台账：每条结论绑定 URL、日期、实验设置，避免口径漂移。",
                "",
                "## 风险与边界",
                "- 本稿仅基于公开来源与摘要信息，不替代原文阅读。",
                "- 若来源结论冲突，应在发布前补充对照实验或二次采访。",
            ]
        )
        draft_section = "\n".join(draft_lines)

        fact_lines = [
            "| 结论条目 | 来源URL | 日期 | HTTP | 核查状态 | 备注 |",
            "|---|---|---|---|---|---|",
        ]
        for item in evidence_items[:6]:
            published = item.get("published", "")
            published_text = published[:10] if published else "待核"
            verify_status = _verification_status_label(str(item.get("verify_status", "")))
            verify_http = str(item.get("verify_http", "-"))
            verify_note = str(item.get("verify_note", ""))
            final_url = str(item.get("verify_final_url", "")) or item["url"]
            fact_lines.append(
                "| "
                f"{_shorten_title(item['title'], 32)} | {final_url} | {published_text} | "
                f"{verify_http} | {verify_status} | {verify_note or '需人工复核原文细节'} |"
            )
        fact_section = "\n".join(fact_lines)

    polish_section = "\n".join(
        [
            "- 标题避免空泛，优先“对象 + 变化 + 影响”结构",
            "- 每段首句先给结论，再给证据",
            "- 删除无法核验的绝对化措辞（如“必然”“一定”）",
            "- 术语首次出现时给一句通俗解释",
            "- 结尾给可执行清单，不只给观点",
        ]
    )

    export_section = "\n".join(
        [
            "1) 先导出 Markdown：保留标题层级、链接、表格。",
            "2) 如需 HTML，可将 Markdown 交给现有 md->html 流程转换。",
            "3) 发布前检查：标题、封面图、摘要、原文链接完整性。",
        ]
    )

    return {
        "topic": topic_section,
        "outline": outline_section,
        "draft": draft_section,
        "factcheck": fact_section,
        "polish": polish_section,
        "export": export_section,
    }


def _build_wechat_article_role_sections(
    topic: str,
    audience: str,
    goal: str,
    style: str,
    length: str,
    evidence_items: list[dict[str, str]],
    evidence_status: str,
    sections: dict[str, str],
) -> dict[str, str]:
    """Build article-writing role sections on top of the classic workflow sections."""
    normalized_topic = _normalize_text(topic) or "待定主题"
    audience_text = _normalize_text(audience) or "通用读者"
    goal_text = _normalize_text(goal) or "解释现象并给出可执行建议"
    style_text = _normalize_text(style) or "专业、清晰、克制"
    _, target_range = _resolve_article_length(length)
    verification_summary = _summarize_evidence_verification(evidence_items)
    prioritized = _prioritize_evidence_for_writing(evidence_items)
    usable = [
        item for item in prioritized
        if str(item.get("verify_status", "")).lower() in {"ok", "skipped"}
    ] or prioritized

    headline_seed = _shorten_title(usable[0]["title"], 28) if usable else normalized_topic
    title_candidates = [
        f"{normalized_topic}: {headline_seed}背后的3个关键变化",
        f"一周读懂{normalized_topic}: 证据、分歧与行动建议",
        f"{normalized_topic}周报: 从信号到决策的公众号写法",
    ]

    role_chain = "\n".join(
        [
            "planner -> researcher -> drafter -> critic -> editor",
            "planner: 定文章角度、读者、篇幅和结构。",
            "researcher: 汇总证据、筛来源、标出可疑项。",
            "drafter: 基于已验证证据组织初稿，不额外发散。",
            "critic: 检查事实、证据覆盖和风险边界。",
            "editor: 收口标题、导语、段落节奏和发稿建议。",
        ]
    )

    planner_lines = [
        f"主题：{normalized_topic}",
        f"目标读者：{audience_text}",
        f"写作目标：{goal_text}",
        f"风格：{style_text}",
        f"目标篇幅：{target_range}",
        "",
        "标题候选：",
    ]
    planner_lines.extend([f"- {title}" for title in title_candidates])
    planner_lines.extend(
        [
            "",
            "写作结构：",
            sections.get("outline", "").strip(),
            "",
            "选题角度：",
            sections.get("topic", "").strip(),
        ]
    )

    researcher_lines = [
        evidence_status,
        verification_summary,
        "",
        "证据池：",
    ]
    if usable:
        for idx, item in enumerate(usable[:5], start=1):
            verify_label = _verification_status_label(str(item.get("verify_status", "")))
            published = str(item.get("published", "") or "").strip()
            published_text = published[:10] if published else "日期待核"
            researcher_lines.extend(
                [
                    f"{idx}. {item.get('title', '')}",
                    f"   URL: {item.get('url', '')}",
                    f"   类型: {item.get('kind', '')} | 来源: {item.get('source', '')}",
                    f"   日期: {published_text} | 核查: {verify_label}",
                    f"   摘要: {str(item.get('snippet', '') or '').strip()[:160] or '需阅读全文提取论据。'}",
                ]
            )
    else:
        researcher_lines.append("- 当前没有可用证据。")

    critic_notes: list[str] = []
    failed_items = [
        item for item in evidence_items
        if str(item.get("verify_status", "")).lower() in {"failed", "blocked", "invalid"}
    ]
    if failed_items:
        critic_notes.append("存在不可直接信赖的来源，发稿前需人工复核：")
        for item in failed_items[:3]:
            critic_notes.append(
                f"- {_shorten_title(str(item.get('title', '') or ''), 40)}"
                f" ({item.get('url', '')})"
            )
    if not any(str(item.get("verify_status", "")).lower() == "ok" for item in evidence_items):
        critic_notes.append("当前没有检测到可达来源，正文结论应降低确定性表述。")
    if not critic_notes:
        critic_notes.append("当前证据覆盖基本可用，但仍应在发布前抽查原文细节。")

    editor_lines = [
        "最终编辑要求：",
        sections.get("polish", "").strip(),
        "",
        "发稿前动作：",
        sections.get("export", "").strip(),
    ]

    return {
        "role_chain": role_chain,
        "planner": "\n".join(planner_lines).strip(),
        "researcher": "\n".join(researcher_lines).strip(),
        "drafter": sections.get("draft", "").strip(),
        "critic": "\n".join([sections.get("factcheck", "").strip(), "", *critic_notes]).strip(),
        "editor": "\n".join(editor_lines).strip(),
    }


def _slugify_export_name(topic: str) -> str:
    """Build safe export file-name segment from topic."""
    normalized = _normalize_text(topic).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not slug:
        return "article"
    return slug[:40]


def _markdown_inline_to_html(text: str) -> str:
    """Convert basic markdown inline syntax into safe HTML."""
    escaped = html.escape(text, quote=True)

    def _replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = html.escape(match.group(2), quote=True)
        return f'<a href="{url}">{label}</a>'

    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        _replace_link,
        escaped,
    )
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
    return escaped


def _markdown_to_basic_html(markdown_text: str) -> str:
    """Convert markdown to lightweight HTML for WeChat publishing."""
    lines = markdown_text.splitlines()
    output: list[str] = []
    in_code = False
    in_ul = False
    in_ol = False
    in_table = False
    table_header_done = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            output.append("</ul>")
            in_ul = False
        if in_ol:
            output.append("</ol>")
            in_ol = False

    def _close_table() -> None:
        nonlocal in_table, table_header_done
        if in_table:
            output.append("</tbody></table>")
            in_table = False
            table_header_done = False

    for raw_line in lines:
        line = raw_line.rstrip()

        if line.startswith("```"):
            _close_lists()
            _close_table()
            if not in_code:
                output.append("<pre><code>")
                in_code = True
            else:
                output.append("</code></pre>")
                in_code = False
            continue

        if in_code:
            output.append(html.escape(line))
            continue

        if not line.strip():
            _close_lists()
            _close_table()
            continue

        if "|" in line and line.count("|") >= 2:
            stripped = line.strip()
            if re.fullmatch(r"\|?[\s:\-]+\|[\s:\-|]*", stripped):
                continue

            _close_lists()
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not in_table:
                output.append('<table><thead><tr>')
                for cell in cells:
                    output.append(f"<th>{_markdown_inline_to_html(cell)}</th>")
                output.append("</tr></thead><tbody>")
                in_table = True
                table_header_done = True
                continue

            if not table_header_done:
                table_header_done = True
                continue

            output.append("<tr>")
            for cell in cells:
                output.append(f"<td>{_markdown_inline_to_html(cell)}</td>")
            output.append("</tr>")
            continue

        _close_table()

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            _close_lists()
            level = len(heading.group(1))
            content = _markdown_inline_to_html(heading.group(2).strip())
            output.append(f"<h{level}>{content}</h{level}>")
            continue

        if line.startswith(">"):
            _close_lists()
            content = _markdown_inline_to_html(line[1:].strip())
            output.append(f"<blockquote>{content}</blockquote>")
            continue

        ordered = re.match(r"^\d+\.\s+(.*)$", line)
        if ordered:
            if in_ul:
                output.append("</ul>")
                in_ul = False
            if not in_ol:
                output.append("<ol>")
                in_ol = True
            content = _markdown_inline_to_html(ordered.group(1).strip())
            output.append(f"<li>{content}</li>")
            continue

        unordered = re.match(r"^[-*]\s+(.*)$", line)
        if unordered:
            if in_ol:
                output.append("</ol>")
                in_ol = False
            if not in_ul:
                output.append("<ul>")
                in_ul = True
            content = _markdown_inline_to_html(unordered.group(1).strip())
            output.append(f"<li>{content}</li>")
            continue

        _close_lists()
        output.append(f"<p>{_markdown_inline_to_html(line.strip())}</p>")

    if in_code:
        output.append("</code></pre>")
    _close_lists()
    _close_table()
    return "\n".join(output)


def _render_wechat_html_document(title: str, body_html: str) -> str:
    """Wrap article body HTML into a standalone WeChat-friendly document."""
    safe_title = html.escape(title)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{safe_title}</title>\n"
        "  <style>\n"
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif; "
        "line-height: 1.7; color: #1f2937; max-width: 820px; margin: 0 auto; padding: 24px; }\n"
        "    h1, h2, h3, h4 { line-height: 1.35; }\n"
        "    blockquote { border-left: 4px solid #d1d5db; margin: 1em 0; padding: 0.5em 1em; "
        "color: #4b5563; background: #f9fafb; }\n"
        "    code { background: #f3f4f6; padding: 0.1em 0.35em; border-radius: 4px; }\n"
        "    pre { background: #111827; color: #f9fafb; padding: 12px; border-radius: 8px; "
        "overflow-x: auto; }\n"
        "    pre code { background: transparent; padding: 0; }\n"
        "    table { border-collapse: collapse; width: 100%; margin: 12px 0; }\n"
        "    th, td { border: 1px solid #e5e7eb; padding: 8px; text-align: left; "
        "vertical-align: top; }\n"
        "    th { background: #f9fafb; }\n"
        "    a { color: #2563eb; text-decoration: none; }\n"
        "    a:hover { text-decoration: underline; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )


def _write_workspace_file(relative_path: str, content: str) -> tuple[bool, str]:
    """Write one file under workspace through FileGuard validation."""
    policy = get_tool_boundary_policy()
    allowed, reason, safe_path = policy.validate_file_write(
        relative_path,
        operation="workflow_export_write",
    )
    if not allowed or safe_path is None:
        return False, reason

    try:
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow
        fd = os.open(str(safe_path), flags, 0o644)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno == 40:
            return False, f"ACCESS DENIED: symlink at write target: {relative_path}"
        return False, f"Error writing file: {exc}"
    except Exception as exc:
        return False, f"Error writing file: {exc}"

    return True, str(safe_path)


def _build_wechat_markdown_bundle(
    topic: str,
    sections: dict[str, str],
    status_line: str,
) -> str:
    """Build a single markdown document from article sections."""
    lines = [f"# {_normalize_text(topic)}", "", f"> {status_line}", ""]
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
    for name in order:
        if name not in sections:
            continue
        lines.append(f"## {name}")
        lines.append(sections.get(name, "").strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _export_wechat_article_bundle(
    topic: str,
    sections: dict[str, str],
    status_line: str,
) -> dict[str, str]:
    """Export wechat article sections to Markdown and HTML files."""
    slug = _slugify_export_name(topic)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slug}"
    md_rel = f"exports/wechat/{base}.md"
    html_rel = f"exports/wechat/{base}.html"

    markdown_text = _build_wechat_markdown_bundle(topic, sections, status_line)
    body_html = _markdown_to_basic_html(markdown_text)
    full_html = _render_wechat_html_document(topic, body_html)

    md_ok, md_msg = _write_workspace_file(md_rel, markdown_text)
    if not md_ok:
        return {
            "ok": "false",
            "message": f"Markdown export failed: {md_msg}",
            "md_rel": md_rel,
            "html_rel": html_rel,
        }

    html_ok, html_msg = _write_workspace_file(html_rel, full_html)
    if not html_ok:
        return {
            "ok": "false",
            "message": f"HTML export failed: {html_msg}",
            "md_rel": md_rel,
            "html_rel": html_rel,
            "md_path": md_msg,
        }

    return {
        "ok": "true",
        "message": "Export completed.",
        "md_rel": md_rel,
        "html_rel": html_rel,
        "md_path": md_msg,
        "html_path": html_msg,
    }


def _resolve_sources_path(raw_path: str) -> Path:
    """Resolve RSS source path with repo-root fallback."""
    candidate = Path(raw_path).expanduser()
    if candidate.exists():
        return candidate

    repo_root_candidate = Path(__file__).resolve().parents[2] / raw_path
    if repo_root_candidate.exists():
        return repo_root_candidate

    return candidate


def _entry_text(node: ET.Element, names: set[str]) -> str:
    """Find first non-empty text in descendants matching local names."""
    for child in node.iter():
        if _local_name(child.tag).lower() in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _entry_link(node: ET.Element) -> str:
    """Extract item/entry link from RSS or Atom nodes."""
    for child in node.iter():
        if _local_name(child.tag).lower() != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href:
            rel = child.attrib.get("rel", "").strip().lower()
            if rel in ("", "alternate"):
                return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def _parse_datetime_value(value: str) -> datetime | None:
    """Parse common RSS/Atom datetime strings into timezone-aware UTC datetime."""
    text = _normalize_text(value)
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _entry_published_at(node: ET.Element) -> str:
    """Extract normalized publication time for one feed item/entry."""
    date_fields = {"pubdate", "published", "updated", "modified", "issued", "date"}
    for child in node.iter():
        if _local_name(child.tag).lower() not in date_fields:
            continue
        raw = (child.text or "").strip()
        if not raw:
            continue
        parsed = _parse_datetime_value(raw)
        if parsed:
            return parsed.isoformat()
    return ""


def _entry_datetime(entry: dict[str, Any]) -> datetime | None:
    """Parse normalized publication time from entry dict."""
    published_at = str(entry.get("published_at", "") or "").strip()
    if not published_at:
        return None
    return _parse_datetime_value(published_at)


def _parse_feed_entries(xml_text: str) -> list[dict[str, Any]]:
    """Parse RSS/Atom feed text into normalized entries."""
    root = ET.fromstring(xml_text)
    root_name = _local_name(root.tag).lower()
    entries: list[dict[str, Any]] = []

    if root_name in {"rss", "rdf"}:
        for item in root.findall(".//item"):
            title = _entry_text(item, {"title"})
            link = _entry_link(item)
            snippet = _entry_text(item, {"description", "summary", "content"})
            published_at = _entry_published_at(item)
            if title and link:
                entries.append(
                    {
                        "title": _normalize_text(title),
                        "url": link,
                        "snippet": _strip_html(snippet),
                        "published_at": published_at,
                    }
                )
        return entries

    if root_name == "feed":
        atom_entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if not atom_entries:
            atom_entries = [
                node for node in root.iter() if _local_name(node.tag).lower() == "entry"
            ]
        for item in atom_entries:
            title = _entry_text(item, {"title"})
            link = _entry_link(item)
            snippet = _entry_text(item, {"summary", "content", "description"})
            published_at = _entry_published_at(item)
            if title and link:
                entries.append(
                    {
                        "title": _normalize_text(title),
                        "url": link,
                        "snippet": _strip_html(snippet),
                        "published_at": published_at,
                    }
                )
        return entries

    return entries


def _score_entry(query: str, title: str, snippet: str) -> int:
    """Score feed entry relevance to the query."""
    query_lower = query.lower().strip()
    terms = [t for t in re.findall(r"[a-z0-9]+", query_lower) if len(t) > 1]
    if not terms and query_lower:
        terms = [query_lower]

    title_lower = title.lower()
    snippet_lower = snippet.lower()
    score = 0
    for term in terms:
        if term in title_lower:
            score += 3
        if term in snippet_lower:
            score += 1
    return score


async def _fetch_feed_entries(
    session: aiohttp.ClientSession,
    source: RssSource,
    timeout_seconds: int,
    items_per_feed: int,
    retries: int,
    layer: str,
    web_cfg: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch and parse one feed source safely."""
    attempts = max(1, retries)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    config = web_cfg or _get_web_search_config()

    for attempt in range(attempts):
        current_url = source.url
        max_redirects = 3

        try:
            for _ in range(max_redirects):
                allowed, _, reason = await _check_outbound_url_policy(
                    current_url,
                    config,
                )
                if not allowed:
                    logger.warning("Blocked RSS source `%s`: %s", source.title, reason)
                    return []

                async with session.get(
                    current_url,
                    timeout=timeout,
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location:
                            return []
                        current_url = urljoin(current_url, location)
                        continue

                    if resp.status != 200:
                        return []

                    body = await resp.text(errors="replace")
                    break
            else:
                return []
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < attempts - 1:
                continue
            return []

        try:
            parsed_entries = _parse_feed_entries(body)
        except ET.ParseError:
            return []

        entries: list[dict[str, Any]] = []
        for entry in parsed_entries[:items_per_feed]:
            entries.append(
                {
                    "title": entry["title"],
                    "url": entry["url"],
                    "snippet": entry["snippet"][:260],
                    "published_at": str(entry.get("published_at", "")),
                    "source": source.title,
                    "channel": source.channel_id,
                    "layer": layer,
                    "tier": str(source.tier),
                }
            )
        return entries

    return []


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate feed entries by URL while preserving order."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        url = entry.get("url", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(entry)
    return deduped


async def _collect_feed_results(
    session: aiohttp.ClientSession,
    sources: list[RssSource],
    timeout_seconds: int,
    items_per_feed: int,
    concurrency: int,
    retries: int,
    layer: str,
    web_cfg: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fetch results from a set of RSS sources with bounded concurrency."""
    if not sources:
        return [], {"checked": 0, "ok_sources": 0, "failed_sources": 0}

    config = web_cfg or _get_web_search_config()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _fetch(source: RssSource) -> list[dict[str, Any]]:
        async with semaphore:
            return await _fetch_feed_entries(
                session=session,
                source=source,
                timeout_seconds=timeout_seconds,
                items_per_feed=items_per_feed,
                retries=retries,
                layer=layer,
                web_cfg=config,
            )

    results_per_source = await asyncio.gather(*[_fetch(source) for source in sources])
    ok_sources = sum(1 for entries in results_per_source if entries)
    failed_sources = len(sources) - ok_sources
    flat_results = [entry for entries in results_per_source for entry in entries]
    return flat_results, {
        "checked": len(sources),
        "ok_sources": ok_sources,
        "failed_sources": failed_sources,
    }


async def _collect_ranked_entries(
    queries: list[str],
    sources: list[RssSource],
    prefer_mainland: bool,
    mainland_only: bool,
    recency_days: int | None,
    max_feeds: int,
    items_per_feed: int,
    timeout_seconds: int,
    concurrency: int,
    retries: int,
    top_k: int,
    web_cfg: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Collect RSS entries and return ranked top-K items."""
    max_feeds = max(1, min(int(max_feeds), 50))
    items_per_feed = max(5, min(int(items_per_feed), 50))
    timeout_seconds = max(3, min(int(timeout_seconds), 30))
    concurrency = max(1, min(int(concurrency), 16))
    retries = max(1, min(int(retries), 3))
    top_k = max(1, min(int(top_k), 20))

    selected_sources = sources[:max_feeds]
    session = await ConnectionPool.get_session()
    flat_results: list[dict[str, Any]] = []
    status_lines: list[str] = []
    fallback_used = False

    use_mainland_first = bool(prefer_mainland and not mainland_only)
    if use_mainland_first:
        mainland_sources = [
            source for source in selected_sources if is_mainland_source(source.tags)
        ]
        global_sources = [
            source for source in selected_sources if not is_mainland_source(source.tags)
        ]

        primary_kwargs = {
            "session": session,
            "sources": mainland_sources,
            "timeout_seconds": timeout_seconds,
            "items_per_feed": items_per_feed,
            "concurrency": concurrency,
            "retries": retries,
            "layer": "primary",
        }
        if web_cfg is not None:
            primary_kwargs["web_cfg"] = web_cfg
        primary_results, primary_stats = await _collect_feed_results(**primary_kwargs)
        flat_results.extend(primary_results)
        status_lines.append(
            "Phase primary(mainland): "
            f"checked={primary_stats['checked']} ok={primary_stats['ok_sources']} "
            f"failed={primary_stats['failed_sources']}"
        )

        if global_sources and not primary_results:
            backup_kwargs = {
                "session": session,
                "sources": global_sources,
                "timeout_seconds": timeout_seconds,
                "items_per_feed": items_per_feed,
                "concurrency": concurrency,
                "retries": retries,
                "layer": "fallback",
            }
            if web_cfg is not None:
                backup_kwargs["web_cfg"] = web_cfg
            backup_results, backup_stats = await _collect_feed_results(**backup_kwargs)
            flat_results.extend(backup_results)
            fallback_used = bool(backup_results)
            status_lines.append(
                "Phase fallback(global): "
                f"checked={backup_stats['checked']} ok={backup_stats['ok_sources']} "
                f"failed={backup_stats['failed_sources']}"
            )
    else:
        default_kwargs = {
            "session": session,
            "sources": selected_sources,
            "timeout_seconds": timeout_seconds,
            "items_per_feed": items_per_feed,
            "concurrency": concurrency,
            "retries": retries,
            "layer": "primary",
        }
        if web_cfg is not None:
            default_kwargs["web_cfg"] = web_cfg
        default_results, default_stats = await _collect_feed_results(**default_kwargs)
        flat_results.extend(default_results)
        status_lines.append(
            "Phase primary: "
            f"checked={default_stats['checked']} ok={default_stats['ok_sources']} "
            f"failed={default_stats['failed_sources']}"
        )

    deduped = _dedupe_entries(flat_results)
    if not deduped:
        return [], status_lines, fallback_used

    candidates, recency_status = _apply_recency_filter(deduped, recency_days)
    if recency_status:
        status_lines.append(recency_status)

    if not candidates:
        return [], status_lines, fallback_used

    effective_queries = [q for q in queries if q.strip()] or ["hotspot latest"]
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for entry in candidates:
        score = max(
            _score_entry(q, entry["title"], entry["snippet"])
            for q in effective_queries
        )
        published_dt = _entry_datetime(entry)
        published_ts = published_dt.timestamp() if published_dt else 0.0
        scored.append((score, published_ts, entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    ranked = [entry for score, _, entry in scored if score > 0][:top_k]
    if not ranked:
        status_lines.append("Relevance filter: no entries matched query keywords.")
        return [], status_lines, fallback_used

    return ranked, status_lines, fallback_used


def _apply_recency_filter(
    entries: list[dict[str, Any]],
    recency_days: int | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Apply recency filtering while preserving undated items."""
    if recency_days is None or recency_days <= 0:
        return entries, None

    cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
    recent_entries: list[dict[str, Any]] = []
    undated_entries: list[dict[str, Any]] = []
    stale_count = 0

    for entry in entries:
        published_dt = _entry_datetime(entry)
        if published_dt is None:
            undated_entries.append(entry)
            continue
        if published_dt >= cutoff:
            recent_entries.append(entry)
        else:
            stale_count += 1

    if recent_entries:
        kept = recent_entries + undated_entries
        status = (
            "Recency filter: "
            f"last={recency_days}d kept_recent={len(recent_entries)} "
            f"undated={len(undated_entries)} dropped_stale={stale_count}"
        )
        return kept, status

    kept = undated_entries
    status = (
        "Recency filter: "
        f"last={recency_days}d no_recent_dated_items; "
        f"undated={len(undated_entries)} dropped_stale={stale_count}"
    )
    return kept, status


async def _search_with_rss(query: str) -> tuple[str, bool]:
    """Search query across configured RSS sources."""
    web_cfg = _get_web_search_config()

    sources_path = _resolve_sources_path(web_cfg.rss_sources_path)
    if not sources_path.exists():
        return (
            "RSS search unavailable: source registry not found at "
            f"`{web_cfg.rss_sources_path}`.",
            False,
        )

    channel_filters = _infer_channels(query)
    query_plan, _ = _build_query_plan(query, channel_filters)
    recency_days = _infer_recency_days(query)
    try:
        sources = load_rss_sources(
            sources_path,
            channel_filters=channel_filters,
            prefer_mainland=web_cfg.prefer_mainland,
            mainland_only=web_cfg.mainland_only,
        )
    except Exception as exc:
        logger.error(f"Failed to load RSS sources: {exc}")
        return (f"RSS search unavailable: failed to load source registry ({exc}).", False)

    if not sources:
        return ("RSS source list is empty for current filters.", False)

    max_feeds = max(1, min(int(web_cfg.rss_max_feeds), 50))
    items_per_feed = max(5, min(int(web_cfg.rss_items_per_feed), 50))
    timeout_seconds = max(3, min(int(web_cfg.rss_timeout), 30))
    concurrency = max(1, min(int(getattr(web_cfg, "rss_concurrency", 8)), 16))
    retries = max(1, min(int(getattr(web_cfg, "rss_retries", 1)), 3))

    return await _search_with_rss_sources(
        query=query,
        sources=sources,
        prefer_mainland=bool(web_cfg.prefer_mainland),
        mainland_only=bool(web_cfg.mainland_only),
        query_plan=query_plan,
        recency_days=recency_days,
        max_feeds=max_feeds,
        items_per_feed=items_per_feed,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        retries=retries,
        web_cfg=web_cfg,
    )


async def _search_with_rss_plan(
    plan: search_planner.SearchQueryPlan,
) -> tuple[str, bool]:
    """Search RSS sources using an already-built planner output."""
    web_cfg = _get_web_search_config()

    sources_path = _resolve_sources_path(web_cfg.rss_sources_path)
    if not sources_path.exists():
        return (
            "RSS search unavailable: source registry not found at "
            f"`{web_cfg.rss_sources_path}`.",
            False,
        )

    channel_filters = set(plan.rss_channels) if plan.rss_channels else None
    try:
        sources = load_rss_sources(
            sources_path,
            channel_filters=channel_filters,
            prefer_mainland=web_cfg.prefer_mainland,
            mainland_only=web_cfg.mainland_only,
        )
    except Exception as exc:
        logger.error(f"Failed to load RSS sources: {exc}")
        return (f"RSS search unavailable: failed to load source registry ({exc}).", False)

    if not sources:
        return ("RSS source list is empty for current filters.", False)

    max_feeds = max(1, min(int(web_cfg.rss_max_feeds), 50))
    items_per_feed = max(5, min(int(web_cfg.rss_items_per_feed), 50))
    timeout_seconds = max(3, min(int(web_cfg.rss_timeout), 30))
    concurrency = max(1, min(int(getattr(web_cfg, "rss_concurrency", 8)), 16))
    retries = max(1, min(int(getattr(web_cfg, "rss_retries", 1)), 3))

    return await _search_with_rss_sources(
        query=plan.query,
        sources=sources,
        prefer_mainland=bool(web_cfg.prefer_mainland),
        mainland_only=bool(web_cfg.mainland_only),
        query_plan=plan.query_variants,
        recency_days=plan.recency_days,
        max_feeds=max_feeds,
        items_per_feed=items_per_feed,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        retries=retries,
        web_cfg=web_cfg,
    )


async def _search_with_rss_sources(
    query: str,
    sources: list[RssSource],
    prefer_mainland: bool,
    mainland_only: bool,
    max_feeds: int,
    items_per_feed: int,
    timeout_seconds: int,
    concurrency: int,
    retries: int,
    query_plan: list[str] | None = None,
    recency_days: int | None = None,
    web_cfg: Any | None = None,
) -> tuple[str, bool]:
    """Search query across preloaded RSS sources."""
    effective_plan = query_plan or _build_query_plan(query, _infer_channels(query))[0]
    effective_recency = recency_days if recency_days is not None else _infer_recency_days(query)

    top_entries, status_lines, fallback_used = await _collect_ranked_entries(
        queries=effective_plan,
        sources=sources,
        prefer_mainland=prefer_mainland,
        mainland_only=mainland_only,
        recency_days=effective_recency,
        max_feeds=max_feeds,
        items_per_feed=items_per_feed,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        retries=retries,
        top_k=5,
        web_cfg=web_cfg,
    )

    if not top_entries:
        details = " | ".join(status_lines) if status_lines else "no sources available"
        return (f"No RSS results found for this query. ({details})", False)

    chunks = []
    for entry in top_entries:
        snippet = entry["snippet"] or "No snippet."
        published_at = str(entry.get("published_at", "") or "").strip()
        if published_at:
            published_line = f"Published: {published_at}\n"
        else:
            published_line = ""
        chunks.append(
            f"**{entry['title']}**\n{entry['url']}\n{snippet}\n"
            f"{published_line}"
            "Source: "
            f"{entry['source']} | Channel: {entry['channel']} | "
            f"Layer: {entry['layer']} | Tier: {entry['tier']} | Provider: rss"
        )
    prefix = ""
    if fallback_used:
        prefix = "Mainland-first fallback triggered: switched to global sources.\n\n"
    plan_text = " | ".join(effective_plan[:4]) if effective_plan else "n/a"
    recency_text = (
        f"Recency window: last {effective_recency} days"
        if effective_recency is not None
        else "Recency window: none"
    )
    status_text = "\n".join(status_lines)
    return (
        f"{prefix}{recency_text}\nQuery plan: {plan_text}\n{status_text}\n\n"
        + "\n\n".join(chunks),
        True,
    )


async def _search_with_brave(query: str, api_key: str) -> str:
    """Search using Brave Search API."""
    global _last_search_time

    async with _get_search_lock():
        now = time.time()
        elapsed = now - _last_search_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        _last_search_time = time.time()

    session = await ConnectionPool.get_session()
    max_retries = 3
    last_error = ""

    for attempt in range(max_retries):
        try:
            async with session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 5},
                headers={"X-Subscription-Token": api_key, "User-Agent": DEFAULT_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return "Brave search rate limited. Try again later."
                if resp.status != 200:
                    return f"Brave search failed: HTTP {resp.status}"
                data = await resp.json()
                break
        except aiohttp.ClientError as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            return f"Brave search failed: {last_error}"
        except Exception as exc:
            return f"Brave search error: {exc}"
    else:
        return f"Brave search failed after {max_retries} retries: {last_error}"

    results = []
    for item in data.get("web", {}).get("results", [])[:5]:
        results.append(
            f"**{item['title']}**\n{item['url']}\n{item.get('description', '')}\n"
            "Source: Brave | Provider: brave"
        )

    return "\n\n".join(results) if results else "No Brave results found."


def _append_provider_note(text: str, note: str) -> str:
    """Append one provider note when available."""
    if not note:
        return text
    if note in text:
        return text
    if not text:
        return note
    return f"{text}\n\n{note}"


async def _consume_serper_quota(max_calls: int) -> tuple[bool, str]:
    """Consume one Serper call from local quota tracking."""
    max_calls = max(0, int(max_calls))
    if max_calls <= 0:
        return True, ""

    from nanoclaw.security.audit import get_audit_log

    quota = await get_audit_log().consume_provider_call("serper", max_calls)
    remaining = int(quota["remaining_calls"])
    note = f"Serper quota remaining: {remaining}/{max_calls}"
    logger.info(note)
    if not bool(quota["allowed"]):
        return False, f"Serper search quota exhausted.\n{note}"
    return True, note


async def _search_with_serper(
    query: str,
    api_key: str,
    *,
    gl: str = "world",
    hl: str = "en",
    max_calls: int = 0,
    mode: str = "web",
    tbs: str | None = None,
) -> str:
    """Search using Serper's Google Search API."""
    global _last_search_time

    quota_allowed, quota_note = await _consume_serper_quota(max_calls)
    if not quota_allowed:
        return quota_note

    async with _get_search_lock():
        now = time.time()
        elapsed = now - _last_search_time
        if elapsed < 1.0:
            await asyncio.sleep(1.0 - elapsed)
        _last_search_time = time.time()

    session = await ConnectionPool.get_session()
    payload: dict[str, Any] = {"q": query, "num": 5}
    if mode != "news":
        payload["autocorrect"] = True
    if hl:
        payload["hl"] = hl
    if gl and gl != "world":
        payload["gl"] = gl
    if tbs and mode != "news":
        payload["tbs"] = tbs

    endpoint = (
        "https://google.serper.dev/news"
        if mode == "news"
        else "https://google.serper.dev/search"
    )

    max_retries = 3
    last_error = ""

    for attempt in range(max_retries):
        try:
            async with session.post(
                endpoint,
                json=payload,
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    return _append_provider_note(
                        "Serper search failed: invalid API key.",
                        quota_note,
                    )
                if resp.status == 429:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return _append_provider_note(
                        "Serper search rate limited. Try again later.",
                        quota_note,
                    )
                if resp.status != 200:
                    return _append_provider_note(
                        f"Serper search failed: HTTP {resp.status}",
                        quota_note,
                    )
                data = await resp.json()
                break
        except aiohttp.ClientError as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            return _append_provider_note(
                f"Serper search failed: {last_error}",
                quota_note,
            )
        except Exception as exc:
            return _append_provider_note(
                f"Serper search error: {exc}",
                quota_note,
            )
    else:
        return _append_provider_note(
            f"Serper search failed after {max_retries} retries: {last_error}",
            quota_note,
        )

    results: list[str] = []
    if mode == "news":
        for item in data.get("news", [])[:5]:
            title = str(item.get("title", "") or "").strip()
            url = str(item.get("link", "") or "").strip()
            snippet = str(item.get("snippet", "") or "").strip()
            if not title or not url:
                continue
            lines = [f"**{title}**", url]
            if snippet:
                lines.append(snippet)
            date = str(item.get("date", "") or "").strip()
            if date:
                lines.append(f"Date: {date}")
            lines.append("Source: Serper News | Provider: serper")
            results.append("\n".join(lines))
    else:
        answer_box = data.get("answerBox", {})
        if answer_box:
            answer = (
                answer_box.get("answer")
                or answer_box.get("snippet")
                or answer_box.get("title")
                or ""
            ).strip()
            link = str(answer_box.get("link", "") or "").strip()
            if answer:
                prefix = f"**{answer_box.get('title', 'Answer Box')}**\n"
                suffix = f"\n{link}" if link else ""
                results.append(
                    f"{prefix}{answer}{suffix}\nSource: Serper | Provider: serper"
                )

        knowledge_graph = data.get("knowledgeGraph", {})
        if knowledge_graph:
            title = str(
                knowledge_graph.get("title", "Knowledge Graph") or "Knowledge Graph"
            ).strip()
            description = str(knowledge_graph.get("description", "") or "").strip()
            link = str(knowledge_graph.get("website", "") or "").strip()
            attributes = knowledge_graph.get("attributes", {}) or {}
            attr_text = ", ".join(
                f"{key}: {value}" for key, value in attributes.items() if str(value).strip()
            )
            lines = [f"**{title}**"]
            if description:
                lines.append(description)
            if attr_text:
                lines.append(attr_text)
            if link:
                lines.append(link)
            if len(lines) > 1:
                lines.append("Source: Serper | Provider: serper")
                results.append("\n".join(lines))

        for item in data.get("organic", [])[:5]:
            title = str(item.get("title", "") or "").strip()
            url = str(item.get("link", "") or "").strip()
            snippet = str(item.get("snippet", "") or "").strip()
            if not title or not url:
                continue
            lines = [f"**{title}**", url]
            if snippet:
                lines.append(snippet)
            date = str(item.get("date", "") or "").strip()
            if date:
                lines.append(f"Date: {date}")
            lines.append("Source: Serper | Provider: serper")
            results.append("\n".join(lines))

    base = "\n\n".join(results) if results else "No Serper results found."
    return _append_provider_note(base, quota_note)


def _extract_hot_terms(text: str) -> set[str]:
    """Extract candidate hot terms from text for daily digest scoring."""
    lowered = text.lower()
    terms: set[str] = set()

    for token in re.findall(r"[a-z][a-z0-9-]{1,24}", lowered):
        if token in HOTNESS_STOPWORDS_EN:
            continue
        terms.add(token)

    for token in re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        if token in HOTNESS_STOPWORDS_ZH:
            continue
        terms.add(token)

    return terms


def _tier_weight(entry: dict[str, Any]) -> int:
    """Return weight based on feed tier."""
    raw_tier = str(entry.get("tier", "3"))
    try:
        tier = max(1, min(int(raw_tier), 3))
    except ValueError:
        tier = 3
    return 4 - tier


def _compute_hot_keywords(
    entries: list[dict[str, Any]],
    limit: int = 8,
) -> list[tuple[str, int]]:
    """Compute hot keyword scores from feed entries."""
    scores: dict[str, int] = {}
    for entry in entries:
        title_terms = _extract_hot_terms(str(entry.get("title", "")))
        snippet_terms = _extract_hot_terms(str(entry.get("snippet", "")))
        weight = _tier_weight(entry)

        for term in title_terms:
            scores[term] = scores.get(term, 0) + (3 * weight)
        for term in snippet_terms:
            scores[term] = scores.get(term, 0) + weight

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ranked[: max(1, min(limit, 12))]


def _compute_item_hotness(
    entry: dict[str, Any],
    hot_keywords: set[str],
) -> int:
    """Compute one entry hotness score."""
    title = str(entry.get("title", ""))
    snippet = str(entry.get("snippet", ""))
    terms = _extract_hot_terms(f"{title} {snippet}")
    overlap = len(terms & hot_keywords)
    tier_bonus = _tier_weight(entry) * 3

    recency_bonus = 0
    published_dt = _entry_datetime(entry)
    if published_dt is not None:
        age_hours = max(
            0.0,
            (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600.0,
        )
        recency_bonus = max(0, 24 - int(age_hours))

    return overlap * 20 + tier_bonus + recency_bonus


def _sort_daily_items(
    entries: list[dict[str, Any]],
    hot_keywords: list[tuple[str, int]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Sort daily digest items by hotness and recency."""
    keyword_set = {term for term, _ in hot_keywords}
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for entry in entries:
        hotness = _compute_item_hotness(entry, keyword_set)
        with_hotness = dict(entry)
        with_hotness["hotness"] = str(hotness)
        published_dt = _entry_datetime(entry)
        published_ts = published_dt.timestamp() if published_dt else 0.0
        scored.append((hotness, published_ts, with_hotness))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in scored[:top_k]]


async def _collect_daily_entries(
    sources: list[RssSource],
    recency_days: int,
    max_feeds: int,
    items_per_feed: int,
    timeout_seconds: int,
    concurrency: int,
    retries: int,
    web_cfg: Any | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect entries for daily digest without relevance filtering."""
    session = await ConnectionPool.get_session()
    selected_sources = sources[:max_feeds]
    fetch_kwargs = {
        "session": session,
        "sources": selected_sources,
        "timeout_seconds": timeout_seconds,
        "items_per_feed": items_per_feed,
        "concurrency": concurrency,
        "retries": retries,
        "layer": "primary",
    }
    if web_cfg is not None:
        fetch_kwargs["web_cfg"] = web_cfg
    flat_results, stats = await _collect_feed_results(**fetch_kwargs)

    status_lines = [
        "Phase daily: "
        f"checked={stats['checked']} ok={stats['ok_sources']} failed={stats['failed_sources']}"
    ]
    deduped = _dedupe_entries(flat_results)
    if not deduped:
        return [], status_lines

    filtered, recency_status = _apply_recency_filter(deduped, recency_days)
    if recency_status:
        status_lines.append(recency_status)

    return filtered, status_lines


def _format_daily_digest(
    topic: str,
    entries: list[dict[str, Any]],
    status_lines: list[str],
    source_scope: str,
    hot_keywords: list[tuple[str, int]],
    recency_days: int,
) -> str:
    """Format daily digest with keywords, links, and hotness."""
    topic_display = topic.strip() or "general"
    if not entries:
        status = " | ".join(status_lines) if status_lines else "no source status"
        return (
            f"Daily digest for `{topic_display}` has no items.\n"
            f"Window: last {recency_days} day(s)\n"
            f"{source_scope}\n"
            f"{status}"
        )

    channel_count: dict[str, int] = {}
    for entry in entries:
        channel = str(entry.get("channel", "unknown"))
        channel_count[channel] = channel_count.get(channel, 0) + 1
    channel_summary = ", ".join(
        f"{name}:{count}"
        for name, count in sorted(channel_count.items(), key=lambda item: item[1], reverse=True)[:6]
    )
    if not channel_summary:
        channel_summary = "n/a"

    lines = [f"Daily digest for `{topic_display}`", ""]
    lines.append(f"Window: last {recency_days} day(s)")
    lines.append(source_scope)
    if status_lines:
        lines.append("Fetch status: " + " | ".join(status_lines))
    lines.append(f"Channel coverage: {channel_summary}")
    lines.append("")
    lines.append("Hot keywords:")
    if hot_keywords:
        for term, score in hot_keywords:
            lines.append(f"- {term} (hotness={score})")
    else:
        lines.append("- n/a")

    lines.append("")
    lines.append("Hot news:")
    for idx, entry in enumerate(entries, start=1):
        title = str(entry.get("title", "Untitled"))
        url = str(entry.get("url", ""))
        channel = str(entry.get("channel", "unknown"))
        tier = str(entry.get("tier", "3"))
        hotness = str(entry.get("hotness", "0"))
        published_at = str(entry.get("published_at", "")).strip()
        snippet = str(entry.get("snippet", ""))[:180]

        lines.append(
            f"{idx}. {title} [{channel}] "
            f"(hotness={hotness}, tier={tier})"
        )
        if url:
            lines.append(f"   {url}")
        if published_at:
            lines.append(f"   Published: {published_at}")
        if snippet:
            lines.append(f"   {snippet}")

    return "\n".join(lines)


def _build_source_scope_summary(
    sources: list[RssSource],
    channel_filters: set[str] | None,
) -> str:
    """Return compact source-scope summary for user-visible diagnostics."""
    channel_counts: dict[str, int] = {}
    for source in sources:
        channel_counts[source.channel_id] = channel_counts.get(source.channel_id, 0) + 1
    sorted_channels = sorted(channel_counts.items(), key=lambda item: item[1], reverse=True)
    channel_text = ", ".join([f"{cid}:{count}" for cid, count in sorted_channels[:6]])
    if not channel_text:
        channel_text = "n/a"

    if channel_filters:
        requested = ", ".join(sorted(channel_filters))
    else:
        requested = "auto/all"

    return (
        f"Source scope: channels={requested}; available_feeds={len(sources)}; "
        f"feed_distribution={channel_text}"
    )


def _format_hotspot_brief(
    topic: str,
    entries: list[dict[str, Any]],
    status_lines: list[str],
    fallback_used: bool,
    query_plan: list[str],
    source_scope: str,
    strategy_notes: list[str],
) -> str:
    """Format a concise hotspot digest from ranked entries."""
    if not entries:
        status = " | ".join(status_lines) if status_lines else "no source status"
        queries = " | ".join(query_plan) if query_plan else "n/a"
        return (
            f"No hotspot items found. ({status})\n"
            f"{source_scope}\n"
            f"Query plan: {queries}"
        )

    topic_display = topic.strip() or "general"
    channel_count: dict[str, int] = {}
    primary_hits = 0
    fallback_hits = 0
    for entry in entries:
        channel = entry.get("channel", "unknown")
        channel_count[channel] = channel_count.get(channel, 0) + 1
        if entry.get("layer") == "fallback":
            fallback_hits += 1
        else:
            primary_hits += 1

    sorted_channels = sorted(
        channel_count.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    channel_summary = ", ".join([f"{name}:{count}" for name, count in sorted_channels[:4]])
    if not channel_summary:
        channel_summary = "n/a"

    lines = [f"Hotspot brief for `{topic_display}`", ""]
    if fallback_used:
        lines.append("Mode: mainland-first fallback -> global")
    else:
        lines.append("Mode: primary sources")
    if status_lines:
        lines.append("Fetch status: " + " | ".join(status_lines))
    lines.append(source_scope)
    if strategy_notes:
        lines.append("Keyword strategy: " + " ".join(strategy_notes))
    if query_plan:
        lines.append("Query plan: " + " | ".join(query_plan))
    lines.append("")
    lines.append("Key signals:")
    lines.append(f"- Channels: {channel_summary}")
    lines.append(f"- Evidence layers: primary={primary_hits}, fallback={fallback_hits}")
    lines.append(f"- Item count: {len(entries)}")
    lines.append("")
    lines.append("Top items:")

    for index, entry in enumerate(entries, start=1):
        snippet = entry.get("snippet", "")[:180]
        published_at = str(entry.get("published_at", "") or "").strip()
        if snippet:
            lines.append(
                f"{index}. {entry['title']} [{entry['channel']}] "
                f"(Layer: {entry['layer']}, Tier: {entry['tier']})"
            )
            lines.append(f"   {entry['url']}")
            if published_at:
                lines.append(f"   Published: {published_at}")
            lines.append(f"   {snippet}")
        else:
            lines.append(
                f"{index}. {entry['title']} [{entry['channel']}] "
                f"(Layer: {entry['layer']}, Tier: {entry['tier']})"
            )
            lines.append(f"   {entry['url']}")
            if published_at:
                lines.append(f"   Published: {published_at}")

    return "\n".join(lines)


@tool(
    name="web_search",
    description=(
        "Search current information. Supports RSS, Brave, and auto fallback mode. "
        "For RSS mode, prefer precise keywords (entity + topic, optionally English aliases). "
        "Returns top 5 results with title, URL, snippet, and source."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Search query. Keep it concise for better relevance.",
        }
    },
    catalog_summary="Search current information via RSS, Brave, or auto fallback.",
    catalog_entry_points=["latest news", "current information", "search the web"],
    risk_level="low",
)
async def web_search(query: str) -> str:
    """Search the web using configured provider strategy."""
    try:
        from nanoclaw.core.config import get_config
        from nanoclaw.tools.search_providers import run_search_provider

        config = get_config()
        web_cfg = config.tools.web_search
        configured_provider = web_cfg.provider.lower().strip() or "rss"
    except Exception:
        from nanoclaw.core.config import Config
        from nanoclaw.tools.search_providers import run_search_provider

        web_cfg = Config().tools.web_search
        configured_provider = web_cfg.provider.lower().strip() or "rss"

    plan = search_planner.build_search_plan(
        query,
        configured_provider=configured_provider,
    )
    provider = search_planner.select_search_provider(configured_provider, plan)
    result = await run_search_provider(provider, query, web_cfg, plan=plan)
    plan_text = search_planner.format_search_plan(plan, provider)
    normalized = search_normalizer.normalize_search_result(result, plan)
    rendered = search_normalizer.render_normalized_result(normalized)
    if not rendered:
        return plan_text
    return f"{plan_text}\n\n{rendered}"


# Default workflow tool entry points live in `web_workflows.py`.


@tool(
    name="web_fetch",
    description=(
        "Fetch and read web page content. Returns clean text extracted from HTML. "
        "Useful for articles, docs, and blog posts."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "Full URL to fetch (https://...)",
        }
    },
    catalog_summary="Fetch and read a web page with SSRF protections.",
    catalog_entry_points=["open this URL", "fetch page content", "read webpage"],
    risk_level="medium",
)
async def web_fetch(url: str) -> str:
    """Fetch URL and convert HTML to readable text."""
    web_cfg = _get_web_search_config()
    allowed, hostname, reason = await _check_outbound_url_policy(
        url,
        web_cfg,
        operation="web_fetch",
    )
    if not allowed:
        return reason

    arxiv_id = _extract_arxiv_id_from_url(url)
    if hostname.endswith("arxiv.org") and arxiv_id:
        query = quote_plus(f"id:{arxiv_id}")
        request_url = (
            f"{ARXIV_API_URL}?search_query={query}"
            "&start=0&max_results=1"
        )
        allowed, _, reason = await _check_outbound_url_policy(
            request_url,
            web_cfg,
            operation="web_fetch",
        )
        if not allowed:
            return reason
        try:
            session = await ConnectionPool.get_session()
            async with session.get(
                request_url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept-Encoding": "gzip, deflate",
                },
            ) as resp:
                if resp.status == 200:
                    xml_text = await resp.text()
                    entries = _parse_arxiv_atom(xml_text)
                    if entries:
                        entry = entries[0]
                        title = entry.get("title", "Untitled")
                        abstract = str(entry.get("summary", ""))[:800]
                        authors = ", ".join(entry.get("authors", [])[:8]) or "n/a"
                        categories = ", ".join(entry.get("categories", [])[:6]) or "n/a"
                        published = str(entry.get("published", "")).replace(
                            "T", " "
                        ).replace("Z", " UTC")
                        return (
                            f"arXiv paper: {title}\n"
                            f"URL: {entry.get('url', url)}\n"
                            f"Published: {published}\n"
                            f"Categories: {categories}\n"
                            f"Authors: {authors}\n"
                            f"Abstract: {abstract}"
                        )
        except Exception:
            # Fall back to generic HTML extraction.
            pass

    try:
        session = await ConnectionPool.get_session()
        max_redirects = 5
        current_url = url
        for _ in range(max_redirects):
            allowed, _, reason = await _check_outbound_url_policy(
                current_url,
                web_cfg,
                operation="web_fetch",
            )
            if not allowed:
                return reason
            async with session.get(
                current_url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept-Encoding": "gzip, deflate",
                },
                allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if not location:
                        return "Redirect with no Location header"
                    redirect_url = urljoin(current_url, location)
                    allowed, _, reason = await _check_outbound_url_policy(
                        redirect_url,
                        web_cfg,
                        operation="web_fetch",
                    )
                    if not allowed:
                        return reason
                    current_url = redirect_url
                    continue

                if resp.status != 200:
                    return f"Failed to fetch: HTTP {resp.status}"

                content_type = resp.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    return f"Not a text page. Content-Type: {content_type}"

                html = await resp.text()
                break
        else:
            return "Too many redirects"
    except aiohttp.ClientError as exc:
        return f"Network error fetching {url}: {exc}"
    except Exception as exc:
        return f"Error fetching {url}: {exc}"

    try:
        import html2text

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0
        text = converter.handle(html)
    except ImportError:
        text = _normalize_text(re.sub(r"<[^>]+>", " ", html))

    if len(text) > 4000:
        text = text[:4000] + "\n\n...[content truncated at 4000 chars]"
    return text
