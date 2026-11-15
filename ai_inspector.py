"""
Unified Hardware AI Semantic Inspector Engine.

Provides shared multi-provider LLM dispatch, persistent AuditLedger memoization,
instant heuristic pre-disqualification (saving tokens on obvious dead/scam ads),
and concurrent multi-batch execution across GPU, CPU, Motherboard, RAM, and PSU.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional

from ai_providers import AIProviderClient
from audit_ledger import ledger
from market_client import check_delivery_refusal

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

HARD_DISQUALIFY_KEYWORDS = (
    "critical defect",
    "non-working",
    "sold for parts",
    "stopped working",
    "prepayment risk",
    "fire hazard",
    "explosion hazard",
    "bent or broken",
    "missing pin",
    "dead memory channel",
    "does not power on",
    "burned vrm",
    "corrupted or bricked",
    "missing all modular cables",
    "missing critical pcie",
    "multi-model catalog",
    "stripped board",
)


def clean_description(raw_desc: str, max_len: int = 3500) -> str:
    if not raw_desc:
        return ""
    no_tags = _HTML_TAG_RE.sub(" ", raw_desc)
    return _WS_RE.sub(" ", no_tags).strip()[:max_len]


class BaseHardwareAIInspector:
    """Base semantic inspector shared by all 5 component categories."""

    category: str = "hardware"

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        cli_path: Optional[str] = None,
    ):
        # Backwards compatibility if legacy code passed cli_path=...
        if cli_path and not provider:
            provider = "agy"
        self.client = AIProviderClient(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
        )

    @property
    def provider_label(self) -> str:
        return self.client.display_name

    def build_candidate_payload(self, idx: int, cand: Dict[str, Any], clean_desc: str) -> Dict[str, Any]:
        raise NotImplementedError

    def build_prompt(self, payload_json: str, sort_by: str) -> str:
        raise NotImplementedError

    def _check_instant_disqualification(self, cand: Dict[str, Any], raw_desc: str) -> Optional[Dict[str, Any]]:
        """
        Short-circuits obvious delivery refusals or critical defects discovered in the
        full unredacted description before spending LLM tokens.
        """
        title = cand.get("summary", {}).get("title", "")
        if check_delivery_refusal(f"{title} {raw_desc}"):
            return {
                "verdict": "REJECTED",
                "confidence_score": 98,
                "reasons": ["Seller explicitly refuses postal shipping / OLX Delivery in listing text."],
                "auto_filtered": True,
            }

        heur = cand.get("heuristic") or {}
        red_flags = heur.get("red_flags") or []
        if red_flags:
            return {
                "verdict": "REJECTED",
                "confidence_score": 95,
                "reasons": red_flags[:2],
                "auto_filtered": True,
            }
        return None

    def inspect_and_rank_batch(
        self,
        candidates: List[Dict[str, Any]],
        sort_by: str = "best",
        re_audit: bool = False,
    ) -> Dict[str, Any]:
        """
        Evaluates a batch of candidates using:
          1. Instant heuristic hard-defect / delivery-refusal check (0ms, 0 tokens)
          2. Persistent SHA-256 AuditLedger lookup (0ms, 0 tokens)
          3. Active AI Provider JSON batch inference for genuinely uninspected items
        """
        if not candidates:
            return {"approved": [], "rejected": []}

        uninspected: List[Dict[str, Any]] = []
        auto_ledger_updates: List[Dict[str, Any]] = []

        for c in candidates:
            summary = c.get("summary") or {}
            offer_id = str(summary.get("id") or c.get("id", ""))
            title = summary.get("title", c.get("model", ""))
            price = float(c.get("price", 0.0))
            raw_desc = (c.get("detail") or {}).get("description") or summary.get("description", "") or (summary.get("raw") or {}).get("description", "")

            # 1. Instant post-detail heuristic gate
            instant_reject = self._check_instant_disqualification(c, raw_desc)
            if instant_reject:
                c["ai_eval"] = instant_reject
                c.setdefault("eval", {})["is_ai_rejected"] = True
                c.setdefault("eval", {})["is_ai_approved"] = False
                auto_ledger_updates.append({
                    "offer_id": offer_id,
                    "category": self.category,
                    "verdict": "REJECTED",
                    "confidence_score": instant_reject["confidence_score"],
                    "reasons": instant_reject["reasons"],
                    "title": title,
                    "price": price,
                    "description": raw_desc,
                    "url": summary.get("url") or c.get("url", ""),
                    "has_delivery": bool(summary.get("has_olx_delivery", True)),
                    "provider": "heuristic-gate",
                    "model": "deterministic",
                })
                continue

            # 2. Persistent AuditLedger lookup
            if not re_audit:
                has_deliv = summary.get("has_olx_delivery", True)
                cached = ledger.get_audit(
                    offer_id,
                    title=title,
                    price=price,
                    description=raw_desc,
                    has_delivery=has_deliv,
                )
                if cached:
                    c["ai_eval"] = {
                        "verdict": cached.get("verdict", "APPROVED"),
                        "confidence_score": cached.get("confidence_score", 85),
                        "reasons": cached.get("reasons", ["Verified from persistent audit ledger"]),
                        "provider": cached.get("provider", "cache"),
                        "from_cache": True,
                    }
                    continue

            uninspected.append(c)

        if auto_ledger_updates:
            ledger.record_batch(auto_ledger_updates)

        # All items resolved without needing an LLM call!
        if not uninspected:
            return self._partition(candidates)

        # Build compact JSON payload for uninspected items
        payload = []
        for idx, c in enumerate(uninspected, 1):
            raw_desc = (c.get("detail") or {}).get("description") or c["summary"].get("description", "")
            c_desc = clean_description(raw_desc)
            payload.append(self.build_candidate_payload(idx, c, c_desc))

        prompt = self.build_prompt(json.dumps(payload, ensure_ascii=False, indent=2), sort_by)

        try:
            ai_result, used_prov, used_model = self.client.generate_json(prompt)
            self._apply_and_persist_ai_results(uninspected, ai_result, used_prov, used_model)
        except Exception as e:
            print(f"Notice: AI Inspection fallback ({e})")
            self._apply_fallback(uninspected)

        return self._partition(candidates)

    def inspect_batches_concurrently(
        self,
        batches: List[List[Dict[str, Any]]],
        sort_by: str = "best",
        max_workers: int = 3,
    ) -> Dict[str, Any]:
        """Executes multiple LLM audit batches in parallel threads."""
        if not batches:
            return {"approved": [], "rejected": []}
        if len(batches) == 1:
            return self.inspect_and_rank_batch(batches[0], sort_by=sort_by)

        approved_all: List[Dict[str, Any]] = []
        rejected_all: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(batches), max_workers)) as pool:
            futures = [pool.submit(self.inspect_and_rank_batch, b, sort_by) for b in batches]
            for fut in futures:
                res = fut.result()
                approved_all.extend(res.get("approved", []))
                rejected_all.extend(res.get("rejected", []))
        return {"approved": approved_all, "rejected": rejected_all}

    def _apply_and_persist_ai_results(
        self,
        newly_inspected: List[Dict[str, Any]],
        ai_result: Dict[str, Any],
        used_prov: str,
        used_model: str,
    ):
        eval_map = {
            int(e["candidate_id"]): e
            for e in (ai_result.get("evaluations") or [])
            if isinstance(e, dict) and "candidate_id" in e
        }
        disq_set = {int(x) for x in (ai_result.get("disqualified_ids") or []) if str(x).isdigit()}

        batch_records = []
        for idx, cand in enumerate(newly_inspected, 1):
            ai_eval = dict(eval_map.get(idx) or {})
            verdict = str(ai_eval.get("verdict", "APPROVED")).upper()
            if idx in disq_set:
                verdict = "REJECTED"
            conf = int(ai_eval.get("confidence_score", 80))
            reasons = ai_eval.get("reasons") or ["Inspected by AI"]
            ai_eval["verdict"] = verdict
            ai_eval["confidence_score"] = conf
            ai_eval["reasons"] = reasons
            ai_eval["provider"] = used_prov
            ai_eval["model"] = used_model
            cand["ai_eval"] = ai_eval
            cand.setdefault("eval", {})["is_ai_rejected"] = (verdict == "REJECTED")
            cand.setdefault("eval", {})["is_ai_approved"] = (verdict == "APPROVED")
            cand["eval"]["audit"] = ai_eval

            cand_sum = cand.get("summary") or {}
            raw_desc = (cand.get("detail") or {}).get("description") or cand_sum.get("description", "") or (cand_sum.get("raw") or {}).get("description", "")
            batch_records.append({
                "offer_id": str(cand_sum.get("id") or cand.get("id", "")),
                "category": self.category,
                "verdict": verdict,
                "confidence_score": conf,
                "reasons": reasons,
                "title": cand_sum.get("title", cand.get("model", "")),
                "price": float(cand.get("price", 0.0)),
                "description": raw_desc,
                "url": cand_sum.get("url") or cand.get("url", ""),
                "has_delivery": bool(cand_sum.get("has_olx_delivery", True)),
                "provider": used_prov,
                "model": used_model,
            })

        ledger.record_batch(batch_records)

    def _apply_fallback(self, uninspected: List[Dict[str, Any]]):
        for c in uninspected:
            heur = c.get("heuristic") or {}
            risk = heur.get("risk_score", 20)
            flags = heur.get("red_flags") or []
            if flags or risk >= 45:
                c["ai_eval"] = {
                    "verdict": "REJECTED",
                    "confidence_score": 80,
                    "reasons": flags[:2] or ["High heuristic risk score"],
                    "fallback": True,
                }
                c.setdefault("eval", {})["is_ai_rejected"] = True
                c.setdefault("eval", {})["is_ai_approved"] = False
            else:
                conf = max(55, 92 - int(risk))
                c["ai_eval"] = {
                    "verdict": "CAUTION" if risk >= 25 else "APPROVED",
                    "confidence_score": conf,
                    "reasons": ["Heuristic check passed (AI offline/fallback active)"],
                    "fallback": True,
                }
                c.setdefault("eval", {})["is_ai_rejected"] = False
                c.setdefault("eval", {})["is_ai_approved"] = True

    @staticmethod
    def _partition(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        approved = []
        rejected = []
        for cand in candidates:
            verdict = (cand.get("ai_eval") or {}).get("verdict", "APPROVED")
            if verdict == "REJECTED":
                rejected.append(cand)
            else:
                approved.append(cand)
        return {"approved": approved, "rejected": rejected}
