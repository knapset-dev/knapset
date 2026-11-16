"""
Auto-Discovery Registry for Pluggable AI Backend Providers.

Dynamically scans and imports modules inside `ai_providers/modules/`.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple, Type

from ai_providers.base import BaseAIProvider
from app_paths import collect_module_candidates, get_subsystem_dirs


class AIProviderRegistry:
    """Discovers, tracks, and selects AI providers and local coding agents."""

    def __init__(self, modules_dir: Optional[Path] = None):
        self._explicit_dir = modules_dir is not None
        self._modules_dir = modules_dir or (Path(__file__).resolve().parent / "modules")
        self._providers: Dict[str, BaseAIProvider] = {}
        self._provider_classes: Dict[str, Type[BaseAIProvider]] = {}
        self._module_files: Dict[str, Path] = {}
        self._errors: Dict[str, str] = {}
        self._aliases: Dict[str, str] = {}
        self.discover()

    def discover(self, clear: bool = False) -> None:
        """Discovers and instantiates all BaseAIProvider subclasses in modules_dir."""
        if clear:
            self._providers.clear()
            self._provider_classes.clear()
            self._module_files.clear()
            self._errors.clear()
            self._aliases.clear()

        dirs = [self._modules_dir] if self._explicit_dir else get_subsystem_dirs("ai_providers/modules")
        candidates = collect_module_candidates(dirs)

        for mod_stem, entry_file, pkg_dir in candidates:
            mod_name = f"ai_providers.modules.{mod_stem}"
            err_key = pkg_dir.name if pkg_dir else entry_file.name
            try:
                submodule_search = [str(pkg_dir)] if pkg_dir else None
                spec = importlib.util.spec_from_file_location(
                    mod_name, entry_file, submodule_search_locations=submodule_search
                )
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)

                for _, obj in inspect.getmembers(mod, inspect.isclass):
                    if issubclass(obj, BaseAIProvider) and obj is not BaseAIProvider:
                        prov_name = getattr(obj, "name", "").strip().lower()
                        if prov_name and prov_name not in ("base", "template", "custom_ai_template"):
                            self._provider_classes[prov_name] = obj
                            self._module_files[prov_name] = pkg_dir if pkg_dir else entry_file
                            for a in getattr(obj, "aliases", []):
                                if isinstance(a, str) and a.strip():
                                    self._aliases[a.strip().lower()] = prov_name
                            try:
                                self._providers[prov_name] = obj()
                            except Exception as inst_err:
                                self._errors[err_key] = f"Instantiation error ({obj.__name__}): {inst_err}"
            except Exception as e:
                sys.modules.pop(mod_name, None)
                self._errors[err_key] = f"{type(e).__name__}: {e}"
                continue

    def get_errors(self) -> Dict[str, str]:
        """Returns any loading errors encountered during plugin discovery."""
        return dict(self._errors)

    def get_module_file(self, name: str) -> Optional[Path]:
        """Returns the file path from which the AI provider was loaded."""
        return self._module_files.get((name or "").strip().lower())

    def register(self, provider: BaseAIProvider) -> None:
        """Manually registers an instance of BaseAIProvider."""
        p_name = provider.name.lower()
        self._providers[p_name] = provider
        self._provider_classes[p_name] = provider.__class__
        for a in getattr(provider, "aliases", []):
            if isinstance(a, str) and a.strip():
                self._aliases[a.strip().lower()] = p_name

    def get(self, name: str) -> Optional[BaseAIProvider]:
        """Resolves an AI provider by name (e.g. 'gemini', 'agy', 'groq', 'cohere' or dynamic alias)."""
        clean = (name or "").strip().lower()
        clean = self._aliases.get(clean, clean)
        if clean in ("claude", "anthropic-claude"):
            clean = "anthropic"
        elif clean in ("gpt", "openai-gpt"):
            clean = "openai"
        return self._providers.get(clean)

    def list_providers(self) -> Dict[str, BaseAIProvider]:
        """Returns all registered AI providers."""
        return dict(self._providers)

    def get_ready_providers(self) -> List[str]:
        """Returns a list of provider names that are currently active and ready."""
        ready = []
        for name, prov in self._providers.items():
            ok, _ = prov.is_ready()
            if ok:
                ready.append(name)
        return ready

    def auto_select(self) -> Optional[BaseAIProvider]:
        """Automatically picks the highest-priority ready provider."""
        # Priority order: local agent (agy) -> fast cloud (groq) -> gemini -> openai -> others
        priority = ["agy", "groq", "gemini", "openai", "anthropic", "deepseek", "cohere", "together", "ollama"]
        for p in priority:
            prov = self.get(p)
            if prov:
                ok, _ = prov.is_ready()
                if ok:
                    return prov
        # Fallback to any ready provider
        for prov in self._providers.values():
            ok, _ = prov.is_ready()
            if ok:
                return prov
        return None


# Global default instance
_DEFAULT_REGISTRY: Optional[AIProviderRegistry] = None


def get_ai_registry() -> AIProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = AIProviderRegistry()
    return _DEFAULT_REGISTRY
