"""
Integration tests for Knapset v2 Web Studio (`web_server.py`) and MCP Server (`mcp_server.py`).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict

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
                csv_path = Path(tmp) / "gpus.csv"
                csv_path.write_text("title,price,score\nCardA,300,24000\nCardB,500,26000\n", encoding="utf-8")

                insp = _http_json(f"http://127.0.0.1:{port}/api/inspect", {"source": str(csv_path)})
                self.assertEqual(insp["schema"]["tables"][0]["row_count"], 2)

                solved = _http_json(
                    f"http://127.0.0.1:{port}/api/solve",
                    {
                        "source": str(csv_path),
                        "mode": "rank",
                        "weights": "score:+0.4, price:-0.6",
                        "top_n": 2,
                    },
                )
                self.assertEqual(solved["mode"], "rank")
                self.assertEqual(solved["results"][0]["row"]["title"], "CardA")
        finally:
            server.shutdown()
            server.server_close()

    def test_mcp_jsonrpc_lifecycle_and_all_five_tools(self) -> None:
        init_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIsNotNone(init_resp)
        assert init_resp is not None
        self.assertEqual(init_resp["result"]["serverInfo"]["version"], "2.0.0")

        self.assertIsNone(handle_jsonrpc_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

        tools_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools_resp is not None
        self.assertEqual(len(tools_resp["result"]["tools"]), 5)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items_csv = root / "snacks.csv"
            items_csv.write_text("name,protein,price\nBar,20,2\nShake,30,4\nNuts,15,3\n", encoding="utf-8")

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

            rank_call = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "knapset_rank_items",
                        "arguments": {"source": str(items_csv), "weights": "protein:+0.7, price:-0.3"},
                    },
                }
            )
            assert rank_call is not None
            self.assertFalse(rank_call["result"]["isError"])

            basket_call = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
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
                    "id": 6,
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
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "knapset_list_presets", "arguments": {}},
                }
            )
            assert presets_call is not None
            self.assertFalse(presets_call["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
