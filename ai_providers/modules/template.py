"""
DEVELOPER BLUEPRINT: How to Create a New AI Provider Module.

This blueprint demonstrates how to integrate any custom AI reasoning backend,
local LLM server, or proprietary inference API (e.g., vLLM, LM Studio, Mistral,
AWS Bedrock, Azure OpenAI) into the `knapset` ecosystem.

================================================================================
STEP-BY-STEP INTEGRATION GUIDE:
================================================================================
1. Copy this file into `ai_providers/modules/<provider_slug>.py`
   (e.g., `ai_providers/modules/mistral.py` or `ai_providers/modules/vllm.py`).
2. Update the class name and class attributes:
   - `name`: Unique lowercase provider slug (e.g. "mistral", "vllm").
   - `title`: Human-readable display label (e.g. "Mistral Large REST API").
   - `kind`: "cloud" | "local" | "cli".
   - `default_model`: Recommended default model string.
   - `env_keys`: Environment variable names containing API keys.
   - `version`, `author`, `description`, `tags`, `url`: Module Marketplace metadata.
3. Implement `is_ready() -> Tuple[bool, str]`:
   Check if required API key(s) or local endpoint ports are active.
4. Implement `complete(prompt, system_prompt="", model=None, **kwargs) -> Dict[str, Any]`:
   - Construct standard HTTP payload (pure Python stdlib `urllib.request` — ZERO external SDKs!).
   - Send HTTPS POST request and parse JSON structured response.
5. Zero configuration required! The registry auto-registers your AI provider and
   exposes it via `main.py providers` and `--ai-provider <provider_slug>`.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from ai_providers.base import BaseAIProvider


class CustomAIProvider(BaseAIProvider):
    """
    Independent AI inference provider module.
    Communicates via pure standard library HTTPS without requiring heavy pip SDKs.
    """

    # --- Marketplace Manifest Metadata (Marketplace Ready) ---
    name: str = "custom_ai_template"
    title: str = "Custom AI Engine Blueprint"
    label: str = "Custom AI Engine"
    domain: str = "general"
    kind: str = "cloud"  # "cloud" | "local" | "cli"
    provider_type: str = "cloud"
    default_model: str = "custom-model-v1"
    env_keys: List[str] = ["CUSTOM_AI_API_KEY"]
    notes: str = "Template for integrating custom cloud, local, or corporate LLM inference endpoints."
    version: str = "1.0.0"
    author: str = "Community Contributor"
    min_app_version: str = "1.0.0"
    category: str = "ai_backend"
    tags: List[str] = ["ai", "llm", "inference", "template"]
    url: str = "https://github.com/knapset-dev/knapset"
    description: str = "Exemplary blueprint for building decoupled standard-library AI inference providers."

    def is_ready(self) -> Tuple[bool, str]:
        """
        Checks if required API key or local daemon is available.
        """
        for k in self.env_keys:
            val = os.environ.get(k, "").strip()
            if val:
                masked = f"{val[:4]}...{val[-3:] if len(val) > 7 else '***'}"
                return True, f"{k} set ({masked})"
        return False, f"Missing {' or '.join(self.env_keys)}"

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Executes structured LLM inference using standard library urllib.

        Arguments:
            prompt: User inspection instructions or listing metadata.
            system_prompt: Guiding security / hardware expert persona.
            model: Specific model override.
            json_schema: Desired response shape.
            timeout: Network request timeout in seconds.

        Returns:
            Structured dictionary parsed from model response.
        """
        target_model = model or self.default_model
        api_key = os.environ.get("CUSTOM_AI_API_KEY", "").strip()

        # If running in mock / demonstration mode without real external credentials
        if not api_key or api_key.startswith("demo"):
            return {
                "verdict": "approved",
                "confidence": 0.95,
                "confidence_score": 95,
                "reasons": ["Custom template mock inference demonstration."],
            }

        endpoint = "https://api.custom-ai-provider.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "HardwareMarketIntelligence/1.0",
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": 0.2,
        }

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                try:
                    return json.loads(content)
                except Exception:
                    return {"raw_response": content}
        except urllib.error.URLError as e:
            return {
                "error": f"Network error: {e}",
                "verdict": "ERROR",
                "confidence_score": 0,
                "reasons": [str(e)],
            }
