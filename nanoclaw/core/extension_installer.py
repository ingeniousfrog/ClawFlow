"""Install and verify local third-party channel and provider extensions."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
import zipfile

from pydantic import BaseModel, Field

from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)

USER_INSTALLABLE_KINDS = {"channel", "search_provider"}
_TRUST_STORE_NAME = ".extension_receipts.json"
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class TrustedFile(BaseModel):
    """One installed file recorded in the local trust receipt."""

    path: str
    sha256: str


class BundleFile(BaseModel):
    """One file recorded inside a distributable extension bundle."""

    path: str
    sha256: str


class BundleSignature(BaseModel):
    """Optional bundle signature metadata."""

    publisher: str
    key_id: str = Field(default="default", alias="keyId")
    algorithm: str = "hmac-sha256"
    value: str

    model_config = {"populate_by_name": True}


class ExtensionBundleManifest(BaseModel):
    """Bundle metadata for distributable local extension archives."""

    version: int = 1
    kind: str
    name: str
    primary_name: str = Field(alias="primaryName")
    manifest_path: str = Field(alias="manifestPath")
    files: list[BundleFile] = Field(default_factory=list)
    signature: BundleSignature | None = None

    model_config = {"populate_by_name": True}


class ExtensionReceipt(BaseModel):
    """Receipt recorded for one installed local extension."""

    key: str
    name: str
    primary_name: str = Field(alias="primaryName")
    kind: str
    risk_level: str = Field(alias="riskLevel")
    manifest_path: str = Field(alias="manifestPath")
    manifest_sha256: str = Field(alias="manifestSha256")
    files: list[TrustedFile] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    sandbox_policy: str = Field(default="", alias="sandboxPolicy")
    distribution_type: str = Field(default="local_manifest", alias="distributionType")
    version: str = ""
    publisher: str = ""
    key_id: str = Field(default="", alias="keyId")
    registry_source: str = Field(default="", alias="registrySource")
    signature_verified: bool = Field(default=False, alias="signatureVerified")
    installed_at: str = Field(alias="installedAt")

    model_config = {"populate_by_name": True}


class ExtensionPolicyResult(BaseModel):
    """Runtime trust result for one local extension."""

    key: str
    allowed: bool
    status: str
    reason: str
    kind: str
    name: str
    risk_level: str = Field(alias="riskLevel")
    require_install_receipt: bool = Field(alias="requireInstallReceipt")
    max_risk_level: str = Field(alias="maxRiskLevel")
    permissions: list[str] = Field(default_factory=list)
    sandbox_policy: str = Field(default="", alias="sandboxPolicy")
    distribution_type: str = Field(default="", alias="distributionType")
    version: str = ""
    publisher: str = ""
    key_id: str = Field(default="", alias="keyId")
    registry_source: str = Field(default="", alias="registrySource")
    signature_verified: bool = Field(default=False, alias="signatureVerified")
    require_signed_bundles: bool = Field(alias="requireSignedBundles")

    model_config = {"populate_by_name": True}


def install_extension_manifest(
    manifest_path: Path,
    *,
    destination_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Install one local channel/provider manifest into the user extension directory."""
    from nanoclaw.core.plugins import PluginManifest, get_user_extension_dir

    source_manifest = manifest_path.expanduser().resolve()
    raw = json.loads(source_manifest.read_text(encoding="utf-8"))
    manifest = PluginManifest(**raw)
    if manifest.kind not in USER_INSTALLABLE_KINDS:
        raise ValueError(
            "Only `channel` and `search_provider` manifests can use extension-install."
        )
    valid_security, security_reason = _validate_declared_security(manifest.metadata)
    if not valid_security:
        raise ValueError(security_reason)

    target_dir = (destination_dir or get_user_extension_dir()).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)

    copied_paths: dict[str, Path] = {}
    for relative_path, source_path in _collect_manifest_source_files(
        source_manifest,
        module_name=manifest.module.strip(),
        metadata=manifest.metadata,
    ).items():
        target_path = target_dir / relative_path
        if target_path.exists() and target_path.resolve() != source_path and not overwrite:
            raise ValueError(f"Extension file already exists: {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_directory_tree(target_path.parent, stop_at=target_dir)
        if target_path.resolve() != source_path:
            shutil.copy2(source_path, target_path)
        target_path.chmod(0o600)
        copied_paths[relative_path] = target_path

    receipt = _build_receipt(
        manifest=manifest,
        manifest_path=copied_paths[source_manifest.name],
        copied_paths=copied_paths,
    )
    receipts = load_extension_receipts(target_dir)
    receipts[receipt.key] = receipt
    save_extension_receipts(receipts, target_dir)
    result = _policy_result_from_receipt(receipt)
    return {
        "key": receipt.key,
        "kind": receipt.kind,
        "name": receipt.primary_name,
        "manifest_path": receipt.manifest_path,
        "installed_files": len(receipt.files) + 1,
        "trust_status": result.status,
        "distribution_type": receipt.distribution_type,
        "version": receipt.version,
        "publisher": receipt.publisher,
        "registry_source": receipt.registry_source,
        "signature_verified": receipt.signature_verified,
        "verify_command": f"nanoclaw extension-verify --name {receipt.primary_name}",
    }


def pack_extension_manifest(
    manifest_path: Path,
    *,
    output_path: Path,
    publisher: str = "",
    key_id: str = "",
    shared_secret: str = "",
) -> dict[str, Any]:
    """Pack one local extension into a distributable bundle archive."""
    from nanoclaw.core.plugins import PluginManifest

    source_manifest = manifest_path.expanduser().resolve()
    raw = json.loads(source_manifest.read_text(encoding="utf-8"))
    manifest = PluginManifest(**raw)
    if manifest.kind not in USER_INSTALLABLE_KINDS:
        raise ValueError(
            "Only `channel` and `search_provider` manifests can use extension-pack."
        )
    valid_security, security_reason = _validate_declared_security(manifest.metadata)
    if not valid_security:
        raise ValueError(security_reason)
    if bool(publisher.strip()) != bool(shared_secret.strip()):
        raise ValueError("publisher and shared_secret must be provided together.")

    files = _collect_manifest_source_files(
        source_manifest,
        module_name=manifest.module.strip(),
        metadata=manifest.metadata,
    )
    bundle_manifest = ExtensionBundleManifest(
        kind=manifest.kind,
        name=manifest.name,
        primaryName=manifest.primary_name,
        manifestPath=source_manifest.name,
        files=[
            BundleFile(path=relative_path, sha256=_sha256_file(path))
            for relative_path, path in sorted(files.items())
        ],
        signature=_build_bundle_signature(
            publisher=publisher.strip(),
            key_id=key_id.strip() or "default",
            shared_secret=shared_secret.strip(),
            kind=manifest.kind,
            name=manifest.name,
            primary_name=manifest.primary_name,
            manifest_path=source_manifest.name,
            files=files,
        ),
    )
    target = output_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "bundle.json",
            json.dumps(_model_dump(bundle_manifest), indent=2, sort_keys=True),
        )
        for relative_path, path in sorted(files.items()):
            archive.write(path, arcname=relative_path)
    return {
        "bundle_path": str(target),
        "kind": manifest.kind,
        "name": manifest.primary_name,
        "publisher": publisher.strip(),
        "key_id": key_id.strip() or ("default" if publisher.strip() else ""),
        "signed": bool(bundle_manifest.signature),
        "file_count": len(files),
    }


def install_extension_bundle(
    bundle_path: Path,
    *,
    destination_dir: Path | None = None,
    overwrite: bool = False,
    trusted_publishers: dict[str, object] | None = None,
    require_signed_bundles: bool = False,
    distribution_version: str = "",
    registry_source: str = "",
    publisher_policy: Any | None = None,
) -> dict[str, Any]:
    """Install one distributable extension bundle into the user extension directory."""
    from nanoclaw.core.plugins import PluginManifest, get_user_extension_dir

    source_bundle = bundle_path.expanduser().resolve()
    bundle_manifest, bundle_files = read_extension_bundle(source_bundle)
    publisher = ""
    key_id = ""
    signature_verified = False
    if bundle_manifest.signature is not None:
        publisher = bundle_manifest.signature.publisher
        key_id = str(bundle_manifest.signature.key_id or "").strip()
        if _is_publisher_revoked(publisher_policy, publisher):
            raise ValueError(f"Bundle publisher `{publisher}` has been revoked.")
        if _is_publisher_key_revoked(publisher_policy, publisher, key_id):
            raise ValueError(
                f"Bundle signing key `{key_id}` for publisher `{publisher}` has been revoked."
            )
        shared_secret = _resolve_publisher_secret(
            publisher_policy,
            trusted_publishers or {},
            publisher,
            key_id,
        )
        if not shared_secret:
            raise ValueError(
                f"Bundle publisher `{publisher}` key `{key_id or 'default'}` is not trusted in config."
            )
        if not _verify_bundle_signature(bundle_manifest, shared_secret):
            raise ValueError("Bundle signature verification failed.")
        signature_verified = True
    elif require_signed_bundles:
        raise ValueError("Signed bundles are required by current extension policy.")

    manifest_bytes = bundle_files.get(bundle_manifest.manifest_path)
    if manifest_bytes is None:
        raise ValueError("Bundle is missing its manifest payload.")
    manifest = PluginManifest(**json.loads(manifest_bytes.decode("utf-8")))
    valid_security, security_reason = _validate_declared_security(manifest.metadata)
    if not valid_security:
        raise ValueError(security_reason)

    target_dir = (destination_dir or get_user_extension_dir()).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)

    copied_paths: dict[str, Path] = {}
    for relative_path, raw_bytes in sorted(bundle_files.items()):
        target_path = target_dir / relative_path
        if target_path.exists() and not overwrite:
            raise ValueError(f"Extension file already exists: {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_directory_tree(target_path.parent, stop_at=target_dir)
        target_path.write_bytes(raw_bytes)
        target_path.chmod(0o600)
        copied_paths[relative_path] = target_path

    receipt = _build_receipt(
        manifest=manifest,
        manifest_path=copied_paths[bundle_manifest.manifest_path],
        copied_paths=copied_paths,
        distribution_type="bundle",
        version=distribution_version.strip(),
        publisher=publisher,
        key_id=key_id,
        registry_source=registry_source.strip(),
        signature_verified=signature_verified,
    )
    receipts = load_extension_receipts(target_dir)
    receipts[receipt.key] = receipt
    save_extension_receipts(receipts, target_dir)
    result = _policy_result_from_receipt(receipt)
    return {
        "key": receipt.key,
        "kind": receipt.kind,
        "name": receipt.primary_name,
        "manifest_path": receipt.manifest_path,
        "installed_files": len(receipt.files) + 1,
        "trust_status": result.status,
        "distribution_type": receipt.distribution_type,
        "version": receipt.version,
        "publisher": receipt.publisher,
        "key_id": receipt.key_id,
        "registry_source": receipt.registry_source,
        "signature_verified": receipt.signature_verified,
        "verify_command": f"nanoclaw extension-verify --name {receipt.primary_name}",
    }


def load_extension_receipts(base_dir: Path) -> dict[str, ExtensionReceipt]:
    """Load the local extension trust store from the target directory."""
    trust_store = base_dir / _TRUST_STORE_NAME
    if not trust_store.exists():
        return {}
    try:
        raw = json.loads(trust_store.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to parse extension receipt store %s: %s", trust_store, exc)
        return {}
    items = raw.get("extensions") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        return {}
    receipts: dict[str, ExtensionReceipt] = {}
    for key, value in items.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        try:
            receipts[key] = ExtensionReceipt(**value)
        except Exception as exc:
            logger.warning("Skipping invalid extension receipt %s: %s", key, exc)
    return receipts


def save_extension_receipts(receipts: dict[str, ExtensionReceipt], base_dir: Path) -> None:
    """Persist the local extension trust store with private permissions."""
    trust_store = base_dir / _TRUST_STORE_NAME
    payload = {
        "version": 1,
        "extensions": {
            key: _model_dump(value)
            for key, value in sorted(receipts.items())
        },
    }
    trust_store.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    trust_store.chmod(0o600)


def evaluate_runtime_extension_policy(
    *,
    manifest_path: Path,
    kind: str,
    primary_name: str,
    module_name: str,
    metadata: dict[str, object],
    risk_level: str,
    require_install_receipt: bool,
    require_signed_bundles: bool,
    max_risk_level: str,
    publisher_policy: Any | None = None,
) -> ExtensionPolicyResult:
    """Return whether one user extension passes risk and trust policy."""
    normalized_risk = _normalize_risk_level(risk_level)
    valid_security, security_reason = _validate_declared_security(metadata)
    if not valid_security:
        return ExtensionPolicyResult(
            key=_extension_key(kind, primary_name),
            allowed=False,
            status="security_blocked",
            reason=security_reason,
            kind=kind,
            name=primary_name,
            riskLevel=normalized_risk,
            requireInstallReceipt=require_install_receipt,
            maxRiskLevel=_normalize_risk_level(max_risk_level),
            permissions=_manifest_permissions(metadata),
            sandboxPolicy=_manifest_sandbox_policy(metadata),
            distributionType="",
            version="",
            publisher="",
            keyId="",
            registrySource="",
            signatureVerified=False,
            requireSignedBundles=require_signed_bundles,
        )
    if _RISK_ORDER[normalized_risk] > _RISK_ORDER[_normalize_risk_level(max_risk_level)]:
        return ExtensionPolicyResult(
            key=_extension_key(kind, primary_name),
            allowed=False,
            status="risk_blocked",
            reason=f"risk level `{normalized_risk}` exceeds maxRiskLevel `{max_risk_level}`",
            kind=kind,
            name=primary_name,
            riskLevel=normalized_risk,
            requireInstallReceipt=require_install_receipt,
            maxRiskLevel=_normalize_risk_level(max_risk_level),
            permissions=_manifest_permissions(metadata),
            sandboxPolicy=_manifest_sandbox_policy(metadata),
            distributionType="",
            version="",
            publisher="",
            keyId="",
            registrySource="",
            signatureVerified=False,
            requireSignedBundles=require_signed_bundles,
        )

    receipts = load_extension_receipts(manifest_path.parent)
    receipt = receipts.get(_extension_key(kind, primary_name))
    if receipt is None:
        status = "untrusted"
        reason = (
            "extension has no signed bundle receipt"
            if require_signed_bundles
            else "extension has no install receipt"
        )
        return ExtensionPolicyResult(
            key=_extension_key(kind, primary_name),
            allowed=(not require_install_receipt) and (not require_signed_bundles),
            status=status,
            reason=reason,
            kind=kind,
            name=primary_name,
            riskLevel=normalized_risk,
            requireInstallReceipt=require_install_receipt,
            maxRiskLevel=_normalize_risk_level(max_risk_level),
            permissions=_manifest_permissions(metadata),
            sandboxPolicy=_manifest_sandbox_policy(metadata),
            distributionType="",
            version="",
            publisher="",
            keyId="",
            registrySource="",
            signatureVerified=False,
            requireSignedBundles=require_signed_bundles,
        )

    current_files = _collect_manifest_source_files(
        manifest_path,
        module_name=module_name,
        metadata=metadata,
    )
    trust_reason = _compare_receipt_against_files(receipt, current_files)
    trust_status = "trusted"
    if trust_reason == "" and require_signed_bundles and not receipt.signature_verified:
        trust_status = "untrusted"
        trust_reason = "extension receipt was not installed from a signed bundle"
    if trust_reason == "" and _is_publisher_revoked(publisher_policy, receipt.publisher):
        trust_status = "revoked"
        trust_reason = f"publisher `{receipt.publisher}` has been revoked"
    if trust_reason == "" and _is_publisher_key_revoked(
        publisher_policy,
        receipt.publisher,
        receipt.key_id,
    ):
        trust_status = "revoked"
        trust_reason = (
            f"publisher key `{receipt.key_id}` for `{receipt.publisher}` has been revoked"
        )
    if trust_reason and trust_status == "trusted":
        trust_status = "modified"
    return ExtensionPolicyResult(
        key=receipt.key,
        allowed=trust_reason == "",
        status=trust_status,
        reason=trust_reason or "install receipt matches current files",
        kind=kind,
        name=primary_name,
        riskLevel=normalized_risk,
        requireInstallReceipt=require_install_receipt,
        maxRiskLevel=_normalize_risk_level(max_risk_level),
        permissions=_manifest_permissions(metadata),
        sandboxPolicy=_manifest_sandbox_policy(metadata),
        distributionType=receipt.distribution_type,
        version=receipt.version,
        publisher=receipt.publisher,
        keyId=receipt.key_id,
        registrySource=receipt.registry_source,
        signatureVerified=bool(receipt.signature_verified),
        requireSignedBundles=require_signed_bundles,
    )


def verify_installed_extensions(
    *,
    base_dir: Path,
    selected_name: str = "",
    require_install_receipt: bool,
    require_signed_bundles: bool,
    max_risk_level: str,
    publisher_policy: Any | None = None,
) -> list[dict[str, Any]]:
    """Verify installed user extensions against local trust receipts."""
    manifests = sorted(base_dir.glob("*.plugin.json"))
    results: list[dict[str, Any]] = []
    for manifest_path in manifests:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        kind = str(raw.get("kind") or "").strip()
        if kind not in USER_INSTALLABLE_KINDS:
            continue
        primary_name = _primary_name_from_manifest(raw)
        if selected_name and selected_name not in {
            primary_name,
            str(raw.get("name") or "").strip(),
        }:
            continue
        result = evaluate_runtime_extension_policy(
            manifest_path=manifest_path,
            kind=kind,
            primary_name=primary_name,
            module_name=str(raw.get("module") or "").strip(),
            metadata=_as_dict(raw.get("metadata")),
            risk_level=str(raw.get("riskLevel") or "low"),
            require_install_receipt=require_install_receipt,
            require_signed_bundles=require_signed_bundles,
            max_risk_level=max_risk_level,
            publisher_policy=publisher_policy,
        )
        results.append(_model_dump(result))
    return results


def _build_receipt(
    *,
    manifest: Any,
    manifest_path: Path,
    copied_paths: dict[str, Path],
    distribution_type: str = "local_manifest",
    version: str = "",
    publisher: str = "",
    key_id: str = "",
    registry_source: str = "",
    signature_verified: bool = False,
) -> ExtensionReceipt:
    """Create one trust receipt for installed files."""
    files = [
        TrustedFile(path=relative_path, sha256=_sha256_file(path))
        for relative_path, path in sorted(copied_paths.items())
        if relative_path != manifest_path.name
    ]
    return ExtensionReceipt(
        key=_extension_key(manifest.kind, manifest.primary_name),
        name=manifest.name,
        primaryName=manifest.primary_name,
        kind=manifest.kind,
        riskLevel=_normalize_risk_level(manifest.risk_level),
        manifestPath=str(manifest_path),
        manifestSha256=_sha256_file(manifest_path),
        files=files,
        permissions=_manifest_permissions(manifest.metadata),
        sandboxPolicy=_manifest_sandbox_policy(manifest.metadata),
        distributionType=distribution_type,
        version=version or _manifest_distribution_version(manifest.metadata),
        publisher=publisher,
        keyId=key_id,
        registrySource=registry_source,
        signatureVerified=signature_verified,
        installedAt=datetime.now(timezone.utc).isoformat(),
    )


def _collect_manifest_source_files(
    manifest_path: Path,
    *,
    module_name: str,
    metadata: dict[str, object],
) -> dict[str, Path]:
    """Return all manifest and local module files required by one extension."""
    manifest_dir = manifest_path.parent
    files = {manifest_path.name: manifest_path}
    for module in _collect_manifest_modules(module_name, metadata):
        target, is_package = _resolve_local_module_target(module, manifest_dir)
        if is_package:
            package_root = target.parent
            for item in sorted(package_root.rglob("*")):
                if item.is_dir() or "__pycache__" in item.parts:
                    continue
                files[str(item.relative_to(manifest_dir))] = item
        else:
            files[str(target.relative_to(manifest_dir))] = target
    return files


def _collect_manifest_modules(module_name: str, metadata: dict[str, object]) -> list[str]:
    """Return unique local module names referenced by one manifest."""
    modules: list[str] = []
    if module_name.strip():
        modules.append(module_name.strip())
    runtime = _as_dict(metadata.get("runtime"))
    for key in ("handlerPath", "autoHandlerPath", "factoryPath"):
        value = str(runtime.get(key) or "").strip()
        if ":" not in value:
            continue
        candidate = value.split(":", 1)[0].strip()
        if candidate and candidate not in modules:
            modules.append(candidate)
    return modules


def _resolve_local_module_target(module_name: str, manifest_dir: Path) -> tuple[Path, bool]:
    """Resolve one local extension module name to a file or package path."""
    relative_path = Path(*module_name.split("."))
    file_candidate = (manifest_dir / relative_path).with_suffix(".py")
    if file_candidate.exists():
        return file_candidate.resolve(), False
    package_candidate = manifest_dir / relative_path / "__init__.py"
    if package_candidate.exists():
        return package_candidate.resolve(), True
    raise ValueError(f"Local extension module `{module_name}` was not found next to its manifest.")


def _compare_receipt_against_files(
    receipt: ExtensionReceipt,
    current_files: dict[str, Path],
) -> str:
    """Return an empty string when the trust receipt still matches current files."""
    manifest_path = Path(receipt.manifest_path)
    if not manifest_path.exists():
        return f"manifest missing: {receipt.manifest_path}"
    if _sha256_file(manifest_path) != receipt.manifest_sha256:
        return "manifest hash no longer matches install receipt"

    expected = {item.path: item.sha256 for item in receipt.files}
    current = {
        relative_path: _sha256_file(path)
        for relative_path, path in current_files.items()
        if relative_path != manifest_path.name
    }
    if set(expected) != set(current):
        return "installed file set no longer matches install receipt"
    for relative_path, digest in expected.items():
        if current.get(relative_path) != digest:
            return f"installed file hash changed: {relative_path}"
    return ""


def _policy_result_from_receipt(receipt: ExtensionReceipt) -> ExtensionPolicyResult:
    """Return one trusted policy result from a fresh install receipt."""
    return ExtensionPolicyResult(
        key=receipt.key,
        allowed=True,
        status="trusted",
        reason="install receipt matches current files",
        kind=receipt.kind,
        name=receipt.primary_name,
        riskLevel=receipt.risk_level,
        requireInstallReceipt=True,
        maxRiskLevel="high",
        permissions=list(receipt.permissions),
        sandboxPolicy=receipt.sandbox_policy,
        distributionType=receipt.distribution_type,
        version=receipt.version,
        publisher=receipt.publisher,
        keyId=receipt.key_id,
        registrySource=receipt.registry_source,
        signatureVerified=bool(receipt.signature_verified),
        requireSignedBundles=False,
    )


def _extension_key(kind: str, primary_name: str) -> str:
    """Return the stable trust-store key for one extension."""
    return f"{kind}:{primary_name}"


def _primary_name_from_manifest(raw: dict[str, Any]) -> str:
    """Return the primary name exposed by one raw manifest payload."""
    for key in ("provides", "toolNames"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return str(value[0]).strip()
    return str(raw.get("name") or "").strip()


def _manifest_permissions(metadata: dict[str, object]) -> list[str]:
    """Return declared permissions from manifest security metadata."""
    security = _as_dict(metadata.get("security"))
    value = security.get("permissions")
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _manifest_sandbox_policy(metadata: dict[str, object]) -> str:
    """Return the declared sandbox policy string from manifest metadata."""
    security = _as_dict(metadata.get("security"))
    return str(security.get("sandboxPolicy") or "").strip()


def _manifest_distribution_version(metadata: dict[str, object]) -> str:
    """Return the optional distribution version declared by one manifest."""
    distribution = _as_dict(metadata.get("distribution"))
    return str(distribution.get("version") or "").strip()


def _validate_declared_security(metadata: dict[str, object]) -> tuple[bool, str]:
    """Return whether one user extension declares explicit security metadata."""
    security = _as_dict(metadata.get("security"))
    permissions = security.get("permissions")
    if not isinstance(permissions, list):
        return False, "manifest missing metadata.security.permissions list"
    if not str(security.get("sandboxPolicy") or "").strip():
        return False, "manifest missing metadata.security.sandboxPolicy"
    return True, ""


def read_extension_bundle(
    bundle_path: Path,
) -> tuple[ExtensionBundleManifest, dict[str, bytes]]:
    """Read and verify one bundle archive before installation."""
    if not zipfile.is_zipfile(bundle_path):
        raise ValueError(f"Unsupported extension bundle: {bundle_path}")
    with zipfile.ZipFile(bundle_path) as archive:
        if "bundle.json" not in archive.namelist():
            raise ValueError("Bundle archive is missing bundle.json.")
        bundle_manifest = ExtensionBundleManifest(
            **json.loads(archive.read("bundle.json").decode("utf-8"))
        )
        files: dict[str, bytes] = {}
        expected = {item.path: item.sha256 for item in bundle_manifest.files}
        for relative_path, expected_sha256 in expected.items():
            try:
                raw_bytes = archive.read(relative_path)
            except KeyError as exc:
                raise ValueError(f"Bundle archive is missing {relative_path}.") from exc
            actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(f"Bundle file hash mismatch: {relative_path}")
            files[relative_path] = raw_bytes
        return bundle_manifest, files


def _build_bundle_signature(
    *,
    publisher: str,
    key_id: str,
    shared_secret: str,
    kind: str,
    name: str,
    primary_name: str,
    manifest_path: str,
    files: dict[str, Path],
) -> BundleSignature | None:
    """Build one optional HMAC bundle signature."""
    if not publisher or not shared_secret:
        return None
    payload = _bundle_signing_payload(
        kind=kind,
        name=name,
        primary_name=primary_name,
        manifest_path=manifest_path,
        files=files,
        publisher=publisher,
        key_id=key_id,
    )
    digest = hmac.new(
        shared_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return BundleSignature(publisher=publisher, keyId=key_id or "default", value=digest)


def _verify_bundle_signature(
    bundle_manifest: ExtensionBundleManifest,
    shared_secret: str,
) -> bool:
    """Verify one bundle signature against the trusted publisher secret."""
    signature = bundle_manifest.signature
    if signature is None or signature.algorithm != "hmac-sha256":
        return False
    payload = _bundle_signing_payload_from_bundle(bundle_manifest)
    expected = hmac.new(
        shared_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.value)


def _bundle_signing_payload(
    *,
    kind: str,
    name: str,
    primary_name: str,
    manifest_path: str,
    files: dict[str, Path],
    publisher: str,
    key_id: str,
) -> str:
    """Return canonical JSON payload used for bundle signing."""
    payload = {
        "version": 1,
        "kind": kind,
        "name": name,
        "primaryName": primary_name,
        "manifestPath": manifest_path,
        "publisher": publisher,
        "keyId": key_id or "default",
        "files": [
            {"path": relative_path, "sha256": _sha256_file(path)}
            for relative_path, path in sorted(files.items())
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _bundle_signing_payload_from_bundle(bundle_manifest: ExtensionBundleManifest) -> str:
    """Return canonical JSON payload for verifying one unpacked bundle manifest."""
    payload = {
        "version": bundle_manifest.version,
        "kind": bundle_manifest.kind,
        "name": bundle_manifest.name,
        "primaryName": bundle_manifest.primary_name,
        "manifestPath": bundle_manifest.manifest_path,
        "publisher": bundle_manifest.signature.publisher if bundle_manifest.signature else "",
        "keyId": bundle_manifest.signature.key_id if bundle_manifest.signature else "default",
        "files": [
            {"path": item.path, "sha256": item.sha256}
            for item in bundle_manifest.files
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _resolve_publisher_secret(
    publisher_policy: Any | None,
    trusted_publishers: dict[str, object],
    publisher: str,
    key_id: str,
) -> str:
    """Return the configured secret for one publisher signing key."""
    if publisher_policy is not None and hasattr(publisher_policy, "get_publisher_secret"):
        return str(publisher_policy.get_publisher_secret(publisher, key_id) or "").strip()
    value = (trusted_publishers or {}).get(publisher)
    if isinstance(value, str):
        if key_id and key_id != "default":
            return ""
        return value.strip()
    return ""


def _is_publisher_revoked(publisher_policy: Any | None, publisher: str) -> bool:
    """Return whether one publisher is revoked by current policy."""
    if not publisher or publisher_policy is None:
        return False
    if hasattr(publisher_policy, "is_publisher_revoked"):
        return bool(publisher_policy.is_publisher_revoked(publisher))
    return False


def _is_publisher_key_revoked(
    publisher_policy: Any | None,
    publisher: str,
    key_id: str,
) -> bool:
    """Return whether one publisher key is revoked by current policy."""
    if not publisher or not key_id or publisher_policy is None:
        return False
    if hasattr(publisher_policy, "is_publisher_key_revoked"):
        return bool(publisher_policy.is_publisher_key_revoked(publisher, key_id))
    return False


def _normalize_risk_level(value: str) -> Literal["low", "medium", "high"]:
    """Return one normalized supported risk level."""
    normalized = str(value or "low").strip().lower()
    if normalized not in _RISK_ORDER:
        return "medium"
    return normalized  # type: ignore[return-value]


def _ensure_private_directory_tree(path: Path, *, stop_at: Path) -> None:
    """Make created destination directories owner-only up to the target root."""
    current = path
    while current != stop_at and current.exists():
        current.chmod(0o700)
        current = current.parent


def _as_dict(value: object) -> dict[str, object]:
    """Return one plain dict when metadata is dict-like."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str)
    }


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for one file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _model_dump(item: BaseModel) -> dict[str, Any]:
    """Dump one pydantic model across v1 and v2."""
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return item.dict()
