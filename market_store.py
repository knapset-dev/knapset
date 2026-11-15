"""
OmniForge SQLite + FTS5 Market Storage & Price History Engine.

Zero-dependency (`sqlite3` Python Standard Library) persistent storage providing:
  - WAL-mode concurrent read/write access across CLI, Web, and MCP processes
  - FTS5 Full-Text Search index for sub-millisecond listing search by keywords
  - Automatic Time-Series Price History tracking (detects price drops & fake discounts)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "omniforge_market.db"


class MarketStore:
    """Thread-safe SQLite + FTS5 market cache & price history store."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self._lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if not self._initialized:
            self._init_schema(conn)
            self._initialized = True
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                source TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                category TEXT NOT NULL,
                domain TEXT NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT DEFAULT 'UAH',
                condition TEXT DEFAULT 'used',
                is_new INTEGER DEFAULT 0,
                has_delivery INTEGER DEFAULT 1,
                url TEXT DEFAULT '',
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, offer_id, category)
            );

            CREATE INDEX IF NOT EXISTS idx_listings_cat_price
                ON listings (category, source, price);

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_lookup
                ON price_history (source, offer_id, recorded_at);

            CREATE TABLE IF NOT EXISTS alert_log (
                source TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                category TEXT NOT NULL,
                alerted_price REAL NOT NULL,
                alerted_at TEXT NOT NULL,
                PRIMARY KEY (source, offer_id, category)
            );
            """
        )
        # Attempt to initialize FTS5 virtual table (supported in standard Python SQLite builds)
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
                    offer_id UNINDEXED,
                    source UNINDEXED,
                    category UNINDEXED,
                    title,
                    tokenize = 'unicode61'
                );
                """
            )
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False
        conn.commit()

    def upsert_offers(
        self,
        category: str,
        offers: List[Dict[str, Any]],
        provider: str = "olx",
        domain: Optional[str] = None,
    ) -> int:
        """
        Upserts a batch of market listings into SQLite, updates the FTS5 index,
        and appends a price_history record whenever a listing's price is new or changed.
        Returns the number of upserted listings (see upsert_offers_detailed for statistics).
        """
        return self.upsert_offers_detailed(category, offers, provider=provider, domain=domain)["upserted"]

    def upsert_offers_detailed(
        self,
        category: str,
        offers: List[Dict[str, Any]],
        provider: str = "olx",
        domain: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Same as upsert_offers but returns {"upserted", "new", "price_changes", "price_drops"}.
        """
        stats = {"upserted": 0, "new": 0, "price_changes": 0, "price_drops": 0}
        if not offers:
            return stats

        cat_clean = category.lower().strip()
        prov_clean = (provider or "olx").lower().strip()
        dom_clean = (domain or "").lower().strip()
        if not dom_clean:
            raise ValueError("upsert_offers requires an explicit domain (no default domain exists)")
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._connect()
            try:
                existing_rows = conn.execute(
                    "SELECT offer_id, price, currency FROM listings WHERE category = ? AND source = ?",
                    (cat_clean, prov_clean),
                ).fetchall()
                existing_prices: Dict[str, Tuple[float, str]] = {
                    str(r["offer_id"]): (float(r["price"]), str(r["currency"])) for r in existing_rows
                }

                upsert_params = []
                history_params = []
                fts_delete_params = []
                fts_insert_params = []

                for item in offers:
                    oid = str(item.get("id") or "")
                    if not oid:
                        continue
                    title = str(item.get("title") or "")
                    price = float(item.get("price") or 0.0)
                    curr = str(item.get("currency") or "UAH")
                    cond = str(item.get("condition") or ("new" if item.get("is_new") else "used"))
                    is_new = 1 if item.get("is_new") or cond == "new" else 0
                    has_deliv = 1 if item.get("has_buyer_protection", item.get("has_olx_delivery", True)) else 0
                    url = str(item.get("url") or "")
                    payload_str = json.dumps(item, ensure_ascii=False, separators=(",", ":"))

                    upsert_params.append((
                        prov_clean, oid, cat_clean, dom_clean, title, price, curr,
                        cond, is_new, has_deliv, url, payload_str, now_iso
                    ))

                    prev = existing_prices.get(oid)
                    if prev is None:
                        stats["new"] += 1
                        history_params.append((prov_clean, oid, cat_clean, price, curr, now_iso))
                    elif abs(prev[0] - price) >= 0.01 or prev[1] != curr:
                        stats["price_changes"] += 1
                        if prev[1] == curr and price < prev[0]:
                            stats["price_drops"] += 1
                        history_params.append((prov_clean, oid, cat_clean, price, curr, now_iso))

                    if getattr(self, "_fts_enabled", False):
                        fts_delete_params.append((oid, prov_clean, cat_clean))
                        fts_insert_params.append((oid, prov_clean, cat_clean, title))

                conn.executemany(
                    """
                    INSERT INTO listings (
                        source, offer_id, category, domain, title, price, currency,
                        condition, is_new, has_delivery, url, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, offer_id, category) DO UPDATE SET
                        title=excluded.title,
                        price=excluded.price,
                        currency=excluded.currency,
                        condition=excluded.condition,
                        is_new=excluded.is_new,
                        has_delivery=excluded.has_delivery,
                        url=excluded.url,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    upsert_params,
                )

                if history_params:
                    conn.executemany(
                        """
                        INSERT INTO price_history (source, offer_id, category, price, currency, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        history_params,
                    )

                if getattr(self, "_fts_enabled", False) and fts_insert_params:
                    conn.executemany(
                        "DELETE FROM listings_fts WHERE offer_id = ? AND source = ? AND category = ?",
                        fts_delete_params,
                    )
                    conn.executemany(
                        "INSERT INTO listings_fts (offer_id, source, category, title) VALUES (?, ?, ?, ?)",
                        fts_insert_params,
                    )

                conn.commit()
                stats["upserted"] = len(upsert_params)
                return stats
            finally:
                conn.close()

    def search_fts(
        self,
        query: str,
        category: Optional[str] = None,
        provider: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Executes a fast FTS5 full-text search (with LIKE fallback) across cached listings.
        """
        with self._lock:
            conn = self._connect()
            try:
                clauses = ["1=1"]
                params: List[Any] = []

                if category:
                    clauses.append("l.category = ?")
                    params.append(category.lower().strip())
                if provider and provider.lower() not in ("all", "multi"):
                    clauses.append("l.source = ?")
                    params.append(provider.lower().strip())
                if min_price is not None:
                    clauses.append("l.price >= ?")
                    params.append(float(min_price))
                if max_price is not None:
                    clauses.append("l.price <= ?")
                    params.append(float(max_price))

                q_clean = (query or "").strip()
                if q_clean and getattr(self, "_fts_enabled", False):
                    tokens = [t for t in q_clean.replace('"', " ").split() if t]
                    fts_expr = " AND ".join(f'"{t}"*' for t in tokens)
                    sql = f"""
                        SELECT l.payload_json
                        FROM listings_fts f
                        JOIN listings l
                          ON l.offer_id = f.offer_id AND l.source = f.source AND l.category = f.category
                        WHERE f.listings_fts MATCH ? AND {' AND '.join(clauses)}
                        ORDER BY l.price ASC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, [fts_expr] + params + [limit]).fetchall()
                else:
                    if q_clean:
                        clauses.append("LOWER(l.title) LIKE ?")
                        params.append(f"%{q_clean.lower()}%")
                    sql = f"""
                        SELECT l.payload_json
                        FROM listings l
                        WHERE {' AND '.join(clauses)}
                        ORDER BY l.price ASC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, params + [limit]).fetchall()

                return [json.loads(r["payload_json"]) for r in rows]
            finally:
                conn.close()

    def get_price_history(self, offer_id: str, provider: str = "olx") -> List[Dict[str, Any]]:
        """Returns chronological price history records for a specific listing."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT price, currency, recorded_at
                    FROM price_history
                    WHERE offer_id = ? AND source = ?
                    ORDER BY recorded_at ASC, id ASC
                    """,
                    (str(offer_id), provider.lower().strip()),
                ).fetchall()
                return [
                    {"price": float(r["price"]), "currency": str(r["currency"]), "recorded_at": str(r["recorded_at"])}
                    for r in rows
                ]
            finally:
                conn.close()

    def get_price_delta(self, offer_id: str, provider: str = "olx") -> Optional[Dict[str, Any]]:
        """
        Computes price change between the earliest recorded observation, peak price, and the latest price.
        Returns None if fewer than 2 distinct price observations exist.
        """
        history = self.get_price_history(offer_id, provider=provider)
        if len(history) < 2:
            return None
        first = history[0]
        last = history[-1]
        if first["currency"] != last["currency"] or first["price"] <= 0:
            return None
        same_curr_prices = [h["price"] for h in history if h["currency"] == last["currency"]]
        peak_price = max(same_curr_prices) if same_curr_prices else first["price"]
        min_price = min(same_curr_prices) if same_curr_prices else last["price"]
        diff = round(last["price"] - first["price"], 2)
        pct = round((diff / first["price"]) * 100.0, 1)
        drop_from_peak_pct = round(((last["price"] - peak_price) / peak_price) * 100.0, 1) if peak_price > 0 else 0.0
        return {
            "initial_price": first["price"],
            "peak_price": peak_price,
            "min_price": min_price,
            "current_price": last["price"],
            "currency": last["currency"],
            "delta": diff,
            "delta_pct": pct,
            "drop_from_peak_pct": drop_from_peak_pct,
            "observations": len(history),
        }

    def get_price_trend(self, offer_id: str, provider: str = "olx") -> Optional[Dict[str, Any]]:
        """Alias for get_price_delta returning full trend summary and history."""
        delta = self.get_price_delta(offer_id, provider=provider)
        if delta is None:
            return None
        delta["history"] = self.get_price_history(offer_id, provider=provider)
        return delta

    def get_batch_price_deltas(
        self,
        category: Optional[str] = None,
        provider: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Computes price deltas for all multi-observation listings in a single SQL query.
        Returns a dictionary keyed by `offer_id` -> delta dict.
        """
        with self._lock:
            conn = self._connect()
            try:
                clauses = ["1=1"]
                params: List[Any] = []
                if category:
                    clauses.append("l.category = ?")
                    params.append(category.lower().strip())
                if provider and provider.lower() not in ("all", "multi", "unified"):
                    clauses.append("l.source = ?")
                    params.append(provider.lower().strip())
                if domain:
                    clauses.append("l.domain = ?")
                    params.append(domain.lower().strip())

                sql = f"""
                    SELECT
                        l.offer_id,
                        l.source,
                        l.category,
                        l.price AS current_price,
                        l.currency,
                        COUNT(ph.id) AS obs_count,
                        MAX(ph.price) AS peak_price,
                        MIN(ph.price) AS min_price,
                        (
                            SELECT ph0.price
                            FROM price_history ph0
                            WHERE ph0.source = l.source
                              AND ph0.offer_id = l.offer_id
                              AND ph0.category = l.category
                              AND ph0.currency = l.currency
                            ORDER BY ph0.recorded_at ASC, ph0.id ASC
                            LIMIT 1
                        ) AS initial_price
                    FROM listings l
                    JOIN price_history ph
                      ON ph.source = l.source
                     AND ph.offer_id = l.offer_id
                     AND ph.category = l.category
                     AND ph.currency = l.currency
                    WHERE {' AND '.join(clauses)}
                    GROUP BY l.source, l.offer_id, l.category
                    HAVING COUNT(ph.id) >= 2
                """
                rows = conn.execute(sql, params).fetchall()
                result: Dict[str, Dict[str, Any]] = {}
                for r in rows:
                    oid = str(r["offer_id"])
                    init_p = float(r["initial_price"] or r["current_price"] or 0.0)
                    curr_p = float(r["current_price"] or 0.0)
                    peak_p = float(r["peak_price"] or init_p)
                    min_p = float(r["min_price"] or curr_p)
                    if init_p <= 0 or peak_p <= 0:
                        continue
                    diff = round(curr_p - init_p, 2)
                    pct = round((diff / init_p) * 100.0, 1)
                    drop_peak_pct = round(((curr_p - peak_p) / peak_p) * 100.0, 1)
                    result[oid] = {
                        "offer_id": oid,
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "initial_price": init_p,
                        "peak_price": peak_p,
                        "min_price": min_p,
                        "current_price": curr_p,
                        "currency": str(r["currency"]),
                        "delta": diff,
                        "delta_pct": pct,
                        "drop_from_peak_pct": drop_peak_pct,
                        "observations": int(r["obs_count"]),
                    }
                return result
            finally:
                conn.close()

    def has_been_alerted(self, source: str, offer_id: str, category: str, price: float) -> bool:
        """True if an alert was already sent for this listing at the same or a lower price."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT alerted_price FROM alert_log WHERE source = ? AND offer_id = ? AND category = ?",
                    (source, str(offer_id), category),
                ).fetchone()
                return row is not None and float(price) >= float(row["alerted_price"]) - 0.005
            finally:
                conn.close()

    def mark_alerted(self, source: str, offer_id: str, category: str, price: float) -> None:
        """Records (or lowers) the price at which this listing was last alerted."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO alert_log (source, offer_id, category, alerted_price, alerted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source, str(offer_id), category, float(price), datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

    def find_price_drops(
        self,
        category: Optional[str] = None,
        provider: Optional[str] = None,
        domain: Optional[str] = None,
        min_drop_pct: float = 3.0,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        Finds listings whose current price has dropped by at least `min_drop_pct`%
        below their historical peak/initial price.
        """
        with self._lock:
            conn = self._connect()
            try:
                clauses = ["1=1"]
                params: List[Any] = []
                if category:
                    clauses.append("l.category = ?")
                    params.append(category.lower().strip())
                if provider and provider.lower() not in ("all", "multi", "unified"):
                    clauses.append("l.source = ?")
                    params.append(provider.lower().strip())
                if domain:
                    clauses.append("l.domain = ?")
                    params.append(domain.lower().strip())

                sql = f"""
                    SELECT
                        l.offer_id,
                        l.source,
                        l.category,
                        l.domain,
                        l.title,
                        l.price AS current_price,
                        l.currency,
                        l.condition,
                        l.is_new,
                        l.url,
                        l.updated_at,
                        COUNT(ph.id) AS obs_count,
                        MAX(ph.price) AS peak_price,
                        MIN(ph.price) AS min_price,
                        (
                            SELECT ph0.price
                            FROM price_history ph0
                            WHERE ph0.source = l.source
                              AND ph0.offer_id = l.offer_id
                              AND ph0.category = l.category
                              AND ph0.currency = l.currency
                            ORDER BY ph0.recorded_at ASC, ph0.id ASC
                            LIMIT 1
                        ) AS initial_price
                    FROM listings l
                    JOIN price_history ph
                      ON ph.source = l.source
                     AND ph.offer_id = l.offer_id
                     AND ph.category = l.category
                     AND ph.currency = l.currency
                    WHERE {' AND '.join(clauses)}
                    GROUP BY l.source, l.offer_id, l.category
                    HAVING COUNT(ph.id) >= 2 AND l.price < MAX(ph.price)
                """
                rows = conn.execute(sql, params).fetchall()
                drops: List[Dict[str, Any]] = []
                for r in rows:
                    curr_p = float(r["current_price"] or 0.0)
                    peak_p = float(r["peak_price"] or curr_p)
                    init_p = float(r["initial_price"] or peak_p)
                    if peak_p <= 0 or curr_p >= peak_p:
                        continue
                    drop_amt = round(peak_p - curr_p, 2)
                    drop_pct = round((drop_amt / peak_p) * 100.0, 1)
                    if drop_pct < float(min_drop_pct):
                        continue
                    drops.append({
                        "offer_id": str(r["offer_id"]),
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "domain": str(r["domain"] or ""),
                        "title": str(r["title"]),
                        "initial_price": init_p,
                        "peak_price": peak_p,
                        "current_price": curr_p,
                        "currency": str(r["currency"]),
                        "drop_amount": drop_amt,
                        "drop_pct": drop_pct,
                        "delta_pct": -drop_pct,
                        "observations": int(r["obs_count"]),
                        "condition": str(r["condition"] or "used"),
                        "is_new": bool(r["is_new"]),
                        "url": str(r["url"] or ""),
                        "updated_at": str(r["updated_at"] or ""),
                    })
                drops.sort(key=lambda x: (-x["drop_pct"], x["current_price"]))
                return drops[: max(1, int(limit))]
            finally:
                conn.close()

    def stats(self, domain: Optional[str] = None) -> Dict[str, Any]:
        """Returns summary metrics for the SQLite MarketStore and price history table."""
        with self._lock:
            conn = self._connect()
            try:
                if domain:
                    dom = domain.lower().strip()
                    total_listings = conn.execute(
                        "SELECT COUNT(*) FROM listings WHERE domain = ?", (dom,)
                    ).fetchone()[0]
                    by_cat_rows = conn.execute(
                        "SELECT category, COUNT(*) AS cnt FROM listings WHERE domain = ? GROUP BY category",
                        (dom,),
                    ).fetchall()
                else:
                    total_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
                    by_cat_rows = conn.execute(
                        "SELECT category, COUNT(*) AS cnt FROM listings GROUP BY category"
                    ).fetchall()

                total_history = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
                tracked_multi = conn.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT source, offer_id, category
                        FROM price_history
                        GROUP BY source, offer_id, category
                        HAVING COUNT(id) >= 2
                    )
                    """
                ).fetchone()[0]
                return {
                    "db_path": str(self.db_path),
                    "fts5_enabled": bool(getattr(self, "_fts_enabled", False)),
                    "total_listings": int(total_listings),
                    "total_history_points": int(total_history),
                    "multi_observation_listings": int(tracked_multi),
                    "by_category": {str(r["category"]): int(r["cnt"]) for r in by_cat_rows},
                }
            finally:
                conn.close()


# Global singleton store
market_store = MarketStore()
