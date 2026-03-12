"""Controlled persona fragments for prompt composition."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nanoclaw.core.logger import get_logger
from nanoclaw.security.prompt_guard import get_prompt_guard

logger = get_logger(__name__)

MAX_ITEMS_PER_SECTION = 6
MAX_FRAGMENT_LENGTH = 180

_SECTION_LABELS = {
    "identity": "Identity",
    "style": "Style",
    "workflow_preferences": "Workflow preferences",
    "config_hints": "Config hints",
}


class PersonaFragments(BaseModel):
    """Protected persona fragments rendered into the system prompt."""

    identity: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    workflow_preferences: list[str] = Field(default_factory=list, alias="workflowPreferences")
    config_hints: list[str] = Field(default_factory=list, alias="configHints")
    updated_at: str = Field(default="", alias="updatedAt")
    review_count: int = Field(default=0, alias="reviewCount")
    last_source: str = Field(default="", alias="lastSource")

    model_config = {"populate_by_name": True}


def get_default_persona_path() -> Path:
    """Return the default persona fragment store path."""
    return Path.home() / ".nanoclaw" / "data" / "persona_fragments.json"


class PersonaStore:
    """Load, save, and update protected persona fragments."""

    def __init__(self, path: Path | None = None) -> None:
        """Initialize the persona store."""
        self.path = path or get_default_persona_path()

    def load(self) -> PersonaFragments:
        """Load protected persona fragments from disk."""
        if not self.path.exists():
            return PersonaFragments()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("persona file must contain one JSON object")
            return PersonaFragments(**data)
        except Exception as exc:
            logger.warning("Failed to load persona fragments %s: %s", self.path, exc)
            return PersonaFragments()

    def save(self, fragments: PersonaFragments) -> PersonaFragments:
        """Persist protected persona fragments."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(_model_dump(fragments, by_alias=True), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            logger.warning("Failed to tighten persona fragment permissions for %s", self.path)
        return fragments

    def apply_review_summary(
        self,
        summary: str,
        *,
        source: str = "reviewed_summary",
    ) -> PersonaFragments:
        """Apply one reviewed summary to the protected fragment store."""
        updated = apply_review_summary(self.load(), summary, source=source)
        return self.save(updated)

    def render_prompt_section(self) -> str:
        """Render one prompt-safe persona fragment section."""
        return render_persona_prompt_section(self.load())


def apply_review_summary(
    current: PersonaFragments,
    summary: str,
    *,
    source: str = "reviewed_summary",
) -> PersonaFragments:
    """Apply one reviewed summary to the protected persona fragments."""
    text = str(summary or "").strip()
    if not text:
        raise ValueError("Review summary is empty.")

    if text.startswith("{"):
        next_fragments = _apply_json_review(current, text)
    else:
        next_fragments = _apply_text_review(current, text)

    next_fragments.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    next_fragments.last_source = str(source or "reviewed_summary").strip()
    next_fragments.review_count = int(current.review_count) + 1
    return next_fragments


def render_persona_prompt_section(fragments: PersonaFragments) -> str:
    """Render protected persona fragments for the runtime system prompt."""
    sections: list[str] = []
    for field_name, label in _SECTION_LABELS.items():
        values = list(getattr(fragments, field_name))
        if not values:
            continue
        body = "\n".join(f"- {item}" for item in values)
        sections.append(f"{label}:\n{body}")
    if not sections:
        return ""
    return (
        "PROTECTED PERSONA FRAGMENTS:\n"
        "These fragments come from reviewed summaries. "
        "The runtime must not edit raw system prompts directly.\n\n"
        + "\n\n".join(sections)
    )


def render_persona_text(fragments: PersonaFragments, path: Path) -> str:
    """Render protected persona fragments as concise plain text."""
    lines = ["Persona Fragments", "=" * 17, f"Path: {path}"]
    for field_name, label in _SECTION_LABELS.items():
        values = list(getattr(fragments, field_name))
        lines.append("")
        lines.append(f"{label} ({len(values)})")
        if not values:
            lines.append("- none")
            continue
        lines.extend(f"- {item}" for item in values)
    lines.append("")
    lines.append(f"Review count: {fragments.review_count}")
    lines.append(f"Last source: {fragments.last_source or '-'}")
    lines.append(f"Updated at: {fragments.updated_at or '-'}")
    return "\n".join(lines).rstrip()


def render_persona_json(fragments: PersonaFragments, path: Path) -> str:
    """Render protected persona fragments as JSON."""
    payload = _model_dump(fragments, by_alias=True)
    payload["path"] = str(path)
    return json.dumps(payload, indent=2, sort_keys=True)


def _apply_json_review(current: PersonaFragments, summary: str) -> PersonaFragments:
    """Apply one JSON review payload."""
    try:
        payload = json.loads(summary)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON review summary: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON review summary must be one object.")

    updated = _clone_fragments(current)
    touched = False
    for raw_key, raw_value in payload.items():
        section = _normalize_section_name(str(raw_key or ""))
        if not section:
            continue
        setattr(updated, section, _normalize_fragments(_coerce_fragments(raw_value)))
        touched = True
    if not touched:
        raise ValueError(
            "JSON review summary must include identity, style, workflowPreferences, "
            "or configHints."
        )
    return updated


def _apply_text_review(current: PersonaFragments, summary: str) -> PersonaFragments:
    """Apply one line-oriented review summary."""
    updates: dict[str, list[str]] = {
        "identity": [],
        "style": [],
        "workflow_preferences": [],
        "config_hints": [],
    }
    touched = False
    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s*", "", line)
        if ":" not in line:
            continue
        prefix, value = line.split(":", 1)
        section = _normalize_section_name(prefix)
        if not section:
            continue
        updates[section].append(value)
        touched = True

    if not touched:
        raise ValueError(
            "Review summary must use identity:, style:, workflow:, or config: prefixes."
        )

    updated = _clone_fragments(current)
    for section, values in updates.items():
        if not values:
            continue
        merged = list(getattr(updated, section)) + values
        setattr(updated, section, _normalize_fragments(merged))
    return updated


def _normalize_section_name(value: str) -> str:
    """Map one review summary key or prefix to a protected section name."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    mapping = {
        "identity": "identity",
        "style": "style",
        "workflow": "workflow_preferences",
        "workflow_preference": "workflow_preferences",
        "workflow_preferences": "workflow_preferences",
        "workflowpreferences": "workflow_preferences",
        "config": "config_hints",
        "config_hint": "config_hints",
        "config_hints": "config_hints",
        "confighints": "config_hints",
    }
    return mapping.get(normalized, "")


def _coerce_fragments(value: Any) -> list[str]:
    """Coerce one review value into a list of string fragments."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _normalize_fragments(values: list[str]) -> list[str]:
    """Normalize, deduplicate, and bound persona fragments."""
    guard = get_prompt_guard()
    normalized: list[str] = []
    for raw_value in values:
        text = str(raw_value or "").strip()
        text = re.sub(r"^[-*]\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        text = text[:MAX_FRAGMENT_LENGTH].rstrip()
        detected, _ = guard.check_injection(text)
        if detected:
            raise ValueError(f"Blocked suspicious persona fragment: {text[:60]}")
        if text not in normalized:
            normalized.append(text)
    if len(normalized) > MAX_ITEMS_PER_SECTION:
        normalized = normalized[-MAX_ITEMS_PER_SECTION:]
    return normalized


def _clone_fragments(fragments: PersonaFragments) -> PersonaFragments:
    """Return one mutable copy of the current fragment set."""
    if hasattr(fragments, "model_copy"):
        return fragments.model_copy(deep=True)
    return fragments.copy(deep=True)


def _model_dump(model: BaseModel, *, by_alias: bool = False) -> dict[str, Any]:
    """Dump one pydantic model across v1 and v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", by_alias=by_alias)
    return model.dict(by_alias=by_alias)
