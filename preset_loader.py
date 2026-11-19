"""
Knapset v2 Preset Loader, Schema Validator, Discovery, Execution & Optional Plugin Host.

Supports:
- Pure declarative `.json` presets (`schema_version: "2.0"`) and `.toml` presets (Python 3.11+ via `tomllib`)
- Cross-DB references, derived formulas, filters, signed-weight profiles, multi-resource limits, and rules
- Optional Python plugin hooks (`BaseRowNormalizer`, `BaseScorer`, `BaseDataSource`)
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import tomllib  # Python 3.11+ stdlib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from app_paths import REPO_ROOT, get_config_dir, get_data_dir, get_plugins_root
from enricher import MatchSpec, enrich_rows, parse_match_expr
from formula_engine import (
    _SAFE_FUNCS,
    LimitSpec,
    SafeExpression,
    _extract_attr_chain,
    filter_rows,
    parse_weights,
)
from solver_engine import DEFAULT_NORM_MODE, solve_basket, solve_rank, solve_sets, validate_norm_mode
from sources.base import BaseDataSource
from sources.registry import get_source_registry, open_source


class BaseRowNormalizer(ABC):
    """Optional Python plugin hook to normalize or enrich rows for a slot/table before solving."""

    @abstractmethod
    def normalize(self, slot_name: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Transforms candidate rows and returns the normalized row list."""
        pass


class BaseScorer(ABC):
    """Optional Python plugin hook to compute domain-specific custom score adjustments."""

    @abstractmethod
    def adjust_solution(self, mode: str, solution_dict: Dict[str, Any]) -> Optional[float]:
        """Returns an optional additive delta to apply to a solution's total_score."""
        pass


@dataclass
class _LoadedPlugins:
    normalizers: List[BaseRowNormalizer] = field(default_factory=list)
    scorers: List[BaseScorer] = field(default_factory=list)
    schemes_registered: List[str] = field(default_factory=list)


def _load_python_plugins(plugin_paths: Sequence[Path]) -> _LoadedPlugins:
    loaded = _LoadedPlugins()
    registry = get_source_registry()
    for p in plugin_paths:
        resolved = p.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Preset plugin file not found: '{p}'")
        mod_name = f"knapset_plugin_{abs(hash(str(resolved)))}"
        spec = importlib.util.spec_from_file_location(mod_name, str(resolved))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load plugin module from '{resolved}'")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if not isinstance(obj, type):
                continue
            if obj in (BaseRowNormalizer, BaseScorer, BaseDataSource):
                continue
            if issubclass(obj, BaseRowNormalizer):
                loaded.normalizers.append(obj())
            elif issubclass(obj, BaseScorer):
                loaded.scorers.append(obj())
            elif issubclass(obj, BaseDataSource):
                scheme = getattr(obj, "scheme", None) or getattr(obj, "source_kind", None)
                if scheme:
                    registry.register_scheme(str(scheme), obj)
                    loaded.schemes_registered.append(str(scheme))
    return loaded


def _normalize_sources_block(raw_sources: Any) -> Dict[str, Dict[str, Any]]:
    if not raw_sources:
        return {}
    if isinstance(raw_sources, dict):
        # Single anonymous source block? e.g. {"source": "items.csv", "ref": "msrp"}
        if "source" in raw_sources and isinstance(raw_sources["source"], str):
            return {"items": dict(raw_sources)}
        out: Dict[str, Dict[str, Any]] = {}
        for slot, val in raw_sources.items():
            if isinstance(val, str):
                if ":" in val and not Path(val).exists():
                    src_part, tbl_part = val.rsplit(":", 1)
                    out[str(slot)] = {"source": src_part, "table": tbl_part}
                else:
                    out[str(slot)] = {"source": val}
            elif isinstance(val, dict):
                out[str(slot)] = dict(val)
            else:
                raise ValueError(f"Invalid source configuration for slot '{slot}'")
        return out
    raise ValueError("'sources' block in preset must be a mapping.")


def _normalize_references_block(raw_refs: Any) -> Dict[str, Dict[str, Any]]:
    if not raw_refs:
        return {}
    if not isinstance(raw_refs, dict):
        raise ValueError("'references' block in preset must be a mapping.")
    out: Dict[str, Dict[str, Any]] = {}
    for key, val in raw_refs.items():
        if not isinstance(val, dict) or "source" not in val:
            raise ValueError(f"Reference '{key}' must define a 'source' path.")
        out[str(key)] = dict(val)
    return out


def _normalize_profiles_block(raw_profiles: Any) -> Dict[str, Dict[str, Any]]:
    if not raw_profiles:
        return {"default": {"weights": {}}}
    if not isinstance(raw_profiles, dict):
        raise ValueError("'profiles' block in preset must be a mapping.")
    out: Dict[str, Dict[str, Any]] = {}
    for name, val in raw_profiles.items():
        if not isinstance(val, dict):
            raise ValueError(f"Profile '{name}' must be an object.")
        entry = dict(val)
        entry.setdefault("weights", {})
        out[str(name)] = entry
    return out


@dataclass
class PresetConfig:
    """Validated declarative Knapset v2 preset specification."""

    schema_version: str
    name: str
    title: str
    description: str
    mode: str  # "rank" | "basket" | "solve"
    sources: Dict[str, Dict[str, Any]]
    references: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    formulas: Any = field(default_factory=dict)
    where: List[str] = field(default_factory=list)
    limits: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    plugins: List[str] = field(default_factory=list)
    default_profile: Optional[str] = None
    top_n: Optional[int] = None
    max_per_item: int = 1
    qty_field: Optional[str] = None
    norm: str = DEFAULT_NORM_MODE
    diverse: bool = False
    source_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: Optional[Path] = None) -> PresetConfig:
        if not isinstance(data, dict):
            raise ValueError("Preset root must be a JSON/TOML object.")
        sv = str(data.get("schema_version", "2.0"))
        if sv not in ("2.0", "2"):
            raise ValueError(f"Unsupported preset schema_version '{sv}' (expected '2.0').")
        mode = str(data.get("mode", "solve")).lower().strip()
        if mode not in ("rank", "basket", "solve"):
            raise ValueError(f"Invalid preset mode '{mode}'. Must be 'rank', 'basket', or 'solve'.")

        raw_name = data.get("name") or (source_path.stem if source_path else "preset")
        name = str(raw_name).strip().lower().replace(" ", "_")
        title = str(data.get("title") or name)
        desc = str(data.get("description") or "")

        return cls(
            schema_version="2.0",
            name=name,
            title=title,
            description=desc,
            mode=mode,
            sources=_normalize_sources_block(data.get("sources")),
            references=_normalize_references_block(data.get("references")),
            formulas=data.get("formulas") or {},
            where=[str(x) for x in (data.get("where") or [])],
            limits=[str(x) for x in (data.get("limits") or [])],
            rules=[str(x) for x in (data.get("rules") or [])],
            profiles=_normalize_profiles_block(data.get("profiles")),
            plugins=[str(x) for x in (data.get("plugins") or [])],
            default_profile=data.get("default_profile"),
            top_n=int(data["top_n"]) if data.get("top_n") is not None else None,
            max_per_item=int(data.get("max_per_item", 1)),
            qty_field=data.get("qty_field"),
            norm=validate_norm_mode(data.get("norm")),
            diverse=bool(data.get("diverse", False)),
            source_path=source_path,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "sources": self.sources,
        }
        if self.references:
            payload["references"] = self.references
        payload["formulas"] = self.formulas
        if self.where:
            payload["where"] = self.where
        payload["limits"] = self.limits
        payload["rules"] = self.rules
        payload["profiles"] = self.profiles
        if self.plugins:
            payload["plugins"] = self.plugins
        if self.default_profile:
            payload["default_profile"] = self.default_profile
        if self.top_n is not None:
            payload["top_n"] = self.top_n
        if self.max_per_item != 1:
            payload["max_per_item"] = self.max_per_item
        if self.qty_field:
            payload["qty_field"] = self.qty_field
        if self.norm != DEFAULT_NORM_MODE:
            payload["norm"] = self.norm
        if self.diverse:
            payload["diverse"] = True
        return payload

    def profile_names(self) -> List[str]:
        return list(self.profiles.keys())

    def resolve_profile(self, profile_name: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        if profile_name:
            if profile_name not in self.profiles:
                raise KeyError(
                    f"Profile '{profile_name}' not found in preset '{self.name}'. "
                    f"Available: {', '.join(self.profile_names())}"
                )
            return profile_name, dict(self.profiles[profile_name])
        if self.default_profile and self.default_profile in self.profiles:
            return self.default_profile, dict(self.profiles[self.default_profile])
        first_name = next(iter(self.profiles))
        return first_name, dict(self.profiles[first_name])

    def _resolve_rel_path(self, target: str) -> str:
        if "://" in target:
            return target
        p = Path(target).expanduser()
        if p.is_absolute() or p.exists():
            return str(p.resolve() if p.exists() else p)
        if self.source_path is not None:
            candidate = (self.source_path.parent / p).resolve()
            if candidate.exists():
                return str(candidate)
        return target

    def load_plugins(self) -> _LoadedPlugins:
        if not self.plugins:
            return _LoadedPlugins()
        paths = [Path(self._resolve_rel_path(p)) for p in self.plugins]
        return _load_python_plugins(paths)

    def load_slots(
        self,
        source_overrides: Optional[Dict[str, str]] = None,
        plugins: Optional[_LoadedPlugins] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Loads all configured source slots, enriches them with wired references, and runs normalizer plugins."""
        loaded_plugins = plugins or self.load_plugins()
        overrides = source_overrides or {}

        # Preload reference datasets once
        ref_cache: Dict[str, List[Dict[str, Any]]] = {}
        for ref_key, ref_cfg in self.references.items():
            ref_target = self._resolve_rel_path(str(ref_cfg["source"]))
            ref_src = open_source(ref_target)
            ref_cache[ref_key] = ref_src.load_rows(
                table=ref_cfg.get("table"),
                query=ref_cfg.get("query"),
            )

        slot_pools: Dict[str, List[Dict[str, Any]]] = {}
        for slot_name, slot_cfg in self.sources.items():
            raw_target = overrides.get(slot_name) or slot_cfg.get("source")
            if not raw_target:
                raise ValueError(f"Slot '{slot_name}' has no source path specified.")
            resolved_target = self._resolve_rel_path(str(raw_target))
            src = open_source(resolved_target)
            rows = src.load_rows(table=slot_cfg.get("table"), query=slot_cfg.get("query"))

            # Apply one or multiple reference attachments wired to this slot
            wired_refs = slot_cfg.get("ref") or slot_cfg.get("refs") or []
            if isinstance(wired_refs, str):
                wired_refs = [wired_refs]

            for ref_name in wired_refs:
                if ref_name not in self.references:
                    raise KeyError(f"Slot '{slot_name}' references unknown ref '{ref_name}'.")
                r_cfg = self.references[ref_name]
                ref_rows = ref_cache[ref_name]
                match_expr = r_cfg.get("match")
                if not match_expr:
                    raise ValueError(f"Reference '{ref_name}' is missing required 'match' rule.")
                lf, rf, inferred_strat = parse_match_expr(str(match_expr))
                strategy = str(r_cfg.get("strategy") or inferred_strat or "token")
                alias = str(r_cfg.get("alias") or "ref")
                on_unmatched = str(r_cfg.get("on_unmatched") or "drop")
                rows = enrich_rows(
                    rows,
                    ref_rows,
                    MatchSpec(
                        left_field=lf,
                        right_field=rf,
                        strategy=strategy,
                        alias=alias,
                        on_unmatched=on_unmatched,
                    ),
                ).rows

            for norm in loaded_plugins.normalizers:
                rows = norm.normalize(slot_name, rows)

            slot_where = slot_cfg.get("where")
            if slot_where:
                clauses = [str(w) for w in slot_where] if isinstance(slot_where, (list, tuple)) else [str(slot_where)]
                rows = filter_rows(rows, clauses)

            slot_pools[slot_name] = rows

        return slot_pools

    def execute(
        self,
        profile_name: Optional[str] = None,
        mode_override: Optional[str] = None,
        prefer_override: Optional[str | Dict[str, Any] | Sequence[str]] = None,
        limits_override: Optional[Sequence[str]] = None,
        where_override: Optional[Sequence[str]] = None,
        formulas_override: Optional[Sequence[str] | Dict[str, str]] = None,
        rules_override: Optional[Sequence[str]] = None,
        top_n: Optional[int] = None,
        source_overrides: Optional[Dict[str, str]] = None,
        slot_pools_override: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        norm_override: Optional[str] = None,
        diverse_override: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Executes the preset solver pipeline and returns serializable results + metadata."""
        plugins = self.load_plugins()
        prof_key, prof_data = self.resolve_profile(profile_name)
        eff_mode = (mode_override or prof_data.get("mode") or self.mode).lower().strip()
        eff_norm = validate_norm_mode(norm_override or prof_data.get("norm") or self.norm)
        eff_diverse = bool(self.diverse if diverse_override is None else diverse_override)

        # Merge weights cleanly (profile base weights overwritten per-term by --prefer)
        base_weights = dict(prof_data.get("weights") or {})
        if prefer_override:
            override_terms = parse_weights(prefer_override)
            for t in override_terms:
                base_weights[t.expression] = t.weight

        # Merge formulas (preset dict/list + overrides)
        merged_formulas: List[str] = []
        if isinstance(self.formulas, dict):
            merged_formulas.extend(f"{k} = {v}" for k, v in self.formulas.items())
        elif isinstance(self.formulas, list):
            merged_formulas.extend(str(x) for x in self.formulas)
        if formulas_override:
            if isinstance(formulas_override, dict):
                merged_formulas.extend(f"{k} = {v}" for k, v in formulas_override.items())
            else:
                merged_formulas.extend(str(x) for x in formulas_override)

        merged_where = list(self.where) + list(prof_data.get("where") or []) + list(where_override or [])
        merged_limits = list(limits_override) if limits_override else list(prof_data.get("limits") or self.limits)
        merged_rules = list(self.rules) + list(prof_data.get("rules") or []) + list(rules_override or [])

        slot_pools = (
            slot_pools_override
            if slot_pools_override is not None
            else self.load_slots(source_overrides=source_overrides, plugins=plugins)
        )

        eff_top = top_n if top_n is not None else (self.top_n or (15 if eff_mode == "rank" else 5))

        if eff_mode == "rank":
            first_rows = next(iter(slot_pools.values()), [])
            ranked, meta = solve_rank(
                rows=first_rows,
                weights=base_weights,
                formulas=merged_formulas,
                where=merged_where,
                top_n=eff_top,
                norm=eff_norm,
            )
            results = [r.to_dict() for r in ranked]
        elif eff_mode == "basket":
            first_rows = next(iter(slot_pools.values()), [])
            sols, meta = solve_basket(
                rows=first_rows,
                weights=base_weights,
                limits=merged_limits,
                formulas=merged_formulas,
                where=merged_where,
                top_n=eff_top,
                max_per_item=self.max_per_item,
                qty_field=self.qty_field,
                norm=eff_norm,
            )
            results = [s.to_dict() for s in sols]
        elif eff_mode == "solve":
            sols, meta = solve_sets(
                slot_pools=slot_pools,
                weights=base_weights,
                limits=merged_limits,
                rules=merged_rules,
                formulas=merged_formulas,
                where=merged_where,
                top_n=eff_top,
                norm=eff_norm,
                diverse=eff_diverse,
            )
            results = [s.to_dict() for s in sols]
        else:
            raise ValueError(f"Unsupported mode '{eff_mode}'")

        # Apply optional BaseScorer plugin adjustments
        if plugins.scorers and results:
            for item in results:
                score_key = "score" if eff_mode == "rank" else "total_score"
                for scorer in plugins.scorers:
                    delta = scorer.adjust_solution(eff_mode, item)
                    if delta is not None:
                        item[score_key] = round(float(item.get(score_key, 0.0)) + float(delta), 2)
            sort_key = "score" if eff_mode == "rank" else "total_score"
            results.sort(key=lambda x: -float(x.get(sort_key, 0.0)))

        return {
            "preset": self.name,
            "title": self.title,
            "mode": eff_mode,
            "norm": eff_norm,
            "diverse": eff_diverse,
            "profile": prof_key,
            "weights": base_weights,
            "limits": merged_limits,
            "rules": merged_rules,
            "formulas": merged_formulas,
            "where": merged_where,
            "meta": meta,
            "results": results,
        }


def load_preset(path: str | Path) -> PresetConfig:
    """Loads and validates a `.json` or `.toml` v2 preset file."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Preset file not found: '{path}'")
    ext = p.suffix.lower()
    text = p.read_text(encoding="utf-8")
    if ext == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML presets require Python 3.11+ (stdlib `tomllib`). Use a .json preset on Python 3.10.")
        data = tomllib.loads(text)
    else:
        data = json.loads(text)
    return PresetConfig.from_dict(data, source_path=p)


def save_preset(preset: PresetConfig | Dict[str, Any], path: str | Path, overwrite: bool = True) -> Path:
    """Writes a v2 JSON preset cleanly to disk."""
    p = Path(path).expanduser().resolve()
    if p.exists() and not overwrite:
        raise FileExistsError(f"Preset file already exists: '{p}'")
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = preset.to_dict() if isinstance(preset, PresetConfig) else PresetConfig.from_dict(preset).to_dict()
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def get_user_presets_dir() -> Path:
    """Returns the writable persistent user presets directory (honors KNAPSET_HOME/XDG_CONFIG_HOME)."""
    import os

    if os.environ.get("KNAPSET_HOME", "").strip():
        return (get_plugins_root() / "presets").resolve()
    if os.environ.get("KNAPSET_CONFIG_DIR", "").strip():
        return (get_config_dir() / "presets").resolve()
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_cfg).expanduser().resolve() if xdg_cfg else (Path.home() / ".config")
    return (base / "knapset" / "presets").resolve()


def get_user_profiles_dir() -> Path:
    """Shared, source-independent library of saved preference profiles (weights/formulas/limits/rules)."""
    return (get_user_presets_dir().parent / "profiles").resolve()


PROFILE_FILE_SCHEMA = "knapset-profile/1"
_PROFILE_KEYS = ("mode", "norm", "diverse", "weights", "formulas", "where", "limits", "rules")


def _clean_profile_name(name: str) -> str:
    clean = re.sub(r"[^a-z0-9_.-]+", "_", str(name).strip().lower()).strip("_.")
    if not clean:
        raise ValueError("Profile name cannot be empty.")
    return clean


def save_shared_profile(name: str, profile: Dict[str, Any], directory: Optional[Path] = None) -> Path:
    """Writes a DB-independent preference profile to the shared profiles directory."""
    clean = _clean_profile_name(name)
    target_dir = Path(directory) if directory else get_user_profiles_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {"schema": PROFILE_FILE_SCHEMA, "name": clean}
    for key in _PROFILE_KEYS:
        if key in profile:
            payload[key] = profile[key]
    path = target_dir / f"{clean}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_profile_file(path: str | Path, profile_name: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """
    Reads a profile from either a standalone profile file (`knapset-profile/1`) or any v2 preset file
    (using `profile_name`, the preset's default profile, or its first profile).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Profile file not found: '{p}'")
    if p.suffix.lower() == ".toml":
        cfg = load_preset(p)
        key, data = cfg.resolve_profile(profile_name)
        return key, dict(data)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("schema") == PROFILE_FILE_SCHEMA:
        data = {k: raw[k] for k in _PROFILE_KEYS if k in raw}
        return _clean_profile_name(raw.get("name") or p.stem), data
    cfg = load_preset(p)
    key, data = cfg.resolve_profile(profile_name)
    return key, dict(data)


def list_shared_profiles(directory: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Returns {name: profile_dict} for every valid profile in the shared profiles directory."""
    d = Path(directory) if directory else get_user_profiles_dir()
    out: Dict[str, Dict[str, Any]] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            name, data = read_profile_file(f)
        except Exception:
            continue
        out.setdefault(name, data)
    return out


def resolve_shared_profile(ref: str, profile_name: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Resolves a saved preference profile from a file path or the shared profiles library."""
    p = Path(ref).expanduser()
    if p.is_file():
        return read_profile_file(p, profile_name)
    lib = list_shared_profiles()
    key = ref.strip().lower()
    if key not in lib:
        avail = ", ".join(lib) or "none saved yet"
        raise KeyError(f"Saved profile '{ref}' not found in {get_user_profiles_dir()} (available: {avail}).")
    return key, dict(lib[key])


def delete_shared_profile(name: str, directory: Optional[Path] = None) -> Path:
    """Deletes a saved preference profile from the shared profiles directory."""
    target_dir = Path(directory) if directory else get_user_profiles_dir()
    lib = list_shared_profiles(directory=target_dir)
    key = name.strip().lower()
    if key not in lib:
        avail = ", ".join(lib) or "none"
        raise KeyError(f"Saved profile '{name}' not found (available: {avail}).")
    path = target_dir / f"{_clean_profile_name(key)}.json"
    if not path.is_file():
        raise FileNotFoundError(f"'{name}' is not a standalone file in {target_dir}.")
    path.unlink()
    return path


def collect_pool_columns(slot_pools: Dict[str, List[Dict[str, Any]]]) -> Tuple[List[str], List[str]]:
    """Collects available column names (including `ref.*` and `slot.col`) and slot names from `slot_pools`."""
    cols: set[str] = set()
    slot_names = list(slot_pools.keys())
    for sname, rows in slot_pools.items():
        for row in rows[:50]:
            for k, v in row.items():
                cols.add(str(k))
                cols.add(f"{sname}.{k}")
                if isinstance(v, dict):
                    for sub_k in v.keys():
                        cols.add(f"{k}.{sub_k}")
                        cols.add(f"{sname}.{k}.{sub_k}")
    return sorted(cols), slot_names


def filter_profile_for_columns(
    data: Dict[str, Any],
    available_columns: Sequence[str],
    slot_names: Sequence[str] = (),
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Filters a profile dict to keep only formulas, filters, limits, rules, and weights
    whose column references exist in `available_columns`. Returns `(filtered_profile, skipped)`.
    """
    known = set(available_columns)
    known.update(c.split(".", 1)[1] for c in available_columns if "." in c)
    slot_names_set = set(slot_names)
    skipped: List[str] = []

    def _unknown_refs_in_expr(expr_text: str) -> List[str]:
        try:
            compiled = SafeExpression(expr_text)
        except Exception:
            return []
        missing: List[str] = []
        for node in ast.walk(compiled.tree):
            if isinstance(node, ast.Attribute):
                chain = _extract_attr_chain(node)
                dotted = ".".join(chain)
                if chain[0] in slot_names_set:
                    if len(chain) >= 2 and dotted not in known:
                        missing.append(dotted)
                elif dotted not in known and chain[0] not in _SAFE_FUNCS:
                    missing.append(dotted)
        for root in compiled.roots_referenced:
            if root not in slot_names_set and root not in known and root not in _SAFE_FUNCS:
                missing.append(root)
        return missing

    kept_formulas: List[str] = []
    for f_str in data.get("formulas") or []:
        fs = str(f_str).strip()
        if "=" in fs:
            fname, rhs = fs.split("=", 1)
            if _unknown_refs_in_expr(rhs):
                skipped.append(fname.strip())
                continue
            known.add(fname.strip())
            kept_formulas.append(fs)

    def _keep_exprs(raw_list: Sequence[Any], is_limit: bool = False) -> List[str]:
        kept: List[str] = []
        for item in raw_list:
            text = str(item).strip()
            check_expr = text
            if is_limit:
                try:
                    check_expr = LimitSpec.parse(text).inner_expr or ""
                except Exception:
                    pass
            missing = _unknown_refs_in_expr(check_expr) if check_expr else []
            if missing:
                skipped.extend(m for m in missing if m not in skipped)
                continue
            kept.append(text)
        return kept

    weights: Dict[str, float] = {}
    for expr, w in (data.get("weights") or {}).items():
        e = str(expr).strip()
        if (re.fullmatch(r"[A-Za-z_][\w.]*", e) and e not in known) or _unknown_refs_in_expr(e):
            if e not in skipped:
                skipped.append(e)
            continue
        weights[e] = float(w)

    prof = dict(data)
    prof["formulas"] = kept_formulas
    prof["where"] = _keep_exprs(data.get("where") or [])
    prof["limits"] = _keep_exprs(data.get("limits") or [], is_limit=True)
    prof["rules"] = _keep_exprs(data.get("rules") or [])
    prof["weights"] = weights
    return prof, skipped


def apply_profile_defaults(
    profile: Dict[str, Any],
    *,
    mode: Optional[str] = None,
    norm: Optional[str] = None,
    diverse: Optional[bool] = None,
    weights: Any = None,
    formulas: Any = None,
    where: Any = None,
    limits: Any = None,
    rules: Any = None,
) -> Dict[str, Any]:
    """Merges saved profile defaults with explicit request arguments (explicit values win)."""
    return {
        "mode": mode or profile.get("mode"),
        "norm": norm or profile.get("norm"),
        "diverse": profile.get("diverse", False) if diverse is None else bool(diverse),
        "weights": weights if weights else (profile.get("weights") or {}),
        "formulas": list(formulas) if formulas else list(profile.get("formulas") or []),
        "where": list(where) if where else list(profile.get("where") or []),
        "limits": list(limits) if limits else list(profile.get("limits") or []),
        "rules": list(rules) if rules else list(profile.get("rules") or []),
    }


def resolve_prefs_for_pools(
    args: Dict[str, Any],
    slot_pools: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Dict[str, Any], List[str]]:
    """Resolves `args['prefs']` against `slot_pools`, filtering missing columns and applying explicit overrides."""
    prefs_ref = args.get("prefs")
    if not prefs_ref:
        return {
            "mode": args.get("mode"),
            "norm": args.get("norm"),
            "diverse": bool(args.get("diverse", False)),
            "weights": args.get("weights") or "",
            "formulas": args.get("formulas") or [],
            "where": args.get("where") or [],
            "limits": args.get("limits") or [],
            "rules": args.get("rules") or [],
        }, []
    _name, raw_prof = resolve_shared_profile(str(prefs_ref))
    cols, slot_names = collect_pool_columns(slot_pools)
    filtered_prof, skipped = filter_profile_for_columns(raw_prof, cols, slot_names)
    merged = apply_profile_defaults(
        filtered_prof,
        mode=args.get("mode"),
        norm=args.get("norm"),
        diverse=args.get("diverse") if "diverse" in args else None,
        weights=args.get("weights"),
        formulas=args.get("formulas"),
        where=args.get("where"),
        limits=args.get("limits"),
        rules=args.get("rules"),
    )
    return merged, skipped


def get_presets_dirs() -> List[Path]:
    """Returns deduplicated preset search directories (cwd presets, user config, plugins/presets, bundled)."""
    dirs: List[Path] = []
    candidates = [
        Path.cwd() / "presets",
        get_user_presets_dir(),
        get_plugins_root() / "presets",
        get_config_dir() / "presets",
        get_data_dir() / "presets",
        REPO_ROOT / "presets",
    ]
    for c in candidates:
        try:
            r = c.resolve()
        except Exception:
            r = c
        if r not in dirs:
            dirs.append(r)
    return dirs


def discover_presets(extra_dirs: Optional[Sequence[Path]] = None) -> Dict[str, PresetConfig]:
    """Discovers valid v2 presets across local & installed preset directories."""
    found: Dict[str, PresetConfig] = {}
    search_dirs = list(extra_dirs or []) + get_presets_dirs()
    for d in search_dirs:
        if not d.is_dir():
            continue
        # Direct preset files (*.json, *.toml) or directory packages (<dir>/<name>/preset.json or <name>.json)
        for f in sorted(d.iterdir()):
            candidate_file: Optional[Path] = None
            if f.is_file() and f.suffix.lower() in (".json", ".toml"):
                candidate_file = f
            elif f.is_dir():
                for sub_name in ("preset.json", f"{f.name}.json", "preset.toml", f"{f.name}.toml"):
                    sub = f / sub_name
                    if sub.is_file():
                        candidate_file = sub
                        break
            if candidate_file is None:
                continue
            try:
                cfg = load_preset(candidate_file)
                if cfg.name not in found:
                    found[cfg.name] = cfg
            except Exception:
                continue
    return found


def resolve_preset(target: str) -> PresetConfig:
    """Resolves a preset either from an explicit file/dir path or by installed preset name."""
    p = Path(target).expanduser()
    if p.is_file():
        return load_preset(p)
    if p.is_dir():
        for sub_name in ("preset.json", f"{p.name}.json", "preset.toml", f"{p.name}.toml"):
            sub = p / sub_name
            if sub.is_file():
                return load_preset(sub)
        raise FileNotFoundError(f"No preset.json found inside directory '{target}'")

    discovered = discover_presets()
    norm = target.strip().lower().replace(" ", "_")
    if norm in discovered:
        return discovered[norm]
    if target.strip().lower() in discovered:
        return discovered[target.strip().lower()]
    raise FileNotFoundError(
        f"Preset '{target}' not found as a file path or installed preset. "
        f"Installed presets: {', '.join(sorted(discovered.keys())) or '(none)'}"
    )
