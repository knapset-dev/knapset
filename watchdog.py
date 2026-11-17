"""
Knapset Deal Watchdog.

Periodically refreshes market data, detects verified price drops recorded in the
SQLite MarketStore, and sends deduplicated alerts to Telegram and/or Discord.

Zero third-party dependencies (urllib only). Credentials come from environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from currency import canonical_currency, convert_currency, format_price, get_active_currency
from market_store import MarketStore, market_store as default_store


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 10.0) -> bool:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Knapset-Watchdog/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def send_telegram_alert(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    return _post_json(url, {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def send_discord_alert(webhook_url: str, text: str) -> bool:
    return _post_json(webhook_url, {"content": text[:1990]})


def format_drop_message(drop: Dict[str, Any], display_currency: Optional[str] = None) -> str:
    """Formats a single price-drop entry as a plain-text alert line."""
    disp = canonical_currency(display_currency or get_active_currency())
    src_curr = drop.get("currency") or disp

    def _conv(v: float) -> float:
        return v if canonical_currency(src_curr) == disp else convert_currency(v, src_curr, disp)

    spec = drop.get("spec") or {}
    heuristic = drop.get("heuristic") or drop.get("eval") or {}
    ff = str(spec.get("form_factor") or drop.get("form_factor") or "").upper()
    needs_confirm = bool(ff == "UNKNOWN" or "confirm with seller" in str(heuristic.get("warnings", [])).lower())

    line = (
        f"[{drop['domain'].upper()}/{drop['category'].upper()}] {drop['title']}\n"
        f"  {format_price(_conv(drop['peak_price']), disp)} -> {format_price(_conv(drop['current_price']), disp)} "
        f"(-{drop['drop_pct']:.1f}%) via {drop['source']}"
    )
    if needs_confirm:
        risk = heuristic.get("risk_score", 25)
        line += f"\n  [WARN] [Confirm with seller: ambiguous form factor, risk penalty +{risk}]"
    if drop.get("url"):
        line += f"\n  {drop['url']}"
    return line


def notify(text: str, env: Optional[Dict[str, str]] = None) -> Dict[str, bool]:
    """Sends text to every configured channel. Returns {channel: success}."""
    env = os.environ if env is None else env
    results: Dict[str, bool] = {}
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        results["telegram"] = send_telegram_alert(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], text)
    if env.get("DISCORD_WEBHOOK_URL"):
        results["discord"] = send_discord_alert(env["DISCORD_WEBHOOK_URL"], text)
    return results


def _default_refresh(domain_id: str, budget: float, provider_name: str) -> None:
    """Refreshes candidates through the domain extension (which records snapshots into MarketStore)."""
    from extension_registry import get_extension
    from provider_template import get_market_provider

    ext = get_extension(domain_id)
    ext.load_candidates(budget=budget, refresh=True, provider=get_market_provider(provider_name, live=True))


def run_watchdog_once(
    domain_id: str,
    budget: float = 600.0,
    min_drop_pct: float = 10.0,
    limit: int = 10,
    provider_name: str = "olx",
    dry_run: bool = False,
    refresh: Optional[Callable[[str, float, str], None]] = _default_refresh,
    store: Optional[MarketStore] = None,
    notifier: Optional[Callable[[str], Dict[str, bool]]] = None,
) -> Dict[str, Any]:
    """
    One watchdog cycle: refresh data -> find drops -> drop already-alerted -> notify -> record.
    Listings are marked alerted only when at least one channel succeeded (never in dry-run).
    """
    st = store or default_store
    notifier = notifier or notify
    refresh_error: Optional[str] = None
    if refresh is not None:
        try:
            refresh(domain_id, budget, provider_name)
        except Exception as exc:  # network/provider failures must not kill the loop
            refresh_error = str(exc)

    drops = st.find_price_drops(domain=domain_id, min_drop_pct=min_drop_pct, limit=limit * 3)
    fresh = [
        d for d in drops
        if not st.has_been_alerted(d["source"], d["offer_id"], d["category"], d["current_price"])
    ][:limit]

    result: Dict[str, Any] = {
        "domain": domain_id,
        "drops_found": len(drops),
        "new_alerts": len(fresh),
        "sent": {},
        "refresh_error": refresh_error,
        "message": "",
    }
    if not fresh:
        return result

    text = f"Knapset: {len(fresh)} new price drop(s) in '{domain_id}'\n\n" + "\n\n".join(
        format_drop_message(d) for d in fresh
    )
    result["message"] = text
    if dry_run:
        return result

    sent = notifier(text)
    result["sent"] = sent
    if any(sent.values()):
        for d in fresh:
            st.mark_alerted(d["source"], d["offer_id"], d["category"], d["current_price"])
    return result


def main(argv: Optional[List[str]] = None) -> None:
    from app_paths import get_cli_prog

    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} watch",
        description="Autonomous deal watchdog: refresh market data and alert on verified price drops.",
    )
    parser.add_argument("--domain", "-D", required=True, help="Domain to watch (e.g. an installed extension id)")
    parser.add_argument("--budget", "-b", type=float, default=600.0, help="Budget used when refreshing candidates")
    parser.add_argument("--provider", "-p", default="olx", help="Marketplace provider to refresh from (default: olx)")
    parser.add_argument("--min-drop", type=float, default=10.0, help="Minimum drop %% from peak to alert (default: 10)")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max alerts per cycle (default: 10)")
    parser.add_argument("--interval", type=int, default=0, help="Seconds between cycles; 0 = run once (default)")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--no-refresh", action="store_true", help="Skip network refresh; only inspect stored history")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending/recording them")
    args = parser.parse_args(argv)

    refresh_fn = None if args.no_refresh else _default_refresh
    looping = args.interval > 0 and not args.once
    while True:
        res = run_watchdog_once(
            args.domain,
            budget=args.budget,
            min_drop_pct=args.min_drop,
            limit=args.limit,
            provider_name=args.provider,
            dry_run=args.dry_run,
            refresh=refresh_fn,
        )
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] domain={res['domain']} drops={res['drops_found']} new_alerts={res['new_alerts']} sent={res['sent'] or '-'}")
        if res["refresh_error"]:
            print(f"  refresh warning: {res['refresh_error']}", file=sys.stderr)
        if res["message"] and (args.dry_run or not res["sent"]):
            print(res["message"])
            if not args.dry_run:
                print("  (no notification channel configured or delivery failed; set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or DISCORD_WEBHOOK_URL)")
        if not looping:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
