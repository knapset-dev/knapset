"""
DEVELOPER BLUEPRINT: How to Create a New Build & Set Composer Module.

This blueprint demonstrates how to implement domain synthesizers for multi-component sets
(e.g., Homelab Cluster, Custom Mechanical Keyboard, Hi-Fi Audio Studio, Sim Racing Rig,
or Solar Power Station) in the `omniforge` ecosystem.

================================================================================
STEP-BY-STEP INTEGRATION GUIDE:
================================================================================
1. Copy this file into `build_composer/modules/<domain_slug>.py`
   (e.g., `build_composer/modules/homelab.py` or `build_composer/modules/keyboard.py`).
2. Update the class name and class attributes:
   - `name`: Domain identifier (e.g. "homelab", "keyboard", "audio").
   - `title`: Human-readable display title (e.g. "Homelab Cluster Synthesizer").
   - `version`, `author`, `description`, `tags`, `url`: Module Marketplace metadata.
3. Implement `compose(candidate_pool, budget, mode='best', weights=None, top_n=5) -> List[ComposerSetResult]`:
   - Iterate or solve multi-knapsack combinations across required component categories.
   - Validate physical/electrical compatibility rules.
   - Score valid sets using optimization weights (performance, efficiency, value).
4. Return up to `top_n` instances of `ComposerSetResult`.
5. Zero configuration required! The registry automatically hooks your composer into
   `main.py build --domain <domain_slug>`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from build_composer.base import BaseBuildComposer, ComposerSetResult


class CustomDomainComposer(BaseBuildComposer):
    """
    Independent multi-component set composer and combinatorial optimizer.
    """

    # --- Marketplace Manifest Metadata (Marketplace Ready) ---
    name: str = "custom_template"
    title: str = "Custom Domain Set Composer Blueprint"
    domain: str = "general"
    version: str = "1.0.0"
    author: str = "Community Contributor"
    min_app_version: str = "1.0.0"
    category: str = "synthesis"
    tags: List[str] = ["composer", "optimizer", "synthesis", "template"]
    url: str = "https://github.com/omniforge-dev/omniforge"
    description: str = "Exemplary blueprint for building isolated combinatorial set synthesizers."

    def compose(
        self,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        mode: str = "best",
        weights: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[ComposerSetResult]:
        """
        Synthesizes optimal combinations of components within the given budget.

        Arguments:
            candidate_pool: Dictionary mapping component slot IDs (e.g. 'node', 'switch', 'storage')
                            to lists of candidate listing dictionaries.
            budget: Total ceiling cost constraint in local currency.
            mode: Target optimization mode profile ('best', 'value', 'safe', etc.).
            weights: Optional custom weight overrides.
            top_n: Maximum number of winning sets to return.

        Returns:
            List of ComposerSetResult instances ranked by highest score descending.
        """
        results: List[ComposerSetResult] = []

        # Example demonstration layout: slot 'part_a' and 'part_b'
        pool_a = candidate_pool.get("part_a", [])
        pool_b = candidate_pool.get("part_b", [])

        if not pool_a or not pool_b:
            return results

        # Simple combinatorial search with compatibility and budget pruning
        for item_a in pool_a:
            price_a = float(item_a.get("price", 0.0))
            if price_a >= budget:
                continue

            for item_b in pool_b:
                price_b = float(item_b.get("price", 0.0))
                total_cost = price_a + price_b
                if total_cost > budget:
                    continue

                # Compatibility Check (Example: socket / interface matching)
                type_a = (item_a.get("spec") or {}).get("interface", "")
                type_b = (item_b.get("spec") or {}).get("interface", "")
                if type_a and type_b and type_a != type_b:
                    continue

                surplus = budget - total_cost
                # Simple value-weighted scoring formula
                score = (budget / max(1.0, total_cost)) * 50.0

                components = {
                    "part_a": item_a,
                    "part_b": item_b,
                }
                metrics = {
                    "total_parts": 2,
                    "budget_utilization_pct": round((total_cost / budget) * 100.0, 1),
                }

                results.append(
                    ComposerSetResult(
                        domain=self.name,
                        mode=mode,
                        total_cost=total_cost,
                        budget=budget,
                        surplus=surplus,
                        score=round(score, 1),
                        components=components,
                        metrics=metrics,
                    )
                )

        # Sort highest score first and take top_n
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]
