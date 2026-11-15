"""
Unit Tests for the Fully Decoupled Modular Subsystems Architecture:
  1. info_providers/modules      (Pluggable Market Info Providers)
  2. scam_analyser/modules       (Pluggable Defect & Scam Analysers)
  3. build_composer/modules      (Pluggable Set & Build Composers)
  4. ai_providers/modules        (Pluggable AI Backend Providers)
  5. component_analyser/modules  (Pluggable Component Spec Analysers)
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from info_providers import get_info_registry, BaseInfoProvider
from scam_analyser import get_scam_registry, BaseScamAnalyser, ScamAnalysisResult
from build_composer import get_composer_registry, BaseBuildComposer, ComposerSetResult
from ai_providers import get_ai_registry, BaseAIProvider
from component_analyser import get_component_registry, BaseComponentAnalyser


class TestModularArchitecture(unittest.TestCase):
    def test_subsystem_directory_structure(self):
        """Verifies that all 5 canonical underscore subsystem directories exist without hyphenated duplicates."""
        subsystems = [
            ("info-providers", "info_providers"),
            ("scam-analyser", "scam_analyser"),
            ("build-composer", "build_composer"),
            ("ai-providers", "ai_providers"),
            ("component-analyser", "component_analyser"),
        ]
        for hyphen_name, pkg_dir in subsystems:
            p = REPO_ROOT / pkg_dir
            self.assertTrue(p.is_dir(), f"Canonical directory '{pkg_dir}' must exist")
            self.assertTrue((p / "modules").is_dir(), f"'{pkg_dir}/modules' directory must exist")
            self.assertFalse((REPO_ROOT / hyphen_name).exists(), f"Legacy hyphenated path '{hyphen_name}' should not exist")

    def test_info_providers_registry(self):
        reg = get_info_registry()
        providers = reg.list_providers()
        # Verify built-in modules are auto-discovered
        self.assertIn("olx", providers)
        self.assertIn("ebay", providers)
        self.assertIn("telemart", providers)
        self.assertIn("hotline", providers)
        self.assertIn("blocket", providers)

        olx = reg.get("olx")
        self.assertIsNotNone(olx)
        self.assertEqual(olx.name, "olx")
        self.assertEqual(olx.default_currency, "UAH")

    def test_scam_analyser_registry(self):
        reg = get_scam_registry()

        class MockDefectAnalyser(BaseScamAnalyser):
            name = "drone"
            def analyze(self, item, detail=None, spec=None):
                desc = (item.get("description") or "").lower()
                flags = ["Crashed motor disclosed"] if "crashed" in desc else []
                return ScamAnalysisResult(
                    category="drone",
                    risk_score=75 if flags else 10,
                    red_flags=flags,
                    warnings=[],
                    trust_signals=[],
                    is_defective=bool(flags),
                )

        reg.register(MockDefectAnalyser())
        self.assertIn("drone", reg.list_analysers())

        # Verify scam detection on defect
        res = reg.analyze("drone", {"title": "FPV Drone 5 inch", "description": "crashed once into tree"})
        self.assertIsInstance(res, ScamAnalysisResult)
        self.assertTrue(res.risk_score >= 50)
        self.assertTrue(len(res.red_flags) > 0)

    def test_build_composer_registry(self):
        reg = get_composer_registry()

        class MockDroneComposer(BaseBuildComposer):
            name = "drone"
            title = "Drone Rig Composer"
            def compose(self, budget=None, mode=None, **kwargs):
                return ComposerSetResult(domain="drone", total_cost=500.0, items={}, is_complete=True)

        reg.register(MockDroneComposer())
        self.assertIn("drone", reg.list_composers())

        composer = reg.get("drone")
        self.assertIsNotNone(composer)
        self.assertEqual(composer.name, "drone")

    def test_ai_providers_registry(self):
        reg = get_ai_registry()
        providers = reg.list_providers()
        self.assertIn("gemini", providers)
        self.assertIn("openai", providers)
        self.assertIn("anthropic", providers)
        self.assertIn("groq", providers)
        self.assertIn("agy", providers)

        agy = reg.get("agy")
        self.assertIsNotNone(agy)
        self.assertEqual(agy.name, "agy")
        self.assertEqual(agy.provider_type, "cli")

    def test_component_analyser_registry(self):
        reg = get_component_registry()

        class MockDroneSpecAnalyser(BaseComponentAnalyser):
            name = "drone_motor"
            def identify(self, title, description=""):
                if "2207" in title:
                    return {"model": "Brushless 2207", "kv": 1950}
                return None

        reg.register(MockDroneSpecAnalyser())
        self.assertIn("drone_motor", reg.list_analysers())

        # Test model identification
        spec = reg.identify("drone_motor", "T-Motor 2207 1950KV Brushless")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.get("model"), "Brushless 2207")
        self.assertEqual(spec.get("kv"), 1950)

    def test_dynamic_extensibility_manual_registration(self):
        """Tests that any new module can be instantiated and dynamically registered on the fly."""
        # Custom Info Provider
        class MockStoreProvider(BaseInfoProvider):
            name = "mock_store"
            source_name = "mockstore.com"
            default_currency = "EUR"
            description = "Mock store provider for testing"
            def search(self, query, **kwargs):
                return []
            def adapt_item(self, raw_item):
                from info_providers.base import NormalizedListing
                return NormalizedListing(
                    offer_id="mock-1",
                    title=raw_item.get("title", ""),
                    price=raw_item.get("price", 0.0),
                    currency=self.default_currency,
                    url="https://mockstore.com/1",
                    source=self.name,
                )

        info_reg = get_info_registry()
        info_reg.register(MockStoreProvider())
        self.assertIn("mock_store", info_reg.list_providers())
        self.assertEqual(info_reg.get("mock_store").default_currency, "EUR")

        # Custom Component Analyser (e.g. SSD)
        class MockSSDAnalyser(BaseComponentAnalyser):
            name = "ssd"
            title = "Solid State Drive Analyser"
            def identify(self, title, description=""):
                if "980 pro" in title.lower():
                    return {"model": "Samsung 980 Pro", "form_factor": "M.2 NVMe", "capacity_gb": 1000}
                return None

        comp_reg = get_component_registry()
        comp_reg.register(MockSSDAnalyser())
        self.assertIn("ssd", comp_reg.list_analysers())
        ssd_spec = comp_reg.identify("ssd", "Samsung 980 PRO 1TB NVMe PCIe 4.0")
        self.assertIsNotNone(ssd_spec)
        self.assertEqual(ssd_spec["capacity_gb"], 1000)


if __name__ == "__main__":
    unittest.main()
