#!/usr/bin/env python3
"""
Knapset Built-in Model Context Protocol (MCP) Server.

Zero-dependency JSON-RPC 2.0 server over stdio (`stdin` / `stdout`) that exposes
Knapset's combinatorial optimization, market search, domain inspection, and
prebuilt listing evaluation tools to AI agents (Claude Desktop, Cursor, Antigravity, etc.).

Usage:
  python3 main.py mcp
  python3 main.py mcp --list-tools
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional

from currency import format_price, get_active_currency, parse_rate_string, set_active_currency
from extension_registry import get_extension, list_domain_ids, list_extensions, rescan_extensions

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "knapset-mcp",
    "version": "1.0.2",
}

MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "knapset_list_domains",
        "description": "List all registered domain extensions, their structural sections, compatibility rules, and optimization modes.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "knapset_build_set",
        "description": "Synthesize optimal constraint-checked multi-component sets/builds under a target budget for a specified domain (e.g. 'template' or any installed extension).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain extension ID (e.g. 'template' or any installed extension).",
                },
                "budget": {
                    "type": "number",
                    "description": "Maximum total budget in the active currency (default: the domain layout's default budget).",
                },
                "mode": {
                    "type": "string",
                    "description": "Optimization mode profile (see the domain's modes).",
                },
                "currency": {
                    "type": "string",
                    "description": "Target currency code (e.g. 'USDT', 'USD', 'UAH', 'EUR'). Default: USDT.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Maximum number of top combinations to return (default: 3).",
                },
            },
            "required": ["domain"],
        },
    },
    {
        "name": "knapset_search_section",
        "description": "Search and score individual items within a domain section (see knapset_list_domains for section ids) with optional query, price bounds, and criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain extension ID (e.g. 'template' or any installed extension).",
                },
                "section": {
                    "type": "string",
                    "description": "Section ID within the domain layout.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional text search query (free text matched against listing titles).",
                },
                "max_price": {
                    "type": "number",
                    "description": "Optional maximum price in active currency.",
                },
                "mode": {
                    "type": "string",
                    "description": "Optional optimization mode to rank items.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum items to return (default: 10).",
                },
                "currency": {
                    "type": "string",
                    "description": "Target currency code (default: USDT).",
                },
            },
            "required": ["domain", "section"],
        },
    },
    {
        "name": "knapset_price_drops",
        "description": "Query the SQLite MarketStore for verified historical seller price drops or inspect chronological price history for a specific offer ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Optional domain filter.",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category/section filter.",
                },
                "provider": {
                    "type": "string",
                    "description": "Optional marketplace provider filter (e.g. 'olx', 'telemart').",
                },
                "offer_id": {
                    "type": "string",
                    "description": "Optional specific offer ID to inspect full chronological price history.",
                },
                "min_drop_pct": {
                    "type": "number",
                    "description": "Minimum price drop percentage from historical peak (default: 3.0).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of price drop items to return (default: 15).",
                },
                "currency": {
                    "type": "string",
                    "description": "Target currency code for formatted prices (default: USDT).",
                },
            },
        },
    },
]


def get_all_mcp_tools() -> List[Dict[str, Any]]:
    """Returns core MCP tools combined with any tools contributed by installed domain extensions."""
    rescan_extensions()
    tools = list(MCP_TOOLS)
    seen = {t["name"] for t in tools}
    for ext in list_extensions().values():
        if hasattr(ext, "get_mcp_tools"):
            for ext_tool in (ext.get_mcp_tools() or []):
                if isinstance(ext_tool, dict) and ext_tool.get("name") and ext_tool["name"] not in seen:
                    seen.add(ext_tool["name"])
                    tools.append(ext_tool)
    return tools


def execute_mcp_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Executes an MCP tool call and returns an MCP CallToolResult payload."""
    args = arguments or {}
    if "currency" in args and args["currency"]:
        set_active_currency(str(args["currency"]))

    rescan_extensions()

    try:
        if name == "knapset_list_domains":
            exts = list_extensions()
            payload = []
            for d_id, ext in exts.items():
                layout = ext.get_layout()
                w_reg = ext.get_weights_registry()
                payload.append({
                    "domain_id": d_id,
                    "title": ext.title,
                    "description": ext.description,
                    "sections": [
                        {"id": s.id, "name": s.name, "label": s.label, "required": s.required}
                        for s in (layout.sections if layout else [])
                    ],
                    "compatibility_rules": [
                        {"name": r.name, "description": r.description, "sections_involved": list(r.sections_involved)}
                        for r in (layout.compatibility_rules if layout else [])
                    ],
                    "modes": list(w_reg.list_profiles().keys()) if w_reg else [],
                })
            return {
                "content": [{"type": "text", "text": json.dumps({"domains": payload}, indent=2, ensure_ascii=False)}],
                "isError": False,
            }

        elif name == "knapset_build_set":
            domain = str(args.get("domain", "")).lower().strip()
            if not domain:
                raise ValueError("Missing required parameter 'domain'.")
            ext = get_extension(domain, fallback=False)
            layout = ext.get_layout()
            w_reg = ext.get_weights_registry()
            scorer = ext.get_scorer()
            sorter = ext.get_sorter()

            curr = get_active_currency()
            budget = float(args.get("budget") or (layout.default_budget if layout else 600.0))
            mode = str(args.get("mode") or (w_reg.default_profile_name if w_reg else "best"))
            top_n = max(1, min(10, int(args.get("top_n", 3))))

            profile = w_reg.resolve_profile(mode)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                candidates = ext.load_candidates(budget=budget)
                sets = sorter.sort_and_synthesize(
                    layout=layout,
                    scorer=scorer,
                    profile=profile,
                    candidate_pool=candidates,
                    budget=budget,
                    top_n=top_n,
                )

            formatted_sets = []
            for idx, s in enumerate(sets, 1):
                comps_out = {}
                for sec_id, c in (s.get("components") or {}).items():
                    c_spec = c.get("spec") or {}
                    c_eval = c.get("eval") or c.get("heuristic") or {}
                    confirm_flag = bool(
                        c.get("confirm_with_seller")
                        or c_eval.get("confirm_with_seller")
                        or "confirm with seller" in str(c_eval.get("warnings", [])).lower()
                        or str(c_spec.get("form_factor") or c.get("form_factor") or "").upper() == "UNKNOWN"
                    )
                    comps_out[sec_id] = {
                        "model": c.get("model") or c.get("name") or c.get("title", "Unknown"),
                        "price": c.get("price", 0.0),
                        "formatted_price": format_price(float(c.get("price", 0.0)), curr),
                        "discount_pct": c.get("discount_pct", 0.0),
                        "url": c.get("url") or (c.get("summary") or {}).get("url", ""),
                        "risk_score": int(c_eval.get("risk_score", 0)),
                        "confirm_with_seller": confirm_flag,
                    }
                formatted_sets.append({
                    "rank": idx,
                    "score": round(float(s.get("set_score", s.get("score", 0.0))), 2),
                    "total_cost": round(float(s.get("total_cost", 0.0)), 2),
                    "formatted_total": format_price(float(s.get("total_cost", 0.0)), curr),
                    "surplus": round(float(s.get("surplus", budget - float(s.get("total_cost", 0.0)))), 2),
                    "components": comps_out,
                })

            result_obj = {
                "domain": domain,
                "mode": profile.name,
                "currency": curr,
                "budget": budget,
                "count": len(formatted_sets),
                "sets": formatted_sets,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(result_obj, indent=2, ensure_ascii=False)}],
                "isError": False,
            }

        elif name == "knapset_search_section":
            domain = str(args.get("domain", "")).lower().strip()
            section = str(args.get("section", "")).lower().strip()
            ext = get_extension(domain, fallback=False)
            layout = ext.get_layout()
            valid_sections = set(layout.section_ids()) if layout else set()
            if valid_sections and section not in valid_sections:
                raise ValueError(
                    f"Invalid section '{section}' for domain '{domain}'. Valid sections: {sorted(list(valid_sections))}"
                )
            limit = max(1, min(50, int(args.get("limit", 10))))

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                results = ext.search(
                    section_id=section,
                    query=args.get("query"),
                    max_price=float(args["max_price"]) if args.get("max_price") is not None else None,
                    mode=args.get("mode"),
                    limit=limit,
                )

            curr = get_active_currency()
            clean_items = []
            for item in results[:limit]:
                clean_items.append({
                    "id": item.get("id"),
                    "model": item.get("model") or item.get("name") or item.get("title", ""),
                    "title": item.get("title") or (item.get("summary") or {}).get("title", ""),
                    "price": item.get("price", 0.0),
                    "formatted_price": format_price(float(item.get("price", 0.0)), curr),
                    "score": item.get("mode_score", item.get("score", 0.0)),
                    "discount_pct": item.get("discount_pct", 0.0),
                    "url": item.get("url") or (item.get("summary") or {}).get("url", ""),
                })

            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"domain": domain, "section": section, "currency": curr, "results": clean_items},
                            indent=2,
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": False,
            }

        elif name == "knapset_price_drops":
            from currency import canonical_currency, convert_currency
            from market_store import market_store

            curr = get_active_currency()
            offer_id = args.get("offer_id")
            prov = str(args.get("provider") or "olx").lower().strip()

            if offer_id:
                trend = market_store.get_price_trend(str(offer_id), provider=prov)
                history = market_store.get_price_history(str(offer_id), provider=prov)
                payload_out = {
                    "offer_id": str(offer_id),
                    "provider": prov,
                    "currency": curr,
                    "trend": trend,
                    "history": history,
                }
                return {
                    "content": [{"type": "text", "text": json.dumps(payload_out, indent=2, ensure_ascii=False)}],
                    "isError": False,
                }

            min_drop = float(args.get("min_drop_pct", 3.0))
            limit = max(1, min(100, int(args.get("limit", 15))))
            drops = market_store.find_price_drops(
                category=args.get("category"),
                provider=args.get("provider"),
                domain=args.get("domain"),
                min_drop_pct=min_drop,
                limit=limit,
            )
            formatted_drops = []
            for d in drops:
                raw_c = d.get("currency") or "UAH"
                peak_conv = convert_currency(float(d["peak_price"]), raw_c, curr) if canonical_currency(raw_c) != canonical_currency(curr) else float(d["peak_price"])
                curr_conv = convert_currency(float(d["current_price"]), raw_c, curr) if canonical_currency(raw_c) != canonical_currency(curr) else float(d["current_price"])
                entry_out = dict(d)
                entry_out["formatted_peak_price"] = format_price(peak_conv, curr)
                entry_out["formatted_current_price"] = format_price(curr_conv, curr)
                formatted_drops.append(entry_out)

            payload_out = {
                "currency": curr,
                "stats": market_store.stats(domain=args.get("domain")),
                "count": len(formatted_drops),
                "drops": formatted_drops,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload_out, indent=2, ensure_ascii=False)}],
                "isError": False,
            }

        else:
            for ext in list_extensions().values():
                if hasattr(ext, "execute_mcp_tool"):
                    ext_res = ext.execute_mcp_tool(name, args)
                    if ext_res is not None:
                        return ext_res
            return {
                "content": [{"type": "text", "text": f"Unknown MCP tool: '{name}'"}],
                "isError": True,
            }
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"Tool execution error in '{name}': {exc}"}],
            "isError": True,
        }


def handle_jsonrpc_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Processes a single JSON-RPC 2.0 request/notification and returns the response dict (or None for notifications)."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications (no id) do not expect a response
    if req_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": get_all_mcp_tools()},
        }

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments") or {}
        tool_res = execute_mcp_tool(tool_name, tool_args)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": tool_res,
        }

    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }
    return None


def run_mcp_stdio_server(argv: Optional[List[str]] = None) -> None:
    from app_paths import get_cli_prog

    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} mcp",
        description="Run Knapset Model Context Protocol (MCP) JSON-RPC 2.0 Server over stdio.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print available MCP tools schema as JSON and exit",
    )
    args = parser.parse_args(argv or [])

    if args.list_tools:
        print(json.dumps({"server": SERVER_INFO, "tools": get_all_mcp_tools()}, indent=2, ensure_ascii=False))
        return

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_jsonrpc_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_mcp_stdio_server()
