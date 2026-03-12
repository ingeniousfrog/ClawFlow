"""RSS source registry helpers used by runtime tools and scripts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class RssSource:
    """One feed source record from the RSS registry."""

    channel_id: str
    channel_name: str
    title: str
    url: str
    tier: int
    fmt: str
    tags: list[str]


def is_mainland_source(tags: Iterable[str]) -> bool:
    """Return True if source tags indicate Mainland-friendly accessibility."""
    lowered = {str(tag).strip().lower() for tag in tags}
    return "mainland-friendly" in lowered or "cn" in lowered


def load_rss_sources(
    path: Path,
    channel_filters: set[str] | None = None,
    prefer_mainland: bool = False,
    mainland_only: bool = False,
) -> list[RssSource]:
    """Load and flatten RSS sources from the registry file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    channels = raw.get("channels", [])

    items: list[RssSource] = []
    for channel in channels:
        channel_id = channel["id"]
        if channel_filters and channel_id not in channel_filters:
            continue
        channel_name = channel.get("name", channel_id)

        source_records: list[tuple[int, dict]] = list(enumerate(channel.get("sources", [])))

        if mainland_only:
            source_records = [
                record
                for record in source_records
                if is_mainland_source(list(record[1].get("tags", [])))
            ]

        if prefer_mainland:
            source_records.sort(
                key=lambda record: (
                    0 if is_mainland_source(list(record[1].get("tags", []))) else 1,
                    record[0],
                )
            )

        for _, source in source_records:
            items.append(
                RssSource(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    title=source["title"],
                    url=source["url"],
                    tier=int(source.get("tier", 3)),
                    fmt=source.get("format", "rss"),
                    tags=list(source.get("tags", [])),
                )
            )

    return items
