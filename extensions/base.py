"""
Base Extension Module.

Defines the decoupled architecture contracts:
1. Universal Provider Template: `BaseMarketProvider` (in `provider_template.py`)
2. Domain Search Instruction: `BaseSearchInstruction` (acts as instruction manual to query providers and normalize candidates)
3. Domain Sorter / Synthesizer: `BaseSorterExtension` (knows nothing about providers, operates strictly by layout template & weights)
4. Domain Extension Bundle: `BaseDomainExtension` (bundles layout, weights, search instruction, and sorter)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from domain_layout import DomainLayout
from domain_weights import DomainScorer, WeightProfile, WeightRegistry
from provider_template import BaseMarketProvider, get_market_provider


class BaseSearchInstruction(ABC):
    """
    Acts as an instruction manual for the domain:
    Defines how to take any provider implementing BaseMarketProvider,
    execute domain-specific searches across layout sections,
    and convert the raw listings into normalized domain Candidates.
    """

    domain_id: str

    @abstractmethod
    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        """Returns dictionary of supported search criteria and descriptions for a section."""
        pass

    @abstractmethod
    def instruct_search(
        self,
        provider: BaseMarketProvider,
        section_id: str,
        query: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        criteria: Optional[Dict[str, Any]] = None,
        limit: int = 40,
        scorer: Optional[DomainScorer] = None,
        weights_reg: Optional[WeightRegistry] = None,
        mode: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """
        Executes search using the given provider according to this domain's search instructions.
        Applies keywords, category mappings, and extracts domain specifications into Candidate dicts.
        """
        pass

    @abstractmethod
    def instruct_load_all(
        self,
        provider: Optional[BaseMarketProvider] = None,
        budget: Optional[float] = None,
        refresh: bool = False,
        **kwargs: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Applies instructions across all layout sections to gather the complete Candidate pool for the sorter.
        """
        pass


class BaseSorterExtension(ABC):
    """
    Domain Sorter / Synthesizer.
    Knows NOTHING about marketplace websites or providers.
    Operates strictly on the Layout template, candidate pools, and weights.
    """

    domain_id: str

    @abstractmethod
    def sort_and_synthesize(
        self,
        layout: DomainLayout,
        scorer: DomainScorer,
        profile: WeightProfile,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """
        Executes combinatorial optimization or ranking over candidate_pool,
        strictly respecting layout constraints and budget.
        """
        pass


class BaseDomainExtension(ABC):
    """Abstract base class uniting layout, weights, search instructions, and sorter."""

    domain_id: str
    title: str
    description: str
    # Alternative identifiers (legacy ids, slugs) that resolve to this domain.
    domain_aliases: tuple = ()

    @abstractmethod
    def get_layout(self) -> DomainLayout:
        """Returns the domain layout defining sections and compatibility rules."""
        pass

    @abstractmethod
    def get_weights_registry(self) -> WeightRegistry:
        """Returns the weights registry defining criteria and weight profiles."""
        pass

    @abstractmethod
    def get_scorer(self) -> DomainScorer:
        """Returns the scoring engine for ranking candidates and evaluating sets."""
        pass

    @abstractmethod
    def get_search_instruction(self) -> BaseSearchInstruction:
        """Returns the search instruction defining how to fetch & parse items via any provider."""
        pass

    @abstractmethod
    def get_sorter(self) -> BaseSorterExtension:
        """Returns the sorter extension that synthesizes sets without knowing providers."""
        pass

    def load_candidates(
        self,
        budget: float,
        refresh: bool = False,
        provider: Optional[BaseMarketProvider] = None,
        **kwargs: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Loads candidate pools via the domain search instruction."""
        return self.get_search_instruction().instruct_load_all(
            provider=provider,
            budget=budget,
            refresh=refresh,
            **kwargs,
        )

    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        return self.get_search_instruction().get_supported_criteria(section_id)

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
        """Searches candidates in section_id using domain search instructions."""
        if isinstance(provider, BaseMarketProvider):
            prov = provider
        elif isinstance(provider, str):
            prov = get_market_provider(provider)
        else:
            prov = get_market_provider()
        return self.get_search_instruction().instruct_search(
            provider=prov,
            section_id=section_id,
            query=query,
            min_price=min_price,
            max_price=max_price,
            criteria=criteria,
            limit=limit,
            scorer=self.get_scorer(),
            weights_reg=self.get_weights_registry(),
            mode=mode,
            **kwargs,
        )

    @abstractmethod
    def print_search_results(
        self,
        results: List[Dict[str, Any]],
        section_id: str,
        mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Formats and prints search results in a clean aligned table."""
        pass

    def run_cli_search(self, argv: List[str]) -> None:
        """Standard CLI runner for domain search commands (<domain> search ...)."""
        import argparse
        parser = argparse.ArgumentParser(
            prog=f"main.py {self.domain_id} search",
            description=f"Search & filter {self.title} candidates by section criteria",
        )
        parser.add_argument("--section", "-s", type=str, default=None, help="Target component section")
        parser.add_argument("--query", "-q", type=str, default=None, help="Text search query")
        parser.add_argument("--mode", "-M", type=str, default=None, help="Rank and score items using optimization mode")
        parser.add_argument("--criteria", "-c", type=str, default=None, help="Domain criteria (e.g. 'key1=val1,key2=val2')")
        parser.add_argument("--min-price", type=float, default=None, help="Minimum price in active currency")
        parser.add_argument("--max-price", type=float, default=None, help="Maximum price in active currency")
        parser.add_argument("--provider", "-p", type=str, default="all", help="Marketplace information provider")
        parser.add_argument("--limit", "-n", type=int, default=15, help="Maximum results to display (default: 15)")
        parser.add_argument("--list-criteria", action="store_true", help="List all supported criteria for this section")
        args = parser.parse_args(argv)

        layout = self.get_layout()
        sec_id = args.section
        if not sec_id:
            sec_id = layout.sections[0].id if layout.sections else "default"

        if args.list_criteria:
            crits = self.get_supported_criteria(sec_id)
            sec_obj = layout.get_section(sec_id)
            sec_name = sec_obj.name if sec_obj else sec_id.upper()
            print(f"\nSupported Search Criteria for {self.title} — {sec_name}:")
            print("─" * 82)
            if not crits:
                print("  No specific criteria declared for this section.")
            for k, desc in sorted(crits.items()):
                print(f"  • {k:<18} : {desc}")
            print("─" * 82)
            print("Pass criteria as: --criteria \"key1=value,key2=value\"\n")
            return

        crit_dict: Dict[str, Any] = {}
        if args.criteria:
            for part in args.criteria.split(","):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                k, v = part.split("=", 1)
                crit_dict[k.strip()] = v.strip()

        from provider_template import get_market_provider
        prov = get_market_provider(args.provider)

        results = self.search(
            section_id=sec_id,
            query=args.query,
            min_price=args.min_price,
            max_price=args.max_price,
            criteria=crit_dict,
            mode=args.mode,
            limit=args.limit,
            provider=prov,
        )
        self.print_search_results(
            results=results,
            section_id=sec_id,
            mode=args.mode,
        )

    def get_mcp_tools(self) -> List[Dict[str, Any]]:
        """Optional domain-specific MCP tool schemas contributed by this extension."""
        return []

    def execute_mcp_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Executes a domain-contributed MCP tool, or returns None if not handled."""
        return None

    def evaluate_prebuilt(
        self,
        listing_a: str,
        listing_b: Optional[str] = None,
        mode: str = "sweet-spot",
    ) -> Dict[str, Any]:
        """Optional whole-bundle/prebuilt listing evaluation for this domain."""
        raise NotImplementedError(f"Domain '{self.domain_id}' does not implement prebuilt bundle evaluation.")

