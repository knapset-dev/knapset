"""
Cross-Database Reference Matcher & Enricher for Knapset v2.

Matches candidate rows from a primary data source against one or more Reference
Databases using exact keys, normalized token/substring specificity matching, or
regex patterns, attaching matched attributes under `row[alias]` (default `'ref'`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sources.registry import open_source

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_match_text(value: Any) -> str:
    """Lowercases and collapses punctuation into single spaces for token matching."""
    if value is None:
        return ""
    return _NON_ALNUM_RE.sub(" ", str(value).lower()).strip()


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


@dataclass
class MatchSpec:
    """Configuration for enriching candidate rows against a Reference DB."""

    left_field: str
    right_field: str
    strategy: str = "token"  # "exact" | "token" | "regex"
    alias: str = "ref"
    on_unmatched: str = "drop"  # "drop" | "keep"

    def __post_init__(self) -> None:
        self.strategy = (self.strategy or "token").strip().lower()
        if self.strategy not in ("exact", "token", "regex"):
            raise ValueError(f"Unsupported match strategy '{self.strategy}'. Use 'exact', 'token', or 'regex'.")
        self.on_unmatched = (self.on_unmatched or "drop").strip().lower()
        if self.on_unmatched not in ("drop", "keep"):
            raise ValueError(f"Unsupported on_unmatched policy '{self.on_unmatched}'. Use 'drop' or 'keep'.")
        self.alias = (self.alias or "ref").strip()


@dataclass
class EnrichResult:
    """Result of matching candidate rows against a Reference DB."""

    rows: List[Dict[str, Any]]
    total: int
    matched: int
    unmatched: int
    alias: str = "ref"
    unmatched_samples: List[str] = field(default_factory=list)

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
        raw_key = r.get(right_field)
        norm = normalize_match_text(raw_key)
        if not norm:
            continue
        tokens = set(norm.split())
        phrase_pat = re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")
        entries.append((len(tokens), len(norm), phrase_pat, tokens, r))

    # Highest token count and longest phrase first so 'RTX 3080 Ti' beats 'RTX 3080'.
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)

    def _match(left_val: Any) -> Optional[Dict[str, Any]]:
        norm_left = normalize_match_text(left_val)
        if not norm_left:
            return None
        for _, _, phrase_pat, _, ref_row in entries:
            if phrase_pat.search(norm_left):
                return ref_row
        left_tokens = set(norm_left.split())
        for tok_count, _, _, ref_tokens, ref_row in entries:
            if tok_count > 0 and ref_tokens.issubset(left_tokens):
                return ref_row
        return None

    return _match


def _build_exact_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
):
    lookup: Dict[str, Dict[str, Any]] = {}
    for r in reference_rows:
        val = r.get(right_field)
        if val is None:
            continue
        key = str(val).strip().lower()
        if key and key not in lookup:
            lookup[key] = r

    def _match(left_val: Any) -> Optional[Dict[str, Any]]:
        if left_val is None:
            return None
        return lookup.get(str(left_val).strip().lower())

    return _match


def _build_regex_matcher(
    reference_rows: Sequence[Dict[str, Any]],
    right_field: str,
):
    compiled: List[Tuple[re.Pattern[str], Dict[str, Any]]] = []
    for idx, r in enumerate(reference_rows):
        pat_raw = r.get(right_field)
        if not pat_raw:
            continue
        try:
            pat = re.compile(str(pat_raw), re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex in reference row {idx} ({right_field}={pat_raw!r}): {exc}") from exc
        compiled.append((pat, r))

    def _match(left_val: Any) -> Optional[Dict[str, Any]]:
        if left_val is None:
            return None
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
        return best_row

    return _match


def enrich_rows(
    candidate_rows: Sequence[Dict[str, Any]],
    reference_rows: Sequence[Dict[str, Any]],
    spec: MatchSpec,
) -> EnrichResult:
    """
    Matches `candidate_rows` against `reference_rows` according to `spec`,
    attaching matched reference records under `row[spec.alias]`.
    """
    if spec.strategy == "exact":
        matcher = _build_exact_matcher(reference_rows, spec.right_field)
    elif spec.strategy == "token":
        matcher = _build_token_matcher(reference_rows, spec.right_field)
    else:
        matcher = _build_regex_matcher(reference_rows, spec.right_field)

    out_rows: List[Dict[str, Any]] = []
    matched = 0
    unmatched = 0
    unmatched_samples: List[str] = []

    for row in candidate_rows:
        left_val = row.get(spec.left_field)
        ref_match = matcher(left_val)
        enriched = dict(row)
        if ref_match is not None:
            enriched[spec.alias] = dict(ref_match)
            matched += 1
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
