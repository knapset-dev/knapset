"""
Comprehensive unit, brute-force equivalence, and CLI tests for Knapset v2 Phase 2:
- formula_engine (SafeExpression, FormulaSpec, filter_rows, LimitSpec, RuleSpec, parse_weights)
- solver_engine (solve_rank, solve_basket, solve_sets + brute-force optimality equivalence)
- v2_cli (`rank`, `basket`, `solve`, and `.json`/`.csv`/`.md` exports)
"""

from __future__ import annotations

import contextlib
import csv
import io
import itertools
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import main as knapset_main
from formula_engine import (
    LimitSpec,
    RuleSpec,
    SafeExpression,
    apply_row_formulas,
    filter_rows,
    parse_formulas,
    parse_weights,
)
from solver_engine import score_rows_normalized, solve_basket, solve_rank, solve_sets


class TestFormulaEngine(unittest.TestCase):
    def test_safe_expression_and_sandbox_rejection(self) -> None:
        expr = SafeExpression("(price - ref.msrp) / ref.msrp * 100")
        self.assertAlmostEqual(expr.evaluate({"price": 500, "ref": {"msrp": 1000}}), -50.0)
        self.assertIsNone(expr.evaluate({"price": 500, "ref": {"msrp": 0}}))

        cond = SafeExpression("'premium' if score >= 80 else 'budget'")
        self.assertEqual(cond.evaluate({"score": 85}), "premium")
        self.assertEqual(cond.evaluate({"score": 40}), "budget")

        fn_expr = SafeExpression("round(clamp(val, 0, 10) + sqrt(16), 2)")
        self.assertAlmostEqual(fn_expr.evaluate({"val": 25}), 14.0)

        for bad in ("__import__('os').system('id')", "open('/etc/passwd')", "x.__class__", "lambda a: a"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    SafeExpression(bad)

    def test_formulas_filters_limits_rules_and_weights(self) -> None:
        rows = [
            {"title": "Card A", "price": 400, "vram": 12, "ref": {"score": 24000, "msrp": 600}},
            {"title": "Card B", "price": 500, "vram": 8, "ref": {"score": 25000, "msrp": 500}},
            {"title": "Card C", "price": 300, "vram": 16, "ref": {"score": 21000, "msrp": 450}},
        ]
        formulas = parse_formulas([
            "perf_per_dollar = ref.score / price",
            "discount_pct = (ref.msrp - price) / ref.msrp * 100",
        ])
        out = apply_row_formulas(rows, formulas)
        self.assertAlmostEqual(out[0]["perf_per_dollar"], 60.0)
        self.assertAlmostEqual(out[2]["discount_pct"], 33.333333, places=4)

        filtered = filter_rows(out, ["vram >= 12", "discount_pct >= 20"])
        self.assertEqual([r["title"] for r in filtered], ["Card A", "Card C"])

        lim_sum = LimitSpec.parse("sum(price) <= 750")
        ok, total = lim_sum.evaluate_items(filtered)
        self.assertTrue(ok)
        self.assertAlmostEqual(total, 700.0)

        lim_avg = LimitSpec.parse("avg(vram) >= 14")
        ok_avg, avg_v = lim_avg.evaluate_items(filtered)
        self.assertTrue(ok_avg)
        self.assertAlmostEqual(avg_v, 14.0)

        rule = RuleSpec.parse("cpu.socket == mobo.socket and cpu.score / gpu.score < 1")
        self.assertEqual(set(rule.slots_involved), {"cpu", "mobo", "gpu"})
        self.assertFalse(rule.is_evaluable({"cpu": {"socket": "AM5"}}))
        self.assertTrue(
            rule.evaluate(
                {
                    "cpu": {"socket": "AM5", "score": 20000},
                    "mobo": {"socket": "AM5"},
                    "gpu": {"score": 25000},
                }
            )
        )

        terms = parse_weights("ref.score:+0.6, price:-0.4, -vram")
        self.assertEqual([(t.expression, t.weight) for t in terms], [("ref.score", 0.6), ("price", -0.4), ("vram", -1.0)])


class TestSolverEngine(unittest.TestCase):
    def test_solve_rank_normalization_and_explain(self) -> None:
        rows = [
            {"name": "Best Value", "score": 90, "price": 100},
            {"name": "Overpriced", "score": 90, "price": 400},
            {"name": "Weak", "score": 60, "price": 100},
            {"name": "Floor", "score": 30, "price": 100},
            {"name": "Missing", "score": None, "price": 100},
        ]
        ranked, meta = solve_rank(rows, weights="score:+0.6, price:-0.4", top_n=5)
        self.assertEqual(meta["returned"], 5)
        self.assertEqual(ranked[0].row["name"], "Best Value")
        self.assertEqual(ranked[0].score, 100.0)  # max score=90 -> 60, min price=100 -> 40
        self.assertEqual(len(ranked[0].breakdown), 2)
        self.assertEqual(ranked[-1].breakdown[0].raw_value, None)
        self.assertEqual(ranked[-1].score, 40.0)  # score=None -> 0, price=100 -> 40

    def test_solve_basket_multi_limit_matches_bruteforce(self) -> None:
        items = [
            {"name": "Item1", "val": 90, "price": 40, "weight_g": 200, "qty_cap": 2},
            {"name": "Item2", "val": 80, "price": 30, "weight_g": 150, "qty_cap": 1},
            {"name": "Item3", "val": 70, "price": 25, "weight_g": 120, "qty_cap": 2},
            {"name": "Item4", "val": 55, "price": 20, "weight_g": 90, "qty_cap": 1},
            {"name": "Item5", "val": 60, "price": 35, "weight_g": 100, "qty_cap": 1},
            {"name": "Item6", "val": 40, "price": 10, "weight_g": 60, "qty_cap": 2},
        ]
        weights = "val:+0.7, price:-0.3"
        limits = ["sum(price) <= 95", "sum(weight_g) <= 450", "count <= 4"]

        sols, meta = solve_basket(
            items,
            weights=weights,
            limits=limits,
            top_n=3,
            max_per_item=2,
            qty_field="qty_cap",
        )
        self.assertTrue(sols)
        self.assertFalse(meta["truncated"])

        # Brute-force enumerate all bounded combinations
        terms = parse_weights(weights)
        unit_scores, _ = score_rows_normalized(items, terms)
        lim_specs = [LimitSpec.parse(l) for l in limits]
        best_bf_util = -1.0
        ranges = [range(it["qty_cap"] + 1) for it in items]
        for counts in itertools.product(*ranges):
            if sum(counts) == 0:
                continue
            if all(lim.evaluate_items(items, counts=list(counts))[0] for lim in lim_specs):
                u = sum(sc * q for sc, q in zip(unit_scores, counts))
                if u > best_bf_util:
                    best_bf_util = u

        self.assertAlmostEqual(sols[0].total_score, round(best_bf_util, 2), places=2)
        for sol in sols:
            self.assertLessEqual(sol.metrics["sum(price) <= 95"], 95.0)
            self.assertLessEqual(sol.metrics["sum(weight_g) <= 450"], 450.0)
            self.assertLessEqual(sol.metrics["count <= 4"], 4.0)

    def test_solve_sets_rules_limits_and_bruteforce_equivalence(self) -> None:
        slot_pools = {
            "cpu": [
                {"name": "Ryzen 5 5600", "socket": "AM4", "price": 120, "score": 21500, "tdp": 65},
                {"name": "Ryzen 7 7800X3D", "socket": "AM5", "price": 360, "score": 34200, "tdp": 120},
                {"name": "Intel i5-12400", "socket": "LGA1700", "price": 150, "score": 19500, "tdp": 65},
            ],
            "mobo": [
                {"name": "B550 Board", "socket": "AM4", "price": 110},
                {"name": "B650 Board", "socket": "AM5", "price": 190},
            ],
            "gpu": [
                {"name": "RTX 3080", "price": 390, "score": 25000, "tdp": 320},
                {"name": "RX 6700 XT", "price": 260, "score": 20000, "tdp": 230},
                {"name": "RTX 4070", "price": 520, "score": 29000, "tdp": 200},
            ],
            "psu": [
                {"name": "550W Bronze", "wattage": 550, "price": 55},
                {"name": "750W Gold", "wattage": 750, "price": 95},
            ],
        }
        rules = [
            "cpu.socket == mobo.socket",
            "psu.wattage >= (cpu.tdp + gpu.tdp) * 1.25",
            "cpu.score / gpu.score < 1",
        ]
        limits = ["sum(price) <= 850"]
        weights = "cpu.score:+0.3, gpu.score:+0.5, price:-0.2"

        sols, meta = solve_sets(slot_pools, weights=weights, limits=limits, rules=rules, top_n=5)
        self.assertTrue(sols)
        self.assertFalse(meta["truncated"])

        # Intel i5 must never win since no LGA1700 mobo exists
        for sol in sols:
            self.assertEqual(sol.slots["cpu"]["socket"], sol.slots["mobo"]["socket"])
            self.assertLess(sol.slots["cpu"]["score"] / sol.slots["gpu"]["score"], 1.0)
            total_price = sum(item["price"] for item in sol.slots.values())
            self.assertLessEqual(total_price, 850.0)
            self.assertGreaterEqual(
                sol.slots["psu"]["wattage"],
                (sol.slots["cpu"]["tdp"] + sol.slots["gpu"]["tdp"]) * 1.25,
            )

        # Verify against brute-force count of feasible combinations
        r_specs = [RuleSpec.parse(r) for r in rules]
        l_specs = [LimitSpec.parse(l) for l in limits]
        bf_count = 0
        for combo in itertools.product(slot_pools["cpu"], slot_pools["mobo"], slot_pools["gpu"], slot_pools["psu"]):
            cand = {"cpu": combo[0], "mobo": combo[1], "gpu": combo[2], "psu": combo[3]}
            if all(r.evaluate(cand) for r in r_specs) and all(l.evaluate_set(cand)[0] for l in l_specs):
                bf_count += 1
        self.assertEqual(meta["feasible_count"], bf_count)


class TestV2Phase2CLI(unittest.TestCase):
    def _run_cli(self, argv: List[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            knapset_main.main(argv)
        return buf.getvalue()

    def test_rank_basket_solve_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "pc.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE cpu (name TEXT, socket TEXT, price REAL, score INTEGER, tdp INTEGER)")
                conn.executemany(
                    "INSERT INTO cpu VALUES (?, ?, ?, ?, ?)",
                    [
                        ("Ryzen 5 5600", "AM4", 120.0, 21500, 65),
                        ("Ryzen 7 7800X3D", "AM5", 360.0, 34200, 120),
                    ],
                )
                conn.execute("CREATE TABLE gpu (title TEXT, price REAL, tdp INTEGER)")
                conn.executemany(
                    "INSERT INTO gpu VALUES (?, ?, ?)",
                    [
                        ("ASUS RTX 3080 Ti", 520.0, 350),
                        ("MSI RTX 3080", 380.0, 320),
                    ],
                )
                conn.execute("CREATE TABLE mobo (name TEXT, socket TEXT, price REAL)")
                conn.executemany(
                    "INSERT INTO mobo VALUES (?, ?, ?)",
                    [
                        ("B550", "AM4", 110.0),
                        ("B650", "AM5", 190.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            gpu_ref = root / "gpu_ref.csv"
            gpu_ref.write_text("model,score,msrp\nRTX 3080 Ti,27500,1199\nRTX 3080,25000,699\n", encoding="utf-8")

            # 1. ks rank + --ref + --formula + --export .csv
            csv_export = root / "ranked.csv"
            rank_out = self._run_cli(
                [
                    "rank",
                    str(db_path),
                    "--table",
                    "gpu",
                    "--ref",
                    str(gpu_ref),
                    "--match",
                    "title~model",
                    "--formula",
                    "val = ref.score / price",
                    "--where",
                    "price <= 600",
                    "--prefer",
                    "val:+0.8, price:-0.2",
                    "--json",
                    "--export",
                    str(csv_export),
                ]
            )
            rank_data = json.loads(rank_out)
            self.assertEqual(len(rank_data["results"]), 2)
            self.assertTrue(csv_export.is_file())
            with csv_export.open("r", encoding="utf-8") as f:
                csv_rows = list(csv.DictReader(f))
            self.assertEqual(len(csv_rows), 2)
            self.assertIn("ref.score", csv_rows[0])

            # 2. ks basket + --export .md
            basket_csv = root / "snacks.csv"
            basket_csv.write_text(
                "name,protein,price,weight_g\nBarA,20,2.5,60\nShakeB,30,4.0,350\nNutsC,15,3.0,100\n",
                encoding="utf-8",
            )
            md_export = root / "basket.md"
            basket_out = self._run_cli(
                [
                    "basket",
                    str(basket_csv),
                    "--limit",
                    "sum(price) <= 7.0",
                    "--limit",
                    "sum(protein) >= 45",
                    "--prefer",
                    "protein:+0.8, price:-0.2",
                    "--json",
                    "--export",
                    str(md_export),
                ]
            )
            basket_data = json.loads(basket_out)
            self.assertGreaterEqual(len(basket_data["solutions"]), 1)
            self.assertTrue(md_export.is_file())
            self.assertIn("# Knapset Basket", md_export.read_text(encoding="utf-8"))

            # 3. ks solve + --slot ref + --rule + --export .json
            solve_json_export = root / "builds.json"
            solve_out = self._run_cli(
                [
                    "solve",
                    str(db_path),
                    "--tables",
                    "cpu,mobo,gpu",
                    "--ref",
                    f"{gpu_ref}:slot=gpu,match=title~model,alias=ref",
                    "--rule",
                    "cpu.socket == mobo.socket",
                    "--rule",
                    "cpu.score / gpu.ref.score < 1.0",
                    "--limit",
                    "sum(price) <= 700",
                    "--prefer",
                    "cpu.score:+0.3, gpu.ref.score:+0.5, price:-0.2",
                    "--json",
                    "--export",
                    str(solve_json_export),
                ]
            )
            solve_data = json.loads(solve_out)
            self.assertEqual(len(solve_data["solutions"]), 1)
            top_sol = solve_data["solutions"][0]
            self.assertEqual(top_sol["slots"]["cpu"]["name"], "Ryzen 5 5600")
            self.assertEqual(top_sol["slots"]["mobo"]["name"], "B550")
            self.assertEqual(top_sol["slots"]["gpu"]["title"], "MSI RTX 3080")
            self.assertTrue(solve_json_export.is_file())

    def test_solve_sets_preserves_compatibility_groups_under_large_pool_pruning(self) -> None:
        # 80 ultra-cheap LGA1150 boards should not crowd out AM4 boards under pareto pruning
        cpus = [
            {"name": "Celeron G1840", "socket": "LGA1150", "score": 30, "price": 10},
            {"name": "Ryzen 7 5800X3D", "socket": "AM4", "score": 95, "price": 200},
        ]
        mobos = [{"name": f"Old H81 #{i}", "socket": "LGA1150", "price": 15 + i * 0.1} for i in range(80)]
        mobos.append({"name": "B550 Tomahawk", "socket": "AM4", "price": 110})

        sols, _ = solve_sets(
            slot_pools={"cpu": cpus, "mobo": mobos},
            weights="cpu.score:+0.8, price:-0.2",
            limits=["sum(price) <= 350"],
            rules=["cpu.socket == mobo.socket"],
            top_n=3,
        )
        self.assertGreaterEqual(len(sols), 1)
        self.assertEqual(sols[0].slots["cpu"]["name"], "Ryzen 7 5800X3D")
        self.assertEqual(sols[0].slots["mobo"]["name"], "B550 Tomahawk")

    def test_solve_sets_prunes_unaffordable_candidates_before_pareto_cap(self) -> None:
        # 50 over-budget GPUs should not crowd out mid-range GPUs that fit the budget
        gpus = [{"name": f"Ultra GPU #{i}", "fps": 200 + i, "price": 1500 + i} for i in range(50)]
        gpus.extend(
            [
                {"name": "Mid GPU", "fps": 120, "price": 400},
                {"name": "Low GPU", "fps": 40, "price": 50},
            ]
        )
        cpus = [{"name": "Main CPU", "fps": 100, "price": 150}]
        sols, _ = solve_sets(
            slot_pools={"gpu": gpus, "cpu": cpus},
            weights="gpu.fps:+0.8, price:-0.2",
            limits=["sum(price) <= 600"],
            top_n=2,
        )
        self.assertGreaterEqual(len(sols), 1)
        self.assertEqual(sols[0].slots["gpu"]["name"], "Mid GPU")

    def test_normalization_modes_robust_minmax_rank_outlier_resilience(self) -> None:
        # One extreme outlier (price=100000) compresses normal prices under minmax,
        # whereas robust (P5-P95) and rank preserve price separation among normal items.
        rows = [
            {"name": "GreatDeal", "score": 82, "price": 100},
            {"name": "OverpricedNormal", "score": 85, "price": 500},
            {"name": "Mid1", "score": 70, "price": 200},
            {"name": "Mid2", "score": 72, "price": 220},
            {"name": "Mid3", "score": 74, "price": 240},
            {"name": "Mid4", "score": 76, "price": 260},
            {"name": "Mid5", "score": 78, "price": 280},
            {"name": "Mid6", "score": 79, "price": 300},
            {"name": "TypoOutlier", "score": 50, "price": 100000},
        ]
        mm_ranked, mm_meta = solve_rank(rows, weights="score:+0.5, price:-0.5", norm="minmax")
        rb_ranked, rb_meta = solve_rank(rows, weights="score:+0.5, price:-0.5", norm="robust")
        rk_ranked, rk_meta = solve_rank(rows, weights="score:+0.5, price:-0.5", norm="rank")

        self.assertEqual(mm_meta["norm"], "minmax")
        self.assertEqual(rb_meta["norm"], "robust")
        self.assertEqual(rk_meta["norm"], "rank")
        # Under minmax, price=100 vs 500 barely differs due to 100000 outlier -> OverpricedNormal wins
        self.assertEqual(mm_ranked[0].row["name"], "OverpricedNormal")
        # Under robust and rank, GreatDeal wins cleanly
        self.assertEqual(rb_ranked[0].row["name"], "GreatDeal")
        self.assertEqual(rk_ranked[0].row["name"], "GreatDeal")

    def test_infeasibility_diagnostics_for_where_limit_and_rule(self) -> None:
        # 1. Where filter drops all rows
        _, meta_w = solve_rank([{"name": "A", "price": 500}], weights="price:-1", where=["price <= 100"])
        self.assertIn("diagnostics", meta_w)
        self.assertIn("price <= 100", meta_w["diagnostics"]["summary"])

        # 2. Limit impossible in solve_sets
        _, meta_l = solve_sets(
            slot_pools={"a": [{"name": "A", "price": 300}], "b": [{"name": "B", "price": 400}]},
            weights="price:-1",
            limits=["sum(price) <= 500"],
        )
        self.assertIn("diagnostics", meta_l)
        self.assertIn("700", meta_l["diagnostics"]["summary"])

        # 3. Rule blocks all combinations
        _, meta_r = solve_sets(
            slot_pools={
                "cpu": [{"name": "C1", "socket": "AM5", "price": 200}],
                "mobo": [{"name": "M1", "socket": "LGA1700", "price": 150}],
            },
            weights="price:-1",
            rules=["cpu.socket == mobo.socket"],
        )
        self.assertIn("diagnostics", meta_r)
        self.assertIn("cpu.socket == mobo.socket", meta_r["diagnostics"]["summary"])

    def test_solve_sets_diverse_avoids_near_duplicate_sets(self) -> None:
        cpus = [{"name": "CPU1", "score": 100, "price": 200}, {"name": "CPU2", "score": 90, "price": 180}]
        gpus = [{"name": "GPU1", "score": 100, "price": 300}, {"name": "GPU2", "score": 92, "price": 280}]
        psus = [{"name": f"PSU{i}", "score": 50 - i, "price": 60 + i} for i in range(5)]

        normal, _ = solve_sets(
            slot_pools={"cpu": cpus, "gpu": gpus, "psu": psus},
            weights="cpu.score:+0.4, gpu.score:+0.4, psu.score:+0.2",
            top_n=3,
            diverse=False,
        )
        diverse, meta_d = solve_sets(
            slot_pools={"cpu": cpus, "gpu": gpus, "psu": psus},
            weights="cpu.score:+0.4, gpu.score:+0.4, psu.score:+0.2",
            top_n=3,
            diverse=True,
        )
        self.assertTrue(meta_d["diverse"])
        # Without diverse, top 3 all share CPU1 + GPU1 and only swap PSU
        self.assertTrue(all(s.slots["cpu"]["name"] == "CPU1" and s.slots["gpu"]["name"] == "GPU1" for s in normal))
        # With diverse, #2 must differ in at least 2 slots from #1
        diffs = sum(1 for k in ("cpu", "gpu", "psu") if diverse[0].slots[k]["name"] != diverse[1].slots[k]["name"])
        self.assertGreaterEqual(diffs, 2)

    def test_new_formula_helpers(self) -> None:
        self.assertEqual(SafeExpression("if_else(price < 100, 'cheap', 'pricy')").evaluate({"price": 50}), "cheap")
        self.assertTrue(SafeExpression("startswith(name, 'rtx')").evaluate({"name": "RTX 4070"}))
        self.assertTrue(SafeExpression("endswith(name, 'ti')").evaluate({"name": "RTX 4070 Ti"}))
        self.assertTrue(SafeExpression("regex_match(name, '40[78]0')").evaluate({"name": "RTX 4080"}))
        self.assertAlmostEqual(SafeExpression("pct_diff(80, 100)").evaluate({}), -20.0)

    def test_pin_exclude_and_sensitivity_analysis(self) -> None:
        from solver_engine import analyze_sensitivity

        # 1. Exclude in rank
        items = [
            {"name": "Alpha", "score": 95, "price": 100},
            {"name": "Beta", "score": 85, "price": 100},
            {"name": "Gamma", "score": 70, "price": 100},
        ]
        ranked, _ = solve_rank(items, weights="score:+1.0", exclude=["Alpha"])
        self.assertEqual(ranked[0].row["name"], "Beta")

        # 2. Pin & exclude in basket
        snacks = [
            {"name": "Bar", "protein": 20, "price": 2},
            {"name": "Shake", "protein": 30, "price": 4},
            {"name": "Nuts", "protein": 10, "price": 3},
        ]
        baskets, _ = solve_basket(snacks, weights="protein:+1.0", limits=["sum(price) <= 5"], pin=["Nuts"])
        self.assertGreaterEqual(len(baskets), 1)
        names_in_top = {it["name"] for it in baskets[0].items}
        self.assertIn("Nuts", names_in_top)

        # 3. Pin & exclude in solve_sets
        cpus = [{"name": "Ryzen 7", "score": 100, "price": 300}, {"name": "Ryzen 5", "score": 80, "price": 180}]
        gpus = [{"name": "RTX 4080", "score": 100, "price": 600}, {"name": "RTX 4070", "score": 85, "price": 400}]
        sets_pinned, _ = solve_sets(
            slot_pools={"cpu": cpus, "gpu": gpus},
            weights="cpu.score:+0.5, gpu.score:+0.5",
            pin={"cpu": "Ryzen 5"},
            exclude=["gpu:4080"],
        )
        self.assertEqual(len(sets_pinned), 1)
        self.assertEqual(sets_pinned[0].slots["cpu"]["name"], "Ryzen 5")
        self.assertEqual(sets_pinned[0].slots["gpu"]["name"], "RTX 4070")

        # 4. Sensitivity report
        rep = analyze_sensitivity(
            "solve",
            slot_pools={"cpu": cpus, "gpu": gpus},
            weights="cpu.score:+0.4, gpu.score:+0.4, price:-0.2",
            limits=["sum(price) <= 750"],
        )
        self.assertIsNotNone(rep["baseline"])
        self.assertGreater(len(rep["weight_perturbations"]), 0)
        self.assertGreater(len(rep["limit_perturbations"]), 0)

        # 5. CLI ks sensitivity --json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "items.csv"
            p.write_text("name,score,price\nA,90,100\nB,85,80\nC,70,50\n", encoding="utf-8")
            out = self._run_cli(["sensitivity", str(p), "--prefer", "score:+0.5,price:-0.5", "--json"])
            data = json.loads(out)
            self.assertEqual(data["mode"], "rank")
            self.assertIsNotNone(data["baseline"])

    def test_basket_explain_unknown_term_hint_and_pareto_frontier(self) -> None:
        from solver_engine import compute_pareto_frontier

        items = [
            {"name": "A", "score": 60, "price": 50},
            {"name": "B", "score": 85, "price": 120},
            {"name": "C", "score": 98, "price": 250},
        ]
        # 1. Unknown column suggestion warning
        _, meta_w = solve_rank(items, weights="scroe:+1.0")
        self.assertTrue(any("score" in w for w in meta_w.get("warnings", [])))

        # 2. Basket breakdown
        sols, _ = solve_basket(items, weights="score:+0.7, price:-0.3", limits=["sum(price) <= 200"])
        self.assertGreaterEqual(len(sols), 1)
        self.assertEqual(len(sols[0].breakdown), 2)
        self.assertIn("breakdown", sols[0].to_dict())

        # 3. Pareto frontier
        fr = compute_pareto_frontier(
            "rank",
            rows=items,
            weights="score:+0.8, price:-0.2",
            cost_expr="price",
            budgets=[60, 150, 300],
        )
        pts = fr["frontier"]
        self.assertGreaterEqual(len(pts), 2)
        self.assertTrue(all(pts[i]["score"] < pts[i + 1]["score"] for i in range(len(pts) - 1)))

        # 4. CLI ks frontier --json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "items.csv"
            p.write_text("name,score,price\nA,60,50\nB,85,120\nC,98,250\n", encoding="utf-8")
            out = self._run_cli(["frontier", str(p), "--prefer", "score:+0.8,price:-0.2", "--budgets", "60,150,300", "--json"])
            data = json.loads(out)
            self.assertGreaterEqual(len(data["frontier"]), 2)


if __name__ == "__main__":
    unittest.main()
