"""
Unit tests for Knapset v2 TUI Session Controller (`tui.TuiSessionState`) & rendering helpers.
Tests weight `+`/`-` adjustments, reference attachment, custom formulas/limits/rules,
live re-solving, mode cycling, and saving customized presets without requiring a physical TTY.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preset_loader import discover_presets, load_preset
from tui import (
    TuiSessionState,
    _build_workspace_items,
    _compute_hold_interval,
    _format_weight_bar,
    _render_result_lines,
    _wrap_suggestion_lines,
    discover_local_data_files,
)


class TestTuiSessionState(unittest.TestCase):
    def test_interactive_source_ref_weights_rules_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "rig.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE cpu (name TEXT, socket TEXT, price REAL, score INTEGER)")
                conn.executemany(
                    "INSERT INTO cpu VALUES (?, ?, ?, ?)",
                    [
                        ("Ryzen 5 5600", "AM4", 120.0, 21500),
                        ("Ryzen 7 7800X3D", "AM5", 355.0, 34200),
                    ],
                )
                conn.execute("CREATE TABLE gpu (title TEXT, price REAL)")
                conn.executemany(
                    "INSERT INTO gpu VALUES (?, ?)",
                    [
                        ("MSI RTX 3080", 380.0),
                        ("ASUS RTX 3080 Ti", 520.0),
                    ],
                )
                conn.execute("CREATE TABLE mobo (name TEXT, socket TEXT, price REAL)")
                conn.executemany(
                    "INSERT INTO mobo VALUES (?, ?, ?)",
                    [
                        ("B550", "AM4", 110.0),
                        ("B650", "AM5", 170.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            gpu_ref = root / "gpu_ref.csv"
            gpu_ref.write_text("model,score,msrp\nRTX 3080,25000,699\nRTX 3080 Ti,27500,1199\n", encoding="utf-8")

            state = TuiSessionState()
            state.load_from_source(str(db_path))
            self.assertEqual(state.mode, "solve")
            self.assertGreaterEqual(len(state.results), 1)

            # Attach Reference DB onto gpu slot
            state.attach_reference("gpu", str(gpu_ref), match_expr="title~model")
            self.assertIn("gpu.ref.score", state.available_columns)

            # Adjust first weight using +/- and add custom cross-slot weight & rule `cpu/gpu < 1`
            init_w = state.weights[0][1]
            state.adjust_weight(0, 0.1)
            self.assertAlmostEqual(state.weights[0][1], round(init_w + 0.1, 2))
            state.adjust_weight(0, -0.2)
            self.assertAlmostEqual(state.weights[0][1], round(init_w - 0.1, 2))

            state.add_or_update_weight("gpu.ref.score", 0.6)
            state.add_rule("cpu.socket == mobo.socket")
            state.add_rule("cpu.score / gpu.ref.score < 1")
            state.add_limit("sum(price) <= 950")
            state.add_formula("gpu_savings = price * 1.0")

            self.assertGreaterEqual(len(state.results), 1)
            top = state.results[0]
            self.assertEqual(top["slots"]["cpu"]["socket"], top["slots"]["mobo"]["socket"])
            self.assertLess(top["slots"]["cpu"]["score"] / top["slots"]["gpu"]["ref"]["score"], 1.0)
            self.assertLessEqual(top["metrics"]["sum(price) <= 950"], 950.0)

            # Verify agy-style rounded solution cards, weight bars, clamping [0..1], pink < 1/N%, & file discovery
            state.explain = True
            rendered = _render_result_lines(state, 90)
            self.assertTrue(any("╭─ #1 │ Score" in line for line in rendered))
            self.assertEqual(len(_format_weight_bar(0.5)), 10)
            self.assertEqual(_format_weight_bar(0.5), "█████░░░░░")
            self.assertEqual(_format_weight_bar(0.6), "██████░░░░")
            self.assertEqual(_format_weight_bar(0.15), "█▌░░░░░░░░")
            self.assertEqual(_compute_hold_interval(0), 0.080)
            self.assertEqual(_compute_hold_interval(2), 0.072)
            self.assertEqual(_compute_hold_interval(6), 0.065)
            self.assertEqual(_compute_hold_interval(20), 0.065)

            # Clamping within [0.0, 1.0] & direction toggle
            state.adjust_weight(0, 5.0)
            self.assertEqual(state.weights[0][1], 1.0)
            state.adjust_weight(0, -5.0)
            self.assertEqual(state.weights[0][1], 0.0)
            state.adjust_weight(0, 0.2)
            self.assertEqual(state.weights[0][1], 0.2)
            prev_dir = state.weight_dirs.get(state.weights[0][0], "max")
            state.toggle_weight_direction(0)
            self.assertNotEqual(state.weight_dirs.get(state.weights[0][0]), prev_dir)
            state.toggle_weight_direction(0)

            # Edit existing weight expression in place (e.g. price -> price / gpu.ref.msrp) + slot-prefixed formula
            state.update_weight_expression(0, f"{state.weights[0][0]} / gpu.ref.msrp")
            self.assertIn("gpu.ref.msrp", state.weights[0][0])
            state.add_formula("gpu_eff = gpu.ref.score / gpu.price")
            self.assertGreaterEqual(len(state.results), 1)

            ws_items = _build_workspace_items(state)
            self.assertTrue(any(it.action_id == "add_weight" for it in ws_items))
            self.assertTrue(any(it.action_id == "add_constraint" for it in ws_items))
            self.assertTrue(any(it.kind == "rule" for it in ws_items))

            # Pink starts where parameter weight share < 1/N% of total weight sum x
            state._set_weights_from_pairs([("a", 0.7), ("b", 0.2), ("c", 0.1)])  # N=3 -> 33.3%: a=70% (pos), b=20% (min/pink), c=10% (min/pink)
            w_items = [it for it in _build_workspace_items(state) if it.kind == "weight"]
            self.assertEqual([it.tone for it in w_items], ["pos", "min", "min"])

            discovered_files = discover_local_data_files(root)
            self.assertIn(db_path.resolve(), discovered_files)
            self.assertIn(gpu_ref.resolve(), discovered_files)

            # Cycle modes cleanly
            state.cycle_mode()
            self.assertEqual(state.mode, "basket")
            state.cycle_mode()
            self.assertEqual(state.mode, "rank")
            state.cycle_mode()
            self.assertEqual(state.mode, "solve")

            # Restore solve weights before exporting customized preset
            state._set_weights_from_pairs([("cpu.score", 0.3), ("gpu.ref.score", 0.6), ("price", -0.2)])
            state.re_solve()

            # Export customized TUI session back to preset JSON and reload it
            out_json = state.export_preset(root / "saved_rig.json", preset_name="saved_rig")
            loaded_cfg = load_preset(out_json)
            self.assertEqual(loaded_cfg.name, "saved_rig")
            self.assertIn("cpu.score / gpu.ref.score < 1", loaded_cfg.rules)
            res_exec = loaded_cfg.execute()
            self.assertEqual(len(res_exec["results"]), len(state.results))

    def test_save_and_hop_preferences_delete_dbs_and_field_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "user_cfg"
            csv_main = root / "gpus.csv"
            csv_main.write_text("title,price,vram\nCardA RTX 3080,380,10\nCardB RTX 4070,520,12\n", encoding="utf-8")
            csv_ref = root / "ref.csv"
            csv_ref.write_text("model,score,msrp\nRTX 3080,25200,699\nRTX 4070,28500,599\n", encoding="utf-8")

            with patch.dict(os.environ, {"KNAPSET_CONFIG_DIR": str(cfg_dir)}):
                state = TuiSessionState()
                state.load_from_source(str(csv_main))
                state.attach_reference("gpus", str(csv_ref), match_expr="title~model")
                self.assertIn("gpus_ref", state.references)
                self.assertIn("ref.score", state.available_columns)

                # Field hint wrapper preserves available columns across lines
                wrapped = _wrap_suggestion_lines(state.available_columns, 1, max_width=28, max_lines=4)
                self.assertGreaterEqual(len(wrapped), 1)
                self.assertTrue(any("[" in ln and "]" in ln for ln in wrapped))

                # Save profile A ("value"), tweak weights and save profile B ("max_perf"), then hop between them
                state.weights = [("ref.score", 0.5), ("price", -0.5)]
                state.re_solve()
                saved_path = state.save_preference_profile("value", persist=True)
                self.assertIsNotNone(saved_path)
                assert saved_path is not None and saved_path.is_file()

                state.weights = [("ref.score", 1.0)]
                state.re_solve()
                state.save_preference_profile("max_perf", persist=True)
                self.assertEqual(state.results[0]["row"]["title"], "CardB RTX 4070")

                # Hop back to "value" profile
                state.switch_profile("value")
                self.assertEqual(state.profile, "value")
                self.assertEqual(state.results[0]["row"]["title"], "CardA RTX 3080")

                # Cycle forward back to "max_perf"
                state.cycle_profile()
                self.assertEqual(state.profile, "max_perf")

                # Delete profile "value"
                state.delete_preference_profile("value")
                self.assertNotIn("value", state.saved_profiles)

                # Persisted preset is discoverable for future sessions
                discovered = discover_presets()
                self.assertIn(state.preset_name, discovered)

                # Detach Reference DB & remove slot DB cleanly
                ws_before = _build_workspace_items(state)
                self.assertTrue(any(it.kind == "ref" for it in ws_before))
                self.assertTrue(any(it.kind == "slot" for it in ws_before))

                state.detach_reference("gpus_ref")
                self.assertNotIn("gpus_ref", state.references)
                self.assertNotIn("ref.score", state.available_columns)

                state.remove_slot("gpus")
                self.assertEqual(state.sources, {})
                self.assertEqual(state.results, [])


class TestTuiSessionRestore(unittest.TestCase):
    def test_saved_preferences_restore_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "parts.csv"
            csv_path.write_text("name,price,score\nA,10,50\nB,20,90\nC,15,70\n", encoding="utf-8")
            with patch.dict(os.environ, {"KNAPSET_HOME": str(root / "home")}):
                first = TuiSessionState()
                first.load_from_source(str(csv_path), mode="rank")
                first.weights = [("score", 0.9), ("price", 0.1)]
                first.weight_dirs = {"score": "max", "price": "min"}
                first.save_preference_profile("My Prefs")

                # Plain re-open restores the full saved session
                second = TuiSessionState()
                second.load_from_source(str(csv_path), mode="rank")
                self.assertTrue(second.restore_saved_session())
                self.assertEqual(second.profile, "my_prefs")
                self.assertEqual(dict(second.weights)["score"], 0.9)
                self.assertFalse(second.dirty)

                # Overlay mode (explicit CLI overrides) keeps fresh slots but restores profiles
                third = TuiSessionState()
                third.load_from_source(str(csv_path), mode="rank")
                self.assertTrue(third.restore_saved_session(overlay_only=True))
                self.assertIn("my_prefs", third.saved_profiles)
                self.assertEqual(dict(third.weights)["score"], 0.9)

                # A different file with the same stem must not pick up the saved session
                other_dir = root / "other"
                other_dir.mkdir()
                other = other_dir / "parts.csv"
                other.write_text("name,price,score\nX,1,1\n", encoding="utf-8")
                fourth = TuiSessionState()
                fourth.load_from_source(str(other), mode="rank")
                self.assertFalse(fourth.restore_saved_session())


class TestSharedProfiles(unittest.TestCase):
    def test_profile_saved_once_reused_across_dbs_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_a = root / "shop_a.csv"
            db_b = root / "shop_b.csv"
            db_a.write_text("name,price,score\nA,10,50\nB,20,90\nC,15,70\n", encoding="utf-8")
            db_b.write_text("name,price,score,extra\nX,5,10,1\nY,9,80,2\n", encoding="utf-8")
            with patch.dict(os.environ, {"KNAPSET_HOME": str(root / "home")}):
                a = TuiSessionState()
                a.load_from_source(str(db_a), mode="rank")
                a.weights = [("score", 0.8), ("price", 0.2)]
                a.weight_dirs = {"score": "max", "price": "min"}
                a.save_preference_profile("Shared Prefs")
                self.assertTrue((root / "home" / "profiles" / "shared_prefs.json").is_file())

                # A different DB with the same column naming reuses it from the shared library
                b = TuiSessionState()
                b.load_from_source(str(db_b), mode="rank")
                self.assertEqual(b.apply_shared_profile("shared_prefs"), [])
                self.assertEqual(b.profile, "shared_prefs")
                self.assertEqual(dict(b.weights)["score"], 0.8)
                self.assertEqual(b.results[0]["row"]["name"], "Y")

                # Columns missing in the new DB are skipped, not fatal
                lone = root / "lone.csv"
                lone.write_text("name,price\nQ,1\nR,2\n", encoding="utf-8")
                c = TuiSessionState()
                c.load_from_source(str(lone), mode="rank")
                self.assertEqual(c.apply_shared_profile("shared_prefs"), ["score"])

                # Loading straight from a profile file and from a full preset file both work
                d = TuiSessionState()
                d.load_from_source(str(db_b), mode="rank")
                d.apply_shared_profile(str(root / "home" / "profiles" / "shared_prefs.json"), store=False)
                self.assertEqual(d.profile, "shared_prefs")
                preset_file = next((root / "home" / "presets").glob("*.json"))
                e = TuiSessionState()
                e.load_from_source(str(db_b), mode="rank")
                e.apply_shared_profile(str(preset_file), store=False)
                self.assertEqual(dict(e.weights)["score"], 0.8)

                with self.assertRaises(KeyError):
                    e.apply_shared_profile("does_not_exist")

    def test_apply_shared_profile_skips_filters_and_rules_using_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_rich = root / "rich.db"
            db_slim = root / "slim.db"
            for dbp, has_extra in ((db_rich, True), (db_slim, False)):
                conn = sqlite3.connect(str(dbp))
                if has_extra:
                    conn.execute("CREATE TABLE cpu (name TEXT, socket TEXT, is_bundle INTEGER, price REAL)")
                    conn.execute("CREATE TABLE mobo (name TEXT, socket TEXT, is_bundle INTEGER, price REAL)")
                    conn.execute("INSERT INTO cpu VALUES ('R5', 'AM4', 0, 100)")
                    conn.execute("INSERT INTO mobo VALUES ('B550', 'AM4', 0, 90)")
                else:
                    conn.execute("CREATE TABLE cpu (name TEXT, socket TEXT, price REAL)")
                    conn.execute("CREATE TABLE mobo (name TEXT, socket TEXT, price REAL)")
                    conn.execute("INSERT INTO cpu VALUES ('R5', 'AM4', 100)")
                    conn.execute("INSERT INTO mobo VALUES ('B550', 'AM4', 90)")
                conn.commit()
                conn.close()

            with patch.dict(os.environ, {"KNAPSET_HOME": str(root / "home")}):
                s1 = TuiSessionState()
                s1.load_from_source(str(db_rich))
                s1.add_where("is_bundle == 0")
                s1.add_rule("cpu.socket == mobo.socket")
                s1.save_preference_profile("rich_prof")

                s2 = TuiSessionState()
                s2.load_from_source(str(db_slim))
                skipped = s2.apply_shared_profile("rich_prof", store=False)
                self.assertIn("is_bundle", skipped)
                self.assertEqual(s2.where, [])
                self.assertEqual(s2.rules, ["cpu.socket == mobo.socket"])
                self.assertEqual(len(s2.results), 1)

    def test_suggest_weights_prefers_multi_slot_metrics_over_flags_and_duplicate_prices(self) -> None:
        from tui import _suggest_weights_from_slots

        pools = {
            "cpu": [
                {"id": 1, "price": 100, "price_uah": 4150, "is_new": 0, "is_bundle": 0, "cores": None},
                {"id": 2, "price": 150, "price_uah": 6200, "is_new": 1, "is_bundle": 1, "cores": 6},
                {"id": 3, "price": 190, "price_uah": 7900, "is_new": 1, "is_bundle": 0, "cores": 8},
            ],
            "gpu": [
                {"id": 1, "price": 200, "price_uah": 8300, "vram_gb": 8},
                {"id": 2, "price": 300, "price_uah": 12450, "vram_gb": 12},
            ],
            "psu": [
                {"id": 1, "price": 50, "wattage": 550},
                {"id": 2, "price": 80, "wattage": 750},
            ],
        }
        suggested = dict(_suggest_weights_from_slots(pools, mode="solve"))
        self.assertEqual(suggested["price"], -0.3)
        self.assertNotIn("price_uah", suggested)
        self.assertNotIn("cpu.is_bundle", suggested)
        self.assertEqual(suggested["cpu.cores"], 0.5)
        self.assertEqual(suggested["gpu.vram_gb"], 0.5)
        self.assertEqual(suggested["psu.wattage"], 0.5)

    def test_weight_trash_remove_restore_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "items.csv"
            p.write_text("name,price,score\nA,10,50\nB,20,90\n", encoding="utf-8")
            s = TuiSessionState()
            s.load_from_source(str(p), mode="rank")
            s._set_weights_from_pairs([("score", 0.7), ("price", -0.3)])

            s.remove_weight(1)  # removes `price` (-0.3 -> min)
            self.assertEqual([w[0] for w in s.weights], ["score"])
            self.assertEqual(s.deleted_weights, [("price", 0.3, "min")])
            ws = _build_workspace_items(s)
            self.assertTrue(any(it.action_id == "weight_trash" for it in ws))

            restored = s.restore_deleted_weight()
            self.assertEqual(restored, "price")
            self.assertEqual(s.deleted_weights, [])
            self.assertEqual(s.weight_dirs["price"], "min")
            self.assertEqual(dict(s.weights)["price"], 0.3)

            s.remove_weight(0)
            s.remove_weight(0)
            self.assertEqual(len(s.deleted_weights), 2)
            self.assertEqual(s.restore_all_deleted_weights(), 2)
            self.assertEqual(len(s.weights), 2)

            s.remove_weight(0)
            self.assertEqual(s.empty_weight_trash(), 1)
            self.assertEqual(s.deleted_weights, [])


if __name__ == "__main__":
    unittest.main()
