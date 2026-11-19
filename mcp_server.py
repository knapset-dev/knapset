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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app_paths import get_cli_prog
from enricher import collect_ref_specs, compare_sources, enrich_slot_pools
from formula_engine import parse_weights
from marketplace.client import MarketplaceClient
from preset_loader import (
    delete_shared_profile,
    discover_presets,
    list_shared_profiles,
    read_profile_file,
    resolve_prefs_for_pools,
    resolve_preset,
    resolve_shared_profile,
    save_shared_profile,
)
from solver_engine import analyze_sensitivity, solve_basket, solve_rank, solve_sets
from sources.registry import inspect_source, open_source
from theme_config import (
    discover_themes,
    load_active_theme,
    reset_active_theme,
    set_active_theme,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "knapset-mcp",
    "version": "2.0.0",
}

_REF_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "Reference DB/file path."},
        "table": {"type": "string", "description": "Optional table inside Reference DB."},
        "query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query."},
        "match": {"type": "string", "description": "Match rule e.g. 'title~model' (token) or 'sku=id' (exact)."},
        "strategy": {"type": "string", "enum": ["exact", "token", "regex", "fuzzy"], "description": "Match strategy override."},
        "alias": {"type": "string", "description": "Attached reference field alias (default: 'ref')."},
        "on_unmatched": {"type": "string", "enum": ["drop", "keep"], "description": "Unmatched policy (default: 'drop')."},
        "slot": {"type": "string", "description": "Optional target slot name (for multi-slot solve)."},
    },
    "required": ["source"],
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
        "name": "knapset_compare_sources",
        "description": "Compare a primary data source against a Reference DB and report match rate, unmatched samples, and enriched preview rows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Primary candidate data source path/URI."},
                "table": {"type": "string", "description": "Optional primary table/collection name."},
                "query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query on primary SQLite source."},
                "ref_source": {"type": "string", "description": "Reference DB/file path/URI."},
                "ref_table": {"type": "string", "description": "Optional Reference DB table/collection name."},
                "ref_query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query on Reference SQLite source."},
                "match": {"type": "string", "description": "Match rule e.g. 'title~model' (token) or 'sku=id' (exact). Inferred if omitted."},
                "strategy": {"type": "string", "enum": ["exact", "token", "regex", "fuzzy"], "description": "Matching strategy override."},
                "alias": {"type": "string", "description": "Attached field alias (default: 'ref')."},
                "on_unmatched": {"type": "string", "enum": ["keep", "drop"], "description": "Keep or drop unmatched rows (default: 'keep')."},
                "limit": {"type": "integer", "description": "Max preview rows to return (default: 15)."},
            },
            "required": ["source", "ref_source"],
        },
    },
    {
        "name": "knapset_rank_items",
        "description": "Rank rows from a table/file using signed weights (positive maximizes, negative minimizes), optional Reference DB enrichment, saved prefs, derived formulas, and filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Primary data source path/URI."},
                "table": {"type": "string", "description": "Optional table/collection name."},
                "query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query (SQLite only)."},
                "ref_source": {"type": "string", "description": "Optional Reference DB path/URI to enrich rows."},
                "ref_table": {"type": "string", "description": "Optional table inside Reference DB."},
                "match": {"type": "string", "description": "Match rule e.g. 'title~model' (token) or 'sku=id' (exact)."},
                "strategy": {"type": "string", "enum": ["exact", "token", "regex", "fuzzy"], "description": "Match strategy override."},
                "alias": {"type": "string", "description": "Attached reference field alias (default: 'ref')."},
                "on_unmatched": {"type": "string", "enum": ["drop", "keep"], "description": "Policy for unmatched rows (default: 'drop')."},
                "references": {"type": "array", "items": _REF_ITEM_SCHEMA, "description": "Optional list of Reference DB specs."},
                "prefs": {"type": "string", "description": "Saved preference profile name or profile/preset file path; explicit args override."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Derived formulas e.g. ['deal = (ref.msrp - price) / ref.msrp']."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Filter predicates e.g. ['price <= 600']."},
                "weights": {"type": "string", "description": "Signed weights e.g. 'ref.score:+0.7, price:-0.3'."},
                "norm": {"type": "string", "enum": ["robust", "minmax", "rank"], "description": "Normalization strategy (default: 'robust')."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Exclude matching rows by title/name/id substring."},
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
                "query": {"type": "string", "description": "Optional read-only SQL SELECT/WITH query (SQLite only)."},
                "ref_source": {"type": "string", "description": "Optional Reference DB path/URI to enrich rows."},
                "ref_table": {"type": "string", "description": "Optional table inside Reference DB."},
                "match": {"type": "string", "description": "Match rule e.g. 'title~model' (token) or 'sku=id' (exact)."},
                "strategy": {"type": "string", "enum": ["exact", "token", "regex", "fuzzy"], "description": "Match strategy override."},
                "alias": {"type": "string", "description": "Attached reference field alias (default: 'ref')."},
                "on_unmatched": {"type": "string", "enum": ["drop", "keep"], "description": "Policy for unmatched rows (default: 'drop')."},
                "references": {"type": "array", "items": _REF_ITEM_SCHEMA, "description": "Optional list of Reference DB specs."},
                "prefs": {"type": "string", "description": "Saved preference profile name or profile/preset file path; explicit args override."},
                "weights": {"type": "string", "description": "Signed weights e.g. 'protein:+0.8, price:-0.2'."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Limits e.g. ['sum(price) <= 50', 'count <= 4']."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Optional derived row formulas."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Optional row filters."},
                "max_per_item": {"type": "integer", "description": "Max quantity per row (default: 1)."},
                "qty_field": {"type": "string", "description": "Optional row field capping per-item quantity."},
                "norm": {"type": "string", "enum": ["robust", "minmax", "rank"], "description": "Normalization strategy (default: 'robust')."},
                "pin": {"type": "array", "items": {"type": "string"}, "description": "Force matching item(s) into every basket."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Exclude matching rows by title/name/id substring."},
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
                "mode": {"type": "string", "enum": ["rank", "basket", "solve"], "description": "Optional mode override when running a preset."},
                "source": {"type": "string", "description": "Optional multi-table SQLite/JSON path (loads each table as a slot)."},
                "tables": {"type": "array", "items": {"type": "string"}, "description": "Optional table subset from source."},
                "slots": {"type": "object", "description": "Explicit mapping {slot_name: {source, table, query}} or {slot_name: [rows]}."},
                "ref_source": {"type": "string", "description": "Optional Reference DB path/URI to enrich slots."},
                "ref_table": {"type": "string", "description": "Optional table inside Reference DB."},
                "match": {"type": "string", "description": "Match rule e.g. 'title~model' or 'sku=id'."},
                "strategy": {"type": "string", "enum": ["exact", "token", "regex", "fuzzy"], "description": "Match strategy override."},
                "alias": {"type": "string", "description": "Attached reference field alias (default: 'ref')."},
                "on_unmatched": {"type": "string", "enum": ["drop", "keep"], "description": "Policy for unmatched rows (default: 'drop')."},
                "references": {"type": "array", "items": _REF_ITEM_SCHEMA, "description": "Optional list of Reference DB specs."},
                "prefs": {"type": "string", "description": "Saved preference profile name or profile/preset file path; explicit args override."},
                "weights": {"type": "string", "description": "Signed weights (supports cross-slot exprs like 'cpu.score/gpu.score:-0.2')."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Aggregate limits e.g. ['sum(price) <= 950']."},
                "rules": {"type": "array", "items": {"type": "string"}, "description": "Cross-slot AST rules e.g. ['cpu.socket == mobo.socket']."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Optional derived formulas."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Optional filters."},
                "norm": {"type": "string", "enum": ["robust", "minmax", "rank"], "description": "Normalization strategy (default: 'robust')."},
                "diverse": {"type": "boolean", "description": "Diversify Top-N multi-slot combinations."},
                "pin": {"type": "object", "description": "Lock specific slots e.g. {'cpu': 'Ryzen 5 7600'}."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Exclude matching rows e.g. ['RTX 3050', 'gpu:GT 1030']."},
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
    {
        "name": "knapset_prefs",
        "description": "Manage saved DB-independent preference profiles (list, show, save, import, export, remove).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "show", "save", "import", "export", "remove"],
                    "description": "Preference library action.",
                },
                "name": {"type": "string", "description": "Profile name (for show, save, import, export, remove)."},
                "file": {"type": "string", "description": "File path (for import or export)."},
                "profile": {"type": "string", "description": "Source profile name when importing from a multi-profile preset."},
                "mode": {"type": "string", "enum": ["rank", "basket", "solve"], "description": "Default mode for save."},
                "weights": {"type": "string", "description": "Signed weights e.g. 'score:+0.7, price:-0.3' (for save)."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Derived formulas (for save)."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Filter predicates (for save)."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Aggregate limits (for save)."},
                "rules": {"type": "array", "items": {"type": "string"}, "description": "Cross-slot rules (for save)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "knapset_registry",
        "description": "Search, inspect, install, verify, or remove knapset-registry presets/themes and manage active TUI/Studio themes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search",
                        "info",
                        "install",
                        "list",
                        "remove",
                        "verify",
                        "theme_list",
                        "theme_show",
                        "theme_use",
                        "theme_reset",
                    ],
                    "description": "Registry or theme action.",
                },
                "target": {"type": "string", "description": "Registry item/bundle/theme name (e.g. 'pc_build', 'theme:nord', 'bundle:starter-presets')."},
                "query": {"type": "string", "description": "Search filter query (for search)."},
                "item_type": {"type": "string", "description": "Optional subsystem filter e.g. 'presets', 'themes' (for search)."},
                "refresh": {"type": "boolean", "description": "Force refresh registry index."},
                "use": {"type": "boolean", "description": "Activate theme immediately after install."},
                "force": {"type": "boolean", "description": "Overwrite or force remove."},
                "registry": {"type": "string", "description": "Optional local registry path or URL override."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "knapset_sensitivity",
        "description": "Test #1 winner stability under weight perturbations and compute marginal shadow gain of relaxing limits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "description": "Optional preset name or path."},
                "profile": {"type": "string", "description": "Optional preset profile name."},
                "source": {"type": "string", "description": "Primary source path/URI (if not using a preset)."},
                "table": {"type": "string", "description": "Optional table name."},
                "mode": {"type": "string", "enum": ["rank", "basket", "solve"], "description": "Optimization mode."},
                "weights": {"type": "string", "description": "Signed weights."},
                "limits": {"type": "array", "items": {"type": "string"}, "description": "Aggregate limits."},
                "rules": {"type": "array", "items": {"type": "string"}, "description": "Cross-slot rules."},
                "formulas": {"type": "array", "items": {"type": "string"}, "description": "Derived formulas."},
                "where": {"type": "array", "items": {"type": "string"}, "description": "Row filters."},
                "norm": {"type": "string", "enum": ["robust", "minmax", "rank"], "description": "Normalization strategy."},
                "weight_delta": {"type": "number", "description": "Relative weight perturbation (default: 0.15)."},
                "limit_delta": {"type": "number", "description": "Relative limit perturbation (default: 0.10)."},
            },
        },
    },
]


def _handle_prefs_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    lib = list_shared_profiles()

    if action == "list":
        return {"profiles": lib}
    if action == "show":
        name = str(args.get("name") or "")
        _, data = resolve_shared_profile(name)
        return {"name": name.strip().lower(), "profile": data}
    if action == "save":
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("'name' is required for prefs save.")
        weights_raw = args.get("weights") or ""
        weights = {t.expression: t.weight for t in parse_weights(weights_raw)} if weights_raw else {}
        data = {
            "mode": args.get("mode") or "rank",
            "weights": weights,
            "formulas": list(args.get("formulas") or []),
            "where": list(args.get("where") or []),
            "limits": list(args.get("limits") or []),
            "rules": list(args.get("rules") or []),
        }
        path = save_shared_profile(name, data)
        return {"saved": name.lower(), "path": str(path), "profile": data}
    if action == "import":
        file_path = str(args.get("file") or "").strip()
        if not file_path:
            raise ValueError("'file' is required for prefs import.")
        parsed_name, data = read_profile_file(file_path, args.get("profile"))
        target_name = str(args.get("name") or parsed_name)
        path = save_shared_profile(target_name, data)
        return {"imported": target_name.lower(), "path": str(path), "profile": data}
    if action == "export":
        name = str(args.get("name") or "").strip()
        file_path = str(args.get("file") or "").strip()
        if not name or not file_path:
            raise ValueError("'name' and 'file' are required for prefs export.")
        _, data = resolve_shared_profile(name)
        target = Path(file_path).expanduser().resolve()
        out = save_shared_profile(name, data, directory=target.parent)
        if out != target:
            out.replace(target)
        return {"exported": name.lower(), "path": str(target)}
    if action == "remove":
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("'name' is required for prefs remove.")
        removed = delete_shared_profile(name)
        return {"removed": name.lower(), "path": str(removed)}
    raise ValueError(f"Unsupported prefs action: '{action}'")


def _handle_registry_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action == "theme_list":
        active = load_active_theme()
        themes = discover_themes()
        return {
            "active": active.name,
            "themes": [
                {
                    "name": t.name,
                    "title": t.title,
                    "description": t.description,
                    "active": t.name == active.name,
                    "colors": t.colors,
                }
                for t in themes.values()
            ],
        }
    if action == "theme_show":
        active = load_active_theme()
        return {"theme": active.to_dict()}
    if action == "theme_use":
        target = str(args.get("target") or "").strip()
        if not target:
            raise ValueError("'target' theme name or path is required for theme_use.")
        theme, written = set_active_theme(target)
        return {"active": theme.name, "path": str(written), "theme": theme.to_dict()}
    if action == "theme_reset":
        theme, written = reset_active_theme()
        return {"active": theme.name, "path": str(written), "theme": theme.to_dict()}

    client = MarketplaceClient(registry_url=args.get("registry"))
    if action == "search":
        res = client.search(
            query=str(args.get("query") or ""),
            subsystem=args.get("item_type"),
            force_refresh=bool(args.get("refresh", False)),
        )
        return res
    if action == "info":
        target = str(args.get("target") or "").strip()
        if not target:
            raise ValueError("'target' is required for registry info.")
        info = client.info(target, force_refresh=bool(args.get("refresh", False)))
        if info is None:
            raise KeyError(f"Registry item '{target}' not found.")
        return info
    if action == "install":
        target = str(args.get("target") or "").strip()
        if not target:
            raise ValueError("'target' is required for registry install.")
        ok, msg = client.install(
            target,
            force=bool(args.get("force", False)),
            force_refresh=bool(args.get("refresh", False)),
        )
        out: Dict[str, Any] = {"ok": ok, "message": msg}
        if ok and args.get("use"):
            clean = target.split(":", 1)[-1]
            if clean in discover_themes():
                activated, _ = set_active_theme(clean)
                out["active_theme"] = activated.name
        return out
    if action == "list":
        return {"installed": client.list_installed()}
    if action == "remove":
        target = str(args.get("target") or "").strip()
        if not target:
            raise ValueError("'target' is required for registry remove.")
        ok, msg = client.remove(target, force=bool(args.get("force", False)))
        return {"ok": ok, "message": msg}
    if action == "verify":
        return {"report": client.verify(args.get("target"))}
    raise ValueError(f"Unsupported registry action: '{action}'")


def handle_tool_call(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatches a Knapset v2 MCP tool call and returns structured JSON payload."""
    args = arguments or {}

    if name == "knapset_inspect_source":
        schema = inspect_source(str(args["source"]), sample_rows=int(args.get("sample_rows", 200)))
        return schema.to_dict()

    if name == "knapset_compare_sources":
        return compare_sources(
            str(args["source"]),
            str(args["ref_source"]),
            table=args.get("table"),
            query=args.get("query"),
            ref_table=args.get("ref_table"),
            ref_query=args.get("ref_query"),
            match=args.get("match"),
            strategy=args.get("strategy"),
            alias=str(args.get("alias") or "ref"),
            on_unmatched=str(args.get("on_unmatched") or "keep"),
            limit=int(args.get("limit", 15)),
        )

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

    if name == "knapset_prefs":
        return _handle_prefs_tool(args)

    if name == "knapset_registry":
        return _handle_registry_tool(args)

    if name == "knapset_rank_items":
        rows = open_source(str(args["source"])).load_rows(table=args.get("table"), query=args.get("query"))
        pools = {"_pool": rows}
        ref_specs = collect_ref_specs(args)
        if ref_specs:
            enrich_slot_pools(pools, ref_specs)
        merged, skipped = resolve_prefs_for_pools(args, pools)
        ranked, meta = solve_rank(
            rows=pools["_pool"],
            weights=merged["weights"] or "",
            formulas=merged["formulas"],
            where=merged["where"],
            top_n=int(args.get("top_n", 10)),
            norm=merged.get("norm") or "robust",
            exclude=args.get("exclude"),
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"meta": meta, "results": [r.to_dict() for r in ranked]}

    if name == "knapset_solve_basket":
        rows = open_source(str(args["source"])).load_rows(table=args.get("table"), query=args.get("query"))
        pools = {"_pool": rows}
        ref_specs = collect_ref_specs(args)
        if ref_specs:
            enrich_slot_pools(pools, ref_specs)
        merged, skipped = resolve_prefs_for_pools(args, pools)
        sols, meta = solve_basket(
            rows=pools["_pool"],
            weights=merged["weights"] or "",
            limits=merged["limits"],
            formulas=merged["formulas"],
            where=merged["where"],
            top_n=int(args.get("top_n", 5)),
            max_per_item=int(args.get("max_per_item", 1)),
            qty_field=args.get("qty_field"),
            norm=merged.get("norm") or "robust",
            pin=args.get("pin"),
            exclude=args.get("exclude"),
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"meta": meta, "solutions": [s.to_dict() for s in sols]}

    if name == "knapset_solve_sets":
        if args.get("preset") and not args.get("source") and not args.get("slots"):
            cfg = resolve_preset(str(args["preset"]))
            return cfg.execute(
                profile_name=args.get("profile"),
                mode_override=args.get("mode") or None,
                prefer_override=args.get("weights") or None,
                limits_override=args.get("limits") or None,
                where_override=args.get("where") or None,
                formulas_override=args.get("formulas") or None,
                rules_override=args.get("rules") or None,
                top_n=args.get("top_n"),
                norm_override=args.get("norm"),
                diverse_override=args.get("diverse") if "diverse" in args else None,
                pin_override=args.get("pin"),
                exclude_override=args.get("exclude"),
            )

        slot_pools: Dict[str, List[Dict[str, Any]]] = {}
        if isinstance(args.get("slots"), dict):
            for sname, scfg in args["slots"].items():
                if isinstance(scfg, list):
                    slot_pools[str(sname)] = list(scfg)
                elif isinstance(scfg, dict) and scfg.get("source"):
                    slot_pools[str(sname)] = open_source(scfg["source"]).load_rows(
                        table=scfg.get("table"),
                        query=scfg.get("query"),
                    )
        elif args.get("source"):
            src = open_source(str(args["source"]))
            wanted = args.get("tables") or src.inspect().table_names()
            for tname in wanted:
                slot_pools[str(tname)] = src.load_rows(table=str(tname))
        else:
            raise ValueError("Provide 'preset', 'source', or 'slots' for knapset_solve_sets.")

        ref_specs = collect_ref_specs(args)
        if ref_specs:
            enrich_slot_pools(slot_pools, ref_specs)
        merged, skipped = resolve_prefs_for_pools(args, slot_pools)

        sols, meta = solve_sets(
            slot_pools=slot_pools,
            weights=merged["weights"] or "",
            limits=merged["limits"],
            rules=merged["rules"],
            formulas=merged["formulas"],
            where=merged["where"],
            top_n=int(args.get("top_n", 5)),
            norm=merged.get("norm") or "robust",
            diverse=bool(merged.get("diverse", False)),
            pin=args.get("pin"),
            exclude=args.get("exclude"),
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"meta": meta, "solutions": [s.to_dict() for s in sols]}

    if name == "knapset_sensitivity":
        if args.get("preset") and not args.get("source"):
            cfg = resolve_preset(str(args["preset"]))
            return cfg.sensitivity(
                profile_name=args.get("profile"),
                mode_override=args.get("mode") or None,
                prefer_override=args.get("weights") or None,
                limits_override=args.get("limits") or None,
                where_override=args.get("where") or None,
                formulas_override=args.get("formulas") or None,
                rules_override=args.get("rules") or None,
                norm_override=args.get("norm"),
                weight_delta=float(args.get("weight_delta", 0.15)),
                limit_delta=float(args.get("limit_delta", 0.10)),
            )
        if not args.get("source"):
            raise ValueError("Provide 'preset' or 'source' for knapset_sensitivity.")
        src = open_source(str(args["source"]))
        schema = src.inspect()
        eff_mode = str(args.get("mode") or ("solve" if len(schema.table_names()) > 1 and not args.get("table") else "rank")).lower()
        if eff_mode == "solve":
            pools = {t: src.load_rows(table=t) for t in schema.table_names()}
            return analyze_sensitivity(
                "solve",
                slot_pools=pools,
                weights=args.get("weights") or "",
                limits=args.get("limits"),
                rules=args.get("rules"),
                formulas=args.get("formulas"),
                where=args.get("where"),
                norm=args.get("norm") or "robust",
                weight_delta=float(args.get("weight_delta", 0.15)),
                limit_delta=float(args.get("limit_delta", 0.10)),
            )
        rows = src.load_rows(table=args.get("table"))
        return analyze_sensitivity(
            eff_mode,
            rows=rows,
            weights=args.get("weights") or "",
            limits=args.get("limits"),
            formulas=args.get("formulas"),
            where=args.get("where"),
            norm=args.get("norm") or "robust",
            weight_delta=float(args.get("weight_delta", 0.15)),
            limit_delta=float(args.get("limit_delta", 0.10)),
        )

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
