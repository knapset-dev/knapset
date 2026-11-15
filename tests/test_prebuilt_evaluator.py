"""
Unit Tests for PrebuiltEvaluator and the eval / vs command routing.
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class TestPrebuiltEvaluator(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from marketplace.client import MarketplaceClient, locate_registry_dir
        from extension_registry import rescan_extensions
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        cls._was_already_installed = (root / "extensions" / "pc").is_dir()
        reg_file = locate_registry_dir() / "registry.json"
        cls.client = MarketplaceClient(registry_url_or_path=str(reg_file), root_dir=root)
        if not cls._was_already_installed:
            cls.client.install("pc")
        rescan_extensions()

    @classmethod
    def tearDownClass(cls):
        from extension_registry import rescan_extensions
        if not getattr(cls, "_was_already_installed", False):
            cls.client.remove("pc", force=True)
            rescan_extensions()

    def setUp(self):
        from extensions.pc.prebuilt_evaluator import PrebuiltEvaluator
        self.evaluator = PrebuiltEvaluator()

    def test_parse_specs_comprehensive(self):
        ad_text = "Gaming PC Ryzen 5 3600, RTX 2060 Super 8GB, B450M, 16GB DDR4, PSU 500W, 512GB SSD. Price 18500 UAH"
        specs = self.evaluator.parse_specs(title=ad_text, description="")

        self.assertAlmostEqual(specs.price, 18500.0)
        self.assertIsNotNone(specs.gpu)
        self.assertEqual(specs.raw_matches.get("gpu"), "RTX 2060 SUPER")
        self.assertIsNotNone(specs.cpu)
        self.assertEqual(specs.raw_matches.get("cpu"), "RYZEN 5 3600")
        self.assertIsNotNone(specs.ram)
        self.assertEqual(specs.ram.get("capacity_gb"), 16)
        self.assertGreaterEqual(specs.confidence_score, 0.8)

    def test_fair_market_value_calculation(self):
        specs = self.evaluator.parse_specs(
            title="PC Ryzen 5 3600, RTX 2060 Super, B450, 16GB, 500W, 18500 UAH",
            description="",
            price=18500.0,
        )
        val = self.evaluator.calculate_fair_value(specs)

        self.assertIn("gpu", val["parts"])
        self.assertIn("cpu", val["parts"])
        self.assertIn("total_fair_value", val)
        self.assertGreater(val["total_fair_value"], 10000.0)
        self.assertEqual(val["asking_price"], 18500.0)
        self.assertIsInstance(val["markup_pct"], float)

    def test_hazard_detection_aerocool_vx(self):
        specs = self.evaluator.parse_specs(
            title="Gaming PC i7 3770, GTX 1060, PSU Aerocool VX-500, 12000 UAH",
            description="Price negotiable",
            price=12000.0,
        )
        risks = self.evaluator.detect_risks(specs)

        risk_titles = [r["title"] for r in risks]
        self.assertTrue(any("Unsafe Power Supply" in t or "Fire-Hazard" in t for t in risk_titles))
        self.assertTrue(any("Obsolete Platform" in t for t in risk_titles))

    def test_evaluate_and_verdict(self):
        report = self.evaluator.evaluate(
            "Gaming PC Ryzen 5 3600, RTX 2060 Super 8GB, B450, 16GB DDR4, PSU 500W, 18500 UAH",
            mode="value",
        )

        self.assertIn("verdict", report)
        self.assertIn("verdict_badge", report)
        self.assertIn("valuation", report)
        self.assertIn("recommendation", report)
        self.assertIn("mode", report)
        self.assertEqual(report["mode"]["name"], "value")
        self.assertIsInstance(report["mode"]["score"], float)
        self.assertIn(report["verdict"], ("SUPER_DEAL", "FAIR_PRICE", "OVERPRICED", "DANGEROUS"))

    def test_compare_duo_head_to_head(self):
        pc_a = "Gaming PC Ryzen 5 3600, RTX 2060 Super 8GB, B450, 16GB DDR4, PSU 500W, 18500 UAH"
        pc_b = "Desktop PC Core i5 10400F, RTX 3060 12GB, B560, 16GB DDR4, PSU 600W, 20500 UAH"

        report = self.evaluator.compare_duo(pc_a, pc_b, mode="sweet-spot")
        self.assertTrue(report.get("is_duo"))
        self.assertEqual(report["mode_name"], "sweet-spot")
        self.assertGreater(report["score_a"], 0.0)
        self.assertGreater(report["score_b"], 0.0)
        self.assertEqual(len(report["matchups"]), 6)
        self.assertIn(report["winner"], ("A", "B", "TIE"))

    def test_compare_duo_hazard_disqualification(self):
        pc_unsafe = "Gaming PC Ryzen 5 3600, RTX 3060 12GB, Aerocool VX-500, 17000 UAH"
        pc_safe = "PC Ryzen 5 3600, RTX 2060 Super, PSU 600W, 18500 UAH"

        report = self.evaluator.compare_duo(pc_unsafe, pc_safe, mode="safe")
        self.assertEqual(report["winner"], "B")
        self.assertIn("critical safety hazard", report["winner_reason"])

    def test_cli_eval_and_vs_routing(self):
        f_pc = io.StringIO()
        with redirect_stdout(f_pc):
            main.main(["pc", "eval"])
        out_pc = f_pc.getvalue()
        self.assertIn("OmniForge Prebuilt Evaluator & Head-to-Head Duel Comparator", out_pc)

        f_vs = io.StringIO()
        with redirect_stdout(f_vs):
            main.main(["pc", "vs", "Ryzen 5 3600, RTX 2060 Super 8GB, B450, 16GB DDR4, 500W, 18500 UAH"])
        out_vs = f_vs.getvalue()
        self.assertIn("OMNIFORGE PREBUILT EVALUATOR & MARKET DUEL", out_vs)
        self.assertIn("VERDICT", out_vs)

        f_cmp = io.StringIO()
        with redirect_stdout(f_cmp):
            main.main([
                "pc", "compare",
                "Ryzen 5 3600, RTX 2060 Super 8GB, 18500 UAH",
                "vs",
                "Core i5 10400F, RTX 3060 12GB, 20500 UAH",
                "-M", "sweet-spot",
            ])
        out_cmp = f_cmp.getvalue()
        self.assertIn("OMNIFORGE HEAD-TO-HEAD PREBUILT DUEL & COMPARATOR", out_cmp)
        self.assertIn("COMPONENT MATCHUP", out_cmp)
        self.assertIn("ACTIVE MODE SCORE [SWEET-SPOT]", out_cmp)
        self.assertIn("WINNER", out_cmp)

        # Ensure top-level unprefixed commands ('eval', 'pc-eval', 'build', 'gpu') are rejected with helpful guidance
        for bare_cmd in ("eval", "build", "gpu"):
            stderr_f = io.StringIO()
            with patch("sys.stderr", stderr_f):
                with self.assertRaises(SystemExit) as cm:
                    main.main([bare_cmd])
                self.assertEqual(cm.exception.code, 1)
            err = stderr_f.getvalue()
            self.assertTrue(
                "must be scoped to a domain module" in err or "must be prefixed with its domain module name" in err,
                f"Expected prefix error message for '{bare_cmd}', got: {err}",
            )


if __name__ == "__main__":
    unittest.main()
