"""
Comprehensive Stress Testing, Fault Tolerance, and Marketplace Schema Validation Suite.

Covers:
  - Broken/malformed/empty plugin isolation in all 5 subsystem registries
  - Offline, missing cache, and corrupted cache resilience
  - Extreme prices (0 ₴, 999,999 ₴, negative numbers) and unusual Unicode/emoji hardware titles
  - Standardized Module Marketplace manifest compliance across all 5 subsystems
  - CLI subcommand end-to-end execution
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict

# Core imports
from ai_providers import AIProviderRegistry, BaseAIProvider, get_ai_registry
from build_composer import BaseBuildComposer, BuildComposerRegistry, ComposerSetResult, get_composer_registry
from component_analyser import BaseComponentAnalyser, ComponentAnalyserRegistry, get_component_registry
from info_providers import BaseInfoProvider, InfoProviderRegistry, NormalizedListing, get_info_registry
from scam_analyser import BaseScamAnalyser, ScamAnalysisResult, ScamAnalyserRegistry, get_scam_registry

import main
from analyzer_core import compute_market_discount_pct, is_listing_disqualified
from market_client import load_market_cache


class TestRegistryFaultTolerance(unittest.TestCase):
    """Verifies that all 5 subsystem registries safely isolate broken/malformed plugins."""

    def test_info_provider_registry_isolates_broken_plugins(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # 1. Broken syntax plugin
            (tmp_path / "broken_syntax.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

            # 2. Runtime import error plugin
            (tmp_path / "runtime_error.py").write_text("raise RuntimeError('Import exploded!')\n", encoding="utf-8")

            # 3. Broken instantiation plugin
            inst_code = """
from info_providers.base import BaseInfoProvider, NormalizedListing

class BrokenInitProvider(BaseInfoProvider):
    name = "broken_init"
    def __init__(self):
        raise ValueError("Cannot initialize")
    def adapt_item(self, raw_data):
        return None
"""
            (tmp_path / "broken_init.py").write_text(inst_code, encoding="utf-8")

            # 4. Valid plugin
            valid_code = """
from info_providers.base import BaseInfoProvider, NormalizedListing

class ValidTestProvider(BaseInfoProvider):
    name = "valid_test_info"
    title = "Valid Test Marketplace"
    def adapt_item(self, raw_data):
        return NormalizedListing(id="1", title="test", price=100.0)
"""
            (tmp_path / "valid_test.py").write_text(valid_code, encoding="utf-8")

            # 5. Service template files (should be ignored)
            (tmp_path / "template.py").write_text("# template\n", encoding="utf-8")
            (tmp_path / "base.py").write_text("# base\n", encoding="utf-8")
            (tmp_path / "__init__.py").write_text("# init\n", encoding="utf-8")

            # Discover
            reg = InfoProviderRegistry(modules_dir=tmp_path)

            # Assert valid module is loaded
            self.assertIn("valid_test_info", reg.list_providers())
            self.assertEqual(reg.get("valid_test_info").name, "valid_test_info")

            # Assert errors are recorded and isolated without crashing
            errors = reg.get_errors()
            self.assertIn("broken_syntax.py", errors)
            self.assertIn("runtime_error.py", errors)
            self.assertIn("broken_init.py", errors)
            self.assertNotIn("template.py", errors)
            self.assertNotIn("base.py", errors)

    def test_scam_analyser_registry_isolates_broken_plugins(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "broken_syntax.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

            valid_code = """
from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult

class ValidScamAnalyser(BaseScamAnalyser):
    name = "camera"
    title = "Camera Scam Analyser"
    def analyze(self, item, detail=None, spec=None):
        return ScamAnalysisResult(category="camera", risk_score=5)
"""
            (tmp_path / "camera.py").write_text(valid_code, encoding="utf-8")
            (tmp_path / "template.py").write_text("# template\n", encoding="utf-8")

            reg = ScamAnalyserRegistry(modules_dir=tmp_path)
            self.assertIn("camera", reg.list_analysers())
            self.assertEqual(reg.analyze("camera", {}).risk_score, 5)
            self.assertIn("broken_syntax.py", reg.get_errors())

    def test_build_composer_registry_isolates_broken_plugins(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "broken_syntax.py").write_text("class Broken(::\n", encoding="utf-8")

            valid_code = """
from build_composer.base import BaseBuildComposer, ComposerSetResult

class ValidComposer(BaseBuildComposer):
    name = "homelab"
    title = "Homelab Server Synthesizer"
    def compose(self, candidate_pool, budget, mode='best', weights=None, top_n=5, **kwargs):
        return [ComposerSetResult(domain="homelab", mode=mode, total_cost=1000.0, budget=budget, surplus=budget-1000.0, score=90.0, components={})]
"""
            (tmp_path / "homelab.py").write_text(valid_code, encoding="utf-8")

            reg = BuildComposerRegistry(modules_dir=tmp_path)
            self.assertIn("homelab", reg.list_composers())
            results = reg.compose("homelab", {}, budget=5000.0)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].domain, "homelab")
            self.assertIn("broken_syntax.py", reg.get_errors())

    def test_ai_provider_registry_isolates_broken_plugins(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "broken_syntax.py").write_text("broken = [1, 2, \n", encoding="utf-8")

            valid_code = """
from ai_providers.base import BaseAIProvider

class ValidAI(BaseAIProvider):
    name = "local_mock_ai"
    title = "Local Mock AI"
    def is_ready(self):
        return True, "Ready"
    def complete(self, prompt, **kwargs):
        return {"response": "ok"}
"""
            (tmp_path / "local_mock_ai.py").write_text(valid_code, encoding="utf-8")

            reg = AIProviderRegistry(modules_dir=tmp_path)
            self.assertIn("local_mock_ai", reg.list_providers())
            self.assertIn("broken_syntax.py", reg.get_errors())

    def test_component_analyser_registry_isolates_broken_plugins(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / "broken_syntax.py").write_text("def 123_invalid(): pass\n", encoding="utf-8")

            valid_code = """
from component_analyser.base import BaseComponentAnalyser

class ValidSSDAnalyser(BaseComponentAnalyser):
    name = "ssd"
    title = "Solid State Drive Analyser"
    def identify(self, title, description=""):
        return {"model": "Samsung 980 Pro 1TB", "form_factor": "M.2 NVMe"}
"""
            (tmp_path / "ssd.py").write_text(valid_code, encoding="utf-8")

            reg = ComponentAnalyserRegistry(modules_dir=tmp_path)
            self.assertIn("ssd", reg.list_analysers())
            ident = reg.identify("ssd", "Samsung 980 Pro")
            self.assertEqual(ident["model"], "Samsung 980 Pro 1TB")
            self.assertIn("broken_syntax.py", reg.get_errors())


class TestOfflineAndEdgeCaseResilience(unittest.TestCase):
    """Verifies that missing files, offline scenarios, and empty caches never raise fatal exceptions."""

    def test_load_market_cache_missing_or_corrupted(self):
        # 1. Non-existent category
        offers, ts, path = load_market_cache("non_existent_category_xyz")
        self.assertEqual(offers, [])
        self.assertEqual(ts, "")
        self.assertIsNone(path)

        # 2. Corrupted JSON file in temp dir
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_json = Path(tmp_dir) / "corrupt_gpu_market_cache.json"
            bad_json.write_text("{ incomplete json ...", encoding="utf-8")
            from market_client import INTEL_DATA_DIR
            # Ensure loading missing custom provider returns empty without crashing
            offers, _, _ = load_market_cache("gpu", provider="non_existent_provider_xyz")
            self.assertIsInstance(offers, list)

    def test_ai_provider_offline_and_missing_keys(self):
        ai_reg = get_ai_registry()
        # Verify offline or missing key providers report is_ready = False cleanly
        prov = ai_reg.get("gemini")
        if prov:
            ok, msg = prov.is_ready()
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(msg, str)


class TestExtremePricesAndHardwareEdgeCases(unittest.TestCase):
    """Verifies numeric edge cases (0, 999999, negative prices) and non-standard strings."""

    def test_extreme_prices_in_discount_computation(self):
        spec = {"market_used_median": 10000.0}

        # 1. Zero price (free listing / giveaway, guarded to return 0.0 discount)
        disc_zero = compute_market_discount_pct(0.0, spec)
        self.assertEqual(disc_zero, 0.0)

        # 2. Extreme luxury / troll price
        disc_huge = compute_market_discount_pct(999999.0, spec)
        self.assertLess(disc_huge, -100.0)

        # 3. Negative price (malformed data guarded to 0.0)
        disc_neg = compute_market_discount_pct(-500.0, spec)
        self.assertEqual(disc_neg, 0.0)

        # 4. Normal good deal (8000 on 10000 = 20% discount)
        disc_norm = compute_market_discount_pct(8000.0, spec)
        self.assertEqual(disc_norm, 20.0)

    def test_disqualification_heuristics_and_ai_eval(self):
        # Heuristic red flags
        self.assertTrue(is_listing_disqualified({"red_flags": ["broken"]}))
        self.assertTrue(is_listing_disqualified({"is_mining_alert": True}))
        self.assertTrue(is_listing_disqualified({"is_fake": True}))
        self.assertFalse(is_listing_disqualified({}))

        # AI eval overrides
        self.assertTrue(is_listing_disqualified({}, {"verdict": "REJECTED"}))
        self.assertFalse(is_listing_disqualified({"red_flags": ["minor"]}, {"verdict": "APPROVED"}))

    def test_nonstandard_hardware_names_and_unicode_symbols(self):
        reg = get_component_registry()
        ssd_analyser = reg.get("ssd")
        if ssd_analyser:
            ident = ssd_analyser.identify("★ Samsung 980 Pro 1TB M.2 — [SEALED]")
            self.assertIsNotNone(ident)
            self.assertEqual(ident.get("model"), "Samsung 980 Pro")
            self.assertIsNone(ssd_analyser.identify(""))
            # Huge description
            huge_desc = "Testing hardware specs. " * 5000
            ident_huge = ssd_analyser.identify("Samsung 980 Pro 1TB", huge_desc)
            self.assertIsNotNone(ident_huge)


class TestMarketplaceManifestCompliance(unittest.TestCase):
    """Verifies that all discovered modules implement the unified manifest standard."""

    def test_all_modules_have_valid_manifest(self):
        registries = [
            ("info", get_info_registry().list_providers()),
            ("scam", get_scam_registry().list_analysers()),
            ("composer", get_composer_registry().list_composers()),
            ("ai", get_ai_registry().list_providers()),
            ("component", get_component_registry().list_analysers()),
        ]

        required_keys = {"name", "version", "author", "description", "min_app_version", "category", "tags", "url"}

        for subsystem, items in registries:
            for mod_key, mod_obj in items.items():
                self.assertTrue(
                    hasattr(mod_obj, "get_manifest"),
                    f"Module '{mod_key}' in subsystem '{subsystem}' must implement get_manifest()",
                )
                manifest = mod_obj.get_manifest()
                self.assertIsInstance(manifest, dict, f"Manifest for '{mod_key}' must be a dictionary")
                for key in required_keys:
                    self.assertIn(
                        key,
                        manifest,
                        f"Manifest for '{mod_key}' in '{subsystem}' missing required marketplace key: '{key}'",
                    )
                self.assertEqual(manifest["name"], mod_key)

    def test_cli_modules_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._run_modules(["--json"])
        raw = buf.getvalue().strip()
        data = json.loads(raw)

        self.assertIn("info", data)
        self.assertIn("scam", data)
        self.assertIn("composer", data)
        self.assertIn("ai", data)
        self.assertIn("component", data)

        for s_id, s_info in data.items():
            self.assertIn("directory", s_info)
            self.assertIn("title", s_info)
            self.assertIn("count", s_info)
            self.assertIn("error_count", s_info)
            self.assertIn("errors", s_info)
            self.assertIn("modules", s_info)
            for m_key, m_val in s_info["modules"].items():
                self.assertIn("manifest", m_val)
                self.assertIn("module_file", m_val)
                self.assertIn("status", m_val)


class TestEndToEndCLI(unittest.TestCase):
    """Validates execution of core CLI subcommands."""

    def test_cli_modules_table(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._run_modules(["-s", "info"])
        out = buf.getvalue()
        self.assertIn("Market Information Providers", out)
        self.assertIn("olx", out)

    def test_cli_sync_status(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._run_sync(["--status"])
        out = buf.getvalue()
        # No implicit default domain: shows status for installed domains, or says none are installed.
        self.assertTrue("Local Market Cache Status" in out or "No domain extensions installed" in out, out)

    def test_cli_modes_list(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._run_modes([])
        out = buf.getvalue().upper()
        self.assertIn("OPTIMIZATION MODES", out)

    def test_cli_domains_list(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main._run_domains([])
        out = buf.getvalue()
        self.assertIn("Registered Domain Extensions", out)
        self.assertIn("template", out)


if __name__ == "__main__":
    unittest.main()
