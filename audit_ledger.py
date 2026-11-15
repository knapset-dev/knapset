"""
Audit Ledger: Persistent Memoization Engine for Hardware Quality Inspections.

Tracks AI inspection verdicts across all providers (Gemini, OpenAI, Anthropic, Groq,
DeepSeek, OpenRouter, Ollama, CLI agents) using SHA-256 content-hash verification
to guarantee 0-token, sub-millisecond re-evaluations across repeated runs.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

DATA_DIR = Path(__file__).resolve().parent / "data"
LEDGER_FILE = DATA_DIR / "audit_ledger.json"


def normalize_category(category: Optional[str]) -> Optional[str]:
    """Normalizes hardware category names and aliases to a consistent canonical key."""
    if not category:
        return None
    c = category.lower().strip()
    if c in ("mobo", "motherboard", "motherboards", "mobos", "materinskie-platy"):
        return "mobo"
    if c in ("gpu", "gpus", "videokarty"):
        return "gpu"
    if c in ("cpu", "cpus", "protsessory"):
        return "cpu"
    if c in ("ram", "rams", "memory", "moduli-pamyati"):
        return "ram"
    if c in ("psu", "psus", "bloki-pitaniya"):
        return "psu"
    return c


def compute_content_hash(title: str, price: float, description: str, has_delivery: bool = True) -> str:
    """Computes SHA-256 fingerprint of the ad content to detect seller edits or delivery status toggles."""
    t_norm = (title or "").strip()
    d_norm = (description or "").strip()[:1000]
    deliv_tag = "d:1" if has_delivery else "d:0"
    payload = f"{t_norm}|{float(price or 0.0):.2f}|{deliv_tag}|{d_norm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class AuditLedger:
    """Thread-safe persistent audit ledger with atomic disk flushes."""

    def __init__(self, ledger_path: Path = LEDGER_FILE):
        self.ledger_path = ledger_path
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if not self.ledger_path.exists():
            self._cache = {}
            return
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._cache = data.get("audits", {}) if isinstance(data, dict) else {}
        except Exception:
            self._cache = {}

    def _save_unlocked(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "metadata": {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_records": len(self._cache),
            },
            "audits": self._cache,
        }
        tmp_path = self.ledger_path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.ledger_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get_audit(
        self,
        offer_id: str,
        title: str = "",
        price: float = 0.0,
        description: str = "",
        has_delivery: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves a cached audit verdict for an offer.
        Verifies content hash when comparison fields are supplied; if the seller
        edited title, price, description, or delivery status, returns None to trigger re-audit.
        """
        str_id = str(offer_id)
        record = self._cache.get(str_id)
        if not record:
            return None

        if (title or description or price) and record.get("hash"):
            current_hash = compute_content_hash(title, price, description, has_delivery=has_delivery)
            if record.get("hash") != current_hash:
                # Backward compatibility for legacy ledger entries recorded prior to delivery-tag payload update
                if record.get("has_delivery") is None and has_delivery:
                    t_norm = (title or "").strip()
                    d_norm = (description or "").strip()[:1000]
                    legacy_payload = f"{t_norm}|{float(price or 0.0):.2f}|{d_norm}"
                    legacy_hash = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()[:16]
                    if record.get("hash") == legacy_hash:
                        return record
                return None

        return record

    def _build_record(
        self,
        offer_id: str,
        category: str,
        verdict: str,
        confidence_score: int,
        reasons: List[str],
        title: str = "",
        price: float = 0.0,
        description: str = "",
        url: str = "",
        provider: str = "",
        model: str = "",
        has_delivery: bool = True,
    ) -> Dict[str, Any]:
        str_id = str(offer_id)
        content_hash = (
            compute_content_hash(title, price, description, has_delivery=has_delivery)
            if (title or description)
            else ""
        )
        rec = {
            "id": str_id,
            "category": normalize_category(category) or category.lower(),
            "verdict": str(verdict or "APPROVED").upper(),
            "confidence_score": int(confidence_score if confidence_score is not None else 75),
            "reasons": list(reasons or []),
            "hash": content_hash,
            "title": (title or "")[:120],
            "price": float(price or 0.0),
            "url": url or "",
            "has_delivery": bool(has_delivery),
            "audited_at": datetime.now(timezone.utc).isoformat(),
        }
        if provider:
            rec["provider"] = provider
        if model:
            rec["model"] = model
        return rec

    def record_audit(
        self,
        offer_id: str,
        category: str,
        verdict: str,
        confidence_score: int,
        reasons: List[str],
        title: str = "",
        price: float = 0.0,
        description: str = "",
        url: str = "",
        provider: str = "",
        model: str = "",
        has_delivery: bool = True,
    ):
        """Records a single AI audit verdict and persists atomically."""
        with self._lock:
            rec = self._build_record(
                offer_id=offer_id,
                category=category,
                verdict=verdict,
                confidence_score=confidence_score,
                reasons=reasons,
                title=title,
                price=price,
                description=description,
                url=url,
                provider=provider,
                model=model,
                has_delivery=has_delivery,
            )
            self._cache[rec["id"]] = rec
            self._save_unlocked()

    def record_batch(self, items: List[Dict[str, Any]]):
        """Records multiple AI audit verdicts with a single atomic disk write."""
        if not items:
            return
        with self._lock:
            for item in items:
                rec = self._build_record(
                    offer_id=item["offer_id"],
                    category=item["category"],
                    verdict=item.get("verdict", "APPROVED"),
                    confidence_score=item.get("confidence_score", 75),
                    reasons=item.get("reasons", []),
                    title=item.get("title", ""),
                    price=item.get("price", 0.0),
                    description=item.get("description", ""),
                    url=item.get("url", ""),
                    provider=item.get("provider", ""),
                    model=item.get("model", ""),
                    has_delivery=item.get("has_delivery", item.get("has_olx_delivery", True)),
                )
                self._cache[rec["id"]] = rec
            self._save_unlocked()

    def get_all_audits(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all audit records, optionally filtered by category."""
        records = list(self._cache.values())
        if category:
            cat_norm = normalize_category(category)
            records = [r for r in records if normalize_category(r.get("category")) == cat_norm]
        return records

    def stats(self) -> Dict[str, Any]:
        """Returns summary metrics for stored audits."""
        by_cat: Dict[str, int] = {}
        by_verdict: Dict[str, int] = {}
        for rec in self._cache.values():
            c = normalize_category(rec.get("category")) or "unknown"
            v = rec.get("verdict", "UNKNOWN")
            by_cat[c] = by_cat.get(c, 0) + 1
            by_verdict[v] = by_verdict.get(v, 0) + 1
        return {
            "total": len(self._cache),
            "by_category": by_cat,
            "by_verdict": by_verdict,
        }

    def clear(self, category: Optional[str] = None):
        """Clears audit records for a category or the entire ledger."""
        with self._lock:
            if category:
                cat_norm = normalize_category(category)
                self._cache = {k: v for k, v in self._cache.items() if normalize_category(v.get("category")) != cat_norm}
            else:
                self._cache = {}
            self._save_unlocked()

    def clear_approved(self, category: Optional[str] = None, provider: Optional[str] = None) -> int:
        """Clears approved audit records while preserving blacklisted/rejected items."""
        with self._lock:
            new_cache = {}
            count = 0
            cat_norm = normalize_category(category) if category else None
            for k, v in self._cache.items():
                if v.get("verdict") == "APPROVED":
                    if cat_norm and normalize_category(v.get("category")) != cat_norm:
                        new_cache[k] = v
                        continue
                    if provider and v.get("provider") != provider:
                        new_cache[k] = v
                        continue
                    count += 1
                    continue
                new_cache[k] = v
            self._cache = new_cache
            self._save_unlocked()
            return count

    def delete_audit(self, offer_id: str) -> bool:
        """Deletes a specific audit record by offer ID."""
        with self._lock:
            oid = str(offer_id)
            if oid in self._cache:
                del self._cache[oid]
                self._save_unlocked()
                return True
            return False


# Global singleton
ledger = AuditLedger()
