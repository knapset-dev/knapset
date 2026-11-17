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
from dataclasses import dataclass, field
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from formula_engine import LimitSpec, RuleSpec, SafeExpression, parse_formulas, parse_weights
from preset_loader import PresetConfig, discover_presets, get_user_presets_dir, resolve_preset, save_preset
from solver_engine import solve_basket, solve_rank, solve_sets
from sources.registry import open_source

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
    cost_tokens = ("price", "cost", "fee", "weight", "mass", "watt", "tdp", "latency", "ms")
    picked: List[Tuple[str, float]] = []
    seen = set()
    multi = mode == "solve" and len(slot_pools) > 1
    for sname, rows in slot_pools.items():
        if not rows:
            continue
        sample = rows[0]
        for k, v in sample.items():
            if str(k).lower() in ("id", "sku") or k in seen:
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                key = f"{sname}.{k}" if multi and not any(t in k.lower() for t in cost_tokens) else str(k)
                if key in seen:
                    continue
                seen.add(key)
                w = -0.3 if any(t in k.lower() for t in cost_tokens) else 0.5
                picked.append((key, w))
                if len(picked) >= 5:
                    return picked
            elif isinstance(v, dict):
                for sk, sv in v.items():
                    if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                        key = f"{sname}.{k}.{sk}" if multi else f"{k}.{sk}"
                        if key not in seen:
                            seen.add(key)
                            picked.append((key, 0.5))
                            if len(picked) >= 5:
                                return picked
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
    formulas: List[str] = field(default_factory=list)
    where: List[str] = field(default_factory=list)
    limits: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    saved_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    top_n: int = 8
    explain: bool = False
    dirty: bool = False

    slot_pools: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    available_columns: List[str] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    ref_stats: Dict[str, str] = field(default_factory=dict)
    status_message: str = "↑↓ Select · ←/→ Slide Weight · s Save Preference · h Hop Profile · / Commands"
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

    def remove_weight(self, idx: int) -> None:
        if 0 <= idx < len(self.weights):
            removed = self.weights.pop(idx)
            self.dirty = True
            self.re_solve()
            self.status_message = f"Removed weight '{removed[0]}'."

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
        self.status_message = f"Saved preference '{clean}' -> {saved}"
        return saved

    def switch_profile(self, profile_name: str) -> None:
        """Hops to another saved preference profile without unloading your DBs/References."""
        if profile_name not in self.saved_profiles:
            raise KeyError(f"Profile '{profile_name}' not found.")
        self._apply_profile_dict(profile_name, self.saved_profiles[profile_name], cfg=self.active_preset_cfg)
        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Switched preference profile -> '{profile_name}'."

    def delete_preference_profile(self, profile_name: str) -> None:
        if len(self.saved_profiles) <= 1 or profile_name not in self.saved_profiles:
            self.status_message = "Cannot delete the only active preference profile."
            return
        del self.saved_profiles[profile_name]
        if self.profile == profile_name:
            first = next(iter(self.saved_profiles.keys()))
            self.switch_profile(first)
        else:
            self.status_message = f"Removed preference profile '{profile_name}'."
        self.dirty = True

    def cycle_mode(self) -> None:
        order = ("solve", "basket", "rank") if len(self.slot_pools) > 1 else ("rank", "basket")
        curr_idx = order.index(self.mode) if self.mode in order else 0
        self.mode = order[(curr_idx + 1) % len(order)]
        self.re_solve()
        self.status_message = f"Mode switched to '{self.mode}'."

    def cycle_profile(self) -> None:
        names = list(self.saved_profiles.keys())
        if len(names) <= 1:
            return
        idx = names.index(self.profile) if self.profile in names else 0
        nxt = names[(idx + 1) % len(names)]
        self.switch_profile(nxt)

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
        }

    @staticmethod
    def run_solve_snapshot(snap: Dict[str, Any], should_abort: Optional[Any] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        slot_pools = snap["slot_pools"]
        if not slot_pools:
            return [], {}
        mode = snap["mode"]
        if mode == "rank":
            first_rows = next(iter(slot_pools.values()), [])
            ranked, meta = solve_rank(
                rows=first_rows,
                weights=snap["weight_map"],
                formulas=snap["formulas"],
                where=snap["where"],
                top_n=max(10, snap["top_n"]),
            )
            return [r.to_dict() for r in ranked], meta
        if mode == "basket":
            first_rows = next(iter(slot_pools.values()), [])
            sols, meta = solve_basket(
                rows=first_rows,
                weights=snap["weight_map"],
                limits=snap["limits"],
                formulas=snap["formulas"],
                where=snap["where"],
                top_n=snap["top_n"],
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
        )
        return [s.to_dict() for s in sols], meta

    def re_solve(self) -> None:
        try:
            results, meta = self.run_solve_snapshot(self.capture_solve_inputs())
            self.results = results
            self.meta = meta
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


def _format_weight_bar(weight: float, width: int = 10) -> str:
    """Renders `[0, 1]` weight bar where every 10% (0.10) is 1 full `█` symbol (and 5% is `▌`)."""
    mag = max(0.0, min(1.0, abs(weight)))
    half_steps = max(0, min(width * 2, int(round(mag * width * 2))))
    full = half_steps // 2
    half = half_steps % 2
    return "█" * full + ("▌" * half) + "░" * (width - full - half)


def _compute_hold_interval(hold_steps: int) -> float:
    """Returns repeat interval (seconds) while holding a key: starts slow and caps at a low max speed."""
    if hold_steps >= 7:
        return 0.075
    if hold_steps >= 3:
        return 0.095
    return 0.125


@dataclass
class _WorkspaceItem:
    kind: str  # "header" | "profile" | "weight" | "limit" | "rule" | "formula" | "where" | "action" | "slot" | "ref"
    label: str
    index: int = -1
    key: str = ""
    action_id: str = ""
    tone: str = "normal"  # "muted" | "pos" | "min" | "accent" | "normal"


def _build_workspace_items(state: TuiSessionState) -> List[_WorkspaceItem]:
    items: List[_WorkspaceItem] = []

    items.append(_WorkspaceItem(kind="header", label="WEIGHTS 0..1 (←/→ tune · t:↑/↓ · Enter:edit · pink<1/N%)", tone="accent"))
    if state.weights:
        mags = [min(1.0, abs(w)) for _, w in state.weights]
        total_w = sum(mags)
        active_n = sum(1 for m in mags if m > 1e-9)
        eq_threshold = (1.0 / active_n) if active_n > 0 else 1.0
        for idx, ((expr, val), mag) in enumerate(zip(state.weights, mags)):
            bar = _format_weight_bar(mag, width=10)
            is_min = (val < 0) or (state.weight_dirs.get(expr) == "min")
            arrow = "↓ min" if is_min else "↑ max"
            share = (mag / total_w) if total_w > 1e-9 else 0.0
            pct = int(round(share * 100))
            if mag <= 1e-9:
                tone = "muted"
                arrow = "· off"
            elif share < eq_threshold - 1e-9:
                tone = "min"  # pink highlight when normalized share is below 1/N% of total weight
            else:
                tone = "pos"
            items.append(
                _WorkspaceItem(
                    kind="weight",
                    index=idx,
                    label=f" {expr[:12]:<12} [-][{bar}][+] {mag:.2f} {pct:>2}% {arrow}",
                    tone=tone,
                )
            )
    else:
        items.append(_WorkspaceItem(kind="action", action_id="add_weight", label="  (no weights yet — press Enter to add)", tone="muted"))
    items.append(_WorkspaceItem(kind="action", action_id="add_weight", label="  + Add Weight / Ratio Expression...", tone="accent"))

    items.append(_WorkspaceItem(kind="header", label="SAVED PREFERENCES  (Enter: hop · s: save · d: del)", tone="accent"))
    if state.saved_profiles:
        for pname in state.saved_profiles.keys():
            if pname == state.profile:
                items.append(_WorkspaceItem(kind="profile", key=pname, label=f" ★ {pname:<16} (active)", tone="pos"))
            else:
                items.append(_WorkspaceItem(kind="profile", key=pname, label=f"   {pname:<16} [Enter: switch]", tone="normal"))
    items.append(_WorkspaceItem(kind="action", action_id="save_profile", label="  + Save Current Preferences As Profile...", tone="accent"))

    items.append(_WorkspaceItem(kind="header", label="RULES, LIMITS & FORMULAS  (d: remove)", tone="accent"))
    for idx, lim in enumerate(state.limits):
        items.append(_WorkspaceItem(kind="limit", index=idx, label=f" Limit    {lim}"))
    for idx, rule in enumerate(state.rules):
        items.append(_WorkspaceItem(kind="rule", index=idx, label=f" Rule     {rule}"))
    for idx, f_str in enumerate(state.formulas):
        items.append(_WorkspaceItem(kind="formula", index=idx, label=f" Formula  {f_str}"))
    for idx, w_str in enumerate(state.where):
        items.append(_WorkspaceItem(kind="where", index=idx, label=f" Filter   {w_str}"))
    items.append(_WorkspaceItem(kind="action", action_id="add_constraint", label="  + Add Rule / Formula / Limit / Filter...", tone="accent"))

    items.append(_WorkspaceItem(kind="header", label="DATABASES & REFERENCES  (Enter: repick · d: del)", tone="accent"))
    for sname, scfg in state.sources.items():
        src_name = Path(str(scfg.get("source", ""))).name
        tbl = scfg.get("table")
        src_desc = f"{src_name}:{tbl}" if tbl else src_name
        items.append(_WorkspaceItem(kind="slot", key=sname, label=f" DB Slot  {sname:<8} <- {src_desc}", tone="normal"))
    for rk, rcfg in state.references.items():
        ref_name = Path(str(rcfg.get("source", ""))).name
        cov = f" ({state.ref_stats[rk]})" if rk in state.ref_stats else ""
        items.append(_WorkspaceItem(kind="ref", key=rk, label=f" Ref DB   {rk:<8} <- {ref_name} [{rcfg.get('match')}]{cov}", tone="normal"))
    items.append(_WorkspaceItem(kind="action", action_id="open_db", label="  + Open / Replace Main DB...", tone="accent"))
    items.append(_WorkspaceItem(kind="action", action_id="attach_ref", label="  + Attach Reference DB (--ref)...", tone="accent"))
    items.append(_WorkspaceItem(kind="action", action_id="pick_preset", label=f"  Switch Preset File ({state.preset_name})...", tone="normal"))
    return items


def _wrap_suggestion_lines(suggestions: Sequence[str], highlighted_idx: int, max_width: int, max_lines: int = 4) -> List[str]:
    """Wraps field chips cleanly across multiple lines without overflowing `max_width`."""
    lines: List[str] = []
    curr = ""
    for i, col in enumerate(suggestions):
        chip = f"[{col}]" if i == highlighted_idx else col
        candidate = f"{curr}  {chip}" if curr else chip
        if len(candidate) > max_width and curr:
            lines.append(curr)
            curr = chip
            if len(lines) >= max_lines - 1:
                if i < len(suggestions) - 1:
                    rem = len(suggestions) - i - 1
                    curr = f"{curr} (+{rem} more)"[:max_width]
                break
        else:
            curr = candidate
    if curr and len(lines) < max_lines:
        lines.append(curr[:max_width])
    return lines


def _render_result_lines(state: TuiSessionState, max_width: int) -> List[str]:
    """Formats top ranked results/baskets/sets into rounded `agy`-style summary lines."""
    lines: List[str] = []
    if not state.slot_pools:
        lines.extend(
            [
                "No database loaded yet.",
                "",
                "Quick ways to start:",
                "  • Press `o` to browse local .db / .csv / .json files",
                "  • Press `p` to pick a saved preset",
                "  • Press `/` to open the command palette",
            ]
        )
        return [ln[:max_width] for ln in lines]

    if not state.results:
        lines.extend(
            [
                "No combinations satisfy all active rules & limits.",
                "",
                "Try relaxing a Limit (e.g. budget) or removing a strict Rule.",
            ]
        )
        return [ln[:max_width] for ln in lines]

    card_w = max(24, max_width - 1)
    inner_w = card_w - 4

    if state.mode == "rank":
        for idx, r in enumerate(state.results[:12], 1):
            label = _format_row_label(r["row"])
            lines.append(f"  {idx:>2}. [{r['score']:5.1f}] {label}"[:max_width])
            if state.explain and r.get("breakdown"):
                bd = "  ·  ".join(f"{b['expression']}: +{b['contribution']:.1f}" for b in r["breakdown"])
                lines.append(f"      {bd}"[:max_width])
        return lines

    for idx, sol in enumerate(state.results, 1):
        score = sol.get("total_score", 0.0)
        lim_str = " · ".join(f"{k.split('<=')[0].strip()}={v:g}" for k, v in (sol.get("metrics") or {}).items())
        head = f" #{idx} │ Score {score:.1f} " + (f"│ {lim_str} " if lim_str else "")
        dash_fill = max(1, card_w - len(head) - 3)
        lines.append(f"╭─{head}{'─' * dash_fill}╮"[:max_width])

        if state.mode == "basket":
            for it in sol["items"]:
                q = f" x{it['count']}" if it["count"] > 1 else ""
                row_txt = f"• {_format_row_label(it['row'])}{q}  ({it['item_score']:.1f})"
                lines.append(f"│ {row_txt:<{inner_w}.{inner_w}} │"[:max_width])
        else:
            for sname, item in sol["slots"].items():
                p_hint = f"  ({item['price']})" if "price" in item else ""
                row_txt = f"{sname:<8} {_format_row_label(item)}{p_hint}"
                lines.append(f"│ {row_txt:<{inner_w}.{inner_w}} │"[:max_width])
            if state.explain and sol.get("breakdown"):
                bd = " | ".join(f"{b['expression']}:+{b['contribution']:.1f}" for b in sol["breakdown"])
                lines.append(f"│ {bd:<{inner_w}.{inner_w}} │"[:max_width])
        lines.append(f"╰{'─' * (card_w - 2)}╯"[:max_width])
    return lines


# ---------------------------------------------------------------------------
# Antigravity (`agy`) style `curses` interactive loop & floating modals
# ---------------------------------------------------------------------------


def run_curses_tui(state: TuiSessionState) -> None:
    try:
        import curses
    except ImportError as exc:
        raise RuntimeError(
            "Python `curses` is unavailable on this platform. "
            "On Windows install `windows-curses` or use `ks serve` for the interactive Web Studio."
        ) from exc

    # Sleek dark agy palette — zero glaring white A_REVERSE slabs
    C_CYAN, C_GREEN, C_MIN, C_AMBER, C_DIM, C_SEL, C_BAR = 1, 2, 3, 4, 5, 6, 7

    def _tone_attr(tone: str, selected: bool = False) -> int:
        base = {
            "accent": curses.color_pair(C_CYAN) | curses.A_BOLD,
            "pos": curses.color_pair(C_GREEN),
            "min": curses.color_pair(C_MIN),
            "muted": curses.color_pair(C_DIM) | curses.A_DIM,
        }.get(tone, curses.A_NORMAL)
        if selected:
            sel_color = {
                "pos": curses.color_pair(C_GREEN) | curses.A_BOLD,
                "min": curses.color_pair(C_MIN) | curses.A_BOLD,
            }.get(tone, curses.color_pair(C_SEL) | curses.A_BOLD)
            return sel_color | (curses.color_pair(C_SEL) if tone not in ("pos", "min") else 0)
        return base

    def _draw_box(stdscr: Any, y: int, x: int, h: int, w: int, title: str = "", active: bool = False) -> None:
        if h < 3 or w < 6:
            return
        attr = curses.color_pair(C_CYAN) if active else (curses.color_pair(C_DIM) | curses.A_DIM)
        top_t = f" {title} " if title else ""
        top_fill = max(0, w - 3 - len(top_t))
        stdscr.addnstr(y, x, f"╭─{top_t}{'─' * top_fill}╮", w, attr | (curses.A_BOLD if active else 0))
        for r in range(1, h - 1):
            stdscr.addnstr(y + r, x, "│", 1, attr)
            stdscr.addnstr(y + r, x + 1, " " * max(0, w - 2), max(0, w - 2))
            stdscr.addnstr(y + r, x + w - 1, "│", 1, attr)
        stdscr.addnstr(y + h - 1, x, f"╰{'─' * max(0, w - 2)}╯", w, attr)

    def _modal_text_input(
        stdscr: Any,
        title: str,
        prompt: str,
        initial: str = "",
        suggestions: Optional[List[str]] = None,
        examples: Optional[List[str]] = None,
        validator: Optional[Any] = None,
    ) -> Optional[str]:
        h, w = stdscr.getmaxyx()
        mw = min(max(54, int(w * 0.78)), max(42, w - 4))
        sugg = list(suggestions or [])
        ex = list(examples or [])
        inner_w = mw - 4
        wrapped_preview = _wrap_suggestion_lines(sugg, 0, inner_w, max_lines=5) if sugg else []
        mh = min(h - 2, 7 + (len(wrapped_preview) + 1 if sugg else 0) + (2 if ex else 0))
        my, mx = max(1, (h - mh) // 2), max(1, (w - mw) // 2)

        buf = list(initial)
        col_idx = 0
        stdscr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                _draw_box(stdscr, my, mx, mh, mw, title=title, active=True)
                row_y = my + 1
                stdscr.addnstr(row_y, mx + 2, prompt, inner_w, curses.A_BOLD)
                row_y += 1

                text = "".join(buf)
                shown = text[-(inner_w - 2) :] if len(text) >= inner_w - 1 else text
                stdscr.addnstr(row_y, mx + 2, f"› {shown:<{inner_w - 2}}", inner_w, curses.color_pair(C_SEL) | curses.A_BOLD)
                input_row, input_col = row_y, mx + 4 + min(len(shown), inner_w - 3)
                row_y += 1

                if validator and text.strip():
                    try:
                        validator(text.strip())
                        stdscr.addnstr(row_y, mx + 2, "✓ Valid expression", inner_w, curses.color_pair(C_GREEN))
                    except Exception as exc:
                        stdscr.addnstr(row_y, mx + 2, f"! {exc}", inner_w, curses.color_pair(C_AMBER))
                else:
                    hint_ctrl = "Enter: Confirm · Esc: Cancel" + (" · Tab: Insert selected field" if sugg else "")
                    stdscr.addnstr(row_y, mx + 2, hint_ctrl, inner_w, curses.A_DIM)
                row_y += 1

                if sugg and row_y < my + mh - 2:
                    stdscr.addnstr(row_y, mx + 2, f"Available DB fields ({len(sugg)} — ←/→ pick, Tab insert):", inner_w, curses.color_pair(C_CYAN))
                    row_y += 1
                    wlines = _wrap_suggestion_lines(sugg, col_idx, inner_w, max_lines=max(1, (my + mh - 2) - row_y))
                    for wl in wlines:
                        stdscr.addnstr(row_y, mx + 2, wl, inner_w)
                        row_y += 1
                elif not sugg and row_y < my + mh - 2:
                    stdscr.addnstr(row_y, mx + 2, "(No DB columns loaded yet — open a DB first to see field hints)", inner_w, curses.A_DIM)
                    row_y += 1

                if ex and row_y < my + mh - 1:
                    stdscr.addnstr(row_y, mx + 2, "Examples: " + "  |  ".join(ex[:2]), inner_w, curses.A_DIM)

                stdscr.move(input_row, input_col)
                stdscr.refresh()
                ch = stdscr.getch()
                if ch == 27:
                    return None
                if ch in (10, 13, curses.KEY_ENTER):
                    res = "".join(buf).strip()
                    return res or None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif ch == 9 and sugg:
                    token = sugg[col_idx % len(sugg)]
                    if buf and buf[-1].isalnum():
                        buf.append(" ")
                    buf.extend(list(token))
                elif ch in (curses.KEY_RIGHT, curses.KEY_DOWN) and sugg:
                    col_idx = (col_idx + 1) % len(sugg)
                elif ch in (curses.KEY_LEFT, curses.KEY_UP) and sugg:
                    col_idx = (col_idx - 1) % len(sugg)
                elif 32 <= ch <= 126:
                    buf.append(chr(ch))
        finally:
            curses.curs_set(0)

    def _modal_picker(stdscr: Any, title: str, choices: List[Tuple[str, str]], hint: str = "↑↓ Move · Enter Select · Esc Cancel") -> Optional[int]:
        if not choices:
            return None
        h, w = stdscr.getmaxyx()
        mw = min(max(54, int(w * 0.70)), max(36, w - 4))
        mh = min(h - 2, len(choices) + 5)
        my, mx = max(1, (h - mh) // 2), max(1, (w - mw) // 2)
        idx = 0
        stdscr.timeout(-1)
        while True:
            _draw_box(stdscr, my, mx, mh, mw, title=title, active=True)
            inner_w = mw - 4
            vis = mh - 4
            top_off = max(0, idx - vis + 1)
            for r in range(vis):
                ci = top_off + r
                if ci >= len(choices):
                    break
                name, desc = choices[ci]
                prefix = "▸ " if ci == idx else "  "
                row_txt = f"{prefix}{name:<22} {desc}" if desc else f"{prefix}{name}"
                attr = (curses.color_pair(C_SEL) | curses.A_BOLD) if ci == idx else curses.A_NORMAL
                stdscr.addnstr(my + 1 + r, mx + 2, row_txt.ljust(inner_w)[:inner_w], inner_w, attr)
            stdscr.addnstr(my + mh - 2, mx + 2, hint, inner_w, curses.A_DIM)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (27, ord("q")):
                return None
            if ch == curses.KEY_UP:
                idx = max(0, idx - 1)
            elif ch == curses.KEY_DOWN:
                idx = min(len(choices) - 1, idx + 1)
            elif ch in (10, 13, curses.KEY_ENTER):
                return idx

    def _pick_file_path(stdscr: Any, modal_title: str, start_dir: Optional[Path] = None) -> Optional[str]:
        curr_dir = (start_dir or Path.cwd()).resolve()
        while True:
            entries: List[Tuple[str, str, Optional[Path]]] = [("✎ Enter path manually...", "type any .db / .csv / .json path", None)]
            if curr_dir.parent != curr_dir:
                entries.append(("../", "parent folder", curr_dir.parent))
            try:
                children = sorted(curr_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                children = []
            for p in children:
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    entries.append((f"{p.name}/", "folder", p))
                elif p.suffix.lower() in _DATA_EXTENSIONS:
                    entries.append((p.name, f"{p.suffix.lstrip('.').upper()} dataset", p))

            pick = _modal_picker(stdscr, f"{modal_title} ({curr_dir.name})", [(a, b) for a, b, _ in entries])
            if pick is None:
                return None
            _, _, target = entries[pick]
            if target is None:
                return _modal_text_input(stdscr, modal_title, "Enter file path (.db / .csv / .tsv / .json / .jsonl):")
            if target.is_dir():
                curr_dir = target
                continue
            return str(target)

    def _action_open_db(stdscr: Any) -> None:
        chosen = _pick_file_path(stdscr, "Open Main Data Source", state.base_dir)
        if not chosen:
            return
        try:
            state.load_from_source(chosen)
        except Exception as exc:
            state.status_message = f"Could not open '{chosen}': {exc}"

    def _action_repick_slot(stdscr: Any, slot_name: str) -> None:
        if slot_name not in state.sources:
            return
        scfg = state.sources[slot_name]
        cur_src = str(scfg.get("source", ""))
        opts = [
            ("Pick a different table in DB", cur_src),
            ("Pick a different DB/CSV file for this slot", "replaces slot file"),
            ("Attach / replace Reference DB for this slot", "add benchmark / MSRP lookup"),
            ("Remove this slot", "same as pressing 'd'"),
        ]
        pick = _modal_picker(stdscr, f"Slot '{slot_name}'", opts)
        if pick is None:
            return
        if pick == 3:
            state.remove_slot(slot_name)
            return
        if pick == 2:
            _action_attach_ref(stdscr, forced_slot=slot_name)
            return
        src_path = cur_src
        if pick == 1:
            new_p = _pick_file_path(stdscr, f"Pick Data Source for '{slot_name}'", state.base_dir)
            if not new_p:
                return
            src_path = str(Path(new_p).expanduser().resolve())
        try:
            schema = open_source(src_path).inspect()
            tbl_name = scfg.get("table")
            if len(schema.tables) > 1:
                t_choices = [(t.name, f"{t.row_count} rows ({len(t.columns)} cols)") for t in schema.tables]
                t_pick = _modal_picker(stdscr, f"Select Table for Slot '{slot_name}'", t_choices)
                if t_pick is None:
                    return
                tbl_name = schema.tables[t_pick].name
            elif schema.tables:
                tbl_name = schema.tables[0].name if schema.kind == "sqlite" else None
            scfg["source"] = src_path
            if tbl_name:
                scfg["table"] = tbl_name
            else:
                scfg.pop("table", None)
            state.dirty = True
            state._rebuild_columns_and_pools()
            state.re_solve()
            state.status_message = f"Updated slot '{slot_name}' -> {Path(src_path).name}"
        except Exception as exc:
            state.status_message = f"Slot update failed: {exc}"

    def _action_attach_ref(stdscr: Any, forced_slot: Optional[str] = None, forced_ref_key: Optional[str] = None) -> None:
        if not state.sources:
            state.status_message = "Open a Main DB first before attaching a Reference DB."
            return
        slots = list(state.sources.keys())
        target_slot = forced_slot or slots[0]
        if not forced_slot and forced_ref_key:
            for sn, sc in state.sources.items():
                r = sc.get("ref")
                if r == forced_ref_key or (isinstance(r, list) and forced_ref_key in r):
                    target_slot = sn
                    break
        elif not forced_slot and len(slots) > 1:
            s_pick = _modal_picker(stdscr, "Select Slot to Enrich with Reference DB", [(s, str(state.sources[s].get("source", ""))) for s in slots])
            if s_pick is None:
                return
            target_slot = slots[s_pick]

        ref_path = _pick_file_path(stdscr, f"Attach Reference DB -> {target_slot}", state.base_dir)
        if not ref_path:
            return
        try:
            schema = open_source(ref_path).inspect()
            ref_tbl = None
            if len(schema.tables) > 1:
                t_pick = _modal_picker(
                    stdscr,
                    f"Select Table in '{Path(ref_path).name}'",
                    [(t.name, f"{t.row_count} rows") for t in schema.tables],
                )
                if t_pick is None:
                    return
                ref_tbl = schema.tables[t_pick].name
        except Exception as exc:
            state.status_message = f"Could not inspect reference DB: {exc}"
            return

        slot_cols = [c.split(".", 1)[-1] for c in state.available_columns if c.startswith(f"{target_slot}.") or "." not in c]
        match_in = _modal_text_input(
            stdscr,
            "Match Rule",
            "How to match left_col~right_col ('~' fuzzy token, '=' exact):",
            initial="title~model",
            suggestions=slot_cols[:12],
            examples=["title~model", "sku=id"],
        )
        if not match_in:
            return
        try:
            state.attach_reference(target_slot, ref_path, ref_table=ref_tbl, match_expr=match_in)
        except Exception as exc:
            state.status_message = f"Reference attach failed: {exc}"

    def _action_pick_preset(stdscr: Any) -> None:
        state.refresh_discovered_presets()
        presets = list(state.available_presets.values())
        choices = [(p.name, f"[{p.mode}] {p.title} (profiles: {', '.join(p.profile_names())})") for p in presets]
        choices.append(("✎ Load preset file path...", "open .json/.toml file"))
        pick = _modal_picker(stdscr, "Switch Preset", choices)
        if pick is None:
            return
        if pick == len(presets):
            path_in = _modal_text_input(stdscr, "Load Preset File", "Enter preset .json/.toml file path:")
            if path_in:
                try:
                    state.load_from_preset(path_in)
                except Exception as exc:
                    state.status_message = f"Preset error: {exc}"
            return
        chosen = presets[pick]
        prof_names = chosen.profile_names()
        chosen_prof = prof_names[0] if prof_names else None
        if len(prof_names) > 1:
            p_pick = _modal_picker(stdscr, f"Choose Preference Profile for '{chosen.name}'", [(n, "") for n in prof_names])
            if p_pick is not None:
                chosen_prof = prof_names[p_pick]
        try:
            state.load_from_preset(str(chosen.source_path or chosen.name), profile_name=chosen_prof)
        except Exception as exc:
            state.status_message = f"Failed loading preset: {exc}"

    def _action_save_preference(stdscr: Any) -> None:
        name_in = _modal_text_input(
            stdscr,
            "Save Preference Profile",
            "Profile name to save (hop between saved profiles anytime with Enter or 'h'):",
            initial=state.profile if state.profile and state.profile != "default" else "my_prefs",
            examples=["gaming", "budget_quiet", "max_value"],
        )
        if not name_in:
            return
        try:
            state.save_preference_profile(name_in, persist=True)
        except Exception as exc:
            state.status_message = f"Save failed: {exc}"

    def _expr_suggestions() -> List[str]:
        merged: List[str] = []
        seen = set()
        for expr, _ in state.weights:
            if expr not in seen and all(ch.isalnum() or ch in ("_", ".") for ch in expr):
                seen.add(expr)
                merged.append(expr)
        for col in state.available_columns:
            if col not in seen:
                seen.add(col)
                merged.append(col)
        return merged

    def _action_add_weight(stdscr: Any) -> None:
        expr = _modal_text_input(
            stdscr,
            "Add Preference Weight",
            "Field or expression to score (←/→ selects field below, Tab inserts it):",
            suggestions=_expr_suggestions(),
            examples=["price / gpu.vram_gb", "gpu.ref.score", "cpu.score / gpu.ref.score"],
            validator=SafeExpression,
        )
        if not expr:
            return
        w_str = _modal_text_input(
            stdscr,
            "Weight Strength (0.0 .. 1.0)",
            f"Strength for '{expr}' in [0, 1] (prefix '-' to start as ↓ minimize, e.g. 0.5 or -0.3):",
            initial="0.5",
        )
        if not w_str:
            return
        try:
            state.add_or_update_weight(expr, float(w_str))
        except Exception as exc:
            state.status_message = f"Invalid weight: {exc}"

    def _action_edit_weight(stdscr: Any, idx: int) -> None:
        if idx < 0 or idx >= len(state.weights):
            return
        old_expr, _ = state.weights[idx]
        new_expr = _modal_text_input(
            stdscr,
            "Edit Weight Expression",
            "Edit expression (e.g. turn 'price' into 'price / gpu.vram_gb' · Tab inserts field):",
            initial=old_expr,
            suggestions=_expr_suggestions(),
            examples=[f"{old_expr} / gpu.vram_gb", "gpu.price / gpu.vram_gb"],
            validator=SafeExpression,
        )
        if new_expr and new_expr != old_expr:
            try:
                state.update_weight_expression(idx, new_expr)
            except Exception as exc:
                state.status_message = f"Invalid expression: {exc}"

    def _action_add_constraint(stdscr: Any) -> None:
        kinds = [
            ("Formula  (derived computed column)", "e.g. deal_pct = (ref.msrp - price) / ref.msrp * 100"),
            ("Rule     (cross-slot compatibility)", "e.g. cpu.socket == mobo.socket or cpu.score / gpu.score < 1"),
            ("Limit    (aggregate cap / floor)", "e.g. sum(price) <= 950 or count <= 5"),
            ("Filter   (row pre-filter)", "e.g. price <= 600 and condition != 'for_parts'"),
        ]
        pick = _modal_picker(stdscr, "Create Formula, Rule, Limit, or Filter", kinds)
        if pick is None:
            return
        try:
            if pick == 0:
                val = _modal_text_input(
                    stdscr,
                    "Create Formula",
                    "Write 'new_name = expression' (Tab inserts highlighted DB field):",
                    suggestions=_expr_suggestions(),
                    examples=["val = price / gpu.vram_gb", "discount = (ref.msrp - price) / ref.msrp * 100"],
                    validator=lambda s: parse_formulas([s]),
                )
                if val:
                    state.add_formula(val)
            elif pick == 1:
                val = _modal_text_input(
                    stdscr,
                    "Add Compatibility Rule",
                    "Write boolean rule expression (Tab inserts highlighted DB field):",
                    suggestions=_expr_suggestions(),
                    examples=["cpu.socket == mobo.socket", "cpu.ref.score / gpu.ref.score < 1"],
                    validator=RuleSpec.parse,
                )
                if val:
                    state.add_rule(val)
            elif pick == 2:
                val = _modal_text_input(
                    stdscr,
                    "Add Resource Limit",
                    "Write aggregate limit (Tab inserts highlighted DB field):",
                    suggestions=_expr_suggestions(),
                    examples=["sum(price) <= 950", "count <= 4"],
                    validator=LimitSpec.parse,
                )
                if val:
                    state.add_limit(val)
            elif pick == 3:
                val = _modal_text_input(
                    stdscr,
                    "Add Row Filter (--where)",
                    "Write filter predicate (Tab inserts highlighted DB field):",
                    suggestions=_expr_suggestions(),
                    examples=["price <= 600"],
                    validator=SafeExpression,
                )
                if val:
                    state.add_where(val)
        except Exception as exc:
            state.status_message = f"Invalid expression: {exc}"

    def _action_command_palette(stdscr: Any) -> None:
        cmds = [
            ("/open", "Open / Replace Main SQLite / CSV / JSON database"),
            ("/ref", "Attach a Reference DB (--ref)"),
            ("/save-pref", "Save current preferences as named profile (persists for reuse)"),
            ("/hop", "Cycle / hop between saved preference profiles"),
            ("/preset", "Load a saved preset file or installed registry preset"),
            ("/weight", "Add a weighted column or ratio expression"),
            ("/formula", "Create a new formula using available DB fields"),
            ("/rule", "Add constraint (Rule / Limit / Filter)"),
            ("/mode", "Switch optimization mode (solve / basket / rank)"),
            ("/explain", "Toggle normalized score breakdown"),
            ("/export", "Export preset to custom .json path"),
            ("/help", "Show keyboard & mouse controls"),
        ]
        pick = _modal_picker(stdscr, "/ Command Palette", cmds)
        if pick is None:
            return
        cmd = cmds[pick][0]
        if cmd == "/open":
            _action_open_db(stdscr)
        elif cmd == "/ref":
            _action_attach_ref(stdscr)
        elif cmd == "/save-pref":
            _action_save_preference(stdscr)
        elif cmd == "/hop":
            state.cycle_profile()
        elif cmd == "/preset":
            _action_pick_preset(stdscr)
        elif cmd == "/weight":
            _action_add_weight(stdscr)
        elif cmd in ("/formula", "/rule"):
            _action_add_constraint(stdscr)
        elif cmd == "/mode":
            state.cycle_mode()
        elif cmd == "/explain":
            state.explain = not state.explain
            state.status_message = f"Explain breakdown: {'ON' if state.explain else 'OFF'}"
        elif cmd == "/export":
            out_p = _modal_text_input(stdscr, "Export Preset JSON", "File path (.json):", initial=f"{state.preset_name}.knapset.json")
            if out_p:
                try:
                    state.export_preset(out_p)
                except Exception as exc:
                    state.status_message = f"Export failed: {exc}"
        elif cmd == "/help":
            _modal_picker(
                stdscr,
                "Knapset v2 TUI Controls",
                [
                    ("↑ / ↓", "Move selection in active pane"),
                    ("← / → or - / +", "Adjust weight (-0.10 / +0.10; [ / ] for ±0.05)"),
                    ("Enter", "Hop profile / Repick DB slot / Open '+ Add' composer"),
                    ("d / Backspace", "Delete weight, profile, formula, limit, rule, DB slot, or Ref DB"),
                    ("s / h", "Save current preferences as profile / Hop to next profile"),
                    ("o / p / m / e", "Open Main DB / Preset picker / Cycle Mode / Explain toggle"),
                    ("Tab / /", "Switch Config<->Results pane / Open Command Palette"),
                    ("q", "Quit TUI"),
                ],
                hint="Press Esc or Enter to close",
            )

    def _run_welcome_if_empty(stdscr: Any) -> None:
        if state.slot_pools:
            return
        state.refresh_discovered_presets()
        local_files = discover_local_data_files()
        choices: List[Tuple[str, str, Tuple[str, str]]] = []
        for p in state.available_presets.values():
            choices.append((f"Preset: {p.name}", f"[{p.mode}] {p.title}", ("preset", str(p.source_path or p.name))))
        for f in local_files[:8]:
            choices.append((f"File: {f.name}", f"{f.suffix.lstrip('.').upper()} in current folder", ("file", str(f))))
        choices.append(("Browse folders...", "choose any .db / .csv / .json file", ("browse", "")))
        if not choices:
            return
        pick = _modal_picker(
            stdscr,
            "◈ Welcome to Knapset v2 — Pick a DB or Saved Preset",
            [(a, b) for a, b, _ in choices],
            hint="↑↓ Choose · Enter Open · Esc Empty Workspace",
        )
        if pick is None:
            return
        kind, val = choices[pick][2]
        if kind == "preset":
            try:
                state.load_from_preset(val)
            except Exception as exc:
                state.status_message = f"Preset error: {exc}"
        elif kind == "file":
            try:
                state.load_from_source(val)
            except Exception as exc:
                state.status_message = f"Open error: {exc}"
        else:
            _action_open_db(stdscr)

    def _loop(stdscr: Any) -> None:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        curses.curs_set(0)
        stdscr.keypad(True)
        try:
            wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
            wheel_dn = getattr(curses, "BUTTON5_PRESSED", 0)
            curses.mousemask(curses.BUTTON1_PRESSED | wheel_up | wheel_dn)
            if hasattr(curses, "mouseinterval"):
                curses.mouseinterval(0)
        except Exception:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            dark_sel_bg = 236 if getattr(curses, "COLORS", 0) >= 256 else -1
            dark_bar_bg = 235 if getattr(curses, "COLORS", 0) >= 256 else -1
            curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
            curses.init_pair(C_MIN, curses.COLOR_MAGENTA, -1)
            curses.init_pair(C_AMBER, curses.COLOR_YELLOW, -1)
            curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
            curses.init_pair(C_SEL, curses.COLOR_CYAN, dark_sel_bg)
            curses.init_pair(C_BAR, curses.COLOR_WHITE, dark_bar_bg)

        _run_welcome_if_empty(stdscr)

        focus_results = False
        left_cursor = 1
        left_scroll = 0
        right_scroll = 0
        solve_pending = False
        solve_due_at = 0.0
        solve_gen = 0
        worker_running = False
        DEBOUNCE_SEC = 0.25
        result_queue: queue.SimpleQueue = queue.SimpleQueue()
        needs_redraw = True
        force_clear = True
        cached_res_key: Any = None
        cached_res_lines: List[str] = []
        last_dims: Tuple[int, int] = (0, 0)
        last_step_idx = -1
        last_step_sign = 0
        last_event_at = 0.0
        last_apply_at = 0.0
        fast_events = 0
        hold_steps = 0

        def _nudge_weight(idx: int, sign: int, allow_accel: bool = True) -> None:
            nonlocal last_step_idx, last_step_sign, last_event_at, last_apply_at, fast_events, hold_steps
            now = time.monotonic()
            same_repeat = allow_accel and idx == last_step_idx and sign == last_step_sign and (now - last_event_at) <= 0.085
            last_step_idx = idx
            last_step_sign = sign
            last_event_at = now
            if same_repeat:
                fast_events += 1
                if fast_events >= 2 and (now - last_apply_at) < _compute_hold_interval(hold_steps):
                    return
                if fast_events >= 2:
                    hold_steps += 1
            else:
                fast_events = 0
                hold_steps = 0
            last_apply_at = now
            state.adjust_weight(idx, sign * 0.05, auto_solve=False)
            _schedule_solve()

        def _schedule_solve() -> None:
            nonlocal solve_pending, solve_due_at, solve_gen, needs_redraw
            solve_gen += 1
            solve_pending = True
            solve_due_at = time.monotonic() + DEBOUNCE_SEC
            needs_redraw = True

        def _start_async_solve() -> None:
            nonlocal worker_running, solve_pending, needs_redraw
            if worker_running:
                return
            solve_pending = False
            worker_running = True
            gen = solve_gen
            snap = state.capture_solve_inputs()
            needs_redraw = True

            def _worker() -> None:
                try:
                    res, meta = TuiSessionState.run_solve_snapshot(snap, should_abort=lambda: solve_gen != gen)
                    result_queue.put((gen, res, meta, None))
                except Exception as exc:
                    result_queue.put((gen, None, None, str(exc)))

            threading.Thread(target=_worker, daemon=True).start()

        def _flush_pending_solve() -> None:
            nonlocal solve_pending, worker_running, solve_gen, needs_redraw
            if solve_pending or worker_running:
                solve_gen += 1
                solve_pending = False
                worker_running = False
                while not result_queue.empty():
                    try:
                        result_queue.get_nowait()
                    except Exception:
                        break
                state.re_solve()
                needs_redraw = True

        while True:
            # Pop completed background solve results cleanly without blocking UI
            while not result_queue.empty():
                try:
                    done_gen, res, meta, err = result_queue.get_nowait()
                except Exception:
                    break
                worker_running = False
                if done_gen == solve_gen:
                    if err is not None:
                        state.status_message = f"Check expression: {err}"
                    else:
                        state.results = res or []
                        state.meta = meta or {}
                    needs_redraw = True
                elif not solve_pending:
                    # A newer solve_gen arrived while previous worker ran; run the latest snapshot now
                    solve_pending = True
                    solve_due_at = time.monotonic()

            if solve_pending and not worker_running and time.monotonic() >= solve_due_at:
                _start_async_solve()

            if needs_redraw:
                needs_redraw = False
                h, w = stdscr.getmaxyx()
                if (h, w) != last_dims:
                    force_clear = True
                    last_dims = (h, w)
                if force_clear:
                    stdscr.erase()
                    force_clear = False
                left_w = max(54 if w >= 88 else 38, min(64, int(w * 0.48)))
                right_x = left_w + 1
                right_w = max(24, w - right_x)

                dirty = "*" if state.dirty else ""
                if solve_pending:
                    sync_tag = " │ ⏳ pending..."
                elif worker_running:
                    sync_tag = " │ ⏳ solving..."
                else:
                    sync_tag = ""
                hdr_left = f" ◈ knapset v2 │ [{state.mode}] │ {state.preset_name}{dirty} │ pref:{state.profile}{sync_tag}"
                hdr_right = f"{len(state.results)} result(s) │ / Cmds │ ? Help "
                stdscr.addnstr(0, 0, hdr_left.ljust(max(0, w - 1)), w - 1, curses.color_pair(C_CYAN) | curses.A_BOLD)
                if w > len(hdr_left) + len(hdr_right) + 2:
                    stdscr.addnstr(0, w - len(hdr_right) - 1, hdr_right, len(hdr_right), curses.A_DIM)

                pane_y = 1
                pane_h = max(6, h - 3)
                inner_h = pane_h - 2

                _draw_box(stdscr, pane_y, 0, pane_h, left_w, title="Workspace Controls", active=not focus_results)
                ws_items = _build_workspace_items(state)
                left_cursor = max(0, min(left_cursor, max(0, len(ws_items) - 1)))
                if ws_items and ws_items[left_cursor].kind == "header" and left_cursor + 1 < len(ws_items):
                    left_cursor += 1
                if left_cursor < left_scroll:
                    left_scroll = left_cursor
                elif left_cursor >= left_scroll + inner_h:
                    left_scroll = left_cursor - inner_h + 1

                for r in range(inner_h):
                    idx = left_scroll + r
                    if idx >= len(ws_items):
                        stdscr.addnstr(pane_y + 1 + r, 2, " " * max(0, left_w - 3), max(0, left_w - 3))
                        continue
                    it = ws_items[idx]
                    selected = (not focus_results) and (idx == left_cursor)
                    marker = "▸" if selected else " "
                    raw_lbl = it.label[1:] if it.label.startswith(" ") else it.label
                    attr = _tone_attr(it.tone, selected=selected)
                    if selected and getattr(curses, "COLORS", 0) >= 256:
                        attr = curses.color_pair(C_SEL) | curses.A_BOLD
                    line_text = f"{marker}{raw_lbl}".ljust(left_w - 3)[: left_w - 3]
                    stdscr.addnstr(pane_y + 1 + r, 2, line_text, left_w - 3, attr)

                ex_tag = " · Explain ON" if state.explain else ""
                _draw_box(stdscr, pane_y, right_x, pane_h, right_w, title=f"Top Solutions ({state.mode}){ex_tag}", active=focus_results)
                res_key = (id(state.results), len(state.results), right_w, state.explain, state.mode)
                if res_key != cached_res_key:
                    cached_res_lines = _render_result_lines(state, right_w - 3)
                    cached_res_key = res_key
                max_r_scroll = max(0, len(cached_res_lines) - inner_h)
                right_scroll = max(0, min(right_scroll, max_r_scroll))
                for r in range(inner_h):
                    r_idx = right_scroll + r
                    if r_idx >= len(cached_res_lines):
                        stdscr.addnstr(pane_y + 1 + r, right_x + 2, " " * max(0, right_w - 3), max(0, right_w - 3))
                        continue
                    txt = cached_res_lines[r_idx].ljust(max(0, right_w - 3))[: max(0, right_w - 3)]
                    attr = curses.color_pair(C_GREEN) if cached_res_lines[r_idx].startswith("╭─") else (curses.A_DIM if cached_res_lines[r_idx].startswith("╰") else curses.A_NORMAL)
                    stdscr.addnstr(pane_y + 1 + r, right_x + 2, txt, right_w - 3, attr)

                stdscr.addnstr(h - 2, 1, state.status_message.ljust(max(0, w - 2))[: max(0, w - 2)], w - 2, curses.color_pair(C_AMBER) if state.status_message.startswith("Check ") else curses.A_DIM)
                footer = " ↑↓:Nav  ←/→ or click [-]/[+]:Weight  t:Dir  Enter:Edit/Hop  d:Del  s:Save  /:Cmds  q:Quit "
                stdscr.addnstr(h - 1, 0, footer.ljust(w)[: w - 1], w - 1, curses.color_pair(C_BAR) | curses.A_DIM)
                stdscr.refresh()

            stdscr.timeout(40 if (solve_pending or worker_running) else -1)
            ch = stdscr.getch()
            if ch == curses.ERR:
                continue
            needs_redraw = True
            if ch == curses.KEY_RESIZE:
                force_clear = True
                continue
            if ch in (ord("q"), ord("Q")):
                break
            if ch == 9:
                focus_results = not focus_results
                continue
            if ch in (ord("/"), ord("?")):
                _flush_pending_solve()
                _action_command_palette(stdscr)
                force_clear = True
                continue
            if ch in (ord("o"), ord("O")):
                _flush_pending_solve()
                _action_open_db(stdscr)
                force_clear = True
                continue
            if ch in (ord("p"), ord("P")):
                _flush_pending_solve()
                _action_pick_preset(stdscr)
                force_clear = True
                continue
            if ch in (ord("h"), ord("H")):
                solve_gen += 1
                solve_pending = False
                worker_running = False
                state.cycle_profile()
                continue
            if ch in (ord("m"), ord("M")):
                solve_gen += 1
                solve_pending = False
                worker_running = False
                state.cycle_mode()
                continue
            if ch in (ord("e"), ord("E")):
                state.explain = not state.explain
                state.status_message = f"Explain breakdown: {'ON' if state.explain else 'OFF'}"
                continue
            if ch in (ord("s"), ord("S")):
                _flush_pending_solve()
                _action_save_preference(stdscr)
                force_clear = True
                continue

            if ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
                    wheel_dn = getattr(curses, "BUTTON5_PRESSED", 0)
                    if wheel_up and (bstate & wheel_up):
                        if focus_results or mx >= right_x:
                            right_scroll = max(0, right_scroll - 2)
                        else:
                            nxt = max(1, left_cursor - 1)
                            while nxt > 1 and ws_items[nxt].kind == "header":
                                nxt -= 1
                            if ws_items[nxt].kind != "header":
                                left_cursor = nxt
                        continue
                    if wheel_dn and (bstate & wheel_dn):
                        if focus_results or mx >= right_x:
                            right_scroll = min(max_r_scroll, right_scroll + 2)
                        else:
                            nxt = min(len(ws_items) - 1, left_cursor + 1)
                            while nxt < len(ws_items) - 1 and ws_items[nxt].kind == "header":
                                nxt += 1
                            if ws_items[nxt].kind != "header":
                                left_cursor = nxt
                        continue

                    if pane_y < my < pane_y + pane_h - 1 and 1 <= mx < left_w - 1:
                        focus_results = False
                        clicked_idx = left_scroll + (my - pane_y - 1)
                        if 0 <= clicked_idx < len(ws_items) and ws_items[clicked_idx].kind != "header":
                            left_cursor = clicked_idx
                            it = ws_items[left_cursor]
                            if it.kind == "weight":
                                if 15 <= mx <= 18:
                                    _nudge_weight(it.index, -1, allow_accel=False)
                                elif 19 <= mx <= 30:
                                    # Click directly along 10-symbol [██████████] bar in 5% steps (0.00 .. 1.00)
                                    frac = max(0.0, min(1.0, (mx - 19) / 10.0))
                                    val_click = round(round(frac * 20) / 20.0, 2)
                                    w_expr, _ = state.weights[it.index]
                                    state.weights[it.index] = (w_expr, val_click)
                                    state.dirty = True
                                    state.status_message = f"{w_expr}: {val_click:.2f}"
                                    _schedule_solve()
                                elif 31 <= mx <= 34:
                                    _nudge_weight(it.index, 1, allow_accel=False)
                                elif mx >= 43:
                                    state.toggle_weight_direction(it.index, auto_solve=False)
                                    _schedule_solve()
                            elif it.kind in ("action", "profile", "slot", "ref"):
                                ch = 10
                    elif pane_y < my < pane_y + pane_h - 1 and mx >= right_x:
                        focus_results = True
                except Exception:
                    pass
                if ch != 10:
                    continue

            if focus_results:
                if ch == curses.KEY_UP:
                    right_scroll = max(0, right_scroll - 1)
                elif ch == curses.KEY_DOWN:
                    right_scroll = min(max_r_scroll, right_scroll + 1)
                elif ch == curses.KEY_PPAGE:
                    right_scroll = max(0, right_scroll - max(3, inner_h // 2))
                elif ch == curses.KEY_NPAGE:
                    right_scroll = min(max_r_scroll, right_scroll + max(3, inner_h // 2))
                continue

            if ch == curses.KEY_UP:
                nxt = left_cursor - 1
                while nxt > 0 and ws_items[nxt].kind == "header":
                    nxt -= 1
                if nxt >= 0 and ws_items[nxt].kind != "header":
                    left_cursor = nxt
                if solve_pending:
                    solve_due_at = time.monotonic() + DEBOUNCE_SEC
            elif ch == curses.KEY_DOWN:
                nxt = left_cursor + 1
                while nxt < len(ws_items) and ws_items[nxt].kind == "header":
                    nxt += 1
                if nxt < len(ws_items):
                    left_cursor = nxt
                if solve_pending:
                    solve_due_at = time.monotonic() + DEBOUNCE_SEC
            elif ch in (ord("+"), ord("="), curses.KEY_RIGHT):
                if ws_items[left_cursor].kind == "weight":
                    _nudge_weight(ws_items[left_cursor].index, 1, allow_accel=True)
            elif ch in (ord("-"), ord("_"), curses.KEY_LEFT):
                if ws_items[left_cursor].kind == "weight":
                    _nudge_weight(ws_items[left_cursor].index, -1, allow_accel=True)
            elif ch == ord("]"):
                if ws_items[left_cursor].kind == "weight":
                    _nudge_weight(ws_items[left_cursor].index, 1, allow_accel=False)
            elif ch == ord("["):
                if ws_items[left_cursor].kind == "weight":
                    _nudge_weight(ws_items[left_cursor].index, -1, allow_accel=False)
            elif ch in (ord("t"), ord("T"), ord(" ")):
                if ws_items[left_cursor].kind == "weight":
                    state.toggle_weight_direction(ws_items[left_cursor].index, auto_solve=False)
                    _schedule_solve()
            elif ch in (ord("d"), ord("D"), curses.KEY_DC, curses.KEY_BACKSPACE, 127):
                solve_gen += 1
                solve_pending = False
                worker_running = False
                cur = ws_items[left_cursor]
                if cur.kind == "weight":
                    state.remove_weight(cur.index)
                elif cur.kind == "profile":
                    state.delete_preference_profile(cur.key)
                elif cur.kind == "slot":
                    state.remove_slot(cur.key)
                elif cur.kind == "ref":
                    state.detach_reference(cur.key)
                elif cur.kind == "limit" and 0 <= cur.index < len(state.limits):
                    state.limits.pop(cur.index)
                    state.re_solve()
                elif cur.kind == "rule" and 0 <= cur.index < len(state.rules):
                    state.rules.pop(cur.index)
                    state.re_solve()
                elif cur.kind == "formula" and 0 <= cur.index < len(state.formulas):
                    state.formulas.pop(cur.index)
                    state._rebuild_columns_and_pools()
                    state.re_solve()
                elif cur.kind == "where" and 0 <= cur.index < len(state.where):
                    state.where.pop(cur.index)
                    state.re_solve()
            elif ch in (ord("a"), ord("A")):
                solve_gen += 1
                solve_pending = False
                worker_running = False
                cur = ws_items[left_cursor]
                if cur.kind in ("limit", "rule", "formula", "where") or cur.action_id == "add_constraint":
                    _action_add_constraint(stdscr)
                else:
                    _action_add_weight(stdscr)
                force_clear = True
            elif ch in (10, 13, curses.KEY_ENTER):
                solve_gen += 1
                solve_pending = False
                worker_running = False
                cur = ws_items[left_cursor]
                if cur.action_id == "add_weight":
                    _action_add_weight(stdscr)
                    force_clear = True
                elif cur.action_id == "save_profile":
                    _action_save_preference(stdscr)
                    force_clear = True
                elif cur.action_id == "add_constraint":
                    _action_add_constraint(stdscr)
                    force_clear = True
                elif cur.action_id == "open_db":
                    _action_open_db(stdscr)
                    force_clear = True
                elif cur.action_id == "attach_ref":
                    _action_attach_ref(stdscr)
                    force_clear = True
                elif cur.action_id == "pick_preset":
                    _action_pick_preset(stdscr)
                    force_clear = True
                elif cur.kind == "profile":
                    state.switch_profile(cur.key)
                elif cur.kind == "slot":
                    _action_repick_slot(stdscr, cur.key)
                    force_clear = True
                elif cur.kind == "ref":
                    _action_attach_ref(stdscr, forced_ref_key=cur.key)
                    force_clear = True
                elif cur.kind == "weight":
                    _action_edit_weight(stdscr, cur.index)
                    force_clear = True

    curses.wrapper(_loop)


def run_tui(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} tui",
        description="Launch interactive Knapset v2 Terminal Studio (agy-style curses TUI).",
    )
    parser.add_argument("target", nargs="?", default=None, help="Optional database file (.db/.csv/.json) or preset to pre-load")
    parser.add_argument("--preset", "-P", default=None, help="Preset file path or installed preset name")
    parser.add_argument("--profile", default=None, help="Initial preset profile")
    parser.add_argument("--ref", default=None, help="Optional reference DB to attach")
    parser.add_argument("--match", default=None, help="Match expression for --ref")
    parser.add_argument("--mode", choices=["solve", "basket", "rank"], default=None, help="Initial solver mode")
    args = parser.parse_args(argv)

    state = TuiSessionState()
    state.refresh_discovered_presets()

    if args.preset:
        state.load_from_preset(args.preset, profile_name=args.profile)
    elif args.target:
        p = Path(args.target).expanduser()
        if p.suffix.lower() in (".json", ".toml") and p.is_file():
            try:
                state.load_from_preset(str(p), profile_name=args.profile)
            except Exception:
                state.load_from_source(str(p), ref_path=args.ref, match_expr=args.match, mode=args.mode)
        else:
            state.load_from_source(args.target, ref_path=args.ref, match_expr=args.match, mode=args.mode)

    run_curses_tui(state)
