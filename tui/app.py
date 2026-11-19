"""Interactive curses runtime, modals, and CLI entrypoint for Knapset v2 TUI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_paths import get_cli_prog
from formula_engine import LimitSpec, RuleSpec, SafeExpression, parse_formulas
from preset_loader import get_user_presets_dir, get_user_profiles_dir, list_shared_profiles
from sources.registry import open_source
from theme_config import ThemeConfig, discover_themes, load_active_theme, set_active_theme
from tui.renderer import (
    _WorkspaceItem,
    _build_workspace_items,
    _compute_hold_interval,
    _format_weight_bar,
    _format_weight_expr,
    _render_result_lines,
    _wrap_suggestion_lines,
)
from tui.state import (
    TuiSessionState,
    _DATA_EXTENSIONS,
    _format_row_label,
    discover_local_data_files,
)


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
        ref_tbl_schema = schema.get_table(ref_tbl) if ref_tbl else (schema.tables[0] if schema.tables else None)
        ref_cols = ref_tbl_schema.column_names() if ref_tbl_schema else []
        match_suggestions: List[str] = []
        default_match = "title~model"
        if "model_key" in slot_cols and "model" in ref_cols:
            default_match = "model_key=model"
            match_suggestions.extend(["model_key=model", "model_key~model"])
        elif "chipset" in slot_cols and "chipset" in ref_cols:
            default_match = "chipset=chipset"
            match_suggestions.extend(["chipset=chipset", "title~chipset"])
        elif "chipset" in ref_cols:
            default_match = "title~chipset"
            match_suggestions.append("title~chipset")
        if "title" in slot_cols and "model" in ref_cols:
            match_suggestions.append("title~model")
        for c in ref_cols[:6]:
            tag = f"ref.{c}"
            if tag not in match_suggestions:
                match_suggestions.append(tag)
        match_in = _modal_text_input(
            stdscr,
            "Match Rule",
            f"Match {target_slot}.col ~ {ref_tbl or Path(ref_path).name}.col ('~' token, '=' exact):",
            initial=default_match,
            suggestions=match_suggestions or slot_cols[:10],
            examples=["model_key=model", "title~model", "title~chipset"],
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

    def _action_load_profile(stdscr: Any) -> None:
        lib = list_shared_profiles()
        names = list(lib.keys())
        choices = []
        for n in names:
            d = lib[n]
            wsum = ", ".join(f"{k}:{v:+g}" for k, v in list((d.get("weights") or {}).items())[:3])
            choices.append((n, f"[{d.get('mode', '-')}] {wsum}"))
        choices.append(("✎ Load profile / preset file path...", "any .json / .toml with profiles"))
        pick = _modal_picker(
            stdscr,
            "Saved Preferences (apply to current DB)",
            choices,
            hint=f"Enter Apply · Esc Cancel · stored in {get_user_profiles_dir()}",
        )
        if pick is None:
            return
        try:
            if pick == len(names):
                path_in = _modal_text_input(stdscr, "Load Profile File", "Enter profile / preset .json or .toml path:")
                if path_in:
                    state.apply_shared_profile(path_in)
            else:
                state.apply_shared_profile(names[pick])
        except Exception as exc:
            state.status_message = f"Load profile failed: {exc}"

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

    active_theme = load_active_theme()

    def _apply_curses_theme(theme_cfg: ThemeConfig) -> None:
        if not curses.has_colors():
            return
        support_256 = getattr(curses, "COLORS", 0) >= 256
        role_pairs = (
            (C_CYAN, "accent", None),
            (C_GREEN, "positive", None),
            (C_MIN, "minimize", None),
            (C_AMBER, "warning", None),
            (C_DIM, "muted", None),
            (C_SEL, "selection_fg", "selection_bg"),
            (C_BAR, "bar_fg", "bar_bg"),
        )
        for pair_id, fg_role, bg_role in role_pairs:
            fg, bg = theme_cfg.resolve_role_pair(fg_role, bg_role, support_256=support_256)
            try:
                curses.init_pair(pair_id, fg, bg)
            except Exception:
                pass

    def _action_pick_theme(scr: Any) -> None:
        nonlocal active_theme
        themes = discover_themes()
        t_list = list(themes.values())
        options = [
            (f"{t.name}{' ★' if t.name == active_theme.name else ''}", f"{t.title} — {t.description}"[:60])
            for t in t_list
        ]
        idx = _modal_picker(scr, "Select Theme (install more via `ks reg install <theme>`)", options)
        if idx is not None:
            chosen = t_list[idx]
            active_theme, _ = set_active_theme(chosen.name)
            _apply_curses_theme(active_theme)
            state.status_message = f"Theme: {active_theme.name} ({active_theme.title})"

    def _action_weight_trash(scr: Any) -> bool:
        """Opens the Weight Trash picker to restore one, restore all, or empty trash. Returns True if weights changed."""
        if not state.deleted_weights:
            state.status_message = "Weight Trash is empty."
            return False
        rev = list(reversed(state.deleted_weights))
        choices: List[Tuple[str, str]] = [
            (expr, f"{mag:.2f} {'↓ min' if d == 'min' else '↑ max'} — restore")
            for expr, mag, d in rev
        ]
        choices.append(("↺ Restore ALL deleted weights", f"bring back all {len(rev)} parameter(s)"))
        choices.append(("✕ Empty Weight Trash", "permanently clear trash list"))
        pick = _modal_picker(scr, "🗑 Weight Trash (Restore Deleted Weights)", choices)
        if pick is None:
            return False
        if pick == len(rev):
            state.restore_all_deleted_weights(auto_solve=False)
            return True
        if pick == len(rev) + 1:
            state.empty_weight_trash()
            return False
        orig_idx = len(state.deleted_weights) - 1 - pick
        state.restore_deleted_weight(orig_idx, auto_solve=False)
        return True

    def _action_cycle_norm() -> None:
        order = ("robust", "minmax", "rank")
        nxt = order[(order.index(state.norm) + 1) % len(order)] if state.norm in order else "robust"
        state.norm = nxt
        state.re_solve()
        state.status_message = f"Normalization: {nxt}"

    def _action_pin_prompt(stdscr: Any) -> None:
        if state.mode == "solve" and state.slot_pools:
            slots = list(state.slot_pools.keys())
            pick = _modal_picker(stdscr, "Pin Item to Slot", [(s, state.pins.get(s, "(unpinned)")) for s in slots])
            if pick is None:
                return
            slot = slots[pick]
            val = _modal_text_input(stdscr, f"Pin {slot}", "Item name / ID substring:", initial=state.pins.get(slot, ""))
            if val:
                state.pin_slot(slot, val)
        else:
            val = _modal_text_input(stdscr, "Pin Item", "Item name / ID substring:")
            if val:
                state.pin_slot("item", val)

    def _action_exclude_prompt(stdscr: Any) -> None:
        val = _modal_text_input(stdscr, "Exclude Item", "Pattern ('name' or 'slot:name'):")
        if val:
            state.exclude_item(val)

    def _action_sensitivity_modal(stdscr: Any) -> None:
        try:
            rep = state.run_sensitivity()
        except Exception as exc:
            state.status_message = f"Sensitivity error: {exc}"
            return
        base = rep.get("baseline")
        if not base:
            _modal_picker(stdscr, "Sensitivity Analysis", [("No baseline", "No feasible solution found.")])
            return
        rows: List[Tuple[str, str]] = [
            ("Baseline #1", f"[{base['score']}] {base['label']}"),
            ("Stability", f"{rep.get('stability_pct', 0.0):.1f}% under ±15% weight shifts"),
        ]
        if rep.get("closest_challenger"):
            rows.append(("Challenger", str(rep["closest_challenger"])))
        for lp in rep.get("limit_perturbations") or []:
            delta_s = f"{lp['score_delta']:+.1f}" if lp.get("score_delta") is not None else "infeasible"
            rows.append((lp["adjusted_limit"], f"score {lp['score']} ({delta_s})"))
        _modal_picker(stdscr, "Sensitivity & Stability Report", rows, hint="Press Esc or Enter to close")

    def _action_command_palette(stdscr: Any) -> None:
        cmds = [
            ("/open", "Open / Replace Main SQLite / CSV / JSON database"),
            ("/ref", "Attach a Reference DB (--ref)"),
            ("/save-pref", "Save current preferences as named profile (persists for reuse)"),
            ("/hop", "Cycle / hop between saved preference profiles"),
            ("/profiles", "Apply a saved preference (from shared library or a file) to this DB"),
            ("/preset", "Load a saved preset file or installed registry preset"),
            ("/weight", "Add a weighted column or ratio expression"),
            ("/trash", f"Open Weight Trash ({len(state.deleted_weights)} deleted weight(s))"),
            ("/formula", "Create a new formula using available DB fields"),
            ("/rule", "Add constraint (Rule / Limit / Filter)"),
            ("/mode", "Switch optimization mode (solve / basket / rank)"),
            ("/norm", f"Cycle normalization mode (current: {state.norm})"),
            ("/diverse", f"Toggle diverse Top-N sets (current: {'ON' if state.diverse else 'OFF'})"),
            ("/pin", "Pin an item to a slot/basket"),
            ("/exclude", "Exclude an item pattern"),
            ("/clear-pins", "Clear all active pins & excludes"),
            ("/sensitivity", "Check #1 winner stability & budget shadow gains"),
            ("/explain", "Toggle normalized score breakdown"),
            ("/theme", "Switch active UI theme (`configs/theme.json`)"),
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
        elif cmd == "/profiles":
            _action_load_profile(stdscr)
        elif cmd == "/preset":
            _action_pick_preset(stdscr)
        elif cmd == "/weight":
            _action_add_weight(stdscr)
        elif cmd == "/trash":
            if _action_weight_trash(stdscr):
                state.re_solve()
        elif cmd in ("/formula", "/rule"):
            _action_add_constraint(stdscr)
        elif cmd == "/mode":
            state.cycle_mode()
        elif cmd == "/norm":
            _action_cycle_norm()
        elif cmd == "/diverse":
            state.diverse = not state.diverse
            state.re_solve()
            state.status_message = f"Diverse Top-N sets: {'ON' if state.diverse else 'OFF'}"
        elif cmd == "/pin":
            _action_pin_prompt(stdscr)
        elif cmd == "/exclude":
            _action_exclude_prompt(stdscr)
        elif cmd == "/clear-pins":
            state.clear_pins_and_excludes()
        elif cmd == "/sensitivity":
            _action_sensitivity_modal(stdscr)
        elif cmd == "/explain":
            state.explain = not state.explain
            state.status_message = f"Explain breakdown: {'ON' if state.explain else 'OFF'}"
        elif cmd == "/theme":
            _action_pick_theme(stdscr)
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
                    ("s / l / h", "Save preferences to shared library / Load saved preferences / Hop profile"),
                    ("o / p / m / n / x / e", "Open DB / Preset / Cycle Mode / Cycle Norm / Exclude / Explain"),
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
            btn1_rel = getattr(curses, "BUTTON1_RELEASED", 0)
            curses.mousemask(curses.BUTTON1_PRESSED | btn1_rel | wheel_up | wheel_dn)
            if hasattr(curses, "mouseinterval"):
                curses.mouseinterval(0)
        except Exception:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            _apply_curses_theme(active_theme)

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
        hold_steps = 0
        mouse_hold: Optional[List[Any]] = None

        def _nudge_weight(idx: int, sign: int, allow_accel: bool = True) -> None:
            nonlocal last_step_idx, last_step_sign, last_event_at, last_apply_at, hold_steps
            now = time.monotonic()
            same_repeat = allow_accel and idx == last_step_idx and sign == last_step_sign and (now - last_event_at) <= 0.090
            last_step_idx = idx
            last_step_sign = sign
            last_event_at = now
            if same_repeat:
                if (now - last_apply_at) < _compute_hold_interval(hold_steps):
                    return
                hold_steps += 1
            else:
                hold_steps = 0
            last_apply_at = now
            state.adjust_weight(idx, sign * 0.05, auto_solve=False)
            _schedule_solve()

        def _schedule_solve(immediate: bool = False) -> None:
            nonlocal solve_pending, solve_due_at, solve_gen, needs_redraw
            solve_gen += 1
            solve_pending = True
            solve_due_at = time.monotonic() + (0.0 if immediate else DEBOUNCE_SEC)
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

            if mouse_hold is not None:
                m_idx, m_sign, m_pressed, m_last, m_steps = mouse_hold
                now_m = time.monotonic()
                if (now_m - m_pressed) >= 0.14 and (now_m - m_last) >= _compute_hold_interval(m_steps):
                    if 0 <= m_idx < len(state.weights):
                        old_v = state.weights[m_idx][1]
                        state.adjust_weight(m_idx, m_sign * 0.05, auto_solve=False)
                        if state.weights[m_idx][1] != old_v:
                            mouse_hold[3] = now_m
                            mouse_hold[4] = m_steps + 1
                            _schedule_solve()
                            needs_redraw = True
                        else:
                            mouse_hold = None
                    else:
                        mouse_hold = None

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
                left_w = max(56 if w >= 88 else 38, min(64, int(w * 0.48)))
                weight_name_w = 18 if left_w >= 56 else 12
                slider_x = 3 + weight_name_w
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
                ws_items = _build_workspace_items(state, theme=active_theme, weight_name_w=weight_name_w)
                left_cursor = max(0, min(left_cursor, max(0, len(ws_items) - 1)))
                if ws_items and ws_items[left_cursor].kind == "header" and left_cursor + 1 < len(ws_items):
                    left_cursor += 1
                if left_cursor < left_scroll:
                    left_scroll = left_cursor
                elif left_cursor >= left_scroll + inner_h:
                    left_scroll = left_cursor - inner_h + 1

                cursor_ch = active_theme.symbols.get("cursor", "▸")
                for r in range(inner_h):
                    idx = left_scroll + r
                    if idx >= len(ws_items):
                        stdscr.addnstr(pane_y + 1 + r, 2, " " * max(0, left_w - 3), max(0, left_w - 3))
                        continue
                    it = ws_items[idx]
                    selected = (not focus_results) and (idx == left_cursor)
                    marker = cursor_ch if selected else " "
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

            if mouse_hold is not None:
                stdscr.timeout(15)
            else:
                stdscr.timeout(40 if (solve_pending or worker_running) else -1)
            ch = stdscr.getch()
            if ch == curses.ERR:
                continue
            if ch != curses.KEY_MOUSE:
                mouse_hold = None
            needs_redraw = True
            if ch == curses.KEY_RESIZE:
                force_clear = True
                continue
            if ch == 27:
                # Drain any unmapped CSI/SS3 escape sequence (e.g. Ctrl+Shift+Left `\x1b[1;6D`) so final 'D'/'A' never fires shortcuts
                try:
                    stdscr.nodelay(True)
                    nxt = stdscr.getch()
                    if nxt in (ord("["), ord("O")):
                        for _ in range(16):
                            b = stdscr.getch()
                            if b == curses.ERR or 0x40 <= b <= 0x7E:
                                break
                finally:
                    stdscr.nodelay(False)
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
                state.cycle_profile(auto_solve=False)
                _schedule_solve(immediate=True)
                continue
            if ch in (ord("m"), ord("M")):
                state.cycle_mode(auto_solve=False)
                _schedule_solve(immediate=True)
                continue
            if ch in (ord("n"), ord("N")):
                _flush_pending_solve()
                _action_cycle_norm()
                continue
            if ch in (ord("x"), ord("X")):
                _flush_pending_solve()
                _action_exclude_prompt(stdscr)
                force_clear = True
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
            if ch in (ord("l"), ord("L")):
                _flush_pending_solve()
                _action_load_profile(stdscr)
                force_clear = True
                continue
            if ch in (ord("u"), ord("U")):
                if state.deleted_weights:
                    state.restore_deleted_weight(-1, auto_solve=False)
                    _schedule_solve(immediate=True)
                else:
                    state.status_message = "Weight Trash is empty."
                continue

            if ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
                    wheel_dn = getattr(curses, "BUTTON5_PRESSED", 0)
                    btn1_rel = getattr(curses, "BUTTON1_RELEASED", 0)
                    btn1_press = getattr(curses, "BUTTON1_PRESSED", 0)
                    btn1_click = getattr(curses, "BUTTON1_CLICKED", 0)
                    if btn1_rel and (bstate & btn1_rel):
                        mouse_hold = None
                        continue
                    if wheel_up and (bstate & wheel_up):
                        mouse_hold = None
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
                        mouse_hold = None
                        if focus_results or mx >= right_x:
                            right_scroll = min(max_r_scroll, right_scroll + 2)
                        else:
                            nxt = min(len(ws_items) - 1, left_cursor + 1)
                            while nxt < len(ws_items) - 1 and ws_items[nxt].kind == "header":
                                nxt += 1
                            if ws_items[nxt].kind != "header":
                                left_cursor = nxt
                        continue
                    if not (bstate & (btn1_press | btn1_click)):
                        mouse_hold = None
                        continue

                    mouse_hold = None
                    if pane_y < my < pane_y + pane_h - 1 and 1 <= mx < left_w - 1:
                        focus_results = False
                        clicked_idx = left_scroll + (my - pane_y - 1)
                        if 0 <= clicked_idx < len(ws_items) and ws_items[clicked_idx].kind != "header":
                            left_cursor = clicked_idx
                            it = ws_items[left_cursor]
                            if it.kind == "weight":
                                if slider_x <= mx <= slider_x + 3:
                                    _nudge_weight(it.index, -1, allow_accel=False)
                                    if btn1_press and (bstate & btn1_press):
                                        now_p = time.monotonic()
                                        mouse_hold = [it.index, -1, now_p, now_p, 0]
                                elif slider_x + 4 <= mx <= slider_x + 15:
                                    # Click directly along 10-symbol [██████████] bar in 5% steps (0.00 .. 1.00)
                                    frac = max(0.0, min(1.0, (mx - (slider_x + 4)) / 10.0))
                                    val_click = round(round(frac * 20) / 20.0, 2)
                                    w_expr, _ = state.weights[it.index]
                                    state.weights[it.index] = (w_expr, val_click)
                                    state.dirty = True
                                    state.status_message = f"{w_expr}: {val_click:.2f}"
                                    _schedule_solve()
                                elif slider_x + 16 <= mx <= slider_x + 19:
                                    _nudge_weight(it.index, 1, allow_accel=False)
                                    if btn1_press and (bstate & btn1_press):
                                        now_p = time.monotonic()
                                        mouse_hold = [it.index, 1, now_p, now_p, 0]
                                elif mx >= slider_x + 28:
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
                cur = ws_items[left_cursor]
                if cur.kind == "weight":
                    state.remove_weight(cur.index, auto_solve=False)
                    _schedule_solve(immediate=True)
                elif cur.kind == "profile":
                    state.delete_preference_profile(cur.key, auto_solve=False)
                    _schedule_solve(immediate=True)
                elif cur.kind == "slot":
                    solve_gen += 1
                    solve_pending = False
                    worker_running = False
                    state.remove_slot(cur.key)
                elif cur.kind == "ref":
                    solve_gen += 1
                    solve_pending = False
                    worker_running = False
                    state.detach_reference(cur.key)
                elif cur.kind == "limit" and 0 <= cur.index < len(state.limits):
                    state.limits.pop(cur.index)
                    _schedule_solve(immediate=True)
                elif cur.kind == "rule" and 0 <= cur.index < len(state.rules):
                    state.rules.pop(cur.index)
                    _schedule_solve(immediate=True)
                elif cur.kind == "formula" and 0 <= cur.index < len(state.formulas):
                    state.formulas.pop(cur.index)
                    state._rebuild_columns_and_pools()
                    _schedule_solve(immediate=True)
                elif cur.kind == "where" and 0 <= cur.index < len(state.where):
                    state.where.pop(cur.index)
                    _schedule_solve(immediate=True)
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
                elif cur.action_id == "weight_trash":
                    if _action_weight_trash(stdscr):
                        _schedule_solve(immediate=True)
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
                    state.switch_profile(cur.key, auto_solve=False)
                    _schedule_solve(immediate=True)
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
    parser.add_argument("--prefs", default=None, help="Saved preference profile name or profile/preset file to apply to the opened DB")
    parser.add_argument("--fresh", action="store_true", help="Ignore previously saved preferences for this source")
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
            if not args.fresh:
                state.restore_saved_session(overlay_only=bool(args.ref or args.mode or args.match))

    if args.prefs:
        try:
            state.apply_shared_profile(args.prefs)
        except Exception as exc:
            state.status_message = f"--prefs failed: {exc}"

    run_curses_tui(state)
