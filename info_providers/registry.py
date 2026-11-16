"""
Auto-Discovery Registry for Pluggable Market Information Providers.

Dynamically scans and imports modules inside `info_providers/modules/`.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Type

from app_paths import collect_module_candidates, get_subsystem_dirs
from info_providers.base import BaseInfoProvider, NormalizedListing


class InfoProviderRegistry:
    """Discovers, tracks, and instantiates marketplace information providers."""

    def __init__(self, modules_dir: Optional[Path] = None):
        self._explicit_dir = modules_dir is not None
        self._modules_dir = modules_dir or (Path(__file__).resolve().parent / "modules")
        self._providers: Dict[str, BaseInfoProvider] = {}
        self._provider_classes: Dict[str, Type[BaseInfoProvider]] = {}
        self._module_files: Dict[str, Path] = {}
        self._errors: Dict[str, str] = {}
        self._aliases: Dict[str, str] = {}
        self.discover()

    def discover(self, clear: bool = False) -> None:
        """Discovers and instantiates all BaseInfoProvider subclasses in modules_dir."""
        if clear:
            self._providers.clear()
            self._provider_classes.clear()
            self._module_files.clear()
            self._errors.clear()

        dirs = [self._modules_dir] if self._explicit_dir else get_subsystem_dirs("info_providers/modules")
        candidates = collect_module_candidates(dirs)

        for mod_stem, entry_file, pkg_dir in candidates:
            mod_name = f"info_providers.modules.{mod_stem}"
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
                    if issubclass(obj, BaseInfoProvider) and obj is not BaseInfoProvider:
                        prov_name = getattr(obj, "name", "").strip().lower()
                        if prov_name and prov_name not in ("base", "template", "template_example", "custom_template"):
                            self._provider_classes[prov_name] = obj
                            self._module_files[prov_name] = pkg_dir if pkg_dir else entry_file
                            for a in getattr(obj, "aliases", []):
                                if isinstance(a, str) and a.strip():
                                    self._aliases[a.strip().lower()] = prov_name
                            for t in getattr(obj, "tags", []):
                                if isinstance(t, str) and t.strip():
                                    self._aliases.setdefault(t.strip().lower(), prov_name)
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
        """Returns the file path from which the provider was loaded."""
        return self._module_files.get((name or "").strip().lower())

    def register(self, provider: BaseInfoProvider) -> None:
        """Manually registers an instance of BaseInfoProvider."""
        p_name = provider.name.lower()
        self._providers[p_name] = provider
        self._provider_classes[p_name] = provider.__class__
        for a in getattr(provider, "aliases", []):
            if isinstance(a, str) and a.strip():
                self._aliases[a.strip().lower()] = p_name
        for t in getattr(provider, "tags", []):
            if isinstance(t, str) and t.strip():
                self._aliases.setdefault(t.strip().lower(), p_name)

    def get(self, name: str) -> Optional[BaseInfoProvider]:
        """Resolves a provider by name (e.g. 'olx', 'ebay', 'blocket', or dynamic alias)."""
        clean = (name or "").strip().lower()
        clean = self._aliases.get(clean, clean)
        if clean in self._providers:
            return self._providers[clean]
        # Check if domain matches
        for prov in self._providers.values():
            if clean in prov.source_domain.lower() or prov.source_domain.lower() in clean:
                return prov
        return None

    def list_providers(self) -> Dict[str, BaseInfoProvider]:
        """Returns all registered providers."""
        return dict(self._providers)

    def adapt_any(self, raw_data: Dict[str, Any], source_hint: str = "") -> Dict[str, Any]:
        """
        Routes raw_data to the matching provider module, or applies universal fallbacks.
        Isolates third-party provider runtime exceptions.
        """
        src = str(raw_data.get("source") or source_hint or "").lower()
        provider = self.get(src)
        if provider:
            try:
                return provider.adapt_item(raw_data).to_dict()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Provider '%s' failed to adapt item: %s. Falling back to universal adapter.", src, e)

        # Universal fallback using ProviderFeatureAdapter if available
        try:
            from provider_template import ProviderFeatureAdapter
            return ProviderFeatureAdapter.adapt_item(raw_data, source_hint=source_hint)
        except Exception:
            # Bare minimum fallback
            return NormalizedListing(
                id=str(raw_data.get("id", "")),
                title=str(raw_data.get("title", "")),
                price=float(raw_data.get("price", 0.0)),
                source=src,
                raw=raw_data,
            ).to_dict()


# Global default instance
_DEFAULT_REGISTRY: Optional[InfoProviderRegistry] = None


def get_info_registry() -> InfoProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = InfoProviderRegistry()
    return _DEFAULT_REGISTRY
