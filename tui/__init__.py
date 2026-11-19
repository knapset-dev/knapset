"""Knapset v2 Interactive Terminal UI package."""

from tui.app import run_curses_tui, run_tui
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
    _collect_columns_from_rows,
    _format_row_label,
    _suggest_weights_from_slots,
    discover_local_data_files,
)

__all__ = [
    "TuiSessionState",
    "discover_local_data_files",
    "run_curses_tui",
    "run_tui",
    "_WorkspaceItem",
    "_build_workspace_items",
    "_compute_hold_interval",
    "_format_weight_bar",
    "_format_weight_expr",
    "_render_result_lines",
    "_wrap_suggestion_lines",
    "_collect_columns_from_rows",
    "_format_row_label",
    "_suggest_weights_from_slots",
    "_DATA_EXTENSIONS",
]
