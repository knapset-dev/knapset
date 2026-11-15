"""
Cohere AI Enterprise Reasoning Provider Module.

Enterprise-grade multilingual extraction & structured classification via Command R+.
"""

import os
from typing import Dict, Any, List, Optional
from ai_providers import BaseAIProvider, register_ai_provider


@register_ai_provider("cohere")
class CohereAIProvider(BaseAIProvider):
    name = "cohere"
    label = "Cohere API"
    kind = "cloud"
    default_model = "command-r-plus-08-2024"
    env_keys = ["COHERE_API_KEY"]
    notes = "Command R+ enterprise multilingual classification"
    aliases = ["cohere-ai", "command-r"]

    def call(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        timeout: float = 60.0,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        http_post_fn: Optional[Any] = None,
        **kwargs,
    ) -> str:
        key = api_key or os.environ.get("COHERE_API_KEY", "")
        if not key:
            raise RuntimeError("Missing COHERE_API_KEY")

        url = (base_url or "https://api.cohere.com/v2").rstrip("/") + "/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {key}"}

        if http_post_fn:
            data = http_post_fn(url, payload, headers=headers, timeout=timeout)
        else:
            from ai_providers import _http_post_json
            data = _http_post_json(url, payload, headers=headers, timeout=timeout)

        msg = data.get("message") or {}
        content_items = msg.get("content") or []
        for c in content_items:
            if c.get("type") == "text":
                return c.get("text", "")
        raise RuntimeError(f"Cohere returned no text content: {data}")
