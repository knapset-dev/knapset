#!/usr/bin/env python3
"""
Knapset v2 — Zero-Dependency Database Preference, Formula & Constraint Optimizer.

Usage:
  ks inspect <db_or_file>                   Inspect tables, columns, inferred types, and ranges
  ks compare <db> --ref <ref_db>            Match and compare rows against a Reference DB
  ks rank    <db> --prefer "..."            Rank rows using signed weights, formulas, and filters
  ks basket  <db> --limit "..."             Pack an optimal multi-item basket (0/1 or bounded knapsack)
  ks solve   <db_or_slots> --limit/rule ... Synthesize multi-slot sets across tables/files
  ks run     <preset> [--profile ...]       Execute a saved v2 preset (.json/.toml) with CLI overrides
  ks preset  <list|search|install|...>      Manage local & registry v2 presets
  ks prefs   <list|show|save|import|...>    Manage saved preferences reusable on any DB (use with --prefs NAME)
  ks init    <db> [--ref ...]               Scaffold a starter v2 preset JSON from an existing DB
  ks tui     [db_or_preset]                 Launch interactive curses TUI (weights +/- , formulas, rules)
  ks serve   [--port 8080]                  Start local Web Studio & REST API server
  ks mcp     [--list-tools]                 Start Model Context Protocol (MCP) JSON-RPC 2.0 stdio server
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_paths import get_cli_prog as _cli_prog  # noqa: E402

APP_VERSION = "2.0.0"

SUBCOMMANDS: Dict[str, str] = {
    "inspect": "Inspect tables, columns, inferred types, and value ranges in a DB/CSV/JSON source",
    "init": "Scaffold a starter v2 JSON preset from an existing database or file",
    "compare": "Match and compare rows from a candidate source against a Reference DB (--ref)",
    "rank": "Rank rows in a table using signed weights (+ max, - min), formulas, and filters",
    "basket": "Pack an optimal multi-item basket under multi-resource limits (0/1 or bounded knapsack)",
    "solve": "Synthesize multi-slot combinations across tables/files with weights, limits, and AST rules",
    "sensitivity": "Test #1 winner stability and marginal budget impact by perturbing weights and limits",
    "run": "Execute a saved Knapset v2 preset (.json/.toml or installed name) with CLI overrides",
    "preset": "Manage local and registry presets (list, search, info, install, remove, verify)",
    "prefs": "Manage saved preference profiles reusable across DBs (list, show, save, import, export, remove)",
    "reg": "Manage registry items & themes (search, info, install, list, remove, verify, theme)",
    "tui": "Launch interactive Terminal UI (curses) for live +/- weights, formulas, limits, and rules",
    "serve": "Start zero-dependency Web Studio & REST API server (alias: web)",
    "mcp": "Start built-in Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio",
}


def print_help() -> None:
    prog = _cli_prog()
    print(f"Knapset v{APP_VERSION} — Database Preference, Formula & Constraint Optimizer\n")
    print("Usage:")
    print(f"  {prog} <command> [options]")
    print(f"  {prog} --help | --version\n")
    print("Commands:")
    for cmd, desc in SUBCOMMANDS.items():
        print(f"  {cmd:<10} {desc}")
    print("\nQuick Start:")
    print(f"  1. {prog} inspect data.db")
    print(f"  2. {prog} rank gpus.csv --ref msrp.csv --match title~model --prefer 'ref.score:+0.7,price:-0.3' --explain")
    print(f"  3. {prog} basket snacks.csv --limit 'sum(price) <= 20' --prefer 'protein:+0.8,price:-0.2'")
    print(f"  4. {prog} solve pc.db --limit 'sum(price) <= 950' --rule 'cpu.socket == mobo.socket'")
    print(f"  5. {prog} tui pc_build")
    print(f"  6. {prog} init data.db -o my_preset.json && {prog} run my_preset.json\n")


def main(argv: Optional[List[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return

    if args[0] in ("-V", "--version", "version"):
        print(f"Knapset v{APP_VERSION}")
        return

    cmd = args[0].lower().strip()
    extra = args[1:]

    if cmd == "inspect":
        from v2_cli import run_inspect
        run_inspect(extra)
    elif cmd == "init":
        from v2_cli import run_init
        run_init(extra)
    elif cmd == "compare":
        from v2_cli import run_compare
        run_compare(extra)
    elif cmd == "rank":
        from v2_cli import run_rank
        run_rank(extra)
    elif cmd == "basket":
        from v2_cli import run_basket
        run_basket(extra)
    elif cmd == "solve":
        from v2_cli import run_solve
        run_solve(extra)
    elif cmd in ("sensitivity", "sens"):
        from v2_cli import run_sensitivity
        run_sensitivity(extra)
    elif cmd == "run":
        from v2_cli import run_run
        run_run(extra)
    elif cmd == "preset":
        from v2_cli import run_preset
        run_preset(extra)
    elif cmd == "prefs":
        from v2_cli import run_prefs
        run_prefs(extra)
    elif cmd in ("reg", "registry"):
        from v2_cli import run_reg
        run_reg(extra)
    elif cmd == "tui":
        from tui import run_tui
        run_tui(extra)
    elif cmd in ("serve", "web"):
        from web_server import run_web_server
        run_web_server(extra)
    elif cmd == "mcp":
        from mcp_server import run_mcp_cli
        run_mcp_cli(extra)
    else:
        print(f"Unknown command '{args[0]}'. Run '{_cli_prog()} --help' for usage.\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
