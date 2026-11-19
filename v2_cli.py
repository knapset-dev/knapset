"""
Knapset v2 CLI Handlers for Phase 1: `inspect`, `init`, and `compare`.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_from_source, enrich_rows, infer_default_match, parse_match_expr
from solver_engine import BasketSolution, RankedItem, SetSolution, solve_basket, solve_rank, solve_sets
from sources.base import SourceSchema, TableSchema
from sources.registry import open_source

_COST_LIKE_TOKENS = ("price", "cost", "fee", "weight", "mass", "watt", "tdp", "power", "latency", "ms", "risk", "calor")


def _is_cost_like(col_name: str) -> bool:
    low = col_name.lower()
    return any(tok in low for tok in _COST_LIKE_TOKENS)


def _infer_default_match(left_cols: List[str], right_cols: List[str]) -> Tuple[str, str, str]:
    """Picks a sensible default `(left_field, right_field, strategy)` when --match is omitted."""
    return infer_default_match(left_cols, right_cols)


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
    parser.add_argument("--strategy", choices=["exact", "token", "regex", "fuzzy"], default=None, help="Matching strategy override")
    parser.add_argument("--threshold", type=float, default=0.78, help="Fuzzy match similarity threshold (default: 0.78)")
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
        threshold=args.threshold,
    )
    res = enrich_rows(left_rows, right_rows, spec, include_provenance=True)

    if args.json:
        payload = {
            "match": {
                "left_field": lf,
                "right_field": rf,
                "strategy": strat,
                "alias": spec.alias,
                "threshold": spec.threshold,
                "total": res.total,
                "matched": res.matched,
                "unmatched": res.unmatched,
                "match_rate": round(res.match_rate, 4),
                "method_counts": dict(res.method_counts),
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


def _parse_ref_spec(
    raw_ref: str,
    default_match: Optional[str],
    default_strategy: Optional[str],
    default_alias: str,
    default_on_unmatched: str,
) -> Dict[str, Any]:
    """
    Parses `--ref <path>` or `--ref <path>:match=a~b,alias=ref,table=t,slot=gpu,strategy=token,on_unmatched=drop`.
    """
    path_part = raw_ref
    opts: Dict[str, str] = {}
    if ":" in raw_ref:
        lhs, rhs = raw_ref.rsplit(":", 1)
        if "=" in rhs and not Path(raw_ref).exists():
            path_part = lhs
            for chunk in rhs.split(","):
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    opts[k.strip().lower()] = v.strip()

    return {
        "path": path_part,
        "table": opts.get("table"),
        "slot": opts.get("slot"),
        "match": opts.get("match") or default_match,
        "strategy": opts.get("strategy") or default_strategy,
        "alias": opts.get("alias") or default_alias,
        "on_unmatched": opts.get("on_unmatched") or default_on_unmatched,
    }


def _enrich_rows_with_refs(
    rows: List[Dict[str, Any]],
    ref_specs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    current = list(rows)
    for cfg in ref_specs:
        if not current:
            break
        ref_src = open_source(cfg["path"])
        ref_rows = ref_src.load_rows(table=cfg.get("table"))
        if cfg.get("match"):
            lf, rf, inferred = parse_match_expr(cfg["match"])
            strat = cfg.get("strategy") or inferred or "token"
        else:
            l_cols = list(current[0].keys()) if current else []
            r_cols = list(ref_rows[0].keys()) if ref_rows else []
            lf, rf, inferred = _infer_default_match(l_cols, r_cols)
            strat = cfg.get("strategy") or inferred
        res = enrich_rows(
            current,
            ref_rows,
            MatchSpec(
                left_field=lf,
                right_field=rf,
                strategy=strat,
                alias=cfg.get("alias") or "ref",
                on_unmatched=cfg.get("on_unmatched") or "drop",
            ),
        )
        current = res.rows
    return current


def _row_label(row: Dict[str, Any]) -> str:
    for k in ("title", "name", "model", "label", "sku", "id"):
        val = row.get(k)
        if val is not None and str(val).strip():
            return str(val)
    for k, v in row.items():
        if not isinstance(v, (dict, list)) and v is not None:
            return f"{k}={v}"
    return "(item)"


def _flatten_scalar_fields(row: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for k, v in row.items():
        key = f"{prefix}{k}" if prefix else str(k)
        if isinstance(v, dict):
            for sk, sv in v.items():
                if not isinstance(sv, (dict, list)):
                    flat[f"{key}.{sk}"] = sv
        elif not isinstance(v, list):
            flat[key] = v
    return flat


def _export_output(export_path: str, json_payload: Dict[str, Any], flat_rows: List[Dict[str, Any]], title: str) -> None:
    out = Path(export_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()
    if ext == ".json":
        out.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif ext == ".csv":
        fieldnames: List[str] = []
        seen = set()
        for r in flat_rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in flat_rows:
                writer.writerow(r)
    elif ext in (".md", ".markdown"):
        lines = [f"# {title}", ""]
        if flat_rows:
            cols: List[str] = []
            seen = set()
            for r in flat_rows:
                for k in r.keys():
                    if k not in seen:
                        seen.add(k)
                        cols.append(k)
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("| " + " | ".join("---" for _ in cols) + " |")
            for r in flat_rows:
                vals = [str(r.get(c, "")).replace("|", "\\|") for c in cols]
                lines.append("| " + " | ".join(vals) + " |")
        else:
            lines.append("_No results._")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        raise ValueError(f"Unsupported export extension '{out.suffix}'. Use .json, .csv, or .md.")
    print(f"[OK] Exported results to {out}", file=sys.stderr)


def _add_common_solve_flags(parser: argparse.ArgumentParser, default_top: int = 10) -> None:
    parser.add_argument("--ref", action="append", default=[], help="Reference DB/file (optionally ':match=a~b,alias=ref,on_unmatched=drop')")
    parser.add_argument("--match", default=None, help="Match rule for --ref, e.g. 'title~model' or 'sku=id'")
    parser.add_argument("--strategy", choices=["exact", "token", "regex", "fuzzy"], default=None, help="Default --ref match strategy")
    parser.add_argument("--alias", default="ref", help="Default --ref alias (default: ref)")
    parser.add_argument("--on-unmatched", choices=["drop", "keep"], default="drop", help="Unmatched row policy for --ref (default: drop)")
    parser.add_argument("--formula", "-f", action="append", default=[], help="Derived row formula, e.g. 'deal = (price - ref.msrp) / ref.msrp'")
    parser.add_argument("--where", "-w", action="append", default=[], help="Row filter expression, e.g. 'vram >= 12 and price <= 600'")
    parser.add_argument("--prefer", "-p", action="append", default=[], help="Signed weights, e.g. 'ref.score:+0.7,price:-0.3'")
    parser.add_argument("--prefs", default=None, help="Saved preference profile name (see `prefs list`) or profile/preset file; explicit flags override it")
    parser.add_argument("--norm", choices=["robust", "minmax", "rank"], default=None, help="Normalization mode: robust (P5-P95 default), minmax, or rank")
    parser.add_argument("--top", "-n", type=int, default=default_top, help=f"Number of results to return (default: {default_top})")
    parser.add_argument("--explain", action="store_true", help="Print per-term normalized score breakdown")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    parser.add_argument("--export", default=None, help="Export results to .json, .csv, or .md")


def _weights_to_prefer(weights: Dict[str, Any]) -> str:
    return ",".join(f"{k}:{float(v):+g}" for k, v in weights.items())


def _apply_prefs(args: argparse.Namespace) -> None:
    """Fills --prefer/--formula/--where/--limit/--rule from `--prefs` for any group left empty on the CLI."""
    ref = getattr(args, "prefs", None)
    if not ref:
        return
    from preset_loader import resolve_shared_profile

    try:
        _name, data = resolve_shared_profile(ref)
    except (FileNotFoundError, KeyError) as exc:
        raise SystemExit(f"error: {exc} See `{get_cli_prog()} prefs list`.") from exc
    if not args.prefer and data.get("weights"):
        args.prefer = [_weights_to_prefer(data["weights"])]
    for attr, key in (("formula", "formulas"), ("where", "where"), ("limit", "limits"), ("rule", "rules")):
        if hasattr(args, attr) and isinstance(getattr(args, attr), list) and not getattr(args, attr) and data.get(key):
            setattr(args, attr, list(data[key]))
    if hasattr(args, "norm") and getattr(args, "norm") is None and data.get("norm"):
        args.norm = data["norm"]
    if hasattr(args, "diverse") and not getattr(args, "diverse") and data.get("diverse"):
        args.diverse = bool(data["diverse"])


def _print_diagnostics(meta: Dict[str, Any]) -> None:
    diag = meta.get("diagnostics") if isinstance(meta, dict) else None
    if not isinstance(diag, dict):
        return
    reasons = diag.get("reasons") or ([diag["summary"]] if diag.get("summary") else [])
    for r in reasons[:3]:
        print(f"    why: {r}")


def _print_breakdown(bd_list: Sequence[Any]) -> None:
    if not bd_list:
        return
    parts = [
        f"{b.expression}[raw={b.raw_value}, n={b.normalized:.2f}, w={b.weight:+g} -> +{b.contribution:.1f}]"
        for b in bd_list
    ]
    print(f"      explain: {' | '.join(parts)}")


def run_rank(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} rank",
        description="Rank rows in a table using signed weights and optional Reference DB enrichment.",
    )
    parser.add_argument("source", help="Primary DB/CSV/JSON source")
    parser.add_argument("--table", "-t", default=None, help="Table/collection name")
    parser.add_argument("--query", "-q", default=None, help="Read-only SQL SELECT query for SQLite")
    _add_common_solve_flags(parser, default_top=15)
    args = parser.parse_args(argv)
    _apply_prefs(args)

    rows = open_source(args.source).load_rows(table=args.table, query=args.query)
    ref_specs = [
        _parse_ref_spec(r, args.match, args.strategy, args.alias, args.on_unmatched)
        for r in args.ref
    ]
    if ref_specs:
        rows = _enrich_rows_with_refs(rows, ref_specs)

    ranked, meta = solve_rank(
        rows=rows,
        weights=args.prefer,
        formulas=args.formula,
        where=args.where,
        top_n=args.top,
        norm=args.norm or "robust",
    )

    payload = {"meta": meta, "results": [r.to_dict() for r in ranked]}
    flat_rows = [{"rank": i, "score": r.score, **_flatten_scalar_fields(r.row)} for i, r in enumerate(ranked, 1)]

    if args.export:
        _export_output(args.export, payload, flat_rows, f"Knapset Rank: {Path(args.source).name}")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"\nRanked Results [{Path(args.source).name}] ({meta['after_filter']}/{meta['total_input']} matched/filtered, norm={meta.get('norm', 'robust')})")
    print("═" * 84)
    if not ranked:
        print("  (No matching rows.)")
        _print_diagnostics(meta)
        print()
        return
    for idx, item in enumerate(ranked, 1):
        label = _row_label(item.row)
        metrics = ", ".join(f"{k}={v}" for k, v in _flatten_scalar_fields(item.row).items() if k not in ("id", "title", "name", "model"))
        print(f"  {idx:>2}. [{item.score:5.1f}] {label[:34]:<34} │ {metrics[:40]}")
        if args.explain:
            _print_breakdown(item.breakdown)
    print()


def run_basket(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} basket",
        description="Pack an optimal multi-item basket from a table under multi-resource limits.",
    )
    parser.add_argument("source", help="Primary DB/CSV/JSON source")
    parser.add_argument("--table", "-t", default=None, help="Table/collection name")
    parser.add_argument("--query", "-q", default=None, help="Read-only SQL SELECT query for SQLite")
    parser.add_argument("--limit", "-l", action="append", default=[], help="Aggregate limit, e.g. 'sum(price) <= 1000', 'count <= 5'")
    parser.add_argument("--max-per-item", type=int, default=1, help="Max copies per row (default: 1 for 0/1 knapsack)")
    parser.add_argument("--qty-field", default=None, help="Optional column capping per-row quantity")
    _add_common_solve_flags(parser, default_top=5)
    args = parser.parse_args(argv)
    _apply_prefs(args)

    rows = open_source(args.source).load_rows(table=args.table, query=args.query)
    ref_specs = [
        _parse_ref_spec(r, args.match, args.strategy, args.alias, args.on_unmatched)
        for r in args.ref
    ]
    if ref_specs:
        rows = _enrich_rows_with_refs(rows, ref_specs)

    solutions, meta = solve_basket(
        rows=rows,
        weights=args.prefer,
        limits=args.limit,
        formulas=args.formula,
        where=args.where,
        top_n=args.top,
        max_per_item=args.max_per_item,
        qty_field=args.qty_field,
        norm=args.norm or "robust",
    )

    payload = {"meta": meta, "solutions": [s.to_dict() for s in solutions]}
    flat_rows: List[Dict[str, Any]] = []
    for s_idx, sol in enumerate(solutions, 1):
        for it, cnt, sc in zip(sol.items, sol.counts, sol.item_scores):
            flat_rows.append(
                {
                    "solution": s_idx,
                    "total_score": sol.total_score,
                    "qty": cnt,
                    "item_score": sc,
                    **_flatten_scalar_fields(it),
                }
            )

    if args.export:
        _export_output(args.export, payload, flat_rows, f"Knapset Basket: {Path(args.source).name}")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"\nBasket Solutions [{Path(args.source).name}] ({meta['after_filter']} candidates, norm={meta.get('norm', 'robust')})")
    print("═" * 84)
    if not solutions:
        print("  (No feasible basket satisfies all limits.)")
        _print_diagnostics(meta)
        print()
        return
    for idx, sol in enumerate(solutions, 1):
        lim_str = ", ".join(f"{k} => {v:g}" for k, v in sol.metrics.items())
        print(f"  Basket #{idx} │ Total Utility: {sol.total_score:.1f} │ Items: {sum(sol.counts)}" + (f" │ {lim_str}" if lim_str else ""))
        for it, cnt, sc in zip(sol.items, sol.counts, sol.item_scores):
            qty_tag = f" x{cnt}" if cnt > 1 else ""
            print(f"    • [{sc:5.1f}] {_row_label(it)}{qty_tag}")
        print()


def run_solve(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} solve",
        description="Synthesize optimal multi-slot combinations across tables/files with weights, limits, and AST rules.",
    )
    parser.add_argument("source", nargs="?", default=None, help="Optional multi-table SQLite/JSON source (loads tables as slots)")
    parser.add_argument("--tables", default=None, help="Comma-separated table subset from positional source")
    parser.add_argument("--slot", action="append", default=[], help="Slot source binding: 'name=path[:table]'")
    parser.add_argument("--limit", "-l", action="append", default=[], help="Aggregate limit, e.g. 'sum(price) <= 1000'")
    parser.add_argument("--rule", "-r", action="append", default=[], help="Cross-slot rule, e.g. 'cpu.socket == mobo.socket'")
    parser.add_argument("--diverse", action="store_true", help="Diversify Top-N combinations (avoid repeating nearly identical sets)")
    _add_common_solve_flags(parser, default_top=5)
    args = parser.parse_args(argv)
    _apply_prefs(args)

    slot_pools: Dict[str, List[Dict[str, Any]]] = {}
    if args.source:
        src = open_source(args.source)
        schema = src.inspect()
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else schema.table_names()
        for tname in wanted:
            slot_pools[tname] = src.load_rows(table=tname)

    for s_spec in args.slot:
        if "=" not in s_spec:
            raise ValueError(f"Invalid --slot '{s_spec}'. Expected 'slot_name=source[:table]'.")
        sname, target = s_spec.split("=", 1)
        sname = sname.strip()
        tbl = None
        if ":" in target:
            lhs, rhs = target.rsplit(":", 1)
            if not Path(target).exists() and rhs:
                target, tbl = lhs, rhs
        slot_pools[sname] = open_source(target).load_rows(table=tbl)

    if not slot_pools:
        raise ValueError("Provide a multi-table source or at least one '--slot name=path'.")

    ref_specs = [
        _parse_ref_spec(r, args.match, args.strategy, args.alias, args.on_unmatched)
        for r in args.ref
    ]
    for cfg in ref_specs:
        target_slots = [cfg["slot"]] if cfg.get("slot") else list(slot_pools.keys())
        for sname in target_slots:
            if sname in slot_pools:
                slot_pools[sname] = _enrich_rows_with_refs(slot_pools[sname], [cfg])

    solutions, meta = solve_sets(
        slot_pools=slot_pools,
        weights=args.prefer,
        limits=args.limit,
        rules=args.rule,
        formulas=args.formula,
        where=args.where,
        top_n=args.top,
        norm=args.norm or "robust",
        diverse=bool(args.diverse),
    )

    payload = {"meta": meta, "solutions": [s.to_dict() for s in solutions]}
    flat_rows: List[Dict[str, Any]] = []
    for idx, sol in enumerate(solutions, 1):
        for sname, item in sol.slots.items():
            flat_rows.append(
                {
                    "solution": idx,
                    "total_score": sol.total_score,
                    "slot": sname,
                    **_flatten_scalar_fields(item),
                }
            )

    if args.export:
        _export_output(args.export, payload, flat_rows, "Knapset Multi-Slot Solve")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"\nSet Combinations ({', '.join(f'{k}={v}' for k, v in meta.get('slot_counts', {}).items())}, norm={meta.get('norm', 'robust')})")
    print("═" * 84)
    if not solutions:
        print("  (No valid combination satisfies all rules and limits.)")
        _print_diagnostics(meta)
        print()
        return
    for idx, sol in enumerate(solutions, 1):
        lim_str = ", ".join(f"{k} => {v:g}" for k, v in sol.metrics.items())
        print(f"  Set #{idx} │ Score: {sol.total_score:.1f}" + (f" │ {lim_str}" if lim_str else ""))
        for sname, item in sol.slots.items():
            price_hint = f" (price={item['price']})" if "price" in item else ""
            print(f"    • {sname:<12}: {_row_label(item)}{price_hint}")
        if args.explain:
            _print_breakdown(sol.breakdown)
        print()


def _flatten_preset_results(mode: str, results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flat_rows: List[Dict[str, Any]] = []
    if mode == "rank":
        for idx, r in enumerate(results, 1):
            flat_rows.append({"rank": idx, "score": r["score"], **_flatten_scalar_fields(r["row"])})
    elif mode == "basket":
        for s_idx, sol in enumerate(results, 1):
            for it in sol["items"]:
                flat_rows.append(
                    {
                        "solution": s_idx,
                        "total_score": sol["total_score"],
                        "qty": it["count"],
                        "item_score": it["item_score"],
                        **_flatten_scalar_fields(it["row"]),
                    }
                )
    else:
        for idx, sol in enumerate(results, 1):
            for sname, item in sol["slots"].items():
                flat_rows.append(
                    {
                        "solution": idx,
                        "total_score": sol["total_score"],
                        "slot": sname,
                        **_flatten_scalar_fields(item),
                    }
                )
    return flat_rows


def _print_preset_execution(res: Dict[str, Any], explain: bool) -> None:
    mode = res["mode"]
    results = res["results"]
    print(f"\nPreset '{res['title']}' [mode={mode}, profile={res['profile']}, norm={res.get('norm', 'robust')}]")
    print("═" * 84)
    if not results:
        print("  (No feasible results found.)")
        _print_diagnostics(res.get("meta") or {})
        print()
        return
    if mode == "rank":
        for idx, r in enumerate(results, 1):
            row = r["row"]
            metrics = ", ".join(f"{k}={v}" for k, v in _flatten_scalar_fields(row).items() if k not in ("id", "title", "name", "model"))
            print(f"  {idx:>2}. [{r['score']:5.1f}] {_row_label(row)[:34]:<34} │ {metrics[:40]}")
            if explain and r.get("breakdown"):
                parts = [
                    f"{b['expression']}[raw={b['raw_value']}, n={b['normalized']:.2f}, w={b['weight']:+g} -> +{b['contribution']:.1f}]"
                    for b in r["breakdown"]
                ]
                print(f"      explain: {' | '.join(parts)}")
    elif mode == "basket":
        for idx, sol in enumerate(results, 1):
            lim_str = ", ".join(f"{k} => {v:g}" for k, v in (sol.get("metrics") or {}).items())
            print(f"  Basket #{idx} │ Utility: {sol['total_score']:.1f} │ Items: {sol['item_count']}" + (f" │ {lim_str}" if lim_str else ""))
            for it in sol["items"]:
                qty_tag = f" x{it['count']}" if it["count"] > 1 else ""
                print(f"    • [{it['item_score']:5.1f}] {_row_label(it['row'])}{qty_tag}")
    else:
        for idx, sol in enumerate(results, 1):
            lim_str = ", ".join(f"{k} => {v:g}" for k, v in (sol.get("metrics") or {}).items())
            print(f"  Set #{idx} │ Score: {sol['total_score']:.1f}" + (f" │ {lim_str}" if lim_str else ""))
            for sname, item in sol["slots"].items():
                price_hint = f" (price={item['price']})" if "price" in item else ""
                print(f"    • {sname:<12}: {_row_label(item)}{price_hint}")
            if explain and sol.get("breakdown"):
                parts = [
                    f"{b['expression']}[raw={b['raw_value']}, n={b['normalized']:.2f}, w={b['weight']:+g} -> +{b['contribution']:.1f}]"
                    for b in sol["breakdown"]
                ]
                print(f"      explain: {' | '.join(parts)}")
    print()


def run_run(argv: List[str]) -> None:
    from preset_loader import resolve_preset

    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} run",
        description="Execute a Knapset v2 JSON/TOML preset (or installed preset name) with optional CLI overrides.",
    )
    parser.add_argument("preset", help="Preset file path (.json/.toml), directory, or installed preset name")
    parser.add_argument("--profile", "-P", default=None, help="Weight profile name defined inside preset")
    parser.add_argument("--mode", "-m", choices=["rank", "basket", "solve"], default=None, help="Override preset optimization mode")
    parser.add_argument("--slot", action="append", default=[], help="Override slot source path: 'slot_name=path'")
    parser.add_argument("--formula", "-f", action="append", default=[], help="Append derived formula")
    parser.add_argument("--where", "-w", action="append", default=[], help="Append filter expression")
    parser.add_argument("--prefer", "-p", action="append", default=[], help="Override signed weights, e.g. 'price:-0.5,score:+0.5'")
    parser.add_argument("--limit", "-l", action="append", default=[], help="Override aggregate limits, e.g. 'sum(price) <= 900'")
    parser.add_argument("--rule", "-r", action="append", default=[], help="Append compatibility rule")
    parser.add_argument("--norm", choices=["robust", "minmax", "rank"], default=None, help="Override normalization mode")
    parser.add_argument("--diverse", action="store_true", help="Diversify Top-N combinations")
    parser.add_argument("--top", "-n", type=int, default=None, help="Override top N results")
    parser.add_argument("--explain", action="store_true", help="Print per-term score breakdown")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    parser.add_argument("--export", default=None, help="Export results to .json, .csv, or .md")
    args = parser.parse_args(argv)

    cfg = resolve_preset(args.preset)
    src_overrides: Dict[str, str] = {}
    for s in args.slot:
        if "=" not in s:
            raise ValueError(f"Invalid --slot override '{s}'. Expected 'slot=path'.")
        k, v = s.split("=", 1)
        src_overrides[k.strip()] = v.strip()

    res = cfg.execute(
        profile_name=args.profile,
        mode_override=args.mode,
        prefer_override=args.prefer or None,
        limits_override=args.limit or None,
        where_override=args.where or None,
        formulas_override=args.formula or None,
        rules_override=args.rule or None,
        top_n=args.top,
        source_overrides=src_overrides or None,
        norm_override=args.norm,
        diverse_override=True if args.diverse else None,
    )

    if args.export:
        flat_rows = _flatten_preset_results(res["mode"], res["results"])
        _export_output(args.export, res, flat_rows, f"Knapset Preset: {res['title']}")

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return

    _print_preset_execution(res, explain=args.explain)


def run_preset(argv: List[str]) -> None:
    from marketplace.client import MarketplaceClient
    from preset_loader import discover_presets

    prog = get_cli_prog()
    parser = argparse.ArgumentParser(
        prog=f"{prog} preset",
        description="Manage local & registry Knapset v2 presets (list, search, info, install, remove, verify).",
    )
    sub = parser.add_subparsers(dest="subcommand", help="Preset action")

    p_list = sub.add_parser("list", help="List locally discovered & installed v2 presets")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    p_search = sub.add_parser("search", help="Search presets & modules in knapset-registry")
    p_search.add_argument("query", nargs="?", default="", help="Search query")
    p_search.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_search.add_argument("--refresh", action="store_true", help="Refresh registry cache")
    p_search.add_argument("--json", action="store_true", help="Output JSON")

    p_info = sub.add_parser("info", help="Show details of a registry preset/module")
    p_info.add_argument("name", help="Preset name")
    p_info.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_info.add_argument("--refresh", action="store_true", help="Refresh registry cache")

    p_inst = sub.add_parser("install", help="Install a preset from knapset-registry")
    p_inst.add_argument("target", help="Preset name or 'preset:<name>'")
    p_inst.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_inst.add_argument("--force", "-f", action="store_true", help="Overwrite existing preset")

    p_rem = sub.add_parser("remove", help="Remove an installed preset")
    p_rem.add_argument("name", help="Installed preset name")

    p_ver = sub.add_parser("verify", help="Verify preset checksums and schema validity")
    p_ver.add_argument("target", nargs="?", default=None, help="Optional preset name or file path")
    p_ver.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return

    if args.subcommand == "list":
        presets = discover_presets()
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": cfg.name,
                            "title": cfg.title,
                            "mode": cfg.mode,
                            "profiles": cfg.profile_names(),
                            "path": str(cfg.source_path) if cfg.source_path else "",
                        }
                        for cfg in presets.values()
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        print(f"\nDiscovered Knapset v2 Presets ({len(presets)})")
        print("═" * 84)
        if not presets:
            print("  (No v2 presets found. Create one with `ks init <db>` or install via `ks preset install <name>`.)\n")
            return
        for cfg in presets.values():
            profs = ", ".join(cfg.profile_names())
            print(f"  {cfg.name:<18} │ {cfg.mode:<6} │ profiles: {profs:<22} │ {cfg.title}")
        print()
        return

    client = MarketplaceClient(registry_url_or_path=getattr(args, "registry", None))
    if args.subcommand == "search":
        matches = client.search(query=args.query, force_refresh=args.refresh or None)
        if args.json:
            print(json.dumps([m.to_dict() if hasattr(m, "to_dict") else vars(m) for m in matches], indent=2, default=str))
            return
        for m in matches:
            print(f"  {m.name:<20} [{m.subsystem}] v{m.version} — {m.description}")
    elif args.subcommand == "info":
        res = client.info(args.name, force_refresh=args.refresh or None)
        print(json.dumps(res, indent=2, default=str))
    elif args.subcommand == "install":
        ok, msg = client.install(args.target, force=args.force)
        if not ok:
            print(f"[FAIL] {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] {msg}")
    elif args.subcommand == "remove":
        ok, msg = client.remove(args.name)
        if not ok:
            print(f"[FAIL] {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] {msg}")
    elif args.subcommand == "verify":
        rep = client.verify(args.target)
        if args.json:
            print(json.dumps(rep, indent=2, default=str))
            return
        for entry in rep:
            print(f"  {entry.get('name'):<20} [{entry.get('status')}] sha_ok={entry.get('checksum_match')} safe={entry.get('security_safe')}")


def run_prefs(argv: List[str]) -> None:
    """`ks prefs` — manage the shared, DB-independent library of saved preference profiles."""
    from preset_loader import (
        get_user_profiles_dir,
        list_shared_profiles,
        read_profile_file,
        save_shared_profile,
    )

    prog = get_cli_prog()
    parser = argparse.ArgumentParser(
        prog=f"{prog} prefs",
        description="Manage saved preference profiles (weights, formulas, filters, limits, rules) reusable on any DB with matching columns.",
    )
    sub = parser.add_subparsers(dest="subcommand", help="Preference action")

    p_list = sub.add_parser("list", help="List saved preference profiles")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    p_show = sub.add_parser("show", help="Show one saved profile")
    p_show.add_argument("name")

    p_save = sub.add_parser("save", help="Create/update a saved profile from flags")
    p_save.add_argument("name")
    p_save.add_argument("--prefer", "-p", action="append", default=[], help="Signed weights, e.g. 'score:+0.7,price:-0.3'")
    p_save.add_argument("--formula", "-f", action="append", default=[], help="Derived row formula")
    p_save.add_argument("--where", "-w", action="append", default=[], help="Row filter expression")
    p_save.add_argument("--limit", "-l", action="append", default=[], help="Aggregate limit")
    p_save.add_argument("--rule", "-r", action="append", default=[], help="Cross-slot rule")
    p_save.add_argument("--mode", choices=["rank", "basket", "solve"], default=None)

    p_imp = sub.add_parser("import", help="Import a profile from a profile file or a preset (.json/.toml)")
    p_imp.add_argument("file")
    p_imp.add_argument("--name", default=None, help="Name in the library (default: name from file)")
    p_imp.add_argument("--profile", default=None, help="Profile to take when importing a preset with several")

    p_exp = sub.add_parser("export", help="Write a saved profile to a standalone file")
    p_exp.add_argument("name")
    p_exp.add_argument("file")

    p_rm = sub.add_parser("remove", help="Delete a saved profile")
    p_rm.add_argument("name")

    sub.add_parser("path", help="Print the shared profiles directory")

    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return

    lib = list_shared_profiles()

    def _get(name: str) -> Dict[str, Any]:
        key = name.strip().lower()
        if key not in lib:
            raise SystemExit(f"error: saved profile '{name}' not found (available: {', '.join(lib) or 'none'}).")
        return lib[key]

    if args.subcommand == "path":
        print(get_user_profiles_dir())
    elif args.subcommand == "list":
        if args.json:
            print(json.dumps(lib, indent=2, ensure_ascii=False))
            return
        if not lib:
            print(f"No saved profiles yet in {get_user_profiles_dir()} (press `s` in `{prog} tui` or use `{prog} prefs save`).")
            return
        print(f"\nSaved Preferences ({get_user_profiles_dir()})")
        print("═" * 84)
        for name, d in lib.items():
            w = _weights_to_prefer(d.get("weights") or {})
            print(f"  {name:<20} [{d.get('mode', '-'):<6}] {w[:50]}")
        print()
    elif args.subcommand == "show":
        print(json.dumps(_get(args.name), indent=2, ensure_ascii=False))
    elif args.subcommand == "save":
        weights: Dict[str, float] = {}
        for chunk in args.prefer:
            for part in chunk.split(","):
                if part.strip():
                    expr, _, val = part.rpartition(":")
                    if not expr:
                        raise SystemExit(f"error: bad weight '{part}', expected 'expr:+0.5'.")
                    weights[expr.strip()] = float(val)
        data = {"mode": args.mode or "rank", "weights": weights, "formulas": args.formula, "where": args.where,
                "limits": args.limit, "rules": args.rule}
        path = save_shared_profile(args.name, data)
        print(f"[OK] Saved profile '{args.name}' -> {path}")
    elif args.subcommand == "import":
        name, data = read_profile_file(args.file, args.profile)
        path = save_shared_profile(args.name or name, data)
        print(f"[OK] Imported '{args.name or name}' -> {path}")
    elif args.subcommand == "export":
        data = _get(args.name)
        out = save_shared_profile(args.name, data, directory=Path(args.file).expanduser().resolve().parent)
        target = Path(args.file).expanduser().resolve()
        if out != target:
            out.replace(target)
        print(f"[OK] Exported '{args.name}' -> {target}")
    elif args.subcommand == "remove":
        from preset_loader import delete_shared_profile

        try:
            delete_shared_profile(args.name)
            print(f"[OK] Removed '{args.name}'")
        except (KeyError, FileNotFoundError) as exc:
            raise SystemExit(f"error: {exc}") from exc


def run_reg(argv: List[str]) -> None:
    """Unified `ks reg` CLI for searching, installing, managing registry themes/presets/modules, and selecting themes."""
    from marketplace.client import TYPE_ALIASES, MarketplaceClient
    from theme_config import (
        discover_themes,
        get_theme_config_path,
        load_active_theme,
        reset_active_theme,
        set_active_theme,
    )

    prog = get_cli_prog()
    parser = argparse.ArgumentParser(
        prog=f"{prog} reg",
        description="Knapset v2 Registry & Theme Manager (search, info, install, list, remove, verify, theme).",
    )
    sub = parser.add_subparsers(dest="subcommand", help="Registry action")

    p_search = sub.add_parser("search", help="Search themes, presets, modules & bundles in knapset-registry")
    p_search.add_argument("query", nargs="?", default="", help="Search query")
    p_search.add_argument("--type", "-t", dest="subsystem", default=None, help="Filter by type/subsystem (e.g. theme, preset, extension)")
    p_search.add_argument("--domain", "-d", default=None, help="Filter by domain")
    p_search.add_argument("--tag", default=None, help="Filter by tag")
    p_search.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_search.add_argument("--refresh", action="store_true", help="Refresh registry cache")
    p_search.add_argument("--json", action="store_true", help="Output JSON")

    p_info = sub.add_parser("info", help="Show details of a registry theme, preset, module, or bundle")
    p_info.add_argument("name", help="Item name")
    p_info.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_info.add_argument("--refresh", action="store_true", help="Refresh registry cache")
    p_info.add_argument("--json", action="store_true", help="Output JSON")

    p_inst = sub.add_parser("install", help="Install a theme, preset, module, or bundle from registry")
    p_inst.add_argument("target", help="Item name or typed target (e.g. theme:nord, preset:pc_build)")
    p_inst.add_argument("--use", action="store_true", help="Activate immediately in theme config if target is a theme")
    p_inst.add_argument("--registry", default=None, help="Custom registry URL or local directory path")
    p_inst.add_argument("--refresh", action="store_true", help="Refresh registry cache")
    p_inst.add_argument("--force", "-f", action="store_true", help="Overwrite if already installed")

    p_list = sub.add_parser("list", help="List installed registry items and local themes")
    p_list.add_argument("--type", "-t", dest="subsystem", default=None, help="Filter by type/subsystem (e.g. theme, preset)")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    p_rem = sub.add_parser("remove", aliases=["uninstall"], help="Remove an installed registry item")
    p_rem.add_argument("name", help="Installed item name")
    p_rem.add_argument("--force", "-f", action="store_true", help="Force removal")

    p_ver = sub.add_parser("verify", help="Verify installed registry items or local files")
    p_ver.add_argument("target", nargs="?", default=None, help="Optional item name or path")
    p_ver.add_argument("--json", action="store_true", help="Output JSON")

    p_theme = sub.add_parser("theme", help="Manage active TUI theme config (`theme.json`)")
    theme_sub = p_theme.add_subparsers(dest="theme_action", help="Theme action")

    p_tlist = theme_sub.add_parser("list", help="List built-in, installed, and custom themes")
    p_tlist.add_argument("--json", action="store_true", help="Output JSON")

    p_tuse = theme_sub.add_parser("use", help="Activate a theme in `theme.json`")
    p_tuse.add_argument("name", help="Theme name or path to theme .json file")

    p_tshow = theme_sub.add_parser("show", help="Show currently active theme and config file path")
    p_tshow.add_argument("--json", action="store_true", help="Output JSON")

    theme_sub.add_parser("reset", help="Reset `theme.json` back to default `agy-dark`")

    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return

    if args.subcommand == "theme":
        action = args.theme_action or "show"
        if action == "list":
            active = load_active_theme()
            themes = discover_themes()
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "name": t.name,
                                "title": t.title,
                                "author": t.author,
                                "description": t.description,
                                "active": t.name == active.name,
                                "path": str(t.source_path) if t.source_path else "builtin",
                            }
                            for t in themes.values()
                        ],
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return
            print(f"\nKnapset Themes (active: {active.name} │ config: {get_theme_config_path()})")
            print("═" * 84)
            for t in themes.values():
                mark = "★ active" if t.name == active.name else "        "
                src = str(t.source_path.name) if t.source_path else "builtin"
                print(f"  {mark} │ {t.name:<18} │ {t.title:<18} │ {t.description or src}")
            print()
            return
        if action == "use":
            try:
                cfg, cfg_path = set_active_theme(args.name)
            except KeyError as exc:
                print(f"[FAIL] {exc}", file=sys.stderr)
                sys.exit(1)
            except Exception as exc:
                print(f"[FAIL] Could not load theme '{args.name}': {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"[OK] Active theme set to '{cfg.name}' ({cfg.title}) -> {cfg_path}")
            return
        if action == "reset":
            cfg, cfg_path = reset_active_theme()
            print(f"[OK] Reset active theme to '{cfg.name}' -> {cfg_path}")
            return
        # show
        active = load_active_theme()
        cfg_path = get_theme_config_path()
        if getattr(args, "json", False):
            payload = active.to_dict()
            payload["config_path"] = str(cfg_path)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        print(f"\nActive Theme : {active.name} ({active.title})")
        print(f"Config File  : {cfg_path}")
        print(f"Colors       : {json.dumps(active.colors, ensure_ascii=False)}")
        print(f"Symbols      : {json.dumps(active.symbols, ensure_ascii=False)}\n")
        return

    client = MarketplaceClient(registry_url_or_path=getattr(args, "registry", None))

    if args.subcommand == "list":
        sub_filter = TYPE_ALIASES.get(args.subsystem.lower(), args.subsystem.lower()) if args.subsystem else None
        records = client.list_installed()
        if sub_filter:
            records = [r for r in records if str(r.get("subsystem", "")).lower() == sub_filter]
        if args.json:
            print(json.dumps(records, indent=2, ensure_ascii=False, default=str))
            return
        active_theme = load_active_theme().name
        print(f"\nInstalled Registry Items ({len(records)})")
        print("═" * 84)
        if not records:
            print(f"  (No registry items installed yet. Search via `{prog} reg search`.)\n")
            return
        for rec in records:
            extra_tag = " ★ active" if rec.get("subsystem") == "themes" and rec.get("name") == active_theme else ""
            print(f"  {rec.get('name', ''):<20} │ [{rec.get('subsystem', ''):<10}] v{rec.get('version', '1.0.0'):<8} │ {rec.get('path', '')}{extra_tag}")
        print()
        return

    if args.subcommand == "search":
        sub_filter = TYPE_ALIASES.get(args.subsystem.lower(), args.subsystem.lower()) if args.subsystem else None
        res = client.search(
            query=args.query,
            domain=args.domain,
            subsystem=sub_filter,
            tag=args.tag,
            force_refresh=args.refresh,
        )
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
            return
        mods = res.get("modules", [])
        bundles = res.get("bundles", []) if not sub_filter else []
        print(f"\nRegistry Search Results ({len(mods)} item(s), {len(bundles)} bundle(s))")
        print("═" * 84)
        for m in mods:
            inst = f" [installed v{m.get('installed_version')}]" if m.get("is_installed") else ""
            print(f"  {m.get('name', ''):<20} [{m.get('subsystem', ''):<10}] v{m.get('version', ''):<6}{inst} — {m.get('description', '')}")
        for b in bundles:
            print(f"  bundle:{b.get('name', ''):<13} [bundle    ] — {b.get('description', '')}")
        print()
        return

    if args.subcommand == "info":
        res = client.info(args.name, force_refresh=args.refresh)
        if not res:
            print(f"[FAIL] Item '{args.name}' not found in registry.", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
            return
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return

    if args.subcommand == "install":
        ok, msg = client.install(
            args.target,
            force=args.force,
            force_refresh=True if args.refresh else None,
        )
        if not ok:
            print(f"[FAIL] {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] {msg}")
        if args.use:
            clean_name = args.target.strip().lower().split(":", 1)[-1]
            installed_map = {r["name"]: r for r in client.list_installed()}
            rec = installed_map.get(clean_name)
            if rec and rec.get("subsystem") in ("theme", "themes"):
                cfg, cfg_path = set_active_theme(clean_name)
                print(f"[OK] Activated theme '{cfg.name}' ({cfg.title}) -> {cfg_path}")
        return

    if args.subcommand in ("remove", "uninstall"):
        ok, msg = client.remove(args.name, force=args.force)
        if not ok:
            print(f"[FAIL] {msg}", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] {msg}")
        return

    if args.subcommand == "verify":
        rep = client.verify(args.target)
        if args.json:
            print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
            return
        for entry in rep:
            print(f"  {entry.get('name'):<20} [{entry.get('status')}] sha_ok={entry.get('checksum_match')} safe={entry.get('security_safe')}")
