"""
Base Interface & Data Models for Pluggable Scam & Defect Analysis Modules.

Modules living in `scam_analyser/modules/` implement `BaseScamAnalyser`.
They are completely self-contained and perform defect/scam detection for specific categories.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScamAnalysisResult:
    """Universal defect & risk analysis result."""
    category: str
    risk_score: int = 0
    red_flags: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    trust_signals: List[str] = field(default_factory=list)
    is_defective: bool = False
    is_ai_approved: bool = False
    is_ai_rejected: bool = False
    audit: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "risk_score": self.risk_score,
            "red_flags": self.red_flags,
            "warnings": self.warnings,
            "trust_signals": self.trust_signals,
            "is_defective": self.is_defective or len(self.red_flags) > 0,
            "is_ai_approved": self.is_ai_approved,
            "is_ai_rejected": self.is_ai_rejected,
            "audit": self.audit,
        }


class BaseScamAnalyser(ABC):
    """
    Abstract contract for all category scam & defect analyzers.
    
    To implement a new defect analyser (e.g. for laptops, monitors, smartphones, audio):
    1. Create a new file in `scam_analyser/modules/<category>.py`
    2. Subclass `BaseScamAnalyser`
    3. Define `name` (category ID) and `title`
    4. Implement `analyze(item, detail, spec)`
    """
    name: str = "base"
    title: str = "Base Scam Analyser"
    domain: str = "general"
    aliases: List[str] = []
    description: str = ""
    version: str = "1.0.0"
    author: str = "Core Team"
    min_app_version: str = "1.0.0"
    category: str = "defect_analysis"
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
            "category": getattr(self, "category", "defect_analysis"),
            "tags": list(getattr(self, "tags", [])),
            "url": getattr(self, "url", ""),
        }

    @abstractmethod
    def analyze(
        self,
        item: Dict[str, Any],
        detail: Optional[Dict[str, Any]] = None,
        spec: Optional[Dict[str, Any]] = None,
    ) -> ScamAnalysisResult:
        """
        Analyzes title, description, price, and seller metrics.
        Returns a ScamAnalysisResult with risk_score and flagged defects.
        """
        pass
