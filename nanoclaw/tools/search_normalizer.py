"""Normalize and rerank heterogeneous search results into one evidence schema."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from nanoclaw.tools.search_planner import SearchQueryPlan
from nanoclaw.tools.search_providers import SearchProviderResult

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref"}
LOW_QUALITY_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "pinterest.com",
    "www.pinterest.com",
    "m.pinterest.com",
}
PROVIDER_WEIGHTS = {
    "serper": 1.0,
    "serper+rss": 0.98,
    "brave": 0.88,
    "brave+rss": 0.86,
    "rss": 0.74,
    "searxng": 0.7,
    "web_model": 0.55,
    "auto": 0.82,
}
TITLE_PATTERN = re.compile(r"^\*\*(.+?)\*\*$")


class SearchEvidence(BaseModel):
    """One normalized search evidence item."""

    title: str
    url: str = ""
    normalized_url: str = ""
    snippet: str = ""
    source: str = ""
    provider: str = ""
    published_at: str = ""
    language: str = "unknown"
    content_type: str = "web"
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedSearchResult(BaseModel):
    """Normalized and reranked provider result bundle."""

    provider: str
    ok: bool
    evidences: list[SearchEvidence] = Field(default_factory=list)
    raw_text: str = ""
    notes: list[str] = Field(default_factory=list)


def _contains_cjk(text: str) -> bool:
    """Return True when text contains CJK characters."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _normalize_text(value: str) -> str:
    """Collapse whitespace and trim text."""
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    """Normalize URL for dedupe and stable scoring."""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not any(key.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    normalized_netloc = parts.netloc.lower()
    if normalized_netloc.endswith(":80") and parts.scheme == "http":
        normalized_netloc = normalized_netloc[:-3]
    if normalized_netloc.endswith(":443") and parts.scheme == "https":
        normalized_netloc = normalized_netloc[:-4]
    normalized_path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower(),
            normalized_netloc,
            normalized_path,
            urlencode(filtered_query, doseq=True),
            "",
        )
    )


def _parse_datetime_value(value: str) -> Optional[datetime]:
    """Parse common published-at formats into UTC datetimes."""
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
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_query_terms(plan: SearchQueryPlan) -> list[str]:
    """Extract compact lexical terms from planner variants."""
    merged = " ".join(plan.query_variants or [plan.query])
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9][a-z0-9-]{1,24}", merged.lower()):
        if len(token) <= 2 or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    for token in re.findall(r"[\u4e00-\u9fff]{2,8}", merged):
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms[:12]


def _lexical_score(plan: SearchQueryPlan, title: str, snippet: str) -> float:
    """Compute a light lexical overlap score."""
    haystack_title = title.lower()
    haystack_snippet = snippet.lower()
    score = 0.0
    for term in _extract_query_terms(plan):
        term_lower = term.lower()
        if term_lower in haystack_title:
            score += 2.0
        if term_lower in haystack_snippet:
            score += 0.8
    return score


def _freshness_score(published_at: str) -> float:
    """Return a freshness score in [0, 10]."""
    published_dt = _parse_datetime_value(published_at)
    if published_dt is None:
        return 0.0
    age_hours = max(
        0.0,
        (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600.0,
    )
    if age_hours <= 24:
        return 10.0
    if age_hours <= 24 * 7:
        return 7.0
    if age_hours <= 24 * 30:
        return 4.0
    return 1.0


def _quality_penalty(url: str) -> float:
    """Apply a small penalty to obviously low-signal hosts."""
    normalized = normalize_url(url)
    if not normalized:
        return 0.0
    host = urlsplit(normalized).netloc.lower()
    if host in LOW_QUALITY_HOSTS:
        return 5.0
    if host.startswith("news.google.com"):
        return 2.0
    return 0.0


def _infer_content_type(plan: SearchQueryPlan, url: str) -> str:
    """Infer content type from planner context and URL."""
    if plan.intent == "paper":
        return "paper"
    if plan.category == "news":
        return "news"
    lowered = url.lower()
    if lowered.endswith(".pdf"):
        return "document"
    return "web"


def _build_evidence(
    item: dict[str, Any],
    plan: SearchQueryPlan,
    provider: str,
) -> SearchEvidence:
    """Convert one raw item into normalized evidence."""
    title = _normalize_text(str(item.get("title", "") or "Untitled"))
    url = str(item.get("url", "") or "").strip()
    snippet = _normalize_text(str(item.get("snippet", "") or ""))
    source = _normalize_text(str(item.get("source", "") or provider))
    published_at = _normalize_text(str(item.get("published_at", "") or item.get("date", "") or ""))
    language = "zh" if _contains_cjk(f"{title} {snippet}") else "en"
    provider_weight = PROVIDER_WEIGHTS.get(provider, 0.75) * 20.0
    score = provider_weight
    score += _lexical_score(plan, title, snippet)
    score += _freshness_score(published_at)
    score -= _quality_penalty(url)
    return SearchEvidence(
        title=title,
        url=url,
        normalized_url=normalize_url(url),
        snippet=snippet,
        source=source,
        provider=provider,
        published_at=published_at,
        language=language,
        content_type=_infer_content_type(plan, url),
        score=score,
        metadata=item.get("metadata", {}) or {},
    )


def _parse_source_line(line: str) -> dict[str, str]:
    """Parse the unified `Source: ... | Provider: ...` line."""
    payload: dict[str, str] = {}
    for chunk in [part.strip() for part in line.split("|") if part.strip()]:
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        payload[key.strip().lower()] = _normalize_text(value)
    return payload


def _parse_text_items(text: str, provider: str) -> list[dict[str, Any]]:
    """Parse current search tool text format into raw evidence items."""
    items: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        title_match = TITLE_PATTERN.match(line)
        if title_match:
            if current and current.get("title"):
                items.append(current)
            current = {
                "title": _normalize_text(title_match.group(1)),
                "url": "",
                "snippet_lines": [],
                "source": provider,
                "provider": provider,
                "published_at": "",
            }
            continue
        if current is None:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            if not current["url"]:
                current["url"] = line
            continue
        if line.startswith("Date:") or line.startswith("Published:"):
            current["published_at"] = _normalize_text(line.split(":", 1)[1])
            continue
        if line.startswith("Source:"):
            parsed = _parse_source_line(line)
            if parsed.get("source"):
                current["source"] = parsed["source"]
            if parsed.get("provider"):
                current["provider"] = parsed["provider"]
            continue
        if line.startswith("Query planner fallback variant:"):
            continue
        current["snippet_lines"].append(line)

    if current and current.get("title"):
        items.append(current)

    normalized: list[dict[str, Any]] = []
    for item in items:
        normalized.append(
            {
                "title": item["title"],
                "url": item["url"],
                "snippet": _normalize_text(" ".join(item["snippet_lines"])),
                "source": item["source"],
                "provider": item["provider"],
                "published_at": item["published_at"],
            }
        )
    return normalized


def _dedupe_and_rank(
    evidences: list[SearchEvidence],
) -> list[SearchEvidence]:
    """Deduplicate by normalized URL and keep the highest-scoring copy."""
    best_by_key: dict[str, SearchEvidence] = {}
    ordered_keys: list[str] = []
    for evidence in evidences:
        key = evidence.normalized_url or evidence.title.lower()
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = evidence
            ordered_keys.append(key)
            continue
        if evidence.score > existing.score:
            best_by_key[key] = evidence

    ranked = [best_by_key[key] for key in ordered_keys]
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:6]


def normalize_search_result(
    result: SearchProviderResult,
    plan: SearchQueryPlan,
) -> NormalizedSearchResult:
    """Normalize one provider result into reranked evidence items."""
    raw_items = list(result.evidence_items)
    if not raw_items:
        raw_items = _parse_text_items(result.text, result.provider)
    evidences = [_build_evidence(item, plan, result.provider) for item in raw_items]
    deduped = _dedupe_and_rank(evidences)
    notes = [
        f"unique_items={len(deduped)}",
        "schema=v1",
    ]
    return NormalizedSearchResult(
        provider=result.provider,
        ok=result.ok,
        evidences=deduped,
        raw_text=result.text,
        notes=notes,
    )


def render_normalized_result(bundle: NormalizedSearchResult) -> str:
    """Render the normalized evidence list back into compact text."""
    if not bundle.evidences:
        return bundle.raw_text
    lines = [
        "Normalized evidence set: "
        f"provider={bundle.provider}; unique_items={len(bundle.evidences)}"
    ]
    for evidence in bundle.evidences:
        lines.append("")
        lines.append(f"**{evidence.title}**")
        if evidence.url:
            lines.append(evidence.url)
        if evidence.snippet:
            lines.append(evidence.snippet)
        if evidence.published_at:
            lines.append(f"Published: {evidence.published_at}")
        lines.append(
            "Source: "
            f"{evidence.source} | Provider: {evidence.provider} | Score: {evidence.score:.1f}"
        )
    return "\n".join(lines)
