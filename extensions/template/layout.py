"""
Template Domain Layout.

Defines the components/sections that make up a set in this domain,
along with cross-section physical and budget compatibility rules.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from domain_layout import CompatibilityRule, DomainLayout, SectionDefinition
from currency import canonical_currency, convert_currency, format_price, get_active_currency


def _fmt_generic_row(section_name: str, cand: Dict[str, Any], disc_fmt: Callable[[float], str]) -> Tuple[str, str, str, str, str]:
    name = cand.get("model") or cand.get("title", section_name)
    price = float(cand.get("price", 0.0))
    disc = float(cand.get("discount_pct", 0.0))
    deal_s = disc_fmt(disc)
    curr = cand.get("currency") or get_active_currency()
    price_str = f"{format_price(price, curr)} ({deal_s})" if price > 0 else format_price(0, curr)
    spec = cand.get("spec") or {}
    spec1 = str(spec.get("tier") or spec.get("type") or "Standard")
    spec2 = str(spec.get("rating") or "Grade A")
    return (section_name.upper()[:6], name, price_str, spec1, spec2)


def create_template_layout() -> DomainLayout:
    """Creates a sample 3-section layout for this domain."""
    return DomainLayout(
        domain_id="template",
        title="Template Set Layout",
        description="Sample layout demonstrating section topology and compatibility rules.",
        default_budget=250.0,
        currency="USDT",
        sections=[
            SectionDefinition(
                id="core",
                name="Core Unit",
                label="CORE",
                description="The primary unit/body of the set.",
                budget_share_min=0.30,
                budget_share_max=0.60,
                pareto_limit=15,
                row_formatter=lambda c, f: _fmt_generic_row("Core", c, f),
            ),
            SectionDefinition(
                id="addon",
                name="Add-on / Accessory",
                label="ADDON",
                description="Secondary module attached to the core.",
                budget_share_min=0.15,
                budget_share_max=0.40,
                pareto_limit=15,
                row_formatter=lambda c, f: _fmt_generic_row("Addon", c, f),
            ),
            SectionDefinition(
                id="power",
                name="Power / Supply Unit",
                label="POWER",
                description="Power source supplying energy to the set.",
                budget_share_min=0.10,
                budget_share_max=0.30,
                pareto_limit=15,
                row_formatter=lambda c, f: _fmt_generic_row("Power", c, f),
            ),
        ],
        compatibility_rules=[
            CompatibilityRule(
                name="core_power_match",
                description="Power supply must deliver sufficient wattage for the core unit.",
                validator=lambda set_dict: (
                    True, None
                ) if not (set_dict.get("core") and set_dict.get("power")) else (
                    (True, None) if float(set_dict["power"].get("spec", {}).get("watts", 500)) >= float(set_dict["core"].get("spec", {}).get("watts", 200))
                    else (False, "Power supply capacity is lower than core unit requirement.")
                ),
                sections_involved=("core", "power"),
            ),
        ],
    )
