"""
Unit Tests for Knapset Dynamic Currency Engine (currency.py).
Tests USDT/USD/UAH/EUR conversion, formatting, listing normalization,
candidate pool normalization, and CLI --currency / --rate flags.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import currency
import main


class TestCurrencyEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_curr = currency._RUNTIME_ACTIVE_CURRENCY
        self._prev_rates = dict(currency._RUNTIME_OVERRIDE_RATES)
        self._prev_live = currency._RUNTIME_LIVE_RATES
        currency._RUNTIME_ACTIVE_CURRENCY = None
        currency._RUNTIME_OVERRIDE_RATES.clear()

    def tearDown(self) -> None:
        currency._RUNTIME_ACTIVE_CURRENCY = self._prev_curr
        currency._RUNTIME_OVERRIDE_RATES.clear()
        currency._RUNTIME_OVERRIDE_RATES.update(self._prev_rates)
        currency._RUNTIME_LIVE_RATES = self._prev_live

    def test_default_active_currency_is_usdt(self) -> None:
        self.assertEqual(currency.get_active_currency(), "USDT")

    def test_canonical_currency_aliases(self) -> None:
        self.assertEqual(currency.canonical_currency("usdt"), "USDT")
        self.assertEqual(currency.canonical_currency("$"), "USD")
        self.assertEqual(currency.canonical_currency("usd"), "USD")
        self.assertEqual(currency.canonical_currency("грн"), "UAH")
        self.assertEqual(currency.canonical_currency("₴"), "UAH")
        self.assertEqual(currency.canonical_currency("uah"), "UAH")
        self.assertEqual(currency.canonical_currency("€"), "EUR")
        self.assertEqual(currency.canonical_currency("eur"), "EUR")

    def test_currency_conversion_usdt_usd_uah(self) -> None:
        currency.set_override_rate("UAH", 40.0)
        # 4000 UAH -> 100 USDT / 100 USD
        self.assertAlmostEqual(currency.convert_currency(4000.0, "UAH", "USDT"), 100.0)
        self.assertAlmostEqual(currency.convert_currency(4000.0, "UAH", "USD"), 100.0)
        # 250 USDT -> 10000 UAH
        self.assertAlmostEqual(currency.convert_currency(250.0, "USDT", "UAH"), 10000.0)

    def test_format_price_for_usdt_usd_uah_eur(self) -> None:
        self.assertEqual(currency.format_price(850.0, "USDT"), "850 USDT")
        self.assertEqual(currency.format_price(850.0, "USD"), "$850")
        self.assertEqual(currency.format_price(35000.0, "UAH"), "35,000 ₴")
        self.assertEqual(currency.format_price(49.5, "USD"), "$49.50")
        self.assertEqual(currency.format_price(500.0, "EUR"), "500 €")

    def test_normalize_listing_and_candidate_pool(self) -> None:
        currency.set_override_rate("UAH", 40.0)
        item = {"id": "1", "title": "RTX 3080", "price": 16000.0}
        norm = currency.normalize_listing_price(dict(item), target_currency="USDT")
        self.assertAlmostEqual(norm["price"], 400.0)
        self.assertEqual(norm["currency"], "USDT")
        self.assertAlmostEqual(norm["orig_price"], 16000.0)
        self.assertEqual(norm["orig_currency"], "UAH")

        pool = {"gpu": [{"id": "g1", "price": 8000.0}]}
        norm_pool = currency.normalize_candidate_pool(pool, target_currency="USD")
        self.assertAlmostEqual(norm_pool["gpu"][0]["price"], 200.0)
        self.assertEqual(norm_pool["gpu"][0]["currency"], "USD")

    def test_parse_rate_string_and_cli_flags(self) -> None:
        currency.parse_rate_string("UAH:42.0,EUR:0.95")
        rates = currency.get_active_rates()
        self.assertAlmostEqual(rates["UAH"], 42.0)
        self.assertAlmostEqual(rates["EUR"], 0.95)

        f = io.StringIO()
        with redirect_stdout(f):
            main.main(["--currency", "USD", "--rate", "UAH:40.0", "help"])
        self.assertEqual(currency.get_active_currency(), "USD")
        self.assertAlmostEqual(currency.get_active_rates()["UAH"], 40.0)

    def test_cached_live_rates_are_opt_in_and_expire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / ".currency_cache.json"
            cache.write_text(json.dumps({"timestamp": time.time(), "rates": {"UAH": 44.0}}), encoding="utf-8")
            with mock.patch.object(currency, "CACHE_FILE", cache):
                currency.set_live_rates_enabled(False)
                self.assertAlmostEqual(currency.get_active_rates()["UAH"], currency.DEFAULT_EXCHANGE_RATES["UAH"])
                currency.set_live_rates_enabled(True)
                self.assertAlmostEqual(currency.get_active_rates()["UAH"], 44.0)
                # Stale cache is ignored
                cache.write_text(json.dumps({"timestamp": 1.0, "rates": {"UAH": 99.0}}), encoding="utf-8")
                self.assertAlmostEqual(currency.get_active_rates()["UAH"], currency.DEFAULT_EXCHANGE_RATES["UAH"])
        currency._RUNTIME_LIVE_RATES = None

    def test_refresh_live_rates_writes_cache_and_survives_offline(self) -> None:
        payload = json.dumps({"rates": {"UAH": 43.2, "EUR": 0.9}}).encode()

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return payload

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "data" / ".currency_cache.json"
            with mock.patch.object(currency, "CACHE_FILE", cache):
                with mock.patch("urllib.request.urlopen", side_effect=[OSError("binance down"), _Resp()]):
                    res = currency.refresh_live_rates(timeout_sec=0.1)
                self.assertTrue(res["updated"])
                self.assertAlmostEqual(res["after"]["UAH"], 43.2)
                self.assertTrue(cache.is_file())
            with mock.patch.object(currency, "CACHE_FILE", Path(tmp) / "none.json"):
                with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
                    res = currency.refresh_live_rates(timeout_sec=0.1)
                self.assertFalse(res["updated"])
                self.assertAlmostEqual(res["after"]["UAH"], currency.DEFAULT_EXCHANGE_RATES["UAH"])


if __name__ == "__main__":
    unittest.main()
