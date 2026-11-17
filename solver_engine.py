"""
Unified Preference, Normalization, and Multi-Limit Solver Engine for Knapset v2.

Implements the three core optimization modes:
  1. `rank`:   Single-table Top-K ranking with signed-weight min-max [0, 1] normalization & `--explain`.
  2. `basket`: Multi-item 0/1 & bounded knapsack under multi-resource limits & constraints.
  3. `solve`:  Multi-slot set synthesis (1 item per slot) with cross-slot AST rules,
               cross-slot preference expressions, and multi-resource limits.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from formula_engine import (
    FormulaSpec,
    LimitSpec,
    RuleSpec,
    SafeExpression,
    WeightTerm,
    apply_row_formulas,
    filter_rows,
    parse_formulas,
    parse_weights,
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
) -> List[Tuple[Optional[float], Optional[float], List[Optional[float]]]]:
    """Evaluates each WeightTerm on every row and returns `(min_v, max_v, per_row_values)`."""
    results: List[Tuple[Optional[float], Optional[float], List[Optional[float]]]] = []
    for term in terms:
        vals: List[Optional[float]] = []
        finite_vals: List[float] = []
        for r in rows:
            num = to_number(term.compiled.evaluate(r))
            vals.append(num)
            if num is not None:
                finite_vals.append(num)
        if finite_vals:
            results.append((min(finite_vals), max(finite_vals), vals))
        else:
            results.append((None, None, vals))
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
) -> Tuple[List[float], List[List[TermBreakdown]]]:
    """Computes `[0, 100]` scores and per-term breakdowns across `rows`."""
    if not rows:
        return [], []
    if not terms:
        return [50.0] * len(rows), [[] for _ in rows]

    total_abs_w = sum(abs(t.weight) for t in terms) or 1.0
    ranges = _compute_term_ranges(rows, terms)

    scores: List[float] = []
    breakdowns: List[List[TermBreakdown]] = []
    for row_idx in range(len(rows)):
        row_total = 0.0
        row_bd: List[TermBreakdown] = []
        for term, (min_v, max_v, vals) in zip(terms, ranges):
            raw = vals[row_idx]
            norm, util = _normalize_value(raw, min_v, max_v, term.weight)
            contrib = (abs(term.weight) / total_abs_w) * util * 100.0
            row_total += contrib
            row_bd.append(
                TermBreakdown(
                    expression=term.expression,
                    weight=term.weight,
                    raw_value=round(raw, 4) if raw is not None else None,
                    normalized=norm,
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
) -> Tuple[List[RankedItem], Dict[str, Any]]:
    """
    Ranks rows from a single (optionally reference-enriched) table using signed weights
    and min-max `[0, 1]` normalization.
    """
    total_input = len(rows)
    filtered = _prepare_rows(rows, formulas=formulas, where=where)
    terms = parse_weights(weights) if weights else []
    scores, breakdowns = score_rows_normalized(filtered, terms)

    ranked = [
        RankedItem(row=row, score=score, breakdown=bd, index=idx)
        for idx, (row, score, bd) in enumerate(zip(filtered, scores, breakdowns))
    ]
    ranked.sort(key=lambda it: (-it.score, it.index))
    meta = {
        "mode": "rank",
        "total_input": total_input,
        "after_filter": len(filtered),
        "returned": min(len(ranked), max(0, int(top_n))) if top_n > 0 else len(ranked),
    }
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
) -> Tuple[List[BasketSolution], Dict[str, Any]]:
    """
    Solves 0/1 or bounded multi-item knapsack from a pool of rows under multi-resource limits.
    Uses branch-and-bound with exact monotone upper-bound pruning and greedy fallback when huge.
    """
    total_input = len(rows)
    filtered = _prepare_rows(rows, formulas=formulas, where=where)
    terms = parse_weights(weights) if weights else []
    parsed_limits = _parse_limits(limits)
    scores, _ = score_rows_normalized(filtered, terms)

    if not filtered:
        return [], {"mode": "basket", "total_input": total_input, "after_filter": 0, "nodes_explored": 0}

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

    # If the candidate pool is very large and we have many items, cap active candidate window while keeping exactness on <=60 items
    active = indices[:60] if len(indices) > 60 else indices
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

        # Utility upper-bound pruning once we have enough solutions
        if len(best_entries) >= top_n:
            cutoff = best_entries[top_n - 1][0]
            if curr_util + suffix_max_util[pos] <= cutoff + 1e-9:
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

    meta = {
        "mode": "basket",
        "total_input": total_input,
        "after_filter": len(filtered),
        "candidates_considered": m,
        "nodes_explored": nodes,
        "truncated": truncated,
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
    processed = apply_row_formulas(list(rows), formulas)
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


def _prune_slot_candidates(
    slot_name: str,
    rows: List[Dict[str, Any]],
    slot_scores: List[float],
    limit_exprs: Sequence[SafeExpression],
    pareto_limit: int,
) -> List[Tuple[Dict[str, Any], float]]:
    """
    Prunes large slot candidate lists while preserving:
    - Top utility candidates
    - Lowest resource-cost candidates (so tight budgets don't starve)
    """
    paired = list(zip(rows, slot_scores))
    if len(paired) <= pareto_limit:
        paired.sort(key=lambda x: -x[1])
        return paired

    # Keep top ~75% by utility and up to ~25% by minimum resource cost
    keep_by_sig: Dict[int, Tuple[Dict[str, Any], float]] = {}
    by_util = sorted(enumerate(paired), key=lambda p: (-p[1][1], p[0]))
    top_k = max(1, int(pareto_limit * 0.75))
    for orig_idx, pair in by_util[:top_k]:
        keep_by_sig[orig_idx] = pair

    if limit_exprs:
        primary_lim = limit_exprs[0]
        by_cost = sorted(
            enumerate(paired),
            key=lambda p: (to_number(primary_lim.evaluate(p[1][0])) or float("inf"), -p[1][1], p[0]),
        )
        for orig_idx, pair in by_cost:
            if len(keep_by_sig) >= pareto_limit:
                break
            keep_by_sig.setdefault(orig_idx, pair)

    result = list(keep_by_sig.values())
    result.sort(key=lambda p: -p[1])
    return result


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
) -> Tuple[List[SetSolution], Dict[str, Any]]:
    """
    Solves multi-slot combinatorial optimization (1 item per slot in `slot_pools`) using
    signed-weight normalization, early AST rule pruning, multi-resource bounds, and
    cross-slot weighting expressions (e.g. `cpu.score / gpu.score`).
    """
    slot_names = list(slot_pools.keys())
    if not slot_names:
        return [], {"mode": "solve", "slots": {}, "nodes_explored": 0}

    f_specs = parse_formulas(formulas)
    where_list = list(where or [])
    filtered_pools: Dict[str, List[Dict[str, Any]]] = {
        sname: _filter_slot_rows(sname, slot_pools[sname], f_specs, where_list)
        for sname in slot_names
    }
    if any(len(filtered_pools[s]) == 0 for s in slot_names):
        return [], {
            "mode": "solve",
            "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
            "nodes_explored": 0,
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
                # Re-normalize attribute path relative to slot item when evaluating inside slot
                single_slot_terms[target_slot].append(term)
        else:
            shared_terms.append(term)

    # Precompute global min/max sum bounds for shared terms across all slots
    # For a shared term `t`, we evaluate `t` on each slot's items where valid and normalize
    # linearly across [sum(min_s), sum(max_s)] so additive per-slot contributions sum cleanly!
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

    # Precompute single-slot term min/max
    single_ranges: Dict[str, List[Tuple[WeightTerm, Optional[float], Optional[float]]]] = {s: [] for s in slot_names}
    for sname in slot_names:
        for term in single_slot_terms[sname]:
            vals = []
            for r in filtered_pools[sname]:
                val = to_number(term.compiled.evaluate({**r, sname: r}))
                if val is not None:
                    vals.append(val)
            if vals:
                single_ranges[sname].append((term, min(vals), max(vals)))
            else:
                single_ranges[sname].append((term, None, None))

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
        for term, min_v, max_v in single_ranges[sname]:
            raw = to_number(term.compiled.evaluate({**row, sname: row}))
            _, u = _normalize_value(raw, min_v, max_v, term.weight)
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

    # Score & optionally Pareto-prune each slot
    upper_cost_exprs = [
        lim.inner_compiled
        for lim in parsed_limits
        if lim.is_upper_monotone() and lim.inner_compiled is not None
    ]
    pruned_slots: Dict[str, List[Tuple[Dict[str, Any], float]]] = {}
    for sname in slot_names:
        raw_scores = [_slot_additive_score(sname, r)[0] for r in filtered_pools[sname]]
        pruned_slots[sname] = _prune_slot_candidates(
            sname,
            filtered_pools[sname],
            raw_scores,
            limit_exprs=upper_cost_exprs,
            pareto_limit=pareto_limit,
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
    seen_set_sigs = set()
    nodes = 0
    truncated = False

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
        nodes += 1

        if depth == n_slots:
            for rule in parsed_rules:
                if not rule.evaluate(current_set):
                    return
            metrics: Dict[str, float] = {}
            for lim in parsed_limits:
                ok, val = lim.evaluate_set(current_set, slot_names=slot_names)
                metrics[lim.raw] = val
                if not ok:
                    return

            ordered_set = {s: current_set[s] for s in slot_names}
            sig = tuple((s, _row_signature(ordered_set[s], 0)) for s in slot_names)
            if sig in seen_set_sigs:
                return
            seen_set_sigs.add(sig)
            feasible_sets.append((ordered_set, current_add_util, dict(current_slot_scores), metrics))
            return

        # Additive upper bound pruning when no cross-slot score terms can swing rankings above best sets
        if not cross_slot_terms and len(feasible_sets) >= max(top_n * 8, 32):
            cutoff_idx = max(top_n * 2 - 1, top_n - 1)
            cutoff = sorted((x[1] for x in feasible_sets), reverse=True)[min(cutoff_idx, len(feasible_sets) - 1)]
            if current_add_util + suffix_max_util[depth] + max_cross_w < cutoff - 1e-9:
                return

        sname = search_order[depth]
        pool = pruned_slots[sname]

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
                    ok_mono = False
                    break
                next_costs[l_i] = new_cost
            if not ok_mono:
                continue

            current_set[sname] = cand
            current_slot_scores[sname] = cand_util

            # Partial rule early pruning
            partial_ok = True
            for rule in parsed_rules:
                if rule.is_evaluable(current_set) and not rule.evaluate(current_set):
                    partial_ok = False
                    break
            if partial_ok:
                _search(depth + 1, current_set, current_slot_scores, current_add_util + cand_util, next_costs)

            del current_set[sname]
            del current_slot_scores[sname]

    _search(0, {}, {}, 0.0, [0.0] * len(mono_upper_limits))

    if not feasible_sets:
        return [], {
            "mode": "solve",
            "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
            "nodes_explored": nodes,
            "feasible_count": 0,
            "truncated": truncated,
        }

    # Finalize normalization for cross-slot and shared terms across discovered feasible sets
    cross_ranges: List[Tuple[WeightTerm, Optional[float], Optional[float], List[Optional[float]]]] = []
    for term in cross_slot_terms:
        vals: List[Optional[float]] = []
        finite: List[float] = []
        for sdict, _, _, _ in feasible_sets:
            v = to_number(term.compiled.evaluate(sdict))
            vals.append(v)
            if v is not None:
                finite.append(v)
        cross_ranges.append((term, min(finite) if finite else None, max(finite) if finite else None, vals))

    shared_set_ranges: List[Tuple[WeightTerm, Optional[float], Optional[float], List[Optional[float]]]] = []
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
        shared_set_ranges.append((term, min(finite) if finite else None, max(finite) if finite else None, vals))

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
                for term, mn, mx in single_ranges[sname]:
                    raw = to_number(term.compiled.evaluate({**row, sname: row}))
                    norm, util = _normalize_value(raw, mn, mx, term.weight)
                    c = (abs(term.weight) / eff_total_w) * util * 100.0
                    score_sum += c
                    bd.append(
                        TermBreakdown(
                            expression=term.expression,
                            weight=term.weight,
                            raw_value=round(raw, 4) if raw is not None else None,
                            normalized=norm,
                            contribution=c,
                        )
                    )
            # Shared terms aggregated accurately across the assembled set
            for term, mn, mx, vals in shared_set_ranges:
                raw = vals[f_idx]
                norm, util = _normalize_value(raw, mn, mx, term.weight)
                c = (abs(term.weight) / eff_total_w) * util * 100.0
                score_sum += c
                bd.append(
                    TermBreakdown(
                        expression=term.expression,
                        weight=term.weight,
                        raw_value=round(raw, 4) if raw is not None else None,
                        normalized=norm,
                        contribution=c,
                    )
                )
            # Cross-slot terms
            for term, mn, mx, vals in cross_ranges:
                raw = vals[f_idx]
                norm, util = _normalize_value(raw, mn, mx, term.weight)
                c = (abs(term.weight) / eff_total_w) * util * 100.0
                score_sum += c
                bd.append(
                    TermBreakdown(
                        expression=term.expression,
                        weight=term.weight,
                        raw_value=round(raw, 4) if raw is not None else None,
                        normalized=norm,
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
    meta = {
        "mode": "solve",
        "slot_counts": {s: len(filtered_pools[s]) for s in slot_names},
        "nodes_explored": nodes,
        "feasible_count": len(feasible_sets),
        "truncated": truncated,
    }
    return solutions[: max(1, int(top_n))], meta
