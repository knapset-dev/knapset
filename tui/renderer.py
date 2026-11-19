"""Pure formatting and layout helpers for the Knapset v2 TUI (testable without a TTY)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

from theme_config import ThemeConfig
from tui.state import TuiSessionState, _format_row_label


def _format_weight_bar(
    weight: float,
    width: int = 10,
    fill_ch: str = "█",
    half_ch: str = "▌",
    empty_ch: str = "░",
) -> str:
    """Renders `[0, 1]` weight bar where every 10% (0.10) is 1 full `█` symbol (and 5% is `▌`)."""
    mag = max(0.0, min(1.0, abs(weight)))
    half_steps = max(0, min(width * 2, int(round(mag * width * 2))))
    full = half_steps // 2
    half = half_steps % 2
    return fill_ch * full + (half_ch * half) + empty_ch * (width - full - half)


def _compute_hold_interval(hold_steps: int) -> float:
    """Returns repeat interval (seconds) while holding a key: responsive start with a low capped max speed."""
    if hold_steps >= 6:
        return 0.065
    if hold_steps >= 2:
        return 0.072
    return 0.080


@dataclass
class _WorkspaceItem:
    kind: str  # "header" | "profile" | "weight" | "limit" | "rule" | "formula" | "where" | "action" | "slot" | "ref"
    label: str
    index: int = -1
    key: str = ""
    action_id: str = ""
    tone: str = "normal"  # "muted" | "pos" | "min" | "accent" | "normal"


def _format_weight_expr(expr: str, name_w: int) -> str:
    """Fits a dotted weight expression into `name_w` chars without hiding `.ref.<metric>` suffixes."""
    if len(expr) <= name_w:
        return expr
    short = expr.replace(".ref.", ".r.")
    if len(short) <= name_w:
        return short
    if "." in short:
        head, tail = short.rsplit(".", 1)
        avail = name_w - len(head) - 1
        if avail >= 6:
            return f"{head}.{tail[:avail]}"
    return short[:name_w]


def _build_workspace_items(
    state: TuiSessionState,
    theme: Optional[ThemeConfig] = None,
    weight_name_w: int = 18,
) -> List[_WorkspaceItem]:
    items: List[_WorkspaceItem] = []
    sym = theme.symbols if theme else {}
    fill_ch = sym.get("fill", "█")
    half_ch = sym.get("half", "▌")
    empty_ch = sym.get("empty", "░")

    items.append(_WorkspaceItem(kind="header", label="WEIGHTS 0..1 (←/→ tune · t:↑/↓ · Enter:edit · pink<1/N%)", tone="accent"))
    if state.weights:
        mags = [min(1.0, abs(w)) for _, w in state.weights]
        total_w = sum(mags)
        active_n = sum(1 for m in mags if m > 1e-9)
        eq_threshold = (1.0 / active_n) if active_n > 0 else 1.0
        for idx, ((expr, val), mag) in enumerate(zip(state.weights, mags)):
            bar = _format_weight_bar(mag, width=10, fill_ch=fill_ch, half_ch=half_ch, empty_ch=empty_ch)
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
            lbl_expr = _format_weight_expr(expr, weight_name_w)
            items.append(
                _WorkspaceItem(
                    kind="weight",
                    index=idx,
                    label=f" {lbl_expr:<{weight_name_w}} [-][{bar}][+] {mag:.2f} {pct:>2}% {arrow}",
                    tone=tone,
                )
            )
    else:
        items.append(_WorkspaceItem(kind="action", action_id="add_weight", label="  (no weights yet — press Enter to add)", tone="muted"))
    items.append(_WorkspaceItem(kind="action", action_id="add_weight", label="  + Add Weight / Ratio Expression...", tone="accent"))
    if state.deleted_weights:
        n_del = len(state.deleted_weights)
        items.append(
            _WorkspaceItem(
                kind="action",
                action_id="weight_trash",
                label=f"  🗑 Weight Trash ({n_del} deleted · Enter: pick · u: undo)",
                tone="min",
            )
        )

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
        diag = state.meta.get("diagnostics") if isinstance(state.meta, dict) else None
        if isinstance(diag, dict) and diag.get("reasons"):
            lines.append("No feasible results:")
            lines.append("")
            for r in diag["reasons"][:4]:
                lines.append(f"  • {r}")
        else:
            lines.extend(
                [
                    "No combinations satisfy all active rules & limits.",
                    "",
                    "Try relaxing a Limit (e.g. budget) or removing a strict Rule.",
                ]
            )
        return [ln[:max_width] for ln in lines]

    header_tags = [f"norm={state.norm}"]
    if state.diverse and state.mode == "solve":
        header_tags.append("diverse")
    if state.pins:
        header_tags.append(f"pins={len(state.pins)}")
    if state.excludes:
        header_tags.append(f"excl={len(state.excludes)}")
    lines.append(f"  [{' · '.join(header_tags)}]"[:max_width])

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
