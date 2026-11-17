"""
Knapset v2 Interactive Terminal UI (`ks tui`) using Python Standard Library `curses`.

Features:
- Load existing SQLite/CSV/JSON sources OR any installed/local v2 Preset
- Attach and configure Reference DBs (--ref) with live match coverage preview
- Browse discovered columns across all slots and references (`cpu.*`, `gpu.ref.*`)
- Live `+`/`-` weight adjustments with instant re-solving
- Interactive editor for custom formulas (`val = ref.score / price`), limits (`sum(price) <= 950`),
  filters (`vram >= 12`), and cross-slot rules (`cpu.score / gpu.score < 1`)
- Switch optimization modes (`solve` / `basket` / `rank`), toggle `--explain`, and save preset `.json`
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from formula_engine import parse_formulas, parse_weights
from preset_loader import PresetConfig, discover_presets, resolve_preset, save_preset
from solver_engine import solve_basket, solve_rank, solve_sets
from sources.registry import open_source


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


@dataclass
class TuiSessionState:
    """Headless reactive state backing the `curses` TUI (independently unit-testable)."""

    title: str = "Knapset v2 Interactive Studio"
    preset_name: str = "custom"
    mode: str = "solve"
    profile: str = "custom"
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    references: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    base_dir: Path = field(default_factory=Path.cwd)
    weights: List[Tuple[str, float]] = field(default_factory=list)
    formulas: List[str] = field(default_factory=list)
    where: List[str] = field(default_factory=list)
    limits: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    top_n: int = 8
    explain: bool = False
    dirty: bool = False

    slot_pools: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    available_columns: List[str] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    ref_stats: Dict[str, str] = field(default_factory=dict)
    status_message: str = "Ready. Use +/- on weights, 'a' to add formula/rule/limit, 's' to save preset."
    available_presets: Dict[str, PresetConfig] = field(default_factory=dict)
    active_preset_cfg: Optional[PresetConfig] = None

    def refresh_discovered_presets(self) -> None:
        self.available_presets = discover_presets()

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
                self.ref_stats[f"{slot_name}->{ref_key}"] = f"{enr.matched}/{enr.total} ({enr.match_rate * 100:.0f}%)"

            pools[slot_name] = rows
            prefix = f"{slot_name}." if len(self.sources) > 1 else ""
            for c in _collect_columns_from_rows(rows, prefix=prefix):
                if c not in cols:
                    cols.append(c)

        # Also include declared formula output column names
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

        prof_key, prof_data = cfg.resolve_profile(profile_name)
        self.profile = prof_key
        self.mode = str(prof_data.get("mode") or cfg.mode)

        if isinstance(cfg.formulas, dict):
            self.formulas = [f"{k} = {v}" for k, v in cfg.formulas.items()]
        else:
            self.formulas = list(cfg.formulas or [])
        self.where = list(cfg.where) + list(prof_data.get("where") or [])
        self.limits = list(prof_data.get("limits") or cfg.limits)
        self.rules = list(cfg.rules) + list(prof_data.get("rules") or [])
        self.weights = [(str(k), float(v)) for k, v in (prof_data.get("weights") or {}).items()]
        self.dirty = False

        self._rebuild_columns_and_pools()
        self.re_solve()
        self.status_message = f"Loaded preset '{cfg.name}' [profile={prof_key}, mode={self.mode}]."

    def load_from_source(
        self,
        source_path: str,
        table: Optional[str] = None,
        ref_path: Optional[str] = None,
        match_expr: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> None:
        p = Path(source_path).expanduser().resolve()
        src = open_source(str(p))
        schema = src.inspect()
        self.base_dir = p.parent
        self.active_preset_cfg = None
        self.preset_name = p.stem.lower().replace(" ", "_")
        self.title = f"{p.name} Interactive Session"
        self.profile = "custom"

        if table:
            tbl_names = [table]
        else:
            tbl_names = schema.table_names()

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
            self.attach_reference(tbl_names[0] if tbl_names else "rows", ref_path, match_expr=match_expr, rebuild=False)

        self.formulas = []
        self.where = []
        self.limits = []
        self.rules = []
        self._rebuild_columns_and_pools()
        self.weights = _suggest_weights_from_slots(self.slot_pools, self.mode)
        self.dirty = True
        self.re_solve()
        self.status_message = f"Loaded '{p.name}' ({len(tbl_names)} table(s), mode={self.mode})."

    def attach_reference(
        self,
        slot_name: str,
        ref_path: str,
        match_expr: Optional[str] = None,
        alias: str = "ref",
        on_unmatched: str = "drop",
        rebuild: bool = True,
    ) -> None:
        if slot_name not in self.sources:
            raise KeyError(f"Unknown slot '{slot_name}'.")
        rp = str(Path(ref_path).expanduser().resolve())
        ref_src = open_source(rp)
        ref_rows = ref_src.load_rows()
        if not match_expr:
            slot_rows = open_source(self.sources[slot_name]["source"]).load_rows(table=self.sources[slot_name].get("table"))
            l_cols = [k for k in (slot_rows[0].keys() if slot_rows else []) if isinstance(slot_rows[0][k], str)]
            r_cols = [k for k in (ref_rows[0].keys() if ref_rows else []) if isinstance(ref_rows[0][k], str)]
            lf = l_cols[0] if l_cols else "title"
            rf = r_cols[0] if r_cols else "model"
            match_expr = f"{lf}~{rf}"
        ref_key = f"{slot_name}_{alias}"
        self.references[ref_key] = {
            "source": rp,
            "match": match_expr,
            "strategy": "regex" if "~=" in match_expr else ("exact" if "=" in match_expr and "~" not in match_expr else "token"),
            "alias": alias,
            "on_unmatched": on_unmatched,
        }
        existing_refs = list(self.sources[slot_name].get("ref") or []) if isinstance(self.sources[slot_name].get("ref"), list) else (
            [self.sources[slot_name]["ref"]] if self.sources[slot_name].get("ref") else []
        )
        if ref_key not in existing_refs:
            existing_refs.append(ref_key)
        self.sources[slot_name]["ref"] = existing_refs[0] if len(existing_refs) == 1 else existing_refs
        self.dirty = True
        if rebuild:
            self._rebuild_columns_and_pools()
            self.re_solve()
            self.status_message = f"Attached reference '{Path(rp).name}' to slot '{slot_name}' ({self.ref_stats.get(f'{slot_name}->{ref_key}', '')})."

    def adjust_weight(self, idx: int, delta: float) -> None:
        if idx < 0 or idx >= len(self.weights):
            return
        expr, val = self.weights[idx]
        new_val = round(val + delta, 2)
        self.weights[idx] = (expr, new_val)
        self.dirty = True
        self.re_solve()
        self.status_message = f"Weight '{expr}' -> {new_val:+.2f}"

    def add_or_update_weight(self, expr: str, weight: float) -> None:
        clean = expr.strip()
        parse_weights([(f"{clean}:{weight}")])  # validates expression AST
        for i, (e, _) in enumerate(self.weights):
            if e == clean:
                self.weights[i] = (clean, round(float(weight), 2))
                break
        else:
            self.weights.append((clean, round(float(weight), 2)))
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added weight '{clean}' = {weight:+.2f}"

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
        self.status_message = f"Added formula '{formula_expr.strip()}'."

    def add_limit(self, limit_expr: str) -> None:
        from formula_engine import LimitSpec
        LimitSpec.parse(limit_expr)
        self.limits.append(limit_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added limit '{limit_expr.strip()}'."

    def add_rule(self, rule_expr: str) -> None:
        from formula_engine import RuleSpec
        RuleSpec.parse(rule_expr)
        self.rules.append(rule_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added rule '{rule_expr.strip()}'."

    def add_where(self, where_expr: str) -> None:
        from formula_engine import SafeExpression
        SafeExpression(where_expr)
        self.where.append(where_expr.strip())
        self.dirty = True
        self.re_solve()
        self.status_message = f"Added filter '{where_expr.strip()}'."

    def cycle_mode(self) -> None:
        order = ("solve", "basket", "rank") if len(self.slot_pools) > 1 else ("rank", "basket")
        curr_idx = order.index(self.mode) if self.mode in order else 0
        self.mode = order[(curr_idx + 1) % len(order)]
        self.re_solve()
        self.status_message = f"Switched mode -> {self.mode}"

    def re_solve(self) -> None:
        try:
            weight_map = {expr: w for expr, w in self.weights if abs(w) > 1e-9}
            if not self.slot_pools:
                self.results = []
                self.meta = {}
                return
            if self.mode == "rank":
                first_rows = next(iter(self.slot_pools.values()), [])
                ranked, meta = solve_rank(
                    rows=first_rows,
                    weights=weight_map,
                    formulas=self.formulas,
                    where=self.where,
                    top_n=max(10, self.top_n),
                )
                self.results = [r.to_dict() for r in ranked]
                self.meta = meta
            elif self.mode == "basket":
                first_rows = next(iter(self.slot_pools.values()), [])
                sols, meta = solve_basket(
                    rows=first_rows,
                    weights=weight_map,
                    limits=self.limits,
                    formulas=self.formulas,
                    where=self.where,
                    top_n=self.top_n,
                )
                self.results = [s.to_dict() for s in sols]
                self.meta = meta
            else:
                sols, meta = solve_sets(
                    slot_pools=self.slot_pools,
                    weights=weight_map,
                    limits=self.limits,
                    rules=self.rules,
                    formulas=self.formulas,
                    where=self.where,
                    top_n=self.top_n,
                )
                self.results = [s.to_dict() for s in sols]
                self.meta = meta
        except Exception as exc:
            self.status_message = f"Solve error: {exc}"

    def export_preset(self, path: str | Path, preset_name: Optional[str] = None) -> Path:
        name = (preset_name or self.preset_name or "custom_preset").strip().lower().replace(" ", "_")
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
            profiles={
                self.profile or "custom": {
                    "weights": {k: v for k, v in self.weights},
                    "limits": list(self.limits),
                    "rules": list(self.rules),
                    "where": list(self.where),
                }
            },
            default_profile=self.profile or "custom",
        )
        saved = save_preset(cfg, path, overwrite=True)
        self.dirty = False
        self.status_message = f"Saved preset to {saved}"
        return saved


def _render_result_lines(state: TuiSessionState, max_width: int) -> List[str]:
    lines: List[str] = []
    if not state.results:
        lines.append("(No feasible solutions match current weights/limits/rules.)")
        return lines

    if state.mode == "rank":
        for idx, r in enumerate(state.results, 1):
            label = _format_row_label(r["row"])
            lines.append(f"{idx:>2}. [{r['score']:5.1f}] {label}"[:max_width])
            if state.explain and r.get("breakdown"):
                bd_str = " | ".join(f"{b['expression']}:{b['contribution']:.1f}" for b in r["breakdown"])
                lines.append(f"    {bd_str}"[:max_width])
    elif state.mode == "basket":
        for idx, sol in enumerate(state.results, 1):
            lim = ", ".join(f"{k}={v:g}" for k, v in (sol.get("metrics") or {}).items())
            lines.append(f"Basket #{idx} [U={sol['total_score']:.1f}] {lim}"[:max_width])
            for it in sol["items"][:5]:
                q = f" x{it['count']}" if it["count"] > 1 else ""
                lines.append(f"  * {_format_row_label(it['row'])}{q} ({it['item_score']:.1f})"[:max_width])
    else:
        for idx, sol in enumerate(state.results, 1):
            lim = ", ".join(f"{k}={v:g}" for k, v in (sol.get("metrics") or {}).items())
            lines.append(f"Set #{idx} [Score={sol['total_score']:.1f}] {lim}"[:max_width])
            for sname, item in sol["slots"].items():
                p_str = f" ({item['price']})" if "price" in item else ""
                lines.append(f"  * {sname}: {_format_row_label(item)}{p_str}"[:max_width])
            if state.explain and sol.get("breakdown"):
                bd_str = " | ".join(f"{b['expression']}:{b['contribution']:.1f}" for b in sol["breakdown"])
                lines.append(f"    {bd_str}"[:max_width])
    return lines


def run_curses_tui(state: TuiSessionState) -> None:
    try:
        import curses
    except ImportError as exc:
        raise RuntimeError(
            "Python `curses` is unavailable on this platform. "
            "On Windows install `windows-curses` or use `ks serve` for the interactive Web Studio."
        ) from exc

    TABS = ("Weights", "Formulas", "Limits/Rules", "Sources/Refs", "Presets")

    def _prompt_input(stdscr: Any, label: str, hint: str = "") -> Optional[str]:
        h, w = stdscr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        try:
            if hint and h >= 5:
                stdscr.move(h - 3, 0)
                stdscr.clrtoeol()
                stdscr.addnstr(h - 3, 1, hint, w - 2)
            stdscr.move(h - 2, 0)
            stdscr.clrtoeol()
            stdscr.addnstr(h - 2, 1, label, w - 2)
            stdscr.refresh()
            raw = stdscr.getstr(h - 2, min(len(label) + 2, w - 4), max(16, w - len(label) - 4))
            text = raw.decode("utf-8", errors="replace").strip()
            return text or None
        finally:
            curses.noecho()
            curses.curs_set(0)

    def _left_items(tab_idx: int) -> List[str]:
        if tab_idx == 0:
            return [f"{expr:<24} {w:+6.2f}" for expr, w in state.weights] or ["(none — press 'a' to add weight)"]
        if tab_idx == 1:
            return [f"formula: {f}" for f in state.formulas] or ["(none — press 'a' to add formula)"]
        if tab_idx == 2:
            items = [f"[limit] {l}" for l in state.limits]
            items.extend(f"[rule ] {r}" for r in state.rules)
            items.extend(f"[where] {w}" for w in state.where)
            return items or ["(none — press 'a' to add limit, rule, or filter)"]
        if tab_idx == 3:
            rows = [f"[slot] {s}: {cfg.get('source')} ({cfg.get('table') or 'default'})" for s, cfg in state.sources.items()]
            for rk, rcfg in state.references.items():
                rows.append(f"[ref ] {rk}: {rcfg.get('source')} [{rcfg.get('match')}]")
            return rows or ["(no source loaded — press 'a' to open DB/CSV)"]
        state.refresh_discovered_presets()
        return [f"{p.name} ({p.mode}) - {p.title}" for p in state.available_presets.values()] or ["(no presets found)"]

    def _loop(stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)

        tab_idx = 0
        cursor = 0

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            left_w = max(32, int(w * 0.48))
            right_x = left_w + 1
            right_w = max(20, w - right_x - 1)

            dirty_mark = "*" if state.dirty else ""
            hdr = f" Knapset v2 TUI │ mode={state.mode} │ preset={state.preset_name}{dirty_mark} │ results={len(state.results)} "
            stdscr.addnstr(0, 0, hdr.ljust(w), w - 1, curses.A_REVERSE)

            tab_line = "  ".join(f"[{i + 1}:{t}]" if i == tab_idx else f" {i + 1}:{t} " for i, t in enumerate(TABS))
            stdscr.addnstr(1, 0, tab_line, left_w)
            stdscr.addnstr(1, right_x, "Live Top Solutions" + (" [EXPLAIN]" if state.explain else ""), right_w, curses.A_BOLD)

            items = _left_items(tab_idx)
            cursor = max(0, min(cursor, max(0, len(items) - 1)))
            body_rows = max(4, h - 5)

            for r in range(body_rows):
                y = 2 + r
                if r < len(items):
                    attr = curses.A_REVERSE if r == cursor else curses.A_NORMAL
                    stdscr.addnstr(y, 1, items[r], left_w - 2, attr)
                if w > left_w + 3:
                    stdscr.addnstr(y, left_w, "|", 1)

            res_lines = _render_result_lines(state, right_w)
            for r in range(min(body_rows, len(res_lines))):
                stdscr.addnstr(2 + r, right_x, res_lines[r], right_w)

            col_hint = "Cols: " + ", ".join(state.available_columns[:10])
            if h >= 5:
                stdscr.addnstr(h - 3, 1, col_hint, w - 2, curses.A_DIM)
            stdscr.addnstr(h - 2, 1, state.status_message, w - 2)
            keys = "q:Quit 1-5:Tabs ↑↓:Nav +/-:Weight [/]:Fine a:Add d:Del m:Mode e:Explain s:Save r:Reload"
            stdscr.addnstr(h - 1, 0, keys.ljust(w), w - 1, curses.A_REVERSE)
            stdscr.refresh()

            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                break
            if ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
                tab_idx = int(chr(ch)) - 1
                cursor = 0
            elif ch == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif ch == curses.KEY_DOWN:
                cursor = min(len(items) - 1, cursor + 1)
            elif ch in (ord("+"), ord("=")) and tab_idx == 0:
                state.adjust_weight(cursor, 0.1)
            elif ch in (ord("-"), ord("_")) and tab_idx == 0:
                state.adjust_weight(cursor, -0.1)
            elif ch == ord("]") and tab_idx == 0:
                state.adjust_weight(cursor, 0.05)
            elif ch == ord("[") and tab_idx == 0:
                state.adjust_weight(cursor, -0.05)
            elif ch in (ord("m"), ord("M")):
                state.cycle_mode()
            elif ch in (ord("e"), ord("E")):
                state.explain = not state.explain
            elif ch in (ord("r"), ord("R")):
                state._rebuild_columns_and_pools()
                state.re_solve()
                state.status_message = "Reloaded data & re-solved."
            elif ch in (ord("s"), ord("S")):
                out_p = _prompt_input(stdscr, "Save preset path (.json): ", hint=f"Default: {state.preset_name}.knapset.json")
                target = out_p or f"{state.preset_name}.knapset.json"
                try:
                    state.export_preset(target)
                except Exception as exc:
                    state.status_message = f"Save failed: {exc}"
            elif ch in (ord("d"), ord("D")):
                if tab_idx == 0 and state.weights:
                    state.remove_weight(cursor)
                elif tab_idx == 1 and state.formulas:
                    state.formulas.pop(cursor)
                    state._rebuild_columns_and_pools()
                    state.re_solve()
                elif tab_idx == 2:
                    n_lim = len(state.limits)
                    n_rul = len(state.rules)
                    if cursor < n_lim:
                        state.limits.pop(cursor)
                    elif cursor < n_lim + n_rul:
                        state.rules.pop(cursor - n_lim)
                    elif cursor - n_lim - n_rul < len(state.where):
                        state.where.pop(cursor - n_lim - n_rul)
                    state.re_solve()
            elif ch in (ord("a"), ord("A")):
                try:
                    if tab_idx == 0:
                        expr = _prompt_input(stdscr, "Weight expression (e.g. 'ref.score:0.5' or 'cpu.score/gpu.score:-0.2'): ", hint=col_hint)
                        if expr:
                            if ":" in expr:
                                e_part, w_part = expr.rsplit(":", 1)
                                state.add_or_update_weight(e_part, float(w_part))
                            else:
                                state.add_or_update_weight(expr, 0.5)
                    elif tab_idx == 1:
                        expr = _prompt_input(stdscr, "Formula ('name = expr'): ", hint=col_hint)
                        if expr:
                            state.add_formula(expr)
                    elif tab_idx == 2:
                        kind = _prompt_input(stdscr, "Add [l]imit, [r]ule, or [w]here filter? (l/r/w): ")
                        if kind and kind.lower().startswith("l"):
                            val = _prompt_input(stdscr, "Limit (e.g. 'sum(price) <= 950'): ", hint=col_hint)
                            if val:
                                state.add_limit(val)
                        elif kind and kind.lower().startswith("r"):
                            val = _prompt_input(stdscr, "Rule (e.g. 'cpu.socket == mobo.socket' or 'cpu.score / gpu.score < 1'): ", hint=col_hint)
                            if val:
                                state.add_rule(val)
                        elif kind and kind.lower().startswith("w"):
                            val = _prompt_input(stdscr, "Filter (e.g. 'price <= 500'): ", hint=col_hint)
                            if val:
                                state.add_where(val)
                    elif tab_idx == 3:
                        kind = _prompt_input(stdscr, "Attach [m]ain DB or [r]eference DB? (m/r): ")
                        if kind and kind.lower().startswith("m"):
                            src_p = _prompt_input(stdscr, "Main DB/CSV/JSON path: ")
                            if src_p:
                                state.load_from_source(src_p)
                        elif kind and kind.lower().startswith("r") and state.sources:
                            slots = list(state.sources.keys())
                            slot_in = _prompt_input(stdscr, f"Target slot ({'/'.join(slots)}): ") or slots[0]
                            ref_p = _prompt_input(stdscr, "Reference DB/CSV path: ")
                            if ref_p:
                                match_in = _prompt_input(stdscr, "Match expr (e.g. 'title~model'): ")
                                state.attach_reference(slot_in, ref_p, match_expr=match_in)
                except Exception as exc:
                    state.status_message = f"Invalid input: {exc}"
            elif ch in (10, 13, curses.KEY_ENTER):
                if tab_idx == 4 and state.available_presets:
                    chosen_name = list(state.available_presets.keys())[cursor]
                    try:
                        state.load_from_preset(chosen_name)
                        tab_idx = 0
                        cursor = 0
                    except Exception as exc:
                        state.status_message = f"Failed loading preset '{chosen_name}': {exc}"

    curses.wrapper(_loop)


def run_tui(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} tui",
        description="Launch interactive Knapset v2 Terminal Studio (curses).",
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
    elif state.available_presets:
        first_preset = next(iter(state.available_presets.keys()))
        state.load_from_preset(first_preset)

    run_curses_tui(state)
