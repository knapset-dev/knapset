"""
Template Domain Weights & Scoring Modes.
"""

from __future__ import annotations

from typing import Any, Dict

from domain_weights import CriterionDefinition, DomainScorer, WeightProfile, WeightRegistry


def _score_template_component(section_id: str, candidate: Dict[str, Any], profile: WeightProfile) -> float:
    price = float(candidate.get("price", 1000.0))
    discount = float(candidate.get("discount_pct", 0.0))
    # Normalize score 0..100
    base_score = 50.0 + min(40.0, max(-30.0, discount))
    return base_score


def _score_template_set(set_dict: Dict[str, Dict[str, Any]], profile: WeightProfile, budget: float) -> float:
    total_cost = sum(float(item.get("price", 0.0)) for item in set_dict.values())
    if total_cost > budget and budget > 0:
        return 0.0
    scores = [_score_template_component(sec, item, profile) for sec, item in set_dict.items()]
    return sum(scores) / max(1, len(scores))


def create_template_weights_registry() -> WeightRegistry:
    reg = WeightRegistry(domain_id="template")
    reg.register_criterion(CriterionDefinition("value", "Market Deal & Discount Value", default_weight=0.5))
    reg.register_criterion(CriterionDefinition("perf", "Performance & Tier Rating", default_weight=0.5))

    reg.register_profile(
        WeightProfile(
            name="balanced",
            title="Balanced Set",
            description="Equal balance between cost value and performance rating.",
            weights={"value": 0.5, "perf": 0.5},
        )
    )
    reg.register_profile(
        WeightProfile(
            name="value",
            title="Deal Hunter",
            description="Maximizes market discount and lowest price.",
            weights={"value": 0.8, "perf": 0.2},
        )
    )
    return reg


def create_template_scorer() -> DomainScorer:
    return DomainScorer(
        component_scorer=_score_template_component,
        set_scorer=_score_template_set,
    )
