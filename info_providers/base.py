"""
Base Interface & Data Models for Pluggable Market Information Providers.

Modules living in `info_providers/modules/` implement `BaseInfoProvider`.
They are completely decoupled and know only their own marketplace protocols.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request


class SecurityViolationError(PermissionError):
    """Raised when an untrusted network request violates security boundaries (SSRF, local IP)."""
    pass


def safe_urlopen(
    url: str,
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """
    Safely executes an HTTP/HTTPS GET request, preventing SSRF attacks.
    - Scheme must be strictly 'http' or 'https'
    - Host must not resolve to loopback, link-local, private, or multicast IP addresses
    """
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise SecurityViolationError(f"Prohibited URL scheme '{parsed.scheme}://'. Only http and https are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise SecurityViolationError("Missing hostname in URL")

    # Check for direct IP address literal or DNS resolution
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise urllib.error.URLError(f"DNS resolution failure for '{hostname}': {e}")

    for _, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback:
            raise SecurityViolationError(f"Access to loopback address '{ip_str}' is prohibited.")
        if ip.is_link_local:
            raise SecurityViolationError(f"Access to link-local address '{ip_str}' is prohibited.")
        if ip.is_private:
            raise SecurityViolationError(f"Access to private internal network address '{ip_str}' is prohibited.")
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise SecurityViolationError(f"Access to reserved/special address '{ip_str}' is prohibited.")

    req = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "HardwareMarketIntelligence/1.0"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


@dataclass
class NormalizedListing:
    """Universal listing representation consumable by any sorter or analyzer."""
    id: str
    title: str
    price: float
    currency: str = "USDT"
    url: str = ""
    source: str = ""
    description: str = ""
    is_new: bool = False
    condition: str = "used"
    condition_label: str = "used"
    is_secure_deal: bool = False
    deal_type: str = "standard"
    seller_name: str = ""
    seller_verified: bool = False
    seller_score: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "url": self.url,
            "source": self.source,
            "description": self.description,
            "is_new": self.is_new,
            "condition": self.condition,
            "condition_label": self.condition_label,
            "is_secure_deal": self.is_secure_deal,
            "has_buyer_protection": self.is_secure_deal,
            "has_olx_delivery": self.is_secure_deal,  # Backward compatibility alias
            "deal_type": self.deal_type,
            "seller_name": self.seller_name,
            "seller_verified": self.seller_verified,
            "seller_score": self.seller_score,
            "raw": self.raw,
        }


class BaseInfoProvider(ABC):
    """
    Abstract contract for all marketplace/aggregator information providers.
    
    To implement a new provider:
    1. Create a new file in `info_providers/modules/<name>.py`
    2. Subclass `BaseInfoProvider`
    3. Define `name`, `title`, `source_domain`, and `default_currency`
    4. Implement `adapt_item` and optionally `fetch_listings`
    """
    name: str = "base"
    title: str = "Base Provider"
    domain: str = "general"
    aliases: List[str] = []
    source_domain: str = "generic.com"
    default_currency: str = "USDT"
    description: str = ""
    version: str = "1.0.0"
    author: str = "Core Team"
    min_app_version: str = "1.0.0"
    category: str = "marketplace"
    tags: List[str] = []
    url: str = ""

    def get_manifest(self) -> Dict[str, Any]:
        """Returns standardized metadata manifest for Module Marketplace."""
        return {
            "name": getattr(self, "name", "unknown"),
            "title": getattr(self, "title", self.name.capitalize()),
            "domain": getattr(self, "domain", "general"),
            "aliases": list(getattr(self, "aliases", [])),
            "version": getattr(self, "version", "1.0.0"),
            "author": getattr(self, "author", "Community"),
            "description": getattr(self, "description", ""),
            "min_app_version": getattr(self, "min_app_version", "1.0.0"),
            "category": getattr(self, "category", "marketplace"),
            "tags": list(getattr(self, "tags", [])),
            "url": getattr(self, "url", ""),
            "source_domain": getattr(self, "source_domain", ""),
            "default_currency": getattr(self, "default_currency", "USDT"),
        }

    def is_available(self) -> bool:
        """Returns True if this provider can be queried (network, credentials, or local cache)."""
        return True

    @abstractmethod
    def adapt_item(self, raw_data: Dict[str, Any]) -> NormalizedListing:
        """
        Converts site-specific raw listing data into a NormalizedListing
        with universal flags (is_secure_deal, condition, etc.).
        """
        pass

    def fetch_listings(
        self,
        category: str,
        query: str = "",
        pages: int = 1,
        **kwargs: Any,
    ) -> List[NormalizedListing]:
        """
        Loads listings for the target category from the local market cache provider.
        """
        try:
            from provider_template import get_market_provider
            kwargs.pop("live", None)
            prov = get_market_provider(self.name)
            items = prov.search_items(query=query, category_hint=category, limit=pages * 40, **kwargs)
            return [self.adapt_item(it.to_dict()) for it in items]
        except Exception:
            return []
