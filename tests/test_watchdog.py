"""Unit tests for the OmniForge deal watchdog (watchdog.py) and MarketStore alert_log."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watchdog
from market_store import MarketStore


def _offer(price: float) -> dict:
    return {"id": "101", "title": "ASUS TUF RTX 3080 10GB", "price": price, "currency": "USDT",
            "condition": "used", "url": "https://example.com/101"}


class TestWatchdog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self._tmp.name) / "m.db")
        self.store.upsert_offers("gpu", [_offer(400.0)], provider="olx", domain="pc")
        self.store.upsert_offers("gpu", [_offer(300.0)], provider="olx", domain="pc")  # -25%

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_upsert_detailed_statistics(self) -> None:
        st = self.store.upsert_offers_detailed("gpu", [_offer(300.0), {**_offer(250.0), "id": "202"}], provider="olx", domain="pc")
        self.assertEqual(st, {"upserted": 2, "new": 1, "price_changes": 0, "price_drops": 0})
        st = self.store.upsert_offers_detailed("gpu", [_offer(280.0)], provider="olx", domain="pc")
        self.assertEqual((st["price_changes"], st["price_drops"]), (1, 1))

    def test_domain_is_mandatory(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upsert_offers("gpu", [_offer(1.0)], provider="olx")

    def test_alert_log_dedup_and_realert_on_lower_price(self) -> None:
        self.assertFalse(self.store.has_been_alerted("olx", "101", "gpu", 300.0))
        self.store.mark_alerted("olx", "101", "gpu", 300.0)
        self.assertTrue(self.store.has_been_alerted("olx", "101", "gpu", 300.0))
        self.assertTrue(self.store.has_been_alerted("olx", "101", "gpu", 350.0))
        self.assertFalse(self.store.has_been_alerted("olx", "101", "gpu", 250.0))

    def test_cycle_sends_once_and_records(self) -> None:
        calls = []
        notifier = lambda text: calls.append(text) or {"telegram": True}
        r1 = watchdog.run_watchdog_once("pc", min_drop_pct=10, refresh=None, store=self.store, notifier=notifier)
        self.assertEqual(r1["new_alerts"], 1)
        self.assertIn("-25.0%", calls[0])
        self.assertIn("https://example.com/101", calls[0])
        r2 = watchdog.run_watchdog_once("pc", min_drop_pct=10, refresh=None, store=self.store, notifier=notifier)
        self.assertEqual(r2["new_alerts"], 0)
        self.assertEqual(len(calls), 1)

    def test_dry_run_and_failed_delivery_do_not_record(self) -> None:
        calls = []
        r = watchdog.run_watchdog_once("pc", refresh=None, store=self.store, dry_run=True,
                                       notifier=lambda t: calls.append(t) or {"telegram": True})
        self.assertEqual(r["new_alerts"], 1)
        self.assertEqual(calls, [])
        failing = lambda t: {"telegram": False}
        watchdog.run_watchdog_once("pc", refresh=None, store=self.store, notifier=failing)
        r3 = watchdog.run_watchdog_once("pc", refresh=None, store=self.store, notifier=failing)
        self.assertEqual(r3["new_alerts"], 1)  # still pending

    def test_threshold_and_refresh_failure_tolerated(self) -> None:
        def boom(*a):
            raise RuntimeError("network down")
        r = watchdog.run_watchdog_once("pc", min_drop_pct=50, refresh=boom, store=self.store, notifier=lambda t: {})
        self.assertEqual(r["new_alerts"], 0)
        self.assertEqual(r["refresh_error"], "network down")

    def test_notify_routes_by_env(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "DISCORD_WEBHOOK_URL": "https://d/hook"}
        with mock.patch.object(watchdog, "_post_json", return_value=True) as post:
            res = watchdog.notify("hi", env=env)
        self.assertEqual(res, {"telegram": True, "discord": True})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(watchdog.notify("hi", env={}), {})

    def test_format_drop_message_includes_confirm_flag_for_unknown(self) -> None:
        drop_unknown = {
            "domain": "pc",
            "category": "ram",
            "title": "16GB DDR4 3200MHz RAM",
            "peak_price": 2000.0,
            "current_price": 1500.0,
            "drop_pct": 25.0,
            "source": "olx",
            "spec": {"form_factor": "UNKNOWN"},
            "heuristic": {"risk_score": 25, "warnings": ["AMBIGUOUS FORM FACTOR: confirm with seller"]},
        }
        msg = watchdog.format_drop_message(drop_unknown)
        self.assertIn("Confirm with seller: ambiguous form factor, risk penalty +25", msg)


if __name__ == "__main__":
    unittest.main()
