"""Search provider registry tests."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from nanoclaw.core.config import WebSearchConfig
from nanoclaw.core.extension_installer import install_extension_manifest
from nanoclaw.core.plugins import reset_plugin_registry
from nanoclaw.tools import web
from nanoclaw.tools.search_planner import SearchQueryPlan
from nanoclaw.tools.search_providers import (
    SearchProviderResult,
    get_search_provider_registry,
    reset_search_provider_registry,
    run_search_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider_registries() -> None:
    """Keep plugin and provider registries isolated between tests."""
    reset_plugin_registry()
    reset_search_provider_registry()
    yield
    reset_plugin_registry()
    reset_search_provider_registry()


def test_search_provider_registry_has_builtin_names() -> None:
    """Built-in provider registry should expose stable canonical names."""
    reset_search_provider_registry()
    registry = get_search_provider_registry()
    assert registry.canonical_names() == ["auto", "brave", "disabled", "rss", "searxng", "serper"]


@pytest.mark.asyncio
async def test_auto_provider_falls_back_to_brave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto provider should use Brave when it is the only configured external source."""

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "No RSS results found for this query.", False

    async def fake_brave(query: str, api_key: str) -> str:
        return "**Hit**\nhttps://example.com\nSource: Brave | Provider: brave"

    monkeypatch.setattr(web, "_search_with_rss", fake_rss)
    monkeypatch.setattr(web, "_search_with_brave", fake_brave)

    result = await run_search_provider(
        "auto",
        "latest ai",
        SimpleNamespace(api_key="brave-key"),
    )
    assert result.ok is True
    assert result.provider == "auto"
    assert "https://example.com" in result.text
    assert "Provider: brave" in result.text


@pytest.mark.asyncio
async def test_auto_provider_prefers_serper_before_brave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto provider should prefer Serper when both Serper and Brave are configured."""

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "**RSS Hit**\nhttps://rss.example.com\nSource: RSS | Provider: rss", True

    async def fake_serper(
        query: str,
        api_key: str,
        *,
        gl: str,
        hl: str,
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        assert api_key == "serper-key"
        return "**Serper Hit**\nhttps://serper.example.com\nSource: Serper | Provider: serper"

    async def fake_brave(query: str, api_key: str) -> str:
        raise AssertionError("Brave should not be called when Serper already succeeds.")

    monkeypatch.setattr(web, "_search_with_rss", fake_rss)
    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_brave", fake_brave)

    result = await run_search_provider(
        "auto",
        "latest ai",
        SimpleNamespace(
            api_key="brave-key",
            serper_api_key="serper-key",
            serper_gl="world",
            serper_hl="en",
        ),
    )
    assert result.ok is True
    assert result.provider == "auto"
    assert "Auto primary web search (serper)" in result.text
    assert "Supplementary RSS evidence" in result.text
    assert "https://serper.example.com" in result.text


@pytest.mark.asyncio
async def test_auto_provider_uses_brave_when_serper_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto provider should fall back to Brave after a Serper miss."""

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "**RSS Hit**\nhttps://rss.example.com\nSource: RSS | Provider: rss", True

    async def fake_serper(
        query: str,
        api_key: str,
        *,
        gl: str,
        hl: str,
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        return "No Serper results found."

    async def fake_brave(query: str, api_key: str) -> str:
        assert api_key == "brave-key"
        return "**Brave Hit**\nhttps://brave.example.com\nSource: Brave | Provider: brave"

    monkeypatch.setattr(web, "_search_with_rss", fake_rss)
    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_brave", fake_brave)

    result = await run_search_provider(
        "auto",
        "latest ai",
        SimpleNamespace(
            api_key="brave-key",
            serper_api_key="serper-key",
            serper_gl="world",
            serper_hl="en",
        ),
    )
    assert result.ok is True
    assert result.provider == "auto"
    assert "Auto primary web search (serper) had no reliable hits:" in result.text
    assert "Auto fallback web search (brave)" in result.text
    assert "Supplementary RSS evidence" in result.text
    assert "https://brave.example.com" in result.text


@pytest.mark.asyncio
async def test_brave_provider_requires_api_key() -> None:
    """Brave provider should fail fast when no API key is configured."""
    result = await run_search_provider(
        "brave",
        "latest ai",
        SimpleNamespace(api_key=""),
    )
    assert result.ok is False
    assert "Brave search is not configured" in result.text


@pytest.mark.asyncio
async def test_searxng_provider_requires_base_url() -> None:
    """SearXNG provider should fail fast when no instance URL is configured."""
    result = await run_search_provider(
        "searxng",
        "latest ai",
        WebSearchConfig(providerConfigs={"searxng": {}}),
    )
    assert result.ok is False
    assert "providerConfigs.searxng.baseUrl" in result.text


@pytest.mark.asyncio
async def test_searxng_provider_maps_plan_hints_to_request_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SearXNG provider should map planner hints into request params."""
    captured: dict[str, object] = {}

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status = 200
            self._payload = payload

        async def __aenter__(self) -> "_FakeResponse":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def json(self, content_type=None) -> dict[str, object]:
            return self._payload

    class _FakeSession:
        def get(self, url: str, **kwargs):
            captured["url"] = url
            captured["params"] = dict(kwargs.get("params") or {})
            return _FakeResponse(
                {
                    "results": [
                        {
                            "title": "Demo result",
                            "url": "https://example.com/demo",
                            "content": "demo snippet",
                            "engines": ["google", "news"],
                            "publishedDate": "2026-03-10",
                        }
                    ]
                }
            )

    async def _fake_get_session() -> _FakeSession:
        return _FakeSession()

    async def _fake_allow(url: str, web_cfg=None, *, operation: str = "") -> tuple[bool, str, str]:
        return True, "search.example.com", ""

    async def _fake_rss(
        query: str,
        web_cfg,
        plan: SearchQueryPlan | None = None,
    ) -> SearchProviderResult:
        return SearchProviderResult(text="No RSS results found.", ok=False, provider="rss")

    monkeypatch.setattr("nanoclaw.tools.search_providers.ConnectionPool.get_session", _fake_get_session)
    monkeypatch.setattr("nanoclaw.tools.web._check_outbound_url_policy", _fake_allow)
    monkeypatch.setattr("nanoclaw.tools.search_providers._rss_provider", _fake_rss)

    plan = SearchQueryPlan(
        query="最新 AI 新闻",
        intent="news",
        category="news",
        provider_hint="searxng",
        time_range="recent",
        recency_days=7,
        language_hint="zh",
        query_variants=["最新 AI 新闻"],
    )
    result = await run_search_provider(
        "searxng",
        "最新 AI 新闻",
        WebSearchConfig(
            providerConfigs={
                "searxng": {
                    "baseUrl": "https://search.example.com",
                    "engines": ["google", "news"],
                    "safeSearch": 0,
                }
            }
        ),
        plan,
    )

    assert result.ok is True
    assert result.provider == "searxng"
    assert "Demo result" in result.text
    assert "Provider: searxng" in result.text
    assert captured["url"] == "https://search.example.com/search"
    assert captured["params"] == {
        "q": "最新 AI 新闻",
        "format": "json",
        "categories": "news",
        "engines": "google,news",
        "language": "zh-CN",
        "time_range": "month",
        "safesearch": 0,
    }


@pytest.mark.asyncio
async def test_user_search_provider_runs_in_subprocess_isolation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-installed search providers should run in a separate subprocess."""
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    source_dir = tmp_path / "source"
    for directory in (
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
        source_dir,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    region = web_cfg.get_provider_config('demo').get('region', 'global')",
                "    return SearchProviderResult(",
                "        text=f'pid={os.getpid()} region={region} query={query}',",
                "        ok=True,",
                "        provider='demo',",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
        json.dumps(
            {
                "name": "demo_provider",
                "kind": "search_provider",
                "module": "demo_provider",
                "provides": ["demo"],
                "summary": "Demo provider",
                "metadata": {
                    "runtime": {"handlerPath": "demo_provider:demo_provider"},
                    "security": {
                        "permissions": ["outbound_http"],
                        "sandboxPolicy": "inherits_core_boundary",
                    },
                },
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    install_extension_manifest(
        source_dir / "demo_provider.plugin.json",
        destination_dir=user_extensions,
    )

    class _Policy:
        require_install_receipt = True
        require_signed_bundles = False
        max_risk_level = "medium"
        isolated_timeout_seconds = 10

        @staticmethod
        def isolates_kind(kind: str) -> bool:
            return kind == "search_provider"

    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(extensions=_Policy()),
    )
    reset_plugin_registry()
    reset_search_provider_registry()

    result = await run_search_provider(
        "demo",
        "latest ai",
        WebSearchConfig(providerConfigs={"demo": {"region": "apac"}}),
    )

    assert result.ok is True
    assert "region=apac" in result.text
    assert f"pid={os.getpid()}" not in result.text


@pytest.mark.asyncio
async def test_brave_provider_respects_blocked_host_policy() -> None:
    """Brave provider should block denied outbound hosts before sending a request."""
    result = await run_search_provider(
        "brave",
        "latest ai",
        WebSearchConfig(apiKey="brave-key", blockedHosts=["brave.com"]),
    )
    assert result.ok is False
    assert "blockedHosts" in result.text
    assert "api.search.brave.com" in result.text


@pytest.mark.asyncio
async def test_unknown_provider_returns_registry_help() -> None:
    """Unknown providers should return a stable error listing canonical names."""
    result = await run_search_provider(
        "unknown",
        "latest ai",
        SimpleNamespace(api_key=""),
    )
    assert result.ok is False
    assert "Unknown webSearch provider" in result.text
    assert "auto, brave, disabled, rss, searxng, serper" in result.text


@pytest.mark.asyncio
async def test_user_installed_provider_loads_from_adjacent_extension_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User providers should load from a safe local module next to the manifest."""
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    (user_extensions / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    config = web_cfg.get_provider_config('demo')",
                "    region = config.get('region', 'global')",
                "    return SearchProviderResult(",
                "        text=f'Demo provider [{region}] {query}',",
                "        ok=True,",
                "        provider='demo',",
                "    )",
            ]
        ),
        encoding="utf-8",
    )
    (user_extensions / "demo_provider.py").chmod(0o600)
    (user_extensions / "demo_provider.plugin.json").write_text(
        (
            "{"
            '"name":"demo_provider",'
            '"kind":"search_provider",'
            '"module":"demo_provider",'
            '"provides":["demo"],'
            '"summary":"Demo custom provider",'
            '"metadata":{'
            '"runtime":{"handlerPath":"demo_provider:demo_provider"},'
            '"security":{"permissions":["outbound_http"],'
            '"sandboxPolicy":"inherits_core_boundary"}'
            "},"
            '"enabled":true'
            "}"
        ),
        encoding="utf-8",
    )
    (user_extensions / "demo_provider.plugin.json").chmod(0o600)

    monkeypatch.setattr("nanoclaw.core.plugins.get_builtin_plugin_dir", lambda: builtin_skills)
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr("nanoclaw.core.plugins.get_user_plugin_dir", lambda: user_skills)
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    install_extension_manifest(
        user_extensions / "demo_provider.plugin.json",
        destination_dir=user_extensions,
        overwrite=True,
    )
    reset_plugin_registry()
    reset_search_provider_registry()

    result = await run_search_provider(
        "demo",
        "latest ai",
        WebSearchConfig(providerConfigs={"demo": {"region": "apac"}}),
    )

    assert result.ok is True
    assert result.provider == "demo"
    assert "Demo provider [apac] latest ai" in result.text
    reset_plugin_registry()
    reset_search_provider_registry()


@pytest.mark.asyncio
async def test_disabled_alias_is_registered_via_manifest_metadata() -> None:
    """Disabled provider aliases should resolve through manifest-backed registration."""
    result = await run_search_provider(
        "off",
        "latest ai",
        SimpleNamespace(),
    )
    assert result.ok is False
    assert result.provider == "disabled"
    assert "Web search is disabled" in result.text


@pytest.mark.asyncio
async def test_serper_provider_requires_api_key() -> None:
    """Serper provider should fail fast when no API key is configured."""
    result = await run_search_provider(
        "serper",
        "latest ai",
        SimpleNamespace(serper_api_key=""),
    )
    assert result.ok is False
    assert "Serper search is not configured" in result.text


@pytest.mark.asyncio
async def test_serper_provider_respects_allowed_host_policy() -> None:
    """Serper provider should block outbound hosts outside the configured allowlist."""
    result = await run_search_provider(
        "serper",
        "latest ai",
        WebSearchConfig(serperApiKey="serper-key", allowedHosts=["example.com"]),
    )
    assert result.ok is False
    assert "allowedHosts" in result.text
    assert "google.serper.dev" in result.text


@pytest.mark.asyncio
async def test_serper_provider_uses_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serper provider should keep provider-first ordering and append RSS evidence."""

    async def fake_serper(
        query: str,
        api_key: str,
        *,
        gl: str,
        hl: str,
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        assert query == "latest ai"
        assert api_key == "serper-key"
        assert gl == "us"
        assert hl == "en"
        return "**Hit**\nhttps://example.com\nSource: Serper | Provider: serper"

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "**RSS Hit**\nhttps://rss.example.com\nSource: RSS | Provider: rss", True

    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_rss", fake_rss)

    result = await run_search_provider(
        "serper",
        "latest ai",
        SimpleNamespace(serper_api_key="serper-key", serper_gl="us", serper_hl="en"),
    )
    assert result.ok is True
    assert result.provider == "serper+rss"
    assert "Primary web search (serper)" in result.text
    assert "Supplementary RSS evidence" in result.text
    assert "https://example.com" in result.text
    assert "https://rss.example.com" in result.text


@pytest.mark.asyncio
async def test_serper_provider_uses_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serper provider should accept env-based key override."""

    async def fake_serper(
        query: str,
        api_key: str,
        *,
        gl: str,
        hl: str,
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        assert api_key == "env-serper-key"
        return "**Hit**\nhttps://example.com\nSource: Serper | Provider: serper"

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "No RSS results found for this query.", False

    monkeypatch.setenv("SERPER_API_KEY", "env-serper-key")
    monkeypatch.setattr(
        "nanoclaw.security.secrets._get_secret_isolation_config",
        lambda: SimpleNamespace(allow_environment_fallback=True, audit_access=True),
    )
    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_rss", fake_rss)
    web_cfg = WebSearchConfig(serperApiKey="", serperGl="world", serperHl="en")
    result = await run_search_provider("serper", "latest ai", web_cfg)
    assert result.ok is True
    assert result.provider == "serper"


@pytest.mark.asyncio
async def test_serper_provider_falls_back_to_rss_when_primary_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serper provider should still return RSS evidence when Serper misses."""

    async def fake_serper(
        query: str,
        api_key: str,
        *,
        gl: str,
        hl: str,
        max_calls: int = 0,
        mode: str = "web",
        tbs: str | None = None,
    ) -> str:
        return "No Serper results found."

    async def fake_rss(query: str) -> tuple[str, bool]:
        return "**RSS Hit**\nhttps://rss.example.com\nSource: RSS | Provider: rss", True

    monkeypatch.setattr(web, "_search_with_serper", fake_serper)
    monkeypatch.setattr(web, "_search_with_rss", fake_rss)

    result = await run_search_provider(
        "serper",
        "latest ai",
        SimpleNamespace(serper_api_key="serper-key", serper_gl="world", serper_hl="en"),
    )
    assert result.ok is True
    assert result.provider == "serper+rss"
    assert "Fallback RSS evidence" in result.text
    assert "https://rss.example.com" in result.text


@pytest.mark.asyncio
async def test_web_search_uses_provider_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """web_search should delegate provider execution to the registry layer."""

    class FakeConfig:
        def __init__(self) -> None:
            self.tools = SimpleNamespace(
                web_search=SimpleNamespace(provider="rss", api_key="")
            )

    async def fake_run(
        provider: str,
        query: str,
        web_cfg: object,
        plan=None,
    ) -> SearchProviderResult:
        assert provider == "rss"
        assert query == "latest ai"
        assert getattr(web_cfg, "provider") == "rss"
        assert plan is not None
        assert plan.intent == "news"
        return SearchProviderResult(text="provider result", ok=True, provider="rss")

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("nanoclaw.tools.search_providers.run_search_provider", fake_run)

    result = await web.web_search("latest ai")
    assert "Search planner: type=news; provider=rss;" in result
    assert result.endswith("provider result")
