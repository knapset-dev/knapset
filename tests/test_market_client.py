"""
Tests for MarketClient category routing, fallback behavior, and strict validation.
Ensures unknown categories raise ValueError rather than silently returning arbitrary results.
"""

import unittest
from market_client import MarketClient, normalize_category


class TestMarketClientCategoryHandling(unittest.TestCase):
    def setUp(self):
        self.client = MarketClient()

    def test_normalize_category(self):
        self.assertEqual(normalize_category("GPU"), "gpu")
        self.assertEqual(normalize_category("videokarty"), "gpu")
        self.assertEqual(normalize_category("MOBO"), "mobo")
        self.assertEqual(normalize_category("motherboard"), "mobo")
        self.assertEqual(normalize_category("materinskie-platy"), "mobo")
        self.assertEqual(normalize_category("RAM"), "ram")
        self.assertEqual(normalize_category("moduli-pamyati"), "ram")
        self.assertEqual(normalize_category("PSU"), "psu")
        self.assertEqual(normalize_category("bloki-pitaniya"), "psu")
        self.assertEqual(normalize_category("prebuilt"), "prebuilt")

    def test_unknown_category_raises_value_error_in_search_fallback(self):
        """Unknown category must raise ValueError instead of silently falling back to GPU or returning arbitrary results."""
        with self.assertRaises(ValueError) as ctx:
            self.client._search_category_fallback("unknown_nonexistent_xyz")
        self.assertIn("Unsupported or unrecognized category", str(ctx.exception))

    def test_unknown_category_raises_value_error_in_harvest(self):
        """harvest_category with an invalid category must raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.client.harvest_category("completely_random_hardware_123")
        self.assertIn("Unsupported or unrecognized category", str(ctx.exception))

    def test_known_category_passes_validation(self):
        """Known categories (gpu, cpu, mobo, ram, psu) pass validation without ValueError."""
        for cat in ("gpu", "cpu", "mobo", "ram", "psu"):
            try:
                # Capped to 0 or mock to verify no ValueError is raised during category validation
                norm = normalize_category(cat)
                self.assertIsNotNone(norm)
            except ValueError:
                self.fail(f"Known category {cat} should not raise ValueError")


if __name__ == "__main__":
    unittest.main()
