"""
DEVELOPER BLUEPRINT: How to Create a New Component Hardware Analyser Module.

This blueprint demonstrates how to create hardware specification extractors and
performance benchmarking engines (e.g., for NVMe SSDs, CPU Coolers, Cases, Monitors,
or Networking Cards) in the `omniforge` ecosystem.

================================================================================
STEP-BY-STEP INTEGRATION GUIDE:
================================================================================
1. Copy this file into `component_analyser/modules/<category_slug>.py`
   (e.g., `component_analyser/modules/ssd.py` or `component_analyser/modules/cooler.py`).
2. Update the class name and class attributes:
   - `name`: Category identifier matching the layout section (e.g. "ssd", "cooler", "case").
   - `title`: Human-readable display title (e.g. "Solid State Drive (SSD) Analyser").
   - `version`, `author`, `description`, `tags`, `url`: Module Marketplace metadata.
3. Implement `identify(title: str, description: str = "") -> Optional[Dict[str, Any]]`:
   - Parse hardware generation, capacity, interface, and form factors.
   - Return normalized specification dictionary or `None` if unidentifiable.
4. Zero configuration required! The registry automatically discovers your module and
   uses it in search pipelines, audit feeds, and compatibility checks.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from component_analyser.base import BaseComponentAnalyser


class CustomComponentAnalyser(BaseComponentAnalyser):
    """
    Independent hardware component spec analyser and model identifier.
    """

    # --- Marketplace Manifest Metadata (Marketplace Ready) ---
    name: str = "custom_template"
    title: str = "Custom Component Analyser Blueprint"
    domain: str = "general"
    version: str = "1.0.0"
    author: str = "Community Contributor"
    min_app_version: str = "1.0.0"
    category: str = "hardware_spec"
    tags: List[str] = ["hardware", "specs", "database", "template"]
    url: str = "https://github.com/omniforge-dev/omniforge"
    description: str = "Exemplary blueprint for building isolated hardware specification identification modules."

    # Pre-compiled regex patterns for model extraction
    _MODEL_PATTERNS = [
        (re.compile(r"\b(?:samsung\s*990\s*pro|990\s*pro)\b", re.IGNORECASE), "Samsung 990 Pro", 7450, "PCIe 4.0 x4"),
        (re.compile(r"\b(?:samsung\s*980\s*pro|980\s*pro)\b", re.IGNORECASE), "Samsung 980 Pro", 7000, "PCIe 4.0 x4"),
        (re.compile(r"\b(?:kingston\s*kc3000|kc3000)\b", re.IGNORECASE), "Kingston KC3000", 7000, "PCIe 4.0 x4"),
        (re.compile(r"\b(?:wd\s*black\s*sn850x|sn850x)\b", re.IGNORECASE), "WD Black SN850X", 7300, "PCIe 4.0 x4"),
        (re.compile(r"\b(?:crucial\s*p3\s*plus|p3\s*plus)\b", re.IGNORECASE), "Crucial P3 Plus", 5000, "PCIe 4.0 x4"),
        (re.compile(r"\b(?:crucial\s*p3\b|p3\b)\b", re.IGNORECASE), "Crucial P3", 3500, "PCIe 3.0 x4"),
    ]

    _CAPACITY_RE = re.compile(r"\b(500\s*gb|512\s*gb|1\s*tb|2\s*tb|4\s*tb)\b", re.IGNORECASE)

    def identify(self, title: str, description: str = "") -> Optional[Dict[str, Any]]:
        """
        Parses title and description to extract hardware specs and benchmark metrics.

        Arguments:
            title: Listing headline string.
            description: Seller ad description.

        Returns:
            Dictionary with normalized hardware specs or None if the item is unrecognized.
        """
        text = f"{title} {description}"

        model_name = None
        read_speed_mb = 3000
        interface = "PCIe 3.0 x4"

        for pattern, m_name, speed, iface in self._MODEL_PATTERNS:
            if pattern.search(text):
                model_name = m_name
                read_speed_mb = speed
                interface = iface
                break

        if not model_name:
            return None

        # Extract capacity
        cap_match = self._CAPACITY_RE.search(text)
        capacity_str = cap_match.group(1).upper().replace(" ", "") if cap_match else "1TB"
        capacity_gb = 500 if "500" in capacity_str or "512" in capacity_str else (
            2000 if "2TB" in capacity_str else (4000 if "4TB" in capacity_str else 1000)
        )

        return {
            "model": f"{model_name} {capacity_str}",
            "base_model": model_name,
            "category": self.name,
            "capacity_gb": capacity_gb,
            "read_speed_mb": read_speed_mb,
            "interface": interface,
            "form_factor": "M.2 2280 NVMe",
            "market_used_median": 2200.0 if capacity_gb == 1000 else (3800.0 if capacity_gb >= 2000 else 1400.0),
        }
