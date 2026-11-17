#!/usr/bin/env python3
"""
Knapset v2 Built-in Model Context Protocol (MCP) Server (`ks mcp`).

Zero-dependency JSON-RPC 2.0 server over stdio exposing Knapset v2's universal
database inspection, preference ranking, multi-limit knapsack, combinatorial set
synthesis, and preset discovery tools to AI agents.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from preset_loader import discover_presets, resolve_preset
from solver_engine import solve_basket, solve_rank, solve_sets
from sources.registry import inspect_source, open_source

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "knapset-mcp",
    "version": "2.0.0",
}

MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "knapset_inspect_source",
        "description": "Inspect tables, column names, inferred types (number/text/boolean), and min/max ranges in a read-only SQLite, CSV, TSV, JSON, or JSONL source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Path or URI to SQLite/CSV/TSV/JSON/JSONL data source."},
                "sample_rows": {"type": "integer", "description": "Sample row count for inference (default: 200)."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "knapset_rank_items",
        "description": "Rank rows from a table/file using signed weights (positive maximizes, negative minimizes), optional Reference DB enrichment, derived formulas, and filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Primary data source path/URI."},
                "table": {"type": "string", "description": "Optional table/collection name."},
                "query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query (SQLite only)."},
                "ref_source": {"type": "string", "description": "Optional Reference DB path/URI to enrich rows."},
                "ref_table": {"type": "string", "description": "Optional table inside Reference DB."},
                "match": {"type": "string", "description": "Match rule e.g. 'title~model' (token) or 'sku=id' (exact)."},
                "alias": {"type": "string", "description": "Attached reference field alias (default: 'ref')."},
                "on_unmatched": {"type": "string", "enum": ["drop", "keep"], "description": "Policy for unmatched rows (default: 'drop')."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Derived formulas e.g. ['deal = (ref.msrp - price) / ref.msrp']."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Filter predicates e.g. ['price <= 600']."},
                "weights": {"type": "string", "description": "Signed weights e.g. 'ref.score:+0.7, price:-0.3'."},
                "top_n": {"type": "integer", "description": "Top items to return (default: 10)."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "knapset_solve_basket",
        "description": "Pack an optimal multi-item basket (0/1 or bounded knapsack) from a table/file under multiple aggregate limits (sum/count/avg/min/max).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Primary data source path/URI."},
                "table": {"type": "string", "description": "Optional table/collection name."},
                "weights": {"type": "string", "description": "Signed weights e.g. 'protein:+0.8, price:-0.2'."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Limits e.g. ['sum(price) <= 50', 'count <= 4']."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Optional derived row formulas."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Optional row filters."},
                "max_per_item": {"type": "integer", "description": "Max quantity per row (default: 1)."},
                "top_n": {"type": "integer", "description": "Top basket solutions to return (default: 5)."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "knapset_solve_sets",
        "description": "Synthesize optimal multi-slot combinations across tables/files (or execute a saved preset) under multi-resource limits and AST compatibility rules.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "description": "Optional v2 preset path or installed name to execute with overrides."},
                "profile": {"type": "string", "description": "Optional profile name inside preset."},
                "source": {"type": "string", "description": "Optional multi-table SQLite/JSON path (loads each table as a slot)."},
                "tables": {"type": "array", "items": {"type": "string"}, "description": "Optional table subset from source."},
                "slots": {"type": "object", "description": "Explicit mapping {slot_name: {source, table}} or {slot_name: [rows]}."},
                "weights": {"type": "string", "description": "Signed weights (supports cross-slot exprs like 'cpu.score/gpu.score:-0.2')."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Aggregate limits e.g. ['sum(price) <= 950']."},
                "rules": {"type": "array", "items": {"type": "string"}, "description": "Cross-slot AST rules e.g. ['cpu.socket == mobo.socket']."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Optional derived formulas."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Optional filters."},
                "top_n": {"type": "integer", "description": "Top sets to return (default: 5)."},
            },
        },
    },
    {
        "name": "knapset_list_presets",
        "description": "List locally discovered and installed Knapset v2 presets with their modes and available weight profiles.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _maybe_enrich_single(rows: List[Dict[str, Any]], args: Dict[str, Any]) -> List[Dict[str, Any]]:
    ref_source = args.get("ref_source")
    if not ref_source or not rows:
        return rows
    ref_rows = open_source(str(ref_source)).load_rows(table=args.get("ref_table"))
    match_expr = str(args.get("match") or "title~model")
    lf, rf, inferred = parse_match_expr(match_expr)
    spec = MatchSpec(
        left_field=lf,
        right_field=rf,
        strategy=inferred or "token",
        alias=str(args.get("alias") or "ref"),
        on_unmatched=str(args.get("on_unmatched") or "drop"),
    )
    return enrich_rows(rows, ref_rows, spec).rows


def handle_tool_call(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatches a Knapset v2 MCP tool call and returns structured JSON payload."""
    args = arguments or {}

    if name == "knapset_inspect_source":
        schema = inspect_source(str(args["source"]), sample_rows=int(args.get("sample_rows", 200)))
        return schema.to_dict()

    if name == "knapset_list_presets":
        presets = discover_presets()
        return {
            "presets": [
                {
                    "name": cfg.name,
                    "title": cfg.title,
                    "description": cfg.description,
                    "mode": cfg.mode,
                    "profiles": cfg.profile_names(),
                    "path": str(cfg.source_path) if cfg.source_path else "",
                }
                for cfg in presets.values()
            ]
        }

    if name == "knapset_rank_items":
        rows = open_source(str(args["source"])).load_rows(table=args.get("table"), query=args.get("query"))
        rows = _maybe_enrich_single(rows, args)
        ranked, meta = solve_rank(
            rows=rows,
            weights=args.get("weights") or "",
            formulas=args.get("formulas"),
            where=args.get("where"),
            top_n=int(args.get("top_n", 10)),
        )
        return {"meta": meta, "results": [r.to_dict() for r in ranked]}

    if name == "knapset_solve_basket":
        rows = open_source(str(args["source"])).load_rows(table=args.get("table"))
        rows = _maybe_enrich_single(rows, args)
        sols, meta = solve_basket(
            rows=rows,
            weights=args.get("weights") or "",
            limits=args.get("limits"),
            formulas=args.get("formulas"),
            where=args.get("where"),
            top_n=int(args.get("top_n", 5)),
            max_per_item=int(args.get("max_per_item", 1)),
        )
        return {"meta": meta, "solutions": [s.to_dict() for s in sols]}

    if name == "knapset_solve_sets":
        if args.get("preset") and not args.get("source") and not args.get("slots"):
            cfg = resolve_preset(str(args["preset"]))
            return cfg.execute(
                profile_name=args.get("profile"),
                prefer_override=args.get("weights") or None,
                limits_override=args.get("limits") or None,
                where_override=args.get("where") or None,
                formulas_override=args.get("formulas") or None,
                rules_override=args.get("rules") or None,
                top_n=args.get("top_n"),
            )

        slot_pools: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(args.get("slots"), dict):
            for sname, scfg in args["slots"].items():
                if isinstance(scfg, list):
                    slot_pools[str(sname)] = list(scfg)
                elif isinstance(scfg, dict) and scfg.get("source"):
                    slot_pools[str(sname)] = open_source(scfg["source"]).load_rows(table=scfg.get("table"))
        elif args.get("source"):
            src = open_source(str(args["source"]))
            wanted = args.get("tables") or src.inspect().table_names()
            for tname in wanted:
                slot_pools[str(tname)] = src.load_rows(table=str(tname))
        else:
            raise ValueError("Provide 'preset', 'source', or 'slots' for knapset_solve_sets.")

        sols, meta = solve_sets(
            slot_pools=slot_pools,
            weights=args.get("weights") or "",
            limits=args.get("limits"),
            rules=args.get("rules"),
            formulas=args.get("formulas"),
            where=args.get("where"),
            top_n=int(args.get("top_n", 5)),
        )
        return {"meta": meta, "solutions": [s.to_dict() for s in sols]}

    raise KeyError(f"Unknown MCP tool: {name}")


def handle_jsonrpc_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Processes a single JSON-RPC 2.0 request or notification."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no `id` and expect no reply
    if method == "notifications/initialized" or msg_id is None and str(method).startswith("notifications/"):
        return None

    def _ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return _ok(
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        )
    if method == "ping":
        return _ok({})
    if method == "tools/list":
        return _ok({"tools": MCP_TOOLS})
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            capture = io.StringIO()
            with redirect_stdout(capture):
                result = handle_tool_call(tool_name, arguments)
            return _ok(
                {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                    "isError": False,
                }
            )
        except Exception as exc:
            return _ok(
                {
                    "content": [{"type": "text", "text": f"Error executing '{tool_name}': {exc}"}],
                    "isError": True,
                }
            )
    return _err(-32601, f"Method not found: {method}")


def run_stdio_server() -> None:
    """Runs newline-delimited JSON-RPC 2.0 MCP loop on stdin/stdout."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue
        reply = handle_jsonrpc_message(req)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def run_mcp_cli(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} mcp",
        description="Start Knapset v2 Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio.",
    )
    parser.add_argument("--list-tools", action="store_true", help="Print registered MCP tool schemas and exit")
    args = parser.parse_args(argv)

    if args.list_tools:
        print(json.dumps({"server": SERVER_INFO, "tools": MCP_TOOLS}, indent=2))
        return
    run_stdio_server()


if __name__ == "__main__":
    run_mcp_cli(sys.argv[1:])
