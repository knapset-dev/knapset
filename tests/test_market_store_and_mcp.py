"""
Unit Tests for MarketStore (SQLite + FTS5 + Price History) and Built-in MCP Server.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from market_store import MarketStore
from mcp_server import execute_mcp_tool, handle_jsonrpc_message
from web_server import dispatch_api_request


class TestMarketStoreAndMCP(unittest.TestCase):
    def test_market_store_upsert_fts_and_price_history(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_market.db"
            store = MarketStore(db_path=db_path)

            batch_1 = [
                {
                    "id": "101",
                    "title": "ASUS TUF RTX 3080 10GB Gaming",
                    "price": 16500.0,
                    "currency": "UAH",
                    "condition": "used",
                    "url": "https://olx.ua/101",
                },
                {
                    "id": "102",
                    "title": "Sapphire Nitro+ RX 6800 XT 16GB",
                    "price": 15000.0,
                    "currency": "UAH",
                    "condition": "used",
                    "url": "https://olx.ua/102",
                },
            ]
            cnt = store.upsert_offers("gpu", batch_1, provider="olx", domain="pc")
            self.assertEqual(cnt, 2)

            # Search via FTS5 / keyword
            res_3080 = store.search_fts("RTX 3080", category="gpu", provider="olx")
            self.assertEqual(len(res_3080), 1)
            self.assertEqual(res_3080[0]["id"], "101")

            # Price filter search
            res_cheap = store.search_fts("", category="gpu", max_price=15500.0)
            self.assertEqual(len(res_cheap), 1)
            self.assertEqual(res_cheap[0]["id"], "102")

            # Initial price history should have 1 entry (no delta yet)
            self.assertIsNone(store.get_price_delta("101", provider="olx"))

            # Seller drops price on item 101 from 16500 to 14850 (-10%)
            batch_2 = [
                {
                    "id": "101",
                    "title": "ASUS TUF RTX 3080 10GB Gaming",
                    "price": 14850.0,
                    "currency": "UAH",
                    "condition": "used",
                    "url": "https://olx.ua/101",
                },
            ]
            store.upsert_offers("gpu", batch_2, provider="olx", domain="pc")

            hist = store.get_price_history("101", provider="olx")
            self.assertEqual(len(hist), 2)
            self.assertEqual(hist[0]["price"], 16500.0)
            self.assertEqual(hist[1]["price"], 14850.0)

            delta = store.get_price_delta("101", provider="olx")
            self.assertIsNotNone(delta)
            self.assertEqual(delta["initial_price"], 16500.0)
            self.assertEqual(delta["peak_price"], 16500.0)
            self.assertEqual(delta["current_price"], 14850.0)
            self.assertEqual(delta["delta"], -1650.0)
            self.assertEqual(delta["delta_pct"], -10.0)
            self.assertEqual(delta["drop_from_peak_pct"], -10.0)

            # Batch deltas and find_price_drops
            batch_deltas = store.get_batch_price_deltas(category="gpu", provider="olx")
            self.assertIn("101", batch_deltas)
            self.assertNotIn("102", batch_deltas)
            self.assertEqual(batch_deltas["101"]["drop_from_peak_pct"], -10.0)

            drops = store.find_price_drops(category="gpu", provider="olx", min_drop_pct=5.0)
            self.assertEqual(len(drops), 1)
            self.assertEqual(drops[0]["offer_id"], "101")
            self.assertEqual(drops[0]["drop_pct"], 10.0)

            st = store.stats(domain="pc")
            self.assertEqual(st["total_listings"], 2)
            self.assertEqual(st["multi_observation_listings"], 1)

    def test_deal_hunter_scoring_rewards_price_drops(self):
        import extensions
        from marketplace.client import locate_registry_dir
        reg_ext = str(locate_registry_dir() / "modules" / "extensions")
        if reg_ext not in extensions.__path__:
            extensions.__path__.append(reg_ext)

        from extensions.pc.modes import resolve_mode, score_component

        prof = resolve_mode("deal-hunter")
        base_entry = {
            "id": "gpu_1",
            "price": 12000.0,
            "currency": "UAH",
            "fps_1440p": 75.0,
            "fps_1080p": 95.0,
            "vram_gb": 12,
            "discount_pct": 15.0,
            "gpu": {"model": "RTX 3060 12GB", "fps_1440p": 75.0, "fps_1080p": 95.0, "vram_gb": 12, "tdp_watts": 170},
            "eval": {"risk_score": 10, "red_flags": [], "warnings": []},
        }
        dropped_entry = dict(base_entry)
        dropped_entry["price_drop_pct"] = 18.0

        score_static = score_component("gpu", base_entry, prof)
        score_dropped = score_component("gpu", dropped_entry, prof)
        self.assertGreater(score_dropped, score_static + 4.0)

    def test_mcp_jsonrpc_protocol_and_tools(self):
        import extensions
        from marketplace.client import locate_registry_dir
        from extension_registry import register_extension, unregister_extension
        reg_ext = str(locate_registry_dir() / "modules" / "extensions")
        if reg_ext not in extensions.__path__:
            extensions.__path__.append(reg_ext)
        from extensions.pc import PCDomainExtension
        register_extension(PCDomainExtension())
        self.addCleanup(lambda: unregister_extension("pc"))

        # 1. initialize handshake
        init_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init_resp["id"], 1)
        self.assertIn("protocolVersion", init_resp["result"])
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "omniforge-mcp")

        # 2. tools/list
        list_resp = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = list_resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        self.assertIn("omniforge_list_domains", tool_names)
        self.assertIn("omniforge_build_set", tool_names)
        self.assertIn("omniforge_search_section", tool_names)
        self.assertIn("omniforge_evaluate_prebuilt_pc", tool_names)
        self.assertIn("omniforge_price_drops", tool_names)

        # 3. tools/call -> omniforge_list_domains
        dom_resp = handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "omniforge_list_domains", "arguments": {}},
        })
        self.assertFalse(dom_resp["result"]["isError"])
        dom_data = json.loads(dom_resp["result"]["content"][0]["text"])
        self.assertTrue(any(d["domain_id"] == "template" for d in dom_data["domains"]))

        # 4. tools/call -> omniforge_build_set on 'template'
        build_res = execute_mcp_tool("omniforge_build_set", {"domain": "template", "budget": 1000, "currency": "USDT"})
        self.assertFalse(build_res["isError"])
        build_data = json.loads(build_res["content"][0]["text"])
        self.assertEqual(build_data["domain"], "template")
        self.assertEqual(build_data["currency"], "USDT")
        self.assertGreater(build_data["count"], 0)

        # 5. tools/call -> omniforge_price_drops
        drops_res = execute_mcp_tool("omniforge_price_drops", {"domain": "pc", "min_drop_pct": 5.0, "currency": "USDT"})
        self.assertFalse(drops_res["isError"])
        drops_data = json.loads(drops_res["content"][0]["text"])
        self.assertEqual(drops_data["currency"], "USDT")
        self.assertIn("stats", drops_data)
        self.assertIn("drops", drops_data)

    def test_web_dashboard_and_rest_api_endpoints(self):
        # 1. Dashboard HTML root
        code, ctype, body = dispatch_api_request("/", {})
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("OmniForge", body)
        self.assertIn("Set & Build Synthesizer", body)

        # 2. /api/health
        code, ctype, health = dispatch_api_request("/api/health", {})
        self.assertEqual(code, 200)
        self.assertEqual(health["status"], "ok")
        self.assertIn("active_currency", health)

        # 3. /api/domains
        code, ctype, dom_payload = dispatch_api_request("/api/domains", {"currency": ["USDT"]})
        self.assertEqual(code, 200)
        self.assertEqual(dom_payload["currency"], "USDT")
        self.assertTrue(any(d["domain_id"] == "template" for d in dom_payload["domains"]))

        # 4. /api/build on template domain
        code, ctype, build_payload = dispatch_api_request(
            "/api/build",
            {"domain": ["template"], "budget": ["1000"], "currency": ["USDT"], "top_n": ["3"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(build_payload["domain"], "template")
        self.assertEqual(build_payload["currency"], "USDT")
        self.assertGreater(build_payload["count"], 0)

        # 5. /api/search on template domain
        code, ctype, search_payload = dispatch_api_request(
            "/api/search",
            {"domain": ["template"], "section": ["core"], "currency": ["USDT"], "limit": ["5"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(search_payload["domain"], "template")
        self.assertEqual(search_payload["section"], "core")
        self.assertIn("results", search_payload)

        # 6. /api/deals
        code, ctype, deals_payload = dispatch_api_request(
            "/api/deals",
            {"min_drop_pct": ["3.0"], "currency": ["USDT"], "limit": ["5"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(deals_payload["currency"], "USDT")
        self.assertIn("stats", deals_payload)
        self.assertIn("drops", deals_payload)

        # 7. /api/prebuilt (when PC extension is registered)
        import extensions
        from marketplace.client import locate_registry_dir
        from extension_registry import register_extension, unregister_extension
        reg_ext = str(locate_registry_dir() / "modules" / "extensions")
        if reg_ext not in extensions.__path__:
            extensions.__path__.append(reg_ext)
        from extensions.pc import PCDomainExtension
        register_extension(PCDomainExtension())
        self.addCleanup(lambda: unregister_extension("pc"))

        code, ctype, prebuilt_payload = dispatch_api_request(
            "/api/prebuilt",
            {"listing_a": ["Ryzen 5 5600, RTX 3060 12GB, 16GB DDR4, 650W, $450"], "currency": ["USDT"]},
        )
        self.assertEqual(code, 200)
        self.assertEqual(prebuilt_payload["type"], "single_evaluation")
        self.assertIn("verdict", prebuilt_payload)

        # 8. Web API & MCP: /api/search invalid section returns clean 400 Bad Request
        code, ctype, err_payload = dispatch_api_request(
            "/api/search",
            {"domain": ["template"], "section": ["invalid_sec"], "currency": ["USDT"]},
        )
        self.assertEqual(code, 400)
        self.assertIn("Invalid section 'invalid_sec'", err_payload.get("error", ""))

        mcp_res = handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 88,
            "method": "tools/call",
            "params": {
                "name": "omniforge_search_section",
                "arguments": {"domain": "template", "section": "invalid_sec"},
            },
        })
        self.assertTrue(mcp_res["result"]["isError"])
        self.assertIn("Invalid section 'invalid_sec'", mcp_res["result"]["content"][0]["text"])

        # 9. Web API & MCP: check consistent confirm_with_seller and risk_score fields in synthesis
        self.assertTrue("components" in build_payload["sets"][0])
        for comp in build_payload["sets"][0]["components"].values():
            self.assertIn("risk_score", comp)
            self.assertIn("confirm_with_seller", comp)

        mcp_build = handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 89,
            "method": "tools/call",
            "params": {
                "name": "omniforge_build_set",
                "arguments": {"domain": "template", "budget": 1000},
            },
        })
        self.assertFalse(mcp_build["result"]["isError"])
        mcp_build_data = json.loads(mcp_build["result"]["content"][0]["text"])
        for comp in mcp_build_data["sets"][0]["components"].values():
            self.assertIn("risk_score", comp)
            self.assertIn("confirm_with_seller", comp)


if __name__ == "__main__":
    unittest.main()
