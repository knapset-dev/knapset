"""
Telemart.ua Ukrainian Computer Store & Outlet Information Provider Module.
"""

from typing import Any, Dict
from info_providers.base import BaseInfoProvider, NormalizedListing
from provider_template import ProviderFeatureAdapter


class TelemartProvider(BaseInfoProvider):
    name = "telemart"
    title = "Telemart.ua (Store & Outlet)"
    source_domain = "telemart.ua"
    default_currency = "UAH"
    description = "Official Ukrainian retail electronics store with certified refurbished outlet section."

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
            condition=str(adapted.get("condition") or "new"),
            condition_label=str(adapted.get("condition_label") or "new (retail warranty)"),
            is_secure_deal=bool(adapted.get("is_secure_deal", True)),
            deal_type=str(adapted.get("deal_type") or "Secure Deal (Store Warranty)"),
            seller_name=str(adapted.get("seller_name") or "Telemart.ua"),
            seller_verified=bool(adapted.get("seller_verified", True)),
            seller_score=float(adapted.get("seller_score") or 99.0),
            raw=adapted.get("raw") if isinstance(adapted.get("raw"), dict) else raw_data,
        )
