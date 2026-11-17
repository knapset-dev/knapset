"""
Knapset v2 Theme Configuration, Validation, Discovery & Curses Color Mapper.

Manages active theme persistence in `configs/theme.json` (or XDG user config dir),
discovers themes installed from `knapset-registry` into `themes/`, and translates
named ANSI colors, 256-color indices, and `#RRGGBB` hex values using stdlib only.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app_paths import REPO_ROOT, get_config_dir, get_config_file, get_plugins_root

THEME_CONFIG_REL = "theme.json"
DEFAULT_THEME_NAME = "agy-dark"

COLOR_ROLES: Tuple[str, ...] = (
    "accent",
    "positive",
    "minimize",
    "warning",
    "muted",
    "selection_fg",
    "selection_bg",
    "bar_fg",
    "bar_bg",
)

SYMBOL_KEYS: Tuple[str, ...] = ("fill", "half", "empty", "cursor")

NAMED_ANSI_COLORS: Dict[str, int] = {
    "default": -1,
    "none": -1,
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "pink": 5,
    "cyan": 6,
    "white": 7,
}

# Approximate RGB centroids for standard 8 ANSI colors (for hex -> 8-color fallback)
_ANSI8_RGB: Tuple[Tuple[int, Tuple[int, int, int]], ...] = (
    (0, (0, 0, 0)),
    (1, (205, 49, 49)),
    (2, (13, 188, 121)),
    (3, (229, 229, 16)),
    (4, (36, 114, 200)),
    (5, (188, 63, 188)),
    (6, (17, 168, 205)),
    (7, (229, 229, 229)),
)

_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)

DEFAULT_COLORS_256: Dict[str, Any] = {
    "accent": 51,
    "positive": 46,
    "minimize": 213,
    "warning": 220,
    "muted": 250,
    "selection_fg": 51,
    "selection_bg": 236,
    "bar_fg": 255,
    "bar_bg": 235,
}

DEFAULT_BASIC_COLORS: Dict[str, Any] = {
    "accent": "cyan",
    "positive": "green",
    "minimize": "magenta",
    "warning": "yellow",
    "muted": "white",
    "selection_fg": "cyan",
    "selection_bg": "default",
    "bar_fg": "white",
    "bar_bg": "default",
}

DEFAULT_SYMBOLS: Dict[str, str] = {
    "fill": "█",
    "half": "▌",
    "empty": "░",
    "cursor": "▸",
}


def hex_to_rgb(spec: str) -> Tuple[int, int, int]:
    """Parses `#RRGGBB` or `#RGB` into an `(r, g, b)` tuple."""
    clean = spec.strip().lstrip("#")
    if len(clean) == 3 and re.fullmatch(r"[0-9a-fA-F]{3}", clean):
        clean = "".join(ch * 2 for ch in clean)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", clean):
        raise ValueError(f"Invalid hex color '{spec}'")
    return int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)


def rgb_to_xterm256(r: int, g: int, b: int) -> int:
    """Quantizes 24-bit RGB to the nearest `xterm-256color` palette index (16..255)."""
    best_idx = 16
    best_dist = 1 << 60

    # 6x6x6 color cube (indices 16..231)
    for ri, rv in enumerate(_CUBE_LEVELS):
        for gi, gv in enumerate(_CUBE_LEVELS):
            for bi, bv in enumerate(_CUBE_LEVELS):
                dist = (r - rv) ** 2 + (g - gv) ** 2 + (b - bv) ** 2
                if dist < best_dist:
                    best_dist = dist
                    best_idx = 16 + 36 * ri + 6 * gi + bi

    # 24-step grayscale ramp (indices 232..255)
    for idx_off in range(24):
        gv = 8 + idx_off * 10
        dist = (r - gv) ** 2 + (g - gv) ** 2 + (b - gv) ** 2
        if dist < best_dist:
            best_dist = dist
            best_idx = 232 + idx_off

    return best_idx


def rgb_to_ansi8(r: int, g: int, b: int) -> int:
    """Maps 24-bit RGB to the nearest basic 8-color ANSI index (`0..7`)."""
    best_code = 7
    best_dist = 1 << 60
    for code, (cr, cg, cb) in _ANSI8_RGB:
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist < best_dist:
            best_dist = dist
            best_code = code
    return best_code


def resolve_color_value(val: Any, support_256: bool = True) -> int:
    """Resolves a theme color spec (int, ANSI name, or `#RRGGBB`) to a curses color integer."""
    if isinstance(val, bool):
        raise ValueError(f"Boolean is not a valid color value: {val!r}")
    if isinstance(val, int):
        if -1 <= val <= 255:
            if not support_256 and val >= 8:
                return val % 8
            return val
        raise ValueError(f"Color index out of range [-1..255]: {val}")
    if isinstance(val, str):
        raw = val.strip().lower()
        if raw in NAMED_ANSI_COLORS:
            return NAMED_ANSI_COLORS[raw]
        if raw.startswith("#"):
            r, g, b = hex_to_rgb(raw)
            return rgb_to_xterm256(r, g, b) if support_256 else rgb_to_ansi8(r, g, b)
        if re.fullmatch(r"-?\d+", raw):
            return resolve_color_value(int(raw), support_256=support_256)
    raise ValueError(f"Unsupported color specification: {val!r}")


def _validate_color_spec(val: Any, role: str) -> None:
    try:
        resolve_color_value(val, support_256=True)
    except ValueError as exc:
        raise ValueError(f"Invalid color for '{role}': {exc}") from exc


@dataclass
class ThemeConfig:
    """Normalized Knapset TUI theme definition."""

    name: str = DEFAULT_THEME_NAME
    title: str = "AGY Dark"
    author: str = "knapset-dev"
    description: str = "Default high-contrast cyan & slate dark palette"
    colors: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_COLORS_256))
    basic_colors: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_BASIC_COLORS))
    symbols: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SYMBOLS))
    source_path: Optional[Path] = None

    @classmethod
    def default(cls) -> "ThemeConfig":
        return cls()

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: Optional[Path] = None) -> "ThemeConfig":
        validate_theme_dict(data, require_colors=False)
        name = str(data.get("name") or data.get("active_theme") or DEFAULT_THEME_NAME).strip().lower()
        title = str(data.get("title") or name.replace("-", " ").replace("_", " ").title()).strip()
        author = str(data.get("author") or "community").strip()
        description = str(data.get("description") or "").strip()

        colors = dict(DEFAULT_COLORS_256)
        raw_colors = data.get("colors") or {}
        for k, v in raw_colors.items():
            if k in COLOR_ROLES:
                colors[k] = v

        basic_colors = dict(DEFAULT_BASIC_COLORS)
        raw_basic = data.get("basic_colors") or {}
        for k, v in raw_basic.items():
            if k in COLOR_ROLES:
                basic_colors[k] = v

        symbols = dict(DEFAULT_SYMBOLS)
        raw_sym = data.get("symbols") or {}
        for k, v in raw_sym.items():
            if k in SYMBOL_KEYS and str(v):
                symbols[k] = str(v)[0]

        return cls(
            name=name,
            title=title,
            author=author,
            description=description,
            colors=colors,
            basic_colors=basic_colors,
            symbols=symbols,
            source_path=source_path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "2.0",
            "name": self.name,
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "colors": copy.deepcopy(self.colors),
            "basic_colors": copy.deepcopy(self.basic_colors),
            "symbols": copy.deepcopy(self.symbols),
        }

    def resolve_role_pair(self, fg_role: str, bg_role: Optional[str], support_256: bool) -> Tuple[int, int]:
        palette = self.colors if support_256 else self.basic_colors
        fg_raw = palette.get(fg_role, self.colors.get(fg_role, "default"))
        fg = resolve_color_value(fg_raw, support_256=support_256)
        if not bg_role:
            return fg, -1
        bg_raw = palette.get(bg_role, self.colors.get(bg_role, "default"))
        bg = resolve_color_value(bg_raw, support_256=support_256)
        return fg, bg


def validate_theme_dict(data: Any, require_colors: bool = True) -> Dict[str, Any]:
    """Validates a declarative Knapset theme JSON payload."""
    if not isinstance(data, dict):
        raise ValueError("Theme payload must be a JSON object")

    raw_name = data.get("name") or data.get("active_theme")
    if require_colors and not str(raw_name or "").strip():
        raise ValueError("Theme JSON missing required 'name' field")
    if raw_name is not None:
        clean_name = str(raw_name).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", clean_name):
            raise ValueError(f"Invalid theme name '{clean_name}': must match ^[a-zA-Z0-9_-]+$")

    raw_colors = data.get("colors")
    if require_colors and not isinstance(raw_colors, dict):
        raise ValueError("Theme JSON missing required 'colors' object")
    if raw_colors is not None:
        if not isinstance(raw_colors, dict):
            raise ValueError("Field 'colors' must be a JSON object")
        for role, val in raw_colors.items():
            if role not in COLOR_ROLES:
                raise ValueError(f"Unknown color role '{role}' (allowed: {', '.join(COLOR_ROLES)})")
            _validate_color_spec(val, role)

    raw_basic = data.get("basic_colors")
    if raw_basic is not None:
        if not isinstance(raw_basic, dict):
            raise ValueError("Field 'basic_colors' must be a JSON object")
        for role, val in raw_basic.items():
            if role not in COLOR_ROLES:
                raise ValueError(f"Unknown basic color role '{role}'")
            _validate_color_spec(val, f"basic_colors.{role}")

    raw_symbols = data.get("symbols")
    if raw_symbols is not None:
        if not isinstance(raw_symbols, dict):
            raise ValueError("Field 'symbols' must be a JSON object")
        for key, sym in raw_symbols.items():
            if key not in SYMBOL_KEYS:
                raise ValueError(f"Unknown symbol key '{key}' (allowed: {', '.join(SYMBOL_KEYS)})")
            if not isinstance(sym, str) or not sym.strip():
                raise ValueError(f"Symbol '{key}' must be a non-empty string")

    return data


def get_theme_config_path(for_write: bool = False) -> Path:
    """Returns active `theme.json` config path (`for_write=True` always resolves writable dir)."""
    if for_write:
        return get_config_dir() / THEME_CONFIG_REL
    return get_config_file(THEME_CONFIG_REL)


def get_themes_dirs(extra_dirs: Optional[List[Path]] = None) -> List[Path]:
    """Returns deduplicated directories scanned for installed/custom theme JSON files."""
    candidates: List[Path] = [
        get_config_dir() / "themes",
        get_plugins_root() / "themes",
        REPO_ROOT / "themes",
        REPO_ROOT / "configs" / "themes",
    ]
    if extra_dirs:
        candidates = list(extra_dirs) + candidates

    seen: List[Path] = []
    for p in candidates:
        resolved = p.resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen


def discover_themes(extra_dirs: Optional[List[Path]] = None) -> Dict[str, ThemeConfig]:
    """Discovers builtin + installed/custom themes keyed by lowercase theme name."""
    builtin = ThemeConfig.default()
    found: Dict[str, ThemeConfig] = {builtin.name: builtin}

    for tdir in get_themes_dirs(extra_dirs=extra_dirs):
        if not tdir.is_dir():
            continue
        for jf in sorted(tdir.glob("*.json")):
            try:
                raw = json.loads(jf.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                if "name" not in raw:
                    raw = dict(raw, name=jf.stem.lower())
                cfg = ThemeConfig.from_dict(raw, source_path=jf)
                found[cfg.name] = cfg
            except Exception:
                continue
    return found


def resolve_theme(name_or_path: str, extra_dirs: Optional[List[Path]] = None) -> ThemeConfig:
    """Resolves a theme by installed name or direct JSON file path."""
    raw = str(name_or_path or "").strip()
    if not raw:
        raise ValueError("Theme name cannot be empty")
    cand = Path(raw).expanduser()
    if cand.is_file():
        data = json.loads(cand.read_text(encoding="utf-8"))
        if "name" not in data:
            data = dict(data, name=cand.stem.lower())
        return ThemeConfig.from_dict(data, source_path=cand)

    key = raw.lower()
    if key.startswith("theme:"):
        key = key[len("theme:") :].strip()
    themes = discover_themes(extra_dirs=extra_dirs)
    if key in themes:
        return themes[key]
    raise KeyError(f"Theme '{name_or_path}' not found locally. Search via `ks reg search --type theme`.")


def load_active_theme(extra_dirs: Optional[List[Path]] = None) -> ThemeConfig:
    """
    Loads the active theme configuration:
    - Reads `theme.json` via `get_theme_config_path()`
    - Merges referenced `active_theme` from `themes/` if present
    - Applies any custom inline `colors`, `basic_colors`, or `symbols` from `theme.json`
    """
    cfg_path = get_theme_config_path(for_write=False)
    if not cfg_path.is_file():
        return ThemeConfig.default()

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ThemeConfig.default()
    except Exception:
        return ThemeConfig.default()

    active_name = str(raw.get("active_theme") or raw.get("name") or DEFAULT_THEME_NAME).strip().lower()
    themes = discover_themes(extra_dirs=extra_dirs)
    base_theme = themes.get(active_name, ThemeConfig.default())

    merged = base_theme.to_dict()
    merged["name"] = active_name
    if raw.get("title"):
        merged["title"] = raw["title"]
    if raw.get("author"):
        merged["author"] = raw["author"]
    if raw.get("description"):
        merged["description"] = raw["description"]
    if isinstance(raw.get("colors"), dict):
        merged["colors"].update(raw["colors"])
    if isinstance(raw.get("basic_colors"), dict):
        merged["basic_colors"].update(raw["basic_colors"])
    if isinstance(raw.get("symbols"), dict):
        merged["symbols"].update(raw["symbols"])

    return ThemeConfig.from_dict(merged, source_path=cfg_path)


def write_theme_config(theme: ThemeConfig, target_path: Optional[Path] = None) -> Path:
    """Atomically writes active theme configuration into `theme.json`."""
    dest = target_path or get_theme_config_path(for_write=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "2.0",
        "active_theme": theme.name,
        "colors": dict(theme.colors),
        "basic_colors": dict(theme.basic_colors),
        "symbols": dict(theme.symbols),
    }
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return dest


def set_active_theme(name_or_path: str, extra_dirs: Optional[List[Path]] = None) -> Tuple[ThemeConfig, Path]:
    """Resolves a local/installed theme and persists it to `theme.json`."""
    theme = resolve_theme(name_or_path, extra_dirs=extra_dirs)
    written = write_theme_config(theme)
    return theme, written


def reset_active_theme() -> Tuple[ThemeConfig, Path]:
    """Resets `theme.json` back to the built-in `agy-dark` default theme."""
    default = ThemeConfig.default()
    written = write_theme_config(default)
    return default, written
