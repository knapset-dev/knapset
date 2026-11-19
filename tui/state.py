"""
Knapset v2 Interactive Terminal Studio (`ks tui`) — Antigravity (`agy`) style.

100% Python Standard Library (`curses`):
- Calm dark palette (zero harsh pink/red; soft cyan/green/amber accents) with rounded cards (`╭─╮│╰─╯`)
- Delete & re-pick Main DB slots and Reference DBs directly in the workspace
- Multi-line wrapped available column hints inside the formula/rule/weight composer (`Tab` inserts field)
- Save named preference profiles (`s` / `+ Save preferences`) persisted across sessions & hop between them (`Enter` / `h`)
- `/` Command Palette modal (`/open`, `/ref`, `/preset`, `/profile`, `/weight`, `/rule`, `/mode`, `/explain`, `/save`)
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from formula_engine import LimitSpec, RuleSpec, SafeExpression, filter_rows, parse_formulas, parse_weights
from preset_loader import (
    PresetConfig,
    discover_presets,
    filter_profile_for_columns,
    get_user_presets_dir,
    get_user_profiles_dir,
    list_shared_profiles,
    resolve_preset,
    resolve_shared_profile,
    save_preset,
    save_shared_profile,
)
from solver_engine import analyze_sensitivity, solve_basket, solve_rank, solve_sets
from sources.registry import open_source
from theme_config import ThemeConfig, discover_themes, load_active_theme, set_active_theme

_DATA_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".json", ".jsonl", ".ndjson"}


def _format_row_label(row: Dict[str, Any]) -> str:
    for key in ("title", "name", "model", "label", "sku", "id"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val)
    for k, v in row.items():
        if not isinstance(v, (dict, list)) and v is not None:
            return f"{k}={v}"
    return "(item)"


def _collect_columns_from_rows(rows: Sequence[Dict[str, Any]], prefix: str = "") -> List[str]:
    cols: List[str] = []
    seen = set()
    for r in rows[:25]:
        for k, v in r.items():
            if isinstance(v, dict):
                for sk in v.keys():
                    name = f"{prefix}{k}.{sk}"
                    if name not in seen:
                        seen.add(name)
                        cols.append(name)
            elif not isinstance(v, list):
                name = f"{prefix}{k}"
                if name not in seen:
                    seen.add(name)
                    cols.append(name)
    return cols


def _suggest_weights_from_slots(slot_pools: Dict[str, List[Dict[str, Any]]], mode: str) -> List[Tuple[str, float]]:
    cost_tokens = ("price", "cost", "fee", "weight", "mass", "tdp", "latency", "cl_latency", "boil", "ms")
    skip_exact = {"id", "sku", "is_bundle", "specs_complete"}
    flag_prefixes = ("is_", "has_")
    flag_names = {"buyer_protection", "olx_delivery", "modular"}
    multi = mode == "solve" and len(slot_pools) > 1
    max_weights = min(6, max(5, len(slot_pools) + 1)) if multi else 5

    slot_col_freq: Dict[str, int] = {}
    for rows in slot_pools.values():
        seen_in_slot = set()
        for r in rows[:40]:
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    seen_in_slot.add(str(k))
        for k in seen_in_slot:
            slot_col_freq[k] = slot_col_freq.get(k, 0) + 1

    def _is_cost(name: str) -> bool:
        low = name.lower()
        if any(t in low for t in cost_tokens):
            return True
        return "watt" in low and "wattage" not in low

    def _collect_slot_candidates(sname: str, rows: List[Dict[str, Any]]) -> List[Tuple[int, str, float, str]]:
        """Returns sorted `(priority, expr_key, default_weight, col_name)` candidates for one slot."""
        col_vals: Dict[str, List[float]] = {}
        col_order: List[str] = []
        nested_cols: List[Tuple[str, str]] = []
        for r in rows[:40]:
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    ks = str(k)
                    if ks not in col_vals:
                        col_vals[ks] = []
                        col_order.append(ks)
                    col_vals[ks].append(float(v))
                elif isinstance(v, dict):
                    for sk, sv in v.items():
                        if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                            pair = (str(k), str(sk))
                            if pair not in nested_cols:
                                nested_cols.append(pair)

        cands: List[Tuple[int, str, float, str]] = []
        for parent, sk in nested_cols:
            if sk.lower() in skip_exact:
                continue
            key = f"{sname}.{parent}.{sk}" if multi else f"{parent}.{sk}"
            w = -0.3 if _is_cost(sk) else 0.5
            # Prioritize attached reference performance/value fields
            cands.append((0 if not _is_cost(sk) else 2, key, w, f"{parent}.{sk}"))

        for col in col_order:
            low = col.lower()
            if low in skip_exact or low.endswith("_id"):
                continue
            vals = col_vals[col]
            uniq = set(vals)
            if len(uniq) <= 1:
                continue  # constant column has zero discrimination
            is_binary = uniq.issubset({0.0, 1.0}) or low.startswith(flag_prefixes) or low in flag_names
            shared_cost = _is_cost(col) and slot_col_freq.get(col, 0) > 1
            key = f"{sname}.{col}" if (multi and not shared_cost) else col
            w = -0.3 if _is_cost(col) else 0.5
            if low == "price":
                prio = 1
            elif _is_cost(col) and not low.startswith("price_"):
                prio = 3
            elif not is_binary and not low.startswith("price_"):
                prio = 2
            elif low.startswith("price_"):
                prio = 5  # secondary currency column (e.g. price_uah)
            else:
                prio = 4  # binary 0/1 flags last
            cands.append((prio, key, w, col))

        cands.sort(key=lambda item: item[0])
        return cands

    per_slot = {
        sname: _collect_slot_candidates(sname, rows)
        for sname, rows in slot_pools.items()
        if rows
    }

    picked: List[Tuple[str, float]] = []
    seen_exprs = set()
    has_price_family = False

    def _try_add(expr_key: str, w: float, raw_col: str) -> bool:
        nonlocal has_price_family
        low = raw_col.lower()
        if expr_key in seen_exprs:
            return False
        if low == "price" or low.startswith("price_"):
            if has_price_family:
                return False
            has_price_family = True
        seen_exprs.add(expr_key)
        picked.append((expr_key, w))
        return True

    # Pull shared `price` once upfront when present
    for sname, cands in per_slot.items():
        for prio, expr_key, w, raw_col in cands:
            if raw_col.lower() == "price":
                _try_add(expr_key, w, raw_col)
                break
        if has_price_family:
            break

    # Round-robin across slots so multi-table DBs represent every slot instead of filling from slot 1
    cursors = {sname: 0 for sname in per_slot}
    while len(picked) < max_weights:
        progressed = False
        for sname, cands in per_slot.items():
            idx = cursors[sname]
            while idx < len(cands):
                _, expr_key, w, raw_col = cands[idx]
                idx += 1
                if _try_add(expr_key, w, raw_col):
                    progressed = True
                    break
            cursors[sname] = idx
            if len(picked) >= max_weights:
                break
        if not progressed:
            break

    return picked or [("price", -0.5)]


def discover_local_data_files(directory: Optional[Path] = None) -> List[Path]:
    """Lists supported DB/CSV/JSON files in `directory` (excluding hidden noise)."""
    root = (directory or Path.cwd()).resolve()
    files: List[Path] = []
    try:
        for item in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith("."):
                continue
            if item.is_file() and item.suffix.lower() in _DATA_EXTENSIONS:
                files.append(item)
    except OSError:
        pass
    return files


@dataclass
class TuiSessionState:
    """Headless reactive state backing the `agy`-style Knapset TUI."""

    title: str = "Knapset v2 Studio"
    preset_name: str = "custom"
    mode: str = "solve"
    profile: str = "default"
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    references: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)
    weights: List[Tuple[str, float]] = field(default_factory=list)
    weight_dirs: Dict[str, str] = field(default_factory=dict)  # expr -> "max" | "min"
    deleted_weights: List[Tuple[str, float, str]] = field(default_factory=list)  # trash stack: (expr, mag, dir)
    formulas: List[str] = field(default_factory=list)
    where: List[str] = field(default_factory=list)
    limits: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    saved_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    top_n: int = 8
    norm: str = "robust"
    diverse: bool = False
    pins: Dict[str, str] = field(default_factory=dict)
    excludes: List[str] = field(default_factory=list)
    explain: bool = False
    dirty: bool = False

    slot_pools: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    available_columns: List[str] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    ref_stats: Dict[str, str] = field(default_factory=dict)
    status_message: str = "↑↓ Select · ←/→ Slide Weight · s Save · l Load Saved Prefs · h Hop · / Commands"
    available_presets: Dict[str, PresetConfig] = field(default_factory=dict)
    active_preset_cfg: Optional[PresetConfig] = None

    def refresh_discovered_presets(self) -> None:
        self.available_presets = discover_presets()

    def _set_weights_from_pairs(self, pairs: Sequence[Tuple[str, float]]) -> None:
        cleaned: List[Tuple[str, float]] = []
        self.weight_dirs = {}
        for expr, raw_w in pairs:
            key = str(expr)
            val = float(raw_w)
            self.weight_dirs[key] = "min" if val < 0 else "max"
            cleaned.append((key, round(min(1.0, abs(val)), 2)))
        self.weights = cleaned

    def _signed_weight_map(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for expr, raw_w in self.weights:
            mag = min(1.0, abs(float(raw_w)))
            if mag <= 1e-9:
                continue
            is_min = (raw_w < 0) or (self.weight_dirs.get(expr) == "min" and raw_w >= 0)
            out[expr] = round(-mag if is_min else mag, 4)
        return out

    def _snapshot_current_profile(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "weights": {
                expr: round((-min(1.0, abs(w)) if (w < 0 or self.weight_dirs.get(expr) == "min") else min(1.0, abs(w))), 2)
                for expr, w in self.weights
            },
            "formulas": list(self.formulas),
            "where": list(self.where),
            "limits": list(self.limits),
            "rules": list(self.rules),
        }

    def _apply_profile_dict(self, prof_name: str, prof_data: Dict[str, Any], cfg: Optional[PresetConfig] = None) -> None:
        self.profile = prof_name
        base_mode = cfg.mode if cfg else self.mode
        self.mode = str(prof_data.get("mode") or base_mode)
        if "formulas" in prof_data:
            self.formulas = list(prof_data.get("formulas") or [])
        elif cfg:
            if isinstance(cfg.formulas, dict):
                self.formulas = [f"{k} = {v}" for k, v in cfg.formulas.items()]
            else:
                self.formulas = list(cfg.formulas or [])

        if "where" in prof_data:
            self.where = (list(cfg.where) if cfg else []) + list(prof_data.get("where") or [])
        elif cfg:
            self.where = list(cfg.where)

        self.limits = list(prof_data.get("limits") if "limits" in prof_data else (cfg.limits if cfg else []))
        if "rules" in prof_data:
            base_rules = list(cfg.rules) if (cfg and prof_name in (cfg.profiles or {})) else []
            # Avoid duplicating base preset rules already baked into saved profile
            prof_rules = list(prof_data.get("rules") or [])
            merged_r = list(base_rules)
            for r in prof_rules:
                if r not in merged_r:
                    merged_r.append(r)
            self.rules = merged_r
        elif cfg:
            self.rules = list(cfg.rules)

        self._set_weights_from_pairs([(str(k), float(v)) for k, v in (prof_data.get("weights") or {}).items()])

    def _rebuild_columns_and_pools(self) -> None:
        ref_cache: Dict[str, List[Dict[str, Any]]] = {}
        for ref_key, r_cfg in self.references.items():
            raw_p = str(r_cfg["source"])
            p = Path(raw_p)
            if not p.is_absolute() and not p.exists():
                cand = self.base_dir / p
                if cand.exists():
                    raw_p = str(cand)
            ref_cache[ref_key] = open_source(raw_p).load_rows(table=r_cfg.get("table"), query=r_cfg.get("query"))

        pools: Dict[str, List[Dict[str, Any]]] = {}
        cols: List[str] = []
        self.ref_stats.clear()

        for slot_name, s_cfg in self.sources.items():
            raw_p = str(s_cfg["source"])
            p = Path(raw_p)
            if not p.is_absolute() and not p.exists():
                cand = self.base_dir / p
                if cand.exists():
                    raw_p = str(cand)
            rows = open_source(raw_p).load_rows(table=s_cfg.get("table"), query=s_cfg.get("query"))

            wired_refs = s_cfg.get("ref") or s_cfg.get("refs") or []
            if isinstance(wired_refs, str):
                wired_refs = [wired_refs]
            for ref_key in wired_refs:
                if ref_key not in ref_cache:
                    continue
                r_cfg = self.references[ref_key]
                match_expr = str(r_cfg.get("match") or "")
                lf, rf, inferred = parse_match_expr(match_expr)
                strat = str(r_cfg.get("strategy") or inferred or "token")
                alias = str(r_cfg.get("alias") or "ref")
                on_unmatched = str(r_cfg.get("on_unmatched") or "drop")
                enr = enrich_rows(
                    rows,
                    ref_cache[ref_key],
                    MatchSpec(left_field=lf, right_field=rf, strategy=strat, alias=alias, on_unmatched=on_unmatched),
                )
                rows = enr.rows
                self.ref_stats[ref_key] = f"{enr.matched}/{enr.total} ({enr.match_rate * 100:.0f}%)"

            slot_where = s_cfg.get("where")
            if slot_where:
                clauses = [str(w) for w in slot_where] if isinstance(slot_where, (list, tuple)) else [str(slot_where)]
                rows = filter_rows(rows, clauses)

            pools[slot_name] = rows
            prefix = f"{slot_name}." if len(self.sources) > 1 else ""
            for c in _collect_columns_from_rows(rows, prefix=prefix):
                if c not in cols:
                    cols.append(c)

        for f_str in self.formulas:
            if "=" in f_str:
                fname = f_str.split("=", 1)[0].strip()
                if fname and fname not in cols:
                    cols.append(fname)

        self.slot_pools = pools
        self.available_columns = cols

    def load_from_preset(self, preset_or_path: str, profile_name: Optional[str] = None) -> None:
        cfg = resolve_preset(preset_or_path)
        self.active_preset_cfg = cfg
        self.preset_name = cfg.name
        self.title = cfg.title
        self.base_dir = cfg.source_path.parent if cfg.source_path else Path.cwd()
        self.sources = {k: dict(v) for k, v in cfg.sources.items()}
        self.references = {k: dict(v) for k, v in cfg.references.items()}
        self.saved_profiles = {k: dict(v) for k, v in cfg.profiles.items()}

        prof_key, prof_data = cfg.resolve_profile(profile_name)
        self.deleted_weights.clear()
        self._apply_profile_dict(prof_key, prof_data, cfg=cfg)
        self.dirty = False

        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Loaded '{cfg.name}' [profile={prof_key}, mode={self.mode}]."

    def load_from_source(
        self,
        source_path: str,
        table: Optional[str] = None,
        ref_path: Optional[str] = None,
        ref_table: Optional[str] = None,
        match_expr: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> None:
        p = Path(source_path).expanduser().resolve()
        src = open_source(str(p))
        schema = src.inspect()
        self.base_dir = p.parent
        self.active_preset_cfg = None
        self.preset_name = p.stem.lower().replace(" ", "_")
        self.title = f"{p.name}"
        self.profile = "default"
        self.deleted_weights.clear()

        tbl_names = [table] if table else schema.table_names()
        eff_mode = mode or ("solve" if len(tbl_names) > 1 else "rank")
        self.mode = eff_mode
        self.sources = {}
        for tname in tbl_names:
            entry: Dict[str, Any] = {"source": str(p)}
            if len(schema.tables) > 1 or schema.kind == "sqlite":
                entry["table"] = tname
            self.sources[tname] = entry

        self.references = {}
        if ref_path:
            self.attach_reference(
                tbl_names[0] if tbl_names else "rows",
                ref_path,
                ref_table=ref_table,
                match_expr=match_expr,
                rebuild=False,
            )

        self.formulas = []
        self.where = []
        self.limits = []
        self.rules = []
        self._rebuild_columns_and_pools()
        self._set_weights_from_pairs(_suggest_weights_from_slots(self.slot_pools, self.mode))
        self.saved_profiles = {"default": self._snapshot_current_profile()}
        self.dirty = True
        self.re_solve()
        self.status_message = f"Opened '{p.name}' ({len(tbl_names)} slot(s), mode={self.mode})."

    def restore_saved_session(self, overlay_only: bool = False) -> bool:
        """
        Restores previously saved preferences for the currently opened source.

        Looks for `<user presets dir>/<preset_name>.json` (written by `s` / `save_preference_profile`)
        and only uses it when its first source points at the same file that was just opened.
        When `overlay_only` is True (explicit CLI `--ref` / `--mode`), the freshly built slots and
        references are kept and only the saved profiles + last active profile are restored.
        """
        if not self.preset_name or not self.sources:
            return False
        path = get_user_presets_dir() / f"{self.preset_name}.json"
        if not path.is_file():
            return False
        try:
            cfg = resolve_preset(str(path))
            saved_srcs = [Path(str(s.get("source", ""))).expanduser() for s in cfg.sources.values()]
            cur_srcs = [Path(str(s.get("source", ""))).expanduser() for s in self.sources.values()]
            if not saved_srcs or not cur_srcs or saved_srcs[0].resolve() != cur_srcs[0].resolve():
                return False
            if not overlay_only:
                self.load_from_preset(str(path))
                self.status_message = f"Restored saved session '{cfg.name}' [profile={self.profile}]."
                return True
            if not cfg.profiles:
                return False
            self.saved_profiles = {k: dict(v) for k, v in cfg.profiles.items()}
            prof_key, prof_data = cfg.resolve_profile(None)
            self._apply_profile_dict(prof_key, prof_data, cfg=None)
            self._rebuild_columns_and_pools()
            self.re_solve()
            self.dirty = False
            self.status_message = f"Restored saved preferences '{cfg.name}' [profile={prof_key}]."
            return True
        except Exception:
            return False

    def remove_slot(self, slot_name: str) -> None:
        """Removes a DB slot/table from the workspace and cleans orphan references."""
        if slot_name not in self.sources:
            return
        del self.sources[slot_name]
        # Prune references no longer referenced by any remaining slot
        used_refs = set()
        for scfg in self.sources.values():
            r = scfg.get("ref") or scfg.get("refs") or []
            if isinstance(r, str):
                used_refs.add(r)
            elif isinstance(r, list):
                used_refs.update(r)
        for rk in list(self.references.keys()):
            if rk not in used_refs:
                del self.references[rk]

        if len(self.sources) <= 1 and self.mode == "solve":
            self.mode = "rank"
        self.dirty = True
        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Removed DB slot '{slot_name}'."

    def attach_reference(
        self,
        slot_name: str,
        ref_path: str,
        ref_table: Optional[str] = None,
        match_expr: Optional[str] = None,
        alias: str = "ref",
        on_unmatched: str = "drop",
        rebuild: bool = True,
    ) -> None:
        if slot_name not in self.sources:
            raise KeyError(f"Unknown slot '{slot_name}'.")
        rp = str(Path(ref_path).expanduser().resolve())
        ref_rows = open_source(rp).load_rows(table=ref_table)
        if not match_expr:
            slot_cfg = self.sources[slot_name]
            slot_src = str(slot_cfg["source"])
            if not Path(slot_src).is_absolute() and not Path(slot_src).exists():
                slot_src = str(self.base_dir / slot_src)
            slot_rows = open_source(slot_src).load_rows(table=slot_cfg.get("table"))
            l_cols = [k for k in (slot_rows[0].keys() if slot_rows else []) if isinstance(slot_rows[0][k], str)]
            r_cols = [k for k in (ref_rows[0].keys() if ref_rows else []) if isinstance(ref_rows[0][k], str)]
            match_expr = f"{l_cols[0] if l_cols else 'title'}~{r_cols[0] if r_cols else 'model'}"
        ref_key = f"{slot_name}_{alias}"
        ref_entry: Dict[str, Any] = {
            "source": rp,
            "match": match_expr,
            "strategy": "regex" if "~=" in match_expr else ("exact" if "=" in match_expr and "~" not in match_expr else "token"),
            "alias": alias,
            "on_unmatched": on_unmatched,
        }
        if ref_table:
            ref_entry["table"] = ref_table
        self.references[ref_key] = ref_entry
        raw_ref = self.sources[slot_name].get("ref")
        existing_refs = list(raw_ref) if isinstance(raw_ref, list) else ([raw_ref] if raw_ref else [])
        if ref_key not in existing_refs:
            existing_refs.append(ref_key)
        self.sources[slot_name]["ref"] = existing_refs[0] if len(existing_refs) == 1 else existing_refs
        self.dirty = True
        if rebuild:
            self._rebuild_columns_and_pools()
            self.re_solve()
            cov = self.ref_stats.get(ref_key, "")
            self.status_message = f"Linked reference '{Path(rp).name}' -> {slot_name} ({cov})."

    def detach_reference(self, ref_key: str) -> None:
        """Detaches and removes a Reference DB binding."""
        if ref_key not in self.references:
            return
        del self.references[ref_key]
        for scfg in self.sources.values():
            raw = scfg.get("ref")
            if raw == ref_key:
                del scfg["ref"]
            elif isinstance(raw, list) and ref_key in raw:
                rem = [x for x in raw if x != ref_key]
                if not rem:
                    del scfg["ref"]
                elif len(rem) == 1:
                    scfg["ref"] = rem[0]
                else:
                    scfg["ref"] = rem
        self.dirty = True
        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Detached reference DB '{ref_key}'."

    def adjust_weight(self, idx: int, delta: float, auto_solve: bool = True) -> None:
        """Adjusts weight strength strictly within `[0.0, 1.0]` while preserving maximize/minimize direction."""
        if idx < 0 or idx >= len(self.weights):
            return
        expr, val = self.weights[idx]
        if val < 0 and expr not in self.weight_dirs:
            self.weight_dirs[expr] = "min"
        new_mag = round(min(1.0, max(0.0, abs(val) + delta)), 2)
        self.weights[idx] = (expr, new_mag)
        self.dirty = True
        if auto_solve:
            self.re_solve()
        direction = "↓ min" if self.weight_dirs.get(expr) == "min" else "↑ max"
        self.status_message = f"{expr}: {new_mag:.2f} ({direction}) [0.00 .. 1.00]"

    def toggle_weight_direction(self, idx: int, auto_solve: bool = True) -> None:
        """Flips a weight between ↑ maximize and ↓ minimize without changing its `[0, 1]` magnitude."""
        if idx < 0 or idx >= len(self.weights):
            return
        expr, val = self.weights[idx]
        curr_min = (val < 0) or (self.weight_dirs.get(expr) == "min")
        self.weight_dirs[expr] = "max" if curr_min else "min"
        self.weights[idx] = (expr, round(min(1.0, abs(val)), 2))
        self.dirty = True
        if auto_solve:
            self.re_solve()
        arrow = "↓ min" if self.weight_dirs[expr] == "min" else "↑ max"
        self.status_message = f"{expr}: direction flipped -> {arrow}"

    def add_or_update_weight(self, expr: str, weight: float) -> None:
        clean = expr.strip()
        parse_weights([f"{clean}:{weight}"])
        self.weight_dirs[clean] = "min" if weight < 0 else self.weight_dirs.get(clean, "max")
        if weight >= 0 and "-" not in str(weight):
            self.weight_dirs[clean] = "max"
        mag = round(min(1.0, abs(float(weight))), 2)
        for i, (e, _) in enumerate(self.weights):
            if e == clean:
                self.weights[i] = (clean, mag)
                break
        else:
            self.weights.append((clean, mag))
        self.dirty = True
        self.re_solve()
        arrow = "↓ min" if self.weight_dirs[clean] == "min" else "↑ max"
        self.status_message = f"Weight set: {clean} = {mag:.2f} ({arrow})"

    def update_weight_expression(self, idx: int, new_expr: str) -> None:
        """Renames/updates an existing weight's expression (e.g. `price` -> `price / gpu.vram_gb`), preserving strength & direction."""
        if idx < 0 or idx >= len(self.weights):
            return
        clean = new_expr.strip()
        SafeExpression(clean)
        old_expr, val = self.weights[idx]
        old_dir = self.weight_dirs.get(old_expr, "min" if val < 0 else "max")
        self.weight_dirs[clean] = old_dir
        self.weights[idx] = (clean, round(min(1.0, abs(val)), 2))
        self.dirty = True
        self.re_solve()
        self.status_message = f"Updated weight: '{old_expr}' -> '{clean}'"

    def remove_weight(self, idx: int, auto_solve: bool = True) -> None:
        if 0 <= idx < len(self.weights):
            expr, raw_v = self.weights.pop(idx)
            direction = self.weight_dirs.get(expr, "min" if raw_v < 0 else "max")
            self.deleted_weights = [item for item in self.deleted_weights if item[0] != expr]
            self.deleted_weights.append((expr, round(abs(raw_v), 2), direction))
            if len(self.deleted_weights) > 50:
                self.deleted_weights = self.deleted_weights[-50:]
            self.dirty = True
            if auto_solve:
                self.re_solve()
            self.status_message = f"Moved '{expr}' to Weight Trash (press 'u' or open Trash to restore)."

    def restore_deleted_weight(self, trash_idx: int = -1, auto_solve: bool = True) -> str:
        if not self.deleted_weights:
            self.status_message = "Weight Trash is empty."
            return ""
        idx = trash_idx if trash_idx >= 0 else (len(self.deleted_weights) + trash_idx)
        if idx < 0 or idx >= len(self.deleted_weights):
            raise IndexError("Trash index out of range.")
        expr, mag, direction = self.deleted_weights.pop(idx)
        self.weight_dirs[expr] = direction
        existing = next((i for i, (k, _) in enumerate(self.weights) if k == expr), None)
        if existing is not None:
            self.weights[existing] = (expr, round(min(1.0, abs(mag)), 2))
        else:
            self.weights.append((expr, round(min(1.0, abs(mag)), 2)))
        self.dirty = True
        if auto_solve:
            self.re_solve()
        self.status_message = f"Restored '{expr}' ({mag:.2f} {'↓ min' if direction == 'min' else '↑ max'})."
        return expr

    def restore_all_deleted_weights(self, auto_solve: bool = True) -> int:
        count = len(self.deleted_weights)
        if count == 0:
            self.status_message = "Weight Trash is empty."
            return 0
        while self.deleted_weights:
            self.restore_deleted_weight(0, auto_solve=False)
        if auto_solve:
            self.re_solve()
        self.status_message = f"Restored {count} weight(s) from Trash."
        return count

    def empty_weight_trash(self) -> int:
        count = len(self.deleted_weights)
        self.deleted_weights.clear()
        self.status_message = f"Emptied {count} weight(s) from Trash." if count else "Weight Trash is empty."
        return count

    def add_formula(self, formula_expr: str) -> None:
        parse_formulas([formula_expr])
        self.formulas.append(formula_expr.strip())
        self.dirty = True
        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Added formula: {formula_expr.strip()}"

    def add_limit(self, limit_expr: str) -> None:
        LimitSpec.parse(limit_expr)
        self.limits.append(limit_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added limit: {limit_expr.strip()}"

    def add_rule(self, rule_expr: str) -> None:
        RuleSpec.parse(rule_expr)
        self.rules.append(rule_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added rule: {rule_expr.strip()}"

    def add_where(self, where_expr: str) -> None:
        SafeExpression(where_expr)
        self.where.append(where_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added filter: {where_expr.strip()}"

    def save_preference_profile(self, profile_name: str, persist: bool = True, out_path: Optional[Path] = None) -> Optional[Path]:
        """
        Saves current weights, formulas, limits, rules, filters, and mode under a named
        preference profile (`profile_name`) so you can hop between preferences anytime,
        and persists the preset into `get_user_presets_dir()` by default.
        """
        clean = profile_name.strip().lower().replace(" ", "_")
        if not clean:
            raise ValueError("Preference profile name cannot be empty.")
        self.saved_profiles[clean] = self._snapshot_current_profile()
        self.profile = clean
        if not persist:
            self.status_message = f"Saved preference profile '{clean}' in session."
            return None

        target = out_path or (get_user_presets_dir() / f"{self.preset_name or 'workspace'}.json")
        saved = self.export_preset(target, preset_name=self.preset_name)
        self.refresh_discovered_presets()
        shared = save_shared_profile(clean, self.saved_profiles[clean])
        self.status_message = f"Saved '{clean}' -> {shared} (reusable on any DB with the same columns)"
        return saved

    def apply_shared_profile(self, name_or_path: str, profile_name: Optional[str] = None, store: bool = True) -> List[str]:
        """
        Applies a saved preference profile (from the shared profiles directory or any profile/preset
        file) onto the *currently open* DBs. Returns a list of weight expressions skipped because their
        column does not exist in this DB.
        """
        name, data = resolve_shared_profile(name_or_path, profile_name)
        if not self.sources:
            raise ValueError("Open a database first, then apply a saved profile.")

        prof, skipped = filter_profile_for_columns(
            data,
            available_columns=self.available_columns,
            slot_names=list(self.sources.keys()),
        )
        if prof.get("mode") == "solve" and len(self.sources) < 2:
            prof["mode"] = self.mode
        snapshot = (self.profile, self.mode, list(self.weights), dict(self.weight_dirs),
                    list(self.formulas), list(self.where), list(self.limits), list(self.rules))
        self._apply_profile_dict(name, prof, cfg=None)
        if not self.slot_pools:
            self._rebuild_columns_and_pools()
        else:
            for f_str in self.formulas:
                if "=" in f_str:
                    fname = f_str.split("=", 1)[0].strip()
                    if fname and fname not in self.available_columns:
                        self.available_columns.append(fname)
        self.status_message = ""
        self.re_solve()
        if self.status_message.startswith("Check expression"):
            (self.profile, self.mode, self.weights, self.weight_dirs,
             self.formulas, self.where, self.limits, self.rules) = snapshot
            if not self.slot_pools:
                self._rebuild_columns_and_pools()
            self.re_solve()
            raise ValueError(f"Profile '{name}' does not fit this DB (check columns / formulas).")
        self.saved_profiles[name] = self._snapshot_current_profile()
        self.dirty = True
        if store:
            save_shared_profile(name, self.saved_profiles[name])
        note = f" (skipped missing columns: {', '.join(skipped)})" if skipped else ""
        self.status_message = f"Applied saved profile '{name}'{note}."
        return skipped

    def switch_profile(self, profile_name: str, auto_solve: bool = True) -> None:
        """Hops to another saved preference profile without unloading your DBs/References."""
        if profile_name not in self.saved_profiles:
            raise KeyError(f"Profile '{profile_name}' not found.")
        self._apply_profile_dict(profile_name, self.saved_profiles[profile_name], cfg=self.active_preset_cfg)
        if not self.slot_pools:
            self._rebuild_columns_and_pools()
        else:
            for f_str in self.formulas:
                if "=" in f_str:
                    fname = f_str.split("=", 1)[0].strip()
                    if fname and fname not in self.available_columns:
                        self.available_columns.append(fname)
        if auto_solve:
            self.re_solve()
        self.status_message = f"Switched preference profile -> '{profile_name}'."

    def delete_preference_profile(self, profile_name: str, auto_solve: bool = True) -> None:
        if len(self.saved_profiles) <= 1 or profile_name not in self.saved_profiles:
            self.status_message = "Cannot delete the only active preference profile."
            return
        del self.saved_profiles[profile_name]
        if self.profile == profile_name:
            first = next(iter(self.saved_profiles.keys()))
            self.switch_profile(first, auto_solve=auto_solve)
        else:
            self.status_message = f"Removed preference profile '{profile_name}'."
        self.dirty = True

    def cycle_mode(self, auto_solve: bool = True) -> None:
        order = ("solve", "basket", "rank") if len(self.slot_pools) > 1 else ("rank", "basket")
        curr_idx = order.index(self.mode) if self.mode in order else 0
        self.mode = order[(curr_idx + 1) % len(order)]
        if auto_solve:
            self.re_solve()
        self.status_message = f"Mode switched to '{self.mode}'."

    def cycle_profile(self, auto_solve: bool = True) -> None:
        names = list(self.saved_profiles.keys())
        if len(names) <= 1:
            return
        idx = names.index(self.profile) if self.profile in names else 0
        nxt = names[(idx + 1) % len(names)]
        self.switch_profile(nxt, auto_solve=auto_solve)

    def capture_solve_inputs(self) -> Dict[str, Any]:
        """Captures an immutable-safe snapshot of solver inputs for background worker threads."""
        return {
            "mode": self.mode,
            "slot_pools": dict(self.slot_pools),
            "weight_map": self._signed_weight_map(),
            "limits": list(self.limits),
            "rules": list(self.rules),
            "formulas": list(self.formulas),
            "where": list(self.where),
            "top_n": self.top_n,
            "norm": self.norm,
            "diverse": self.diverse,
            "pins": dict(self.pins),
            "excludes": list(self.excludes),
        }

    @staticmethod
    def run_solve_snapshot(snap: Dict[str, Any], should_abort: Optional[Any] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        slot_pools = snap["slot_pools"]
        if not slot_pools:
            return [], {}
        mode = snap["mode"]
        norm = snap.get("norm", "robust")
        diverse = bool(snap.get("diverse", False))
        pins = snap.get("pins") or None
        excludes = snap.get("excludes") or None
        if mode == "rank":
            first_rows = next(iter(slot_pools.values()), [])
            ranked, meta = solve_rank(
                rows=first_rows,
                weights=snap["weight_map"],
                formulas=snap["formulas"],
                where=snap["where"],
                top_n=max(10, snap["top_n"]),
                norm=norm,
                exclude=excludes,
            )
            return [r.to_dict() for r in ranked], meta
        if mode == "basket":
            first_rows = next(iter(slot_pools.values()), [])
            pin_list = list(pins.values()) if isinstance(pins, dict) else pins
            sols, meta = solve_basket(
                rows=first_rows,
                weights=snap["weight_map"],
                limits=snap["limits"],
                formulas=snap["formulas"],
                where=snap["where"],
                top_n=snap["top_n"],
                norm=norm,
                pin=pin_list,
                exclude=excludes,
            )
            return [s.to_dict() for s in sols], meta
        sols, meta = solve_sets(
            slot_pools=slot_pools,
            weights=snap["weight_map"],
            limits=snap["limits"],
            rules=snap["rules"],
            formulas=snap["formulas"],
            where=snap["where"],
            top_n=snap["top_n"],
            should_abort=should_abort,
            norm=norm,
            diverse=diverse,
            pin=pins,
            exclude=excludes,
        )
        return [s.to_dict() for s in sols], meta

    def pin_slot(self, slot: str, needle: str) -> None:
        self.pins[slot.strip()] = needle.strip()
        self.dirty = True
        self.re_solve()
        self.status_message = f"Pinned {slot}={needle}"

    def exclude_item(self, needle: str) -> None:
        clean = needle.strip()
        if clean and clean not in self.excludes:
            self.excludes.append(clean)
            self.dirty = True
            self.re_solve()
            self.status_message = f"Excluded '{clean}'"

    def clear_pins_and_excludes(self) -> None:
        self.pins.clear()
        self.excludes.clear()
        self.re_solve()
        self.status_message = "Cleared all pins and excludes."

    def run_sensitivity(self, weight_delta: float = 0.15, limit_delta: float = 0.10) -> Dict[str, Any]:
        snap = self.capture_solve_inputs()
        pools = snap["slot_pools"]
        first_rows = next(iter(pools.values()), [])
        return analyze_sensitivity(
            snap["mode"],
            rows=first_rows if snap["mode"] in ("rank", "basket") else None,
            slot_pools=pools if snap["mode"] == "solve" else None,
            weights=snap["weight_map"],
            limits=snap["limits"],
            rules=snap["rules"],
            formulas=snap["formulas"],
            where=snap["where"],
            norm=snap["norm"],
            weight_delta=weight_delta,
            limit_delta=limit_delta,
            pin=snap["pins"] or None,
            exclude=snap["excludes"] or None,
        )

    def re_solve(self) -> None:
        try:
            results, meta = self.run_solve_snapshot(self.capture_solve_inputs())
            self.results = results
            self.meta = meta
            if not results and isinstance(meta.get("diagnostics"), dict) and meta["diagnostics"].get("summary"):
                self.status_message = f"0 results: {meta['diagnostics']['summary']}"
        except Exception as exc:
            self.status_message = f"Check expression: {exc}"

    def export_preset(self, path: str | Path, preset_name: Optional[str] = None) -> Path:
        name = (preset_name or self.preset_name or "custom_preset").strip().lower().replace(" ", "_")
        profiles_out = dict(self.saved_profiles)
        profiles_out[self.profile or "default"] = self._snapshot_current_profile()
        cfg = PresetConfig(
            schema_version="2.0",
            name=name,
            title=self.title,
            description=f"Customized interactively in Knapset v2 TUI ({self.mode})",
            mode=self.mode,
            sources=self.sources,
            references=self.references,
            formulas=self.formulas,
            where=self.where,
            limits=self.limits,
            rules=self.rules,
            profiles=profiles_out,
            default_profile=self.profile or next(iter(profiles_out.keys())),
        )
        saved = save_preset(cfg, path, overwrite=True)
        self.dirty = False
        self.status_message = f"Saved preset -> {saved}"
        return saved


# ---------------------------------------------------------------------------
# Pure formatting helpers (cleanly unit-testable without a TTY)
# ---------------------------------------------------------------------------
