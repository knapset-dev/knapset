"""
Pluggable Market Information Providers Subsystem.

Houses modular marketplace adapters in `info_providers/modules/`.
"""

from info_providers.base import BaseInfoProvider, NormalizedListing
from info_providers.registry import InfoProviderRegistry, get_info_registry

get_provider_registry = get_info_registry

__all__ = [
    "BaseInfoProvider",
    "NormalizedListing",
    "InfoProviderRegistry",
    "get_info_registry",
    "get_provider_registry",
]
