"""
Template Domain Search Instruction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from domain_layout import DomainLayout
from domain_weights import DomainScorer, WeightRegistry
from extensions.base import BaseSearchInstruction
from provider_template import BaseMarketProvider, ProviderFeatureAdapter, get_market_provider


SAMPLE_TEMPLATE_ITEMS: Dict[str, List[Dict[str, Any]]] = {
    "core": [
        {"id": "c1", "title": "Core Unit Alpha (Standard Edition)", "price": 100.0, "currency": "USD", "discount_pct": 15.0, "spec": {"watts": 250, "tier": "A", "type": "standard"}},
        {"id": "c2", "title": "Core Unit Pro (High-Performance)", "price": 150.0, "currency": "USD", "discount_pct": 10.0, "spec": {"watts": 350, "tier": "A", "type": "pro"}},
        {"id": "c3", "title": "Core Unit Lite (Budget Variant)", "price": 60.0, "currency": "USD", "discount_pct": 25.0, "spec": {"watts": 150, "tier": "B", "type": "lite"}},
    ],
    "addon": [
        {"id": "a1", "title": "Addon Module Beta (Standard)", "price": 50.0, "currency": "USD", "discount_pct": 10.0, "spec": {"tier": "B"}},
        {"id": "a2", "title": "Addon Module Ultra (Expansion)", "price": 80.0, "currency": "USD", "discount_pct": 5.0, "spec": {"tier": "A"}},
    ],
    "power": [
        {"id": "p1", "title": "Power Supply Gamma (500W)", "price": 40.0, "currency": "USD", "discount_pct": 20.0, "spec": {"watts": 500, "tier": "B"}},
        {"id": "p2", "title": "Power Supply Titan (750W Gold)", "price": 70.0, "currency": "USD", "discount_pct": 15.0, "spec": {"watts": 750, "tier": "A"}},
    ],
}


def get_template_criteria(section_id: Optional[str] = None) -> Dict[str, str]:
    return {
        "tier": "Quality tier (A, B, C)",
        "type": "Component subtype",
    }


def search_template(
    section_id: str,
    query: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    criteria: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
    limit: int = 15,
    scorer: Optional[DomainScorer] = None,
    weights_reg: Optional[WeightRegistry] = None,
    provider: Optional[BaseMarketProvider] = None,
) -> List[Dict[str, Any]]:
    sec = (section_id or "core").lower().strip()
    pool = SAMPLE_TEMPLATE_ITEMS.get(sec, [])
    if provider is not None and getattr(provider, "source_name", "") not in ("all", "sample", "template"):
        try:
            raw_items = provider.search_items(category_hint=sec, query=query or "", min_price=min_price, max_price=max_price, limit=limit)
            if raw_items:
                pool = [item.to_dict() for item in raw_items]
        except Exception:
            pass

    out: List[Dict[str, Any]] = []
    q = (query or "").lower().strip()
    for item in pool:
        p = float(item.get("price", 0.0))
        if min_price is not None and p < min_price:
            continue
        if max_price is not None and p > max_price:
            continue
        if q and q not in str(item.get("title", "")).lower():
            continue
        out.append(dict(item))
    return out[:limit]


def print_template_search_results(
    results: List[Dict[str, Any]],
    section_id: str,
    layout: DomainLayout,
    mode: Optional[str] = None,
) -> None:
    print(f"\nSearch Results for [{section_id.upper()}] ({len(results)} items):")
    for r in results[:10]:
        print(f"  • {r.get('title', 'Item')} — {float(r.get('price', 0)):,.0f} ₴")


class TemplateSearchInstruction(BaseSearchInstruction):
    domain_id = "template"

    def get_supported_criteria(self, section_id: Optional[str] = None) -> Dict[str, str]:
        return get_template_criteria(section_id)

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
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        return search_template(
            section_id=section_id,
            query=query,
            min_price=min_price,
            max_price=max_price,
            criteria=criteria,
            limit=limit,
            scorer=scorer,
            weights_reg=weights_reg,
            provider=provider,
        )

    def instruct_load_all(
        self,
        provider: Optional[BaseMarketProvider] = None,
        budget: float = 10000.0,
        refresh: bool = False,
        **kwargs: Any,
    ) -> Dict[str, List[Dict[str, Any]]]:
        # Return mock candidate pools for the 3 sections
        return {
            "core": [{"id": "c1", "title": "Core Unit Alpha", "price": budget * 0.5, "discount_pct": 15.0, "spec": {"watts": 250}}],
            "addon": [{"id": "a1", "title": "Addon Module Beta", "price": budget * 0.3, "discount_pct": 10.0, "spec": {}}],
            "power": [{"id": "p1", "title": "Power Supply Gamma", "price": budget * 0.2, "discount_pct": 20.0, "spec": {"watts": 500}}],
        }


template_search_instruction = TemplateSearchInstruction()
