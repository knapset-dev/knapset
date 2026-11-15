"""
Pluggable AI Backend Providers Subsystem.

Houses modular AI model and agent implementations in `ai_providers/modules/`.
Provides full backward compatibility for existing callers.
"""

from ai_providers.base import BaseAIProvider, AIProviderMetadata
from ai_providers.registry import AIProviderRegistry, get_ai_registry

# Backward-compatible re-exports from core engine
from ai_providers.engine import (
    AIProviderClient,
    check_provider_status,
    extract_json_object,
    list_provider_names,
    list_all_provider_statuses,
    load_ai_provider_file,
    normalize_provider_name,
    register_ai_provider,
    auto_discover_ai_providers,
    _http_post_json,
    main,
)

__all__ = [
    "BaseAIProvider",
    "AIProviderMetadata",
    "AIProviderRegistry",
    "get_ai_registry",
    "AIProviderClient",
    "check_provider_status",
    "extract_json_object",
    "list_provider_names",
    "list_all_provider_statuses",
    "load_ai_provider_file",
    "normalize_provider_name",
    "register_ai_provider",
    "auto_discover_ai_providers",
    "_http_post_json",
    "main",
]
