#!/usr/bin/env python3
"""
Unit Tests for Domain Layout, Domain Weights,
Extension Architecture, and Blueprint Domain Extension.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from domain_layout import CompatibilityRule, DomainLayout, SectionDefinition
from domain_weights import CriterionDefinition, DomainScorer, WeightProfile, WeightRegistry
from extension_registry import (
    DEFAULT_DOMAIN,
    get_extension,
    list_domain_ids,
    list_extensions,
    register_extension,
    unregister_extension,
)
from extensions.base import BaseDomainExtension, BaseSearchInstruction, BaseSorterExtension
from extensions.template import TemplateDomainExtension
from set_synthesizer import synthesize_domain_sets


class TestDomainLayout(unittest.TestCase):
    def test_section_definition(self):
        sec = SectionDefinition(
            id="rod",
            name="Fishing Rod",
            label="ROD",
            budget_share_min=0.2,
            budget_share_max=0.5,
        )
        self.assertEqual(sec.id, "rod")
        self.assertEqual(sec.label, "ROD")
        self.assertTrue(sec.required)

        # Default formatting fallback
        row = sec.format_row(
            {"model": "Test Rod", "price": 1000.0, "discount_pct": -10.0},
            lambda d: f"{d:+.0f}%",
        )
        self.assertEqual(row[0], "ROD")
        self.assertEqual(row[1], "Test Rod")
        self.assertTrue(any(sym in row[2] for sym in ("1,000 USDT", "1,000 ₴")))

    def test_compatibility_rule_and_partial_eval(self):
        def rule_fn(s):
            a = s.get("partA", {}).get("val", 0)
            b = s.get("partB", {}).get("val", 0)
            if a > b:
                return False, "partA exceeds partB"
            return True, None

        rule = CompatibilityRule(
            name="test_rule",
            description="A must be <= B",
            validator=rule_fn,
            sections_involved=("partA", "partB"),
        )
        # Not evaluable if only partA is present
        self.assertFalse(rule.is_evaluable({"partA": {"val": 10}}))
        # Evaluable when both present
        self.assertTrue(rule.is_evaluable({"partA": {"val": 5}, "partB": {"val": 10}}))
        ok, reason = rule.evaluate({"partA": {"val": 5}, "partB": {"val": 10}})
        self.assertTrue(ok)
        self.assertIsNone(reason)

        ok2, reason2 = rule.evaluate({"partA": {"val": 15}, "partB": {"val": 10}})
        self.assertFalse(ok2)
        self.assertEqual(reason2, "partA exceeds partB")

    def test_domain_layout_validation(self):
        layout = DomainLayout(
            domain_id="test_domain",
            title="Test Layout",
            sections=[
                SectionDefinition(id="partA", name="A", label="A"),
                SectionDefinition(id="partB", name="B", label="B"),
            ],
            compatibility_rules=[
                CompatibilityRule(
                    name="match_rule",
                    description="A must match B",
                    validator=lambda s: (s["partA"]["type"] == s["partB"]["type"], "Mismatch"),
                    sections_involved=("partA", "partB"),
                )
            ],
        )
        # Partial validation with only partA passes (early prune rule skipped)
        p_ok, _ = layout.validate_partial({"partA": {"type": "X"}})
        self.assertTrue(p_ok)

        # Full validation with mismatch fails
        f_ok, errs = layout.validate_combination({"partA": {"type": "X"}, "partB": {"type": "Y"}})
        self.assertFalse(f_ok)
        self.assertIn("Mismatch", errs)

        # Full validation with match passes
        f_ok2, errs2 = layout.validate_combination({"partA": {"type": "X"}, "partB": {"type": "X"}})
        self.assertTrue(f_ok2)
        self.assertEqual(errs2, [])


class TestDomainWeights(unittest.TestCase):
    def test_criterion_definition(self):
        c = CriterionDefinition(key="casting", title="Casting Distance", is_positive=True)
        self.assertEqual(c.key, "casting")
        self.assertEqual(c.title, "Casting Distance")
        self.assertTrue(c.is_positive)

    def test_weight_profile_normalization(self):
        prof = WeightProfile(
            name="test_prof",
            title="Test Profile",
            weights={"critA": 2.0, "critB": 2.0},
        )
        norm = prof.normalized_weights()
        self.assertAlmostEqual(norm["critA"], 0.5)
        self.assertAlmostEqual(norm["critB"], 0.5)

    def test_weight_profile_overrides(self):
        prof = WeightProfile(
            name="base",
            title="Base",
            weights={"a": 0.5, "b": 0.5},
            constraints={"min_tier": "B"},
        )
        over = prof.with_overrides(weights_override={"a": 0.8, "b": 0.2}, constraints_override={"min_tier": "A"})
        self.assertAlmostEqual(over.weights["a"], 0.8)
        self.assertAlmostEqual(over.weights["b"], 0.2)
        self.assertEqual(over.constraints["min_tier"], "A")

    def test_weight_registry_parse_string(self):
        parsed = WeightRegistry.parse_weights_string("perf=3, value=1")
        self.assertAlmostEqual(parsed["perf"], 0.75)
        self.assertAlmostEqual(parsed["value"], 0.25)

    def test_weight_registry_resolution(self):
        reg = WeightRegistry(domain_id="test", default_profile_name="prof1")
        reg.register_profile(
            WeightProfile(name="prof1", title="P1", weights={"w1": 1.0})
        )
        reg.register_profile(
            WeightProfile(name="prof2", title="P2", weights={"w2": 1.0})
        )
        # Default resolution
        self.assertEqual(reg.resolve_profile().name, "prof1")
        # Explicit resolution
        self.assertEqual(reg.resolve_profile("prof2").name, "prof2")
        # Unknown fallback to default
        self.assertEqual(reg.resolve_profile("unknown").name, "prof1")


class TestExtensionRegistry(unittest.TestCase):
    def test_blueprint_extension_discovered(self):
        exts = list_extensions()
        self.assertIn("template", exts)
        self.assertIsInstance(exts["template"], TemplateDomainExtension)

    def test_get_extension_by_id(self):
        template = get_extension("template")
        self.assertIsInstance(template, TemplateDomainExtension)

    def test_dynamic_extension_registration(self):
        class MockDroneExtension(BaseDomainExtension):
            domain_id = "test_drone_mock"
            title = "Mock Drone Rig"
            def get_layout(self): return None
            def get_weights_registry(self): return None
            def get_scorer(self): return None
            def get_search_instruction(self): return None
            def get_sorter(self): return None
            def load_candidates(self, budget): return {}
            def print_search_results(self, section_id, results, raw_query=""): pass

        mock_ext = MockDroneExtension()
        try:
            register_extension(mock_ext)
            self.assertIn("test_drone_mock", list_domain_ids())
            retrieved = get_extension("test_drone_mock")
            self.assertEqual(retrieved.title, "Mock Drone Rig")
        finally:
            unregister_extension("test_drone_mock")


class TestTemplateDomainExtension(unittest.TestCase):
    def setUp(self):
        self.ext = TemplateDomainExtension()
        self.layout = self.ext.get_layout()
        self.weights_reg = self.ext.get_weights_registry()
        self.scorer = self.ext.get_scorer()

    def test_template_layout_and_rules(self):
        self.assertEqual(self.layout.domain_id, "template")
        sections = self.layout.section_ids()
        self.assertIn("core", sections)
        self.assertIn("addon", sections)
        self.assertIn("power", sections)

        # Check compatibility rules defined on template
        rules = {r.name: r for r in self.layout.compatibility_rules}
        self.assertIn("core_power_match", rules)

        # Test valid pair: power 500W >= core 250W
        valid_set = {
            "core": {"spec": {"watts": 250}},
            "power": {"spec": {"watts": 500}},
        }
        ok, reason = rules["core_power_match"].evaluate(valid_set)
        self.assertTrue(ok)
        self.assertIsNone(reason)

        # Test invalid pair: power 150W < core 300W
        invalid_set = {
            "core": {"spec": {"watts": 300}},
            "power": {"spec": {"watts": 150}},
        }
        ok_bad, reason_bad = rules["core_power_match"].evaluate(invalid_set)
        self.assertFalse(ok_bad)
        self.assertIn("lower", reason_bad)

    def test_template_synthesis(self):
        profile = self.weights_reg.resolve_profile("balanced")
        candidates = self.ext.load_candidates(budget=16000.0)

        sets = synthesize_domain_sets(
            layout=self.layout,
            scorer=self.scorer,
            profile=profile,
            candidate_pool=candidates,
            budget=16000.0,
            top_n=3,
        )
        self.assertGreater(len(sets), 0)
        best = sets[0]
        self.assertLessEqual(best["total_cost"], 16000.0)
        self.assertIn("core", best["components"])
        self.assertIn("addon", best["components"])
        self.assertIn("power", best["components"])

        # Validate that synthesized combination satisfies rules
        valid, errs = self.layout.validate_combination(best["components"])
        self.assertTrue(valid, f"Template set failed rules: {errs}")


class TestDeclarativeRuleDSL(unittest.TestCase):
    """Tests zero-code declarative CompatibilityRule DSL and DomainLayout.from_dict."""

    def test_from_expression_infers_sections_and_evaluates(self):
        rule = CompatibilityRule.from_expression(
            name="esc_motor_headroom",
            expression="esc.max_amps >= motor.peak_amps * 1.2 and esc.cells_s == battery.cells_s",
            error_message="ESC current or voltage rating mismatch",
        )
        self.assertEqual(rule.sections_involved, ("esc", "motor", "battery"))

        # Partial evaluation when battery is missing -> not evaluable yet
        partial = {
            "esc": {"spec": {"max_amps": 60, "cells_s": 6}},
            "motor": {"spec": {"peak_amps": 45}},
        }
        self.assertFalse(rule.is_evaluable(partial))

        # Full valid set (60 >= 45 * 1.2 = 54, cells_s == 6)
        valid_set = {
            "esc": {"spec": {"max_amps": 60, "cells_s": 6}},
            "motor": {"spec": {"peak_amps": 45}},
            "battery": {"cells_s": 6},
        }
        self.assertTrue(rule.is_evaluable(valid_set))
        ok, err = rule.evaluate(valid_set)
        self.assertTrue(ok)
        self.assertIsNone(err)

        # Invalid set (45A ESC < 45 * 1.2 = 54A)
        invalid_set = {
            "esc": {"spec": {"max_amps": 45, "cells_s": 6}},
            "motor": {"spec": {"peak_amps": 45}},
            "battery": {"cells_s": 6},
        }
        ok2, err2 = rule.evaluate(invalid_set)
        self.assertFalse(ok2)
        self.assertEqual(err2, "ESC current or voltage rating mismatch")

    def test_domain_layout_from_dict_and_security(self):
        blueprint = {
            "domain_id": "solar_backup",
            "title": "Solar & Inverter Backup Kit",
            "default_budget": 1200.0,
            "currency": "USDT",
            "sections": [
                {"id": "inverter", "name": "Hybrid Inverter", "label": "INVERTER"},
                {"id": "battery", "name": "LiFePO4 Battery", "label": "BATTERY"},
            ],
            "compatibility_rules": [
                {
                    "name": "system_voltage_match",
                    "expression": "inverter.voltage_v == battery.voltage_v and battery.capacity_wh >= inverter.min_wh",
                    "error_message": "Inverter and Battery voltage/capacity mismatch",
                }
            ],
        }
        layout = DomainLayout.from_dict(blueprint)
        self.assertEqual(layout.domain_id, "solar_backup")
        self.assertEqual(layout.section_ids(), ["inverter", "battery"])

        ok, _ = layout.validate_combination({
            "inverter": {"voltage_v": 48, "min_wh": 2000},
            "battery": {"spec": {"voltage_v": 48, "capacity_wh": 5120}},
        })
        self.assertTrue(ok)

        ok_bad, errs = layout.validate_combination({
            "inverter": {"voltage_v": 48, "min_wh": 2000},
            "battery": {"spec": {"voltage_v": 24, "capacity_wh": 5120}},
        })
        self.assertFalse(ok_bad)
        self.assertIn("Inverter and Battery voltage/capacity mismatch", errs)

        # Ensure dangerous dunder / arbitrary calls are rejected in DSL
        with self.assertRaises(ValueError):
            CompatibilityRule.from_expression("bad", "inverter.__class__ == 1")
        with self.assertRaises(ValueError):
            CompatibilityRule.from_expression("bad_call", "eval('1+1') == 2")

    def test_json_blueprint_domain_extension_and_autodiscovery(self):
        import json
        import tempfile
        import extension_registry
        from extension_registry import JsonBlueprintDomainExtension

        blueprint = {
            "domain_id": "solar_rig",
            "title": "Solar & Inverter Backup Kit",
            "default_budget": 1500.0,
            "currency": "USDT",
            "default_mode": "efficiency",
            "sections": [
                {"id": "inverter", "name": "Hybrid Inverter", "label": "INVERTER"},
                {"id": "battery", "name": "LiFePO4 Battery", "label": "BATTERY"},
            ],
            "compatibility_rules": [
                {
                    "name": "voltage_match",
                    "expression": "inverter.voltage_v == battery.voltage_v and battery.capacity_wh >= inverter.min_wh",
                    "error_message": "Inverter and Battery voltage/capacity mismatch",
                }
            ],
            "criteria": [
                {"key": "value", "title": "Cost Efficiency", "default_weight": 0.5},
                {"key": "perf", "title": "Power Output", "default_weight": 0.5},
            ],
            "modes": {
                "efficiency": {
                    "title": "Max Efficiency",
                    "description": "Prioritizes value and capacity per dollar",
                    "weights": {"value": 0.6, "perf": 0.4},
                }
            },
            "sample_market": {
                "inverter": [
                    {"id": "inv_48", "title": "Deye 5kW 48V Hybrid", "price": 600.0, "currency": "USDT", "rating": 92.0, "spec": {"voltage_v": 48, "min_wh": 2500}},
                    {"id": "inv_24", "title": "PowMr 3kW 24V", "price": 280.0, "currency": "USDT", "rating": 78.0, "spec": {"voltage_v": 24, "min_wh": 1500}},
                ],
                "battery": [
                    {"id": "bat_24_bad", "title": "LiFePO4 24V 2560Wh", "price": 450.0, "currency": "USDT", "rating": 85.0, "spec": {"voltage_v": 24, "capacity_wh": 2560}},
                    {"id": "bat_48_ok", "title": "Pylontech 48V 5120Wh", "price": 800.0, "currency": "USDT", "rating": 95.0, "spec": {"voltage_v": 48, "capacity_wh": 5120}},
                ],
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            bp_dir = Path(tmp) / "blueprints"
            bp_dir.mkdir(parents=True, exist_ok=True)
            bp_file = bp_dir / "solar_rig.json"
            bp_file.write_text(json.dumps(blueprint), encoding="utf-8")

            orig_dir = extension_registry.BLUEPRINTS_DIR
            try:
                extension_registry.BLUEPRINTS_DIR = bp_dir
                extension_registry.rescan_extensions()

                self.assertIn("solar_rig", list_domain_ids())
                ext = get_extension("solar_rig")
                self.assertIsInstance(ext, JsonBlueprintDomainExtension)
                self.assertEqual(ext.find_owner_test if hasattr(ext, "find_owner_test") else ext.domain_id, "solar_rig")

                # Verify search & synthesis obey the declarative voltage rule
                cands = ext.load_candidates(budget=1500.0)
                prof = ext.get_weights_registry().resolve_profile("efficiency")
                sets = ext.get_sorter().sort_and_synthesize(
                    layout=ext.get_layout(),
                    scorer=ext.get_scorer(),
                    profile=prof,
                    candidate_pool=cands,
                    budget=1500.0,
                    top_n=5,
                )
                self.assertGreater(len(sets), 0)
                for s in sets:
                    inv_v = s["components"]["inverter"]["spec"]["voltage_v"]
                    bat_v = s["components"]["battery"]["spec"]["voltage_v"]
                    self.assertEqual(inv_v, bat_v)

                # Verify section search works
                res = ext.search("inverter", query="Deye")
                self.assertEqual(len(res), 1)
                self.assertEqual(res[0]["id"], "inv_48")
            finally:
                extension_registry.BLUEPRINTS_DIR = orig_dir
                extension_registry.rescan_extensions()


class TestCustomDomainCombinatorialSynthesis(unittest.TestCase):
    """Verifies that an arbitrary new domain can be constructed and solved by the engine."""

    def test_robotics_arm_domain_synthesis(self):
        layout = DomainLayout(
            domain_id="robot_arm",
            title="Robotic Arm Rig",
            sections=[
                SectionDefinition(id="motor", name="Servo Motor", label="MOTOR", budget_share_min=0.3, budget_share_max=0.6),
                SectionDefinition(id="battery", name="LiPo Battery", label="BATTERY", budget_share_min=0.2, budget_share_max=0.4),
                SectionDefinition(id="controller", name="Microcontroller", label="MCU", budget_share_min=0.1, budget_share_max=0.3),
            ],
            compatibility_rules=[
                CompatibilityRule(
                    name="voltage_match",
                    description="Motor voltage must not exceed battery voltage",
                    validator=lambda s: (s["motor"]["spec"]["voltage_v"] <= s["battery"]["spec"]["voltage_v"], "Motor voltage exceeds battery!"),
                    sections_involved=("motor", "battery"),
                )
            ],
        )

        candidates = {
            "motor": [
                {"id": "m1", "title": "Servo 12V High Torque", "price": 1200.0, "spec": {"voltage_v": 12.0, "torque_kg": 25.0}},
                {"id": "m2", "title": "Servo 24V Industrial", "price": 2500.0, "spec": {"voltage_v": 24.0, "torque_kg": 50.0}},
            ],
            "battery": [
                {"id": "b1", "title": "3S LiPo 11.1V", "price": 800.0, "spec": {"voltage_v": 11.1, "capacity_mah": 2200}},
                {"id": "b2", "title": "4S LiPo 14.8V", "price": 1100.0, "spec": {"voltage_v": 14.8, "capacity_mah": 3000}},
            ],
            "controller": [
                {"id": "c1", "title": "ESP32 DevKit", "price": 300.0, "spec": {"cores": 2, "freq_mhz": 240}},
                {"id": "c2", "title": "STM32F4 Arm Core", "price": 500.0, "spec": {"cores": 1, "freq_mhz": 168}},
            ],
        }

        weights_reg = WeightRegistry(domain_id="robot_arm", default_profile_name="balanced")
        weights_reg.register_profile(
            WeightProfile(name="balanced", title="Balanced Robotics", weights={"perf": 0.5, "value": 0.5})
        )

        scorer = DomainScorer(
            component_scorer=lambda sec, item, prof: 80.0,
            set_scorer=lambda comps, prof, budget: 85.0,
        )

        sets = synthesize_domain_sets(
            layout=layout,
            scorer=scorer,
            profile=weights_reg.resolve_profile("balanced"),
            candidate_pool=candidates,
            budget=3000.0,
            top_n=3,
        )

        self.assertGreater(len(sets), 0)
        top = sets[0]
        self.assertLessEqual(top["total_cost"], 3000.0)
        # Motor 12V with Battery 14.8V matches voltage rule
        self.assertLessEqual(top["components"]["motor"]["spec"]["voltage_v"], top["components"]["battery"]["spec"]["voltage_v"])

    def test_min_new_ratio_and_export_sets(self):
        import json
        import tempfile
        from set_synthesizer import export_domain_sets, format_sets_markdown

        layout = DomainLayout(
            domain_id="test_rig",
            title="Test Rig",
            sections=[
                SectionDefinition(id="a", name="Part A", label="A"),
                SectionDefinition(id="b", name="Part B", label="B"),
            ],
        )
        candidates = {
            "a": [
                {"id": "a_used", "title": "Used A", "price": 100.0, "is_new": False, "condition": "used", "rating": 95.0},
                {"id": "a_new", "title": "New A", "price": 150.0, "is_new": True, "condition": "new", "rating": 80.0},
            ],
            "b": [
                {"id": "b_used", "title": "Used B", "price": 100.0, "is_new": False, "condition": "used", "rating": 95.0},
                {"id": "b_new", "title": "New B", "price": 150.0, "is_new": True, "condition": "new", "rating": 80.0},
            ],
        }
        weights_reg = WeightRegistry(domain_id="test_rig", default_profile_name="balanced")
        weights_reg.register_profile(WeightProfile(name="balanced", title="Balanced", weights={"value": 0.5, "perf": 0.5}))
        prof = weights_reg.resolve_profile("balanced")
        scorer = DomainScorer()

        # Require 100% new items (min_new_ratio=1.0)
        sets_all_new = synthesize_domain_sets(
            layout=layout,
            scorer=scorer,
            profile=prof,
            candidate_pool=candidates,
            budget=500.0,
            top_n=3,
            min_new_ratio=1.0,
        )
        self.assertEqual(len(sets_all_new), 1)
        self.assertEqual(sets_all_new[0]["components"]["a"]["id"], "a_new")
        self.assertEqual(sets_all_new[0]["components"]["b"]["id"], "b_new")
        self.assertEqual(sets_all_new[0]["new_count"], 2)

        # Verify Markdown & JSON export
        md_text = format_sets_markdown(layout, prof, sets_all_new)
        self.assertIn("New A", md_text)
        self.assertIn("New B", md_text)

        with tempfile.TemporaryDirectory() as tmp:
            md_path = export_domain_sets(Path(tmp) / "sets.md", layout, prof, sets_all_new)
            json_path = export_domain_sets(Path(tmp) / "sets.json", layout, prof, sets_all_new)
            self.assertTrue(md_path.is_file())
            self.assertIn("## Set #1", md_path.read_text(encoding="utf-8"))
            parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed_json["domain"], "test_rig")
            self.assertEqual(parsed_json["count"], 1)


if __name__ == "__main__":
    unittest.main()
