"""
DEVELOPER BLUEPRINT: How to Create a New Information Provider Module.

This blueprint demonstrates how to integrate any third-party marketplace, classifieds site,
or retail aggregator (e.g. Amazon, eBay, Allegro, Blocket, or custom JSON feeds)
into the `omniforge` ecosystem.

================================================================================
STEP-BY-STEP INTEGRATION GUIDE:
================================================================================
1. Copy this file into `info_providers/modules/<your_site_slug>.py`
   (e.g., `info_providers/modules/example_market.py`).
2. Update the class name and class attributes:
   - `name`: Unique lowercase slug (e.g. "example_market").
   - `title`: Human-readable display title (e.g. "Example Marketplace").
   - `source_domain`: The canonical domain (e.g. "example-market.org").
   - `default_currency`: 3-letter ISO code (e.g. "UAH", "SEK", "USD", "EUR").
   - `version`, `author`, `description`, `tags`, `url`: Module Marketplace metadata.
3. Implement `adapt_item(raw_data) -> NormalizedListing`:
   Extract universal safety, condition, and pricing flags:
   - `is_secure_deal`: True if buyer escrow or retail return guarantee applies.
   - `condition`: 'new' | 'used' | 'refurbished' | 'for_parts'.
   - `price`: Numeric float in default currency.
4. (Optional) Implement `fetch_listings(category, query, pages)`:
   Connect to a local HTTP endpoint, API adapter, or JSON export directory.
5. Zero configuration required! The registry will automatically discover, validate,
   and register your module upon application start.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from info_providers.base import BaseInfoProvider, NormalizedListing


class CustomMarketplaceProvider(BaseInfoProvider):
    """
    Independent marketplace adapter module.
    Converts site-specific listing schemas into universal NormalizedListing instances.
    """

    # --- Marketplace Manifest Metadata (Marketplace Ready) ---
    name: str = "custom_template"
    title: str = "Custom Marketplace Blueprint"
    domain: str = "general"
    source_domain: str = "custom-market.com"
    default_currency: str = "USD"
    version: str = "1.0.0"
    author: str = "Community Contributor"
    min_app_version: str = "1.0.0"
    category: str = "marketplace"
    tags: List[str] = ["marketplace", "template", "classifieds"]
    url: str = "https://github.com/omniforge-dev/omniforge"
    description: str = "Exemplary blueprint for building decoupled marketplace information providers."

    # Pre-compiled regex patterns for refusal / escrow detection
    _REFUSAL_PATTERN = re.compile(
        r"\b(?:no\s+returns|sold\s+as-is|wire\s+transfer\s+only|cash\s+only|no\s+shipping)\b",
        re.IGNORECASE,
    )

    def is_available(self) -> bool:
        """
        Returns True if this provider is reachable (network connectivity, credentials, or local cache).
        """
        return True

    def adapt_item(self, raw_data: Dict[str, Any]) -> NormalizedListing:
        """
        Translates raw listing payload into universal NormalizedListing.

        Arguments:
            raw_data: Dictionary representing an ad/product from the marketplace API or HTML parser.

        Returns:
            NormalizedListing dataclass populated with standardized fields.
        """
        item_id = str(raw_data.get("id") or raw_data.get("item_id") or "")
        title = str(raw_data.get("title") or raw_data.get("name") or "")
        price = float(raw_data.get("price") or 0.0)
        currency = str(raw_data.get("currency") or self.default_currency)
        url = str(raw_data.get("url") or raw_data.get("link") or "")
        description = str(raw_data.get("description") or "")
        raw = raw_data.get("raw") if isinstance(raw_data.get("raw"), dict) else raw_data

        # 1. Escrow / Buyer Protection Detection
        text_haystack = f"{title} {description}".lower()
        if self._REFUSAL_PATTERN.search(text_haystack):
            is_secure_deal = False
            deal_type = "Escrow Refused / No Buyer Protection"
        else:
            # Check site-specific escrow flags
            is_secure_deal = bool(
                raw_data.get("has_buyer_protection")
                or raw_data.get("escrow_available")
                or raw.get("buyerProtection")
            )
            deal_type = "Secure Deal (Escrow)" if is_secure_deal else "Standard Deal"

        # 2. Condition Detection ('new' | 'used' | 'refurbished' | 'for_parts')
        raw_cond = str(raw_data.get("condition") or raw.get("condition") or "").lower()
        if any(w in raw_cond for w in ("new", "brand new", "sealed", "новий")):
            is_new = True
            condition = "new"
            condition_label = "new"
        elif any(w in raw_cond for w in ("refurbished", "outlet", "renewed")):
            is_new = False
            condition = "refurbished"
            condition_label = "refurbished"
        elif any(w in raw_cond for w in ("parts", "broken", "defekt", "на запчастини")):
            is_new = False
            condition = "for_parts"
            condition_label = "for parts"
        else:
            is_new = False
            condition = "used"
            condition_label = "used"

        # 3. Seller Metrics
        seller_name = str(raw_data.get("seller_name") or (raw.get("seller") or {}).get("name") or "")
        seller_score = float(raw_data.get("seller_score") or 75.0)
        seller_verified = bool(raw_data.get("seller_verified") or seller_score >= 80.0)

        return NormalizedListing(
            id=item_id,
            title=title,
            price=price,
            currency=currency,
            url=url,
            source=self.source_domain,
            description=description,
            is_new=is_new,
            condition=condition,
            condition_label=condition_label,
            is_secure_deal=is_secure_deal,
            deal_type=deal_type,
            seller_name=seller_name,
            seller_verified=seller_verified,
            seller_score=seller_score,
            raw=raw,
        )

    def fetch_listings(
        self,
        category: str,
        query: str = "",
        pages: int = 1,
        **kwargs: Any,
    ) -> List[NormalizedListing]:
        """
        Optional: Queries remote REST API or live endpoint and returns normalized listings.
        """
        return []
