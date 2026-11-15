"""
Unit Tests for Modular Information Providers (Market Data Providers & AI Reasoning Backends).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_client import list_market_providers, save_market_cache, load_market_cache
from ai_providers import (
    BaseAIProvider,
    register_ai_provider,
    auto_discover_ai_providers,
    load_ai_provider_file,
    list_provider_names,
    list_all_provider_statuses,
    check_provider_status,
    AIProviderClient,
)


class TestInformationProviders(unittest.TestCase):
    def test_marketplace_data_providers_metadata(self):
        providers = list_market_providers()
        self.assertIn("olx", providers)
        self.assertIn("telemart", providers)
        self.assertIn("ebay", providers)
        self.assertIn("hotline", providers)

        self.assertEqual(providers["telemart"]["source_name"], "telemart.ua")
        self.assertEqual(providers["ebay"]["default_currency"], "USD")
        self.assertEqual(providers["hotline"]["default_currency"], "UAH")

    def test_marketplace_listing_normalization_unification(self):
        """Verifies that modular info_providers and ProviderFeatureAdapter produce unified output."""
        from info_providers import get_info_registry
        from provider_template import ProviderFeatureAdapter

        registry = get_info_registry()
        olx = registry.get("olx")
        self.assertIsNotNone(olx)

        # 1. Nova Poshta shipping mention should NOT make item 'new'
        item_used = {
            "id": "item-1",
            "title": "Відеокарта GTX 1060 6GB Робоча",
            "price": 3200.0,
            "description": "Відправка через OLX-доставку (Нова Пошта, Укрпошта). Без торгу.",
            "is_new": False,
            "condition": "used",
            "source": "olx.ua",
        }
        norm_adapter = ProviderFeatureAdapter.adapt_item(item_used, "olx")
        norm_mod = olx.adapt_item(item_used)
        self.assertFalse(norm_adapter["is_new"])
        self.assertEqual(norm_adapter["condition"], "used")
        self.assertFalse(norm_mod.is_new)
        self.assertEqual(norm_mod.condition, "used")

        # 2. For parts / defective text priority over mislabelled flags
        item_parts = {
            "id": "item-2",
            "title": "RTX 3080 10GB неробоча на запчастини",
            "price": 4000.0,
            "description": "Під відновлення або на запчастини, чорний екран.",
            "is_new": True,  # Accidental seller flag
            "condition": "new",
            "source": "olx.ua",
        }
        norm_adapter_p = ProviderFeatureAdapter.adapt_item(item_parts, "olx")
        norm_mod_p = olx.adapt_item(item_parts)
        self.assertFalse(norm_adapter_p["is_new"])
        self.assertEqual(norm_adapter_p["condition"], "for_parts")
        self.assertFalse(norm_mod_p.is_new)
        self.assertEqual(norm_mod_p.condition, "for_parts")

    def test_marketplace_cache_partitioning_by_provider(self):
        fake_offers = [
            {"id": "tele-101", "title": "Telemart Outlet GPU RTX 3070", "price": 11500.0}
        ]
        saved_path = save_market_cache("gpu", fake_offers, provider="telemart")
        try:
            self.assertTrue(saved_path.is_file())
            self.assertIn("telemart_gpu_market_cache.json", str(saved_path))

            # Loading with provider="telemart" retrieves these offers
            offers, ts, p = load_market_cache("gpu", provider="telemart")
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0]["id"], "tele-101")

            # Loading default (olx) does not return telemart cache
            olx_offers, _, _ = load_market_cache("gpu", provider="olx")
            # Should either be the real OLX cache or empty, but not containing tele-101 as only offer
            if olx_offers:
                self.assertNotEqual(olx_offers[0]["id"], "tele-101")
        finally:
            if saved_path.exists():
                saved_path.unlink()

    def test_modular_ai_providers_auto_discovery(self):
        names = list_provider_names()
        self.assertIn("together", names)
        self.assertIn("cohere", names)
        self.assertIn("custom_ai_template", names)

        statuses = {s["provider"]: s for s in list_all_provider_statuses()}
        self.assertIn("together", statuses)
        self.assertEqual(statuses["together"]["label"], "Together AI API")
        self.assertEqual(statuses["together"]["kind"], "cloud")

        self.assertIn("cohere", statuses)
        self.assertEqual(statuses["cohere"]["default_model"], "command-r-plus-08-2024")

    def test_modular_ai_provider_execution(self):
        # custom_ai_template has a mock call returning structured JSON
        client = AIProviderClient(provider="custom_ai_template")
        res, used_p, used_m = client.generate_json("Analyze this GPU")
        self.assertEqual(used_p, "custom_ai_template")
        self.assertEqual(res.get("verdict"), "approved")
        self.assertEqual(res.get("confidence"), 0.95)

    def test_dynamic_ai_provider_file_loading(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_path = Path(tmp_dir) / "custom_deep_thinker.py"
            plugin_path.write_text(
                'from ai_providers import BaseAIProvider, register_ai_provider\n'
                '@register_ai_provider("deep_thinker")\n'
                'class DeepThinkerProvider(BaseAIProvider):\n'
                '    name = "deep_thinker"\n'
                '    label = "Deep Thinker Lab"\n'
                '    kind = "local"\n'
                '    default_model = "thinker-v1"\n'
                '    notes = "Dynamically injected AI provider"\n'
                '    def call(self, model, prompt, system_prompt, **kwargs):\n'
                '        return \'{"verdict": "rejected", "risk": 85, "reason": "Dynamic plugin executed"}\'\n'
            )

            inst = load_ai_provider_file(plugin_path)
            self.assertIsNotNone(inst)
            self.assertIn("deep_thinker", list_provider_names())

            client = AIProviderClient(provider="deep_thinker")
            res, prov, _ = client.generate_json("Check listing")
            self.assertEqual(prov, "deep_thinker")
            self.assertEqual(res.get("verdict"), "rejected")
            self.assertEqual(res.get("risk"), 85)


if __name__ == "__main__":
    unittest.main()
