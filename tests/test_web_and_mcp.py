"""
Integration tests for Knapset v2 Web Studio (`web_server.py`) and MCP Server (`mcp_server.py`).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from mcp_server import handle_jsonrpc_message
from web_server import create_web_server


def _http_json(url: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestV2WebAndMCP(unittest.TestCase):
    def test_web_server_endpoints(self) -> None:
        server = create_web_server("127.0.0.1", 0)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5.0) as r:
                html = r.read().decode("utf-8")
                self.assertIn("Knapset v2 Studio", html)

            health = _http_json(f"http://127.0.0.1:{port}/api/health")
            self.assertEqual(health, {"status": "ok", "version": "2.0.0"})

            with tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                home_dir = tmp_root / "knapset_home"
                csv_path = tmp_root / "gpus.csv"
                csv_path.write_text("title,price,score\nRTX 4070,300,24000\nRTX 4080,500,26000\n", encoding="utf-8")
                ref_path = tmp_root / "bench.csv"
                ref_path.write_text("model,msrp,rating\nRTX 4070,400,90\nRTX 4080,600,98\n", encoding="utf-8")

                with patch.dict(os.environ, {"KNAPSET_HOME": str(home_dir)}):
                    insp = _http_json(f"http://127.0.0.1:{port}/api/inspect", {"source": str(csv_path)})
                    self.assertEqual(insp["schema"]["tables"][0]["row_count"], 2)

                    cmp_res = _http_json(
                        f"http://127.0.0.1:{port}/api/compare",
                        {"source": str(csv_path), "ref_source": str(ref_path), "match": "title~model"},
                    )
                    self.assertEqual(cmp_res["match"]["matched"], 2)

                    solved = _http_json(
                        f"http://127.0.0.1:{port}/api/solve",
                        {
                            "source": str(csv_path),
                            "ref_source": str(ref_path),
                            "match": "title~model",
                            "mode": "rank",
                            "where": ["price <= 400"],
                            "weights": "ref.rating:+0.5, price:-0.5",
                            "top_n": 2,
                        },
                    )
                    self.assertEqual(solved["mode"], "rank")
                    self.assertEqual(len(solved["results"]), 1)
                    self.assertEqual(solved["results"][0]["row"]["title"], "RTX 4070")

                    saved_pref = _http_json(
                        f"http://127.0.0.1:{port}/api/prefs/save",
                        {
                            "name": "value_focus",
                            "mode": "rank",
                            "weights": "score:+0.4, missing_col:+0.3, price:-0.6",
                            "where": ["price > 0"],
                        },
                    )
                    self.assertEqual(saved_pref["saved"], "value_focus")
                    prefs_list = _http_json(f"http://127.0.0.1:{port}/api/prefs")
                    self.assertIn("value_focus", prefs_list["profiles"])

                    solved_prefs = _http_json(
                        f"http://127.0.0.1:{port}/api/solve",
                        {"source": str(csv_path), "prefs": "value_focus", "top_n": 2},
                    )
                    self.assertEqual(solved_prefs["meta"]["skipped_prefs_terms"], ["missing_col"])
                    self.assertEqual(solved_prefs["results"][0]["row"]["title"], "RTX 4070")

                    del_pref = _http_json(
                        f"http://127.0.0.1:{port}/api/prefs/delete",
                        {"name": "value_focus"},
                    )
                    self.assertEqual(del_pref["removed"], "value_focus")

                    themes = _http_json(f"http://127.0.0.1:{port}/api/themes")
                    self.assertIn("active", themes)
                    switched = _http_json(f"http://127.0.0.1:{port}/api/themes/use", {"name": "agy-dark"})
                    self.assertEqual(switched["active"], "agy-dark")

                    reg = _http_json(f"http://127.0.0.1:{port}/api/registry?q=pc_build")
                    self.assertTrue(any(m["name"] == "pc_build" for m in reg.get("modules", [])))

                    files_resp = _http_json(f"http://127.0.0.1:{port}/api/files?dir={tmp_root}")
                    self.assertTrue(any(f.endswith("gpus.csv") for f in files_resp["files"]))

                    saved_preset = _http_json(
                        f"http://127.0.0.1:{port}/api/save-preset",
                        {
                            "preset": {
                                "schema_version": "2.0",
                                "name": "studio_test",
                                "mode": "rank",
                                "sources": {"gpus": {"source": str(csv_path)}},
                                "profiles": {"default": {"weights": {"score": 0.5, "price": -0.5}}},
                            }
                        },
                    )
                    self.assertEqual(saved_preset["name"], "studio_test")
                    self.assertTrue(Path(saved_preset["saved"]).is_file())

                    sens = _http_json(
                        f"http://127.0.0.1:{port}/api/sensitivity",
                        {"source": str(csv_path), "mode": "rank", "weights": "score:+0.4, price:-0.6"},
                    )
                    self.assertEqual(sens["baseline"]["label"], "RTX 4070")
                    self.assertGreaterEqual(sens["stability_pct"], 0.0)

                    self.assertIn("document.body.appendChild(a)", html)
                    self.assertIn("data-exclude-idx", html)
        finally:
            server.shutdown()
            server.server_close()

    def test_mcp_jsonrpc_lifecycle_and_all_tools(self) -> None:
        init_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIsNotNone(init_resp)
        assert init_resp is not None
        self.assertEqual(init_resp["result"]["serverInfo"]["version"], "2.0.0")

        self.assertIsNone(handle_jsonrpc_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

        tools_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools_resp is not None
        self.assertEqual(len(tools_resp["result"]["tools"]), 9)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "knapset_home"
            items_csv = root / "snacks.csv"
            items_csv.write_text("name,protein,price\nBar,20,2\nShake,30,4\nNuts,15,3\n", encoding="utf-8")
            ref_csv = root / "brands.csv"
            ref_csv.write_text("snack,score\nBar,90\nShake,95\n", encoding="utf-8")

            with patch.dict(os.environ, {"KNAPSET_HOME": str(home_dir)}):
                insp_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "knapset_inspect_source", "arguments": {"source": str(items_csv)}},
                    }
                )
                assert insp_call is not None
                self.assertFalse(insp_call["result"]["isError"])
                insp_data = json.loads(insp_call["result"]["content"][0]["text"])
                self.assertEqual(insp_data["tables"][0]["row_count"], 3)

                cmp_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_compare_sources",
                            "arguments": {
                                "source": str(items_csv),
                                "ref_source": str(ref_csv),
                                "match": "name~snack",
                            },
                        },
                    }
                )
                assert cmp_call is not None
                cmp_data = json.loads(cmp_call["result"]["content"][0]["text"])
                self.assertEqual(cmp_data["match"]["matched"], 2)

                pref_save = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_prefs",
                            "arguments": {
                                "action": "save",
                                "name": "high_protein",
                                "weights": "protein:+0.8, bogus_col:+0.1, price:-0.2",
                            },
                        },
                    }
                )
                assert pref_save is not None
                self.assertFalse(pref_save["result"]["isError"])

                rank_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_rank_items",
                            "arguments": {"source": str(items_csv), "prefs": "high_protein"},
                        },
                    }
                )
                assert rank_call is not None
                self.assertFalse(rank_call["result"]["isError"])
                rank_data = json.loads(rank_call["result"]["content"][0]["text"])
                self.assertEqual(rank_data["meta"]["skipped_prefs_terms"], ["bogus_col"])

                basket_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_solve_basket",
                            "arguments": {
                                "source": str(items_csv),
                                "weights": "protein:+1.0",
                                "limits": ["sum(price) <= 5"],
                            },
                        },
                    }
                )
                assert basket_call is not None
                basket_data = json.loads(basket_call["result"]["content"][0]["text"])
                self.assertGreaterEqual(len(basket_data["solutions"]), 1)

                sets_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_solve_sets",
                            "arguments": {
                                "slots": {
                                    "a": [{"name": "A1", "price": 10, "val": 90}],
                                    "b": [{"name": "B1", "price": 15, "val": 80}],
                                },
                                "weights": "val:+1.0",
                                "limits": ["sum(price) <= 30"],
                            },
                        },
                    }
                )
                assert sets_call is not None
                sets_data = json.loads(sets_call["result"]["content"][0]["text"])
                self.assertEqual(len(sets_data["solutions"]), 1)

                presets_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "knapset_list_presets", "arguments": {}},
                    }
                )
                assert presets_call is not None
                self.assertFalse(presets_call["result"]["isError"])

                reg_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "knapset_registry", "arguments": {"action": "theme_list"}},
                    }
                )
                assert reg_call is not None
                self.assertFalse(reg_call["result"]["isError"])
                reg_data = json.loads(reg_call["result"]["content"][0]["text"])
                self.assertTrue(any(t["name"] == "agy-dark" for t in reg_data["themes"]))

                theme_use_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {"name": "knapset_registry", "arguments": {"action": "theme_use", "target": "agy-dark"}},
                    }
                )
                assert theme_use_call is not None
                self.assertFalse(theme_use_call["result"]["isError"])
                self.assertEqual(json.loads(theme_use_call["result"]["content"][0]["text"])["active"], "agy-dark")

                sens_call = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {
                            "name": "knapset_sensitivity",
                            "arguments": {"source": str(items_csv), "mode": "rank", "weights": "protein:+0.7, price:-0.3"},
                        },
                    }
                )
                assert sens_call is not None
                self.assertFalse(sens_call["result"]["isError"])
                sens_data = json.loads(sens_call["result"]["content"][0]["text"])
                self.assertIsNotNone(sens_data["baseline"])


if __name__ == "__main__":
    unittest.main()
