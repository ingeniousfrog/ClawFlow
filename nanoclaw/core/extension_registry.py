"""Remote registry and update helpers for third-party extension bundles."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from pydantic import BaseModel, Field

from nanoclaw.core.extension_installer import (
    install_extension_bundle,
    load_extension_receipts,
    read_extension_bundle,
)


class ExtensionRegistryEntry(BaseModel):
    """One installable extension bundle exposed by a registry."""

    kind: str
    name: str
    version: str = ""
    summary: str = ""
    publisher: str = ""
    bundle_url: str = Field(alias="bundleUrl")
    sha256: str = ""
    notes: str = ""

    model_config = {"populate_by_name": True}


class ExtensionRegistryDocument(BaseModel):
    """Remote registry payload consumed by CLI install/update flows."""

    version: int = 1
    registry_name: str = Field(default="", alias="registryName")
    generated_at: str = Field(default="", alias="generatedAt")
    extensions: list[ExtensionRegistryEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def load_extension_registry(source: str) -> ExtensionRegistryDocument:
    """Load one extension registry from a local path or remote URL."""
    raw = json.loads(_read_text_source(source))
    return ExtensionRegistryDocument(**raw)


def list_registry_entries(
    *,
    source: str,
    install_dir: Path,
) -> list[dict[str, Any]]:
    """Return registry entries annotated with local install/update status."""
    registry = load_extension_registry(source)
    receipts = load_extension_receipts(install_dir)
    rows: list[dict[str, Any]] = []
    for item in registry.extensions:
        key = f"{item.kind}:{item.name}"
        receipt = receipts.get(key)
        installed_version = str(receipt.version if receipt is not None else "").strip()
        status = "available"
        if receipt is not None:
            if not item.version or item.version == installed_version:
                status = "current"
            elif _compare_versions(item.version, installed_version) > 0:
                status = "update_available"
            else:
                status = "ahead"
        rows.append(
            {
                "kind": item.kind,
                "name": item.name,
                "summary": item.summary,
                "version": item.version,
                "installed_version": installed_version,
                "status": status,
                "publisher": item.publisher,
                "bundle_url": _resolve_bundle_url(item.bundle_url, source),
                "notes": item.notes,
            }
        )
    return rows


def install_registry_extension(
    *,
    source: str,
    name: str,
    install_dir: Path,
    overwrite: bool,
    trusted_publishers: dict[str, object],
    publisher_policy: Any | None = None,
) -> dict[str, Any]:
    """Install or update one registry entry from its signed bundle."""
    normalized_name = str(name or "").strip().lower()
    if not normalized_name:
        raise ValueError("Registry install requires a non-empty extension name.")

    registry = load_extension_registry(source)
    entry = next(
        (
            item
            for item in registry.extensions
            if item.name.strip().lower() == normalized_name
        ),
        None,
    )
    if entry is None:
        raise ValueError(f"Extension `{name}` was not found in the configured registry.")

    bundle_url = _resolve_bundle_url(entry.bundle_url, source)
    bundle_bytes = _read_bytes_source(bundle_url)
    if entry.sha256:
        actual_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
        if actual_sha256 != entry.sha256:
            raise ValueError(
                f"Registry bundle hash mismatch for `{entry.name}`."
            )

    with tempfile.TemporaryDirectory(prefix="nanoclaw-ext-registry-") as temp_dir:
        temp_bundle = Path(temp_dir) / f"{entry.name}.ncext.zip"
        temp_bundle.write_bytes(bundle_bytes)
        bundle_manifest, _ = read_extension_bundle(temp_bundle)
        if bundle_manifest.kind != entry.kind:
            raise ValueError(
                f"Registry entry `{entry.name}` kind does not match bundle payload."
            )
        if bundle_manifest.primary_name != entry.name:
            raise ValueError(
                f"Registry entry `{entry.name}` name does not match bundle payload."
            )
        result = install_extension_bundle(
            temp_bundle,
            destination_dir=install_dir,
            overwrite=overwrite,
            trusted_publishers=trusted_publishers,
            require_signed_bundles=True,
            distribution_version=entry.version,
            registry_source=source,
            publisher_policy=publisher_policy,
        )
    result["registry_source"] = source
    result["version"] = entry.version
    return result


def _resolve_bundle_url(bundle_url: str, registry_source: str) -> str:
    """Resolve one bundle URL relative to the registry source."""
    normalized = str(bundle_url or "").strip()
    if not normalized:
        raise ValueError("Registry entry is missing bundleUrl.")
    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https", "file"}:
        return normalized
    source_path = Path(str(registry_source or "")).expanduser()
    if source_path.exists():
        return str((source_path.resolve().parent / normalized).resolve())
    return urljoin(str(registry_source), normalized)


def _read_text_source(source: str) -> str:
    """Read UTF-8 text from a local path or remote URL."""
    return _read_bytes_source(source).decode("utf-8")


def _read_bytes_source(source: str) -> bytes:
    """Read bytes from a local path or remote URL."""
    normalized = str(source or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme in {"http", "https"}:
        with urlopen(normalized, timeout=15) as response:
            return response.read()
    if parsed.scheme == "file":
        return Path(parsed.path).read_bytes()
    local_path = Path(normalized).expanduser()
    if local_path.exists():
        return local_path.read_bytes()
    raise ValueError(f"Unsupported extension registry source: {source}")


def _compare_versions(left: str, right: str) -> int:
    """Return a stable comparison result for two loose version strings."""
    if left == right:
        return 0
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    size = max(len(left_parts), len(right_parts))
    for index in range(size):
        left_part = left_parts[index] if index < len(left_parts) else 0
        right_part = right_parts[index] if index < len(right_parts) else 0
        if left_part == right_part:
            continue
        if left_part > right_part:
            return 1
        return -1
    return 0


def _version_parts(value: str) -> list[int]:
    """Convert one loose version string into comparable integer segments."""
    parts: list[int] = []
    for raw in str(value or "").replace("-", ".").split("."):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.extend(ord(char) for char in chunk.lower())
    return parts or [0]
