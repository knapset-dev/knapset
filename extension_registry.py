"""
Extension Registry Module.

Manages dynamic discovery and registration of domain extensions:
- Auto-discovery of custom user extensions in extensions/ directory
- Zero-code declarative JSON blueprints in configs/blueprints/*.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from analyzer_core import make_terminal_link, pad_ansi
from currency import (
    canonical_currency,
    convert_currency,
    format_price,
    get_active_currency,
    normalize_candidate_pool,
    normalize_listing_price,
)
from domain_layout import DomainLayout
from domain_weights import CriterionDefinition, DomainScorer, WeightProfile, WeightRegistry
from extensions.base import BaseDomainExtension, BaseSearchInstruction, BaseSorterExtension
from provider_template import BaseMarketProvider, MockMarketProvider, ProviderFeatureAdapter
from set_synthesizer import GenericDomainSorter, export_domain_sets, print_domain_sets

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, BaseDomainExtension] = {}
_MANUAL_REGISTRY: Dict[str, BaseDomainExtension] = {}
BLUEPRINTS_DIR = Path(__file__).resolve().parent / "configs" / "blueprints"


class _BlueprintSearchInstruction(BaseSearchInstruction):
    """Generic search instruction backed by a declarative JSON blueprint."""

    def __init__(self, domain_id: str, criteria_docs: Dict[str, Dict[str, str]], sample_market: Dict[str, List[Dict[str, Any]]]):
        self.domain_id = domain_id
        self._criteria_docs = criteria_docs
        self._sample_market = sample_market

    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        if section_id:
            return dict(self._criteria_docs.get(section_id.lower().strip(), {}))
        merged: Dict[str, str] = {}
        for sec, crits in self._criteria_docs.items():
            for k, v in crits.items():
                merged[f"{sec}.{k}"] = v
        return merged

    def instruct_search(
        self,
        provider: BaseMarketProvider,
        section_id: str,
        query: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        criteria: Optional[Dict[str, Any]] = None,
        limit: int = 15,
        scorer: Optional[DomainScorer] = None,
        weights_reg: Optional[WeightRegistry] = None,
        mode: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        sec_key = (section_id or "").lower().strip()
        active_curr = get_active_currency()
        items = provider.search_items(
            query=query or "",
            category_hint=sec_key,
            min_price=min_price,
            max_price=max_price,
            limit=limit * 3,
        )
        if not items and self._sample_market:
            sample_catalog = normalize_candidate_pool(self._sample_market, target_currency=active_curr)
            fallback_prov = MockMarketProvider(f"{self.domain_id}_blueprint", sample_catalog)
            items = fallback_prov.search_items(
                query=query or "",
                category_hint=sec_key,
                min_price=min_price,
                max_price=max_price,
                limit=limit * 3,
            )

        prov_src = getattr(provider, "source_name", getattr(provider, "name", self.domain_id))
        crit = criteria or {}
        matched: List[Dict[str, Any]] = []
        for it in items:
            item = ProviderFeatureAdapter.adapt_item(it, prov_src)
            normalize_listing_price(item, target_currency=active_curr)
            price = float(item.get("price", 0.0))
            if min_price is not None and price < min_price:
                continue
            if max_price is not None and price > max_price:
                continue

            spec = item.get("spec") or item.get("specs") or {}
            title = str(item.get("title") or item.get("model") or "")
            if query and query.lower().strip() not in f"{title} {json.dumps(spec)}".lower():
                continue

            ok = True
            for ck, cv in crit.items():
                actual = item.get(ck, spec.get(ck))
                if actual is None or str(cv).lower() not in str(actual).lower():
                    ok = False
                    break
            if not ok:
                continue

            candidate = dict(item)
            if isinstance(spec, dict):
                candidate.update(spec)
            candidate["model"] = str(item.get("model") or title)
            candidate["title"] = title or candidate["model"]
            candidate["spec"] = spec if isinstance(spec, dict) else {}

            if scorer and weights_reg:
                prof = weights_reg.resolve_profile(mode)
                score = scorer.score_component(sec_key, candidate, prof)
            else:
                score = float(candidate.get("rating") or spec.get("rating", 75.0))
            candidate["mode_score"] = score
            candidate["search_score"] = score
            matched.append(candidate)

        matched.sort(key=lambda x: (-x.get("mode_score", 0.0), float(x.get("price", 0.0))))
        return matched[:limit]

    def instruct_load_all(
        self,
        provider: Optional[BaseMarketProvider] = None,
        budget: Optional[float] = None,
        refresh: bool = False,
        sections: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        active_curr = get_active_currency()
        sec_ids = sections or list(self._sample_market.keys())
        if provider and hasattr(provider, "search_items"):
            pools: Dict[str, List[Dict[str, Any]]] = {}
            for sid in sec_ids:
                found = self.instruct_search(provider=provider, section_id=sid, limit=60)
                if found:
                    pools[sid] = normalize_candidate_pool({sid: found}, target_currency=active_curr)[sid]
            if len(pools) == len(sec_ids) and pools:
                return pools
        return normalize_candidate_pool(self._sample_market, target_currency=active_curr)


class JsonBlueprintDomainExtension(BaseDomainExtension):
    """
    Zero-code domain extension constructed directly from a declarative JSON blueprint.
    Supports DomainLayout sections, AST-validated compatibility rules, weight modes,
    sample market catalogs, CLI commands, Web UI, and MCP tools.
    """

    def __init__(self, data: Dict[str, Any], source_path: Optional[Path] = None):
        self._raw = dict(data)
        self.source_path = source_path
        self._layout = DomainLayout.from_dict(data)
        self.domain_id = self._layout.domain_id
        self.title = self._layout.title
        self.description = self._layout.description or f"Declarative JSON blueprint domain '{self.domain_id}'."

        default_mode = str(data.get("default_mode", "balanced"))
        self._weights_reg = WeightRegistry(domain_id=self.domain_id, default_profile_name=default_mode)
        for c_data in data.get("criteria", []):
            self._weights_reg.register_criterion(
                CriterionDefinition(
                    key=str(c_data["key"]),
                    title=str(c_data.get("title", c_data["key"])),
                    description=str(c_data.get("description", "")),
                    default_weight=float(c_data.get("default_weight", 0.25)),
                    is_positive=bool(c_data.get("is_positive", True)),
                )
            )

        modes_data = data.get("modes") or {}
        if isinstance(modes_data, dict):
            for m_name, m_cfg in modes_data.items():
                self._weights_reg.register_profile(
                    WeightProfile(
                        name=str(m_name),
                        title=str(m_cfg.get("title", m_name.title())),
                        description=str(m_cfg.get("description", "")),
                        domain_id=self.domain_id,
                        weights={str(k): float(v) for k, v in (m_cfg.get("weights") or {}).items()},
                    )
                )
        if not self._weights_reg.list_profiles():
            self._weights_reg.register_profile(
                WeightProfile(
                    name="balanced",
                    title="Balanced Set",
                    description="Balanced optimization across value, performance, deal, and safety.",
                    domain_id=self.domain_id,
                    weights={"value": 0.35, "perf": 0.35, "deal": 0.15, "safety": 0.15},
                )
            )

        self._scorer = DomainScorer()
        self._sorter = GenericDomainSorter(domain_id=self.domain_id)
        self._sample_market: Dict[str, List[Dict[str, Any]]] = data.get("sample_market") or {}
        self._search_instruction = _BlueprintSearchInstruction(
            domain_id=self.domain_id,
            criteria_docs=data.get("search_criteria") or {},
            sample_market=self._sample_market,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> JsonBlueprintDomainExtension:
        p = Path(path)
        payload = json.loads(p.read_text(encoding="utf-8"))
        return cls(payload, source_path=p)

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
            sections=self._layout.section_ids(),
            **kwargs,
        )

    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        return self._search_instruction.get_supported_criteria(section_id)

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
        active_curr = get_active_currency()
        sample_catalog = normalize_candidate_pool(self._sample_market, target_currency=active_curr)
        prov = provider or MockMarketProvider(f"{self.domain_id}_blueprint", sample_catalog)
        return self._search_instruction.instruct_search(
            provider=prov,
            section_id=section_id,
            query=query,
            min_price=min_price,
            max_price=max_price,
            criteria=criteria,
            limit=limit,
            scorer=self._scorer,
            weights_reg=self._weights_reg,
            mode=mode,
        )

    def print_search_results(
        self,
        results: List[Dict[str, Any]],
        section_id: str,
        mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        sec_key = (section_id or "").lower().strip()
        sec = self._layout.get_section(sec_key)
        sec_label = sec.label if sec else sec_key.upper()
        print(f"\nFound {len(results)} {sec_label} candidates in '{self.domain_id}':\n")
        if not results:
            print(f"  No {sec_label} items matched your criteria.\n")
            return
        for item in results:
            curr = item.get("currency") or get_active_currency()
            p_str = format_price(float(item.get("price", 0.0)), curr)
            title = item.get("title") or item.get("model") or "Item"
            score = float(item.get("mode_score", 0.0))
            print(f"  • {title:<34} │ {p_str:<12} │ Score: {score:>5.1f}")
        print()

    def run_cli(self, argv: List[str]) -> None:
        args = list(argv)
        sec_ids = self._layout.section_ids()
        if not args or args[0] in ("-h", "--help", "help"):
            print(f"\n{self.title} ({self.domain_id}) — Declarative Blueprint CLI\n")
            print(f"Usage: python main.py {self.domain_id} <command> [options]\n")
            print("Commands:")
            print("  build    Synthesize optimal sets under budget")
            for s in self._layout.sections:
                print(f"  {s.id:<8} Search {s.name}")
            print("  modes    List optimization modes and weights\n")
            return

        cmd = args[0].lower()
        extra = args[1:]
        active_curr = get_active_currency()
        def_b = (
            self._layout.default_budget
            if canonical_currency(active_curr) == canonical_currency(self._layout.currency)
            else convert_currency(self._layout.default_budget, self._layout.currency, active_curr)
        )

        if cmd == "build":
            parser = argparse.ArgumentParser(prog=f"main.py {self.domain_id} build")
            parser.add_argument("--budget", "-b", type=float, default=def_b)
            parser.add_argument("--mode", "-M", type=str, default=self._weights_reg.default_profile_name)
            parser.add_argument("--top", "-n", type=int, default=5)
            parser.add_argument("--weights", "-w", type=str, default=None)
            parser.add_argument("--min-new-ratio", type=float, default=0.0, help="Minimum fraction of new items (0.0..1.0)")
            parser.add_argument("--export", type=str, default=None, help="Export synthesized sets to .md or .json file")
            p_args = parser.parse_args(extra)

            t0 = time.perf_counter()
            prof = self._weights_reg.resolve_profile(p_args.mode, weights_override=p_args.weights)
            cands = self.load_candidates(budget=p_args.budget)
            sets = self._sorter.sort_and_synthesize(
                layout=self._layout,
                scorer=self._scorer,
                profile=prof,
                candidate_pool=cands,
                budget=p_args.budget,
                top_n=p_args.top,
                min_new_ratio=p_args.min_new_ratio,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print_domain_sets(self._layout, prof, sets, elapsed_ms=elapsed_ms)
            if p_args.export:
                out_file = export_domain_sets(p_args.export, self._layout, prof, sets)
                print(f"   Exported {len(sets)} set(s) to {out_file}\n")
        elif cmd in sec_ids:
            parser = argparse.ArgumentParser(prog=f"main.py {self.domain_id} {cmd}")
            parser.add_argument("--query", "-q", type=str, default=None)
            parser.add_argument("--min-price", type=float, default=None)
            parser.add_argument("--max-price", type=float, default=None)
            parser.add_argument("--mode", "-M", type=str, default=None)
            parser.add_argument("--limit", "-n", type=int, default=15)
            p_args = parser.parse_args(extra)
            res = self.search(
                section_id=cmd,
                query=p_args.query,
                min_price=p_args.min_price,
                max_price=p_args.max_price,
                mode=p_args.mode,
                limit=p_args.limit,
            )
            self.print_search_results(res, section_id=cmd, mode=p_args.mode)
        elif cmd in ("search", "find"):
            self.run_cli_search(extra)
        elif cmd in ("modes", "mods"):
            print(f"\nOptimization Modes for {self.title}")
            print("─" * 74)
            for prof in self._weights_reg.list_profiles().values():
                top_w = ", ".join(f"{k}:{v:.2f}" for k, v in prof.weights.items())
                print(f"  {prof.name:<16} │ {prof.title:<24} │ {top_w}")
            print("─" * 74 + "\n")
        else:
            print(f"Error: Unknown command '{cmd}' for domain '{self.domain_id}'", file=sys.stderr)


def rescan_extensions() -> None:
    """Forces re-scanning of the extensions/ and configs/blueprints/ directories."""
    _REGISTRY.clear()
    _initialize_registry()


def _initialize_registry() -> None:
    if _REGISTRY:
        return

    # 1. Auto-discover Python domain extension sub-packages in extensions/
    ext_dir = Path(__file__).parent / "extensions"
    if ext_dir.is_dir():
        for sub in sorted(ext_dir.iterdir()):
            if sub.is_dir() and not sub.name.startswith(("_", ".")):
                init_file = sub / "__init__.py"
                if init_file.is_file():
                    try:
                        mod = importlib.import_module(f"extensions.{sub.name}")
                        for attr in dir(mod):
                            obj = getattr(mod, attr)
                            if (
                                isinstance(obj, type)
                                and issubclass(obj, BaseDomainExtension)
                                and obj is not BaseDomainExtension
                                and obj is not JsonBlueprintDomainExtension
                            ):
                                inst = obj()
                                _REGISTRY[inst.domain_id] = inst
                    except Exception as e:
                        logger.debug("Could not auto-load extension %s: %s", sub.name, e)

    # 2. Auto-discover declarative JSON blueprints in configs/blueprints/*.json
    if BLUEPRINTS_DIR.is_dir():
        for bp_file in sorted(BLUEPRINTS_DIR.glob("*.json")):
            if bp_file.name.startswith(("_", ".")):
                continue
            try:
                ext = JsonBlueprintDomainExtension.from_file(bp_file)
                if ext.domain_id not in _REGISTRY:
                    _REGISTRY[ext.domain_id] = ext
            except Exception as e:
                logger.warning("Could not load JSON blueprint %s: %s", bp_file.name, e)

    for k, v in _MANUAL_REGISTRY.items():
        _REGISTRY[k] = v


def register_extension(ext: BaseDomainExtension) -> None:
    """Manually registers an extension instance."""
    _initialize_registry()
    _MANUAL_REGISTRY[ext.domain_id] = ext
    _REGISTRY[ext.domain_id] = ext


def unregister_extension(domain_id: str) -> None:
    """Manually unregisters an extension instance."""
    _initialize_registry()
    norm = domain_id.lower().strip()
    _MANUAL_REGISTRY.pop(norm, None)
    _REGISTRY.pop(norm, None)


def get_extension(domain_id: Optional[str] = None, fallback: bool = True) -> BaseDomainExtension:
    """Retrieves an extension by domain ID, or default domain if domain_id is not specified or fallback=True."""
    _initialize_registry()
    if domain_id:
        norm_id = domain_id.lower().strip()
        if norm_id in _REGISTRY:
            return _REGISTRY[norm_id]
        if not fallback:
            raise KeyError(f"Domain extension '{domain_id}' not found.")

    # If no explicit domain requested, resolve the active installed domain
    installed = [k for k in _REGISTRY.keys() if k != "template"]
    if installed:
        return _REGISTRY[installed[0]]
    if "template" in _REGISTRY:
        return _REGISTRY["template"]
    if _REGISTRY:
        return next(iter(_REGISTRY.values()))
    raise KeyError("No domain extensions registered. Install one via 'main.py module install <name>'.")


def list_extensions() -> Dict[str, BaseDomainExtension]:
    """Returns a dictionary of all registered domain extensions."""
    _initialize_registry()
    return dict(_REGISTRY)


def list_domain_ids() -> List[str]:
    """Returns a list of all registered domain IDs."""
    _initialize_registry()
    return sorted(list(_REGISTRY.keys()))


DEFAULT_DOMAIN = None


def find_extension_by_section(section_id: str) -> Optional[BaseDomainExtension]:
    """
    Finds the registered domain extension whose layout owns the given section ID.
    Enables dynamic, un-hardcoded CLI routing based on domain layout definitions.
    """
    _initialize_registry()
    sec_norm = (section_id or "").lower().strip()
    for ext in _REGISTRY.values():
        layout = ext.get_layout()
        if sec_norm in layout.section_ids():
            return ext
    return None


def find_extension_by_alias(alias: str) -> Optional[BaseDomainExtension]:
    """Finds the registered domain extension that declares the given alias in `domain_aliases`."""
    _initialize_registry()
    norm = (alias or "").lower().strip()
    for ext in _REGISTRY.values():
        if norm in tuple(a.lower() for a in getattr(ext, "domain_aliases", ())):
            return ext
    return None
