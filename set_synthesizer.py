"""
Generic Domain Set Synthesizer.

Performs constraint-checked, branch-and-bound combinatorial synthesis
for arbitrary multi-component sets (PC builds, fishing tackle kits, audio studios, etc.)
using a decoupled DomainLayout and WeightProfile / DomainScorer.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from analyzer_core import fmt_deal_pct, make_terminal_link, pad_ansi
from currency import format_price, get_active_currency
from domain_layout import DomainLayout
from domain_weights import DomainScorer, WeightProfile
from extensions.base import BaseSorterExtension


def _is_candidate_new(cand: Dict[str, Any]) -> bool:
    if cand.get("is_new"):
        return True
    return str(cand.get("condition") or "").lower().strip() == "new"


class GenericDomainSorter(BaseSorterExtension):
    """
    Template-based combinatorial synthesizer that knows ZERO details about providers.
    Synthesizes sets strictly from layout constraints, scorer weights, and candidate pool.
    """

    def __init__(self, domain_id: str = "generic"):
        self.domain_id = domain_id

    def sort_and_synthesize(
        self,
        *args: Any,
        layout: Optional[DomainLayout] = None,
        scorer: Optional[DomainScorer] = None,
        profile: Optional[WeightProfile] = None,
        candidate_pool: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        budget: Optional[float] = None,
        top_n: int = 5,
        excluded_ids: Optional[set] = None,
        min_new_ratio: float = 0.0,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        if args:
            if isinstance(args[0], DomainLayout):
                layout = args[0]
                if len(args) > 1 and isinstance(args[1], DomainScorer):
                    scorer = args[1]
                if len(args) > 2 and isinstance(args[2], WeightProfile):
                    profile = args[2]
                if len(args) > 3 and isinstance(args[3], dict):
                    candidate_pool = args[3]
                if len(args) > 4:
                    budget = float(args[4])
                if len(args) > 5:
                    top_n = int(args[5])
            elif isinstance(args[0], dict):
                candidate_pool = args[0]
                if len(args) > 1:
                    budget = float(args[1])
                if len(args) > 2:
                    top_n = int(args[2])

        if layout is None:
            raise ValueError("GenericDomainSorter requires a DomainLayout instance.")
        if scorer is None:
            raise ValueError("GenericDomainSorter requires a DomainScorer instance.")
        if profile is None:
            raise ValueError("GenericDomainSorter requires a WeightProfile instance.")

        target_budget = float(budget or kwargs.get("budget", 1000.0))
        target_pool = candidate_pool if candidate_pool is not None else {}

        return synthesize_domain_sets(
            layout=layout,
            scorer=scorer,
            profile=profile,
            candidate_pool=target_pool,
            budget=target_budget,
            top_n=top_n,
            excluded_ids=excluded_ids,
            min_new_ratio=min_new_ratio,
        )


def synthesize_domain_sets(
    layout: DomainLayout,
    scorer: DomainScorer,
    profile: WeightProfile,
    candidate_pool: Dict[str, List[Dict[str, Any]]],
    budget: float,
    top_n: int = 5,
    excluded_ids: Optional[set] = None,
    max_evaluations: int = 25000,
    min_new_ratio: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Synthesizes up to `top_n` optimal set combinations from `candidate_pool`
    strictly satisfying `layout.compatibility_rules`, `min_new_ratio`, and within `budget`.
    Uses cost-floor, new-item-feasibility, and upper-bound score pruning.
    """
    excluded = excluded_ids or set()
    section_ids = layout.section_ids()
    n_sections = len(section_ids)
    required_new_count = max(0, math.ceil(float(min_new_ratio) * n_sections - 1e-9)) if min_new_ratio > 0 else 0

    # 1. Pareto pruning per section
    pruned_pools: Dict[str, List[Dict[str, Any]]] = {}
    for sec in layout.sections:
        cands = candidate_pool.get(sec.id, [])
        scored_cands = []
        for c in cands:
            cid = str(c.get("id", ""))
            if cid in excluded:
                continue
            s_val = scorer.score_component(sec.id, c, profile)
            c_copy = dict(c)
            c_copy["_solver_score"] = s_val
            scored_cands.append(c_copy)

        # Sort by (-score, price)
        scored_cands.sort(key=lambda x: (-x["_solver_score"], float(x.get("price", 0.0))))
        pruned_pools[sec.id] = scored_cands[: sec.pareto_limit]

    # Check for empty sections
    for sec_id in section_ids:
        sec_def = layout.get_section(sec_id)
        if sec_def and sec_def.required and not pruned_pools.get(sec_id):
            return []

    # 2. Precompute suffix bounds for Branch-and-Bound pruning
    best_sets: List[Dict[str, Any]] = []
    min_price_per_sec = {
        sid: (min((float(c.get("price", 0.0)) for c in pruned_pools[sid]), default=0.0))
        for sid in section_ids
    }
    max_score_per_sec = {
        sid: (max((float(c.get("_solver_score", 0.0)) for c in pruned_pools[sid]), default=0.0))
        for sid in section_ids
    }
    has_new_per_sec = {
        sid: (1 if any(_is_candidate_new(c) for c in pruned_pools[sid]) else 0)
        for sid in section_ids
    }
    # Max synergy/surplus/condition bonus any set_scorer can add above mean component score
    MAX_SET_BONUS = 15.0

    evaluations_count = 0

    def _search(
        sec_idx: int,
        current_set: Dict[str, Dict[str, Any]],
        current_cost: float,
        current_score_sum: float,
        current_new_count: int,
    ) -> None:
        nonlocal evaluations_count
        evaluations_count += 1

        if sec_idx == n_sections:
            if current_new_count < required_new_count:
                return
            # All sections assigned — validate full set
            is_valid, _ = layout.validate_combination(current_set, strict=True)
            if not is_valid:
                return

            total_score = scorer.score_set(current_set, profile, budget)
            surplus = budget - current_cost

            set_entry = {
                "components": dict(current_set),
                "total_cost": current_cost,
                "set_score": total_score,
                "surplus": surplus,
                "budget": budget,
                "new_count": current_new_count,
            }

            best_sets.append(set_entry)
            best_sets.sort(key=lambda x: (-x["set_score"], x["total_cost"]))
            if len(best_sets) > top_n * 4:
                del best_sets[top_n * 4 :]
            return

        current_sec_id = section_ids[sec_idx]
        candidates = pruned_pools[current_sec_id]

        remaining_secs = section_ids[sec_idx + 1 :]
        remaining_floor = sum(min_price_per_sec[sid] for sid in remaining_secs)
        remaining_max_score_sum = sum(max_score_per_sec[sid] for sid in remaining_secs)
        remaining_max_new = sum(has_new_per_sec[sid] for sid in remaining_secs)

        # Threshold score of the top_n-th best set found so far
        cutoff_score = best_sets[top_n - 1]["set_score"] if len(best_sets) >= top_n else -1e9

        for cand in candidates:
            if evaluations_count >= max_evaluations:
                break
            cand_price = float(cand.get("price", 0.0))
            if current_cost + cand_price + remaining_floor > budget:
                continue

            cand_is_new = 1 if _is_candidate_new(cand) else 0
            if current_new_count + cand_is_new + remaining_max_new < required_new_count:
                continue

            cand_score = float(cand.get("_solver_score", 0.0))
            if n_sections > 0 and cutoff_score > 0:
                optimistic_mean = (current_score_sum + cand_score + remaining_max_score_sum) / n_sections
                if optimistic_mean + MAX_SET_BONUS < cutoff_score:
                    # Since candidates are sorted by descending _solver_score, all subsequent
                    # candidates in this section have even lower cand_score -> break!
                    break

            next_set = dict(current_set)
            next_set[current_sec_id] = cand

            # Early pruning check for rules involving assigned sections
            is_part_valid, _ = layout.validate_partial(next_set)
            if not is_part_valid:
                continue

            _search(
                sec_idx + 1,
                next_set,
                current_cost + cand_price,
                current_score_sum + cand_score,
                current_new_count + cand_is_new,
            )

    _search(0, {}, 0.0, 0.0, 0)

    # Deduplicate by key components and select top_n
    unique_sets: List[Dict[str, Any]] = []
    seen_combos = set()
    for s in best_sets:
        signature = tuple(sorted(str(c.get("id")) for c in s["components"].values()))
        if signature not in seen_combos:
            seen_combos.add(signature)
            unique_sets.append(s)
            if len(unique_sets) >= top_n:
                break

    return unique_sets


def print_domain_sets(
    layout: DomainLayout,
    profile: WeightProfile,
    sets: List[Dict[str, Any]],
    elapsed_ms: float = 0.0,
) -> None:
    """
    Renders domain set combinations in the standardized terminal layout.
    """
    print(f"   Top {layout.title} sets (solved in {elapsed_ms:.0f}ms):\n")

    for idx, s in enumerate(sets, 1):
        comps = s["components"]
        total_cost = s["total_cost"]
        score = s["set_score"]
        surplus = s["surplus"]
        curr = layout.currency or get_active_currency()
        
        if surplus > 0:
            surplus_col = f"+{format_price(surplus, curr)}"
        elif surplus == 0:
            surplus_col = format_price(0, curr)
        else:
            surplus_col = format_price(surplus, curr)

        # Check if layout provides custom headline formatting
        line3_extra = None
        if layout.headline_formatter:
            res_head = layout.headline_formatter(s, idx, total_cost, score, profile)
            if len(res_head) == 9:
                title_col, score_col, surplus_str, line2_left, line2_mid, line2_right, l3_left, l3_mid, l3_right = res_head
                line3_extra = (l3_left, l3_mid, l3_right)
            else:
                title_col, score_col, surplus_str, line2_left, line2_mid, line2_right = res_head
        else:
            first_sec = layout.sections[0].id if layout.sections else ""
            second_sec = layout.sections[1].id if len(layout.sections) > 1 else ""
            name1 = comps.get(first_sec, {}).get("model") or comps.get(first_sec, {}).get("name", "")
            name2 = comps.get(second_sec, {}).get("model") or comps.get(second_sec, {}).get("name", "")
            title_col = f"Set #{idx}: {name1} + {name2}"
            score_col = f"{format_price(total_cost, curr)} (Score: {score:.1f})"

            avg_disc = sum(float(c.get("discount_pct", 0.0)) for c in comps.values()) / max(1, len(comps))
            line2_left = f"Domain: {layout.title}"
            line2_mid = f"Items: {len(comps)} parts"
            line2_right = f"Mkt: {fmt_deal_pct(avg_disc)}"

        # Truncate title_col and line2_left if needed to avoid column overflow
        if len(title_col) > 38:
            title_col = title_col[:37] + "…"
        if len(line2_left) > 38:
            line2_left = line2_left[:37] + "…"

        print("━" * 82)
        print(f"{title_col:<39}│  {score_col:<23}│  {surplus_col}")
        print(f"{line2_left:<39}│  {line2_mid:<23}│  {line2_right}")
        if line3_extra:
            l3_l, l3_m, l3_r = line3_extra
            print(f"{l3_l:<39}│  {l3_m:<23}│  {l3_r}")
        print("─" * 82)

        for sec in layout.sections:
            cand = comps.get(sec.id)
            if not cand:
                continue
            label, name, price_str, spec1, spec2 = sec.format_row(cand, fmt_deal_pct)
            url = cand.get("url") or (cand.get("summary") or {}).get("url", "")
            linked_name = pad_ansi(make_terminal_link(url, name[:19]), 22)
            price_padded = pad_ansi(price_str, 17)
            spec1_padded = pad_ansi(spec1, 17)

            print(f"  • {label:<7}{linked_name}{price_padded}{spec1_padded}{spec2}")

        print()


def format_sets_markdown(
    layout: DomainLayout,
    profile: WeightProfile,
    sets: List[Dict[str, Any]],
) -> str:
    """Formats synthesized sets into a clean, shareable GitHub/Reddit/Discord Markdown report."""
    curr = layout.currency or get_active_currency()
    lines = [
        f"# {layout.title} — Synthesized Sets (Mode: `{profile.name}`)",
        "",
    ]
    if not sets:
        lines.append("_No valid sets found under budget._")
        return "\n".join(lines)

    for idx, s in enumerate(sets, 1):
        comps = s.get("components") or {}
        total_cost = float(s.get("total_cost", 0.0))
        score = float(s.get("set_score", s.get("score", 0.0)))
        surplus = float(s.get("surplus", 0.0))
        lines.append(
            f"## Set #{idx} — {format_price(total_cost, curr)} "
            f"(Score: {score:.1f} | Surplus: +{format_price(surplus, curr)})"
        )
        lines.append("")
        lines.append("| Section | Component | Price | Condition | Link |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for sec in layout.sections:
            c = comps.get(sec.id)
            if not c:
                continue
            name = c.get("model") or c.get("name") or c.get("title", "Unknown")
            p_str = format_price(float(c.get("price", 0.0)), curr)
            cond = "New" if _is_candidate_new(c) else "Used"
            url = c.get("url") or (c.get("summary") or {}).get("url", "")
            link_cell = f"[View]({url})" if url else "—"
            lines.append(f"| **{sec.label}** | {name} | {p_str} | {cond} | {link_cell} |")
        lines.append("")
    return "\n".join(lines)


def export_domain_sets(
    export_path: str | Path,
    layout: DomainLayout,
    profile: WeightProfile,
    sets: List[Dict[str, Any]],
) -> Path:
    """Writes synthesized sets to `.md` (Markdown) or `.json` file based on extension."""
    out = Path(export_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".json":
        payload = {
            "domain": layout.domain_id,
            "title": layout.title,
            "mode": profile.name,
            "currency": layout.currency or get_active_currency(),
            "count": len(sets),
            "sets": sets,
        }
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    else:
        out.write_text(format_sets_markdown(layout, profile, sets), encoding="utf-8")
    return out
