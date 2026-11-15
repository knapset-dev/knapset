#!/usr/bin/env python3
"""
Pluggable Multi-Provider AI Backend Engine (Zero External Dependencies).

Supports Cloud REST APIs, Local LLM Servers, and Local CLI Coding Agents:
  - auto          : Smart detection & automatic failover across ready providers
  - gemini        : Google Gemini REST API (GEMINI_API_KEY / GOOGLE_API_KEY)
  - openai        : OpenAI REST API (OPENAI_API_KEY)
  - anthropic     : Anthropic Claude Messages API (ANTHROPIC_API_KEY) [alias: claude]
  - groq          : Groq LPU Ultra-Fast API (GROQ_API_KEY)
  - deepseek      : DeepSeek REST API (DEEPSEEK_API_KEY)
  - openrouter    : OpenRouter Unified Gateway (OPENROUTER_API_KEY)
  - mistral       : Mistral AI REST API (MISTRAL_API_KEY)
  - ollama        : Local Ollama Server (OLLAMA_HOST, default http://127.0.0.1:11434)
  - openai-compat : Custom OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp, Together)
  - agy           : Antigravity CLI local agent
  - claude-cli    : Anthropic Claude CLI local agent
  - gemini-cli    : Google Gemini CLI local agent
"""

import argparse
import importlib
import importlib.util
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent
if ROOT_DIR.name == "ai_providers":
    ROOT_DIR = ROOT_DIR.parent


def _load_dotenv_files():
    """Lightweight zero-dependency .env loader (does not overwrite existing env vars)."""
    candidates = [
        ROOT_DIR / ".env",
        Path.cwd() / ".env",
        Path.home() / ".config" / "hmi" / ".env",
    ]
    seen = set()
    for env_path in candidates:
        try:
            resolved = env_path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[7:].strip()
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'").strip('"')
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            continue


_load_dotenv_files()

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """
    Robustly extracts a JSON object from any LLM output, handling reasoning
    <think> tags, Markdown fences, preamble prose, and trailing commas.
    """
    if not raw_text:
        raise ValueError("Empty AI response")

    cleaned = _THINK_TAG_RE.sub("", raw_text).strip()

    fence_match = _CODE_FENCE_RE.search(cleaned)
    if fence_match:
        candidate = fence_match.group(1).strip()
        if candidate.startswith("{"):
            cleaned = candidate

    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        snippet = cleaned[start_idx : end_idx + 1]
    else:
        snippet = cleaned

    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        fixed = _TRAILING_COMMA_RE.sub(r"\1", snippet)
        return json.loads(fixed)


def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float = 45.0,
    retries: int = 1,
) -> Dict[str, Any]:
    """Performs an HTTPS/HTTP JSON POST using stdlib urllib with retry on transient errors."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "HardwareMarketIntelligence/2.0",
        **headers,
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                pass
            last_err = RuntimeError(f"HTTP {e.code} from {url}: {err_body or e.reason}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.8 * (2 ** attempt))
                continue
            raise last_err
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.6 * (2 ** attempt))
                continue
            raise
    raise last_err or RuntimeError("HTTP request failed")


def _find_agy_binary() -> Optional[str]:
    env_p = os.environ.get("HMI_AGY_PATH")
    if env_p and Path(env_p).is_file():
        return env_p
    which_p = shutil.which("agy")
    if which_p:
        return which_p
    local_bin = Path.home() / ".local" / "bin" / "agy"
    if local_bin.is_file():
        return str(local_bin)
    return None


def _probe_ollama(host: str, timeout: float = 0.5) -> Tuple[bool, List[str]]:
    """Checks if local Ollama daemon is running and lists installed models."""
    url = f"{host.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            return True, models
    except Exception:
        return False, []


def _select_best_ollama_model(installed: List[str]) -> str:
    if not installed:
        return "qwen2.5:7b"
    preferred_prefixes = ("qwen3", "qwen2.5", "llama3.3", "llama3.1", "llama3", "gemma3", "gemma2", "mistral", "deepseek")
    for pref in preferred_prefixes:
        for m in installed:
            if m.lower().startswith(pref):
                return m
    return installed[0]


PROVIDER_ALIASES = {
    "auto": "auto",
    "gemini": "gemini",
    "google": "gemini",
    "openai": "openai",
    "gpt": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "groq": "groq",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "mistral": "mistral",
    "ollama": "ollama",
    "local": "ollama",
    "openai-compat": "openai-compat",
    "lmstudio": "openai-compat",
    "vllm": "openai-compat",
    "custom-api": "openai-compat",
    "agy": "agy",
    "claude-cli": "claude-cli",
    "gemini-cli": "gemini-cli",
}

PROVIDER_SPECS: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini API",
        "default_model": "gemini-2.5-flash",
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "kind": "cloud",
        "notes": "Fast JSON mode, generous free tier via Google AI Studio",
    },
    "openai": {
        "label": "OpenAI API",
        "default_model": "gpt-4o-mini",
        "env_keys": ["OPENAI_API_KEY"],
        "kind": "cloud",
        "notes": "Structured JSON output (gpt-4o-mini, gpt-4.1-mini, gpt-4o)",
    },
    "anthropic": {
        "label": "Anthropic Claude API",
        "default_model": "claude-3-5-haiku-latest",
        "env_keys": ["ANTHROPIC_API_KEY"],
        "kind": "cloud",
        "notes": "High semantic nuance in Ukrainian/Russian classifieds",
    },
    "groq": {
        "label": "Groq LPU API",
        "default_model": "llama-3.3-70b-versatile",
        "env_keys": ["GROQ_API_KEY"],
        "kind": "cloud",
        "notes": "Ultra-low latency (~500+ tok/s) on Llama / Qwen models",
    },
    "deepseek": {
        "label": "DeepSeek API",
        "default_model": "deepseek-chat",
        "env_keys": ["DEEPSEEK_API_KEY"],
        "kind": "cloud",
        "notes": "Very low cost per million tokens (DeepSeek-V3 / R1)",
    },
    "openrouter": {
        "label": "OpenRouter Gateway",
        "default_model": "google/gemini-2.5-flash",
        "env_keys": ["OPENROUTER_API_KEY"],
        "kind": "cloud",
        "notes": "Single API key for 300+ models (including free models)",
    },
    "mistral": {
        "label": "Mistral AI API",
        "default_model": "mistral-small-latest",
        "env_keys": ["MISTRAL_API_KEY"],
        "kind": "cloud",
        "notes": "Fast European multilingual API with native JSON mode",
    },
    "ollama": {
        "label": "Ollama (Local Offline)",
        "default_model": "auto",
        "env_keys": ["OLLAMA_HOST"],
        "kind": "local",
        "notes": "100% free & private local inference on your own GPU/CPU",
    },
    "openai-compat": {
        "label": "OpenAI-Compatible Endpoint",
        "default_model": "local-model",
        "env_keys": ["HMI_API_BASE", "HMI_API_KEY"],
        "kind": "local/custom",
        "notes": "Works with LM Studio (:1234), vLLM (:8000), llama.cpp, Together",
    },
    "agy": {
        "label": "Antigravity CLI (agy)",
        "default_model": "gemini-3.8-flash-low",
        "env_keys": [],
        "kind": "cli",
        "notes": "Zero-key local CLI agent (supports Gemini, Claude, GPT-OSS)",
    },
    "claude-cli": {
        "label": "Claude Code CLI",
        "default_model": "haiku",
        "env_keys": [],
        "kind": "cli",
        "notes": "Uses local `claude -p` authenticated session",
    },
    "gemini-cli": {
        "label": "Google Gemini CLI",
        "default_model": "gemini-2.5-flash",
        "env_keys": [],
        "kind": "cli",
        "notes": "Uses local `gemini -p` authenticated session",
    },
}

from ai_providers.base import BaseAIProvider


_MODULAR_AI_PROVIDERS: Dict[str, BaseAIProvider] = {}
_AI_DISCOVERED: bool = False


def register_ai_provider(name: Optional[str] = None):
    """Decorator or function to register a custom AI reasoning provider."""
    def decorator(cls_or_instance):
        obj = cls_or_instance() if isinstance(cls_or_instance, type) else cls_or_instance
        key = (name or getattr(obj, "name", "")).lower().strip()
        _MODULAR_AI_PROVIDERS[key] = obj

        PROVIDER_SPECS[key] = {
            "label": getattr(obj, "label", key),
            "default_model": getattr(obj, "default_model", "auto"),
            "env_keys": getattr(obj, "env_keys", []),
            "kind": getattr(obj, "kind", "custom"),
            "notes": getattr(obj, "notes", "Modular AI provider"),
        }
        PROVIDER_ALIASES[key] = key
        for alias in getattr(obj, "aliases", []):
            clean_alias = alias.lower().strip()
            PROVIDER_ALIASES[clean_alias] = key

        return cls_or_instance
    return decorator


def load_ai_provider_file(file_path: Path) -> Optional[BaseAIProvider]:
    """Dynamically loads and registers a modular AI provider from an arbitrary .py file."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"AI provider file not found: {path}")

    mod_name = f"custom_ai_provider_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if not spec or not spec.loader:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    for attr in dir(module):
        val = getattr(module, attr)
        if isinstance(val, type) and issubclass(val, BaseAIProvider) and val is not BaseAIProvider:
            inst = val()
            register_ai_provider()(inst)
            return inst
    return None


def auto_discover_ai_providers(extra_dirs: Optional[List[str]] = None) -> None:
    """
    Scans `ai_providers/modules/` and any paths in `HMI_AI_PROVIDERS_DIR`
    to dynamically discover and register all modular AI providers.
    """
    global _AI_DISCOVERED
    subsystem_dir = ROOT_DIR / "ai_providers" / "modules"

    dirs_to_scan: List[Path] = []
    if subsystem_dir.is_dir():
        dirs_to_scan.append(subsystem_dir)

    env_paths = os.environ.get("HMI_AI_PROVIDERS_DIR", "").strip()
    if env_paths:
        for p_str in env_paths.split(os.pathsep):
            p = Path(p_str.strip())
            if p.is_dir() and p not in dirs_to_scan:
                dirs_to_scan.append(p)

    if extra_dirs:
        for p_str in extra_dirs:
            p = Path(p_str.strip())
            if p.is_dir() and p not in dirs_to_scan:
                dirs_to_scan.append(p)

    for scan_dir in dirs_to_scan:
        for _, module_name, is_pkg in pkgutil.iter_modules([str(scan_dir)]):
            if is_pkg or module_name.startswith("_"):
                continue
            try:
                file_path = scan_dir / f"{module_name}.py"
                load_ai_provider_file(file_path)
            except Exception:
                continue

    _AI_DISCOVERED = True


SUPPORTED_PROVIDER_CHOICES = sorted(list(PROVIDER_ALIASES.keys()))


def list_provider_names() -> List[str]:
    """Returns canonical provider names plus aliases accepted by --ai-provider."""
    if not _AI_DISCOVERED:
        auto_discover_ai_providers()
    canonical = ["auto"] + list(PROVIDER_SPECS.keys())
    extras = [k for k in sorted(PROVIDER_ALIASES.keys()) if k not in canonical]
    return canonical + extras


def normalize_provider_name(name: Optional[str]) -> str:
    if not _AI_DISCOVERED:
        auto_discover_ai_providers()
    if not name:
        name = os.environ.get("HMI_AI_PROVIDER", "auto")
    clean = name.strip().lower()
    if clean not in PROVIDER_ALIASES:
        valid = ", ".join(["auto"] + list(PROVIDER_SPECS.keys()))
        raise ValueError(f"Unknown AI provider '{name}'. Supported providers: {valid}")
    return PROVIDER_ALIASES[clean]


def check_provider_status(provider: str) -> Dict[str, Any]:
    """Inspects whether a specific provider is currently configured and available."""
    p = normalize_provider_name(provider)
    if p in _MODULAR_AI_PROVIDERS:
        return _MODULAR_AI_PROVIDERS[p].check_status()
    spec = PROVIDER_SPECS[p]
    ready = False
    detail = ""
    default_model = spec["default_model"]

    if p in ("gemini", "openai", "anthropic", "groq", "deepseek", "openrouter", "mistral"):
        for k in spec["env_keys"]:
            val = os.environ.get(k, "").strip()
            if val:
                ready = True
                detail = f"{k} set ({val[:4]}...{val[-3:] if len(val) > 7 else '***'})"
                break
        if not ready:
            detail = f"Set {' or '.join(spec['env_keys'])}"

    elif p == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        alive, models = _probe_ollama(host)
        if alive:
            ready = True
            default_model = _select_best_ollama_model(models)
            detail = f"Online at {host} ({len(models)} models)"
        else:
            detail = f"Offline at {host} (run `ollama serve`)"

    elif p == "openai-compat":
        base = os.environ.get("HMI_API_BASE", "").strip()
        if base:
            ready = True
            detail = f"HMI_API_BASE={base}"
        else:
            detail = "Set HMI_API_BASE (e.g. http://127.0.0.1:1234/v1)"

    elif p == "agy":
        bin_p = _find_agy_binary()
        if bin_p:
            ready = True
            detail = f"Binary: {bin_p}"
        else:
            detail = "`agy` binary not found in PATH"

    elif p == "claude-cli":
        bin_p = shutil.which("claude")
        if bin_p:
            ready = True
            detail = f"Binary: {bin_p}"
        else:
            detail = "`claude` binary not found in PATH"

    elif p == "gemini-cli":
        bin_p = shutil.which("gemini")
        if bin_p:
            ready = True
            detail = f"Binary: {bin_p}"
        else:
            detail = "`gemini` binary not found in PATH"

    return {
        "provider": p,
        "label": spec["label"],
        "kind": spec["kind"],
        "ready": ready,
        "default_model": default_model,
        "detail": detail,
        "notes": spec["notes"],
    }


def list_all_provider_statuses() -> List[Dict[str, Any]]:
    if not _AI_DISCOVERED:
        auto_discover_ai_providers()
    return [check_provider_status(p) for p in list(PROVIDER_SPECS.keys())]


class AIProviderClient:
    """
    Unified multi-provider LLM client for structured JSON hardware evaluations.
    """

    # Priority order when provider="auto"
    AUTO_PRIORITY = [
        "gemini",
        "groq",
        "openai",
        "anthropic",
        "deepseek",
        "openrouter",
        "mistral",
        "openai-compat",
        "agy",
        "gemini-cli",
        "claude-cli",
        "ollama",
    ]

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.requested_provider = normalize_provider_name(provider)
        self.explicit_model = model or os.environ.get("HMI_AI_MODEL") or None
        self.base_url = base_url or os.environ.get("HMI_API_BASE") or None
        self.api_key = api_key or os.environ.get("HMI_API_KEY") or None
        self.timeout = timeout

        # Resolve active provider chain
        self._chain = self._build_provider_chain()
        self.active_provider = self._chain[0] if self._chain else "none"
        self.active_model = self._resolve_model(self.active_provider)

    def _build_provider_chain(self) -> List[str]:
        if not _AI_DISCOVERED:
            auto_discover_ai_providers()
        if self.requested_provider != "auto":
            return [self.requested_provider]

        ready_list = []
        checked = set()
        for p in self.AUTO_PRIORITY:
            checked.add(p)
            st = check_provider_status(p)
            if st["ready"]:
                ready_list.append(p)
        for p in _MODULAR_AI_PROVIDERS:
            if p not in checked:
                st = _MODULAR_AI_PROVIDERS[p].check_status()
                if st.get("ready"):
                    ready_list.append(p)
        return ready_list

    def _resolve_model(self, provider: str) -> str:
        if self.explicit_model:
            return self.explicit_model
        if provider == "none":
            return "none"
        if provider in _MODULAR_AI_PROVIDERS:
            return getattr(_MODULAR_AI_PROVIDERS[provider], "default_model", "auto")
        if provider not in PROVIDER_SPECS:
            return "none"
        if provider == "ollama":
            host = self.base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
            _, models = _probe_ollama(host)
            return _select_best_ollama_model(models)
        return PROVIDER_SPECS[provider]["default_model"]

    @property
    def display_name(self) -> str:
        if self.active_provider == "none":
            return "Heuristic Fallback (No AI Provider Configured)"
        label = PROVIDER_SPECS.get(self.active_provider, {}).get("label", self.active_provider)
        return f"{label} [{self.active_model}]"

    def generate_json(self, prompt: str, system_prompt: str = "") -> Tuple[Dict[str, Any], str, str]:
        """
        Executes prompt against active provider (with automatic failover in 'auto' mode).
        Returns (parsed_json_dict, used_provider, used_model).
        """
        if not self._chain:
            raise RuntimeError(
                "No AI provider available. Configure an API key (e.g. GEMINI_API_KEY, OPENAI_API_KEY, "
                "GROQ_API_KEY, ANTHROPIC_API_KEY), start `ollama serve`, or install `agy`."
            )

        errors = []
        for idx, prov in enumerate(self._chain):
            model = self.explicit_model if (idx == 0 and self.explicit_model) else self._resolve_model(prov)
            try:
                raw_text = self._dispatch(prov, model, prompt, system_prompt)
                parsed = extract_json_object(raw_text)
                self.active_provider = prov
                self.active_model = model
                return parsed, prov, model
            except Exception as e:
                errors.append(f"{prov}({model}): {e}")
                # Only failover if user requested 'auto'
                if self.requested_provider != "auto":
                    break

        raise RuntimeError(" | ".join(errors))

    def _dispatch(self, provider: str, model: str, prompt: str, system_prompt: str) -> str:
        if provider in _MODULAR_AI_PROVIDERS:
            mod = _MODULAR_AI_PROVIDERS[provider]
            return mod.call(
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                timeout=self.timeout,
                api_key=self.api_key,
                base_url=self.base_url,
                http_post_fn=_http_post_json,
            )
        if provider == "gemini":
            return self._call_gemini(model, prompt, system_prompt)
        if provider == "openai":
            url = (self.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/") + "/chat/completions"
            key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=True)
        if provider == "groq":
            url = "https://api.groq.com/openai/v1/chat/completions"
            key = self.api_key or os.environ.get("GROQ_API_KEY", "")
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=True)
        if provider == "deepseek":
            url = "https://api.deepseek.com/chat/completions"
            key = self.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
            json_mode = "reasoner" not in model.lower()
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=json_mode)
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            key = self.api_key or os.environ.get("OPENROUTER_API_KEY", "")
            extra_headers = {"HTTP-Referer": "https://github.com/omniforge-dev/omniforge", "X-Title": "OmniForge"}
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=False, extra_headers=extra_headers)
        if provider == "mistral":
            url = "https://api.mistral.ai/v1/chat/completions"
            key = self.api_key or os.environ.get("MISTRAL_API_KEY", "")
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=True)
        if provider == "openai-compat":
            base = (self.base_url or os.environ.get("HMI_API_BASE", "http://127.0.0.1:1234/v1")).rstrip("/")
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            key = self.api_key or os.environ.get("HMI_API_KEY", "not-needed")
            return self._call_openai_compatible(url, key, model, prompt, system_prompt, json_mode=False)
        if provider == "anthropic":
            return self._call_anthropic(model, prompt, system_prompt)
        if provider == "ollama":
            return self._call_ollama(model, prompt, system_prompt)
        if provider == "agy":
            return self._call_agy_cli(model, prompt, system_prompt)
        if provider == "claude-cli":
            return self._call_claude_cli(model, prompt, system_prompt)
        if provider == "gemini-cli":
            return self._call_gemini_cli(model, prompt, system_prompt)
        raise ValueError(f"Unsupported provider dispatch: {provider}")

    def _call_gemini(self, model: str, prompt: str, system_prompt: str) -> str:
        key = self.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("Missing GEMINI_API_KEY / GOOGLE_API_KEY")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
        payload = {
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        data = _http_post_json(url, payload, headers={}, timeout=self.timeout)
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)

    def _call_openai_compatible(
        self,
        url: str,
        api_key: str,
        model: str,
        prompt: str,
        system_prompt: str,
        json_mode: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        if not api_key:
            raise RuntimeError(f"Missing API key for {url}")
        messages = []
        sys_msg = system_prompt or "You are a precise hardware audit engine. Always respond with valid JSON only."
        messages.append({"role": "system", "content": sys_msg})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {api_key}"}
        if extra_headers:
            headers.update(extra_headers)

        data = _http_post_json(url, payload, headers=headers, timeout=self.timeout)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"API returned no choices: {data}")
        return (choices[0].get("message") or {}).get("content", "")

    def _call_anthropic(self, model: str, prompt: str, system_prompt: str) -> str:
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY")
        url = "https://api.anthropic.com/v1/messages"
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = system_prompt
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        data = _http_post_json(url, payload, headers=headers, timeout=self.timeout)
        content_blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")

    def _call_ollama(self, model: str, prompt: str, system_prompt: str) -> str:
        host = (self.base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
        url = f"{host}/api/chat"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1},
        }
        data = _http_post_json(url, payload, headers={}, timeout=max(self.timeout, 90.0), retries=0)
        return (data.get("message") or {}).get("content", "")

    def _call_agy_cli(self, model: str, prompt: str, system_prompt: str) -> str:
        bin_p = _find_agy_binary()
        if not bin_p:
            raise RuntimeError("`agy` CLI binary not found")
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
        cmd = [
            bin_p,
            "--model", model,
            "--effort", "low",
            "--disable-slash-commands",
            "-p", full_prompt,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="ignore").strip()[:200]
            raise RuntimeError(f"agy exited {proc.returncode}: {err}")
        return proc.stdout.decode("utf-8", errors="ignore").strip()

    def _call_claude_cli(self, model: str, prompt: str, system_prompt: str) -> str:
        bin_p = shutil.which("claude")
        if not bin_p:
            raise RuntimeError("`claude` CLI binary not found")
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
        cmd = [bin_p, "--model", model, "--output-format", "text", "-p", full_prompt]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="ignore").strip()[:200]
            raise RuntimeError(f"claude exited {proc.returncode}: {err}")
        return proc.stdout.decode("utf-8", errors="ignore").strip()

    def _call_gemini_cli(self, model: str, prompt: str, system_prompt: str) -> str:
        bin_p = shutil.which("gemini")
        if not bin_p:
            raise RuntimeError("`gemini` CLI binary not found")
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt
        cmd = [bin_p, "--model", model, "-p", full_prompt]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="ignore").strip()[:200]
            raise RuntimeError(f"gemini exited {proc.returncode}: {err}")
        return proc.stdout.decode("utf-8", errors="ignore").strip()


def print_providers_table():
    """Renders a diagnostic readiness table for all supported AI providers."""
    statuses = list_all_provider_statuses()
    auto_client = AIProviderClient(provider="auto")

    print("\nSupported AI Providers & Readiness Status:\n")
    headers = ["Provider", "Type", "Status", "Default Model", "Configuration / Notes"]
    widths = [14, 12, 12, 24, 42]

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    head = "│ " + " │ ".join(f"{h:<{w}}" for h, w in zip(headers, widths)) + " │"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    print(top)
    print(head)
    print(mid)
    for st in statuses:
        status_txt = "[READY]     " if st["ready"] else "[INACTIVE]  "
        status_col = f"\033[32m{status_txt}\033[0m" if st["ready"] else f"\033[90m{status_txt}\033[0m"
        prov_str = f"{st['provider']:<{widths[0]}}"
        kind_str = f"{st['kind']:<{widths[1]}}"
        mod_str = f"{st['default_model'][:widths[3]]:<{widths[3]}}"
        det_str = f"{st['detail'][:widths[4]]:<{widths[4]}}"
        print(f"│ {prov_str} │ {kind_str} │ {status_col} │ {mod_str} │ {det_str} │")
    print(bot)
    print(f"\nAuto-Selected Active Provider: \033[1m{auto_client.display_name}\033[0m")
    print("\nUsage Examples:")
    print("  python main.py gpu --ai-provider gemini --ai-model gemini-2.5-flash")
    print("  python main.py gpu --ai-provider groq --ai-model llama-3.3-70b-versatile")
    print("  python main.py gpu --ai-provider openai --ai-model gpt-4o-mini")
    print("  python main.py gpu --ai-provider anthropic --ai-model claude-3-5-haiku-latest")
    print("  python main.py gpu --ai-provider ollama --ai-model qwen2.5:7b")
    print("  python main.py gpu --ai-provider agy --ai-model claude-sonnet-5-5-low")
    print("  python main.py providers --test auto\n")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Inspect and test configured AI providers")
    parser.add_argument(
        "--test",
        nargs="?",
        const="auto",
        default=None,
        metavar="PROVIDER",
        help="Run a live JSON ping test against a provider (default: 'auto', or 'gemini', 'agy', 'ollama', etc.)",
    )
    parser.add_argument("--model", type=str, default=None, help="Optional model override for --test")
    parser.add_argument("--base-url", type=str, default=None, help="Optional custom endpoint URL for --test")
    args = parser.parse_args(argv)

    print_providers_table()

    if args.test:
        print(f"Testing live JSON inference with provider='{args.test}'...")
        t0 = time.perf_counter()
        try:
            client = AIProviderClient(provider=args.test, model=args.model, base_url=args.base_url, timeout=30.0)
            res, used_p, used_m = client.generate_json(
                'Return ONLY valid JSON: {"status": "ok", "engine": "omniforge"}'
            )
            dt = time.perf_counter() - t0
            print(f"Success in {dt:.2f}s via {used_p} [{used_m}]: {json.dumps(res)}")
        except Exception as e:
            print(f"Error: Test failed: {e}")


if __name__ == "__main__":
    main()
