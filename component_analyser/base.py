"""
Base Interface & Data Models for Pluggable Component Analysers & Spec Databases.

Modules living in `component_analyser/modules/` implement `BaseComponentAnalyser`.
They identify specific hardware models, extract specifications, and calculate performance metrics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseComponentAnalyser(ABC):
    """
    Abstract contract for component specification identification and analysis.
    
    To implement a new component analyser (e.g. SSD, cooler, case, monitor):
    1. Create a new file in `component_analyser/modules/<category>.py`
    2. Subclass `BaseComponentAnalyser`
    3. Define `name` and `title`
    4. Implement `identify(...)`
    """
    name: str = "base"
    title: str = "Base Component Analyser"
    domain: str = "general"
    aliases: List[str] = []
    description: str = ""
    version: str = "1.0.0"
    author: str = "Core Team"
    min_app_version: str = "1.0.0"
    category: str = "hardware_spec"
    tags: List[str] = []
    url: str = ""

    def get_manifest(self) -> Dict[str, Any]:
        """Returns standardized metadata manifest for Module Marketplace."""
        return {
            "name": getattr(self, "name", "unknown"),
            "title": getattr(self, "title", self.name.capitalize()),
            "domain": getattr(self, "domain", "general"),
            "aliases": list(getattr(self, "aliases", [])),
            "version": getattr(self, "version", "1.0.0"),
            "author": getattr(self, "author", "Community"),
            "description": getattr(self, "description", ""),
            "min_app_version": getattr(self, "min_app_version", "1.0.0"),
            "category": getattr(self, "category", "hardware_spec"),
            "tags": list(getattr(self, "tags", [])),
            "url": getattr(self, "url", ""),
        }

    @abstractmethod
    def identify(self, title: str, description: str = "") -> Optional[Dict[str, Any]]:
        """
        Parses title and description to identify hardware model and specifications.
        Returns a dictionary of specs (model, performance, tdp, etc.) or None if unrecognized.
        """
        pass
