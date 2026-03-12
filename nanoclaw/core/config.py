"""Configuration management with pydantic validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class OpenRouterConfig(BaseModel):
    """OpenRouter API configuration."""

    api_key: str = Field(alias="apiKey")
    default_model: str = Field(
        default="anthropic/claude-sonnet-4", alias="defaultModel"
    )


class AnthropicConfig(BaseModel):
    """Anthropic API configuration."""

    api_key: str = Field(alias="apiKey")
    default_model: str = Field(
        default="claude-sonnet-4-20250514", alias="defaultModel"
    )


class OpenAIConfig(BaseModel):
    """OpenAI API configuration."""

    api_key: str = Field(alias="apiKey")
    default_model: str = Field(default="gpt-4o", alias="defaultModel")
    base_url: Optional[str] = Field(default=None, alias="baseUrl")


class DeepSeekConfig(BaseModel):
    """DeepSeek API configuration."""

    api_key: str = Field(alias="apiKey")
    default_model: str = Field(default="deepseek-chat", alias="defaultModel")


class ProvidersConfig(BaseModel):
    """LLM providers configuration."""

    openrouter: Optional[OpenRouterConfig] = None
    anthropic: Optional[AnthropicConfig] = None
    openai: Optional[OpenAIConfig] = None
    deepseek: Optional[DeepSeekConfig] = None

    model_config = {"populate_by_name": True}


class TelegramConfig(BaseModel):
    """Telegram bot configuration."""

    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list, alias="allowFrom")

    model_config = {"populate_by_name": True}


class FeishuConfig(BaseModel):
    """Feishu app channel configuration."""

    enabled: bool = False
    app_id: str = Field(default="", alias="appId")
    app_secret: str = Field(default="", alias="appSecret")
    verify_token: str = Field(default="", alias="verifyToken")
    encrypt_key: str = Field(default="", alias="encryptKey")
    webhook_host: str = Field(default="0.0.0.0", alias="webhookHost")
    webhook_port: int = Field(default=15097, alias="webhookPort")
    webhook_path: str = Field(default="/feishu/events", alias="webhookPath")
    allow_from: list[str] = Field(default_factory=list, alias="allowFrom")
    default_chat_id: str = Field(default="", alias="defaultChatId")

    model_config = {"populate_by_name": True}


class ChannelsConfig(BaseModel):
    """Communication channels configuration."""

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    extensions: dict[str, dict[str, object]] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    api_key: str = Field(default="", alias="apiKey")
    serper_api_key: str = Field(default="", alias="serperApiKey")
    serper_max_calls: int = Field(default=0, alias="serperMaxCalls")
    serper_gl: str = Field(default="world", alias="serperGl")
    serper_hl: str = Field(default="en", alias="serperHl")
    provider: str = "rss"
    rss_sources_path: str = Field(default="assets/rss-sources.json", alias="rssSourcesPath")
    prefer_mainland: bool = Field(default=True, alias="preferMainland")
    mainland_only: bool = Field(default=False, alias="mainlandOnly")
    rss_max_feeds: int = Field(default=12, alias="rssMaxFeeds")
    rss_items_per_feed: int = Field(default=20, alias="rssItemsPerFeed")
    rss_timeout: int = Field(default=10, alias="rssTimeout")
    rss_concurrency: int = Field(default=8, alias="rssConcurrency")
    rss_retries: int = Field(default=1, alias="rssRetries")
    allowed_hosts: list[str] = Field(default_factory=list, alias="allowedHosts")
    blocked_hosts: list[str] = Field(default_factory=list, alias="blockedHosts")
    provider_configs: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        alias="providerConfigs",
    )

    model_config = {"populate_by_name": True}

    def get_brave_api_key(self) -> str:
        """Return the Brave API key with environment override."""
        return os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() or self.api_key.strip()

    def get_serper_api_key(self) -> str:
        """Return the Serper API key with environment override."""
        return (
            os.environ.get("SERPER_API_KEY", "").strip()
            or os.environ.get("SERP_API_KEY", "").strip()
            or self.serper_api_key.strip()
        )

    def get_provider_config(self, name: str) -> dict[str, object]:
        """Return the provider-specific config block for one runtime provider."""
        key = str(name or "").strip().lower()
        if not key:
            return {}
        value = self.provider_configs.get(key) or {}
        if isinstance(value, dict):
            return dict(value)
        return {}


class ShellConfig(BaseModel):
    """Shell execution configuration."""

    enabled: bool = True
    mode: Literal["disabled", "inline", "subprocess"] = "subprocess"
    backend: Literal[
        "native", "portable", "auto", "docker", "podman", "bubblewrap", "sandbox-exec"
    ] = "portable"
    container_image: str = Field(default="", alias="containerImage")
    timeout: int = 30
    max_memory_mb: int = Field(default=512, alias="maxMemoryMb")
    max_file_size_kb: int = Field(default=8192, alias="maxFileSizeKb")
    isolate_home: bool = Field(default=True, alias="isolateHome")
    confirm_dangerous: bool = Field(default=True, alias="confirmDangerous")

    model_config = {"populate_by_name": True}


class SecretIsolationConfig(BaseModel):
    """Per-tool secret exposure policy."""

    allow_environment_fallback: bool = Field(
        default=False,
        alias="allowEnvironmentFallback",
    )
    audit_access: bool = Field(default=True, alias="auditAccess")

    model_config = {"populate_by_name": True}


class BackgroundTasksConfig(BaseModel):
    """Background task runtime configuration."""

    max_concurrency: int = Field(default=3, alias="maxConcurrency")
    starvation_threshold_seconds: int = Field(
        default=300,
        alias="starvationThresholdSeconds",
    )
    stall_threshold_seconds: int = Field(default=120, alias="stallThresholdSeconds")
    alert_channel: str = Field(default="", alias="alertChannel")
    alert_escalation_channel: str = Field(default="", alias="alertEscalationChannel")
    alert_cooldown_seconds: int = Field(default=300, alias="alertCooldownSeconds")
    schedule_alert_retrying_after: int = Field(
        default=2,
        alias="scheduleAlertRetryingAfter",
    )
    schedule_alert_escalate_after: int = Field(
        default=3,
        alias="scheduleAlertEscalateAfter",
    )

    model_config = {"populate_by_name": True}


class ToolsConfig(BaseModel):
    """Tools configuration."""

    shell: ShellConfig = Field(default_factory=ShellConfig)
    secret_isolation: SecretIsolationConfig = Field(
        default_factory=SecretIsolationConfig,
        alias="secretIsolation",
    )
    background_tasks: BackgroundTasksConfig = Field(
        default_factory=BackgroundTasksConfig,
        alias="backgroundTasks",
    )
    web_search: WebSearchConfig = Field(
        default_factory=WebSearchConfig, alias="webSearch"
    )

    model_config = {"populate_by_name": True}


class MemoryConfig(BaseModel):
    """Memory system configuration."""

    max_history: int = Field(default=50, alias="maxHistory")
    semantic_search: bool = Field(default=False, alias="semanticSearch")

    model_config = {"populate_by_name": True}


class AgentDefaults(BaseModel):
    """Agent default settings."""

    model: str = ""  # Empty means use provider's defaultModel


class AgentsConfig(BaseModel):
    """Agents configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ModelRoutingConfig(BaseModel):
    """Intent-based model routing configuration."""

    enabled: bool = True
    daily_model: str = Field(default="gpt-5.2", alias="dailyModel")
    paper_model: str = Field(default="gpt-5.2", alias="paperModel")
    general_model: str = Field(
        default="qwen3-max-2026-01-23",
        alias="generalModel",
    )
    qwen_enable_search: bool = Field(default=True, alias="qwenEnableSearch")
    qwen_search_options: dict[str, object] = Field(
        default_factory=dict,
        alias="qwenSearchOptions",
    )

    model_config = {"populate_by_name": True}


class WorkflowDefaultsConfig(BaseModel):
    """Default workflow labels used for telemetry and capability presentation."""

    chat: str = "default_chat_loop"
    grounded: str = "grounded_current_info"
    web_model: str = Field(default="web_model_grounding", alias="webModel")
    scheduled: str = "scheduled_job_flow"
    heartbeat: str = "heartbeat_checklist"
    feishu_paper: str = Field(default="feishu_paper_template", alias="feishuPaper")
    wechat_article: str = Field(default="wechat_article_flow", alias="wechatArticle")

    model_config = {"populate_by_name": True}


class WorkflowRolePolicyConfig(BaseModel):
    """Configurable role-task budget and graph policy."""

    retry_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "executor": 2,
            "critic": 1,
            "summarizer": 1,
        },
        alias="retryBudgets",
    )
    turn_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "planner": 1,
            "router": 1,
            "executor": 2,
            "critic": 2,
            "summarizer": 2,
        },
        alias="turnBudgets",
    )
    enable_graph_fanout: bool = Field(default=True, alias="enableGraphFanout")

    model_config = {"populate_by_name": True}

    def get_retry_budget(self, role: str, default: int) -> int:
        """Return one configured retry budget for a workflow role."""
        value = self.retry_budgets.get(str(role or "").strip())
        return max(1, int(value or default))

    def get_turn_budget(self, role: str, default: int) -> int:
        """Return one configured turn budget for a workflow role."""
        value = self.turn_budgets.get(str(role or "").strip())
        return max(1, int(value or default))


class AgentConfig(BaseModel):
    """Agent runtime configuration."""

    max_iterations: int = Field(default=15, alias="maxIterations")
    max_tokens_per_session: int = Field(default=50000, alias="maxTokensPerSession")
    session_timeout: int = Field(default=300, alias="sessionTimeout")
    system_prompt: str = Field(default="", alias="systemPrompt")
    model_routing: ModelRoutingConfig = Field(
        default_factory=ModelRoutingConfig,
        alias="modelRouting",
    )
    workflow_defaults: WorkflowDefaultsConfig = Field(
        default_factory=WorkflowDefaultsConfig,
        alias="workflowDefaults",
    )
    workflow_role_policy: WorkflowRolePolicyConfig = Field(
        default_factory=WorkflowRolePolicyConfig,
        alias="workflowRolePolicy",
    )

    model_config = {"populate_by_name": True}


class DashboardConfig(BaseModel):
    """Dashboard configuration."""

    enabled: bool = True
    port: int = 18790
    password: Optional[str] = None


class HeartbeatConfig(BaseModel):
    """Periodic heartbeat checklist configuration."""

    enabled: bool = False
    interval_seconds: int = Field(default=1800, alias="intervalSeconds")
    notify_channel: str = Field(default="", alias="notifyChannel")
    checklist_path: str = Field(default="HEARTBEAT.md", alias="checklistPath")

    model_config = {"populate_by_name": True}


class ExtensionPolicyConfig(BaseModel):
    """Third-party extension trust and risk policy."""

    require_install_receipt: bool = Field(
        default=True,
        alias="requireInstallReceipt",
    )
    require_signed_bundles: bool = Field(
        default=False,
        alias="requireSignedBundles",
    )
    max_risk_level: Literal["low", "medium", "high"] = Field(
        default="medium",
        alias="maxRiskLevel",
    )
    trusted_publishers: dict[str, object] = Field(
        default_factory=dict,
        alias="trustedPublishers",
    )
    revoked_publishers: list[str] = Field(
        default_factory=list,
        alias="revokedPublishers",
    )
    registry_url: str = Field(
        default="",
        alias="registryUrl",
    )
    runtime_isolation_mode: Literal["disabled", "subprocess"] = Field(
        default="subprocess",
        alias="runtimeIsolationMode",
    )
    runtime_isolated_kinds: list[str] = Field(
        default_factory=lambda: ["search_provider", "channel"],
        alias="runtimeIsolatedKinds",
    )
    isolated_timeout_seconds: int = Field(
        default=15,
        alias="isolatedTimeoutSeconds",
    )

    model_config = {"populate_by_name": True}

    def get_publisher_secret(self, publisher: str, key_id: str = "") -> str:
        """Return the configured shared secret for one trusted publisher key."""
        key = str(publisher or "").strip()
        if not key:
            return ""
        value = self.trusted_publishers.get(key)
        normalized_key_id = str(key_id or "").strip()
        if isinstance(value, str):
            if normalized_key_id and normalized_key_id != "default":
                return ""
            return value.strip()
        if isinstance(value, dict):
            keys = value.get("keys")
            if isinstance(keys, dict):
                candidate_key = normalized_key_id or str(value.get("activeKeyId") or "").strip()
                if candidate_key:
                    candidate = keys.get(candidate_key)
                    return str(candidate or "").strip()
                if "default" in keys:
                    return str(keys.get("default") or "").strip()
            secret = value.get("secret") or ""
            return str(secret).strip()
        return ""

    def is_publisher_revoked(self, publisher: str) -> bool:
        """Return whether the publisher is explicitly revoked."""
        normalized = str(publisher or "").strip()
        if not normalized:
            return False
        return normalized in {
            str(item or "").strip()
            for item in self.revoked_publishers
        }

    def is_publisher_key_revoked(self, publisher: str, key_id: str) -> bool:
        """Return whether one publisher signing key is explicitly revoked."""
        normalized_publisher = str(publisher or "").strip()
        normalized_key_id = str(key_id or "").strip()
        if not normalized_publisher or not normalized_key_id:
            return False
        value = self.trusted_publishers.get(normalized_publisher)
        if not isinstance(value, dict):
            return False
        revoked = value.get("revokedKeyIds")
        if not isinstance(revoked, list):
            return False
        return normalized_key_id in {
            str(item or "").strip()
            for item in revoked
        }

    def isolates_kind(self, kind: str) -> bool:
        """Return whether the current runtime policy isolates one extension kind."""
        if self.runtime_isolation_mode != "subprocess":
            return False
        normalized = str(kind or "").strip().lower()
        if not normalized:
            return False
        return normalized in {
            str(item or "").strip().lower()
            for item in self.runtime_isolated_kinds
        }


class Config(BaseModel):
    """Main configuration model."""

    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    extensions: ExtensionPolicyConfig = Field(default_factory=ExtensionPolicyConfig)

    model_config = {"populate_by_name": True}

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> Config:
        """Load configuration from file."""
        if config_path is None:
            config_path = Path.home() / ".nanoclaw" / "config.json"

        if not config_path.exists():
            raise FileNotFoundError(
                f"Config not found at {config_path}. Run 'nanoclaw init' first."
            )

        data = json.loads(config_path.read_text())
        return cls(**data)

    def get_active_provider(self) -> tuple[str, str, str, Optional[str]]:
        """
        Get active provider details.

        Returns: (provider_name, api_key, default_model, base_url)
        """
        if self.providers.deepseek:
            # DeepSeek uses OpenAI-compatible API
            return (
                "openai",  # Use openai client code
                self.providers.deepseek.api_key,
                self.providers.deepseek.default_model,
                "https://api.deepseek.com",
            )
        elif self.providers.openrouter:
            return (
                "openrouter",
                self.providers.openrouter.api_key,
                self.providers.openrouter.default_model,
                None,
            )
        elif self.providers.anthropic:
            return (
                "anthropic",
                self.providers.anthropic.api_key,
                self.providers.anthropic.default_model,
                None,
            )
        elif self.providers.openai:
            return (
                "openai",
                self.providers.openai.api_key,
                self.providers.openai.default_model,
                self.providers.openai.base_url,
            )
        else:
            raise ValueError("No LLM provider configured.")

    def get_default_model(self) -> str:
        """Get the default model from agents config or provider."""
        if self.agents.defaults.model:
            return self.agents.defaults.model
        _, _, model, _ = self.get_active_provider()
        return model


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def set_config(config: Config) -> None:
    """Set the global config instance."""
    global _config
    _config = config


def get_workspace_path() -> Path:
    """Get the workspace directory path."""
    return Path.home() / ".nanoclaw" / "workspace"


def get_data_path() -> Path:
    """Get the data directory path."""
    data_dir = Path.home() / ".nanoclaw" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
