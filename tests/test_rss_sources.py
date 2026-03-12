"""Tests for RSS source registry helpers."""

from __future__ import annotations

import json
from pathlib import Path

from nanoclaw.core.rss_sources import is_mainland_source, load_rss_sources


def _write_registry(path: Path) -> None:
    """Write a minimal RSS registry fixture."""
    payload = {
        "channels": [
            {
                "id": "ai",
                "name": "AI",
                "sources": [
                    {
                        "title": "Global A",
                        "url": "https://example.com/a.xml",
                        "tier": 1,
                        "format": "rss",
                        "tags": ["ai"],
                    },
                    {
                        "title": "Mainland A",
                        "url": "https://example.com/cn.xml",
                        "tier": 2,
                        "format": "rss",
                        "tags": ["ai", "mainland-friendly"],
                    },
                ],
            },
            {
                "id": "finance",
                "name": "Finance",
                "sources": [
                    {
                        "title": "Mainland F",
                        "url": "https://example.com/f.xml",
                        "tier": 1,
                        "format": "rss",
                        "tags": ["cn", "finance"],
                    }
                ],
            },
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_is_mainland_source() -> None:
    """Mainland tag detection should accept cn and mainland-friendly tags."""
    assert is_mainland_source(["cn"]) is True
    assert is_mainland_source(["mainland-friendly"]) is True
    assert is_mainland_source(["ai", "global"]) is False


def test_load_rss_sources_prefer_mainland(tmp_path: Path) -> None:
    """prefer_mainland should order Mainland-friendly entries first per channel."""
    registry_path = tmp_path / "sources.json"
    _write_registry(registry_path)

    sources = load_rss_sources(registry_path, prefer_mainland=True)
    ai_titles = [source.title for source in sources if source.channel_id == "ai"]
    assert ai_titles == ["Mainland A", "Global A"]


def test_load_rss_sources_mainland_only_with_channel_filter(tmp_path: Path) -> None:
    """mainland_only should filter out non-mainland feeds and honor channel filters."""
    registry_path = tmp_path / "sources.json"
    _write_registry(registry_path)

    sources = load_rss_sources(
        registry_path,
        channel_filters={"ai"},
        prefer_mainland=True,
        mainland_only=True,
    )

    assert len(sources) == 1
    assert sources[0].title == "Mainland A"
    assert sources[0].channel_id == "ai"
