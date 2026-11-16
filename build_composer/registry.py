"""
Auto-Discovery Registry for Pluggable Build & Set Composers.

Dynamically scans and imports modules inside `build_composer/modules/`.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Type

from app_paths import collect_module_candidates, get_subsystem_dirs
from build_composer.base import BaseBuildComposer, ComposerSetResult


class BuildComposerRegistry:
    """Discovers, tracks, and invokes domain build composers."""

    def __init__(self, modules_dir: Optional[Path] = None):
        self._explicit_dir = modules_dir is not None
        self._modules_dir = modules_dir or (Path(__file__).resolve().parent / "modules")
        self._composers: Dict[str, BaseBuildComposer] = {}
        self._composer_classes: Dict[str, Type[BaseBuildComposer]] = {}
        self._module_files: Dict[str, Path] = {}
        self._errors: Dict[str, str] = {}
        self._aliases: Dict[str, str] = {
            "desktop": "pc",
            "computer": "pc",
            "pc_build": "pc",
            "pc_builder": "pc",
            "pc:builder": "pc",
            "builder": "pc",
        }
        self.discover()

    def discover(self, clear: bool = False) -> None:
        """Discovers and instantiates all BaseBuildComposer subclasses in modules_dir."""
        if clear:
            self._composers.clear()
            self._composer_classes.clear()
            self._module_files.clear()
            self._errors.clear()

        dirs = [self._modules_dir] if self._explicit_dir else get_subsystem_dirs("build_composer/modules")
        candidates = collect_module_candidates(dirs)

        for mod_stem, entry_file, pkg_dir in candidates:
            mod_name = f"build_composer.modules.{mod_stem}"
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
                    if issubclass(obj, BaseBuildComposer) and obj is not BaseBuildComposer:
                        domain_name = getattr(obj, "name", "").strip().lower()
                        if domain_name and domain_name not in ("base", "template", "custom_template"):
                            self._composer_classes[domain_name] = obj
                            self._module_files[domain_name] = pkg_dir if pkg_dir else entry_file
                            for a in getattr(obj, "aliases", []):
                                if isinstance(a, str) and a.strip():
                                    self._aliases[a.strip().lower()] = domain_name
                            try:
                                self._composers[domain_name] = obj()
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
        """Returns the file path from which the composer was loaded."""
        return self._module_files.get((name or "").strip().lower())

    def register(self, composer: BaseBuildComposer) -> None:
        """Manually registers an instance of BaseBuildComposer."""
        domain_name = composer.name.lower()
        self._composers[domain_name] = composer
        self._composer_classes[domain_name] = composer.__class__
        for a in getattr(composer, "aliases", []):
            if isinstance(a, str) and a.strip():
                self._aliases[a.strip().lower()] = domain_name

    def get(self, name: str) -> Optional[BaseBuildComposer]:
        """Resolves a composer by domain name (e.g. 'pc', 'fishing', or custom domain alias)."""
        clean = (name or "").strip().lower()
        clean = self._aliases.get(clean, clean)
        return self._composers.get(clean)

    def list_composers(self) -> Dict[str, BaseBuildComposer]:
        """Returns all registered composers."""
        return dict(self._composers)

    def compose(
        self,
        domain: str,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        mode: str = "best",
        weights: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[ComposerSetResult]:
        """Synthesizes sets using the matching domain composer."""
        composer = self.get(domain)
        if not composer:
            raise ValueError(f"Unknown build composer domain '{domain}'. Registered: {list(self._composers.keys())}")
        try:
            return composer.compose(
                candidate_pool=candidate_pool,
                budget=budget,
                mode=mode,
                weights=weights,
                top_n=top_n,
                **kwargs,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Build composer '%s' crashed: %s", domain, e)
            return []


# Global default instance
_DEFAULT_REGISTRY: Optional[BuildComposerRegistry] = None


def get_composer_registry() -> BuildComposerRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = BuildComposerRegistry()
    return _DEFAULT_REGISTRY
