"""
Market Client Adapter & Local Cache Manager.

Reads normalized marketplace listing datasets from local JSON caches and SQLite
MarketStore, resolving cache freshness and cross-provider aggregation.
"""

import concurrent.futures
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from app_paths import REPO_ROOT, get_data_dir

INTEL_DIR = REPO_ROOT
INTEL_DATA_DIR = get_data_dir()

from provider_template import ProviderFeatureAdapter

check_delivery_refusal = ProviderFeatureAdapter.check_delivery_refusal

CATEGORY_CACHE_FILES = {
    "gpu": "gpu_market_cache.json",
    "cpu": "cpu_market_cache.json",
    "motherboard": "mobo_market_cache.json",
    "mobo": "mobo_market_cache.json",
    "ram": "ram_market_cache.json",
    "psu": "psu_market_cache.json",
    "prebuilt": "prebuilt_market_cache.json",
    "desktop": "prebuilt_market_cache.json",
}

def normalize_category(category: Optional[str]) -> str:
    """Normalizes category name or alias to canonical intelligence key (gpu, cpu, mobo, ram, psu, prebuilt)."""
    if not category:
        return ""
    c = category.lower().strip()
    if c in ("mobo", "mobos", "motherboard", "motherboards", "materinskie-platy"):
        return "mobo"
    if c in ("gpu", "gpus", "videokarty"):
        return "gpu"
    if c in ("cpu", "cpus", "protsessory"):
        return "cpu"
    if c in ("ram", "rams", "memory", "moduli-pamyati"):
        return "ram"
    if c in ("psu", "psus", "bloki-pitaniya"):
        return "psu"
    if c in ("prebuilt", "prebuilts", "desktop", "desktops", "system", "systems", "nastolnye-kompyutery", "kompyutery"):
        return "prebuilt"
    return c


def normalize_data_domain(domain: Optional[str] = None, category: Optional[str] = None) -> str:
    """
    Resolves the canonical shared data directory for domain datasets.
    Prevents plugin/bundle cache proliferation: multiple consumers requesting
    category data converge on the exact same shared dataset directory.
    Dynamically queries registered domain extensions first before falling back to known aliases.
    """
    if domain:
        d = domain.lower().strip()
        try:
            from extension_registry import find_extension_by_alias
            owner = find_extension_by_alias(d)
            if owner is not None:
                return owner.domain_id
        except Exception:
            pass
        return d
    if category:
        cat_clean = normalize_category(category)
        try:
            from extension_registry import find_extension_by_section
            owner = find_extension_by_section(cat_clean)
            if owner is not None:
                return owner.domain_id
        except Exception:
            pass
    return ""


def _extract_cache_meta(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    ts = data.get("timestamp") or (data.get("metadata") or {}).get("harvested_at") or ""
    offers = data.get("offers") or []
    return str(ts), offers


_HEADER_TS_RE = re.compile(r'"(?:timestamp|harvested_at)"\s*:\s*"([^"]+)"')
_MEMORY_CACHE: Dict[str, Tuple[int, List[Dict[str, Any]], str, Path]] = {}


def _peek_cache_timestamp(path: Path) -> Tuple[str, int]:
    """Reads only the first 600 bytes + mtime_ns (~0.05ms) to avoid parsing stale multi-MB JSON."""
    st = path.stat()
    try:
        with open(path, "rb") as f:
            head = f.read(600).decode("utf-8", errors="ignore")
        m = _HEADER_TS_RE.search(head)
        if m:
            return m.group(1), st.st_mtime_ns
    except OSError:
        pass
    return "", st.st_mtime_ns


def list_market_providers() -> Dict[str, Dict[str, Any]]:
    """
    Returns metadata for all available marketplace data information providers
    registered in `info_providers/modules/`.
    """
    providers: Dict[str, Dict[str, Any]] = {}

    try:
        from info_providers import get_info_registry
        for name, prov in get_info_registry().list_providers().items():
            providers[name] = {
                "name": name,
                "class_name": prov.__class__.__name__,
                "source_name": getattr(prov, "source_domain", getattr(prov, "title", name)),
                "default_currency": getattr(prov, "default_currency", "USDT"),
                "description": getattr(prov, "description", getattr(prov, "title", "")),
                "module": f"info_providers.modules.{prov.__class__.__module__.split('.')[-1]}",
            }
    except Exception:
        pass

    if not providers:
        providers["local"] = {
            "name": "local",
            "class_name": "LocalCacheProvider",
            "source_name": "local",
            "default_currency": "UAH",
            "description": "Local JSON Market Dataset Provider",
            "module": "provider_template",
        }
    return providers


def _enrich_offers_with_price_history(
    cat_clean: str,
    offers: List[Dict[str, Any]],
    provider: Optional[str] = None,
) -> None:
    """
    Attaches time-series price drop metadata (`price_delta_pct`, `price_drop_pct`, `initial_price`, `peak_price`)
    from SQLite MarketStore in a single batch query when available.
    """
    if not offers:
        return
    try:
        from market_store import market_store
        if not market_store.db_path.exists():
            return
        deltas = market_store.get_batch_price_deltas(category=cat_clean, provider=provider)
        if not deltas:
            return
        for item in offers:
            oid = str(item.get("id") or "")
            d = deltas.get(oid)
            if d:
                item["price_delta_pct"] = d["delta_pct"]
                item["price_drop_pct"] = abs(min(0.0, d["drop_from_peak_pct"]))
                item["initial_price"] = d["initial_price"]
                item["peak_price"] = d["peak_price"]
                item["price_observations"] = d["observations"]
    except Exception:
        pass


def load_market_cache(category: str, provider: Optional[str] = None, domain: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str, Optional[Path]]:
    """
    Loads the freshest available market cache from `data/` and domain subdirectories.
    Supports multi-provider cache partitions and multi-provider aggregation ('all').
    Peeks timestamps in ~0.05ms so stale multi-MB caches are never JSON-decoded,
    and memoizes decoded caches in RAM keyed by file mtime_ns.
    Returns (offers_list, iso_timestamp, source_path).
    """
    cat_clean = normalize_category(category) or category.lower()
    prov_clean = (provider or "olx").lower().strip()
    dom_clean = normalize_data_domain(domain, cat_clean)

    # Multi-provider aggregation ('all' / 'multi')
    if prov_clean in ("all", "multi", "unified"):
        all_offers: List[Dict[str, Any]] = []
        seen_keys = set()
        latest_ts = ""

        # 1. Base cache
        base_offers, base_ts, _ = load_market_cache(cat_clean, provider="olx", domain=dom_clean)
        for o in base_offers:
            oid = str(o.get("id", ""))
            src = str(o.get("source", "default"))
            seen_keys.add((src, oid))
            all_offers.append(o)
        if base_ts > latest_ts:
            latest_ts = base_ts

        # 2. Discover any additional provider caches (*_{cat_clean}_market_cache.json)
        if INTEL_DATA_DIR.exists():
            for p in INTEL_DATA_DIR.glob(f"*_{cat_clean}_market_cache.json"):
                stem = p.name
                prov_prefix = stem.replace(f"_{cat_clean}_market_cache.json", "")
                if prov_prefix in ("olx", "all"):
                    continue
                p_offers, p_ts, _ = load_market_cache(cat_clean, provider=prov_prefix, domain=domain)
                if p_ts > latest_ts:
                    latest_ts = p_ts
                for o in p_offers:
                    oid = str(o.get("id", ""))
                    src = str(o.get("source", prov_prefix))
                    if (src, oid) not in seen_keys:
                        seen_keys.add((src, oid))
                        all_offers.append(o)

        return all_offers, latest_ts, None

    if prov_clean in ("olx", "olx.ua", "default"):
        filename = CATEGORY_CACHE_FILES.get(cat_clean, f"{cat_clean}_market_cache.json")
    else:
        safe_p = re.sub(r"[^a-z0-9_-]", "_", prov_clean)
        filename = f"{safe_p}_{cat_clean}_market_cache.json"

    candidate_paths: List[Path] = [
        # 1. Flat canonical cache directly in data/
        INTEL_DATA_DIR / filename,
    ]

    # 2. Fallback to any legacy subdirectories under data/
    if INTEL_DATA_DIR.exists():
        for sub in INTEL_DATA_DIR.iterdir():
            if sub.is_dir() and sub.name not in ("__pycache__", ".git"):
                p_sub = sub / filename
                if p_sub not in candidate_paths:
                    candidate_paths.append(p_sub)

    ranked_candidates: List[Tuple[str, int, Path]] = []
    for p in candidate_paths:
        if p.is_file():
            try:
                ts_hint, mtime_ns = _peek_cache_timestamp(p)
                ranked_candidates.append((ts_hint, mtime_ns, p))
            except OSError:
                continue

    # Sort newest timestamp (and newest mtime) first so we only parse the winning file!
    ranked_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    for _, mtime_ns, p in ranked_candidates:
        cache_key = str(p)
        cached = _MEMORY_CACHE.get(cache_key)
        if cached and cached[0] == mtime_ns:
            _enrich_offers_with_price_history(cat_clean, cached[1], provider=prov_clean)
            return cached[1], cached[2], cached[3]

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts, offers = _extract_cache_meta(data)
            if not offers:
                continue
            _enrich_offers_with_price_history(cat_clean, offers, provider=prov_clean)
            _MEMORY_CACHE[cache_key] = (mtime_ns, offers, ts, p)
            return offers, ts, p
        except Exception:
            continue

    return [], "", None


def save_market_cache(category: str, offers: List[Dict[str, Any]], provider: str = "olx", domain: Optional[str] = None) -> Path:
    """Atomically writes compact market cache JSON directly to data/ directory grouped flat by category."""
    cat_clean = normalize_category(category) or category.lower()
    prov_clean = (provider or "olx").lower().strip()

    if prov_clean in ("olx", "olx.ua", "default"):
        filename = CATEGORY_CACHE_FILES.get(cat_clean, f"{cat_clean}_market_cache.json")
    else:
        safe_p = re.sub(r"[^a-z0-9_-]", "_", prov_clean)
        filename = f"{safe_p}_{cat_clean}_market_cache.json"

    target_dir = INTEL_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / filename
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "timestamp": now_iso,
        "total": len(offers),
        "metadata": {
            "source": prov_clean,
            "category": cat_clean,
            "harvested_at": now_iso,
            "count": len(offers),
        },
        "offers": offers,
    }
    tmp_path = out_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    # Automatically mirror into SQLite + FTS5 + Price History store
    try:
        from market_store import market_store
        mirror_domain = normalize_data_domain(domain, cat_clean)
        if mirror_domain:
            market_store.upsert_offers(cat_clean, offers, provider=prov_clean, domain=mirror_domain)
    except Exception:
        pass

    return out_path


class MarketClient:
    """
    Unified local-first client providing hardware search, dataset synchronization,
    and offer detail retrieval from local market caches and registered info providers.
    """

    def __init__(self, provider: str = "olx", max_concurrency: int = 4):
        self.provider_name = provider.lower()
        self.max_concurrency = max_concurrency
        from provider_template import LocalCacheProvider
        self._provider = LocalCacheProvider(provider_id=self.provider_name)

    def _search_category_fallback(self, category: str, **kwargs) -> List[Dict[str, Any]]:
        cat_clean = normalize_category(category) or (category or "").lower().strip()
        known_sections = {"gpu", "cpu", "mobo", "motherboard", "ram", "psu", "prebuilt", "desktop"}
        if cat_clean not in known_sections:
            try:
                from extension_registry import find_extension_by_section
                owner = find_extension_by_section(cat_clean)
                if owner is None:
                    raise ValueError(f"Unsupported or unrecognized category: '{category}'")
            except ImportError:
                raise ValueError(f"Unsupported or unrecognized category: '{category}'")

        return [r.to_dict() for r in self._provider.search_items(category_hint=category, **kwargs)]

    def search_gpus(self, **kwargs) -> List[Dict[str, Any]]:
        return self._search_category_fallback("gpu", **kwargs)

    def search_cpus(self, **kwargs) -> List[Dict[str, Any]]:
        return self._search_category_fallback("cpu", **kwargs)

    def search_motherboards(self, **kwargs) -> List[Dict[str, Any]]:
        return self._search_category_fallback("motherboard", **kwargs)

    def search_ram(self, **kwargs) -> List[Dict[str, Any]]:
        return self._search_category_fallback("ram", **kwargs)

    search_rams = search_ram

    def search_psus(self, **kwargs) -> List[Dict[str, Any]]:
        return self._search_category_fallback("psu", **kwargs)

    def harvest_category(
        self,
        category: str,
        max_pages: int = 40,
        deep: Optional[bool] = None,
        query: str = "",
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        save_to_disk: bool = True,
        quiet: bool = False,
        domain: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Loads and indexes category listings from local market caches or registered
        offline providers into the local SQLite MarketStore.
        """
        cat_key = normalize_category(category) or category.lower()
        cat_norm = "motherboard" if cat_key == "mobo" else cat_key

        known_sections = {"gpu", "cpu", "mobo", "motherboard", "ram", "psu", "prebuilt", "desktop"}
        if cat_norm not in known_sections:
            try:
                from extension_registry import find_extension_by_section
                owner = find_extension_by_section(cat_norm)
                if owner is None:
                    raise ValueError(f"Unsupported or unrecognized category: '{category}'")
            except ImportError:
                raise ValueError(f"Unsupported or unrecognized category: '{category}'")

        offers = self._search_category_fallback(
            cat_norm,
            query=query,
            min_price=min_price,
            max_price=max_price,
        )

        if save_to_disk and offers and not query and min_price is None and max_price is None:
            out_p = save_market_cache(category, offers, provider=self.provider_name, domain=domain)
            if not quiet:
                sz = out_p.stat().st_size / (1024 * 1024)
                print(f"Saved {len(offers)} listings to {out_p} ({sz:.2f} MB)")

        return offers

    def get_offer_details(self, offer_id: Any) -> Optional[Dict[str, Any]]:
        if hasattr(self._provider, "get_item_details"):
            return self._provider.get_item_details(str(offer_id))
        return None

    def get_batch_details(self, offer_ids: List[Any]) -> Dict[str, Dict[str, Any]]:
        """Batch retrieval of cached offer details."""
        if not offer_ids:
            return {}
        str_ids = [str(oid) for oid in offer_ids]
        return {str(oid): (self.get_offer_details(oid) or {}) for oid in str_ids}
