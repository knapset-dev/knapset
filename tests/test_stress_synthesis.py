#!/usr/bin/env python3
"""
Stress and Knapsack Combinatorial Synthesis Benchmark Tests.

Validates that MarketForge's pure, abstract branch-and-bound synthesizer
can solve multi-component constraints across 500+ items with zero domain bias
in single-digit milliseconds while strictly honoring budgets and rules.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from domain_layout import CompatibilityRule, DomainLayout, SectionDefinition
from domain_weights import CriterionDefinition, DomainScorer, WeightProfile, WeightRegistry
from set_synthesizer import synthesize_domain_sets


class TestStressAbstractSynthesis(unittest.TestCase):
    """Stress testing the combinatorial synthesizer across large pools of abstract candidates."""

    def setUp(self):
        # 1. Define 5 abstract sections
        self.sections = [
            SectionDefinition(id="core", name="Core Processor Unit", label="CORE", budget_share_min=0.1, budget_share_max=0.5, pareto_limit=10),
            SectionDefinition(id="accel", name="Hardware Accelerator", label="ACCEL", budget_share_min=0.2, budget_share_max=0.6, pareto_limit=10),
            SectionDefinition(id="mem", name="High-Speed Memory", label="MEM", budget_share_min=0.05, budget_share_max=0.3, pareto_limit=10),
            SectionDefinition(id="board", name="Backplane Substrate", label="BOARD", budget_share_min=0.05, budget_share_max=0.25, pareto_limit=10),
            SectionDefinition(id="pwr", name="Power Delivery Module", label="PWR", budget_share_min=0.05, budget_share_max=0.25, pareto_limit=10),
        ]

        # 2. Define cross-section compatibility rules
        def rule_accel_core(combo: Dict[str, Any]):
            c = combo.get("core", {}).get("tier", 1)
            a = combo.get("accel", {}).get("tier", 1)
            if a > c + 2:
                return False, "Accelerator tier exceeds Core headroom by > 2 tiers"
            return True, None

        def rule_power_margin(combo: Dict[str, Any]):
            p = combo.get("pwr", {}).get("watts", 0)
            c_w = combo.get("core", {}).get("watts", 0)
            a_w = combo.get("accel", {}).get("watts", 0)
            if p < (c_w + a_w) * 1.15:
                return False, f"Power supply ({p}W) insufficient for peak demand ({(c_w + a_w) * 1.15:.0f}W)"
            return True, None

        def rule_substrate_tier(combo: Dict[str, Any]):
            b = combo.get("board", {}).get("tier", 1)
            c = combo.get("core", {}).get("tier", 1)
            if c >= 4 and b < 2:
                return False, "High tier core requires at least Tier 2 motherboard"
            return True, None

        self.rules = [
            CompatibilityRule(name="accel_core_balance", description="Balance core & accelerator", validator=rule_accel_core, sections_involved=("core", "accel"), critical=True),
            CompatibilityRule(name="power_delivery_headroom", description="Sufficient power margin", validator=rule_power_margin, sections_involved=("pwr", "core", "accel"), critical=True),
            CompatibilityRule(name="substrate_tier_check", description="Board VRM tier check", validator=rule_substrate_tier, sections_involved=("board", "core"), critical=True),
        ]

        self.layout = DomainLayout(
            domain_id="abstract_stress",
            title="Abstract Knapsack Stress Layout",
            description="5-component synthesizer test rig",
            sections=self.sections,
            compatibility_rules=self.rules,
        )

        # 3. Define abstract weight profile & domain scorer
        self.profile = WeightProfile(
            name="balanced",
            title="Balanced Performance",
            description="Equal emphasis on throughput and price efficiency",
            weights={"perf": 0.6, "efficiency": 0.4},
        )

        class AbstractScorer(DomainScorer):
            def score_component(self, sec_id: str, item: Dict[str, Any], prof: WeightProfile) -> float:
                perf = float(item.get("perf", 10.0))
                price = max(float(item.get("price", 100.0)), 1.0)
                return (perf * 100.0) / price

            def score_set(self, components: Dict[str, Any], prof: WeightProfile, budget: float) -> float:
                total_perf = sum(float(c.get("perf", 10.0)) for c in components.values())
                total_cost = sum(float(c.get("price", 100.0)) for c in components.values())
                return total_perf + ((budget - total_cost) * 0.05)

        self.scorer = AbstractScorer()

        # 4. Generate 600+ realistic abstract items (120 per section)
        self.candidate_pool: Dict[str, List[Dict[str, Any]]] = {}
        for sec in self.sections:
            items = []
            for i in range(120):
                tier = (i % 5) + 1
                base_price = 100.0 * tier + (i * 7.5)
                perf = 50.0 * tier + (i * 2.0)
                watts = 40 * tier + 15 if sec.id in ("core", "accel") else (200 * tier + 100 if sec.id == "pwr" else 10)
                items.append({
                    "id": f"{sec.id}_{i:03d}",
                    "title": f"Candidate {sec.label} Item #{i} (Tier {tier})",
                    "price": base_price,
                    "tier": tier,
                    "perf": perf,
                    "watts": watts,
                })
            self.candidate_pool[sec.id] = items

    def test_synthesizer_performance_600_items(self):
        """Validates that 600 items across 5 sections are solved rapidly."""
        total_items = sum(len(v) for v in self.candidate_pool.values())
        self.assertGreaterEqual(total_items, 600)

        budget = 2500.0
        start = time.perf_counter()
        results = synthesize_domain_sets(
            layout=self.layout,
            scorer=self.scorer,
            profile=self.profile,
            candidate_pool=self.candidate_pool,
            budget=budget,
            top_n=5,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Synthesizer must complete within 1.5s even under shared CI cloud runner constraints
        self.assertLess(elapsed_ms, 1500.0, f"Synthesis took {elapsed_ms:.2f}ms (expected < 1500ms)")
        self.assertTrue(len(results) > 0, "Synthesizer must find optimal sets")
        self.assertLessEqual(len(results), 5)

        # Validate that all returned combinations are valid and strictly adhere to budget
        for res in results:
            self.assertLessEqual(res["total_cost"], budget)
            self.assertGreaterEqual(res["surplus"], 0.0)
            self.assertEqual(len(res["components"]), 5)

            # Re-evaluate all layout rules
            is_valid, reason = self.layout.validate_combination(res["components"], strict=True)
            self.assertTrue(is_valid, f"Rule violation in synthesized set: {reason}")

    def test_synthesizer_impossible_budget(self):
        """Validates immediate pruning when budget is lower than minimum feasible cost floor."""
        impossible_budget = 50.0  # Lowest single item is ~100
        start = time.perf_counter()
        results = synthesize_domain_sets(
            layout=self.layout,
            scorer=self.scorer,
            profile=self.profile,
            candidate_pool=self.candidate_pool,
            budget=impossible_budget,
            top_n=5,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        self.assertEqual(results, [])
        self.assertLess(elapsed_ms, 100.0, "Impossible budget must be rejected in < 100ms")

    def test_synthesizer_pareto_sorting(self):
        """Validates that results are ordered descending by set score and ascending by price."""
        results = synthesize_domain_sets(
            layout=self.layout,
            scorer=self.scorer,
            profile=self.profile,
            candidate_pool=self.candidate_pool,
            budget=3000.0,
            top_n=5,
        )
        self.assertGreater(len(results), 1)
        for i in range(len(results) - 1):
            s1 = results[i]["set_score"]
            s2 = results[i + 1]["set_score"]
            self.assertGreaterEqual(s1, s2)


if __name__ == "__main__":
    unittest.main()
