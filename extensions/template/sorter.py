"""
Template Domain Sorter Extension.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from domain_layout import DomainLayout
from domain_weights import DomainScorer, WeightProfile
from extensions.base import BaseSorterExtension
from set_synthesizer import GenericDomainSorter


class TemplateSorter(BaseSorterExtension):
    """Combinatorial set synthesizer for template domain."""

    domain_id = "template"

    def __init__(self):
        self._generic = GenericDomainSorter(domain_id="template")

    def sort_and_synthesize(
        self,
        *args: Any,
        candidate_pool: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        budget: Optional[float] = None,
        layout: Optional[DomainLayout] = None,
        scorer: Optional[DomainScorer] = None,
        profile: Optional[WeightProfile] = None,
        mode: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        return self._generic.sort_and_synthesize(
            *args,
            candidate_pool=candidate_pool,
            budget=budget,
            layout=layout,
            scorer=scorer,
            profile=profile,
            mode=mode,
            top_n=top_n,
            **kwargs,
        )


template_sorter = TemplateSorter()
