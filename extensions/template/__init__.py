"""
DEVELOPER BLUEPRINT: Domain Extension Template.

This blueprint demonstrates how to create a complete domain extension (e.g. laptops, photo_video,
audio_hifi, bicycles, instruments) for MarketForge.

An Extension Package defines:
1. Layout (sections & compatibility rules)
2. Weights (scoring profiles & modes)
3. Search (instruction for marketplace providers)
4. Sorter (combinatorial synthesis)
5. CLI (standalone / embedded commands)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from domain_layout import DomainLayout
from domain_weights import DomainScorer, WeightRegistry
from extensions.base import BaseDomainExtension, BaseSearchInstruction, BaseSorterExtension
from extensions.template.layout import create_template_layout
from extensions.template.search import template_search_instruction, get_template_criteria, print_template_search_results, search_template
from extensions.template.sorter import template_sorter
from extensions.template.weights import create_template_scorer, create_template_weights_registry
from provider_template import BaseMarketProvider


class TemplateDomainExtension(BaseDomainExtension):
    """Blueprint extension for synthesizing multi-component sets."""

    domain_id = "template"
    title = "Template Domain Extension"
    description = "Exemplary domain extension blueprint for combinatorial set synthesis."

    def __init__(self):
        self._layout = create_template_layout()
        self._weights_reg = create_template_weights_registry()
        self._scorer = create_template_scorer()
        self._search_instruction = template_search_instruction
        self._sorter = template_sorter

    def get_layout(self) -> DomainLayout:
        return self._layout

    def get_weights_registry(self) -> WeightRegistry:
        return self._weights_reg

    def get_scorer(self) -> DomainScorer:
        return self._scorer

    def get_search_instruction(self) -> BaseSearchInstruction:
        return self._search_instruction

    def get_sorter(self) -> BaseSorterExtension:
        return self._sorter

    def load_candidates(
        self,
        budget: float,
        refresh: bool = False,
        provider: Optional[BaseMarketProvider] = None,
        **kwargs: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        return self._search_instruction.instruct_load_all(
            provider=provider,
            budget=budget,
            refresh=refresh,
            **kwargs,
        )

    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        return get_template_criteria(section_id)

    def search(
        self,
        section_id: str,
        query: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        criteria: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
        limit: int = 15,
        provider: Optional[BaseMarketProvider] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        return search_template(
            section_id=section_id,
            query=query,
            min_price=min_price,
            max_price=max_price,
            criteria=criteria,
            mode=mode,
            limit=limit,
            scorer=self._scorer,
            weights_reg=self._weights_reg,
            provider=provider,
        )

    def print_search_results(
        self,
        results: List[Dict[str, Any]],
        section_id: str,
        mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        print_template_search_results(
            results=results,
            section_id=section_id,
            layout=self._layout,
            mode=mode,
        )

    def run_cli(self, argv: List[str]) -> None:
        from extensions.template.cli import main as template_cli_main
        template_cli_main(argv)
