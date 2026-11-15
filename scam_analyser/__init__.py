"""
Pluggable Scam & Defect Analysis Subsystem.

Houses modular category defect detectors in `scam_analyser/modules/`.
"""

from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult
from scam_analyser.registry import ScamAnalyserRegistry, get_scam_registry

__all__ = [
    "BaseScamAnalyser",
    "ScamAnalysisResult",
    "ScamAnalyserRegistry",
    "get_scam_registry",
]
