"""
OpenAI GPT REST API AI Provider Module.
"""

import os
from typing import Any, Dict, Optional, Tuple
from ai_providers.base import BaseAIProvider


class OpenAIProvider(BaseAIProvider):
    name = "openai"
    title = "OpenAI GPT REST API"
    provider_type = "cloud"
    default_model = "gpt-4o-mini"
    description = "OpenAI Chat Completions API with structured json_object response."

    def is_ready(self) -> Tuple[bool, str]:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            return True, f"Key set ({key[:6]}...)"
        return False, "Set OPENAI_API_KEY"

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

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Missing OPENAI_API_KEY")

        target_model = model or self.default_model
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        data = _http_post_json(url, payload, headers=headers, timeout=timeout)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI returned no choices: {data}")
        raw_text = (choices[0].get("message") or {}).get("content", "")
        return extract_json_object(raw_text)
