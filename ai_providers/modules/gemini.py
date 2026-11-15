"""
Google Gemini REST API AI Provider Module.
"""

import os
from typing import Any, Dict, Optional, Tuple
from ai_providers.base import BaseAIProvider


class GeminiProvider(BaseAIProvider):
    name = "gemini"
    title = "Google Gemini REST API"
    provider_type = "cloud"
    default_model = "gemini-2.5-flash"
    description = "Ultra-fast Google Gemini multimodal REST API."

    def is_ready(self) -> Tuple[bool, str]:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if key:
            return True, f"Key set ({key[:6]}...)"
        return False, "Set GEMINI_API_KEY or GOOGLE_API_KEY"

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

        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("Missing GEMINI_API_KEY or GOOGLE_API_KEY")

        target_model = model or self.default_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={key}"
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
        payload = {
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        data = _http_post_json(url, payload, headers={}, timeout=timeout)
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        raw_text = "".join(p.get("text", "") for p in parts)
        return extract_json_object(raw_text)
