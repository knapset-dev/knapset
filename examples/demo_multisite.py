#!/usr/bin/env python3
"""
Interactive demonstration of Multi-Site Feature Normalization & Sorter Isolation.

Demonstrates:
1. Provider Layer: Raw listings with platform-specific keys (OLX, Blocket, eBay, Telemart).
2. Search Extension Layer (ProviderFeatureAdapter):
   - Normalizes OLX Доставка, Blocket Köpskydd, eBay Protection, Store Warranty -> is_secure_deal
   - Multilingual refusal detection (UA, SE, EN) -> unmasks fraud/refusals
3. Sorter Extension Layer:
   - Evaluates items with ZERO knowledge of site names
   - Rejects unsecure deals
   - Synthesizes an optimal cross-marketplace PC build under budget
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from marketplace.client import locate_registry_dir
REGISTRY_MODULES = locate_registry_dir() / "modules"
if REGISTRY_MODULES.exists() and str(REGISTRY_MODULES) not in sys.path:
    sys.path.insert(0, str(REGISTRY_MODULES))

from provider_template import ProviderFeatureAdapter, MockMarketProvider
from extensions.pc.search import PCSearchInstruction
from extensions.pc.sorter import PCSorter


def main():
    print("=" * 80)
    print(" ARCHITECTURE DEMO: MARKETPLACES -> SEARCH ADAPTER -> COMBINATORIAL SORTER")
    print("=" * 80)

    # -------------------------------------------------------------------------
    # 1. RAW LISTINGS FROM MULTIPLE PLATFORMS (Provider Layer)
    # -------------------------------------------------------------------------
    raw_samples = [
        {
            "name": "OLX - Sealed Brand New",
            "item": {"id": "olx_new", "source": "olx.ua", "title": "Kingston Fury Beast 16GB DDR4", "price": 1400, "has_olx_delivery": True, "description": "Brand new RAM, factory sealed in retail blister pack."}
        },
        {
            "name": "OLX - Working Used",
            "item": {"id": "olx_ok", "source": "olx.ua", "title": "Kingston Fury Beast 16GB DDR4", "price": 1200, "has_olx_delivery": True, "description": "Used after upgrade, fully functional. Buyer protection delivery active."}
        },
        {
            "name": "OLX - Refusal / Scam",
            "item": {"id": "olx_bad", "source": "olx.ua", "title": "Sapphire Nitro+ RX 6800 XT", "price": 11000, "has_olx_delivery": True, "description": "No shipping! Local pickup only or wire transfer only."}
        },
        {
            "name": "Blocket (SE) - Brand New",
            "item": {"id": "blocket_new", "source": "blocket.se", "title": "Sapphire Nitro+ RX 6800 XT", "price": 12500, "kopsydd": True, "condition": "brand new", "description": "Brand new unopened in original box. Ships via Blocket Buyer Protection."}
        },
        {
            "name": "Blocket (SE) - Refusal / Scam",
            "item": {"id": "blocket_bad", "source": "blocket.se", "title": "RTX 3080 10GB", "price": 13000, "kopsydd": True, "description": "Wire transfer only in advance! No shipping."}
        },
        {
            "name": "eBay (US) - Brand New",
            "item": {"id": "ebay_new", "source": "ebay.com", "title": "AMD Ryzen 5 5600X AM4 12T", "price": 3800, "condition": "BRAND NEW", "buyerProtection": True, "description": "Brand New Sealed in box. Covered by eBay Money Back Guarantee."}
        },
        {
            "name": "Telemart (Retail / Outlet)",
            "item": {"id": "telemart_ok", "source": "telemart.ua", "title": "MSI B550-A PRO AM4 DDR4", "price": 3200, "outlet_reason": "Open-box, full 36-month store warranty", "description": "Official store warranty 36 months."}
        },
    ]

    print("\n[STAGE 1] Raw Marketplace Data (each site uses platform-specific flags & language):")
    print("─" * 90)
    for sample in raw_samples:
        it = sample["item"]
        keys = [k for k in ("has_olx_delivery", "kopsydd", "buyerProtection", "outlet_reason", "condition") if k in it]
        feat_str = f"{keys[0]}={it[keys[0]]}" if keys else "none"
        print(f"  • {sample['name']:<32} │ Site: {it['source']:<12} │ Native Flag: {feat_str}")

    # -------------------------------------------------------------------------
    # 2. NORMALIZATION IN SEARCH EXTENSION LAYER (ProviderFeatureAdapter)
    # -------------------------------------------------------------------------
    print("\n[STAGE 2] Search Extension Adaptation (normalizing into 'Secure Deal' & 'Condition'):")
    print("─" * 90)
    print(f"  {'Listing':<32} │ {'Secure Deal?':<15} │ {'Item Condition':<24} │ {'Deal Type'}")
    print("─" * 90)

    for sample in raw_samples:
        norm = ProviderFeatureAdapter.adapt_item(sample["item"])
        status = "YES (Secure)" if norm["is_secure_deal"] else "NO  (Refused)"
        cond_str = f"{norm['condition_label']} ({'new' if norm['is_new'] else 'used'})"
        print(f"  {sample['name']:<32} │ {status:<15} │ {cond_str:<24} │ {norm['deal_type']}")

    # -------------------------------------------------------------------------
    # 3. SITE-AGNOSTIC COMBINATORIAL SORTER (Sorter Layer)
    # -------------------------------------------------------------------------
    print("\n[STAGE 3] Sorter Synthesizes Build Under Budget (25,000 UAH):")
    print("          (Sorter only inspects 'is_secure_deal', condition, price, and specs,")
    print("           automatically rejecting fraudulent listings from any marketplace)")
    print("─" * 90)

    # Multi-marketplace candidate catalog
    multi_catalog = {
        "gpu": [
            # g_bad: OLX listing with escrow refusal in description (must be rejected!)
            {"id": "g_bad", "source": "olx.ua", "title": "Sapphire Nitro+ RX 6800 XT", "price": 11000, "has_olx_delivery": True, "description": "no shipping, wire transfer only", "photos": ["p.jpg"]},
            # g_blocket: Swedish Blocket listing with Buyer Protection (must be selected!)
            {"id": "g_blocket", "source": "blocket.se", "title": "Sapphire Nitro+ RX 6800 XT", "price": 12500, "kopsydd": True, "description": "Ships with buyer protection", "photos": ["p.jpg"]},
        ],
        "cpu": [
            # c_ebay: eBay with buyer protection
            {"id": "c_ebay", "source": "ebay.com", "title": "AMD Ryzen 5 5600X AM4 12T", "price": 3800, "buyerProtection": True, "photos": ["p.jpg"]},
        ],
        "mobo": [
            # m_telemart: Telemart with store warranty
            {"id": "m_telemart", "source": "telemart.ua", "title": "MSI B550-A PRO AM4 DDR4", "price": 3200, "warranty_months": 36, "photos": ["p.jpg"]},
        ],
        "ram": [
            # r_olx: OLX with verified OLX Delivery
            {"id": "r_olx", "source": "olx.ua", "title": "Kingston Fury Beast 16GB (2x8GB) DDR4 3200", "price": 1200, "has_olx_delivery": True, "photos": ["p.jpg"]},
        ],
        "psu": [
            # p_blocket: Blocket with Blocketpaketet
            {"id": "p_blocket", "source": "blocket.se", "title": "Seasonic FOCUS GX-750 750W Gold", "price": 3000, "blocketpaketet": True, "photos": ["p.jpg"]},
        ],
    }

    mock_provider = MockMarketProvider(name="multi_market", catalog=multi_catalog)
    instruction = PCSearchInstruction()
    candidate_pool = instruction.instruct_load_all(provider=mock_provider)

    sorter = PCSorter()
    builds = sorter.sort_and_synthesize(
        candidate_pool=candidate_pool,
        budget=25000,
        mode="best",
        max_proposals=1
    )

    if builds:
        b = builds[0]
        score = b.get("build_score", b.get("score", 0.0))
        print(f"  Synthesized Build Result: Total {b['total_cost']:,.0f} UAH (Surplus: {25000 - b['total_cost']:,.0f} UAH)")
        print(f"  Build Score: {score:.1f} pts\n")
        for sec in ("gpu", "cpu", "mobo", "ram", "psu"):
            part = b[sec]
            deal_lbl = part.get("deal_type") or ("Secure" if part.get("is_secure_deal") else "Unsecured")
            cond = part.get("condition_label") or ("new" if part.get("is_new") else "used")
            print(f"  • {sec.upper():<5} │ {part['model']:<32} │ {part['price']:>6,} UAH │ Cond: {cond:<12} │ Source: {part.get('source', 'unknown'):<11} │ {deal_lbl}")
        print("\n  Conclusion: The Sorter rejected the fraudulent RX 6800 XT at 11,000 UAH")
        print("  (due to escrow refusal) and synthesized a cross-border build from Blocket (SE),")
        print("  eBay (US), Telemart, and OLX (UA) using the normalized 'is_secure_deal' contract.")
    else:
        print("  No valid builds found.")
    print("=" * 80)


if __name__ == "__main__":
    main()
