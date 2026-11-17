"""
Marketplace Client & Package Manager Engine.

Handles searching, installing, updating, and removing pluggable modules
across all 5 subsystems using Python Standard Library only.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import urllib.error
import urllib.parse
import urllib.request

from app_paths import get_data_dir, get_plugins_root
from marketplace.models import (
    BundleManifest,
    InstalledModuleRecord,
    ModuleManifest,
    check_version_constraint,
    is_compatible_version,
    parse_dependency_spec,
    parse_semver,
)
from marketplace.security import SecurityScanner

logger = logging.getLogger(__name__)

APP_VERSION = "1.0.2"
DEFAULT_REGISTRY_CACHE_TTL_SEC = 3600.0
OFFICIAL_REMOTE_REGISTRY_URL = (
    "https://raw.githubusercontent.com/knapset-dev/knapset-registry/main/registry.json"
)


def locate_registry_dir() -> Path:
    """
    Resolves the local knapset-registry directory when present on disk:
    1. $KNAPSET_REGISTRY environment variable
    2. Sibling ../knapset-registry checkout next to the core repository
    3. Legacy marketplace/sample_registry fallback path
    """
    env_dir = os.environ.get("KNAPSET_REGISTRY", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists():
            return p
    repo_root = Path(__file__).resolve().parent.parent
    sibling = repo_root.parent / "knapset-registry"
    if sibling.is_dir():
        return sibling
    return Path(__file__).resolve().parent / "sample_registry"


DEFAULT_REGISTRY_DIR = locate_registry_dir()

SUBSYSTEMS_MAP = {
    "info_providers": "info_providers/modules",
    "scam_analyser": "scam_analyser/modules",
    "component_analyser": "component_analyser/modules",
    "build_composer": "build_composer/modules",
    "ai_providers": "ai_providers/modules",
    "extension": "extensions",
    "extensions": "extensions",
    "preset": "presets",
    "presets": "presets",
}


def compute_directory_hash(dir_path: Union[str, Path]) -> str:
    """
    Computes a deterministic SHA256 hash of all files in a directory package.
    Files are sorted by relative POSIX paths, excluding bytecode and __pycache__.
    """
    p = Path(dir_path)
    if not p.is_dir():
        return ""
    files = []
    for item in p.rglob("*"):
        if item.is_file() and "__pycache__" not in item.parts and not item.name.endswith((".pyc", ".pyo")):
            rel = item.relative_to(p).as_posix()
            files.append((rel, item))
    files.sort(key=lambda x: x[0])
    h = hashlib.sha256()
    for rel_path, fpath in files:
        h.update(rel_path.encode("utf-8"))
        h.update(fpath.read_bytes())
    return h.hexdigest()


def _atomic_write_text(file_path: Path, content: str) -> None:
    """Atomically writes text to file using a temporary file in the same directory and replace."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(f"{file_path.suffix}.tmp.{datetime.now().timestamp()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(file_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


TYPE_ALIASES = {
    "info_providers": "info_providers",
    "provider": "info_providers",
    "info": "info_providers",
    "scam_analyser": "scam_analyser",
    "scam": "scam_analyser",
    "component_analyser": "component_analyser",
    "component": "component_analyser",
    "build_composer": "build_composer",
    "composer": "build_composer",
    "ai_providers": "ai_providers",
    "ai": "ai_providers",
    "extension": "extension",
    "domain": "extension",
    "preset": "presets",
    "presets": "presets",
}


class MarketplaceClient:
    """Core package manager and marketplace client for Knapset."""

    def __init__(
        self,
        registry_url_or_path: Optional[str] = None,
        root_dir: Optional[Union[str, Path]] = None,
    ):
        self.root_dir = Path(root_dir or get_plugins_root()).resolve()
        env_registry = os.environ.get("KNAPSET_REGISTRY_URL", "").strip()
        self.registry_source = registry_url_or_path or env_registry or OFFICIAL_REMOTE_REGISTRY_URL
        self.data_dir = (self.root_dir / "data") if root_dir is not None else get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.installed_file = self.data_dir / "installed_modules.json"
        self.cache_file = self.data_dir / "marketplace_cache.json"

    def _local_registry_path(self) -> Optional[Path]:
        """Resolves `registry_source` to a local filesystem Path when it is not HTTP(S)."""
        src = str(self.registry_source or "").strip()
        if not src or src.startswith(("http://", "https://")):
            return None
        if src.startswith("file://"):
            parsed = urllib.parse.urlparse(src)
            src = urllib.request.url2pathname(parsed.path)
        return Path(src).expanduser()

    def _local_registry_base(self) -> Optional[Path]:
        """Returns the base directory for resolving relative module payloads from a local registry."""
        p = self._local_registry_path()
        if p is None:
            return None
        if p.is_file() or p.suffix.lower() == ".json":
            return p.parent
        return p

    def _read_installed_records(self) -> Dict[str, InstalledModuleRecord]:
        if not self.installed_file.exists():
            return {}
        try:
            raw = json.loads(self.installed_file.read_text(encoding="utf-8"))
            return {k: InstalledModuleRecord.from_dict(v) for k, v in raw.items()}
        except Exception as e:
            logger.warning("Installed records file corrupted: %s. Backing up to .corrupt", e)
            try:
                corrupt_backup = self.installed_file.with_suffix(".json.corrupt")
                shutil.copy2(self.installed_file, corrupt_backup)
            except Exception:
                pass
            return {}

    def _save_installed_records(self, records: Dict[str, InstalledModuleRecord]) -> None:
        data = {k: v.to_dict() for k, v in records.items()}
        _atomic_write_text(self.installed_file, json.dumps(data, indent=2, ensure_ascii=False))

    def _registry_cache_ttl_sec(self) -> float:
        raw = os.environ.get("KNAPSET_REGISTRY_CACHE_TTL", "").strip()
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass
        return DEFAULT_REGISTRY_CACHE_TTL_SEC

    def _is_cache_fresh(self) -> bool:
        if not self.cache_file.is_file():
            return False
        ttl = self._registry_cache_ttl_sec()
        if ttl <= 0:
            return False
        try:
            age = time.time() - self.cache_file.stat().st_mtime
            return age < ttl
        except OSError:
            return False

    def fetch_registry_index(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Fetches the registry index from URL or local directory/file with TTL caching."""
        # 1. Local file or directory check (including file:// URLs)
        source_path = self._local_registry_path()
        if source_path is not None:
            if source_path.is_file():
                return json.loads(source_path.read_text(encoding="utf-8"))
            if source_path.is_dir():
                index_file = source_path / "registry.json"
                if index_file.is_file():
                    return json.loads(index_file.read_text(encoding="utf-8"))
                # Build on the fly from directory if registry.json missing
                return self._build_index_from_local_dir(source_path)

        # 2. Remote URL (http / https)
        if self.registry_source.startswith(("http://", "https://")):
            index_url = self.registry_source
            if not index_url.endswith(".json"):
                index_url = index_url.rstrip("/") + "/registry.json"

            if not force_refresh and self._is_cache_fresh():
                try:
                    return json.loads(self.cache_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            try:
                req = urllib.request.Request(
                    index_url, headers={"User-Agent": f"HardwareMarketIntelligence/{APP_VERSION}"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    _atomic_write_text(self.cache_file, json.dumps(data, indent=2))
                    return data
            except Exception as e:
                if hasattr(e, "close"):
                    try:
                        e.close()
                    except Exception:
                        pass
                if self.cache_file.exists():
                    logger.warning("Remote registry unreachable (%s). Using cached index.", e)
                    try:
                        return json.loads(self.cache_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                # Fallback to default bundled sample registry
                default_index = DEFAULT_REGISTRY_DIR / "registry.json"
                if default_index.is_file():
                    logger.warning("Remote registry unreachable (%s). Falling back to local registry checkout.", e)
                    return json.loads(default_index.read_text(encoding="utf-8"))
                raise RuntimeError(f"Could not connect to registry index at '{self.registry_source}': {e}")

        # Fallback to default bundled sample registry
        default_index = DEFAULT_REGISTRY_DIR / "registry.json"
        if default_index.is_file():
            return json.loads(default_index.read_text(encoding="utf-8"))
        return {"modules": [], "bundles": []}

    def _build_index_from_local_dir(self, base_dir: Path) -> Dict[str, Any]:
        modules = []
        bundles = []
        modules_dir = base_dir / "modules"
        if modules_dir.is_dir():
            for jf in modules_dir.rglob("*.json"):
                try:
                    modules.append(json.loads(jf.read_text(encoding="utf-8")))
                except Exception:
                    pass
        bundles_dir = base_dir / "bundles"
        if bundles_dir.is_dir():
            for bf in bundles_dir.glob("*.json"):
                try:
                    bundles.append(json.loads(bf.read_text(encoding="utf-8")))
                except Exception:
                    pass
        return {"modules": modules, "bundles": bundles}

    def search(
        self,
        query: str = "",
        domain: Optional[str] = None,
        subsystem: Optional[str] = None,
        tag: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Searches available modules and bundles matching criteria."""
        index = self.fetch_registry_index(force_refresh=force_refresh)
        raw_modules = index.get("modules", [])
        raw_bundles = index.get("bundles", [])

        q = query.lower().strip()
        matched_modules: List[Dict[str, Any]] = []
        matched_bundles: List[Dict[str, Any]] = []

        installed_map = self._read_installed_records()

        for m_data in raw_modules:
            m = ModuleManifest.from_dict(m_data)
            if domain and m.domain != domain.lower().strip():
                continue
            if subsystem and m.subsystem != subsystem.lower().strip():
                continue
            if tag and tag.lower().strip() not in m.tags:
                continue
            if q:
                searchable = f"{m.name} {m.description} {m.author} {' '.join(m.tags)} {m.domain} {m.subsystem}".lower()
                if q not in searchable:
                    continue

            d = m.to_dict()
            d["is_installed"] = m.name in installed_map
            d["installed_version"] = installed_map[m.name].version if m.name in installed_map else None
            matched_modules.append(d)

        for b_data in raw_bundles:
            b = BundleManifest.from_dict(b_data)
            if domain and b.domain and b.domain != domain.lower().strip():
                continue
            if tag and tag.lower().strip() not in b.tags:
                continue
            if q:
                searchable = f"{b.name} {b.description} {' '.join(b.tags)} {' '.join(b.modules)}".lower()
                if q not in searchable:
                    continue

            b_dict = b.to_dict()
            all_installed = all(m_name in installed_map for m_name in b.modules)
            b_dict["all_installed"] = all_installed
            matched_bundles.append(b_dict)

        return {"modules": matched_modules, "bundles": matched_bundles}

    def info(self, name: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """Returns detailed manifest and installation info for a module or bundle."""
        clean_name = name.strip().lower()
        if clean_name.startswith("bundle:"):
            clean_name = clean_name[len("bundle:") :]

        index = self.fetch_registry_index(force_refresh=force_refresh)
        installed_map = self._read_installed_records()

        # Check in modules
        for m_data in index.get("modules", []):
            if m_data.get("name", "").strip().lower() == clean_name:
                m = ModuleManifest.from_dict(m_data)
                res = m.to_dict()
                res["type"] = "module"
                res["is_installed"] = m.name in installed_map
                if m.name in installed_map:
                    res["installed_record"] = installed_map[m.name].to_dict()
                return res

        # Check in bundles
        for b_data in index.get("bundles", []):
            if b_data.get("name", "").strip().lower() == clean_name:
                b = BundleManifest.from_dict(b_data)
                res = b.to_dict()
                res["type"] = "bundle"
                res["modules_status"] = {
                    mod: {
                        "installed": mod in installed_map,
                        "version": installed_map[mod].version if mod in installed_map else None,
                    }
                    for mod in b.modules
                }
                return res

        return None

    def _resolve_resource_bytes(self, relative_or_absolute_url: str) -> bytes:
        """Reads file bytes from local registry path or downloads from remote URL."""
        # 1. Direct Remote HTTP/HTTPS
        if relative_or_absolute_url.startswith(("http://", "https://")):
            req = urllib.request.Request(
                relative_or_absolute_url,
                headers={"User-Agent": f"HardwareMarketIntelligence/{APP_VERSION}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()

        # 2. Remote registry relative URL download
        if self.registry_source.startswith(("http://", "https://")):
            base_remote = self.registry_source.rsplit("/", 1)[0]
            remote_url = f"{base_remote}/{relative_or_absolute_url.lstrip('/')}"
            try:
                req = urllib.request.Request(
                    remote_url,
                    headers={"User-Agent": f"HardwareMarketIntelligence/{APP_VERSION}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.read()
            except Exception as e:
                if hasattr(e, "close"):
                    try:
                        e.close()
                    except Exception:
                        pass
                # Fallback to local candidates if offline
                pass

        # 3. Local path resolution against local registry base or DEFAULT_REGISTRY_DIR or root_dir
        local_base = self._local_registry_base()
        candidate_paths = [
            local_base / relative_or_absolute_url if local_base else None,
            DEFAULT_REGISTRY_DIR / relative_or_absolute_url,
            self.root_dir / relative_or_absolute_url,
            Path(relative_or_absolute_url),
        ]
        for p in candidate_paths:
            if p and p.is_file():
                return p.read_bytes()

        raise FileNotFoundError(f"Could not locate module payload for '{relative_or_absolute_url}'")

    def _copy_directory_resource(
        self,
        relative_or_absolute_url: str,
        dest_dir: Path,
        files: Optional[List[str]] = None,
    ) -> None:
        """Copies a directory resource from local registry or downloads its file manifest from a remote registry."""
        if files and self.registry_source.startswith(("http://", "https://")):
            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                dest_dir.mkdir(parents=True, exist_ok=True)
                rel_base = relative_or_absolute_url.strip("/")
                for rel_file in files:
                    clean_rel = rel_file.lstrip("/")
                    target_file = (dest_dir / clean_rel).resolve()
                    if not target_file.is_relative_to(dest_dir.resolve()):
                        shutil.rmtree(dest_dir, ignore_errors=True)
                        raise ValueError(f"Security violation: package file escapes destination root: '{rel_file}'")
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    data_bytes = self._resolve_resource_bytes(f"{rel_base}/{clean_rel}")
                    target_file.write_bytes(data_bytes)
                return
            except ValueError:
                raise
            except Exception:
                shutil.rmtree(dest_dir, ignore_errors=True)

        local_base = self._local_registry_base()
        candidate_paths = [
            local_base / relative_or_absolute_url if local_base else None,
            locate_registry_dir() / relative_or_absolute_url,
            DEFAULT_REGISTRY_DIR / relative_or_absolute_url,
            self.root_dir / relative_or_absolute_url,
            Path(relative_or_absolute_url),
        ]
        for p in candidate_paths:
            if p and p.is_dir():
                # Security check: reject if source directory contains any symlinks
                for item in p.rglob("*"):
                    if item.is_symlink():
                        raise ValueError(f"Security violation: symlinks prohibited in package '{item.name}'")

                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(p, dest_dir, symlinks=True)

                # Security check: verify no symlinks and all files stay within dest_dir
                for item in dest_dir.rglob("*"):
                    if item.is_symlink():
                        shutil.rmtree(dest_dir, ignore_errors=True)
                        raise ValueError(f"Security violation: symlinks prohibited in package '{item.name}'")
                    if not item.resolve().is_relative_to(dest_dir.resolve()):
                        shutil.rmtree(dest_dir, ignore_errors=True)
                        raise ValueError(f"Security violation: package contents escape root: '{item.name}'")
                return

        raise FileNotFoundError(f"Could not locate module directory for '{relative_or_absolute_url}'")

    def install(
        self,
        target: str,
        force: bool = False,
        visited_bundles: Optional[Set[str]] = None,
        visited_modules: Optional[Set[str]] = None,
        force_refresh: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """Installs a module, bundle, domain, or tag."""
        target_norm = target.strip().lower()
        should_refresh = (visited_bundles is None and visited_modules is None) if force_refresh is None else force_refresh

        # 1. Bundle installation
        if target_norm.startswith("bundle:"):
            bundle_name = target_norm[len("bundle:") :]
            if visited_bundles is None:
                visited_bundles = set()
            if bundle_name in visited_bundles:
                return False, f"Cyclic bundle dependency detected: '{bundle_name}' already visited."
            visited_bundles.add(bundle_name)

            index = self.fetch_registry_index(force_refresh=should_refresh)
            bundle = next(
                (b for b in index.get("bundles", []) if b.get("name", "").lower() == bundle_name), None
            )
            if not bundle:
                return False, f"Bundle '{bundle_name}' not found in registry."
            modules = bundle.get("modules", [])
            results = []
            for mod_name in modules:
                ok, msg = self.install(
                    mod_name,
                    force=force,
                    visited_bundles=visited_bundles,
                    visited_modules=visited_modules,
                    force_refresh=False,
                )
                results.append(f"• {mod_name}: {'OK' if ok else 'FAIL'} ({msg})")
            return True, f"Installed bundle '{bundle_name}':\n" + "\n".join(results)

        # 2. Domain installation
        if target_norm.startswith("domain:"):
            dom_name = target_norm[len("domain:") :]
            index = self.fetch_registry_index(force_refresh=should_refresh)
            matched = [
                m.get("name")
                for m in index.get("modules", [])
                if m.get("domain", "").lower() == dom_name and m.get("name")
            ]
            if not matched:
                return False, f"No modules found for domain '{dom_name}'."
            results = []
            for mod_name in matched:
                ok, msg = self.install(
                    mod_name,
                    force=force,
                    visited_bundles=visited_bundles,
                    visited_modules=visited_modules,
                    force_refresh=False,
                )
                results.append(f"• {mod_name}: {'OK' if ok else 'FAIL'} ({msg})")
            return True, f"Installed domain '{dom_name}' ({len(matched)} modules):\n" + "\n".join(results)

        # 3. Tag installation
        if target_norm.startswith("tag:"):
            tag_name = target_norm[len("tag:") :]
            index = self.fetch_registry_index(force_refresh=should_refresh)
            matched = [
                m.get("name")
                for m in index.get("modules", [])
                if tag_name in [str(t).lower() for t in m.get("tags", [])] and m.get("name")
            ]
            if not matched:
                return False, f"No modules found with tag '{tag_name}'."
            results = []
            for mod_name in matched:
                ok, msg = self.install(
                    mod_name,
                    force=force,
                    visited_bundles=visited_bundles,
                    visited_modules=visited_modules,
                    force_refresh=False,
                )
                results.append(f"• {mod_name}: {'OK' if ok else 'FAIL'} ({msg})")
            return True, f"Installed tag '{tag_name}' ({len(matched)} modules):\n" + "\n".join(results)

        # Strip optional explicit single-item prefixes
        if target_norm.startswith("plugin:"):
            target_norm = target_norm[len("plugin:") :].strip()
        elif target_norm.startswith("module:"):
            target_norm = target_norm[len("module:") :].strip()
        elif target_norm.startswith("extension:"):
            target_norm = target_norm[len("extension:") :].strip()

        # 4. Single Module installation (with Zero-Prefix Smart Auto-Resolution)
        index = self.fetch_registry_index(force_refresh=should_refresh)
        m_data = next(
            (m for m in index.get("modules", []) if m.get("name", "").lower() == target_norm), None
        )
        if not m_data:
            # Check bundles (e.g. 'pc-starter-kit', 'photo-kit')
            bundle_match = next(
                (b for b in index.get("bundles", []) if b.get("name", "").lower() == target_norm), None
            )
            if bundle_match:
                return self.install(
                    f"bundle:{bundle_match['name']}",
                    force=force,
                    visited_bundles=visited_bundles,
                    visited_modules=visited_modules,
                    force_refresh=False,
                )

            # Check domain groups
            dom_matches = [
                m.get("name")
                for m in index.get("modules", [])
                if m.get("domain", "").lower() == target_norm and m.get("name")
            ]
            if dom_matches:
                return self.install(
                    f"domain:{target_norm}",
                    force=force,
                    visited_bundles=visited_bundles,
                    visited_modules=visited_modules,
                    force_refresh=False,
                )

            return False, f"Item '{target_norm}' not found in registry (checked modules, bundles, and domains)."

        manifest = ModuleManifest.from_dict(m_data)

        # Cyclic module dependency detection
        if visited_modules is None:
            visited_modules = set()
        if manifest.name in visited_modules:
            return False, f"Cyclic module dependency detected: '{manifest.name}' is already being installed."
        visited_modules.add(manifest.name)

        # Name validation: must be a safe identifier (alphanumeric, hyphens, underscores)
        if not re.match(r"^[a-zA-Z0-9_-]+$", manifest.name):
            return (
                False,
                f"Invalid module name '{manifest.name}'. Only alphanumeric characters, hyphens, and underscores are allowed.",
            )

        # App version check
        if not is_compatible_version(manifest.min_app_version, APP_VERSION):
            return (
                False,
                f"Incompatible core app version: module requires >={manifest.min_app_version}, current app is {APP_VERSION}.",
            )

        # Check installed status
        installed_records = self._read_installed_records()
        if manifest.name in installed_records and not force:
            cur_ver = installed_records[manifest.name].version
            return False, f"Module '{manifest.name}' is already installed (v{cur_ver}). Use --force or 'update'."

        # Resolve declared dependencies first with rollback on failure
        dep_notes = []
        installed_during_tx: List[str] = []

        def fail_install(reason: str) -> Tuple[bool, str]:
            for rollback_mod in installed_during_tx:
                self.remove(rollback_mod, force=True)
            return False, reason

        if manifest.dependencies:
            reg_modules = {m.get("name", "").lower(): m for m in index.get("modules", [])}
            for dep_spec in manifest.dependencies:
                dep_name, dep_op, dep_ver = parse_dependency_spec(dep_spec)
                if not dep_name:
                    continue

                dep_reg_info = reg_modules.get(dep_name)
                if not dep_reg_info:
                    return fail_install(
                        f"Required dependency '{dep_spec}' for module '{manifest.name}' not found in registry."
                    )
                dep_reg_ver = str(dep_reg_info.get("version", "1.0.0"))

                if dep_name in installed_records:
                    cur_ver = installed_records[dep_name].version
                    if dep_op and not check_version_constraint(cur_ver, dep_op, dep_ver):
                        if check_version_constraint(dep_reg_ver, dep_op, dep_ver):
                            ok_up, msg_up = self.install(
                                dep_name,
                                force=True,
                                visited_bundles=visited_bundles,
                                visited_modules=visited_modules,
                            )
                            if not ok_up:
                                return fail_install(
                                    f"Installed dependency '{dep_name}' v{cur_ver} does not satisfy requirement '{dep_spec}', and update failed: {msg_up}"
                                )
                            dep_notes.append(f"Updated dependency '{dep_name}' to v{dep_reg_ver}")
                        else:
                            return fail_install(
                                f"Installed dependency '{dep_name}' v{cur_ver} does not satisfy requirement '{dep_spec}' for module '{manifest.name}', and registry (v{dep_reg_ver}) has no compatible version."
                            )
                else:
                    if dep_op and not check_version_constraint(dep_reg_ver, dep_op, dep_ver):
                        return fail_install(
                            f"Dependency '{dep_name}' in registry (v{dep_reg_ver}) does not satisfy requirement '{dep_spec}' for module '{manifest.name}'."
                        )

                    ok_dep, msg_dep = self.install(
                        dep_name,
                        force=force,
                        visited_bundles=visited_bundles,
                        visited_modules=visited_modules,
                    )
                    if not ok_dep:
                        return fail_install(
                            f"Failed to install dependency '{dep_spec}' for module '{manifest.name}': {msg_dep}"
                        )
                    installed_during_tx.append(dep_name)
                    dep_notes.append(f"Installed dependency '{dep_spec}'")

        subsystem_dir_rel = SUBSYSTEMS_MAP.get(manifest.subsystem)
        if not subsystem_dir_rel:
            return fail_install(f"Unknown subsystem '{manifest.subsystem}' in manifest.")

        target_modules_dir = self.root_dir / subsystem_dir_rel
        target_modules_dir.mkdir(parents=True, exist_ok=True)

        computed_sha = ""
        # Single-file installation
        if manifest.source_type == "single_file":
            dl_ext = Path(manifest.download_url.split("?")[0]).suffix.lower()
            is_preset_data = manifest.subsystem in ("preset", "presets") or dl_ext in (".json", ".toml")
            ext = dl_ext if (is_preset_data and dl_ext in (".json", ".toml")) else (".json" if is_preset_data else ".py")
            dest_file = target_modules_dir / f"{manifest.name}{ext}"
            if not dest_file.resolve().is_relative_to(target_modules_dir.resolve()):
                return fail_install(f"Security violation: path traversal detected for module '{manifest.name}'.")

            # Clean up old directory package if transitioning from directory to single file
            old_pkg_dir = target_modules_dir / manifest.name
            if old_pkg_dir.is_dir():
                shutil.rmtree(old_pkg_dir, ignore_errors=True)

            try:
                code_bytes = self._resolve_resource_bytes(manifest.download_url)
            except Exception as e:
                return fail_install(f"Failed to download module payload: {e}")

            computed_sha = hashlib.sha256(code_bytes).hexdigest()
            if manifest.sha256 and manifest.sha256.lower() != computed_sha.lower():
                return fail_install(f"SHA256 mismatch! Expected {manifest.sha256}, got {computed_sha}")

            code_text = code_bytes.decode("utf-8", errors="replace")
            if is_preset_data:
                try:
                    from preset_loader import PresetConfig

                    if ext == ".toml":
                        import tomllib
                        PresetConfig.from_dict(tomllib.loads(code_text))
                    else:
                        PresetConfig.from_dict(json.loads(code_text))
                except Exception as exc:
                    return fail_install(f"Invalid v2 preset schema in '{manifest.name}': {exc}")
            else:
                # Run AST Security Scanner with subsystem role checking
                sec_report = SecurityScanner.scan_code(
                    code_text, filename=f"{manifest.name}.py", subsystem=manifest.subsystem
                )
                if not sec_report.is_safe:
                    return fail_install(f"Security Scanner Rejected Module:\n{sec_report.summary()}")

            _atomic_write_text(dest_file, code_text)
            dest_rel_path = f"{subsystem_dir_rel}/{manifest.name}{ext}"

        # Directory package installation
        elif manifest.source_type == "directory":
            dest_dir = target_modules_dir / manifest.name
            if not dest_dir.resolve().is_relative_to(target_modules_dir.resolve()):
                return fail_install(f"Security violation: path traversal detected for directory package '{manifest.name}'.")

            # Clean up old single-file module if transitioning from single file to directory package
            old_single_file = target_modules_dir / f"{manifest.name}.py"
            if old_single_file.is_file():
                old_single_file.unlink()

            try:
                self._copy_directory_resource(manifest.download_url, dest_dir, files=manifest.files)
            except Exception as e:
                return fail_install(f"Failed to copy directory package: {e}")

            # Run AST Security Scanner across directory with subsystem role checking
            sec_report = SecurityScanner.scan_directory(dest_dir, subsystem=manifest.subsystem)
            if not sec_report.is_safe:
                shutil.rmtree(dest_dir, ignore_errors=True)
                return fail_install(f"Security Scanner Rejected Directory Package:\n{sec_report.summary()}")

            computed_sha = compute_directory_hash(dest_dir)
            if manifest.sha256 and manifest.sha256.lower() != computed_sha.lower():
                shutil.rmtree(dest_dir, ignore_errors=True)
                return fail_install(f"Directory package SHA256 mismatch! Expected {manifest.sha256}, got {computed_sha}")

            dest_rel_path = f"{subsystem_dir_rel}/{manifest.name}"
        else:
            return fail_install(f"Unsupported source_type: {manifest.source_type}")

        # Register record
        now_str = datetime.now(timezone.utc).isoformat()
        rec = InstalledModuleRecord(
            name=manifest.name,
            subsystem=manifest.subsystem,
            domain=manifest.domain,
            version=manifest.version,
            installed_at=now_str,
            path=dest_rel_path,
            sha256=computed_sha,
            trust_level=manifest.trust_level,
            source_type=manifest.source_type,
            env_keys=manifest.env_keys,
            dependencies=manifest.dependencies,
        )
        # Re-read installed records to capture any newly installed dependencies
        fresh_records = self._read_installed_records()
        fresh_records[manifest.name] = rec
        self._save_installed_records(fresh_records)

        # Check environment keys warning
        missing_keys = [k for k in manifest.env_keys if not (k in sys.modules or k in sys.argv)]
        import os

        actual_missing = [k for k in manifest.env_keys if not os.environ.get(k)]
        note = f"Successfully installed '{manifest.name}' v{manifest.version} into {dest_rel_path}."
        if dep_notes:
            note += "\n  " + "\n  ".join(dep_notes)
        if actual_missing:
            note += f"\n  [WARN] Module requires environment variables: {', '.join(actual_missing)}."
            note += "\n  Configure them via .env or system environment."

        return True, note

    def update(
        self, name: Optional[str] = None, update_all: bool = False
    ) -> List[Tuple[str, bool, str]]:
        """Updates installed module(s) if a newer SemVer is available in the registry."""
        installed = self._read_installed_records()
        if not installed:
            return [("all", False, "No modules are currently installed.")]

        targets = list(installed.keys()) if update_all else ([name.strip().lower()] if name else [])
        if not targets:
            return [("none", False, "Specify module name or use --all to update.")]

        index = self.fetch_registry_index(force_refresh=True)
        reg_map = {m.get("name", "").lower(): m for m in index.get("modules", [])}

        results = []
        for mod_name in targets:
            if mod_name not in installed:
                results.append((mod_name, False, f"Module '{mod_name}' is not installed."))
                continue

            rec = installed[mod_name]
            if mod_name not in reg_map:
                results.append((mod_name, False, f"Module '{mod_name}' no longer found in registry."))
                continue

            remote_manifest = ModuleManifest.from_dict(reg_map[mod_name])
            if parse_semver(remote_manifest.version) <= parse_semver(rec.version):
                results.append(
                    (mod_name, True, f"Already at latest version (v{rec.version}).")
                )
                continue

            # Backup current file/folder
            cur_path = self.root_dir / rec.path
            bak_path = (
                cur_path.with_suffix(cur_path.suffix + ".bak")
                if cur_path.is_file()
                else cur_path.parent / f"{cur_path.name}.bak"
            )
            # Remove stale backup if it exists from a previous interrupted update
            if bak_path.exists():
                if bak_path.is_file():
                    bak_path.unlink()
                elif bak_path.is_dir():
                    shutil.rmtree(bak_path, ignore_errors=True)

            try:
                if cur_path.is_file():
                    shutil.copy2(cur_path, bak_path)
                elif cur_path.is_dir():
                    shutil.copytree(cur_path, bak_path)
            except Exception as e:
                results.append((mod_name, False, f"Failed to create backup before update: {e}"))
                continue

            # Perform update via install(force=True)
            ok, msg = self.install(mod_name, force=True)
            if ok:
                if bak_path.is_file():
                    bak_path.unlink()
                elif bak_path.is_dir():
                    shutil.rmtree(bak_path, ignore_errors=True)
                results.append((mod_name, True, f"Updated to v{remote_manifest.version}."))
            else:
                # Restore backup
                if bak_path.exists():
                    if cur_path.exists():
                        if cur_path.is_file():
                            cur_path.unlink()
                        else:
                            shutil.rmtree(cur_path, ignore_errors=True)
                    bak_path.rename(cur_path)
                results.append((mod_name, False, f"Update failed, rolled back: {msg}"))

        return results

    def remove(self, name: str, force: bool = False) -> Tuple[bool, str]:
        """Removes an installed module and cleans its record."""
        clean_name = name.strip().lower()
        installed = self._read_installed_records()

        # Check if other installed modules depend on this one
        if not force:
            dependent_mods = []
            for m_name, rec in installed.items():
                if m_name == clean_name:
                    continue
                for d_spec in getattr(rec, "dependencies", []):
                    d_name, _, _ = parse_dependency_spec(d_spec)
                    if d_name == clean_name:
                        dependent_mods.append(m_name)
                        break

            if dependent_mods:
                return (
                    False,
                    f"Cannot remove module '{clean_name}': required by installed module(s) {', '.join(dependent_mods)}. Use --force to override.",
                )

        dest_path = None
        if clean_name in installed:
            rec = installed[clean_name]
            dest_path = self.root_dir / rec.path
        else:
            # Search across subsystem modules dirs
            for sub_rel in SUBSYSTEMS_MAP.values():
                cand_file = self.root_dir / sub_rel / f"{clean_name}.py"
                cand_dir = self.root_dir / sub_rel / clean_name
                if cand_file.is_file():
                    dest_path = cand_file
                    break
                if cand_dir.is_dir():
                    dest_path = cand_dir
                    break

        if not dest_path or not dest_path.exists():
            return False, f"Module '{clean_name}' is not installed or file not found."

        try:
            if dest_path.is_file():
                dest_path.unlink()
            elif dest_path.is_dir():
                shutil.rmtree(dest_path)

            if clean_name in installed:
                del installed[clean_name]
                self._save_installed_records(installed)

            # Evict from sys.modules if loaded, including all package submodules
            for sub_name in list(SUBSYSTEMS_MAP.keys()):
                top_keys = [f"{sub_name}.modules.{clean_name}", f"{sub_name}.{clean_name}"]
                for top_key in top_keys:
                    sys.modules.pop(top_key, None)
                    sub_prefix = f"{top_key}."
                    for mod_key in list(sys.modules.keys()):
                        if mod_key.startswith(sub_prefix):
                            sys.modules.pop(mod_key, None)

            # Evict from extension_registry if cached
            try:
                import extension_registry
                if hasattr(extension_registry, "_REGISTRY") and clean_name in extension_registry._REGISTRY:
                    del extension_registry._REGISTRY[clean_name]
            except Exception:
                pass

            return True, f"Module '{clean_name}' successfully removed."
        except Exception as e:
            return False, f"Error deleting module files: {e}"

    def list_installed(self) -> List[Dict[str, Any]]:
        """Returns all installed module records."""
        records = self._read_installed_records()
        res = []
        for rec in records.values():
            full_path = self.root_dir / rec.path
            d = rec.to_dict()
            d["exists_on_disk"] = full_path.exists()
            res.append(d)
        return res

    def config(self, name: str) -> Dict[str, Any]:
        """Inspects required env_keys and configuration status for a module."""
        clean_name = name.strip().lower()
        installed = self._read_installed_records()
        keys: List[str] = []
        if clean_name in installed:
            keys = installed[clean_name].env_keys
        else:
            info_data = self.info(clean_name)
            if info_data:
                keys = info_data.get("env_keys", [])

        import os

        status = {}
        for k in keys:
            val = os.environ.get(k)
            status[k] = {
                "configured": bool(val),
                "preview": f"{val[:3]}...{val[-3:]}" if val and len(val) > 6 else ("SET" if val else "MISSING"),
            }

        return {
            "name": clean_name,
            "required_keys": keys,
            "status": status,
            "all_configured": all(s["configured"] for s in status.values()) if keys else True,
        }

    def scaffold_new(
        self,
        module_type: str,
        name: str,
        domain: str = "general",
        dest_dir: Optional[Union[str, Path]] = None,
    ) -> Tuple[bool, str, Optional[Path]]:
        """
        Scaffolds a new module or domain extension boilerplate from templates.
        """
        type_norm = TYPE_ALIASES.get(module_type.lower().strip())
        if not type_norm:
            valid_types = "info_providers, scam_analyser, component_analyser, build_composer, ai_providers, extension"
            return False, f"Unknown module type '{module_type}'. Choose from: {valid_types}", None

        clean_slug = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower().strip()).strip("_")
        if not clean_slug:
            return False, f"Invalid module name '{name}'. Must contain alphanumeric characters.", None

        # PascalCase name for classes
        camel_name = "".join(part.capitalize() for part in clean_slug.split("_"))

        # 1. Domain Extension Package
        if type_norm == "extension":
            target_dir = Path(dest_dir) if dest_dir else (self.root_dir / "extensions" / clean_slug)
            if target_dir.exists():
                return False, f"Destination directory '{target_dir}' already exists. Choose a different name or remove existing directory.", None

            template_dir = self.root_dir / "extensions" / "template"
            if not template_dir.is_dir():
                template_dir = Path(__file__).resolve().parent.parent / "extensions" / "template"
            if not template_dir.is_dir():
                return False, f"Template directory missing at '{template_dir}'", None

            target_dir.mkdir(parents=True, exist_ok=True)
            for f in template_dir.iterdir():
                if f.is_file() and f.suffix == ".py":
                    content = f.read_text(encoding="utf-8")
                    content = content.replace("extensions.template", f"extensions.{clean_slug}")
                    content = content.replace('domain_id = "template"', f'domain_id = "{clean_slug}"')
                    content = content.replace('domain_id="template"', f'domain_id="{clean_slug}"')
                    content = content.replace("template_", f"{clean_slug}_")
                    content = content.replace('"template"', f'"{clean_slug}"')
                    content = content.replace("Template", camel_name)
                    (target_dir / f.name).write_text(content, encoding="utf-8")

            msg = f"Successfully scaffolded new domain extension '{clean_slug}' at {target_dir}"
            return True, msg, target_dir

        # 2. Subsystem Single-File Module
        sub_folder = SUBSYSTEMS_MAP.get(type_norm)
        if not sub_folder:
            return False, f"No subsystem directory mapped for '{type_norm}'", None

        target_file = Path(dest_dir) / f"{clean_slug}.py" if dest_dir else (self.root_dir / sub_folder / f"{clean_slug}.py")
        if target_file.exists():
            return False, f"Destination file '{target_file}' already exists. Choose a different name or remove existing file.", None

        template_file = self.root_dir / sub_folder / "template.py"
        if not template_file.is_file():
            template_file = Path(__file__).resolve().parent.parent / sub_folder / "template.py"
        if not template_file.is_file():
            return False, f"Template file missing at '{template_file}'", None

        content = template_file.read_text(encoding="utf-8")
        # Template replacements
        content = content.replace("custom_template", clean_slug)
        content = content.replace("CustomMarketplaceProvider", f"{camel_name}Provider")
        content = content.replace("CustomScamAnalyser", f"{camel_name}ScamAnalyser")
        content = content.replace("CustomComponentAnalyser", f"{camel_name}ComponentAnalyser")
        content = content.replace("CustomBuildComposer", f"{camel_name}BuildComposer")
        content = content.replace("CustomAIProvider", f"{camel_name}AIProvider")
        content = content.replace("Custom", camel_name)
        content = content.replace("custom-market.com", f"{clean_slug}.com")
        content = content.replace('"general"', f'"{domain}"')

        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")

        msg = f"Successfully scaffolded new {type_norm} module '{clean_slug}' at {target_file}"
        return True, msg, target_file

    def _verify_local_path(self, path: Path) -> Dict[str, Any]:
        subsystem = None
        for sub, sub_dir in SUBSYSTEMS_MAP.items():
            if sub in str(path):
                subsystem = sub
                break
        if "extensions" in str(path):
            subsystem = "extension"

        if path.is_file() and path.suffix.lower() in (".json", ".toml"):
            current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            violations: List[str] = []
            warnings: List[str] = []
            try:
                from preset_loader import load_preset
                load_preset(path)
            except Exception as exc:
                violations.append(f"Invalid preset schema: {exc}")
            is_safe = len(violations) == 0
            return {
                "name": path.name,
                "path": str(path),
                "subsystem": "preset",
                "version": "2.0",
                "status": "HEALTHY" if is_safe else "UNSAFE",
                "exists": True,
                "checksum_match": True,
                "security_safe": is_safe,
                "current_sha": current_sha,
                "violations": violations,
                "warnings": warnings,
            }

        if path.is_file():
            sec_report = SecurityScanner.scan_file(path, subsystem=subsystem)
            current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            sec_report = SecurityScanner.scan_directory(path, subsystem=subsystem)
            current_sha = compute_directory_hash(path)

        violations = list(sec_report.violations)
        warnings = list(sec_report.warnings)

        if path.is_file():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
                classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
                if not classes:
                    warnings.append("No class definitions found in module.")
            except SyntaxError as e:
                violations.append(f"SyntaxError at line {e.lineno}: {e.msg}")

        is_safe = sec_report.is_safe and len(violations) == 0
        status = "HEALTHY" if is_safe else "UNSAFE"

        return {
            "name": path.name,
            "path": str(path),
            "subsystem": subsystem or "custom",
            "version": "local",
            "status": status,
            "exists": True,
            "checksum_match": True,
            "security_safe": is_safe,
            "current_sha": current_sha,
            "violations": violations,
            "warnings": warnings,
        }

    def verify(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Audits installed modules against their disk state, verifying checksums and AST security,
        or audits an arbitrary local file or directory path directly.
        """
        if name:
            p = Path(name)
            if not p.is_absolute():
                candidate = (self.root_dir / p).resolve()
                if candidate.exists():
                    p = candidate

            if p.exists():
                return [self._verify_local_path(p)]

        installed = self._read_installed_records()
        targets = [name.strip().lower()] if name else sorted(installed.keys())

        results = []
        for mod_name in targets:
            if mod_name not in installed:
                results.append({
                    "name": mod_name,
                    "status": "NOT_INSTALLED",
                    "exists": False,
                    "checksum_match": False,
                    "security_safe": False,
                    "violations": [f"Module '{mod_name}' is not in installed records"],
                })
                continue

            rec = installed[mod_name]
            file_path = self.root_dir / rec.path
            if not file_path.exists():
                results.append({
                    "name": mod_name,
                    "subsystem": rec.subsystem,
                    "version": rec.version,
                    "status": "MISSING",
                    "exists": False,
                    "checksum_match": False,
                    "security_safe": False,
                    "violations": [f"File or directory missing on disk: '{rec.path}'"],
                })
                continue

            if file_path.is_file() and file_path.suffix.lower() in (".json", ".toml"):
                from marketplace.security import SecurityReport
                current_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
                try:
                    from preset_loader import load_preset
                    load_preset(file_path)
                    sec_report = SecurityReport(is_safe=True)
                except Exception as exc:
                    sec_report = SecurityReport(is_safe=False, violations=[f"Invalid preset: {exc}"])
            elif file_path.is_file():
                current_sha = hashlib.sha256(file_path.read_bytes()).hexdigest()
                sec_report = SecurityScanner.scan_file(file_path, subsystem=rec.subsystem)
            else:
                current_sha = compute_directory_hash(file_path)
                sec_report = SecurityScanner.scan_directory(file_path, subsystem=rec.subsystem)

            checksum_ok = bool(rec.sha256 and current_sha.lower() == rec.sha256.lower())

            # Dependency verification
            dep_violations = []
            if getattr(rec, "dependencies", None):
                for dep_spec in rec.dependencies:
                    dep_name, dep_op, dep_ver = parse_dependency_spec(dep_spec)
                    if not dep_name:
                        continue
                    if dep_name not in installed:
                        dep_violations.append(f"Missing required dependency '{dep_spec}'")
                    else:
                        dep_rec = installed[dep_name]
                        if dep_op and not check_version_constraint(dep_rec.version, dep_op, dep_ver):
                            dep_violations.append(
                                f"Dependency '{dep_name}' (v{dep_rec.version}) does not satisfy required '{dep_spec}'"
                            )

            deps_ok = len(dep_violations) == 0
            is_healthy = checksum_ok and sec_report.is_safe and deps_ok

            status = "HEALTHY" if is_healthy else ("CORRUPTED" if not checksum_ok else ("BROKEN_DEPENDENCY" if not deps_ok else "VULNERABLE"))
            all_violations = list(sec_report.violations)
            if not checksum_ok:
                all_violations.append(
                    f"Checksum mismatch: expected '{rec.sha256}', found '{current_sha}' on disk"
                )
            all_violations.extend(dep_violations)

            results.append({
                "name": mod_name,
                "subsystem": rec.subsystem,
                "version": rec.version,
                "status": status,
                "exists": True,
                "checksum_match": checksum_ok,
                "security_safe": sec_report.is_safe,
                "expected_sha": rec.sha256,
                "current_sha": current_sha,
                "violations": all_violations,
                "warnings": sec_report.warnings,
            })

        return results
