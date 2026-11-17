"""
Unit tests for Knapset v2 TUI Session Controller (`tui.TuiSessionState`) & rendering helpers.
Tests weight `+`/`-` adjustments, reference attachment, custom formulas/limits/rules,
live re-solving, mode cycling, and saving customized presets without requiring a physical TTY.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from preset_loader import load_preset
from tui import TuiSessionState, _render_result_lines


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

            # Verify result lines formatting with explain toggled
            state.explain = True
            rendered = _render_result_lines(state, 90)
            self.assertTrue(any("Set #1" in line for line in rendered))

            # Cycle modes cleanly
            state.cycle_mode()
            self.assertEqual(state.mode, "basket")
            state.cycle_mode()
            self.assertEqual(state.mode, "rank")
            state.cycle_mode()
            self.assertEqual(state.mode, "solve")

            # Export customized TUI session back to preset JSON and reload it
            out_json = state.export_preset(root / "saved_rig.json", preset_name="saved_rig")
            loaded_cfg = load_preset(out_json)
            self.assertEqual(loaded_cfg.name, "saved_rig")
            self.assertIn("cpu.score / gpu.ref.score < 1", loaded_cfg.rules)
            res_exec = loaded_cfg.execute()
            self.assertEqual(len(res_exec["results"]), len(state.results))


if __name__ == "__main__":
    unittest.main()
