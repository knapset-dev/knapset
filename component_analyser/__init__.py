"""
Component Analyser Subsystem.

Pluggable hardware specification extractors and database matchers.
New component modules are placed inside `component_analyser/modules/` and automatically discovered.
"""

from component_analyser.base import BaseComponentAnalyser
from component_analyser.registry import ComponentAnalyserRegistry, get_component_registry

__all__ = [
    "BaseComponentAnalyser",
    "ComponentAnalyserRegistry",
    "get_component_registry",
]
