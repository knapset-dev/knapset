"""
Auto-Discovery Registry for Pluggable Hardware Component Analysers & Spec Databases.

Dynamically scans and imports modules inside `component_analyser/modules/`.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Type

from app_paths import collect_module_candidates, get_subsystem_dirs
from component_analyser.base import BaseComponentAnalyser


class ComponentAnalyserRegistry:
    """Discovers, tracks, and invokes category component specification analysers."""

    def __init__(self, modules_dir: Optional[Path] = None):
        self._explicit_dir = modules_dir is not None
        self._modules_dir = modules_dir or (Path(__file__).resolve().parent / "modules")
        self._analysers: Dict[str, BaseComponentAnalyser] = {}
        self._analyser_classes: Dict[str, Type[BaseComponentAnalyser]] = {}
        self._module_files: Dict[str, Path] = {}
        self._errors: Dict[str, str] = {}
        self._aliases: Dict[str, str] = {
            "videokarty": "gpu", "gpus": "gpu",
            "protsessory": "cpu", "cpus": "cpu",
            "motherboards": "mobo", "materinskie-platy": "mobo", "motherboard": "mobo",
            "memory": "ram", "rams": "ram",
            "bloki-pitaniya": "psu", "psus": "psu", "power_supply": "psu",
        }
        self.discover()

    def discover(self, clear: bool = False) -> None:
        """Discovers and instantiates all BaseComponentAnalyser subclasses in modules_dir."""
        if clear:
            self._analysers.clear()
            self._analyser_classes.clear()
            self._module_files.clear()
            self._errors.clear()

        dirs = [self._modules_dir] if self._explicit_dir else get_subsystem_dirs("component_analyser/modules")
        candidates = collect_module_candidates(dirs)

        for mod_stem, entry_file, pkg_dir in candidates:
            mod_name = f"component_analyser.modules.{mod_stem}"
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
                    if issubclass(obj, BaseComponentAnalyser) and obj is not BaseComponentAnalyser:
                        cat_name = getattr(obj, "name", "").strip().lower()
                        if cat_name and cat_name not in ("base", "template", "custom_template"):
                            self._analyser_classes[cat_name] = obj
                            self._module_files[cat_name] = pkg_dir if pkg_dir else entry_file
                            cat_attr = getattr(obj, "category", "")
                            if isinstance(cat_attr, str) and cat_attr.strip():
                                self._aliases.setdefault(cat_attr.strip().lower(), cat_name)
                            for a in getattr(obj, "aliases", []):
                                if isinstance(a, str) and a.strip():
                                    self._aliases[a.strip().lower()] = cat_name
                            for t in getattr(obj, "tags", []):
                                if isinstance(t, str) and t.strip():
                                    self._aliases.setdefault(t.strip().lower(), cat_name)
                            try:
                                self._analysers[cat_name] = obj()
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
        """Returns the file path from which the component analyser was loaded."""
        return self._module_files.get((name or "").strip().lower())

    def register(self, analyser: BaseComponentAnalyser) -> None:
        """Manually registers an instance of BaseComponentAnalyser."""
        cat_name = analyser.name.lower()
        self._analysers[cat_name] = analyser
        self._analyser_classes[cat_name] = analyser.__class__
        cat_attr = getattr(analyser, "category", "")
        if isinstance(cat_attr, str) and cat_attr.strip():
            self._aliases.setdefault(cat_attr.strip().lower(), cat_name)
        for a in getattr(analyser, "aliases", []):
            if isinstance(a, str) and a.strip():
                self._aliases[a.strip().lower()] = cat_name
        for t in getattr(analyser, "tags", []):
            if isinstance(t, str) and t.strip():
                self._aliases.setdefault(t.strip().lower(), cat_name)

    def get(self, name: str) -> Optional[BaseComponentAnalyser]:
        """Resolves an analyser by category name (e.g. 'gpu', 'cpu', 'psu', or custom domain alias)."""
        clean = (name or "").strip().lower()
        clean = self._aliases.get(clean, clean)
        return self._analysers.get(clean)

    def list_analysers(self) -> Dict[str, BaseComponentAnalyser]:
        """Returns all registered component analysers."""
        return dict(self._analysers)

    def identify(
        self,
        category: str,
        title: str,
        description: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Identifies hardware specs using the matching category analyser."""
        analyser = self.get(category)
        if analyser:
            try:
                return analyser.identify(title=title, description=description)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning("Component analyser '%s' crashed: %s", category, e)
                return None
        return None


# Global default instance
_DEFAULT_REGISTRY: Optional[ComponentAnalyserRegistry] = None


def get_component_registry() -> ComponentAnalyserRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ComponentAnalyserRegistry()
    return _DEFAULT_REGISTRY
