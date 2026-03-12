"""Registry-backed search provider dispatch for the web_search tool."""

from __future__ import annotations

from urllib.parse import urljoin
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

from nanoclaw.core.llm import ConnectionPool
from nanoclaw.core.extension_runtime import (
    run_isolated_search_provider,
    should_isolate_extension_runtime,
)
from nanoclaw.core.logger import get_logger
from nanoclaw.core.plugins import get_plugin_registry, load_manifest_object
from nanoclaw.security.secrets import (
    describe_secret_requirement,
    has_tool_secret,
    resolve_tool_secret,
)
from nanoclaw.tools.search_planner import SearchQueryPlan

logger = get_logger(__name__)


class SearchProviderResult(BaseModel):
    """Unified result returned by one search provider."""

    text: str
    ok: bool
    provider: str
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)


SearchProviderHandler = Callable[
    [str, Any, Optional[SearchQueryPlan]],
    Awaitable[SearchProviderResult],
]


class SearchProviderSpec(BaseModel):
    """Runtime metadata for one manifest-backed search provider."""

    name: str
    handler_path: str = Field(alias="handlerPath")
    aliases: list[str] = Field(default_factory=list)
    auto_handler_path: str = Field(default="", alias="autoHandlerPath")
    auto_priority: int = Field(default=0, alias="autoPriority")
    secret_capability: str = Field(default="", alias="secretCapability")
    manifest_path: str = Field(default="", alias="manifestPath")
    source_scope: str = Field(default="", alias="sourceScope")

    model_config = {"populate_by_name": True}


class SearchProviderRegistry:
    """Map provider names to async handlers."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._handlers: dict[str, SearchProviderHandler] = {}
        self._auto_handlers: dict[str, SearchProviderHandler] = {}
        self._specs: dict[str, SearchProviderSpec] = {}

    def register(
        self,
        spec: SearchProviderSpec,
        handler: SearchProviderHandler,
        auto_handler: SearchProviderHandler | None = None,
    ) -> None:
        """Register one provider handler."""
        key = spec.name.strip().lower()
        if not key:
            raise ValueError("Search provider name cannot be empty.")
        self._specs[key] = _model_copy(spec, update={"name": key})
        self._handlers[key] = handler
        self._auto_handlers[key] = auto_handler or handler
        for alias in spec.aliases:
            alias_key = alias.strip().lower()
            if alias_key and alias_key not in self._handlers:
                self._handlers[alias_key] = handler

    async def run(
        self,
        provider: str,
        query: str,
        web_cfg: Any,
        plan: Optional[SearchQueryPlan] = None,
    ) -> SearchProviderResult:
        """Execute the named provider or return a stable unknown-provider error."""
        key = provider.strip().lower()
        handler = self._handlers.get(key)
        if handler is None:
            names = ", ".join(self.canonical_names())
            return SearchProviderResult(
                text=f"Unknown webSearch provider: `{provider}`. Use {names}.",
                ok=False,
                provider=key or provider,
            )
        return await handler(query, web_cfg, plan)

    def canonical_names(self) -> list[str]:
        """Return canonical provider names for display and validation."""
        return sorted(self._specs)

    def auto_candidates(self, web_cfg: Any) -> list[tuple[str, SearchProviderHandler]]:
        """Return auto-eligible providers in manifest-defined priority order."""
        items = sorted(
            self._specs.values(),
            key=lambda item: (item.auto_priority, item.name),
        )
        candidates: list[tuple[str, SearchProviderHandler]] = []
        for spec in items:
            if spec.auto_priority <= 0:
                continue
            if spec.secret_capability and not has_tool_secret(
                spec.secret_capability,
                tool_name="web_search",
                web_cfg=web_cfg,
            ):
                continue
            candidates.append((spec.name, self._auto_handlers[spec.name]))
        return candidates


async def _rss_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute RSS-backed search."""
    from nanoclaw.tools import web as web_tools

    if plan is not None:
        text, ok = await web_tools._search_with_rss_plan(plan)
    else:
        text, ok = await web_tools._search_with_rss(query)
    return SearchProviderResult(text=text, ok=ok, provider="rss")


async def _brave_only_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute Brave-backed search without supplementary sources."""
    from nanoclaw.tools import web as web_tools

    api_key = resolve_tool_secret(
        "web_search.brave_api_key",
        tool_name="web_search",
        web_cfg=web_cfg,
    )
    if not api_key:
        return SearchProviderResult(
            text=(
                "Brave search is not configured. "
                + describe_secret_requirement("web_search.brave_api_key")
            ),
            ok=False,
            provider="brave",
        )
    allowed, _, reason = await web_tools._check_outbound_url_policy(
        "https://api.search.brave.com/res/v1/web/search",
        web_cfg,
        operation="web_search",
    )
    if not allowed:
        return SearchProviderResult(text=reason, ok=False, provider="brave")
    variants = _plan_variants(query, plan)
    first_text = ""
    for variant in variants:
        text = await web_tools._search_with_brave(variant, api_key)
        if not first_text:
            first_text = text
        if _is_brave_text_ok(text):
            return SearchProviderResult(
                text=_with_variant_note(text, variant, variants[0]),
                ok=True,
                provider="brave",
            )
    return SearchProviderResult(text=first_text, ok=False, provider="brave")


async def _serper_only_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute Serper-backed search without supplementary sources."""
    from nanoclaw.tools import web as web_tools

    api_key = resolve_tool_secret(
        "web_search.serper_api_key",
        tool_name="web_search",
        web_cfg=web_cfg,
    )
    if not api_key:
        return SearchProviderResult(
            text=(
                "Serper search is not configured. "
                + describe_secret_requirement("web_search.serper_api_key")
            ),
            ok=False,
            provider="serper",
        )
    endpoint = (
        "https://google.serper.dev/news"
        if plan is not None and plan.category == "news"
        else "https://google.serper.dev/search"
    )
    allowed, _, reason = await web_tools._check_outbound_url_policy(
        endpoint,
        web_cfg,
        operation="web_search",
    )
    if not allowed:
        return SearchProviderResult(text=reason, ok=False, provider="serper")
    hl = str(getattr(web_cfg, "serper_hl", "en") or "en").strip() or "en"
    if plan is not None and plan.language_hint == "zh" and hl == "en":
        hl = "zh-cn"
    variants = _plan_variants(query, plan)
    first_text = ""
    for variant in variants:
        text = await web_tools._search_with_serper(
            variant,
            api_key,
            gl=str(getattr(web_cfg, "serper_gl", "world") or "world").strip() or "world",
            hl=hl,
            max_calls=max(0, int(getattr(web_cfg, "serper_max_calls", 0) or 0)),
            mode="news" if plan is not None and plan.category == "news" else "web",
            tbs=_serper_tbs_from_plan(plan),
        )
        if not first_text:
            first_text = text
        if _is_serper_text_ok(text):
            return SearchProviderResult(
                text=_with_variant_note(text, variant, variants[0]),
                ok=True,
                provider="serper",
            )
    return SearchProviderResult(text=first_text, ok=False, provider="serper")


async def _searxng_only_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute SearXNG-backed search without supplementary sources."""
    from nanoclaw.tools import web as web_tools

    provider_cfg = _provider_config_block(web_cfg, "searxng")
    request_url = _searxng_request_url(
        str(provider_cfg.get("baseUrl") or provider_cfg.get("base_url") or "").strip()
    )
    if not request_url:
        return SearchProviderResult(
            text=(
                "SearXNG search is not configured. Set "
                "`tools.webSearch.providerConfigs.searxng.baseUrl`."
            ),
            ok=False,
            provider="searxng",
        )
    allowed, _, reason = await web_tools._check_outbound_url_policy(
        request_url,
        web_cfg,
        operation="web_search",
    )
    if not allowed:
        return SearchProviderResult(text=reason, ok=False, provider="searxng")

    session = await ConnectionPool.get_session()
    variants = _plan_variants(query, plan)
    first_text = ""
    for variant in variants:
        params = _searxng_request_params(variant, provider_cfg, plan)
        try:
            async with session.get(
                request_url,
                params=params,
                headers={"User-Agent": "nanoClaw/1.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    text = "SearXNG search rate limited. Try again later."
                elif resp.status != 200:
                    text = f"SearXNG search failed: HTTP {resp.status}"
                else:
                    payload = await resp.json(content_type=None)
                    text = _format_searxng_response(payload)
        except aiohttp.ClientError as exc:
            text = f"SearXNG search failed: {exc}"
        except Exception as exc:
            text = f"SearXNG search error: {exc}"
        if not first_text:
            first_text = text
        if _is_searxng_text_ok(text):
            return SearchProviderResult(
                text=_with_variant_note(text, variant, variants[0]),
                ok=True,
                provider="searxng",
            )
    return SearchProviderResult(text=first_text, ok=False, provider="searxng")


async def _provider_with_rss_supplement(
    provider_name: str,
    query: str,
    web_cfg: Any,
    primary_handler: SearchProviderHandler,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Run provider-first search and append RSS evidence when available."""
    primary_result = await primary_handler(query, web_cfg, plan)
    if (
        "not configured" in primary_result.text.lower()
        or primary_result.text.startswith("BLOCKED:")
    ):
        return primary_result

    rss_result = await _rss_provider(query, web_cfg, plan)

    if primary_result.ok and rss_result.ok:
        return SearchProviderResult(
            text=(
                f"Primary web search ({provider_name}):\n\n{primary_result.text}\n\n"
                f"Supplementary RSS evidence:\n\n{rss_result.text}"
            ),
            ok=True,
            provider=f"{provider_name}+rss",
        )

    if primary_result.ok:
        return primary_result

    if rss_result.ok:
        return SearchProviderResult(
            text=(
                f"Primary web search ({provider_name}) had no reliable hits:\n\n"
                f"{primary_result.text}\n\n"
                f"Fallback RSS evidence:\n\n{rss_result.text}"
            ),
            ok=True,
            provider=f"{provider_name}+rss",
        )

    if primary_result.text:
        return primary_result
    return rss_result


def _plan_variants(query: str, plan: Optional[SearchQueryPlan]) -> list[str]:
    """Return ordered query variants from the planner."""
    if plan and plan.query_variants:
        return list(plan.query_variants)
    return [query]


def _with_variant_note(text: str, variant: str, primary_variant: str) -> str:
    """Annotate provider output when a fallback query variant wins."""
    if variant == primary_variant:
        return text
    return f"Query planner fallback variant: {variant}\n\n{text}"


def _is_brave_text_ok(text: str) -> bool:
    """Return True when Brave output contains usable evidence."""
    return bool(text.strip()) and not text.startswith(
        (
            "Brave search rate limited",
            "Brave search failed",
            "Brave search error",
            "No Brave results found.",
        )
    )


def _is_serper_text_ok(text: str) -> bool:
    """Return True when Serper output contains usable evidence."""
    return bool(text.strip()) and not text.startswith(
        (
            "Serper search rate limited",
            "Serper search failed",
            "Serper search error",
            "No Serper results found.",
        )
    )


def _serper_tbs_from_plan(plan: Optional[SearchQueryPlan]) -> str | None:
    """Map planner recency windows to Serper's coarse TBS filter."""
    if plan is None or plan.recency_days is None or plan.category == "news":
        return None
    if plan.recency_days <= 1:
        return "qdr:d"
    if plan.recency_days <= 7:
        return "qdr:w"
    if plan.recency_days <= 31:
        return "qdr:m"
    return None


def _auto_external_candidates(web_cfg: Any) -> list[tuple[str, SearchProviderHandler]]:
    """Return configured external providers for auto mode in priority order."""
    return get_search_provider_registry().auto_candidates(web_cfg)


async def _brave_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute Brave-backed search with RSS supplement when available."""
    return await _provider_with_rss_supplement(
        "brave",
        query,
        web_cfg,
        _brave_only_provider,
        plan,
    )


async def _serper_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute Serper-backed search with RSS supplement when available."""
    return await _provider_with_rss_supplement(
        "serper",
        query,
        web_cfg,
        _serper_only_provider,
        plan,
    )


async def _searxng_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute SearXNG-backed search with RSS supplement when available."""
    return await _provider_with_rss_supplement(
        "searxng",
        query,
        web_cfg,
        _searxng_only_provider,
        plan,
    )


async def _auto_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Use the best configured external provider first, then supplement with RSS."""
    candidates = _auto_external_candidates(web_cfg)
    rss_result = await _rss_provider(query, web_cfg, plan)
    if not candidates:
        return SearchProviderResult(
            text=rss_result.text,
            ok=rss_result.ok,
            provider="auto",
        )

    first_name, first_handler = candidates[0]
    first_result = await first_handler(query, web_cfg, plan)
    if first_result.ok:
        if rss_result.ok:
            return SearchProviderResult(
                text=(
                    f"Auto primary web search ({first_name}):\n\n{first_result.text}\n\n"
                    f"Supplementary RSS evidence:\n\n{rss_result.text}"
                ),
                ok=True,
                provider="auto",
            )
        return SearchProviderResult(text=first_result.text, ok=True, provider="auto")

    for name, handler in candidates[1:]:
        next_result = await handler(query, web_cfg, plan)
        if not next_result.ok:
            continue
        if rss_result.ok:
            return SearchProviderResult(
                text=(
                    f"Auto primary web search ({first_name}) had no reliable hits:\n\n"
                    f"{first_result.text}\n\n"
                    f"Auto fallback web search ({name}):\n\n{next_result.text}\n\n"
                    f"Supplementary RSS evidence:\n\n{rss_result.text}"
                ),
                ok=True,
                provider="auto",
            )
        return SearchProviderResult(
            text=(
                f"Auto primary web search ({first_name}) had no reliable hits:\n\n"
                f"{first_result.text}\n\n"
                f"Auto fallback web search ({name}):\n\n{next_result.text}"
            ),
            ok=True,
            provider="auto",
        )

    if rss_result.ok:
        return SearchProviderResult(
            text=(
                f"Auto external providers ({', '.join(name for name, _ in candidates)}) "
                "had no reliable hits.\n\n"
                f"{first_result.text}\n\n"
                f"Fallback RSS evidence:\n\n{rss_result.text}"
            ),
            ok=True,
            provider="auto",
        )

    return SearchProviderResult(
        text=first_result.text if first_result.text else rss_result.text,
        ok=False,
        provider="auto",
    )


async def _disabled_provider(
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Return a disabled-search response."""
    return SearchProviderResult(
        text="Web search is disabled in configuration.",
        ok=False,
        provider="disabled",
    )


def _provider_config_block(web_cfg: Any, provider_name: str) -> dict[str, Any]:
    """Return one provider-specific config block when available."""
    getter = getattr(web_cfg, "get_provider_config", None)
    if callable(getter):
        value = getter(provider_name)
        if isinstance(value, dict):
            return dict(value)
    provider_configs = getattr(web_cfg, "provider_configs", None)
    if isinstance(provider_configs, dict):
        value = provider_configs.get(provider_name) or {}
        if isinstance(value, dict):
            return dict(value)
    return {}


def _provider_string_list(value: object) -> list[str]:
    """Return one normalized list from string or list input."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _searxng_request_url(base_url: str) -> str:
    """Return the concrete SearXNG JSON endpoint URL."""
    normalized = str(base_url or "").strip()
    if not normalized:
        return ""
    if normalized.rstrip("/").endswith("/search"):
        return normalized.rstrip("/")
    return urljoin(normalized.rstrip("/") + "/", "search")


def _searxng_request_params(
    query: str,
    provider_cfg: dict[str, Any],
    plan: Optional[SearchQueryPlan],
) -> dict[str, str | int]:
    """Map config and planner hints into SearXNG request params."""
    params: dict[str, str | int] = {"q": query, "format": "json"}
    categories = _provider_string_list(provider_cfg.get("categories"))
    if not categories:
        categories = _searxng_categories_from_plan(plan)
    if categories:
        params["categories"] = ",".join(categories)
    engines = _provider_string_list(provider_cfg.get("engines"))
    if engines:
        params["engines"] = ",".join(engines)
    language = _searxng_language_from_plan(plan, provider_cfg)
    if language:
        params["language"] = language
    time_range = _searxng_time_range_from_plan(plan, provider_cfg)
    if time_range:
        params["time_range"] = time_range
    safe_search = _searxng_safe_search_value(provider_cfg.get("safeSearch"))
    if safe_search is not None:
        params["safesearch"] = safe_search
    return params


def _searxng_categories_from_plan(plan: Optional[SearchQueryPlan]) -> list[str]:
    """Map planner categories into SearXNG category hints."""
    if plan is None:
        return []
    if plan.category == "news":
        return ["news"]
    if plan.category == "paper":
        return ["science"]
    return []


def _searxng_language_from_plan(
    plan: Optional[SearchQueryPlan],
    provider_cfg: dict[str, Any],
) -> str:
    """Resolve SearXNG language from config override or planner hints."""
    configured = str(provider_cfg.get("language") or "").strip()
    if configured:
        return configured
    if plan is None:
        return ""
    if plan.language_hint == "zh":
        return "zh-CN"
    if plan.language_hint == "en":
        return "en-US"
    return ""


def _searxng_time_range_from_plan(
    plan: Optional[SearchQueryPlan],
    provider_cfg: dict[str, Any],
) -> str:
    """Resolve SearXNG time range from config override or recency hints."""
    configured = str(provider_cfg.get("timeRange") or "").strip().lower()
    if configured in {"day", "month", "year"}:
        return configured
    if plan is None or plan.recency_days is None:
        return ""
    if plan.recency_days <= 1:
        return "day"
    if plan.recency_days <= 31:
        return "month"
    return "year"


def _searxng_safe_search_value(value: object) -> int | None:
    """Normalize optional SearXNG safe-search config into an integer."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(normalized, 2))


def _format_searxng_response(payload: Any) -> str:
    """Render a compact text view from a SearXNG JSON response."""
    if not isinstance(payload, dict):
        return "SearXNG search failed: invalid JSON payload."
    results: list[str] = []
    for item in list(payload.get("results") or [])[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        snippet = str(item.get("content", "") or item.get("snippet", "") or "").strip()
        if not title or not url:
            continue
        lines = [f"**{title}**", url]
        if snippet:
            lines.append(snippet)
        published = str(
            item.get("publishedDate")
            or item.get("published_date")
            or item.get("published")
            or ""
        ).strip()
        if published:
            lines.append(f"Date: {published}")
        lines.append(f"Source: {_searxng_source_label(item)} | Provider: searxng")
        results.append("\n".join(lines))
    return "\n\n".join(results) if results else "No SearXNG results found."


def _searxng_source_label(item: dict[str, Any]) -> str:
    """Return a stable source label for one SearXNG result row."""
    engines = _provider_string_list(item.get("engines"))
    if not engines:
        engine = str(item.get("engine", "") or "").strip()
        engines = [engine] if engine else []
    if engines:
        return "SearXNG (" + ", ".join(engines[:3]) + ")"
    return "SearXNG"


def _is_searxng_text_ok(text: str) -> bool:
    """Return True when SearXNG output contains usable evidence."""
    return bool(text.strip()) and not text.startswith(
        (
            "SearXNG search is not configured",
            "SearXNG search failed",
            "SearXNG search error",
            "SearXNG search rate limited",
            "No SearXNG results found.",
        )
    )


def _build_default_registry() -> SearchProviderRegistry:
    """Create the default provider registry from manifest metadata."""
    registry = SearchProviderRegistry()
    for manifest in get_plugin_registry().get_enabled_search_provider_manifests():
        try:
            spec = _manifest_to_search_provider_spec(manifest)
            base_handler = load_manifest_object(
                spec.handler_path,
                manifest_name=manifest.name,
                manifest_path=spec.manifest_path,
                source_scope=spec.source_scope,
            )
            base_auto_handler = (
                load_manifest_object(
                    spec.auto_handler_path,
                    manifest_name=manifest.name,
                    manifest_path=spec.manifest_path,
                    source_scope=spec.source_scope,
                )
                if spec.auto_handler_path
                else base_handler
            )
            handler = _maybe_wrap_isolated_handler(spec, base_handler)
            auto_handler = _maybe_wrap_isolated_handler(spec, base_auto_handler)
            registry.register(
                spec,
                handler,
                auto_handler=auto_handler,
            )
        except Exception as exc:
            logger.error(
                "Failed to register search provider manifest %s: %s",
                manifest.manifest_path or manifest.name,
                exc,
            )
    return registry


_search_provider_registry: Optional[SearchProviderRegistry] = None


def get_search_provider_registry() -> SearchProviderRegistry:
    """Return the global search provider registry."""
    global _search_provider_registry
    if _search_provider_registry is None:
        _search_provider_registry = _build_default_registry()
    return _search_provider_registry


def reset_search_provider_registry() -> None:
    """Reset the global registry to built-in defaults."""
    global _search_provider_registry
    _search_provider_registry = _build_default_registry()


async def run_search_provider(
    provider: str,
    query: str,
    web_cfg: Any,
    plan: Optional[SearchQueryPlan] = None,
) -> SearchProviderResult:
    """Execute one provider via the global registry."""
    return await get_search_provider_registry().run(provider, query, web_cfg, plan)


def _manifest_to_search_provider_spec(manifest: Any) -> SearchProviderSpec:
    """Convert one extension manifest into a runtime search-provider spec."""
    metadata = _as_dict(getattr(manifest, "metadata", {}))
    runtime = _as_dict(metadata.get("runtime"))
    handler_path = str(runtime.get("handlerPath") or "").strip()
    if not handler_path:
        raise ValueError("search_provider manifest is missing runtime.handlerPath")
    return SearchProviderSpec(
        name=manifest.primary_name,
        handlerPath=handler_path,
        aliases=_string_list(runtime.get("aliases")),
        autoHandlerPath=str(runtime.get("autoHandlerPath") or "").strip(),
        autoPriority=int(runtime.get("autoPriority") or 0),
        secretCapability=str(runtime.get("secretCapability") or "").strip(),
        manifestPath=str(getattr(manifest, "manifest_path", "") or ""),
        sourceScope=str(getattr(manifest, "source_scope", "") or ""),
    )


def _maybe_wrap_isolated_handler(
    spec: SearchProviderSpec,
    handler: SearchProviderHandler,
) -> SearchProviderHandler:
    """Wrap user-installed providers in the isolated subprocess runtime when enabled."""
    if not should_isolate_extension_runtime(
        kind="search_provider",
        source_scope=spec.source_scope,
    ):
        return handler

    async def _isolated_handler(
        query: str,
        web_cfg: Any,
        plan: Optional[SearchQueryPlan] = None,
    ) -> SearchProviderResult:
        try:
            result = await run_isolated_search_provider(
                handler_path=spec.handler_path,
                manifest_name=spec.name,
                manifest_path=spec.manifest_path,
                source_scope=spec.source_scope,
                query=query,
                web_config=web_cfg,
                plan=plan,
            )
        except Exception as exc:
            logger.error(
                "Isolated search provider %s failed: %s",
                spec.name,
                exc,
            )
            return SearchProviderResult(
                text=f"Extension provider `{spec.name}` failed: {exc}",
                ok=False,
                provider=spec.name,
            )
        return SearchProviderResult(**result)

    return _isolated_handler


def _as_dict(value: object) -> dict[str, object]:
    """Return a plain dictionary from manifest metadata."""
    if isinstance(value, dict):
        return {
            str(key): item
            for key, item in value.items()
            if isinstance(key, str)
        }
    return {}


def _string_list(value: object) -> list[str]:
    """Return one normalized string list from manifest metadata."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _model_copy(item: SearchProviderSpec, update: dict[str, object]) -> SearchProviderSpec:
    """Copy a pydantic model across v1 and v2."""
    if hasattr(item, "model_copy"):
        return item.model_copy(update=update, deep=True)
    return item.copy(update=update, deep=True)
