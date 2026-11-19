"""
Unified Preference, Normalization, and Multi-Limit Solver Engine for Knapset v2.

Implements the three core optimization modes:
  1. `rank`:   Single-table Top-K ranking with signed-weight min-max [0, 1] normalization & `--explain`.
  2. `basket`: Multi-item 0/1 & bounded knapsack under multi-resource limits & constraints.
  3. `solve`:  Multi-slot set synthesis (1 item per slot) with cross-slot AST rules,
               cross-slot preference expressions, and multi-resource limits.
"""

from __future__ import annotations

import ast
import heapq
import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from formula_engine import (
    FormulaSpec,
    LimitSpec,
    RuleSpec,
    SafeExpression,
    WeightTerm,
    _extract_attr_chain,
    apply_row_formulas,
    filter_rows,
    parse_formulas,
    parse_weights,
    resolve_field_path,
    to_number,
)

_AGG_WRAPPER_RE = re.compile(r"^\s*(?:sum|avg|mean)\s*\(\s*(.+?)\s*\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class TermBreakdown:
    expression: str
    weight: float
    raw_value: Optional[float]
    normalized: float
    contribution: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expression": self.expression,
            "weight": self.weight,
            "raw_value": self.raw_value,
            "normalized": round(self.normalized, 4),
            "contribution": round(self.contribution, 2),
        }


@dataclass
class RankedItem:
    row: Dict[str, Any]
    score: float
    breakdown: List[TermBreakdown]
    index: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "row": self.row,
            "breakdown": [b.to_dict() for b in self.breakdown],
        }


@dataclass
class BasketSolution:
    items: List[Dict[str, Any]]
    counts: List[int]
    total_score: float
    mean_score: float
    metrics: Dict[str, float]
    item_scores: List[float]

    def to_dict(self) -> Dict[str, Any]:
        picked = []
        for it, cnt, sc in zip(self.items, self.counts, self.item_scores):
            picked.append({"count": cnt, "item_score": round(sc, 2), "row": it})
        return {
            "total_score": round(self.total_score, 2),
            "mean_score": round(self.mean_score, 2),
            "item_count": sum(self.counts),
            "metrics": dict(self.metrics),
            "items": picked,
        }


@dataclass
class SetSolution:
    slots: Dict[str, Dict[str, Any]]
    total_score: float
    slot_scores: Dict[str, float]
    metrics: Dict[str, float]
    breakdown: List[TermBreakdown]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": round(self.total_score, 2),
            "slot_scores": {k: round(v, 2) for k, v in self.slot_scores.items()},
            "metrics": dict(self.metrics),
            "slots": self.slots,
            "breakdown": [b.to_dict() for b in self.breakdown],
        }


def _row_signature(row: Dict[str, Any], fallback_idx: int) -> str:
    for key in ("id", "sku", "title", "name", "model"):
        if row.get(key) is not None:
            return f"{key}:{row[key]}"
    return f"idx:{fallback_idx}"


VALID_NORM_MODES: Tuple[str, ...] = ("robust", "minmax", "rank")
DEFAULT_NORM_MODE: str = "robust"


def validate_norm_mode(norm: Optional[str]) -> str:
    """Validates and normalizes a scoring normalization strategy ('robust' | 'minmax' | 'rank')."""
    mode = (norm or DEFAULT_NORM_MODE).strip().lower()
    if mode not in VALID_NORM_MODES:
        raise ValueError(f"Unsupported normalization mode '{norm}'. Use one of: {', '.join(VALID_NORM_MODES)}.")
    return mode


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = max(0.0, min(float(n - 1), q * (n - 1)))
    lo = int(math.floor(pos))
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


@dataclass(frozen=True)
class _TermScale:
    mode: str
    lo: Optional[float]
    hi: Optional[float]
    rank_map: Optional[Dict[float, float]] = None

    @classmethod
    def from_values(cls, finite_vals: Sequence[float], norm: str = DEFAULT_NORM_MODE) -> _TermScale:
        mode = validate_norm_mode(norm)
        if not finite_vals:
            return cls(mode=mode, lo=None, hi=None)
        sv = sorted(finite_vals)
        mn, mx = sv[0], sv[-1]
        if mode == "rank":
            n = len(sv)
            if n <= 1 or mx <= mn:
                return cls(mode=mode, lo=mn, hi=mx, rank_map={mn: 0.5})
            rmap: Dict[float, float] = {}
            i = 0
            while i < n:
                j = i + 1
                while j < n and sv[j] == sv[i]:
                    j += 1
                avg_rank = ((i + (j - 1)) / 2.0) / (n - 1)
                rmap[sv[i]] = avg_rank
                i = j
            return cls(mode=mode, lo=mn, hi=mx, rank_map=rmap)
        if mode == "robust" and len(sv) >= 8:
            q1 = _percentile(sv, 0.25)
            q3 = _percentile(sv, 0.75)
            iqr = q3 - q1
            if iqr > 0.0:
                lo = max(mn, q1 - 1.5 * iqr)
                hi = min(mx, q3 + 1.5 * iqr)
                if hi > lo:
                    return cls(mode=mode, lo=lo, hi=hi)
            # Fall back to nearest-rank P5..P95 if IQR collapses (e.g. >50% ties)
            lo_idx = max(0, int(len(sv) * 0.05))
            hi_idx = min(len(sv) - 1, int(math.ceil(len(sv) * 0.95)) - 1)
            if sv[hi_idx] > sv[lo_idx]:
                return cls(mode=mode, lo=sv[lo_idx], hi=sv[hi_idx])
        return cls(mode=mode, lo=mn, hi=mx)

    def normalize(self, val: Optional[float], weight: float) -> Tuple[float, float]:
        if val is None or self.lo is None or self.hi is None:
            return (0.0 if weight >= 0 else 1.0), 0.0
        if self.mode == "rank" and self.rank_map is not None:
            norm = self.rank_map.get(val)
            if norm is None:
                norm = 0.5 if self.hi <= self.lo else max(0.0, min(1.0, (val - self.lo) / (self.hi - self.lo)))
            util = norm if weight >= 0 else (1.0 - norm)
            return norm, util
        return _normalize_value(val, self.lo, self.hi, weight)


def _diagnose_where(
    rows: Sequence[Dict[str, Any]],
    where_clauses: Optional[Sequence[str]],
    slot_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Explains which `where` clause(s) eliminated all rows."""
    clauses = [str(w).strip() for w in (where_clauses or []) if str(w).strip()]
    if not rows:
        label = f"slot '{slot_name}'" if slot_name else "source"
        return {"summary": f"No input rows in {label}.", "reasons": [f"Input {label} has 0 rows."]}
    if not clauses:
        return {"summary": "No rows remained after filtering.", "reasons": []}
    reasons: List[str] = []
    clause_counts: Dict[str, int] = {}
    for w in clauses:
        expr = SafeExpression(w)
        passed = 0
        for row in rows:
            ctx = {**row, slot_name: row} if slot_name else row
            if expr.evaluate(ctx):
                passed += 1
        clause_counts[w] = passed
        if passed == 0:
            reasons.append(f"Filter '{w}' matched 0 / {len(rows)} rows.")
    if not reasons:
        reasons.append(f"Combined filters ({', '.join(clauses)}) matched 0 / {len(rows)} rows.")
    return {
        "summary": reasons[0],
        "reasons": reasons,
        "where_matches": clause_counts,
    }


def _prepare_rows(
    rows: Sequence[Dict[str, Any]],
    formulas: Optional[Sequence[str] | Dict[str, str]] = None,
    where: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    f_specs = parse_formulas(formulas)
    processed = apply_row_formulas(list(rows), f_specs)
    return filter_rows(processed, where)


def _compute_term_ranges(
    rows: Sequence[Dict[str, Any]],
    terms: Sequence[WeightTerm],
    norm: str = DEFAULT_NORM_MODE,
) -> List[Tuple[_TermScale, List[Optional[float]]]]:
    """Evaluates each WeightTerm on every row and returns `(_TermScale, per_row_values)`."""
    results: List[Tuple[_TermScale, List[Optional[float]]]] = []
    for term in terms:
        vals: List[Optional[float]] = []
        finite_vals: List[float] = []
        for r in rows:
            num = to_number(term.compiled.evaluate(r))
            vals.append(num)
            if num is not None:
                finite_vals.append(num)
        results.append((_TermScale.from_values(finite_vals, norm=norm), vals))
    return results


def _normalize_value(val: Optional[float], min_v: Optional[float], max_v: Optional[float], weight: float) -> Tuple[float, float]:
    """
    Normalizes `val` to [0, 1] and computes directional utility in [0, 1].
    Missing (`None`) values receive worst-case utility (0.0).
    """
    if val is None or min_v is None or max_v is None:
        return (0.0 if weight >= 0 else 1.0), 0.0
    if max_v <= min_v:
        return 0.5, 0.5
    norm = max(0.0, min(1.0, (val - min_v) / (max_v - min_v)))
    util = norm if weight >= 0 else (1.0 - norm)
    return norm, util


def score_rows_normalized(
    rows: Sequence[Dict[str, Any]],
    terms: Sequence[WeightTerm],
    norm: str = DEFAULT_NORM_MODE,
) -> Tuple[List[float], List[List[TermBreakdown]]]:
    """Computes `[0, 100]` scores and per-term breakdowns across `rows`."""
    norm_mode = validate_norm_mode(norm)
    if not rows:
        return [], []
    if not terms:
        return [50.0] * len(rows), [[] for _ in rows]

    total_abs_w = sum(abs(t.weight) for t in terms) or 1.0
    ranges = _compute_term_ranges(rows, terms, norm=norm_mode)

    scores: List[float] = []
    breakdowns: List[List[TermBreakdown]] = []
    for row_idx in range(len(rows)):
        row_total = 0.0
        row_bd: List[TermBreakdown] = []
        for term, (scale, vals) in zip(terms, ranges):
            raw = vals[row_idx]
            norm_v, util = scale.normalize(raw, term.weight)
            contrib = (abs(term.weight) / total_abs_w) * util * 100.0
            row_total += contrib
            row_bd.append(
                TermBreakdown(
                    expression=term.expression,
                    weight=term.weight,
                    raw_value=round(raw, 4) if raw is not None else None,
                    normalized=norm_v,
                    contribution=contrib,
                )
            )
        scores.append(round(row_total, 2))
        breakdowns.append(row_bd)
    return scores, breakdowns


def solve_rank(
    rows: Sequence[Dict[str, Any]],
    weights: str | Dict[str, Any] | Sequence[str],
    formulas: Optional[Sequence[str] | Dict[str, str]] = None,
    where: Optional[Sequence[str]] = None,
    top_n: int = 20,
    norm: str = DEFAULT_NORM_MODE,
) -> Tuple[List[RankedItem], Dict[str, Any]]:
    """
    Ranks rows from a single (optionally reference-enriched) table using signed weights
    and configurable normalization (`robust` P5-P95, `minmax`, or `rank`).
    """
    norm_mode = validate_norm_mode(norm)
    total_input = len(rows)
    f_specs = parse_formulas(formulas)
    processed = apply_row_formulas(list(rows), f_specs)
    filtered = filter_rows(processed, where)
    terms = parse_weights(weights) if weights else []
    scores, breakdowns = score_rows_normalized(filtered, terms, norm=norm_mode)

    ranked = [
        RankedItem(row=row, score=score, breakdown=bd, index=idx)
        for idx, (row, score, bd) in enumerate(zip(filtered, scores, breakdowns))
    ]
    ranked.sort(key=lambda it: (-it.score, it.index))
    meta: Dict[str, Any] = {
        "mode": "rank",
        "norm": norm_mode,
        "total_input": total_input,
        "after_filter": len(filtered),
        "returned": min(len(ranked), max(0, int(top_n))) if top_n > 0 else len(ranked),
    }
    if not filtered:
        meta["diagnostics"] = _diagnose_where(processed, where)
    if top_n > 0:
        ranked = ranked[: int(top_n)]
    return ranked, meta


def _parse_limits(limits: Optional[Sequence[str | LimitSpec]]) -> List[LimitSpec]:
    if not limits:
        return []
    out: List[LimitSpec] = []
    for lim in limits:
        if isinstance(lim, LimitSpec):
            out.append(lim)
        elif str(lim).strip():
            out.append(LimitSpec.parse(str(lim)))
    return out


def _collect_limit_metrics(
    limits: Sequence[LimitSpec],
    items: Sequence[Dict[str, Any]],
    counts: Optional[Sequence[int]] = None,
) -> Tuple[bool, Dict[str, float]]:
    metrics: Dict[str, float] = {}
    all_ok = True
    for lim in limits:
        ok, val = lim.evaluate_items(items, counts=counts)
        metrics[lim.raw] = val
        if not ok:
            all_ok = False
    return all_ok, metrics


def _resolve_item_max_qty(row: Dict[str, Any], default_max: int, qty_field: Optional[str]) -> int:
    if not qty_field:
        return default_max
    num = to_number(row.get(qty_field))
    if num is None:
        return default_max
    return max(0, min(default_max, int(num)))


def solve_basket(
    rows: Sequence[Dict[str, Any]],
    weights: str | Dict[str, Any] | Sequence[str],
    limits: Optional[Sequence[str | LimitSpec]] = None,
    formulas: Optional[Sequence[str] | Dict[str, str]] = None,
    where: Optional[Sequence[str]] = None,
    top_n: int = 5,
    max_per_item: int = 1,
    qty_field: Optional[str] = None,
    max_nodes: int = 120000,
    norm: str = DEFAULT_NORM_MODE,
) -> Tuple[List[BasketSolution], Dict[str, Any]]:
    """
    Solves 0/1 or bounded multi-item knapsack from a pool of rows under multi-resource limits.
    Uses branch-and-bound with fractional-knapsack upper-bound pruning and infeasibility diagnostics.
    """
    norm_mode = validate_norm_mode(norm)
    total_input = len(rows)
    f_specs = parse_formulas(formulas)
    processed = apply_row_formulas(list(rows), f_specs)
    filtered = filter_rows(processed, where)
    terms = parse_weights(weights) if weights else []
    parsed_limits = _parse_limits(limits)
    scores, _ = score_rows_normalized(filtered, terms, norm=norm_mode)

    if not filtered:
        return [], {
            "mode": "basket",
            "norm": norm_mode,
            "total_input": total_input,
            "after_filter": 0,
            "nodes_explored": 0,
            "diagnostics": _diagnose_where(processed, where),
        }

    max_qty_list = [_resolve_item_max_qty(r, max(1, int(max_per_item)), qty_field) for r in filtered]

    # Precompute item costs for upper-monotone limits (sum(expr) <= R with non-negative costs)
    upper_sum_limits: List[Tuple[LimitSpec, List[float]]] = []
    lower_sum_limits: List[Tuple[LimitSpec, List[float]]] = []
    count_upper_rhs: Optional[float] = None
    for lim in parsed_limits:
        if lim.agg == "count" and lim.op in ("<=", "<"):
            bound = lim.rhs if lim.op == "<=" else (lim.rhs - 1e-9)
            count_upper_rhs = bound if count_upper_rhs is None else min(count_upper_rhs, bound)
        elif lim.agg == "sum" and lim.inner_compiled is not None:
            item_vals = [to_number(lim.inner_compiled.evaluate(r)) or 0.0 for r in filtered]
            if lim.op in ("<=", "<") and all(v >= 0.0 for v in item_vals):
                upper_sum_limits.append((lim, item_vals))
            elif lim.op in (">=", ">") and all(v >= 0.0 for v in item_vals):
                lower_sum_limits.append((lim, item_vals))

    # Order items by efficiency (score / primary upper cost) descending so strong solutions appear immediately
    indices = [i for i, mq in enumerate(max_qty_list) if mq > 0]
    if upper_sum_limits:
        primary_costs = upper_sum_limits[0][1]
        indices.sort(key=lambda i: (-(scores[i] / max(1e-6, primary_costs[i])), -scores[i], primary_costs[i], i))
    else:
        indices.sort(key=lambda i: (-scores[i], i))

    # Adaptive window: fractional-knapsack pruning scales comfortably to larger candidate sets
    cap = 120 if upper_sum_limits else 60
    active = indices[:cap] if len(indices) > cap else indices
    m = len(active)

    suffix_max_util = [0.0] * (m + 1)
    for k in range(m - 1, -1, -1):
        idx = active[k]
        suffix_max_util[k] = suffix_max_util[k + 1] + scores[idx] * max_qty_list[idx]

    suffix_lower_sums: List[List[float]] = []
    for _, vals in lower_sum_limits:
        arr = [0.0] * (m + 1)
        for k in range(m - 1, -1, -1):
            idx = active[k]
            arr[k] = arr[k + 1] + vals[idx] * max_qty_list[idx]
        suffix_lower_sums.append(arr)

    best_entries: List[Tuple[float, float, Tuple[Tuple[int, int], ...], Dict[str, float]]] = []
    seen_sigs = set()
    nodes = 0
    truncated = False

    chosen_pairs: List[Tuple[int, int]] = []
    curr_upper = [0.0] * len(upper_sum_limits)
    curr_lower = [0.0] * len(lower_sum_limits)

    def _upper_bound(pos: int, curr_util: float, curr_count: int) -> float:
        if count_upper_rhs is not None and int(math.floor(count_upper_rhs - curr_count + 1e-9)) <= 0:
            return curr_util
        if not upper_sum_limits:
            return curr_util + suffix_max_util[pos]
        lim0, vals0 = upper_sum_limits[0]
        rem_cap = (lim0.rhs if lim0.op == "<=" else (lim0.rhs - 1e-9)) - curr_upper[0]
        if rem_cap < -1e-9:
            return curr_util
        bound = curr_util
        for k in range(pos, m):
            idx = active[k]
            sc = scores[idx]
            if sc <= 0.0:
                continue
            q_avail = max_qty_list[idx]
            cost = vals0[idx]
            if cost <= 0.0:
                bound += sc * q_avail
                continue
            need = cost * q_avail
            if need <= rem_cap + 1e-9:
                bound += sc * q_avail
                rem_cap -= need
            else:
                bound += sc * (rem_cap / cost)
                break
        return min(bound, curr_util + suffix_max_util[pos])

    def _record_leaf(curr_util: float, curr_count: int) -> None:
        if curr_count <= 0:
            return
        sig = tuple(sorted(chosen_pairs))
        if sig in seen_sigs:
            return
        sel_items = [filtered[idx] for idx, _ in chosen_pairs]
        sel_counts = [qty for _, qty in chosen_pairs]
        ok, metrics = _collect_limit_metrics(parsed_limits, sel_items, counts=sel_counts)
        if not ok:
            return
        seen_sigs.add(sig)
        mean_sc = curr_util / max(1, curr_count)
        best_entries.append((round(curr_util, 6), round(mean_sc, 6), sig, metrics))
        best_entries.sort(key=lambda e: (-e[0], -e[1], e[2]))
        if len(best_entries) > max(10, top_n * 4):
            del best_entries[max(10, top_n * 4) :]

    def _dfs(pos: int, curr_util: float, curr_count: int) -> None:
        nonlocal nodes, truncated
        if nodes >= max_nodes:
            truncated = True
            return
        nodes += 1

        if pos == m:
            _record_leaf(curr_util, curr_count)
            return

        # Admissible fractional-knapsack upper-bound pruning once we have enough solutions
        if len(best_entries) >= top_n:
            cutoff = best_entries[top_n - 1][0]
            if _upper_bound(pos, curr_util, curr_count) <= cutoff + 1e-9:
                return

        # Lower-bound reachability pruning
        for l_idx, (lim, _) in enumerate(lower_sum_limits):
            if curr_lower[l_idx] + suffix_lower_sums[l_idx][pos] < lim.rhs - 1e-9:
                return

        item_idx = active[pos]
        max_q = max_qty_list[item_idx]
        if count_upper_rhs is not None:
            rem_count = int(math.floor(count_upper_rhs - curr_count + 1e-9))
            if rem_count <= 0:
                max_q = 0
            else:
                max_q = min(max_q, rem_count)

        # Determine maximum feasible quantity against all monotone <= upper limits
        for u_idx, (lim, vals) in enumerate(upper_sum_limits):
            unit_cost = vals[item_idx]
            if unit_cost > 0:
                allowed = lim.rhs if lim.op == "<=" else (lim.rhs - 1e-9)
                rem = allowed - curr_upper[u_idx]
                if rem < -1e-9:
                    max_q = 0
                    break
                max_q = min(max_q, int(math.floor((rem + 1e-9) / unit_cost)))

        # Branch descending from max_q down to 0 (greedy-first explores strong packings early)
        for q in range(max_q, -1, -1):
            if nodes >= max_nodes:
                truncated = True
                return
            if q > 0:
                for u_idx, (_, vals) in enumerate(upper_sum_limits):
                    curr_upper[u_idx] += vals[item_idx] * q
                for l_idx, (_, vals) in enumerate(lower_sum_limits):
                    curr_lower[l_idx] += vals[item_idx] * q
                chosen_pairs.append((item_idx, q))
                _dfs(pos + 1, curr_util + scores[item_idx] * q, curr_count + q)
                chosen_pairs.pop()
                for u_idx, (_, vals) in enumerate(upper_sum_limits):
                    curr_upper[u_idx] -= vals[item_idx] * q
                for l_idx, (_, vals) in enumerate(lower_sum_limits):
                    curr_lower[l_idx] -= vals[item_idx] * q
            else:
                _dfs(pos + 1, curr_util, curr_count)

    _dfs(0, 0.0, 0)

    solutions: List[BasketSolution] = []
    for total_u, mean_u, sig, metrics in best_entries[: max(1, int(top_n))]:
        sel_items = [filtered[idx] for idx, _ in sig]
        sel_counts = [qty for _, qty in sig]
        sel_scores = [scores[idx] for idx, _ in sig]
        solutions.append(
            BasketSolution(
                items=sel_items,
                counts=sel_counts,
                total_score=round(total_u, 2),
                mean_score=round(mean_u, 2),
                metrics=metrics,
                item_scores=sel_scores,
            )
        )

    meta: Dict[str, Any] = {
        "mode": "basket",
        "norm": norm_mode,
        "total_input": total_input,
        "after_filter": len(filtered),
        "candidates_considered": m,
        "nodes_explored": nodes,
        "truncated": truncated,
    }
    if not solutions:
        reasons: List[str] = []
        if m == 0:
            reasons.append("All candidate items have max quantity 0.")
        for lim, vals in upper_sum_limits:
            active_vals = [vals[idx] for idx in active]
            if active_vals and min(active_vals) > lim.rhs + 1e-9:
                reasons.append(f"Limit '{lim.raw}' cannot fit any single item (cheapest={min(active_vals):g}).")
        for l_idx, (lim, _) in enumerate(lower_sum_limits):
            max_possible = suffix_lower_sums[l_idx][0]
            if max_possible < lim.rhs - 1e-9:
                reasons.append(f"Limit '{lim.raw}' unreachable (max possible sum={max_possible:g}).")
        if not reasons and parsed_limits:
            reasons.append(f"No combination satisfies all limits: {', '.join(l.raw for l in parsed_limits)}.")
        meta["diagnostics"] = {
            "summary": reasons[0] if reasons else "No feasible basket found.",
            "reasons": reasons,
        }
    return solutions, meta


def _filter_slot_rows(
    slot_name: str,
    rows: Sequence[Dict[str, Any]],
    formulas: Sequence[FormulaSpec],
    where_clauses: Sequence[str],
) -> List[Dict[str, Any]]:
    """
    Applies row formulas and filters relevant to `slot_name`.
    Supports both slot-prefixed clauses (`cpu.cores >= 6`) and unqualified clauses matching row keys.
    """
    processed: List[Dict[str, Any]] = []
    for raw_row in rows:
        item = dict(raw_row)
        for f in formulas:
            val = f.compiled.evaluate({**item, slot_name: item})
            if isinstance(val, float) and math.isfinite(val):
                item[f.name] = round(val, 6)
            else:
                item[f.name] = val
        processed.append(item)
    if not where_clauses:
        return processed

    out: List[Dict[str, Any]] = []
    compiled = [(w, SafeExpression(w)) for w in where_clauses if str(w).strip()]
    for row in processed:
        keep = True
        ctx = {**row, slot_name: row}
        for _, expr in compiled:
            # Skip single-slot filters targeted at a *different* slot (when root matches other slot names)
            if not expr.evaluate(ctx):
                # Only reject if this expression actually applies to fields present in row or slot_name
                roots = expr.roots_referenced
                if slot_name in roots or all(r in row for r in roots) or not roots:
                    keep = False
                    break
        if keep:
            out.append(row)
    return out


def _strip_agg_if_needed(term: WeightTerm) -> Tuple[Optional[str], WeightTerm]:
    m = _AGG_WRAPPER_RE.match(term.expression)
    if m:
        inner = m.group(1).strip()
        return "agg", WeightTerm(expression=term.expression, weight=term.weight, compiled=SafeExpression(inner))
    return None, term


def _extract_slot_eq_fields(
    parsed_rules: Sequence[RuleSpec],
    slot_names: Sequence[str],
) -> Dict[str, Tuple[Tuple[str, ...], ...]]:
    """Extracts per-slot field chains referenced in cross-slot `==` compatibility rules."""
    slot_set = set(slot_names)
    found: Dict[str, List[Tuple[str, ...]]] = {s: [] for s in slot_names}

    for rule in parsed_rules:
        for node in ast.walk(rule.compiled.tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for left_n, op, right_n in zip(operands, node.ops, operands[1:]):
                if not isinstance(op, ast.Eq):
                    continue
                if not isinstance(left_n, ast.Attribute) or not isinstance(right_n, ast.Attribute):
                    continue
                l_chain = _extract_attr_chain(left_n)
                r_chain = _extract_attr_chain(right_n)
                l_slot, r_slot = l_chain[0], r_chain[0]
                if l_slot in slot_set and r_slot in slot_set and l_slot != r_slot:
                    l_path = tuple(l_chain[1:])
                    r_path = tuple(r_chain[1:])
                    if l_path and l_path not in found[l_slot]:
                        found[l_slot].append(l_path)
                    if r_path and r_path not in found[r_slot]:
                        found[r_slot].append(r_path)

    return {s: tuple(paths) for s, paths in found.items()}


def _prune_slot_candidates(
    slot_name: str,
    rows: List[Dict[str, Any]],
    slot_scores: List[float],
    limit_exprs: Sequence[SafeExpression],
    pareto_limit: int,
    group_paths: Sequence[Tuple[str, ...]] = (),
) -> List[Tuple[Dict[str, Any], float]]:
    """
    Prunes large slot candidate lists while preserving:
    - Representation across cross-slot `==` compatibility groups (e.g. socket/DDR buckets)
    - Top utility candidates
    - Lowest resource-cost candidates across ALL upper-monotone limits (so multi-resource budgets don't starve)
    """
    paired = list(zip(rows, slot_scores))
    if len(paired) <= pareto_limit:
        paired.sort(key=lambda x: -x[1])
        return paired

    def _cost_val_for(expr: SafeExpression, row: Dict[str, Any]) -> float:
        val = to_number(expr.evaluate(row))
        return val if val is not None else float("inf")

    if group_paths:
        buckets: Dict[Tuple[Any, ...], List[Tuple[int, Tuple[Dict[str, Any], float]]]] = {}
        for orig_idx, pair in enumerate(paired):
            key = tuple(resolve_field_path(pair[0], path) for path in group_paths)
            buckets.setdefault(key, []).append((orig_idx, pair))

        if len(buckets) > 1:
            eff_limit = min(len(paired), max(pareto_limit, min(96, len(buckets) * 6)))
            keep_by_sig: Dict[int, Tuple[Dict[str, Any], float]] = {}

            bucket_util_iters = []
            bucket_cost_iters = []
            for key in sorted(buckets.keys(), key=lambda k: tuple("" if v is None else str(v) for v in k)):
                entries = buckets[key]
                bucket_util_iters.append(iter(sorted(entries, key=lambda p: (-p[1][1], p[0]))))
                for lim_expr in limit_exprs:
                    bucket_cost_iters.append(
                        iter(sorted(entries, key=lambda p, le=lim_expr: (_cost_val_for(le, p[1][0]), -p[1][1], p[0])))
                    )

            util_cap = eff_limit if not limit_exprs else max(1, int(eff_limit * 0.75))
            active_iters = list(bucket_util_iters)
            while active_iters and len(keep_by_sig) < util_cap:
                next_iters = []
                for it in active_iters:
                    added = False
                    for orig_idx, pair in it:
                        if orig_idx not in keep_by_sig:
                            keep_by_sig[orig_idx] = pair
                            added = True
                            break
                    if added and len(keep_by_sig) < util_cap:
                        next_iters.append(it)
                    if len(keep_by_sig) >= util_cap:
                        break
                active_iters = next_iters

            if limit_exprs and len(keep_by_sig) < eff_limit:
                active_costs = list(bucket_cost_iters)
                while active_costs and len(keep_by_sig) < eff_limit:
                    next_costs = []
                    for it in active_costs:
                        added = False
                        for orig_idx, pair in it:
                            if orig_idx not in keep_by_sig:
                                keep_by_sig[orig_idx] = pair
                                added = True
                                break
                        if added and len(keep_by_sig) < eff_limit:
                            next_costs.append(it)
                        if len(keep_by_sig) >= eff_limit:
                            break
                    active_costs = next_costs

            result = list(keep_by_sig.values())
            result.sort(key=lambda p: -p[1])
            return result

    # Keep top ~75% by utility and up to ~25% round-robin by minimum cost across all limit expressions
    keep_by_sig = {}
    by_util = sorted(enumerate(paired), key=lambda p: (-p[1][1], p[0]))
    top_k = max(1, int(pareto_limit * 0.75))
    for orig_idx, pair in by_util[:top_k]:
        keep_by_sig[orig_idx] = pair

    if limit_exprs and len(keep_by_sig) < pareto_limit:
        cost_iters = [
            iter(sorted(enumerate(paired), key=lambda p, le=lim_expr: (_cost_val_for(le, p[1][0]), -p[1][1], p[0])))
            for lim_expr in limit_exprs
        ]
        active_costs = list(cost_iters)
        while active_costs and len(keep_by_sig) < pareto_limit:
            next_costs = []
            for it in active_costs:
                added = False
                for orig_idx, pair in it:
                    if orig_idx not in keep_by_sig:
                        keep_by_sig[orig_idx] = pair
                        added = True
                        break
                if added and len(keep_by_sig) < pareto_limit:
                    next_costs.append(it)
                if len(keep_by_sig) >= pareto_limit:
                    break
            active_costs = next_costs

    result = list(keep_by_sig.values())
    result.sort(key=lambda p: -p[1])
    return result


def _select_diverse_sets(
    solutions: List[SetSolution],
    slot_names: Sequence[str],
    top_n: int,
) -> List[SetSolution]:
    """Selects up to `top_n` sets maximizing slot diversity (at most `n_slots - 2` overlapping items)."""
    if len(solutions) <= top_n or len(slot_names) < 2:
        return solutions[:top_n]
    max_overlap = max(0, len(slot_names) - 2)
    sigs = [
        {s: _row_signature(sol.slots[s], idx) for s in slot_names}
        for idx, sol in enumerate(solutions)
    ]
    picked_indices: List[int] = []
    for idx in range(len(solutions)):
        if len(picked_indices) >= top_n:
            break
        cand_sig = sigs[idx]
        if all(
            sum(1 for s in slot_names if cand_sig[s] == sigs[p][s]) <= max_overlap
            for p in picked_indices
        ):
            picked_indices.append(idx)
    # Backfill with remaining highest-scoring sets if fewer than top_n strictly diverse sets exist
    if len(picked_indices) < top_n:
        picked_set = set(picked_indices)
        for idx in range(len(solutions)):
            if idx not in picked_set:
                picked_indices.append(idx)
                if len(picked_indices) >= top_n:
                    break
    return [solutions[i] for i in picked_indices]


def solve_sets(
    slot_pools: Dict[str, Sequence[Dict[str, Any]]],
    weights: str | Dict[str, Any] | Sequence[str],
    limits: Optional[Sequence[str | LimitSpec]] = None,
    rules: Optional[Sequence[str | RuleSpec]] = None,
    formulas: Optional[Sequence[str] | Dict[str, str]] = None,
    where: Optional[Sequence[str]] = None,
    top_n: int = 5,
    pareto_limit: int = 40,
    max_nodes: int = 120000,
    should_abort: Optional[Callable[[], bool]] = None,
    norm: str = DEFAULT_NORM_MODE,
    diverse: bool = False,
) -> Tuple[List[SetSolution], Dict[str, Any]]:
    """
    Solves multi-slot combinatorial optimization (1 item per slot in `slot_pools`) using
    signed-weight normalization (`robust`/`minmax`/`rank`), early AST rule pruning,
    multi-resource bounds, infeasibility diagnostics, and optional diverse Top-N selection.
    """
    norm_mode = validate_norm_mode(norm)
    slot_names = list(slot_pools.keys())
    if not slot_names:
        return [], {"mode": "solve", "norm": norm_mode, "slots": {}, "nodes_explored": 0}

    f_specs = parse_formulas(formulas)
    where_list = list(where or [])
    filtered_pools: Dict[str, List[Dict[str, Any]]] = {
        sname: _filter_slot_rows(sname, slot_pools[sname], f_specs, where_list)
        for sname in slot_names
    }
    empty_slots = [s for s in slot_names if len(filtered_pools[s]) == 0]
    if empty_slots:
        reasons: List[str] = []
        for es in empty_slots:
            raw_slot_rows = _filter_slot_rows(es, slot_pools[es], f_specs, [])
            diag = _diagnose_where(raw_slot_rows, where_list, slot_name=es)
            reasons.append(f"Slot '{es}' empty: {diag['summary']}")
        return [], {
            "mode": "solve",
            "norm": norm_mode,
            "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
            "nodes_explored": 0,
            "feasible_count": 0,
            "diagnostics": {"summary": reasons[0], "reasons": reasons},
        }

    raw_terms = parse_weights(weights) if weights else []
    parsed_limits = _parse_limits(limits)
    parsed_rules: List[RuleSpec] = []
    for r in rules or []:
        if isinstance(r, RuleSpec):
            parsed_rules.append(r)
        elif str(r).strip():
            parsed_rules.append(RuleSpec.parse(str(r)))

    # Classify weight terms into:
    #  1) single-slot terms (`cpu.score`)
    #  2) shared additive terms (`price` or `sum(price)`)
    #  3) cross-slot set terms (`cpu.score / gpu.score`)
    single_slot_terms: Dict[str, List[WeightTerm]] = {s: [] for s in slot_names}
    shared_terms: List[WeightTerm] = []
    cross_slot_terms: List[WeightTerm] = []

    for orig_term in raw_terms:
        agg_kind, term = _strip_agg_if_needed(orig_term)
        roots = term.compiled.roots_referenced
        slot_refs = [r for r in roots if r in slot_pools]
        if len(slot_refs) >= 2:
            cross_slot_terms.append(term)
        elif len(slot_refs) == 1:
            target_slot = slot_refs[0]
            if agg_kind == "agg":
                cross_slot_terms.append(term)
            else:
                single_slot_terms[target_slot].append(term)
        else:
            shared_terms.append(term)

    # Pre-filter candidates that cannot satisfy any monotone upper limit even with minimum costs in all other slots
    # (Done BEFORE computing normalization scales so unaffordable outliers don't skew scoring)
    upper_cost_exprs = [
        lim.inner_compiled
        for lim in parsed_limits
        if lim.is_upper_monotone() and lim.inner_compiled is not None
    ]
    for lim in parsed_limits:
        if lim.agg == "sum" and lim.op in ("<=", "<") and lim.inner_compiled is not None:
            if not any(r in slot_pools for r in lim.inner_compiled.roots_referenced):
                slot_costs: Dict[str, List[float]] = {}
                slot_mins_c: Dict[str, float] = {}
                all_nonneg = True
                for sname in slot_names:
                    cvals: List[float] = []
                    for r in filtered_pools[sname]:
                        cv = to_number(lim.inner_compiled.evaluate(r))
                        if cv is None or cv < 0:
                            all_nonneg = False
                            break
                        cvals.append(cv)
                    if not all_nonneg or not cvals:
                        all_nonneg = False
                        break
                    slot_costs[sname] = cvals
                    slot_mins_c[sname] = min(cvals)
                if all_nonneg:
                    total_min = sum(slot_mins_c.values())
                    allowed = lim.rhs if lim.op == "<=" else (lim.rhs - 1e-9)
                    for sname in slot_names:
                        other_min = total_min - slot_mins_c[sname]
                        kept_rows = [
                            r for r, cv in zip(filtered_pools[sname], slot_costs[sname])
                            if cv + other_min <= allowed + 1e-9
                        ]
                        if len(kept_rows) != len(filtered_pools[sname]):
                            filtered_pools[sname] = kept_rows
                    if any(len(filtered_pools[s]) == 0 for s in slot_names):
                        reason = (
                            f"Limit '{lim.raw}' is infeasible: minimum possible sum across slots "
                            f"is {total_min:g} ({', '.join(f'{s}>={slot_mins_c[s]:g}' for s in slot_names)})."
                        )
                        return [], {
                            "mode": "solve",
                            "norm": norm_mode,
                            "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
                            "nodes_explored": 0,
                            "feasible_count": 0,
                            "diagnostics": {"summary": reason, "reasons": [reason]},
                        }

    # Precompute global min/max sum bounds for shared terms across all slots
    shared_bounds: List[Tuple[WeightTerm, float, float, Dict[str, float], Dict[str, float]]] = []
    for term in shared_terms:
        slot_mins: List[float] = []
        slot_maxs: List[float] = []
        s_mins_map: Dict[str, float] = {}
        s_maxs_map: Dict[str, float] = {}
        for sname in slot_names:
            vals = [to_number(term.compiled.evaluate(r)) for r in filtered_pools[sname]]
            finite = [v for v in vals if v is not None]
            if finite:
                mn, mx = min(finite), max(finite)
                slot_mins.append(mn)
                slot_maxs.append(mx)
                s_mins_map[sname] = mn
                s_maxs_map[sname] = mx
        if slot_mins:
            shared_bounds.append((term, sum(slot_mins), sum(slot_maxs), s_mins_map, s_maxs_map))

    # Precompute single-slot term scales
    single_ranges: Dict[str, List[Tuple[WeightTerm, _TermScale]]] = {s: [] for s in slot_names}
    for sname in slot_names:
        for term in single_slot_terms[sname]:
            vals = []
            for r in filtered_pools[sname]:
                val = to_number(term.compiled.evaluate({**r, sname: r}))
                if val is not None:
                    vals.append(val)
            single_ranges[sname].append((term, _TermScale.from_values(vals, norm=norm_mode)))

    additive_abs_w = sum(abs(t.weight) for s in slot_names for t in single_slot_terms[s]) + sum(
        abs(t.weight) for t, _, _, _, _ in shared_bounds
    )
    total_abs_w = additive_abs_w + sum(abs(t.weight) for t in cross_slot_terms)

    def _slot_additive_score(sname: str, row: Dict[str, Any]) -> Tuple[float, List[Tuple[str, float, Optional[float], float]]]:
        """Returns weighted utility in `[0, additive_abs_w]` for one slot item."""
        if additive_abs_w <= 0 and not cross_slot_terms:
            return 50.0, []
        util_sum = 0.0
        parts: List[Tuple[str, float, Optional[float], float]] = []
        for term, scale in single_ranges[sname]:
            raw = to_number(term.compiled.evaluate({**row, sname: row}))
            _, u = scale.normalize(raw, term.weight)
            util_sum += abs(term.weight) * u
            parts.append((term.expression, term.weight, raw, u))
        for term, g_min, g_max, s_mins_map, s_maxs_map in shared_bounds:
            if sname not in s_mins_map:
                continue
            raw = to_number(term.compiled.evaluate(row))
            if raw is None:
                delta_u = 0.0
            elif g_max <= g_min:
                delta_u = 0.5 / max(1, len(s_mins_map))
            elif term.weight >= 0:
                delta_u = max(0.0, (raw - s_mins_map[sname]) / (g_max - g_min))
            else:
                delta_u = max(0.0, (s_maxs_map[sname] - raw) / (g_max - g_min))
            util_sum += abs(term.weight) * delta_u
        return util_sum, parts

    slot_eq_fields = _extract_slot_eq_fields(parsed_rules, slot_names)
    pruned_slots: Dict[str, List[Tuple[Dict[str, Any], float]]] = {}
    for sname in slot_names:
        raw_scores = [_slot_additive_score(sname, r)[0] for r in filtered_pools[sname]]
        pruned_slots[sname] = _prune_slot_candidates(
            sname,
            filtered_pools[sname],
            raw_scores,
            limit_exprs=upper_cost_exprs,
            pareto_limit=pareto_limit,
            group_paths=slot_eq_fields.get(sname, ()),
        )

    # Order slots so tightly constrained slots appear early for fast partial rule pruning
    rule_degree = {s: sum(1 for rule in parsed_rules if s in rule.slots_involved) for s in slot_names}
    search_order = sorted(slot_names, key=lambda s: (-rule_degree[s], len(pruned_slots[s]), slot_names.index(s)))
    n_slots = len(search_order)

    # Monotone <= limits suffix min-cost bounds (when item cost >= 0 and not cross-slot expr)
    mono_upper_limits: List[Tuple[LimitSpec, Dict[str, List[float]], List[float]]] = []
    for lim in parsed_limits:
        if lim.agg == "sum" and lim.op in ("<=", "<") and lim.inner_compiled is not None:
            if not any(r in slot_pools for r in lim.inner_compiled.roots_referenced):
                per_slot_costs: Dict[str, List[float]] = {}
                all_nonneg = True
                for sname in search_order:
                    costs = [to_number(lim.inner_compiled.evaluate(c)) or 0.0 for c, _ in pruned_slots[sname]]
                    if any(v < 0 for v in costs):
                        all_nonneg = False
                        break
                    per_slot_costs[sname] = costs
                if all_nonneg:
                    suffix_min = [0.0] * (n_slots + 1)
                    for k in range(n_slots - 1, -1, -1):
                        sname = search_order[k]
                        mn = min(per_slot_costs[sname]) if per_slot_costs[sname] else 0.0
                        suffix_min[k] = suffix_min[k + 1] + mn
                    mono_upper_limits.append((lim, per_slot_costs, suffix_min))

    max_slot_util = {
        sname: max((u for _, u in pruned_slots[sname]), default=0.0)
        for sname in search_order
    }
    suffix_max_util = [0.0] * (n_slots + 1)
    for k in range(n_slots - 1, -1, -1):
        suffix_max_util[k] = suffix_max_util[k + 1] + max_slot_util[search_order[k]]

    max_cross_w = sum(abs(t.weight) for t in cross_slot_terms)

    feasible_sets: List[Tuple[Dict[str, Dict[str, Any]], float, Dict[str, float], Dict[str, float]]] = []
    top_scores_heap: List[float] = []
    cutoff_cap = max(top_n * 8, 48) if diverse else max(top_n * 4, 24)
    seen_set_sigs = set()
    nodes = 0
    truncated = False
    rule_rejections: Dict[str, int] = {r.expression: 0 for r in parsed_rules}
    limit_rejections: Dict[str, int] = {lim.raw: 0 for lim in parsed_limits}
    best_limit_observed: Dict[str, float] = {}

    # Pre-assign each rule to the exact search depth where its last slot becomes bound
    slot_depth = {s: idx for idx, s in enumerate(search_order)}
    rules_at_depth: List[List[RuleSpec]] = [[] for _ in range(n_slots)]
    always_rules: List[RuleSpec] = []
    for rule in parsed_rules:
        depths = [slot_depth[s] for s in rule.slots_involved if s in slot_depth]
        if any(s not in slot_depth for s in rule.slots_involved):
            always_rules.append(rule)
        elif not depths:
            always_rules.append(rule)
        else:
            rules_at_depth[max(depths)].append(rule)

    for rule in always_rules:
        if not rule.evaluate({}):
            reason = f"Rule '{rule.expression}' evaluated False (check referenced slot names: {', '.join(slot_names)})."
            return [], {
                "mode": "solve",
                "norm": norm_mode,
                "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
                "nodes_explored": 0,
                "feasible_count": 0,
                "diagnostics": {"summary": reason, "reasons": [reason]},
            }

    def _record_limit_obs(lim: LimitSpec, val: float) -> None:
        prev = best_limit_observed.get(lim.raw)
        if prev is None:
            best_limit_observed[lim.raw] = val
        elif lim.op in ("<=", "<"):
            best_limit_observed[lim.raw] = min(prev, val)
        elif lim.op in (">=", ">"):
            best_limit_observed[lim.raw] = max(prev, val)
        elif abs(val - lim.rhs) < abs(prev - lim.rhs):
            best_limit_observed[lim.raw] = val

    def _search(
        depth: int,
        current_set: Dict[str, Dict[str, Any]],
        current_slot_scores: Dict[str, float],
        current_add_util: float,
        curr_mono_costs: List[float],
    ) -> None:
        nonlocal nodes, truncated
        if nodes >= max_nodes:
            truncated = True
            return
        if should_abort is not None and (nodes & 0x3F) == 0 and should_abort():
            truncated = True
            return
        nodes += 1

        if depth == n_slots:
            metrics: Dict[str, float] = {}
            for lim in parsed_limits:
                ok, val = lim.evaluate_set(current_set, slot_names=slot_names)
                metrics[lim.raw] = val
                if not ok:
                    limit_rejections[lim.raw] = limit_rejections.get(lim.raw, 0) + 1
                    _record_limit_obs(lim, val)
                    return

            ordered_set = {s: current_set[s] for s in slot_names}
            sig = tuple((s, _row_signature(ordered_set[s], 0)) for s in slot_names)
            if sig in seen_set_sigs:
                return
            seen_set_sigs.add(sig)
            feasible_sets.append((ordered_set, current_add_util, dict(current_slot_scores), metrics))
            if len(top_scores_heap) < cutoff_cap:
                heapq.heappush(top_scores_heap, current_add_util)
            elif current_add_util > top_scores_heap[0]:
                heapq.heapreplace(top_scores_heap, current_add_util)
            return

        # O(1) additive upper bound pruning via cached min-heap floor; skip aggressive pruning under diverse=True
        if not cross_slot_terms and not diverse and len(top_scores_heap) >= cutoff_cap:
            optimistic = current_add_util + suffix_max_util[depth] + max_cross_w
            if len(feasible_sets) >= max(top_n * 16, 128):
                if optimistic <= top_scores_heap[0] + 1e-9:
                    return
            elif optimistic < top_scores_heap[0] - 1e-9:
                return

        sname = search_order[depth]
        pool = pruned_slots[sname]
        depth_rules = rules_at_depth[depth]

        for idx, (cand, cand_util) in enumerate(pool):
            if nodes >= max_nodes:
                truncated = True
                return

            # Monotone <= limit feasibility check with remaining suffix minimum costs
            ok_mono = True
            next_costs = list(curr_mono_costs)
            for l_i, (lim, per_slot_costs, suffix_min) in enumerate(mono_upper_limits):
                new_cost = curr_mono_costs[l_i] + per_slot_costs[sname][idx]
                rem_min = suffix_min[depth + 1]
                allowed = lim.rhs if lim.op == "<=" else (lim.rhs - 1e-9)
                if new_cost + rem_min > allowed + 1e-9:
                    limit_rejections[lim.raw] = limit_rejections.get(lim.raw, 0) + 1
                    _record_limit_obs(lim, new_cost + rem_min)
                    ok_mono = False
                    break
                next_costs[l_i] = new_cost
            if not ok_mono:
                continue

            current_set[sname] = cand
            current_slot_scores[sname] = cand_util

            # Partial rule early pruning (only check rules that just became evaluable at this depth)
            partial_ok = True
            for rule in depth_rules:
                if not rule.evaluate(current_set):
                    rule_rejections[rule.expression] = rule_rejections.get(rule.expression, 0) + 1
                    partial_ok = False
                    break
            if partial_ok:
                _search(depth + 1, current_set, current_slot_scores, current_add_util + cand_util, next_costs)

            del current_set[sname]
            del current_slot_scores[sname]

    _search(0, {}, {}, 0.0, [0.0] * len(mono_upper_limits))

    if not feasible_sets:
        reasons = []
        for expr, cnt in sorted(rule_rejections.items(), key=lambda kv: -kv[1]):
            if cnt > 0:
                reasons.append(f"Rule '{expr}' rejected {cnt} partial combination(s).")
        for raw_lim, cnt in sorted(limit_rejections.items(), key=lambda kv: -kv[1]):
            if cnt > 0:
                obs = best_limit_observed.get(raw_lim)
                obs_hint = f" (best observed: {obs:g})" if obs is not None else ""
                reasons.append(f"Limit '{raw_lim}' rejected {cnt} combination(s){obs_hint}.")
        summary = reasons[0] if reasons else "No valid combination satisfies all rules and limits."
        return [], {
            "mode": "solve",
            "norm": norm_mode,
            "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
            "nodes_explored": nodes,
            "feasible_count": 0,
            "truncated": truncated,
            "diagnostics": {
                "summary": summary,
                "reasons": reasons,
                "rule_rejections": {k: v for k, v in rule_rejections.items() if v > 0},
                "limit_rejections": {k: v for k, v in limit_rejections.items() if v > 0},
            },
        }

    # Finalize normalization for cross-slot and shared terms across discovered feasible sets
    cross_ranges: List[Tuple[WeightTerm, _TermScale, List[Optional[float]]]] = []
    for term in cross_slot_terms:
        vals: List[Optional[float]] = []
        finite: List[float] = []
        for sdict, _, _, _ in feasible_sets:
            v = to_number(term.compiled.evaluate(sdict))
            vals.append(v)
            if v is not None:
                finite.append(v)
        cross_ranges.append((term, _TermScale.from_values(finite, norm=norm_mode), vals))

    shared_set_ranges: List[Tuple[WeightTerm, _TermScale, List[Optional[float]]]] = []
    for term, _, _, _, _ in shared_bounds:
        vals = []
        finite = []
        for sdict, _, _, _ in feasible_sets:
            sub_vals = [to_number(term.compiled.evaluate(sdict[s])) for s in slot_names]
            valid = [v for v in sub_vals if v is not None]
            agg_v = sum(valid) if valid else None
            vals.append(agg_v)
            if agg_v is not None:
                finite.append(agg_v)
        shared_set_ranges.append((term, _TermScale.from_values(finite, norm=norm_mode), vals))

    solutions: List[SetSolution] = []
    eff_total_w = total_abs_w if total_abs_w > 0 else 1.0
    for f_idx, (sdict, _, raw_slot_scores, metrics) in enumerate(feasible_sets):
        bd: List[TermBreakdown] = []
        score_sum = 0.0
        if total_abs_w <= 0:
            score_sum = 50.0
        else:
            # Single-slot normalized terms
            for sname in slot_names:
                row = sdict[sname]
                for term, scale in single_ranges[sname]:
                    raw = to_number(term.compiled.evaluate({**row, sname: row}))
                    norm_v, util = scale.normalize(raw, term.weight)
                    c = (abs(term.weight) / eff_total_w) * util * 100.0
                    score_sum += c
                    bd.append(
                        TermBreakdown(
                            expression=term.expression,
                            weight=term.weight,
                            raw_value=round(raw, 4) if raw is not None else None,
                            normalized=norm_v,
                            contribution=c,
                        )
                    )
            # Shared terms aggregated accurately across the assembled set
            for term, scale, vals in shared_set_ranges:
                raw = vals[f_idx]
                norm_v, util = scale.normalize(raw, term.weight)
                c = (abs(term.weight) / eff_total_w) * util * 100.0
                score_sum += c
                bd.append(
                    TermBreakdown(
                        expression=term.expression,
                        weight=term.weight,
                        raw_value=round(raw, 4) if raw is not None else None,
                        normalized=norm_v,
                        contribution=c,
                    )
                )
            # Cross-slot terms
            for term, scale, vals in cross_ranges:
                raw = vals[f_idx]
                norm_v, util = scale.normalize(raw, term.weight)
                c = (abs(term.weight) / eff_total_w) * util * 100.0
                score_sum += c
                bd.append(
                    TermBreakdown(
                        expression=term.expression,
                        weight=term.weight,
                        raw_value=round(raw, 4) if raw is not None else None,
                        normalized=norm_v,
                        contribution=c,
                    )
                )

        solutions.append(
            SetSolution(
                slots=sdict,
                total_score=round(score_sum, 2),
                slot_scores=raw_slot_scores,
                metrics=metrics,
                breakdown=bd,
            )
        )

    solutions.sort(key=lambda s: -s.total_score)
    limit_k = max(1, int(top_n))
    selected = _select_diverse_sets(solutions, slot_names, limit_k) if diverse else solutions[:limit_k]
    meta: Dict[str, Any] = {
        "mode": "solve",
        "norm": norm_mode,
        "diverse": diverse,
        "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
        "nodes_explored": nodes,
        "feasible_count": len(feasible_sets),
        "truncated": truncated,
    }
    return selected, meta
