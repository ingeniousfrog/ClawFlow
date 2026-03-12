"""Manifest-backed runtime registry for channel contracts and startup."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from nanoclaw.core.extension_runtime import (
    SubprocessChannelProxy,
    should_isolate_extension_runtime,
)
from nanoclaw.core.logger import get_logger
from nanoclaw.core.plugins import get_plugin_registry, load_manifest_object

logger = get_logger(__name__)


class ChannelRuntimeSpec(BaseModel):
    """Resolved channel runtime metadata derived from one plugin manifest."""

    name: str
    label: str
    delivery_mode: str = Field(alias="deliveryMode")
    managed: bool = True
    supports_incoming: bool = Field(default=True, alias="supportsIncoming")
    supports_proactive: bool = Field(default=True, alias="supportsProactive")
    supports_targeted_proactive: bool = Field(
        default=False,
        alias="supportsTargetedProactive",
    )
    supports_confirmation: bool = Field(default=True, alias="supportsConfirmation")
    config_name: str = Field(default="", alias="configName")
    factory_path: str = Field(default="", alias="factoryPath")
    required_fields: list[str] = Field(default_factory=list, alias="requiredFields")
    missing_reason: str = Field(default="channel config incomplete", alias="missingReason")
    ready_reason: str = Field(default="channel configured", alias="readyReason")
    starting_detail: str = Field(default="starting channel runtime", alias="startingDetail")
    running_detail: str = Field(default="channel runtime active", alias="runningDetail")
    proactive_target_mode: str = Field(default="", alias="proactiveTargetMode")
    proactive_target_field: str = Field(default="", alias="proactiveTargetField")
    proactive_target_missing_reason: str = Field(
        default="route target not configured",
        alias="proactiveTargetMissingReason",
    )
    proactive_target_ready_detail: str = Field(
        default="route target configured",
        alias="proactiveTargetReadyDetail",
    )
    auth_mode: str = Field(default="", alias="authMode")
    auth_detail: str = Field(default="", alias="authDetail")
    routing_priorities: dict[str, int] = Field(default_factory=dict, alias="routingPriorities")
    manifest_path: str = Field(default="", alias="manifestPath")
    source_scope: str = Field(default="", alias="sourceScope")

    model_config = {"populate_by_name": True}

    def get_channel_config(self, config: Any) -> Any | None:
        """Return the config object for this channel, when present."""
        channels_cfg = getattr(config, "channels", None)
        if channels_cfg is None:
            return None
        config_name = self.config_name or self.name
        cfg = getattr(channels_cfg, config_name, None)
        if cfg is not None:
            return cfg
        extensions = getattr(channels_cfg, "extensions", {}) or {}
        if isinstance(extensions, dict):
            value = extensions.get(config_name)
            if value is None and config_name != self.name:
                value = extensions.get(self.name)
            if isinstance(value, dict):
                return dict(value)
        return None

    def is_enabled(self, config: Any) -> bool:
        """Return whether config marks the channel enabled."""
        cfg = self.get_channel_config(config)
        return bool(_get_config_value(cfg, "enabled", False))

    def is_configured(self, config: Any) -> bool:
        """Return whether the channel has the required config fields."""
        cfg = self.get_channel_config(config)
        if cfg is None:
            return False
        if not self.required_fields:
            return True
        return all(
            _is_config_value_set(_get_config_value(cfg, field, ""))
            for field in self.required_fields
        )

    def validate_start(self, config: Any) -> tuple[bool, str]:
        """Return whether config allows this managed channel to start."""
        if not self.managed:
            return False, "channel is runtime-only"
        if not self.is_enabled(config):
            return False, "channel disabled in config"
        if not self.is_configured(config):
            return False, self.missing_reason
        return True, self.ready_reason

    def create_channel(self, config: Any, gateway: Any) -> Any:
        """Instantiate the channel runtime declared by this spec."""
        if not self.factory_path:
            raise ValueError(f"Channel `{self.name}` does not declare a factoryPath.")
        channel_config = self.get_channel_config(config)
        if (
            not self.supports_incoming
            and should_isolate_extension_runtime(kind="channel", source_scope=self.source_scope)
        ):
            return SubprocessChannelProxy(
                factory_path=self.factory_path,
                manifest_name=self.name,
                manifest_path=self.manifest_path,
                source_scope=self.source_scope,
                channel_config=channel_config,
            )
        factory = load_manifest_object(
            self.factory_path,
            manifest_name=self.name,
            manifest_path=self.manifest_path,
            source_scope=self.source_scope,
        )
        return factory(channel_config, gateway)

    def route_priority(self, purpose: str) -> int:
        """Return the manifest-defined routing priority for one purpose."""
        return int(self.routing_priorities.get(purpose, 100))


class ChannelRuntimeRegistry:
    """Runtime registry derived from enabled channel manifests."""

    def __init__(self, specs: list[ChannelRuntimeSpec]) -> None:
        """Store channel runtime specs in stable manifest order."""
        self._specs = {spec.name: spec for spec in specs}

    def channel_names(self) -> list[str]:
        """Return known channel names in manifest order."""
        return list(self._specs)

    def managed_channel_names(self) -> list[str]:
        """Return manifest-backed managed channel names."""
        return [name for name, spec in self._specs.items() if spec.managed]

    def get(self, name: str) -> ChannelRuntimeSpec | None:
        """Return one channel spec by name."""
        return self._specs.get(str(name or "").strip().lower())

    def all_specs(self) -> list[ChannelRuntimeSpec]:
        """Return all channel specs in manifest order."""
        return list(self._specs.values())

    def fallback_order(self, purpose: str) -> list[str]:
        """Return proactive-routing fallback order for one purpose."""
        candidates = [spec for spec in self._specs.values() if spec.supports_proactive]
        return [
            spec.name
            for spec in sorted(
                candidates,
                key=lambda item: (item.route_priority(purpose), item.name),
            )
        ]


_channel_runtime_registry: Optional[ChannelRuntimeRegistry] = None


def get_channel_runtime_registry(*, force_reload: bool = False) -> ChannelRuntimeRegistry:
    """Return the cached manifest-backed channel runtime registry."""
    global _channel_runtime_registry
    if _channel_runtime_registry is None or force_reload:
        specs: list[ChannelRuntimeSpec] = []
        for manifest in get_plugin_registry().get_enabled_channel_manifests():
            try:
                specs.append(_manifest_to_channel_spec(manifest))
            except Exception as exc:
                logger.error(
                    "Failed to build channel runtime spec from %s: %s",
                    manifest.manifest_path or manifest.name,
                    exc,
                )
        _channel_runtime_registry = ChannelRuntimeRegistry(specs)
    return _channel_runtime_registry


def reset_channel_runtime_registry() -> None:
    """Reset the cached channel runtime registry."""
    global _channel_runtime_registry
    _channel_runtime_registry = None


def _manifest_to_channel_spec(manifest: Any) -> ChannelRuntimeSpec:
    """Convert one plugin manifest into a channel runtime spec."""
    metadata = _as_dict(getattr(manifest, "metadata", {}))
    contract = _as_dict(metadata.get("contract"))
    runtime = _as_dict(metadata.get("runtime"))
    routing = _as_dict(metadata.get("routing"))
    return ChannelRuntimeSpec(
        name=manifest.primary_name,
        label=str(contract.get("label") or manifest.primary_name.title()),
        deliveryMode=str(contract.get("deliveryMode") or "runtime"),
        managed=bool(contract.get("managed", True)),
        supportsIncoming=bool(contract.get("supportsIncoming", True)),
        supportsProactive=bool(contract.get("supportsProactive", True)),
        supportsTargetedProactive=bool(contract.get("supportsTargetedProactive", False)),
        supportsConfirmation=bool(contract.get("supportsConfirmation", True)),
        configName=str(runtime.get("configName") or manifest.primary_name),
        factoryPath=str(runtime.get("factoryPath") or ""),
        requiredFields=_string_list(runtime.get("requiredFields")),
        missingReason=str(runtime.get("missingReason") or "channel config incomplete"),
        readyReason=str(runtime.get("readyReason") or "channel configured"),
        startingDetail=str(runtime.get("startingDetail") or "starting channel runtime"),
        runningDetail=str(runtime.get("runningDetail") or "channel runtime active"),
        proactiveTargetMode=str(routing.get("targetMode") or ""),
        proactiveTargetField=str(routing.get("targetField") or ""),
        proactiveTargetMissingReason=str(
            routing.get("targetMissingReason") or "route target not configured"
        ),
        proactiveTargetReadyDetail=str(
            routing.get("targetReadyDetail") or "route target configured"
        ),
        authMode=str(routing.get("authMode") or ""),
        authDetail=str(routing.get("authDetail") or ""),
        routingPriorities=_int_dict(routing.get("priorities")),
        manifestPath=str(getattr(manifest, "manifest_path", "") or ""),
        sourceScope=str(getattr(manifest, "source_scope", "") or ""),
    )


def _as_dict(value: object) -> dict[str, object]:
    """Return a plain dictionary when the raw manifest metadata is dict-like."""
    if isinstance(value, dict):
        return {
            str(key): item
            for key, item in value.items()
            if isinstance(key, str)
        }
    return {}


def _string_list(value: object) -> list[str]:
    """Return one normalized list of strings from manifest metadata."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _int_dict(value: object) -> dict[str, int]:
    """Return one normalized int dictionary from manifest metadata."""
    raw = _as_dict(value)
    normalized: dict[str, int] = {}
    for key, item in raw.items():
        try:
            normalized[key] = int(item)
        except (TypeError, ValueError):
            continue
    return normalized


def _is_config_value_set(value: object) -> bool:
    """Return whether one config field counts as populated."""
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


def _get_config_value(config: Any, field_name: str, default: object) -> object:
    """Return one config value from either a pydantic object or a raw extension dict."""
    if isinstance(config, dict):
        if field_name in config:
            return config[field_name]
        camel_name = _to_camel_case(field_name)
        if camel_name in config:
            return config[camel_name]
        return default
    return getattr(config, field_name, default)


def _to_camel_case(value: str) -> str:
    """Convert one snake_case name into camelCase."""
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)
