"""
Base Interface & Data Models for Pluggable Build & Set Composers.

Modules living in `build_composer/modules/` implement `BaseBuildComposer`.
They perform combinatorial synthesis across candidate pools under budget and weight profiles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ComposerSetResult:
    """Universal representation of a synthesized multi-component set or PC build."""
    domain: str
    mode: str
    total_cost: float
    budget: float
    surplus: float
    score: float
    components: Dict[str, Dict[str, Any]]
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "mode": self.mode,
            "total_cost": self.total_cost,
            "budget": self.budget,
            "surplus": self.surplus,
            "score": self.score,
            "components": self.components,
            "metrics": self.metrics,
        }


class BaseBuildComposer(ABC):
    """
    Abstract contract for all combinatorial build/set synthesizers.
    
    To implement a new composer (e.g. homelab, audio studio, keyboard):
    1. Create a new file in `build_composer/modules/<domain>.py`
    2. Subclass `BaseBuildComposer`
    3. Define `name`, `title`, and `description`
    4. Implement `compose(candidate_pool, budget, mode, weights, top_n)`
    """
    name: str = "base"
    title: str = "Base Composer"
    domain: str = "general"
    aliases: List[str] = []
    description: str = ""
    version: str = "1.0.0"
    author: str = "Core Team"
    min_app_version: str = "1.0.0"
    category: str = "synthesis"
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
            "category": getattr(self, "category", "synthesis"),
            "tags": list(getattr(self, "tags", [])),
            "url": getattr(self, "url", ""),
        }

    @abstractmethod
    def compose(
        self,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        mode: str = "best",
        weights: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[ComposerSetResult]:
        """
        Synthesizes up to top_n optimal sets within budget according to domain rules.
        """
        pass
