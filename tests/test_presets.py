"""
Tests for Knapset v2 Phase 3:
- preset_loader (JSON/TOML preset schema, cross-DB refs, profiles, CLI overrides, save_preset, discover_presets)
- Optional Python plugins (BaseRowNormalizer, BaseScorer, BaseDataSource URI scheme registration)
- End-to-end execution of `knapset-registry` presets (`pc_build`, `fishing_tackle`)
- CLI `ks run` and `ks preset` (`list`, `search`, `install`, `verify`, `remove`)
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import List

import main as knapset_main
from marketplace.client import MarketplaceClient
from preset_loader import PresetConfig, discover_presets, load_preset, resolve_preset, save_preset


REGISTRY_DIR = Path(__file__).resolve().parent.parent.parent / "knapset-registry"


class TestPresetLoaderAndPlugins(unittest.TestCase):
    def test_preset_load_save_execute_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items_csv = root / "gpus.csv"
            items_csv.write_text(
                "title,price,vram\nASUS RTX 3080 Ti,520,12\nMSI RTX 3080,380,10\n",
                encoding="utf-8",
            )
            ref_csv = root / "gpu_ref.csv"
            ref_csv.write_text(
                "model,score,msrp\nRTX 3080 Ti,27500,1199\nRTX 3080,25000,699\n",
                encoding="utf-8",
            )

            preset_data = {
                "schema_version": "2.0",
                "name": "gpu_deals",
                "title": "GPU Deal Finder",
                "mode": "rank",
                "sources": {"gpus": {"source": "gpus.csv", "ref": "gpu_ref"}},
                "references": {
                    "gpu_ref": {
                        "source": "gpu_ref.csv",
                        "match": "title~model",
                        "strategy": "token",
                        "alias": "ref",
                        "on_unmatched": "drop",
                    }
                },
                "formulas": {"perf_per_dollar": "ref.score / price"},
                "profiles": {
                    "value": {"weights": {"perf_per_dollar": 0.7, "price": -0.3}},
                    "raw_perf": {"weights": {"ref.score": 1.0}},
                },
            }
            preset_path = save_preset(preset_data, root / "gpu_deals.json")
            cfg = load_preset(preset_path)
            self.assertEqual(cfg.profile_names(), ["value", "raw_perf"])

            res_val = cfg.execute(profile_name="value")
            self.assertEqual(res_val["results"][0]["row"]["title"], "MSI RTX 3080")

            res_perf = cfg.execute(profile_name="raw_perf")
            self.assertEqual(res_perf["results"][0]["row"]["title"], "ASUS RTX 3080 Ti")

            # CLI-style override flips preference back strongly toward low price / high vram
            res_over = cfg.execute(profile_name="raw_perf", prefer_override="vram:+0.9, price:-0.1", where_override=["vram >= 12"])
            self.assertEqual(len(res_over["results"]), 1)
            self.assertEqual(res_over["results"][0]["row"]["title"], "ASUS RTX 3080 Ti")

    def test_optional_python_plugin_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items_csv = root / "parts.csv"
            items_csv.write_text("name,price,score\nAlpha,100,80\nBeta,150,95\n", encoding="utf-8")

            plugin_py = root / "custom_hook.py"
            plugin_py.write_text(
                "\n".join(
                    [
                        "from preset_loader import BaseRowNormalizer, BaseScorer",
                        "from sources.base import BaseDataSource, SourceSchema",
                        "",
                        "class AlphaBooster(BaseRowNormalizer):",
                        "    def normalize(self, slot_name, rows):",
                        "        out = []",
                        "        for r in rows:",
                        "            c = dict(r)",
                        "            if c.get('name') == 'Alpha':",
                        "                c['bonus'] = 50",
                        "            else:",
                        "                c['bonus'] = 0",
                        "            out.append(c)",
                        "        return out",
                        "",
                        "class BonusScorer(BaseScorer):",
                        "    def adjust_solution(self, mode, sol):",
                        "        row = sol.get('row', {})",
                        "        return float(row.get('bonus', 0))",
                        "",
                        "class MemDemoSource(BaseDataSource):",
                        "    scheme = 'memdemo'",
                        "    def __init__(self, uri): self.uri = uri",
                        "    def inspect(self, sample_rows=200):",
                        "        return SourceSchema(location=self.uri, kind='memdemo', tables=[])",
                        "    def load_rows(self, table=None, query=None, limit=None):",
                        "        return [{'name': 'MemRow', 'price': 10, 'score': 99}]",
                    ]
                ),
                encoding="utf-8",
            )

            preset_path = save_preset(
                {
                    "schema_version": "2.0",
                    "name": "plugin_demo",
                    "mode": "rank",
                    "sources": {"parts": {"source": "parts.csv"}},
                    "plugins": ["custom_hook.py"],
                    "profiles": {"std": {"weights": {"score": 1.0}}},
                },
                root / "plugin_demo.json",
            )
            res = load_preset(preset_path).execute()
            # Alpha gets score=0 normalized (80 vs 95), +50 bonus -> 50.0 vs Beta=100.0
            by_name = {r["row"]["name"]: r for r in res["results"]}
            self.assertEqual(by_name["Alpha"]["row"]["bonus"], 50)
            self.assertEqual(by_name["Alpha"]["score"], 50.0)


class TestPresetCLIAndRegistry(unittest.TestCase):
    def _run_cli(self, argv: List[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            knapset_main.main(argv)
        return buf.getvalue()

    def test_execute_registry_v2_presets_and_single_file_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "knapset_home"
            reg_dir = root / "mock_reg"
            (reg_dir / "modules" / "presets").mkdir(parents=True)

            single_preset = {
                "schema_version": "2.0",
                "name": "tiny_rank",
                "title": "Tiny Rank Preset",
                "mode": "rank",
                "sources": {},
                "profiles": {"fast": {"weights": {"score": 1.0}}},
            }
            preset_bytes = json.dumps(single_preset).encode("utf-8")
            (reg_dir / "modules" / "presets" / "tiny_rank.json").write_bytes(preset_bytes)
            sha = hashlib.sha256(preset_bytes).hexdigest()
            (reg_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "modules": [
                            {
                                "name": "tiny_rank",
                                "subsystem": "presets",
                                "domain": "general",
                                "version": "2.0.0",
                                "author": "test",
                                "description": "Single JSON preset test",
                                "source_type": "single_file",
                                "download_url": "modules/presets/tiny_rank.json",
                                "sha256": sha,
                                "files": [],
                            }
                        ],
                        "bundles": [],
                    }
                ),
                encoding="utf-8",
            )

            old_home = os.environ.get("KNAPSET_HOME")
            os.environ["KNAPSET_HOME"] = str(home_dir)
            try:
                client = MarketplaceClient(registry_url_or_path=str(reg_dir), root_dir=home_dir)
                ok, msg = client.install("tiny_rank")
                self.assertTrue(ok, msg)
                self.assertTrue((home_dir / "presets" / "tiny_rank.json").is_file())

                ver = client.verify("tiny_rank")
                self.assertEqual(len(ver), 1)
                self.assertEqual(ver[0]["status"], "HEALTHY")

                discovered = discover_presets(extra_dirs=[home_dir / "presets"])
                self.assertIn("tiny_rank", discovered)
                self.assertEqual(resolve_preset(str(home_dir / "presets" / "tiny_rank.json")).name, "tiny_rank")

                ok_rem, rem_msg = client.remove("tiny_rank")
                self.assertTrue(ok_rem, rem_msg)
            finally:
                if old_home is None:
                    os.environ.pop("KNAPSET_HOME", None)
                else:
                    os.environ["KNAPSET_HOME"] = old_home

    def test_run_cli_on_registry_pc_and_fishing_presets(self) -> None:
        pc_preset = REGISTRY_DIR / "modules" / "presets" / "pc_build" / "preset.json"
        fish_preset = REGISTRY_DIR / "modules" / "presets" / "fishing_tackle" / "preset.json"
        if not pc_preset.is_file() or not fish_preset.is_file():
            self.skipTest("knapset-registry v2 sample presets not found on disk")

        out_pc = json.loads(self._run_cli(["run", str(pc_preset), "--profile", "budget", "--json"]))
        self.assertEqual(out_pc["mode"], "solve")
        self.assertGreaterEqual(len(out_pc["results"]), 1)
        top_pc = out_pc["results"][0]
        self.assertLessEqual(top_pc["metrics"]["sum(price) <= 700"], 700.0)
        self.assertEqual(top_pc["slots"]["cpu"]["socket"], top_pc["slots"]["mobo"]["socket"])

        out_fish = json.loads(self._run_cli(["run", str(fish_preset), "--json"]))
        self.assertEqual(out_fish["mode"], "solve")
        self.assertGreaterEqual(len(out_fish["results"]), 1)
        top_fish = out_fish["results"][0]
        self.assertLessEqual(top_fish["metrics"]["sum(price) <= 220"], 220.0)
        self.assertGreaterEqual(top_fish["slots"]["lure"]["weight_g"], top_fish["slots"]["rod"]["cast_min_g"])
        self.assertLessEqual(top_fish["slots"]["lure"]["weight_g"], top_fish["slots"]["rod"]["cast_max_g"])

    def test_per_slot_where_filters_source_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = root / "items.csv"
            items.write_text("name,score,price,flag\nBad,99,10,1\nGood,70,20,0\n", encoding="utf-8")
            pfile = root / "p.json"
            pfile.write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "name": "slot_where_test",
                        "mode": "rank",
                        "sources": {"items": {"source": str(items), "where": "flag == 0"}},
                        "profiles": {"std": {"weights": {"score": 1.0}}},
                    }
                ),
                encoding="utf-8",
            )
            res = PresetConfig.from_dict(json.loads(pfile.read_text(encoding="utf-8")), source_path=pfile).execute()
            self.assertEqual(len(res["results"]), 1)
            self.assertEqual(res["results"][0]["row"]["name"], "Good")


if __name__ == "__main__":
    unittest.main()
