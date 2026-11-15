"""
Data Models and Types for Hardware Market Intelligence Marketplace.

Pure Python standard library implementation of manifests, bundles,
installed records, and version checking utilities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Dict, List, Optional


def parse_semver(ver: Optional[str]) -> tuple:
    """Parses a SemVer string into a comparable tuple of ints, stripping prefixes."""
    if not ver or not isinstance(ver, str):
        return (0, 0, 0)
    clean = ver.strip().lstrip("vV")
    parts = []
    for part in clean.split("."):
        num = ""
        for char in part:
            if char.isdigit():
                num += char
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_compatible_version(min_ver: str, current_ver: str) -> bool:
    """Returns True if current_ver >= min_ver according to semantic versioning."""
    if not min_ver:
        return True
    return parse_semver(current_ver) >= parse_semver(min_ver)


def parse_dependency_spec(spec: str) -> tuple[str, str, str]:
    """
    Parses a dependency requirement spec like 'base_helper>=1.2.0' into
    (name, operator, version_str).
    If no operator is specified (e.g. 'base_helper'), returns ('base_helper', '', '').
    """
    if not spec or not isinstance(spec, str):
        return ("", "", "")
    match = re.match(r"^([a-zA-Z0-9_-]+)\s*(>=|<=|==|>|<)?\s*(.*)$", spec.strip())
    if not match:
        return (spec.strip().lower(), "", "")
    name, op, ver = match.groups()
    return (name.lower(), op or "", ver.strip())


def check_version_constraint(version: str, op: str, required_ver: str) -> bool:
    """
    Checks if a given version satisfies an operator constraint against required_ver.
    Supports '>=', '<=', '==', '>', '<', or '' (any version).
    """
    if not op or not required_ver:
        return True
    v_actual = parse_semver(version)
    v_req = parse_semver(required_ver)
    if op == ">=":
        return v_actual >= v_req
    elif op == "<=":
        return v_actual <= v_req
    elif op == "==":
        return v_actual == v_req
    elif op == ">":
        return v_actual > v_req
    elif op == "<":
        return v_actual < v_req
    return True


@dataclass
class ModuleManifest:
    name: str
    subsystem: str
    domain: str = "general"
    version: str = "1.0.0"
    author: str = "Community"
    description: str = ""
    min_app_version: str = "1.0.0"
    trust_level: str = "community"  # 'official', 'verified', 'community'
    source_type: str = "single_file"  # 'single_file' or 'directory'
    download_url: str = ""
    sha256: Optional[str] = None
    files: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    env_keys: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ModuleManifest:
        return cls(
            name=str(data.get("name", "")).strip().lower(),
            subsystem=str(data.get("subsystem", "")).strip().lower(),
            domain=str(data.get("domain", "general")).strip().lower(),
            version=str(data.get("version", "1.0.0")),
            author=str(data.get("author", "Community")),
            description=str(data.get("description", "")),
            min_app_version=str(data.get("min_app_version", "1.0.0")),
            trust_level=str(data.get("trust_level", "community")).lower(),
            source_type=str(data.get("source_type", "single_file")),
            download_url=str(data.get("download_url", "")),
            sha256=data.get("sha256"),
            files=[str(f) for f in data.get("files", []) if str(f).strip()],
            tags=[str(t).lower() for t in data.get("tags", [])],
            env_keys=[str(k).strip() for k in data.get("env_keys", [])],
            dependencies=[str(d).strip().lower() for d in data.get("dependencies", []) if str(d).strip()],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BundleManifest:
    name: str
    description: str = ""
    domain: Optional[str] = None
    modules: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BundleManifest:
        return cls(
            name=str(data.get("name", "")).strip().lower(),
            description=str(data.get("description", "")),
            domain=str(data.get("domain", "")).strip().lower() if data.get("domain") else None,
            modules=[str(m).strip() for m in data.get("modules", [])],
            tags=[str(t).lower() for t in data.get("tags", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InstalledModuleRecord:
    name: str
    subsystem: str
    domain: str
    version: str
    installed_at: str
    path: str
    sha256: str
    trust_level: str
    source_type: str = "single_file"
    env_keys: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InstalledModuleRecord:
        return cls(
            name=str(data.get("name", "")).strip().lower(),
            subsystem=str(data.get("subsystem", "")).strip().lower(),
            domain=str(data.get("domain", "general")),
            version=str(data.get("version", "1.0.0")),
            installed_at=str(data.get("installed_at", "")),
            path=str(data.get("path", "")),
            sha256=str(data.get("sha256", "")),
            trust_level=str(data.get("trust_level", "community")),
            source_type=str(data.get("source_type", "single_file")),
            env_keys=[str(k).strip() for k in data.get("env_keys", [])],
            dependencies=[str(d).strip().lower() for d in data.get("dependencies", []) if str(d).strip()],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
