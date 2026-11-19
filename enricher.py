"""
Cross-Database Reference Matcher & Enricher for Knapset v2.

Matches candidate rows from a primary data source against one or more Reference
Databases using exact keys, normalized token/substring specificity matching, or
regex patterns, attaching matched attributes under `row[alias]` (default `'ref'`).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sources.registry import open_source

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_HOMOGLYPH_TRANS = str.maketrans("аеікморстх", "aeikmopctx")


def normalize_match_text(value: Any) -> str:
    """Lowercases, folds Cyrillic-Latin lookalikes, and collapses punctuation for matching."""
    if value is None:
        return ""
    folded = str(value).lower().translate(_HOMOGLYPH_TRANS)
    return _NON_ALNUM_RE.sub(" ", folded).strip()


def parse_match_expr(expr: str) -> Tuple[str, str, Optional[str]]:
    """
    Parses a match expression like `'title~model'` (token), `'sku=id'` (exact),
    or `'model'` (same field on both sides).
    Returns `(left_field, right_field, inferred_strategy)`.
    """
    raw = (expr or "").strip()
    if not raw:
        raise ValueError("Match expression must not be empty (expected e.g. 'title~model' or 'id=id').")
    if "~" in raw:
        left, right = raw.split("~", 1)
        return left.strip(), right.strip(), "token"
    if "==" in raw:
        left, right = raw.split("==", 1)
        return left.strip(), right.strip(), "exact"
    if "=" in raw:
        left, right = raw.split("=", 1)
        return left.strip(), right.strip(), "exact"
    return raw, raw, None


def _extract_field_value(row: Dict[str, Any], field_spec: str) -> Optional[str]:
    """Extracts a single field or composite '+'-joined key (e.g. 'brand+model') from `row`."""
    if "+" not in field_spec:
        val = row.get(field_spec)
        return None if val is None else str(val)
    parts: List[str] = []
    for part in field_spec.split("+"):
        k = part.strip()
        if not k:
            continue
        v = row.get(k)
        if v is not None and str(v).strip():
            parts.append(str(v).strip())
    return " ".join(parts) if parts else None


@dataclass
class MatchSpec:
    """Configuration for enriching candidate rows against a Reference DB."""

    left_field: str
    right_field: str
    strategy: str = "token"  # "exact" | "token" | "regex" | "fuzzy"
    alias: str = "ref"
    on_unmatched: str = "drop"  # "drop" | "keep"
    threshold: float = 0.78

    def __post_init__(self) -> None:
        self.strategy = (self.strategy or "token").strip().lower()
        if self.strategy not in ("exact", "token", "regex", "fuzzy"):
            raise ValueError(
                f"Unsupported match strategy '{self.strategy}'. Use 'exact', 'token', 'regex', or 'fuzzy'."
            )
        self.on_unmatched = (self.on_unmatched or "drop").strip().lower()
        if self.on_unmatched not in ("drop", "keep"):
            raise ValueError(f"Unsupported on_unmatched policy '{self.on_unmatched}'. Use 'drop' or 'keep'.")
        self.alias = (self.alias or "ref").strip()
        self.threshold = max(0.1, min(1.0, float(self.threshold)))


@dataclass
class EnrichResult:
    """Result of matching candidate rows against a Reference DB."""

    rows: List[Dict[str, Any]]
    total: int
    matched: int
    unmatched: int
    alias: str = "ref"
    unmatched_samples: List[str] = field(default_factory=list)
    method_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def match_rate(self) -> float:
        return (self.matched / self.total) if self.total > 0 else 0.0


def _build_token_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
):
    """Pre-compiles specificity-ordered token/phrase matchers for reference rows."""
    entries: List[Tuple[int, int, re.Pattern[str], set[str], Dict[str, Any]]] = []
    for r in reference_rows:
        raw_key = _extract_field_value(r, right_field)
        norm = normalize_match_text(raw_key)
        if not norm:
            continue
        tokens = set(norm.split())
        phrase_pat = re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")
        entries.append((len(tokens), len(norm), phrase_pat, tokens, r))

    # Highest token count and longest phrase first so 'RTX 3080 Ti' beats 'RTX 3080'.
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)

    def _match(left_val: Any) -> Tuple[Optional[Dict[str, Any]], str, float]:
        norm_left = normalize_match_text(left_val)
        if not norm_left:
            return None, "", 0.0
        for _, _, phrase_pat, _, ref_row in entries:
            if phrase_pat.search(norm_left):
                return ref_row, "phrase", 1.0
        left_tokens = set(norm_left.split())
        for tok_count, _, _, ref_tokens, ref_row in entries:
            if tok_count > 0 and ref_tokens.issubset(left_tokens):
                return ref_row, "token_subset", 1.0
        return None, "", 0.0

    return _match


def _build_exact_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
):
    lookup: Dict[str, Dict[str, Any]] = {}
    for r in reference_rows:
        val = _extract_field_value(r, right_field)
        if val is None:
            continue
        key = str(val).strip().lower()
        if key and key not in lookup:
            lookup[key] = r

    def _match(left_val: Any) -> Tuple[Optional[Dict[str, Any]], str, float]:
        if left_val is None:
            return None, "", 0.0
        found = lookup.get(str(left_val).strip().lower())
        return (found, "exact", 1.0) if found is not None else (None, "", 0.0)

    return _match


def _build_regex_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
):
    compiled: List[Tuple[re.Pattern[str], Dict[str, Any]]] = []
    for idx, r in enumerate(reference_rows):
        pat_raw = _extract_field_value(r, right_field)
        if not pat_raw:
            continue
        try:
            pat = re.compile(str(pat_raw), re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex in reference row {idx} ({right_field}={pat_raw!r}): {exc}") from exc
        compiled.append((pat, r))

    def _match(left_val: Any) -> Tuple[Optional[Dict[str, Any]], str, float]:
        if left_val is None:
            return None, "", 0.0
        text = str(left_val)
        best_row: Optional[Dict[str, Any]] = None
        best_span = -1
        for pat, ref_row in compiled:
            m = pat.search(text)
            if m:
                span = m.end() - m.start()
                if span > best_span:
                    best_span = span
                    best_row = ref_row
        return (best_row, "regex", 1.0) if best_row is not None else (None, "", 0.0)

    return _match


def _build_fuzzy_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
    threshold: float = 0.78,
):
    """Fuzzy edit-similarity matcher (`difflib.SequenceMatcher`) with exact/token fast path."""
    token_matcher = _build_token_matcher(reference_rows, right_field)
    entries: List[Tuple[str, Dict[str, Any]]] = []
    for r in reference_rows:
        norm = normalize_match_text(_extract_field_value(r, right_field))
        if norm:
            entries.append((norm, r))

    def _match(left_val: Any) -> Tuple[Optional[Dict[str, Any]], str, float]:
        fast_row, fast_method, fast_score = token_matcher(left_val)
        if fast_row is not None:
            return fast_row, fast_method, fast_score
        norm_left = normalize_match_text(left_val)
        if not norm_left:
            return None, "", 0.0
        best_row: Optional[Dict[str, Any]] = None
        best_ratio = 0.0
        for norm_ref, ref_row in entries:
            ratio = difflib.SequenceMatcher(None, norm_left, norm_ref).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = ref_row
        if best_row is not None and best_ratio >= threshold:
            return best_row, "fuzzy", round(best_ratio, 4)
        return None, "", 0.0

    return _match


def enrich_rows(
    candidate_rows: Sequence[Dict[str, Any]],
    reference_rows: Sequence[Dict[str, Any]],
    spec: MatchSpec,
    include_provenance: bool = False,
) -> EnrichResult:
    """
    Matches `candidate_rows` against `reference_rows` according to `spec`,
    attaching matched reference records under `row[spec.alias]`.
    """
    if spec.strategy == "exact":
        matcher = _build_exact_matcher(reference_rows, spec.right_field)
    elif spec.strategy == "token":
        matcher = _build_token_matcher(reference_rows, spec.right_field)
    elif spec.strategy == "fuzzy":
        matcher = _build_fuzzy_matcher(reference_rows, spec.right_field, threshold=spec.threshold)
    else:
        matcher = _build_regex_matcher(reference_rows, spec.right_field)

    out_rows: List[Dict[str, Any]] = []
    matched = 0
    unmatched = 0
    unmatched_samples: List[str] = []
    method_counts: Dict[str, int] = {}

    for row in candidate_rows:
        left_val = _extract_field_value(row, spec.left_field)
        ref_match, method, score = matcher(left_val)
        enriched = dict(row)
        if ref_match is not None:
            enriched[spec.alias] = dict(ref_match)
            if include_provenance:
                enriched["_match_method"] = method
                enriched["_match_score"] = score
            matched += 1
            method_counts[method] = method_counts.get(method, 0) + 1
            out_rows.append(enriched)
        else:
            unmatched += 1
            if len(unmatched_samples) < 10 and left_val is not None:
                unmatched_samples.append(str(left_val))
            if spec.on_unmatched == "keep":
                enriched[spec.alias] = None
                out_rows.append(enriched)

    return EnrichResult(
        rows=out_rows,
        total=len(candidate_rows),
        matched=matched,
        unmatched=unmatched,
        alias=spec.alias,
        unmatched_samples=unmatched_samples,
        method_counts=method_counts,
    )


def enrich_from_source(
    candidate_rows: Sequence[Dict[str, Any]],
    ref_source: str | Path,
    spec: MatchSpec,
    ref_table: Optional[str] = None,
    ref_query: Optional[str] = None,
) -> EnrichResult:
    """Loads `ref_source` and enriches `candidate_rows` using `spec`."""
    ref_rows = open_source(ref_source).load_rows(table=ref_table, query=ref_query)
    return enrich_rows(candidate_rows, ref_rows, spec)


def infer_default_match(left_cols: Sequence[str], right_cols: Sequence[str]) -> Tuple[str, str, str]:
    """Infers `(left_field, right_field, strategy)` when `--match` is omitted."""
    l_lower = {c.lower(): c for c in left_cols}
    r_lower = {c.lower(): c for c in right_cols}
    for key in ("id", "sku", "part_number", "code", "upc", "ean"):
        if key in l_lower and key in r_lower:
            return l_lower[key], r_lower[key], "exact"
    for key in ("model", "title", "name", "product"):
        if key in l_lower and key in r_lower:
            return l_lower[key], r_lower[key], "token"
    for lk in ("title", "name", "model", "listing", "description", "label"):
        if lk in l_lower:
            for rk in ("model", "name", "title", "pattern", "label"):
                if rk in r_lower:
                    return l_lower[lk], r_lower[rk], "token"
    if left_cols and right_cols:
        return left_cols[0], right_cols[0], "token"
    raise ValueError("Cannot infer match columns from empty schema; pass '--match left~right'.")


def compare_sources(
    source: str | Path,
    ref_source: str | Path,
    *,
    table: Optional[str] = None,
    query: Optional[str] = None,
    ref_table: Optional[str] = None,
    ref_query: Optional[str] = None,
    match: Optional[str] = None,
    strategy: Optional[str] = None,
    alias: str = "ref",
    on_unmatched: str = "keep",
    threshold: float = 0.78,
    limit: int = 15,
) -> Dict[str, Any]:
    """Compares a primary source against a Reference DB and returns match coverage + preview rows."""
    left_rows = open_source(source).load_rows(table=table, query=query)
    right_rows = open_source(ref_source).load_rows(table=ref_table, query=ref_query)

    if match:
        lf, rf, inferred_strat = parse_match_expr(match)
        strat = strategy or inferred_strat or "token"
    else:
        l_cols = list(left_rows[0].keys()) if left_rows else []
        r_cols = list(right_rows[0].keys()) if right_rows else []
        lf, rf, inferred_strat = infer_default_match(l_cols, r_cols)
        strat = strategy or inferred_strat

    spec = MatchSpec(
        left_field=lf,
        right_field=rf,
        strategy=strat,
        alias=alias,
        on_unmatched=on_unmatched,
        threshold=threshold,
    )
    res = enrich_rows(left_rows, right_rows, spec, include_provenance=True)
    preview = res.rows[:limit] if limit and limit > 0 else res.rows
    return {
        "match": {
            "left_field": lf,
            "right_field": rf,
            "strategy": strat,
            "alias": spec.alias,
            "on_unmatched": spec.on_unmatched,
            "threshold": spec.threshold,
            "total": res.total,
            "matched": res.matched,
            "unmatched": res.unmatched,
            "match_rate": round(res.match_rate, 4),
            "method_counts": dict(res.method_counts),
            "unmatched_samples": res.unmatched_samples,
        },
        "rows": preview,
    }


def collect_ref_specs(args: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalizes single (`ref_source`) and list (`references`) ref specs from API/MCP payloads."""
    specs: List[Dict[str, Any]] = []
    if args.get("ref_source"):
        specs.append(
            {
                "source": args["ref_source"],
                "table": args.get("ref_table"),
                "query": args.get("ref_query"),
                "match": args.get("match") or "title~model",
                "strategy": args.get("strategy"),
                "alias": args.get("alias") or "ref",
                "on_unmatched": args.get("on_unmatched") or "drop",
                "threshold": float(args.get("threshold", 0.78)),
            }
        )
    for item in args.get("references") or []:
        if isinstance(item, dict) and item.get("source"):
            specs.append(dict(item))
    return specs


def enrich_slot_pools(
    slot_pools: Dict[str, List[Dict[str, Any]]],
    references: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Applies a sequence of reference attachment specs onto matching slots in `slot_pools`."""
    for ref_cfg in references:
        ref_source = ref_cfg.get("source")
        if not ref_source:
            continue
        r_rows = open_source(str(ref_source)).load_rows(
            table=ref_cfg.get("table"),
            query=ref_cfg.get("query"),
        )
        match_expr = str(ref_cfg.get("match") or "title~model")
        lf, rf, inferred = parse_match_expr(match_expr)
        spec = MatchSpec(
            left_field=lf,
            right_field=rf,
            strategy=str(ref_cfg.get("strategy") or inferred or "token"),
            alias=str(ref_cfg.get("alias") or "ref"),
            on_unmatched=str(ref_cfg.get("on_unmatched") or "drop"),
            threshold=float(ref_cfg.get("threshold", 0.78)),
        )
        target_slots = [str(ref_cfg["slot"])] if ref_cfg.get("slot") else list(slot_pools.keys())
        for sname in target_slots:
            if sname in slot_pools:
                slot_pools[sname] = enrich_rows(slot_pools[sname], r_rows, spec).rows
    return slot_pools
