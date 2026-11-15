"""
Domain Weights Module.

Defines the scoring criteria, optimization objectives, and weighting profiles:
- Criteria Definitions
- Weight Profiles & Optimization Modes
- Domain Scoring Engines
- Weights Registry & Custom Profile Resolution
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class CriterionDefinition:
    """
    Defines a single scoring dimension (e.g. Performance, Durability, Value, Safety).
    """
    key: str
    title: str
    description: str = ""
    default_weight: float = 0.20
    is_positive: bool = True  # True: higher is better; False: lower is better (e.g. risk)


@dataclass(frozen=True)
class WeightProfile:
    """
    An optimization mode profile containing weights for defined criteria and domain constraints.
    """
    name: str
    title: str
    description: str = ""
    domain_id: str = "generic"
    weights: Dict[str, float] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    max_risk: int = 35
    require_delivery: bool = True
    require_secure_deal: bool = True
    is_custom: bool = False

    def normalized_weights(self) -> Dict[str, float]:
        """Returns weights normalized to sum to 1.0."""
        total = sum(self.weights.values())
        if total <= 0:
            return dict(self.weights)
        return {k: round(v / total, 4) for k, v in self.weights.items()}

    def with_overrides(
        self,
        weights_override: Optional[Dict[str, float]] = None,
        constraints_override: Optional[Dict[str, Any]] = None,
    ) -> WeightProfile:
        new_w = dict(self.weights)
        if weights_override:
            new_w.update(weights_override)
            total = sum(new_w.values())
            if total > 0:
                new_w = {k: round(v / total, 4) for k, v in new_w.items()}

        new_c = dict(self.constraints)
        if constraints_override:
            new_c.update(constraints_override)

        return WeightProfile(
            name=f"{self.name}-custom" if weights_override else self.name,
            title=f"{self.title} (Custom Weights)" if weights_override else self.title,
            description=self.description,
            domain_id=self.domain_id,
            weights=new_w,
            constraints=new_c,
            max_risk=self.max_risk,
            require_delivery=self.require_delivery,
            is_custom=True if weights_override else self.is_custom,
        )


class DomainScorer:
    """
    Pluggable scoring engine for calculating candidate utility and composite set scores.
    """

    def __init__(
        self,
        component_scorer: Optional[Callable[[str, Dict[str, Any], WeightProfile], float]] = None,
        set_scorer: Optional[Callable[[Dict[str, Dict[str, Any]], WeightProfile, float], float]] = None,
    ):
        self._component_scorer = component_scorer
        self._set_scorer = set_scorer

    def score_component(
        self,
        section_id: str,
        candidate: Dict[str, Any],
        profile: WeightProfile,
    ) -> float:
        """Calculates normalized score (0.0 .. 100.0) for a component in a section."""
        if self._component_scorer:
            return self._component_scorer(section_id, candidate, profile)

        # Default generic scoring: utility = weighted sum of available numeric spec keys vs price
        w = profile.normalized_weights()
        price = float(candidate.get("price", 1.0))
        spec = candidate.get("spec") or {}
        raw_val = float(spec.get("value_metric") or spec.get("perf_metric") or candidate.get("rating", 50.0))
        val_score = min(100.0, max(0.0, (raw_val / max(1.0, price)) * 1000.0))
        risk = float((candidate.get("eval") or candidate.get("heuristic") or {}).get("risk_score", 0.0))
        safety_score = max(0.0, 100.0 - risk * 1.5)
        deal_pct = float(candidate.get("discount_pct", 0.0))
        price_drop_pct = float(
            candidate.get("price_drop_pct")
            or max(0.0, -float(candidate.get("price_delta_pct") or 0.0))
        )
        deal_score = min(100.0, max(0.0, 50.0 + deal_pct * 1.5 + price_drop_pct * 1.8))

        score = (
            w.get("value", 0.35) * val_score
            + w.get("perf", 0.35) * min(100.0, raw_val)
            + w.get("safety", 0.15) * safety_score
            + w.get("deal", 0.15) * deal_score
        )
        return round(score, 2)

    def score_set(
        self,
        set_dict: Dict[str, Dict[str, Any]],
        profile: WeightProfile,
        budget: float,
    ) -> float:
        """Calculates composite score (0.0 .. 100.0) for an assembled set."""
        if self._set_scorer:
            return self._set_scorer(set_dict, profile, budget)

        # Default generic composite scoring
        comp_scores = []
        total_cost = 0.0
        for sec_id, item in set_dict.items():
            total_cost += float(item.get("price", 0.0))
            s = item.get("mode_score")
            if s is None:
                s = self.score_component(sec_id, item, profile)
            comp_scores.append(s)

        if not comp_scores:
            return 0.0

        base_score = sum(comp_scores) / len(comp_scores)

        # Budget efficiency adjustment
        if budget > 0 and total_cost <= budget:
            budget_surplus_pct = (budget - total_cost) / budget
            # Moderate bonus for saving budget without sacrificing component quality
            base_score += min(5.0, budget_surplus_pct * 10.0)

        return round(min(100.0, max(0.0, base_score)), 2)


class WeightRegistry:
    """
    Registry for domain-specific criteria and weight profiles (modes).
    """

    def __init__(self, domain_id: str, default_profile_name: str = "best"):
        self.domain_id = domain_id
        self.default_profile_name = default_profile_name
        self._criteria: Dict[str, CriterionDefinition] = {}
        self._profiles: Dict[str, WeightProfile] = {}

    def register_criterion(self, criterion: CriterionDefinition) -> None:
        self._criteria[criterion.key] = criterion

    def register_profile(self, profile: WeightProfile) -> None:
        self._profiles[profile.name] = profile

    def get_criterion(self, key: str) -> Optional[CriterionDefinition]:
        return self._criteria.get(key)

    def list_criteria(self) -> List[CriterionDefinition]:
        return list(self._criteria.values())

    def list_profiles(self) -> Dict[str, WeightProfile]:
        return dict(self._profiles)

    def get_profile_names(self) -> List[str]:
        return list(self._profiles.keys())

    def resolve_profile(
        self,
        name: Optional[str] = None,
        weights_override: Optional[Dict[str, float] | str] = None,
        constraints_override: Optional[Dict[str, Any]] = None,
    ) -> WeightProfile:
        target_name = (name or self.default_profile_name).lower().strip()
        base = self._profiles.get(target_name)
        if not base:
            # Fallback to default
            base = self._profiles.get(self.default_profile_name) or list(self._profiles.values())[0]

        parsed_weights = None
        if isinstance(weights_override, str):
            parsed_weights = self.parse_weights_string(weights_override)
        elif isinstance(weights_override, dict):
            parsed_weights = weights_override

        if parsed_weights or constraints_override:
            return base.with_overrides(parsed_weights, constraints_override)
        return base

    @staticmethod
    def parse_weights_string(s: str) -> Dict[str, float]:
        """Parses 'key1=0.4,key2=0.3,key3=0.3' format into a normalized float dict."""
        res: Dict[str, float] = {}
        if not s:
            return res
        for part in s.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip().lower()
            try:
                res[k] = max(0.0, float(v.strip()))
            except ValueError:
                continue
        total = sum(res.values())
        if total > 0:
            res = {k: round(v / total, 4) for k, v in res.items()}
        return res
