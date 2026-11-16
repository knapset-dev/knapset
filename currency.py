"""
Knapset Dynamic Currency Engine.

Provides universal multi-currency support, dynamic exchange rate conversion,
live rate synchronization with offline fallback, and consistent price formatting.
Default system currency: USDT.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from app_paths import REPO_ROOT, get_config_file, get_data_dir

CONFIGS_DIR = get_config_file("currency.json").parent
DATA_DIR = get_data_dir()
CONFIG_FILE = get_config_file("currency.json")
CACHE_FILE = DATA_DIR / ".currency_cache.json"

DEFAULT_CURRENCY = "USDT"

# Default calibrated offline exchange rates relative to 1.0 USDT (1 USDT = 1 USD)
DEFAULT_EXCHANGE_RATES: Dict[str, float] = {
    "USDT": 1.0,
    "USD": 1.0,
    "UAH": 41.5,
    "EUR": 0.92,
    "GBP": 0.79,
    "PLN": 4.0,
    "SEK": 10.5,
}

# Currency symbol and position mapping
CURRENCY_SYMBOLS: Dict[str, Tuple[str, bool]] = {
    # code: (symbol, is_suffix)
    "USDT": ("USDT", True),
    "USD": ("$", False),
    "EUR": ("€", True),
    "UAH": ("₴", True),
    "GBP": ("£", False),
    "PLN": ("zł", True),
    "SEK": ("kr", True),
}

# Common currency aliases and symbols
CURRENCY_ALIASES: Dict[str, str] = {
    "usdt": "USDT",
    "tether": "USDT",
    "usd": "USD",
    "$": "USD",
    "dollar": "USD",
    "uah": "UAH",
    "грн": "UAH",
    "гривня": "UAH",
    "гривень": "UAH",
    "гривні": "UAH",
    "₴": "UAH",
    "hryvnia": "UAH",
    "eur": "EUR",
    "€": "EUR",
    "euro": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "pound": "GBP",
    "pln": "PLN",
    "zł": "PLN",
    "zloty": "PLN",
    "sek": "SEK",
    "kr": "SEK",
    "krona": "SEK",
}

# Runtime overrides
_RUNTIME_ACTIVE_CURRENCY: Optional[str] = None
_RUNTIME_OVERRIDE_RATES: Dict[str, float] = {}


def canonical_currency(curr: Optional[str]) -> str:
    """Normalizes any currency string, symbol, or alias to standardized ISO code (e.g. 'грн' -> 'UAH', 'usdt' -> 'USDT')."""
    if not curr:
        return get_active_currency()
    if curr in CURRENCY_SYMBOLS:
        return curr
    if curr in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[curr]
    cleaned = str(curr).strip().lower()
    return CURRENCY_ALIASES.get(cleaned, str(curr).strip().upper())


def get_active_currency() -> str:
    """
    Returns the currently active target currency.
    Priority:
    1. Runtime override (via CLI --currency or set_active_currency)
    2. Environment variable `KNAPSET_CURRENCY`
    3. Config file `configs/currency.json`
    4. Default: "USDT"
    """
    global _RUNTIME_ACTIVE_CURRENCY
    if _RUNTIME_ACTIVE_CURRENCY:
        return _RUNTIME_ACTIVE_CURRENCY

    env_val = os.getenv("KNAPSET_CURRENCY")
    if env_val:
        return canonical_currency(env_val)

    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "default_currency" in cfg:
                return canonical_currency(cfg["default_currency"])
        except Exception:
            pass

    return DEFAULT_CURRENCY


def set_active_currency(currency_code: str) -> str:
    """Sets the active currency for the current runtime session."""
    global _RUNTIME_ACTIVE_CURRENCY
    canonical = canonical_currency(currency_code)
    _RUNTIME_ACTIVE_CURRENCY = canonical
    return canonical


def set_override_rate(currency_code: str, rate: float) -> None:
    """Sets an in-memory exchange rate override (units per 1.0 USDT)."""
    canonical = canonical_currency(currency_code)
    if rate > 0:
        _RUNTIME_OVERRIDE_RATES[canonical] = float(rate)


def parse_rate_string(rate_spec: str) -> None:
    """
    Parses rate specifications like 'UAH:41.5,EUR:0.92' or '41.5' (assumes UAH if single number)
    and stores them in runtime overrides.
    """
    if not rate_spec:
        return
    parts = rate_spec.split(",")
    for p in parts:
        p = p.strip()
        if ":" in p:
            code, val = p.split(":", 1)
            try:
                set_override_rate(code.strip(), float(val.strip()))
            except ValueError:
                pass
        elif "=" in p:
            code, val = p.split("=", 1)
            try:
                set_override_rate(code.strip(), float(val.strip()))
            except ValueError:
                pass
        else:
            try:
                val = float(p)
                set_override_rate("UAH", val)
            except ValueError:
                pass


def fetch_live_rates(timeout_sec: float = 1.2, cache_hours: float = 24.0) -> Dict[str, float]:
    """
    Attempts to fetch live market exchange rates relative to USDT/USD with fallback to local cache
    and offline defaults. Never crashes if network is unavailable.
    """
    now = time.time()
    cache_max_age = cache_hours * 3600.0

    # 1. Check local cache
    if CACHE_FILE.is_file():
        try:
            cached_data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            timestamp = cached_data.get("timestamp", 0)
            rates = cached_data.get("rates", {})
            if (now - timestamp) < cache_max_age and rates:
                return {canonical_currency(k): float(v) for k, v in rates.items()}
        except Exception:
            pass

    # 2. Try fetching from free zero-auth public endpoint
    fresh_rates: Dict[str, float] = dict(DEFAULT_EXCHANGE_RATES)
    fetched = False

    # Try Binance public ticker for USDT/UAH
    try:
        req = urllib.request.Request(
            "https://api.binance.com/api/v3/ticker/price?symbol=USDTUAH",
            headers={"User-Agent": "Knapset/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "price" in data:
                uah_rate = float(data["price"])
                if 20.0 < uah_rate < 100.0:
                    fresh_rates["UAH"] = uah_rate
                    fetched = True
    except Exception:
        pass

    # Try open.er-api for standard fiat rates
    if not fetched:
        try:
            req = urllib.request.Request(
                "https://open.er-api.com/v6/latest/USD",
                headers={"User-Agent": "Knapset/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rates = data.get("rates", {})
                for k, v in rates.items():
                    c_k = canonical_currency(k)
                    if c_k in DEFAULT_EXCHANGE_RATES and v > 0:
                        fresh_rates[c_k] = float(v)
                fresh_rates["USDT"] = 1.0
                fetched = True
        except Exception:
            pass

    # Save cache if fetched
    if fetched:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps({"timestamp": now, "rates": fresh_rates}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return fresh_rates


_RUNTIME_LIVE_RATES: Optional[bool] = None


def set_live_rates_enabled(enabled: bool) -> None:
    """Enables/disables merging of cached live FX rates into get_active_rates() for this session."""
    global _RUNTIME_LIVE_RATES
    _RUNTIME_LIVE_RATES = bool(enabled)


def live_rates_enabled() -> bool:
    """Live rates are opt-in: CLI --live-rates or env KNAPSET_LIVE_RATES=1 (keeps offline runs deterministic)."""
    if _RUNTIME_LIVE_RATES is not None:
        return _RUNTIME_LIVE_RATES
    return os.getenv("KNAPSET_LIVE_RATES", "").strip().lower() in ("1", "true", "yes", "on")


def load_cached_rates(max_age_hours: float = 24.0 * 7) -> Dict[str, float]:
    """Returns rates from the local FX cache file if present and not older than max_age_hours (never hits network)."""
    if not CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("timestamp", 0))
        if age > max_age_hours * 3600.0:
            return {}
        return {
            canonical_currency(k): float(v)
            for k, v in (data.get("rates") or {}).items()
            if float(v) > 0
        }
    except Exception:
        return {}


def _cache_timestamp() -> float:
    try:
        return float(json.loads(CACHE_FILE.read_text(encoding="utf-8")).get("timestamp", 0))
    except Exception:
        return 0.0


def refresh_live_rates(timeout_sec: float = 5.0) -> Dict[str, Any]:
    """
    Forces a network refresh (ignoring cache freshness) and reports before/after rates.
    Falls back to the previous cache / offline defaults without raising when offline.
    """
    before = {**DEFAULT_EXCHANGE_RATES, **load_cached_rates(max_age_hours=24.0 * 3650)}
    # Invalidate freshness only by bypassing the cache read (cache_hours=0)
    ts_before = _cache_timestamp()
    after = fetch_live_rates(timeout_sec=timeout_sec, cache_hours=0.0)
    updated = _cache_timestamp() > ts_before
    return {"before": before, "after": after, "updated": updated}


def get_active_rates() -> Dict[str, float]:
    """
    Returns active exchange rates relative to 1.0 USDT combining:
    - DEFAULT_EXCHANGE_RATES
    - Cached live rates (only when live rates are enabled)
    - Environment variables
    - Runtime CLI overrides
    """
    rates = dict(DEFAULT_EXCHANGE_RATES)
    if live_rates_enabled():
        rates.update(load_cached_rates())

    # Environment variable KNAPSET_RATES
    env_rates = os.getenv("KNAPSET_RATES")
    if env_rates:
        parse_rate_string(env_rates)

    # Runtime overrides take precedence
    rates.update(_RUNTIME_OVERRIDE_RATES)
    return rates


def convert_currency(
    amount: float,
    from_currency: Optional[str] = None,
    to_currency: Optional[str] = None,
    rates: Optional[Dict[str, float]] = None,
) -> float:
    """
    Converts a monetary amount between currencies.
    If either from_currency or to_currency is omitted, active currency is assumed.
    """
    if amount == 0:
        return 0.0

    src = canonical_currency(from_currency or get_active_currency())
    dst = canonical_currency(to_currency or get_active_currency())

    if src == dst:
        return float(amount)

    active_rates = rates or get_active_rates()
    rate_src = active_rates.get(src, DEFAULT_EXCHANGE_RATES.get(src, 1.0))
    rate_dst = active_rates.get(dst, DEFAULT_EXCHANGE_RATES.get(dst, 1.0))

    if rate_src <= 0 or rate_dst <= 0:
        return float(amount)

    # amount in USDT = amount / rate_src
    # amount in dst = (amount / rate_src) * rate_dst
    converted = (float(amount) / rate_src) * rate_dst
    return round(converted, 2)


def format_price(
    amount: float,
    currency: Optional[str] = None,
    show_decimals: Optional[bool] = None,
) -> str:
    """
    Formats a price using standard formatting and currency placement.
    For USDT: '450 USDT' or '450.50 USDT'
    For USD: '$450' or '$450.50'
    For UAH: '18,500 ₴'
    """
    curr = canonical_currency(currency or get_active_currency())
    sym_info = CURRENCY_SYMBOLS.get(curr, (curr, True))
    symbol, is_suffix = sym_info

    # Determine decimal places
    has_fraction = abs(amount - round(amount)) > 0.001
    if show_decimals is True or (show_decimals is None and has_fraction and abs(amount) < 1000):
        formatted_num = f"{amount:,.2f}"
    else:
        formatted_num = f"{amount:,.0f}"

    if is_suffix:
        return f"{formatted_num} {symbol}"
    return f"{symbol}{formatted_num}"


def normalize_listing_price(
    listing: Dict[str, Any],
    target_currency: Optional[str] = None,
    rates: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Normalizes a listing dictionary in-place (and returns it) so that
    'price' is converted to target_currency (default: active currency)
    and 'currency' reflects target_currency. Original values are saved
    in 'orig_price' and 'orig_currency'.
    """
    target_curr = canonical_currency(target_currency or get_active_currency())
    curr_raw = listing.get("currency") or "UAH"
    curr_orig = canonical_currency(curr_raw)

    price = float(listing.get("price", 0.0))
    if "orig_price" not in listing:
        listing["orig_price"] = price
        listing["orig_currency"] = curr_orig

    if curr_orig != target_curr and price > 0:
        listing["price"] = convert_currency(price, curr_orig, target_curr, rates=rates)
        listing["currency"] = target_curr
    else:
        listing["currency"] = target_curr

    return listing


def normalize_candidate_pool(
    pool: Dict[str, List[Dict[str, Any]]],
    target_currency: Optional[str] = None,
    rates: Optional[Dict[str, float]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Normalizes all listings in a candidate pool dictionary to target_currency.
    """
    target_curr = canonical_currency(target_currency or get_active_currency())
    normalized_pool: Dict[str, List[Dict[str, Any]]] = {}

    for sec_id, candidates in pool.items():
        converted_list = []
        for cand in candidates:
            c_copy = dict(cand)
            normalize_listing_price(c_copy, target_currency=target_curr, rates=rates)
            converted_list.append(c_copy)
        normalized_pool[sec_id] = converted_list

    return normalized_pool


def normalize_spec_ranges(
    spec: Dict[str, Any],
    target_currency: Optional[str] = None,
    spec_currency: str = "UAH",
    rates: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Normalizes a domain spec database dictionary (e.g. from component/product spec dictionaries)
    converting 'market_used_min', 'market_used_max', 'market_used_median'
    from spec_currency (default: UAH) to target_currency.
    """
    target_curr = canonical_currency(target_currency or get_active_currency())
    spec_curr = canonical_currency(spec_currency)

    if target_curr == spec_curr:
        return spec

    converted_spec = dict(spec)
    for k in ("market_used_min", "market_used_max", "market_used_median", "fair_price", "msrp"):
        if k in converted_spec and converted_spec[k] is not None:
            converted_spec[k] = convert_currency(
                float(converted_spec[k]),
                from_currency=spec_curr,
                to_currency=target_curr,
                rates=rates,
            )
    converted_spec["currency"] = target_curr
    return converted_spec
