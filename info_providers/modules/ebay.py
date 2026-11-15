"""
eBay Global Secondary Market Information Provider Module.
"""

from typing import Any, Dict
from info_providers.base import BaseInfoProvider, NormalizedListing
from provider_template import ProviderFeatureAdapter


class EbayProvider(BaseInfoProvider):
    name = "ebay"
    title = "eBay Marketplace"
    source_domain = "ebay.com"
    default_currency = "USD"
    description = "Global secondary & refurbished electronics market with Money Back Guarantee."

    def adapt_item(self, raw_data: Dict[str, Any]) -> NormalizedListing:
        adapted = ProviderFeatureAdapter.adapt_item(raw_data, self.source_domain)

        return NormalizedListing(
            id=str(adapted.get("id") or ""),
            title=str(adapted.get("title") or ""),
            price=float(adapted.get("price") or 0.0),
            currency=str(adapted.get("currency") or self.default_currency),
            url=str(adapted.get("url") or ""),
            source=str(adapted.get("source") or self.source_domain),
            description=str(adapted.get("description") or ""),
            is_new=bool(adapted.get("is_new", False)),
            condition=str(adapted.get("condition") or "used"),
            condition_label=str(adapted.get("condition_label") or "used"),
            is_secure_deal=bool(adapted.get("is_secure_deal", False)),
            deal_type=str(adapted.get("deal_type") or "Secure Deal (eBay Protection)"),
            seller_name=str(adapted.get("seller_name") or ""),
            seller_verified=bool(adapted.get("seller_verified", False)),
            seller_score=float(adapted.get("seller_score") or 75.0),
            raw=adapted.get("raw") if isinstance(adapted.get("raw"), dict) else raw_data,
        )
