"""
Unit and CLI smoke tests for Knapset v2 Phase 1:
- sources.sqlite_source (read-only enforcement, multi-table/view inspection, SQL validation)
- sources.tabular_source (CSV, TSV, JSON, JSONL loading & scalar coercion)
- sources.registry (DataSourceRegistry custom schemes & auto-detection)
- enricher (exact, specificity-ordered token matching, regex, on_unmatched drop|keep)
- v2_cli (`inspect`, `init`, `compare`) via main.main()
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

import main as knapset_main
from enricher import MatchSpec, enrich_from_source, enrich_rows, normalize_match_text, parse_match_expr
from sources.base import BaseDataSource, SourceSchema, TableSchema, ColumnInfo
from sources.registry import DataSourceRegistry, inspect_source, open_source
from sources.sqlite_source import SQLiteDataSource, validate_readonly_sql
from sources.tabular_source import TabularDataSource


def _make_sample_sqlite(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE gpu (id INTEGER PRIMARY KEY, title TEXT, price REAL, vram INTEGER)")
        conn.execute(
            "INSERT INTO gpu (title, price, vram) VALUES "
            "('ASUS RTX 3080 Ti 12GB', 520.0, 12),"
            "('MSI RTX 3080 10GB', 390.0, 10),"
            "('Generic Mystery Card', 150.0, 4)"
        )
        conn.execute("CREATE TABLE cpu (id INTEGER PRIMARY KEY, name TEXT, price REAL, score INTEGER)")
        conn.execute(
            "INSERT INTO cpu (name, price, score) VALUES "
            "('Ryzen 5 5600', 120.0, 21500),"
            "('Ryzen 7 7800X3D', 360.0, 34200)"
        )
        conn.execute("CREATE VIEW affordable_gpu AS SELECT title, price, vram FROM gpu WHERE price < 500")
        conn.commit()
    finally:
        conn.close()


class TestSQLiteSource(unittest.TestCase):
    def test_validate_readonly_sql_allows_select_and_with(self) -> None:
        self.assertEqual(validate_readonly_sql("  SELECT * FROM gpu; "), "SELECT * FROM gpu")
        self.assertIn("WITH cte AS", validate_readonly_sql("WITH cte AS (SELECT 1) SELECT * FROM cte"))

    def test_validate_readonly_sql_rejects_mutations_and_multistatement(self) -> None:
        bad_queries = [
            "",
            "INSERT INTO gpu VALUES (99, 'x', 1, 1)",
            "UPDATE gpu SET price = 0",
            "DELETE FROM gpu",
            "DROP TABLE gpu",
            "PRAGMA writable_schema=ON",
            "ATTACH DATABASE '/tmp/x.db' AS x",
            "SELECT * FROM gpu; DELETE FROM gpu",
            "/* comment */ UPDATE gpu SET price = 0",
            "SELECT 1; SELECT 2",
        ]
        for q in bad_queries:
            with self.subTest(query=q):
                with self.assertRaises(ValueError):
                    validate_readonly_sql(q)

    def test_inspect_and_load_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "parts.db"
            _make_sample_sqlite(db_path)

            src = SQLiteDataSource(db_path)
            schema = src.inspect()
            self.assertEqual(schema.kind, "sqlite")
            self.assertEqual(schema.table_names(), ["cpu", "gpu", "affordable_gpu"])

            gpu_tbl = schema.get_table("gpu")
            self.assertIsNotNone(gpu_tbl)
            assert gpu_tbl is not None
            self.assertEqual(gpu_tbl.row_count, 3)
            self.assertIn("price", gpu_tbl.numeric_columns())
            self.assertIn("title", gpu_tbl.text_columns())

            rows = src.load_rows(table="gpu", limit=2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["price"], 520.0)

            view_rows = src.load_rows(table="affordable_gpu")
            self.assertEqual(len(view_rows), 2)

            filtered = src.load_rows(query="SELECT title, price FROM gpu WHERE vram >= 12")
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["title"], "ASUS RTX 3080 Ti 12GB")

            with self.assertRaises(ValueError):
                src.load_rows(query="DELETE FROM gpu")

            with self.assertRaises(ValueError):
                src.load_rows(table="does_not_exist")


class TestTabularSource(unittest.TestCase):
    def test_csv_tsv_json_jsonl_loading_and_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            csv_path = root / "items.csv"
            csv_path.write_text("name,price,in_stock\nWidget,12.5,true\nGadget,100,false\n", encoding="utf-8")

            tsv_path = root / "items.tsv"
            tsv_path.write_text("name\tprice\nAlpha\t10\nBeta\t20.5\n", encoding="utf-8")

            json_list_path = root / "items_list.json"
            json_list_path.write_text(json.dumps([{"model": "A", "msrp": 300}, {"model": "B", "msrp": 500}]), encoding="utf-8")

            json_col_path = root / "items_cols.json"
            json_col_path.write_text(
                json.dumps({"cards": [{"model": "X", "msrp": 100}, {"model": "Y", "msrp": 200}]}),
                encoding="utf-8",
            )

            jsonl_path = root / "items.jsonl"
            jsonl_path.write_text('{"id": 1, "val": 9.5}\n{"id": 2, "val": 4.2}\n', encoding="utf-8")

            csv_src = TabularDataSource(csv_path)
            csv_schema = csv_src.inspect()
            self.assertEqual(csv_schema.kind, "csv")
            csv_rows = csv_src.load_rows()
            self.assertEqual(csv_rows[0]["price"], 12.5)
            self.assertIs(csv_rows[0]["in_stock"], True)
            self.assertIs(csv_rows[1]["in_stock"], False)

            tsv_rows = TabularDataSource(tsv_path).load_rows()
            self.assertEqual(tsv_rows[1]["price"], 20.5)

            j_rows = TabularDataSource(json_list_path).load_rows()
            self.assertEqual(len(j_rows), 2)
            self.assertEqual(j_rows[0]["msrp"], 300)

            jc_rows = TabularDataSource(json_col_path).load_rows(table="cards")
            self.assertEqual(jc_rows, [{"model": "X", "msrp": 100}, {"model": "Y", "msrp": 200}])

            jl_rows = TabularDataSource(jsonl_path).load_rows(limit=1)
            self.assertEqual(jl_rows, [{"id": 1, "val": 9.5}])


class TestRegistry(unittest.TestCase):
    def test_custom_scheme_registration_and_builtin_open(self) -> None:
        class DummySource(BaseDataSource):
            def __init__(self, target: str) -> None:
                self.target = target

            def inspect(self, sample_rows: int = 200) -> SourceSchema:
                return SourceSchema(
                    location=self.target,
                    kind="dummy",
                    tables=[TableSchema(name="items", row_count=1, columns=[ColumnInfo(name="x", kind="number", non_null_count=1)])],
                )

            def load_rows(self, table=None, query=None, limit=None) -> List[Dict[str, Any]]:
                return [{"x": 42}]

        reg = DataSourceRegistry()
        reg.register_scheme("dummy", DummySource)
        self.assertIn("dummy", reg.list_schemes())
        src = reg.open("dummy://sample")
        self.assertEqual(src.load_rows(), [{"x": 42}])

        with tempfile.TemporaryDirectory() as tmp:
            csv_file = Path(tmp) / "data.csv"
            csv_file.write_text("a,b\n1,hello\n", encoding="utf-8")
            self.assertIsInstance(open_source(csv_file), TabularDataSource)
            schema = inspect_source(csv_file)
            self.assertEqual(schema.kind, "csv")


class TestEnricher(unittest.TestCase):
    def test_parse_match_expr(self) -> None:
        self.assertEqual(parse_match_expr("title~model"), ("title", "model", "token"))
        self.assertEqual(parse_match_expr("sku = id"), ("sku", "id", "exact"))
        self.assertEqual(parse_match_expr("col"), ("col", "col", None))
        self.assertIn("rtx", normalize_match_text("ASUS-RTX 3080_Ti!"))

    def test_token_specificity_exact_regex_and_on_unmatched(self) -> None:
        candidates = [
            {"title": "ASUS ROG RTX 3080 Ti 12GB", "price": 520},
            {"title": "MSI Ventus RTX 3080 10GB", "price": 390},
            {"title": "Unknown Adapter Card", "price": 40},
        ]
        references = [
            {"model": "RTX 3080", "score": 25000, "msrp": 699},
            {"model": "RTX 3080 Ti", "score": 27500, "msrp": 1199},
        ]

        res_drop = enrich_rows(
            candidates,
            references,
            MatchSpec(left_field="title", right_field="model", strategy="token", alias="ref", on_unmatched="drop"),
        )
        self.assertEqual(res_drop.total, 3)
        self.assertEqual(res_drop.matched, 2)
        self.assertEqual(res_drop.unmatched, 1)
        self.assertAlmostEqual(res_drop.match_rate, 2 / 3)
        self.assertEqual(res_drop.rows[0]["ref"]["model"], "RTX 3080 Ti")
        self.assertEqual(res_drop.rows[1]["ref"]["model"], "RTX 3080")
        self.assertEqual(res_drop.unmatched_samples, ["Unknown Adapter Card"])

        res_keep = enrich_rows(
            candidates,
            references,
            MatchSpec(left_field="title", right_field="model", strategy="token", alias="bench", on_unmatched="keep"),
        )
        self.assertEqual(len(res_keep.rows), 3)
        self.assertIsNone(res_keep.rows[2]["bench"])

        exact_res = enrich_rows(
            [{"sku": "RTX3080"}, {"sku": "OTHER"}],
            [{"id": "rtx3080", "msrp": 699}],
            MatchSpec(left_field="sku", right_field="id", strategy="exact", on_unmatched="drop"),
        )
        self.assertEqual(len(exact_res.rows), 1)
        self.assertEqual(exact_res.rows[0]["ref"]["msrp"], 699)

        regex_res = enrich_rows(
            [{"title": "Gigabyte GeForce RTX-4070 Super"}, {"title": "No match"}],
            [{"pattern": r"rtx[- ]?4070\s+super", "tier": "high"}],
            MatchSpec(left_field="title", right_field="pattern", strategy="regex", on_unmatched="drop"),
        )
        self.assertEqual(len(regex_res.rows), 1)
        self.assertEqual(regex_res.rows[0]["ref"]["tier"], "high")

    def test_enrich_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ref_csv = Path(tmp) / "ref.csv"
            ref_csv.write_text("model,score\nRTX 3080 Ti,27500\nRTX 3080,25000\n", encoding="utf-8")
            res = enrich_from_source(
                [{"title": "EVGA RTX 3080 Ti FTW3", "price": 510}],
                ref_csv,
                MatchSpec(left_field="title", right_field="model", strategy="token"),
            )
            self.assertEqual(res.matched, 1)
            self.assertEqual(res.rows[0]["ref"]["score"], 27500)

    def test_token_match_folds_cyrillic_homoglyphs(self) -> None:
        res = enrich_rows(
            [
                {"title": "Процесор Intel Core і7-8700K 4.7GHz"},
                {"title": "Відеокарта Gigabyte RTХ 4070 Ti 12GB"},
            ],
            [
                {"model": "i7-8700K", "score": 15900},
                {"model": "RTX 4070 Ti", "score": 31800},
            ],
            MatchSpec(left_field="title", right_field="model", strategy="token", on_unmatched="drop"),
        )
        self.assertEqual(res.matched, 2)
        self.assertEqual(res.rows[0]["ref"]["model"], "i7-8700K")
        self.assertEqual(res.rows[1]["ref"]["model"], "RTX 4070 Ti")


class TestV2Phase1CLI(unittest.TestCase):
    def _run_cli(self, argv: List[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            knapset_main.main(argv)
        return buf.getvalue()

    def test_inspect_init_and_compare_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "market.db"
            _make_sample_sqlite(db_path)

            ref_csv = root / "gpu_ref.csv"
            ref_csv.write_text("model,score,msrp\nRTX 3080 Ti,27500,1199\nRTX 3080,25000,699\n", encoding="utf-8")

            inspect_out = self._run_cli(["inspect", str(db_path), "--json"])
            schema_payload = json.loads(inspect_out)
            self.assertEqual(schema_payload["kind"], "sqlite")
            self.assertEqual([t["name"] for t in schema_payload["tables"]], ["cpu", "gpu", "affordable_gpu"])

            preset_path = root / "starter.json"
            init_out = self._run_cli(
                ["init", str(db_path), "--ref", str(ref_csv), "--match", "title~model", "--out", str(preset_path)]
            )
            self.assertIn("Wrote starter preset", init_out)
            preset_data = json.loads(preset_path.read_text(encoding="utf-8"))
            self.assertEqual(preset_data["schema_version"], "2.0")
            self.assertIn("gpu", preset_data["sources"])
            self.assertIn("gpu_ref_ref", preset_data["references"])

            compare_out = self._run_cli(
                [
                    "compare",
                    str(db_path),
                    "--table",
                    "gpu",
                    "--ref",
                    str(ref_csv),
                    "--match",
                    "title~model",
                    "--on-unmatched",
                    "drop",
                    "--json",
                ]
            )
            cmp_payload = json.loads(compare_out)
            self.assertEqual(cmp_payload["match"]["matched"], 2)
            self.assertEqual(cmp_payload["match"]["unmatched"], 1)
            self.assertEqual(cmp_payload["rows"][0]["ref"]["model"], "RTX 3080 Ti")


if __name__ == "__main__":
    unittest.main()
