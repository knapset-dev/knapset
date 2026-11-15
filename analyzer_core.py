"""
Shared High-Performance Execution Core for all 5 Hardware Component Analyzers
(GPU, CPU, Motherboard, RAM, PSU).

Centralizes:
  1. Cross-repo freshest market cache loading & parallel async harvesting
  2. Unified Optimization Modes ('Mods') + legacy sort-by scoring
  3. Zero-waste pre-filtered Laser AI Audit loop (AuditLedger + multi-provider LLM)
  4. OSC-8 clickable ANSI box-table & verbose diagnostic rendering
  5. Universal dynamic currency conversions & formatting
"""

import argparse
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ai_providers import list_provider_names
from audit_ledger import ledger
from currency import convert_currency, format_price, get_active_currency
from market_client import MarketClient, load_market_cache

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;.*?\x1b\\")
_NON_ALNUM_BADGE_RE = re.compile(r"[^a-zA-Z0-9_\-+% ]")


def make_terminal_link(url: str, text: str) -> str:
    """Embeds an OSC-8 clickable terminal hyperlink."""
    if not url:
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def visible_len(s: str) -> int:
    return len(_ANSI_ESCAPE_RE.sub("", s))


def pad_ansi(s: str, width: int) -> str:
    pad = max(0, width - visible_len(s))
    return s + (" " * pad)


def fmt_deal_pct(discount_pct: float) -> str:
    """Formats % discount vs used market median (+20% = 20% below median = good deal)."""
    rounded = int(round(discount_pct))
    if rounded >= 12:
        return f"\033[32m-{rounded}%\033[0m"
    if rounded <= -15:
        return f"\033[33m+{abs(rounded)}%\033[0m"
    if rounded > 0:
        return f"-{rounded}%"
    if rounded < 0:
        return f"+{abs(rounded)}%"
    return "0%"


def compute_market_discount_pct(
    price: float,
    spec: Dict[str, Any],
    price_currency: Optional[str] = None,
    spec_currency: Optional[str] = None,
) -> float:
    """
    Returns percentage below used market median.
    Positive (+22.5) = 22.5% cheaper than typical market median (great deal).
    Negative (-15.0) = 15.0% above typical market median.
    Automatically handles currency conversion if price and spec currencies differ.
    """
    median = spec.get("market_used_median")
    if not median:
        mn = spec.get("market_used_min", 0)
        mx = spec.get("market_used_max", 0)
        median = (mn + mx) / 2.0 if (mn and mx) else 0.0
    if not median or price <= 0:
        return 0.0

    p_curr = price_currency
    s_curr = spec_currency or spec.get("currency")
    if p_curr and s_curr and p_curr != s_curr:
        median = convert_currency(median, from_currency=s_curr, to_currency=p_curr)

    return round(((median - price) / median) * 100.0, 1)


def is_listing_disqualified(
    heur: Optional[Dict[str, Any]],
    ai_eval: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when listing has hard heuristic red flags or a cached AI REJECTED verdict."""
    if ai_eval:
        if ai_eval.get("verdict") == "REJECTED":
            return True
        if ai_eval.get("verdict") == "APPROVED":
            return False
    if heur:
        if heur.get("red_flags"):
            return True
        if heur.get("is_mining_alert"):
            return True
        if heur.get("is_fake"):
            return True
    return False


def get_status_badge(cand: Dict[str, Any]) -> str:
    if cand.get("is_owned"):
        return "\033[36m[OWNED]\033[0m"

    summary = cand.get("summary") or {}
    raw = summary.get("raw") or cand.get("raw") or {}
    is_new = bool(cand.get("is_new") or summary.get("is_new") or raw.get("is_new") or cand.get("condition") == "new" or summary.get("condition") == "new")
    cond = str(cand.get("condition") or summary.get("condition") or raw.get("condition") or ("new" if is_new else "")).lower()
    is_outlet = bool(cond in ("refurbished", "outlet") or summary.get("outlet_reason") or raw.get("outlet_reason"))

    cond_badge = ""
    if is_new:
        cond_badge = "\033[36m[NEW]\033[0m "
    elif is_outlet:
        cond_badge = "\033[35m[OUTLET]\033[0m "

    ai_eval = cand.get("ai_eval")
    if ai_eval:
        verdict = str(ai_eval.get("verdict", "APPROVED")).upper()
        conf = ai_eval.get("confidence_score")
        if verdict == "REJECTED":
            reasons = ai_eval.get("reasons") or ["Defect"]
            r_clean = _NON_ALNUM_BADGE_RE.sub("", str(reasons[0]))[:8].strip() or "Flagged"
            return f"{cond_badge}\033[31m[REJ: {r_clean}]\033[0m"
        if verdict == "CAUTION":
            conf_str = f" ({conf}%)" if conf else ""
            return f"{cond_badge}\033[33m[CAUT{conf_str}]\033[0m"
        if ai_eval.get("provider") == "human":
            return f"{cond_badge}\033[32m[HUMAN]\033[0m"
        conf_str = f" ({conf}%)" if conf else ""
        return f"{cond_badge}\033[32m[OK{conf_str}]\033[0m"

    heur = cand.get("heuristic")
    if heur:
        flags = heur.get("red_flags") or []
        if flags:
            f_clean = _NON_ALNUM_BADGE_RE.sub("", str(flags[0]))[:6].strip() or "DEFECT"
            return f"{cond_badge}\033[31m[FLAG: {f_clean}]\033[0m"
        warns = heur.get("warnings") or []
        if warns and heur.get("risk_score", 0) >= 25:
            w_clean = _NON_ALNUM_BADGE_RE.sub("", str(warns[0]))[:6].strip() or "WARN"
            return f"{cond_badge}\033[33m[WARN: {w_clean}]\033[0m"
        if heur.get("risk_score", 0) <= 22:
            return f"{cond_badge}\033[32m[CLEAN]\033[0m"

    return f"{cond_badge}[UNCHECKED]"


def render_box_table(
    items: Sequence[Dict[str, Any]],
    columns: Sequence[Tuple[str, int, Callable[[int, Dict[str, Any]], str]]],
    max_rows: Optional[int] = None,
) -> None:
    """Renders a clean Unicode box table with ANSI/OSC-8 width alignment."""
    if not items:
        return
    display_items = items[:max_rows] if max_rows else items
    widths = [w for _, w, _ in columns]

    top_border = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    head_line = "│ " + " │ ".join(f"{h:<{w}}" for h, w, _ in columns) + " │"
    mid_border = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot_border = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

    print(top_border)
    print(head_line)
    print(mid_border)
    for idx, cand in enumerate(display_items, 1):
        cells = [pad_ansi(fn(idx, cand), w) for _, w, fn in columns]
        print("│ " + " │ ".join(cells) + " │")
    print(bot_border + "\n")


def add_common_mode_and_ai_args(parser: argparse.ArgumentParser) -> None:
    """Adds unified Optimization Modes ('Mods') and Multi-Provider AI CLI options."""
    try:
        from extensions.pc.modes import get_mode_names
        mode_names_str = ", ".join(get_mode_names())
    except ImportError:
        mode_names_str = "best, sweet-spot, value, safe, deal-hunter"

    mode_group = parser.add_argument_group("Optimization Modes ('Mods')")
    mode_group.add_argument(
        "--mode", "-M",
        type=str,
        default=None,
        help=(
            f"Optimization mode ({mode_names_str}). "
            "Overrides --sort/--sort-by with multi-factor ranking (e.g. 'best', 'sweet-spot', 'safe', 'deal-hunter')."
        ),
    )
    mode_group.add_argument(
        "--weights",
        type=str,
        default=None,
        help="Custom weight overrides, e.g. 'perf=0.5,value=0.3,safety=0.2' (auto-enables custom weighting)",
    )
    mode_group.add_argument(
        "--max-risk",
        type=int,
        default=None,
        help="Maximum acceptable heuristic risk score (0-100)",
    )

    ai_group = parser.add_argument_group("AI Provider & Audit Ledger")
    ai_group.add_argument(
        "--ai-provider",
        choices=list_provider_names(),
        default=None,
        help="AI provider backend (auto, gemini, openai, anthropic, groq, deepseek, openrouter, mistral, ollama, openai-compat, agy, claude-cli, gemini-cli)",
    )
    ai_group.add_argument(
        "--ai-model",
        type=str,
        default=None,
        help="Override AI model name (e.g. gemini-2.5-flash, gpt-4o-mini, llama-3.3-70b-versatile, qwen2.5:14b)",
    )
    ai_group.add_argument(
        "--ai-base-url",
        type=str,
        default=None,
        help="Custom API base URL for ollama or openai-compat (LM Studio / vLLM / llama.cpp)",
    )
    ai_group.add_argument(
        "--re-audit",
        action="store_true",
        help="Force fresh AI inspection even if listing is cached in AuditLedger",
    )
    ai_group.add_argument(
        "--human-review", "--interactive", "--validate", "--human",
        dest="human_review",
        action="store_true",
        help="Interactive human validation: surface top deals and CPU+Platform proposals for manual review",
    )
    if "--show-rejected" not in parser._option_string_actions:
        ai_group.add_argument(
            "--show-rejected",
            action="store_true",
            help="Include flagged/disqualified items in the final results table",
        )


def fetch_category_offers(
    client: MarketClient,
    category: str,
    search_fn: Callable[..., List[Dict[str, Any]]],
    query: str = "",
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    max_pages: int = 25,
    harvest: bool = False,
    deep: Optional[bool] = None,
    refresh: bool = False,
) -> List[Dict[str, Any]]:
    """
    Loads listings from the freshest local market cache or falls back to search_fn.
    """
    if not query:
        offers, updated_at, cache_path = load_market_cache(category)
        if offers:
            ts_str = updated_at[:19] if updated_at else "recent"
            name = cache_path.name if cache_path else f"{category}_market_cache.json"
            print(f"[1/3] Loaded {len(offers)} {category.upper()} listings from local cache ({name}, saved: {ts_str}).")
            return offers

    print(f"[1/3] Querying {category.upper()} listings from local provider (query='{query or '*'}')...")
    offers: List[Dict[str, Any]] = []
    seen_ids = set()
    for page in range(max_pages):
        batch = search_fn(query=query, min_price=min_price, max_price=max_price, offset=page * 40, limit=40)
        if not batch:
            break
        new_cnt = 0
        for item in batch:
            oid = item.get("id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                offers.append(item)
                new_cnt += 1
        if new_cnt == 0:
            break
    return offers


def prepare_entry_metadata(
    category: str,
    entry: Dict[str, Any],
    spec: Dict[str, Any],
    detector: Any,
    re_audit: bool = False,
) -> None:
    """Attaches instant heuristic evaluation, market discount %, and cached AuditLedger verdict."""
    item = entry["summary"]
    price = entry["price"]
    desc = item.get("description") or (item.get("raw") or {}).get("description", "")
    heur = detector.analyze_listing(item, item.get("raw") or {}, spec)
    has_deliv = item.get("has_olx_delivery", True)
    cached_audit = None
    if not re_audit:
        cached_audit = ledger.get_audit(
            str(item.get("id")),
            title=item.get("title", ""),
            price=price,
            description=desc,
            has_delivery=has_deliv,
        )
    entry["heuristic"] = heur
    entry["ai_eval"] = cached_audit
    entry["discount_pct"] = compute_market_discount_pct(
        price,
        spec,
        price_currency=entry.get("currency") or item.get("currency"),
    )


def sort_entries(
    category: str,
    items: List[Dict[str, Any]],
    mode_profile: Optional[Any],
    legacy_key_fn: Callable[[Dict[str, Any]], Any],
) -> str:
    """
    Sorts `items` in-place either by `mode_profile` composite score or by `legacy_key_fn`
    (with disqualified/defective items pushed below clean working candidates).
    Returns human-readable sort label.
    """
    if mode_profile is not None:
        try:
            from extensions.pc.modes import score_component
            for it in items:
                it["mode_score"] = score_component(category, it, mode_profile)
        except ImportError:
            for it in items:
                it["mode_score"] = float(it.get("score") or it.get("discount_pct") or 0.0)
        items.sort(key=lambda x: (-x["mode_score"], x["price"]))
        return f"MODE:{mode_profile.name.upper()} ({mode_profile.title})"

    items.sort(
        key=lambda x: (
            1 if is_listing_disqualified(x.get("heuristic"), x.get("ai_eval")) else 0,
            legacy_key_fn(x),
        )
    )
    return ""


def run_laser_audit_loop(
    matched_items: List[Dict[str, Any]],
    spec_key: str,
    client: MarketClient,
    detector: Any,
    ai_inspector: Any,
    target_approved: int,
    sort_label: str,
    max_risk: Optional[int] = None,
    re_audit: bool = False,
    batch_size: int = 7,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Executes Step 3 Laser AI Audit:
      - Pre-disqualified items (hard defects, prepayment scams, cached REJECTED) never burn LLM tokens.
      - Cached APPROVED items in AuditLedger resolve in 0.0ms with 0 HTTP calls.
      - Unchecked viable finalists fetch full OLX details in batch and run through the active AI provider.
    """
    print(f"[3/3] Laser AI Audit via {ai_inspector.provider_label}...")

    all_approved: List[Dict[str, Any]] = []
    all_rejected: List[Dict[str, Any]] = []
    pending_viable: List[Dict[str, Any]] = []

    for cand in matched_items:
        heur = cand.get("heuristic") or {}
        cached_ai = cand.get("ai_eval")

        # Enforce explicit max_risk ceiling if requested by mode or CLI
        if max_risk is not None and heur.get("risk_score", 0) > max_risk and not heur.get("red_flags"):
            # Soft skip if over strict risk cap (e.g. safe mode <= 15) unless no other items exist
            cand.setdefault("_over_risk_cap", True)

        if is_listing_disqualified(heur, cached_ai if not re_audit else None):
            if not cached_ai:
                cand["ai_eval"] = {
                    "verdict": "REJECTED",
                    "confidence_score": 95,
                    "reasons": (heur.get("red_flags") or ["Disqualified by heuristic defect filter"])[:2],
                    "auto_filtered": True,
                }
            all_rejected.append(cand)
            continue

        if max_risk is not None and heur.get("risk_score", 0) > max_risk:
            continue

        if cached_ai and not re_audit:
            if str(cached_ai.get("verdict", "APPROVED")).upper() != "REJECTED":
                all_approved.append(cand)
                if len(all_approved) >= target_approved:
                    break
            else:
                all_rejected.append(cand)
            continue

        pending_viable.append(cand)

    idx = 0
    if all_approved:
        print(f"   Instant Ledger Hit: {len(all_approved)} verified finalist(s) loaded in 0ms (0 tokens).")

    while len(all_approved) < target_approved and idx < len(pending_viable):
        needed = target_approved - len(all_approved)
        take = max(batch_size, min(batch_size * 2, needed + 2)) if needed > batch_size else batch_size
        window = pending_viable[idx : idx + take]
        idx += len(window)
        if not window:
            break

        sub_batches = [window[i : i + batch_size] for i in range(0, len(window), batch_size)]
        all_ids = [c["summary"]["id"] for c in window]
        print(f"   • Auditing {len(window)} viable finalist(s)...")
        details_map = client.get_batch_details(all_ids)

        for cand in window:
            oid = str(cand["summary"]["id"])
            detail = details_map.get(oid) or cand["summary"].get("raw") or {}
            cand["detail"] = detail
            cand["heuristic"] = detector.analyze_listing(cand["summary"], detail, cand[spec_key])

        if len(sub_batches) > 1:
            ai_res = ai_inspector.inspect_batches_concurrently(sub_batches, sort_by=sort_label)
        else:
            ai_res = ai_inspector.inspect_and_rank_batch(sub_batches[0], sort_by=sort_label, re_audit=re_audit)

        all_approved.extend(ai_res.get("approved", []))
        all_rejected.extend(ai_res.get("rejected", []))

    return all_approved[: max(target_approved, len(all_approved))], all_rejected


def print_verbose_evaluations(
    spec_key: str,
    all_approved: Sequence[Dict[str, Any]],
    all_rejected: Sequence[Dict[str, Any]],
) -> None:
    print("=" * 84)
    print("AI & HEURISTIC DETAILED EVALUATIONS:")
    print("=" * 84)
    for idx, cand in enumerate(all_approved, 1):
        spec = cand[spec_key]
        ai_eval = cand.get("ai_eval") or {}
        heur = cand.get("heuristic") or {}
        risk_lvl = spec.get("risk_level", "LOW")
        risk_notes = spec.get("risk_notes", "")
        prov = ai_eval.get("provider", "ai")
        curr = cand.get("currency") or (cand.get("summary") or {}).get("currency")
        p_str = format_price(cand["price"], curr)

        print(f"\n#{idx}: {spec['model']} ({p_str}) [{prov}] — {cand['summary']['url']}")
        if risk_lvl in ("CRITICAL", "HIGH"):
            print(f"   [{risk_lvl} ARCH RISK] {risk_notes}")
        for r in ai_eval.get("reasons") or []:
            print(f"   AI note: {r}")
        for w in heur.get("warnings") or []:
            if not any(w in r for r in (ai_eval.get("reasons") or [])):
                print(f"   Heuristic note: {w}")

    if all_rejected:
        print("\n" + "=" * 84)
        print(f"FLAGGED / DISQUALIFIED CANDIDATES ({len(all_rejected)} shown up to 15):")
        print("=" * 84)
        for cand in all_rejected[:15]:
            spec = cand[spec_key]
            ai_eval = cand.get("ai_eval") or {}
            curr = cand.get("currency") or (cand.get("summary") or {}).get("currency")
            p_str = format_price(cand["price"], curr)
            print(f"\n{spec['model']} ({p_str}) — {cand['summary']['url']}")
            for r in ai_eval.get("reasons") or []:
                print(f"   {r}")
