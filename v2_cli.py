"""
Knapset v2 CLI Handlers for Phase 1: `inspect`, `init`, and `compare`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from sources.base import SourceSchema, TableSchema
from sources.registry import open_source

_COST_LIKE_TOKENS = ("price", "cost", "fee", "weight", "mass", "watt", "tdp", "power", "latency", "ms", "risk", "calor")


def _is_cost_like(col_name: str) -> bool:
    low = col_name.lower()
    return any(tok in low for tok in _COST_LIKE_TOKENS)


def _infer_default_match(left_cols: List[str], right_cols: List[str]) -> Tuple[str, str, str]:
    """Picks a sensible default `(left_field, right_field, strategy)` when --match is omitted."""
    left_low = {c.lower(): c for c in left_cols}
    right_low = {c.lower(): c for c in right_cols}
    for common in ("id", "sku", "part_number", "code", "model", "name"):
        if common in left_low and common in right_low:
            strat = "exact" if common in ("id", "sku", "part_number", "code") else "token"
            return left_low[common], right_low[common], strat
    for lc in ("title", "name", "listing", "description", "label"):
        for rc in ("model", "name", "title", "pattern", "label"):
            if lc in left_low and rc in right_low:
                return left_low[lc], right_low[rc], "token"
    if left_cols and right_cols:
        return left_cols[0], right_cols[0], "token"
    raise ValueError("Could not infer match columns; specify --match '<left>~<right>' or '<left>=<right>'.")


def _build_starter_preset(
    schema: SourceSchema,
    mode: str = "solve",
    ref_path: Optional[str] = None,
    ref_schema: Optional[SourceSchema] = None,
    ref_table: Optional[str] = None,
    match_expr: Optional[str] = None,
) -> Dict[str, Any]:
    tables = schema.tables
    if mode == "solve" and len(tables) <= 1:
        chosen_mode = "rank"
    else:
        chosen_mode = mode

    sources_cfg: Dict[str, Any] = {}
    for tbl in tables:
        entry: Dict[str, Any] = {"source": Path(schema.location).name}
        if len(tables) > 1 or schema.kind == "sqlite":
            entry["table"] = tbl.name
        sources_cfg[tbl.name] = entry

    references_cfg: Dict[str, Any] = {}
    if ref_schema and ref_schema.tables:
        r_tbl = ref_schema.get_table(ref_table) if ref_table else ref_schema.tables[0]
        if r_tbl is None:
            raise ValueError(f"Reference table '{ref_table}' not found in '{ref_schema.location}'.")
        primary_tbl = tables[0] if tables else None
        if match_expr:
            lf, rf, strat = parse_match_expr(match_expr)
            strategy = strat or "token"
        else:
            lf, rf, strategy = _infer_default_match(
                primary_tbl.text_columns() if primary_tbl else [],
                r_tbl.text_columns(),
            )
        ref_key = f"{r_tbl.name}_ref"
        references_cfg[ref_key] = {
            "source": Path(ref_path or ref_schema.location).name,
            "table": r_tbl.name,
            "match": f"{lf}~{rf}" if strategy == "token" else f"{lf}={rf}",
            "strategy": strategy,
            "on_unmatched": "drop",
        }
        for slot_cfg in sources_cfg.values():
            slot_cfg["ref"] = ref_key

    # Suggest balanced weights from detected numeric columns
    weights: Dict[str, float] = {}
    limits: List[str] = []
    numeric_candidates: List[Tuple[str, Optional[float], Optional[float]]] = []
    for tbl in tables:
        prefix = f"{tbl.name}." if len(tables) > 1 else ""
        for col in tbl.columns:
            if col.kind == "number" and col.name.lower() not in ("id",):
                numeric_candidates.append((f"{prefix}{col.name}", col.min_val, col.max_val))

    picked = numeric_candidates[:4]
    if picked:
        each = round(1.0 / len(picked), 2)
        for col_ref, _, max_val in picked:
            sign = -1.0 if _is_cost_like(col_ref) else 1.0
            weights[col_ref] = round(sign * each, 2)
            if not limits and _is_cost_like(col_ref) and max_val is not None:
                base_col = col_ref.split(".")[-1]
                cap = round(max_val * (max(1, len(tables)) if chosen_mode == "solve" else 2), 2)
                limits.append(f"sum({base_col}) <= {cap:g}")

    preset: Dict[str, Any] = {
        "schema_version": "2.0",
        "name": Path(schema.location).stem.lower().replace(" ", "_"),
        "title": f"{Path(schema.location).stem} Preset",
        "description": f"Auto-generated v2 preset for {Path(schema.location).name}",
        "mode": chosen_mode,
        "sources": sources_cfg,
    }
    if references_cfg:
        preset["references"] = references_cfg
    preset["formulas"] = {}
    preset["limits"] = limits
    preset["rules"] = []
    preset["profiles"] = {
        "balanced": {
            "weights": weights,
        }
    }
    return preset


def run_inspect(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} inspect",
        description="Inspect tables, columns, inferred types, and value ranges in an existing DB/CSV/JSON source.",
    )
    parser.add_argument("source", help="Path or URI to SQLite (.db/.sqlite), .csv, .tsv, .json, or .jsonl")
    parser.add_argument("--table", "-t", default=None, help="Inspect only a specific table/collection")
    parser.add_argument("--sample", "-s", type=int, default=200, help="Rows to sample for type/range inference (default: 200)")
    parser.add_argument("--json", action="store_true", help="Output schema summary as JSON")
    args = parser.parse_args(argv)

    src = open_source(args.source)
    schema = src.inspect(sample_rows=args.sample)
    if args.table:
        tbl = schema.get_table(args.table)
        if tbl is None:
            print(
                f"Error: Table '{args.table}' not found in '{schema.location}'. Available: {', '.join(schema.table_names())}",
                file=sys.stderr,
            )
            sys.exit(1)
        schema = SourceSchema(location=schema.location, kind=schema.kind, tables=[tbl])

    if args.json:
        print(json.dumps(schema.to_dict(), indent=2, ensure_ascii=False))
        return

    print(f"\nSource: {schema.location} [{schema.kind}] ({len(schema.tables)} table(s))")
    print("═" * 84)
    if not schema.tables:
        print("  (No tables or rows found.)\n")
        return

    for tbl in schema.tables:
        print(f"\n  Table: {tbl.name} ({tbl.row_count:,} rows)")
        print("  ┌──────────────────────┬──────────┬──────────┬────────────────────────┬──────────────────────────┐")
        print(f"  │ {'Column':<20} │ {'Kind':<8} │ {'Non-Null':>8} │ {'Range':<22} │ {'Samples':<24} │")
        print("  ├──────────────────────┼──────────┼──────────┼────────────────────────┼──────────────────────────┤")
        for col in tbl.columns:
            rng = f"{col.min_val:g} .. {col.max_val:g}" if col.min_val is not None and col.max_val is not None else "—"
            samp = ", ".join(str(v) for v in col.sample_values)[:24]
            print(
                f"  │ {col.name[:20]:<20} │ {col.kind[:8]:<8} │ {col.non_null_count:>8,} │ {rng[:22]:<22} │ {samp:<24} │"
            )
        print("  └──────────────────────┴──────────┴──────────┴────────────────────────┴──────────────────────────┘")
    print()


def run_init(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} init",
        description="Scaffold a starter Knapset v2 JSON preset from an existing database or data file.",
    )
    parser.add_argument("source", help="Path or URI to primary SQLite/CSV/JSON data source")
    parser.add_argument("--out", "-o", default=None, help="Write preset JSON to file path (default: print to stdout)")
    parser.add_argument("--mode", "-m", choices=["solve", "basket", "rank"], default="solve", help="Default preset mode")
    parser.add_argument("--ref", default=None, help="Optional reference database/file to wire into the preset")
    parser.add_argument("--ref-table", default=None, help="Table name inside --ref")
    parser.add_argument("--match", default=None, help="Match expression for --ref (e.g. 'title~model' or 'id=id')")
    parser.add_argument("--force", "-f", action="store_true", help="Overwrite --out if it already exists")
    args = parser.parse_args(argv)

    schema = open_source(args.source).inspect()
    ref_schema = open_source(args.ref).inspect() if args.ref else None
    preset = _build_starter_preset(
        schema,
        mode=args.mode,
        ref_path=args.ref,
        ref_schema=ref_schema,
        ref_table=args.ref_table,
        match_expr=args.match,
    )
    rendered = json.dumps(preset, indent=2, ensure_ascii=False) + "\n"

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        if out_path.exists() and not args.force:
            print(f"Error: '{out_path}' already exists. Pass --force to overwrite.", file=sys.stderr)
            sys.exit(1)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"[OK] Wrote starter preset to {out_path}")
    else:
        print(rendered, end="")


def run_compare(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} compare",
        description="Match and compare rows from a primary source against a Reference DB.",
    )
    parser.add_argument("source", help="Primary candidate data source (.db, .csv, .json, .jsonl)")
    parser.add_argument("--table", "-t", default=None, help="Primary table/collection name")
    parser.add_argument("--query", "-q", default=None, help="Read-only SQL SELECT query for primary SQLite source")
    parser.add_argument("--ref", required=True, help="Reference data source (.db, .csv, .json, .jsonl)")
    parser.add_argument("--ref-table", default=None, help="Reference table/collection name")
    parser.add_argument("--ref-query", default=None, help="Read-only SQL SELECT query for reference SQLite source")
    parser.add_argument("--match", "-m", default=None, help="Match rule, e.g. 'title~model' (token) or 'sku=id' (exact)")
    parser.add_argument("--strategy", choices=["exact", "token", "regex"], default=None, help="Matching strategy override")
    parser.add_argument("--alias", default="ref", help="Field alias for attached reference attributes (default: ref)")
    parser.add_argument("--on-unmatched", choices=["keep", "drop"], default="keep", help="Keep or drop unmatched rows (default: keep)")
    parser.add_argument("--limit", "-n", type=int, default=15, help="Maximum rows to display (default: 15)")
    parser.add_argument("--json", action="store_true", help="Output match stats and enriched rows as JSON")
    args = parser.parse_args(argv)

    left_rows = open_source(args.source).load_rows(table=args.table, query=args.query)
    right_rows = open_source(args.ref).load_rows(table=args.ref_table, query=args.ref_query)

    if args.match:
        lf, rf, inferred_strat = parse_match_expr(args.match)
        strat = args.strategy or inferred_strat or "token"
    else:
        l_cols = list(left_rows[0].keys()) if left_rows else []
        r_cols = list(right_rows[0].keys()) if right_rows else []
        lf, rf, inferred_strat = _infer_default_match(l_cols, r_cols)
        strat = args.strategy or inferred_strat

    spec = MatchSpec(
        left_field=lf,
        right_field=rf,
        strategy=strat,
        alias=args.alias,
        on_unmatched=args.on_unmatched,
    )
    res = enrich_rows(left_rows, right_rows, spec)

    if args.json:
        payload = {
            "match": {
                "left_field": lf,
                "right_field": rf,
                "strategy": strat,
                "alias": spec.alias,
                "total": res.total,
                "matched": res.matched,
                "unmatched": res.unmatched,
                "match_rate": round(res.match_rate, 4),
                "unmatched_samples": res.unmatched_samples,
            },
            "rows": res.rows[: args.limit] if args.limit and args.limit > 0 else res.rows,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    pct = res.match_rate * 100.0
    print(f"\nCross-Database Comparison [{Path(args.source).name} -> {Path(args.ref).name}]")
    print("═" * 84)
    print(f"  Match rule   : {lf} ({strat}) -> {spec.alias}.{rf}")
    print(f"  Coverage     : {res.matched:,} / {res.total:,} matched ({pct:.1f}%), {res.unmatched:,} unmatched")
    if res.unmatched_samples:
        print(f"  Unmatched ex : {', '.join(res.unmatched_samples[:5])}")
    print("─" * 84)

    preview = res.rows[: args.limit] if args.limit and args.limit > 0 else res.rows
    for idx, row in enumerate(preview, 1):
        left_val = row.get(lf, "")
        ref_obj = row.get(spec.alias)
        if isinstance(ref_obj, dict):
            ref_preview = ", ".join(f"{k}={v}" for k, v in list(ref_obj.items())[:4])
            print(f"  {idx:>2}. [MATCH] {str(left_val)[:36]:<36} -> {spec.alias}({ref_preview})")
        else:
            print(f"  {idx:>2}. [MISS ] {str(left_val)[:36]:<36} -> (no match)")
    print()
