"""
DEVELOPER BLUEPRINT: How to Create a New Category Scam & Defect Analyser Module.

This blueprint demonstrates how to create category-specific defect and fraud detectors
(e.g., for Laptops, Monitors, NVMe SSDs, Handheld Consoles, Audio Equipment, or Drones)
in the `omniforge` ecosystem.

================================================================================
STEP-BY-STEP INTEGRATION GUIDE:
================================================================================
1. Copy this file into `scam_analyser/modules/<category_slug>.py`
   (e.g., `scam_analyser/modules/laptop.py` or `scam_analyser/modules/monitor.py`).
2. Update the class name and class attributes:
   - `name`: Category identifier matching the layout section (e.g. "laptop", "monitor", "ssd").
   - `title`: Human-readable display title (e.g. "Laptop Hardware Defect Analyser").
   - `version`, `author`, `description`, `tags`, `url`: Module Marketplace metadata.
3. Define pre-compiled regex triggers for hardware-specific defects:
   - Display matrix defects (dead pixels, lines, back-light bleeding).
   - Thermal / motherboard issues (reballed chips, liquid damage, battery degradation).
   - Prepayment or evasion traps.
4. Implement `analyze(item, detail=None, spec=None) -> ScamAnalysisResult`:
   Return risk score (0-100), red flags (hard disqualifiers), warnings, and trust signals.
5. Zero configuration required! The registry dynamically loads your module and binds
   it to audit pipelines and interactive reviews.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult


class CustomCategoryScamAnalyser(BaseScamAnalyser):
    """
    Independent defect and risk analyser for a specific hardware category.
    """

    # --- Marketplace Manifest Metadata (Marketplace Ready) ---
    name: str = "custom_template"
    title: str = "Custom Category Scam & Defect Blueprint"
    domain: str = "general"
    version: str = "1.0.0"
    author: str = "Community Contributor"
    min_app_version: str = "1.0.0"
    category: str = "defect_analysis"
    tags: List[str] = ["security", "defect_detection", "template"]
    url: str = "https://github.com/omniforge-dev/omniforge"
    description: str = "Exemplary blueprint for building isolated category defect and scam detection modules."

    # Pre-compiled regex triggers for optimal microsecond execution
    _CRITICAL_DEFECT_PATTERNS = [
        (re.compile(r"\b(?:битий\s+екран|розбита\s+матриця|полосы\s+на\s+экране|смуги\s+на\s+екрані)\b", re.IGNORECASE), "Broken display panel / matrix damage"),
        (re.compile(r"\b(?:залитий|залита|після\s+води|коррози\w*|окислени\w*)\b", re.IGNORECASE), "Liquid damage / corrosion disclosed"),
        (re.compile(r"\b(?:не\s*вмикається|не\s*робоч\w*|не\s*працює|на\s+запчастини)\b", re.IGNORECASE), "Non-functional hardware sold for parts"),
        (re.compile(r"\b(?:пароль\s+в\s+біос\w*|заблокований|mdm\s*lock|icloud\s*lock)\b", re.IGNORECASE), "Firmware / BIOS / account lock trap"),
    ]

    _WARNING_PATTERNS = [
        (re.compile(r"\b(?:акумулятор\s+(?:помер|не\s+тримає)|знос\s+(?:батареї|акб)\s*(?:80|90|100)?%?)\b", re.IGNORECASE), "Severe battery degradation"),
        (re.compile(r"\b(?:гріється|шумить|гуде\s+кулер|тротлить)\b", re.IGNORECASE), "Thermal throttling or worn cooling fan"),
        (re.compile(r"\b(?:немає\s+зарядки|без\s+бп|без\s+зарядного)\b", re.IGNORECASE), "Missing charger or power supply brick"),
    ]

    _TRUST_PATTERNS = [
        (re.compile(r"\b(?:повний\s+комплект|рідна\s+коробка|чек|офіційна\s+гарантія)\b", re.IGNORECASE), "Complete factory retail kit / proof of purchase"),
        (re.compile(r"\b(?:будь-які\s+тести|згоден\s+на\s+перевірку|тести\s+в\s+aida64|crystaldiskmark)\b", re.IGNORECASE), "Benchmark diagnostic verification offered"),
    ]

    def analyze(
        self,
        item: Dict[str, Any],
        detail: Optional[Dict[str, Any]] = None,
        spec: Optional[Dict[str, Any]] = None,
    ) -> ScamAnalysisResult:
        """
        Executes heuristic rule analysis on title, seller description, and pricing.

        Arguments:
            item: Summary listing dictionary (id, title, price, seller metrics).
            detail: Detailed listing data (full description, photo count, seller account date).
            spec: Normalized hardware specifications (TDP, generation, socket, median price).

        Returns:
            ScamAnalysisResult containing risk_score, red_flags, warnings, and trust_signals.
        """
        title = str(item.get("title") or "")
        det = detail or item.get("detail") or item.get("raw") or {}
        description = str(item.get("description") or det.get("description") or "")
        text = f"{title}\n{description}"

        risk_score = 0
        red_flags: List[str] = []
        warnings: List[str] = []
        trust_signals: List[str] = []

        # 1. Evaluate Critical Defect Triggers (Hard Disqualifiers)
        for pattern, reason in self._CRITICAL_DEFECT_PATTERNS:
            if pattern.search(text):
                risk_score += 45
                red_flags.append(f"CRITICAL DEFECT: {reason}")

        # 2. Evaluate Moderate Warnings
        for pattern, reason in self._WARNING_PATTERNS:
            if pattern.search(text):
                risk_score += 15
                warnings.append(f"Hardware concern: {reason}")

        # 3. Evaluate Trust Signals
        for pattern, reason in self._TRUST_PATTERNS:
            if pattern.search(text):
                risk_score -= 10
                trust_signals.append(f"Trust signal: {reason}")

        # 4. Delivery & Escrow Refusal Trap
        if re.search(r"\b(?:тільки\s+самовивіз|лише\s+передплата|только\s+предоплата)\b", text, re.IGNORECASE):
            risk_score += 35
            red_flags.append("Prepayment trap / delivery refusal")

        # Clamp risk score to [0, 100]
        risk_score = max(0, min(100, risk_score))

        return ScamAnalysisResult(
            category=self.name,
            risk_score=risk_score,
            red_flags=red_flags,
            warnings=warnings,
            trust_signals=trust_signals,
            is_defective=len(red_flags) > 0,
        )
