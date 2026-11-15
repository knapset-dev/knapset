"""
Together AI Reasoning Provider Module.

Ultra-fast inference gateway for open-weights LLMs (Llama, Qwen, DeepSeek, Mistral).
"""

import os
from typing import Dict, Any, List, Optional
from ai_providers import BaseAIProvider, register_ai_provider


@register_ai_provider("together")
class TogetherAIProvider(BaseAIProvider):
    name = "together"
    label = "Together AI API"
    kind = "cloud"
    default_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    env_keys = ["TOGETHER_API_KEY"]
    notes = "High-throughput cloud inference for open-weights LLMs"
    aliases = ["together-ai"]

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
        key = api_key or os.environ.get("TOGETHER_API_KEY", "")
        if not key:
            raise RuntimeError("Missing TOGETHER_API_KEY")

        url = (base_url or "https://api.together.xyz/v1").rstrip("/") + "/chat/completions"
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

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Together AI returned no choices: {data}")
        return choices[0].get("message", {}).get("content", "")
