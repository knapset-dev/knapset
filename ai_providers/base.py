"""
Base Interface & Data Models for Pluggable AI Backend Providers.

Modules living in `ai_providers/modules/` implement `BaseAIProvider`.
They are completely self-contained and execute structured LLM inference.
Provides unified compatibility with both modern `complete()` and legacy `call()` protocols.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AIProviderMetadata:
    name: str
    title: str
    provider_type: str = "cloud"  # "cloud" | "local" | "cli"
    default_model: str = ""
    description: str = ""


class BaseAIProvider:
    """
    Contract for all AI model providers and local agents.
    
    Supports both:
    1. Modern protocol: implement `complete(prompt, system_prompt, ...)` and `is_ready()`
    2. Legacy protocol: implement `call(model, prompt, system_prompt, ...)` with `env_keys`
    """
    name: str = "base"
    title: str = "Base AI Provider"
    label: str = ""
    kind: str = "cloud"
    provider_type: str = "cloud"
    default_model: str = ""
    env_keys: List[str] = []
    notes: str = ""
    description: str = ""
    aliases: List[str] = []
    version: str = "1.0.0"
    author: str = "Core Team"
    min_app_version: str = "1.0.0"
    category: str = "ai_backend"
    tags: List[str] = []
    url: str = ""

    def get_manifest(self) -> Dict[str, Any]:
        """Returns standardized metadata manifest for Module Marketplace."""
        return {
            "name": getattr(self, "name", "unknown"),
            "title": getattr(self, "label", getattr(self, "title", self.name.capitalize())),
            "version": getattr(self, "version", "1.0.0"),
            "author": getattr(self, "author", "Community"),
            "description": getattr(self, "description", getattr(self, "notes", "")),
            "min_app_version": getattr(self, "min_app_version", "1.0.0"),
            "category": getattr(self, "category", "ai_backend"),
            "tags": list(getattr(self, "tags", [])),
            "url": getattr(self, "url", ""),
            "kind": getattr(self, "kind", getattr(self, "provider_type", "cloud")),
            "default_model": getattr(self, "default_model", "auto"),
            "env_keys": list(getattr(self, "env_keys", [])),
        }

    def check_status(self) -> Dict[str, Any]:
        ready = False
        detail = ""
        for k in self.env_keys:
            val = os.environ.get(k, "").strip()
            if val:
                ready = True
                detail = f"{k} set ({val[:4]}...{val[-3:] if len(val) > 7 else '***'})"
                break
        if not ready and self.env_keys:
            detail = f"Set {' or '.join(self.env_keys)}"
        elif not self.env_keys:
            ready, detail = self.is_ready()

        lbl = self.label or self.title or self.name.capitalize()
        return {
            "provider": self.name,
            "label": lbl,
            "kind": self.kind or self.provider_type,
            "ready": ready,
            "default_model": self.default_model or "auto",
            "detail": detail,
            "notes": self.notes or self.description,
        }

    def is_ready(self) -> Tuple[bool, str]:
        """Checks if credentials or binary are available."""
        if self.env_keys:
            for k in self.env_keys:
                if os.environ.get(k, "").strip():
                    return True, "Ready"
            return False, f"Missing {' or '.join(self.env_keys)}"
        return True, "Ready"

    def call(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        http_post_fn: Optional[Any] = None,
        **kwargs: Any,
    ) -> str:
        """Executes LLM reasoning and returns raw response string."""
        res = self.complete(prompt=prompt, system_prompt=system_prompt, model=model, **kwargs)
        if isinstance(res, (dict, list)):
            return json.dumps(res)
        return str(res)

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Sends prompt to LLM and returns parsed JSON or structured dictionary."""
        m = model or self.default_model or "auto"
        raw = self.call(model=m, prompt=prompt, system_prompt=system_prompt, **kwargs)
        try:
            return json.loads(raw)
        except Exception:
            return {"raw_text": raw}
