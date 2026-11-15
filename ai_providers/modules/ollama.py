"""
Local Ollama Server AI Provider Module.
"""

import os
from typing import Any, Dict, Optional, Tuple
from ai_providers.base import BaseAIProvider


class OllamaProvider(BaseAIProvider):
    name = "ollama"
    title = "Local Ollama Server"
    provider_type = "local"
    default_model = "auto"
    description = "Private offline local LLM inference server."

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

    def is_ready(self) -> Tuple[bool, str]:
        from ai_providers.engine import _probe_ollama
        ok, models = _probe_ollama(self.host)
        if ok and models:
            return True, f"Online ({len(models)} models: {', '.join(models[:2])})"
        elif ok:
            return False, "Online but 0 models pulled (run `ollama pull qwen2.5:7b`)"
        return False, f"Offline at {self.host} (run `ollama serve`)"

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        timeout: int = 120,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        from ai_providers.engine import _http_post_json, _probe_ollama, _select_best_ollama_model, extract_json_object

        target_model = model
        if not target_model or target_model == "auto":
            _, models = _probe_ollama(self.host)
            target_model = _select_best_ollama_model(models)

        url = f"{self.host.rstrip('/')}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        data = _http_post_json(url, payload, headers={}, timeout=timeout)
        raw_text = (data.get("message") or {}).get("content", "")
        return extract_json_object(raw_text)
