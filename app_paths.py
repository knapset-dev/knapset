"""
Runtime Path & XDG Directory Resolver for Knapset.

Supports both source-tree execution (in-repo `data/`, `configs/`, `extensions/`)
and read-only system installs (e.g. AUR / `paru` into `/usr/lib/knapset`, writing
mutable databases, caches, configs, and installed plugins to XDG user dirs).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent


def _is_writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def is_checkout_mode() -> bool:
    """Returns True when running directly from a writable source checkout."""
    if os.environ.get("KNAPSET_HOME", "").strip():
        return False
    if os.environ.get("KNAPSET_DATA_DIR", "").strip():
        return False
    data_dir = REPO_ROOT / "data"
    has_git = (REPO_ROOT / ".git").exists()
    return _is_writable_dir(REPO_ROOT) and (has_git or _is_writable_dir(data_dir))


def get_data_dir() -> Path:
    """Returns the writable directory for SQLite DBs, market caches, and audit state."""
    explicit = os.environ.get("KNAPSET_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    home_override = os.environ.get("KNAPSET_HOME", "").strip()
    if home_override:
        return Path(home_override).expanduser().resolve() / "data"
    if is_checkout_mode():
        return REPO_ROOT / "data"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser().resolve() if xdg_data else (Path.home() / ".local" / "share")
    return base / "knapset"


def get_config_dir() -> Path:
    """Returns the writable user config directory (`configs/` in checkout or XDG config)."""
    explicit = os.environ.get("KNAPSET_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    home_override = os.environ.get("KNAPSET_HOME", "").strip()
    if home_override:
        return Path(home_override).expanduser().resolve() / "configs"
    if is_checkout_mode():
        return REPO_ROOT / "configs"
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_cfg).expanduser().resolve() if xdg_cfg else (Path.home() / ".config")
    return base / "knapset"


def get_config_file(rel_path: str) -> Path:
    """
    Resolves a configuration file, preferring the writable user config dir
    and falling back to shipped defaults in `REPO_ROOT/configs/`.
    """
    user_path = get_config_dir() / rel_path
    if user_path.is_file():
        return user_path
    bundled = REPO_ROOT / "configs" / rel_path
    if bundled.is_file():
        return bundled
    return user_path


def get_plugins_root() -> Path:
    """Returns the writable root directory where marketplace installs extensions & plugins."""
    home_override = os.environ.get("KNAPSET_HOME", "").strip()
    if home_override:
        return Path(home_override).expanduser().resolve()
    if is_checkout_mode():
        return REPO_ROOT
    return get_data_dir()


def get_blueprints_dirs() -> List[Path]:
    """Returns deduplicated blueprint directories (user overrides + bundled defaults)."""
    dirs: List[Path] = []
    for candidate in (get_config_dir() / "blueprints", REPO_ROOT / "configs" / "blueprints"):
        resolved = candidate.resolve()
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def get_subsystem_dirs(rel_subpath: str) -> List[Path]:
    """
    Returns deduplicated plugin search directories for a subsystem subpath
    (e.g. 'extensions', 'info_providers/modules'), checking both bundled core
    modules and user-installed plugins under `get_plugins_root()`.
    """
    dirs: List[Path] = []
    for base in (REPO_ROOT, get_plugins_root()):
        p = (base / rel_subpath).resolve()
        if p not in dirs:
            dirs.append(p)
    return dirs


def collect_module_candidates(search_dirs: List[Path]) -> list:
    """
    Collects `(module_stem, entry_file, pkg_dir)` plugin candidates across `search_dirs`,
    preserving discovery order and skipping duplicates/templates.
    """
    candidates = []
    seen_stems = set()
    for mod_dir in search_dirs:
        if not mod_dir.exists():
            continue
        for py_file in sorted(mod_dir.glob("*.py")):
            if py_file.stem in ("template", "base") or py_file.name.startswith(("_", ".")):
                continue
            if py_file.stem not in seen_stems:
                seen_stems.add(py_file.stem)
                candidates.append((py_file.stem, py_file, None))
        for dir_path in sorted(mod_dir.iterdir()):
            if not dir_path.is_dir() or dir_path.name.startswith(("_", ".")):
                continue
            if dir_path.name in ("template", "base", "__pycache__"):
                continue
            init_file = dir_path / "__init__.py"
            if init_file.is_file() and dir_path.name not in seen_stems:
                seen_stems.add(dir_path.name)
                candidates.append((dir_path.name, init_file, dir_path))
    return candidates
