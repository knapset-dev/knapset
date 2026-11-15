"""
Anthropic Claude Messages API AI Provider Module.
"""

import os
from typing import Any, Dict, Optional, Tuple
from ai_providers.base import BaseAIProvider


class AnthropicProvider(BaseAIProvider):
    name = "anthropic"
    title = "Anthropic Claude REST API"
    provider_type = "cloud"
    default_model = "claude-3-5-haiku-latest"
    description = "Anthropic Claude Messages API."

    def is_ready(self) -> Tuple[bool, str]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return True, f"Key set ({key[:6]}...)"
        return False, "Set ANTHROPIC_API_KEY"

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        from ai_providers.engine import _http_post_json, extract_json_object

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY")

        target_model = model or self.default_model
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        sys_txt = (system_prompt + "\nReturn strictly a valid JSON object.").strip()
        payload = {
            "model": target_model,
            "max_tokens": 1024,
            "temperature": 0.1,
            "system": sys_txt,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _http_post_json(url, payload, headers=headers, timeout=timeout)
        content_blocks = data.get("content") or []
        raw_text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        return extract_json_object(raw_text)
