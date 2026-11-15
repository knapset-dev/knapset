"""
High-speed compiled regex & metadata helpers shared across all 5 Hardware Scam Detectors.
Eliminates repeated regex compilation, HTML stripping, and ISO-timestamp parsing overhead.
"""

import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Dict, Any, List, Optional, Tuple

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Sellers who leave the OLX Delivery toggle ON for visibility, then refuse it in description or demand local pickup only
DELIVERY_REFUSAL_PATTERNS = [
    r"\b(?:без\s+почт\w*|без\s+пошт\w*)\b",
    r"\b(?:без\s+отправок|без\s+відправок)\b",
    r"\b(?:не\s+отправляю|не\s+відправляю)\b",
    r"\b(?:тільки\s+з\s+рук\s+в\s+руки|только\s+из\s+рук\s+в\s+руки)\b",
    r"\b(?:тільки\s+в\s+руки|только\s+в\s+руки)\b",
    r"\b(?:тільки\s+самовивіз|только\s+самовывоз|лише\s+самовивіз)\b",
    r"\b(?:без\s+(?:олх|olx)\s*доставк\w*|без\s+(?:олх|olx))\b",
    r"\b(?:не\s+відправляю\s+(?:олх|olx)|не\s+отправляю\s+(?:олх|olx))\b",
    r"\b(?:(?:олх|olx)\s*доставк\w*\s+не\s+(?:відправля\w*|отправля\w*|працю\w*|работа\w*|користу\w*))\b",
    r"\b(?:(?:олх|olx)\s+не\s+пропонувати|(?:олх|olx)\s+не\s+предлагать)\b",
]

DELIVERY_REFUSAL_RE = re.compile("|".join(DELIVERY_REFUSAL_PATTERNS), re.IGNORECASE)

CompiledTriggerGroup = Tuple[re.Pattern, Tuple[Tuple[re.Pattern, str], ...]]


def compile_trigger_group(triggers: List[Tuple[str, str]]) -> CompiledTriggerGroup:
    """
    Compiles a list of (pattern_str, explanation) into:
      1. A single union gate regex (fast early-out for the ~85% clean listings)
      2. Individual compiled regex tuples (evaluated only when the union gate hits)
    """
    union_re = re.compile("|".join(f"(?:{pat})" for pat, _ in triggers))
    individual = tuple((re.compile(pat), explanation) for pat, explanation in triggers)
    return union_re, individual


def match_triggers(
    group: CompiledTriggerGroup,
    text_lower: str,
    first_only: bool = False,
) -> List[str]:
    """Evaluates a compiled trigger group against pre-lowercased text."""
    union_re, individual = group
    if not union_re.search(text_lower):
        return []
    hits: List[str] = []
    for cre, explanation in individual:
        if cre.search(text_lower):
            hits.append(explanation)
            if first_only:
                break
    return hits


def strip_html_fast(raw: str) -> str:
    """Fast path avoids regex call when description contains no HTML tags."""
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        return _HTML_TAG_RE.sub(" ", raw).strip()
    return raw.strip()


@lru_cache(maxsize=4096)
def _seller_age_days_cached(date_prefix: str, today_iso: str) -> Optional[int]:
    try:
        c_dt = datetime.fromisoformat(date_prefix)
        t_dt = datetime.fromisoformat(today_iso)
        return (t_dt - c_dt).days
    except Exception:
        return None


def evaluate_seller_age(
    seller_created: Optional[str],
    red_flags: List[str],
    warnings: List[str],
    trust_signals: List[str],
) -> int:
    """Evaluates seller account age and appends appropriate signals; returns risk delta."""
    if not seller_created or len(seller_created) < 10:
        return 0
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    age_days = _seller_age_days_cached(seller_created[:10], today_iso)
    if age_days is None:
        return 0
    if age_days < 14:
        red_flags.append(f"Brand new seller account: registered only {age_days} days ago.")
        return 35
    if age_days < 60:
        warnings.append(f"Young seller account: registered {age_days} days ago.")
        return 15
    if age_days > 730:
        trust_signals.append(f"Established seller account ({age_days // 365}+ years old).")
    return 0


def resolve_photos_count(offer_summary: Dict[str, Any], offer_detail: Dict[str, Any]) -> int:
    """Reads photo count without requiring synthetic list allocations."""
    for source in (
        offer_detail,
        offer_summary,
        (offer_summary.get("raw") or {}) if isinstance(offer_summary, dict) and isinstance(offer_summary.get("raw"), dict) else {},
    ):
        if not isinstance(source, dict):
            continue
        photos = source.get("photos")
        if isinstance(photos, list):
            return len(photos)
        cnt = source.get("photos_count")
        if isinstance(cnt, int):
            return cnt
    return 0


def extract_snippet(description: str) -> str:
    """Extracts seller notes snippet (prioritizing bottom lines when e-katalog specs precede)."""
    if len(description) <= 400:
        return description[:350]
    lines = [ln.strip() for ln in description.split("\n") if ln.strip()]
    tail = " ".join(lines[-4:]) if len(lines) >= 4 else description[-400:]
    return tail[:350]


def classify_risk_tier(
    risk_score: int,
    suspicious_label: str = "PROBABLE ISSUE",
    scam_label: str = "LIKELY SCAM",
) -> Tuple[str, str]:
    if risk_score <= 20:
        return "SAFE", "SAFE / HIGH CONFIDENCE"
    if risk_score <= 45:
        return "CAUTION", "CAUTION / VERIFY DETAILS"
    if risk_score <= 70:
        return "SUSPICIOUS", f"SUSPICIOUS / {suspicious_label}"
    return "SCAM", f"HIGH RISK / {scam_label}"


# Universal triggers shared across all hardware components
UNIVERSAL_PREPAYMENT_GROUP: CompiledTriggerGroup = compile_trigger_group([
    (r"\b(?:передплат\w+|передоплат\w+|завдаток\w*|предоплат\w+)\b", "Demands partial or full prepayment"),
    (r"\b(?:на\s+картку|на\s+карту|скиньте\s+на|оплата\s+на\s+карту)\b", "Mentions direct card payment"),
    (r"\b(?:тільки\s+наложк\w+\s+з\s+передплат|лише\s+з\s+передплат)\b", "Requires prepayment before shipping"),
    (r"\b(?:(?:писат\w*|пишіть|пишите|зверта\w*|обраща\w*|зв'яз\w*|связ\w*|телеграм\w*|telegram|вайбер\w*|viber|контакт\w*|номер)\s+(?:\w+\s+){0,2}(?:на\s+)?[23]\s*[-мй]?\s*фото|(?:на\s+)?[23]\s*[-мй]?\s*фото\s+(?:\w+\s+){0,2}(?:телеграм\w*|telegram|вайбер\w*|viber|контакт\w*|для\s+зв'язку|для\s+связи))\b", "Off-platform contact redirect via photo"),
])

UNIVERSAL_CRITICAL_DEFECT_GROUP: CompiledTriggerGroup = compile_trigger_group([
    (r"\b(?:неробоч\w*|нерабоч\w*|напівробоч\w*|полурабоч\w*|не\s+робоч\w*|не\s+рабоч\w*|несправн\w*|неисправн\w*|неисправен\b|не\s+справн\w*|не\s+исправн\w*|не\s+исправен\b|непрацездатн\w*|не\s+працездатн\w*|непрацездатан\w*|не\s+працездатан\w*|не\s+функціону\w*|не\s+функциониру\w*|бракован\w*|бракований)\b", "Non-working / defective hardware disclosed"),
    (r"\b(?:під\s+відновлення|під\s+відновлен\w*|под\s+в[оа]с+т[ао]новлен\w*|на\s+ремонт|під\s+ремонт|под\s+ремонт|на\s+запчасти|на\s+запчастини|на\s+деталі|як\s+донор|донор|(?:під|под)\s+заміну\s+(?:дрмос\w*|drmos|чіп\w*|чип\w*|пам'ят\w*|памят\w*|вентилятор\w*|кулер\w*|мікросхем\w*|микросхем\w*|сокет\w*|хаб\w*|конденсатор\w*|мосфет\w*|транзистор\w*))\b", "Sold for parts / repair / component replacement"),
    (r"\b(?:перестал\w*\s+(?:робот|прац|включ|вмик)\w*|не\s+(?:вмика|ввімкн|увімкн|включа|старту|запуска)\w*|(?:post|пост)\s+не\s+проход\w*|не\s+проход\w*\s+(?:етап\s+|этап\s+)?(?:post|пост)|(?:спікер|спикер|post)\s+(?:мовчить|молчит))\b", "Stopped working / does not turn on or pass POST"),
    (r"\b(?:потребує\s+(?:діагностик\w*|ремонт\w*|замін\w*\s+(?:дрмос\w*|drmos|чіп\w*|чип\w*|мікросхем\w*|микросхем\w*|конденсатор\w*|вентилятор\w*|кулер\w*))|требует\s+(?:диагностик\w*|ремонт\w*|замен\w*\s+(?:дрмос\w*|drmos|чип\w*|микросхем\w*|конденсатор\w*|вентилятор\w*|кулер\w*))|на\s+діагностику|на\s+диагностику|(?:мікросхем\w*|микросхем\w*|дрмос\w*|drmos|чіп\w*|чип\w*|контролер\w*|контроллер\w*|сокет\w*)\s*,?\s*(?:яку|який|которую|который)\s+(?:потрібно|треба|нужно|надо)\s+замінити)\b", "Disclosed as needing diagnostics or IC/component replacement"),
    (r"\b(?:по\s+причині\s+кз|по\s+причине\s+кз|після\s+кз|после\s+кз|коротке\s+замикання|короткое\s+замыкание|пробит\w*\s+(?:дрмос\w*|drmos|мосфет\w*|фаз\w*|чіп\w*|чип\w*|хаб\w*)|прогар\w*)\b", "Short circuit (КЗ) or blown power stage / MOSFET disclosed"),
    (r"\b(?:(?:умовно|условно)\s+(?:робоч\w*|рабоч\w*|працю\w*|робот\w*|работа\w*)|(?:працює|работает|показує|показывает)\s+(?:себе\s+)?не\s*стабільн\w*|не\s*стабільн\w*\s+(?:працює|робот\w*|работа\w*|показує|показывает)|(?:працює|работает)\s+лише\s+на\s+стенді|(?:працює|работает)\s+только\s+на\s+стенде)\b", "Only conditionally working / unstable operation disclosed"),
    (r"\b(?:з\s+дефектом|с\s+дефектом|дефектн\w*|є\s+дефект|есть\s+дефект|нюанс\w*|з\s+нюансом|с\s+нюансом)\b", "Known defect or nuance disclosed"),
    (r"\b(?:без\s+претензій|без\s+повернень|без\s+возвратов|(?:купува\w*|покупа\w*)\s+(?:на\s+(?:олх|olx)\s+)?(?:під|под)\s+(?:виглядом|видом)\s+(?:робоч\w*|рабоч\w*))\b", "Strict 'as-is, no returns' or previously bought defective disclaimer"),
])


class BaseScamDetector:
    """
    Template Method base class for all 5 Hardware Scam Detectors.
    Unifies:
      - Clean description stripping & seller age parsing
      - Price outlier bounds checking against database benchmarks
      - OLX Delivery verification & evasion detection
      - Universal prepayment and critical defect triggers
      - Photo count resolution & bounded risk tier classification
    """
    category: str = "generic"
    suspicious_label: str = "PROBABLE ISSUE"
    scam_label: str = "LIKELY SCAM"
    min_market_crit_ratio: float = 0.45
    min_market_warn_ratio: float = 0.70
    max_market_overprice_ratio: float = 1.35

    def analyze_listing(
        self,
        offer_summary: Dict[str, Any],
        offer_detail: Dict[str, Any],
        spec_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        risk_score = 0
        red_flags: List[str] = []
        warnings: List[str] = []
        trust_signals: List[str] = []

        price = offer_summary.get("price", 0)
        title = offer_summary.get("title", "")
        description_raw = offer_detail.get("description") or offer_summary.get("description", "")
        description = strip_html_fast(description_raw)
        seller = offer_detail.get("user") or {}
        seller_name = seller.get("name") or offer_summary.get("seller_name", "Unknown")
        seller_created = seller.get("created") or offer_summary.get("seller_created")
        has_olx_delivery = offer_summary.get("has_olx_delivery", False)

        # 1. Price Deviance Analysis
        from currency import convert_currency, format_price, canonical_currency
        curr = offer_summary.get("currency") or "USDT"
        spec_curr = spec_info.get("currency") or "UAH"
        raw_min = spec_info.get("market_used_min", 0)
        raw_max = spec_info.get("market_used_max", 0)
        min_market = convert_currency(raw_min, spec_curr, curr) if (raw_min and curr != spec_curr) else raw_min
        max_market = convert_currency(raw_max, spec_curr, curr) if (raw_max and curr != spec_curr) else raw_max
        if min_market > 0:
            price_ratio = price / min_market
            p_str = format_price(price, curr)
            min_str = format_price(min_market, curr)
            max_str = format_price(max_market, curr)
            if price_ratio < self.min_market_crit_ratio:
                risk_score += 45
                red_flags.append(
                    f"Critically low price: {p_str} is {int((1 - price_ratio)*100)}% below minimum market value ({min_str})."
                )
            elif price_ratio < self.min_market_warn_ratio:
                risk_score += 20
                warnings.append(
                    f"Unusually low price: {p_str} is noticeably below typical used threshold ({min_str})."
                )
            elif max_market > 0 and price > max_market * self.max_market_overprice_ratio:
                warnings.append(f"Overpriced: {p_str} is well above standard market range ({min_str} - {max_str}).")
            else:
                trust_signals.append(f"Price ({p_str}) aligns with market range ({min_str} - {max_str}).")

        # 2. Seller Account Age
        risk_score += evaluate_seller_age(seller_created, red_flags, warnings, trust_signals)

        # 3. Delivery Refusal Evasion & Status
        desc_lower = f"{title} {description} {offer_summary.get('url', '')}".lower()
        if DELIVERY_REFUSAL_RE.search(desc_lower):
            has_olx_delivery = False
            risk_score += 25
            red_flags.append("Seller explicitly refuses OLX Delivery in listing text.")

        if has_olx_delivery:
            trust_signals.append("OLX Delivery is enabled (escrow & inspection at post office).")
        else:
            risk_score += 10
            warnings.append("OLX Delivery is NOT active on this listing.")

        # 4. Prepayment checks
        for exp in match_triggers(UNIVERSAL_PREPAYMENT_GROUP, desc_lower, first_only=True):
            risk_score += 30
            red_flags.append(f"Prepayment risk detected: {exp}")

        # 5. Universal critical defects
        for exp in match_triggers(UNIVERSAL_CRITICAL_DEFECT_GROUP, desc_lower):
            risk_score += 35
            red_flags.append(f"Hardware defect concern: {exp}")

        # 6. Component-specific hooks
        risk_score += self._evaluate_component_specific(
            desc_lower=desc_lower,
            title=title,
            description=description,
            price=price,
            min_market=min_market,
            max_market=max_market,
            spec_info=spec_info,
            red_flags=red_flags,
            warnings=warnings,
            trust_signals=trust_signals,
            offer_summary=offer_summary,
            offer_detail=offer_detail,
        )

        # 7. Photos resolution
        num_photos = resolve_photos_count(offer_summary, offer_detail)
        if num_photos == 0:
            risk_score += 25
            red_flags.append("No photos provided in the listing.")
        elif num_photos >= 3:
            trust_signals.append(f"Multiple photos ({num_photos} photos).")

        risk_score = max(0, min(100, risk_score))
        tier, tier_badge = classify_risk_tier(risk_score, self.suspicious_label, self.scam_label)

        return {
            "risk_score": risk_score,
            "tier": tier,
            "tier_badge": tier_badge,
            "red_flags": red_flags,
            "warnings": warnings,
            "trust_signals": trust_signals,
            "description_snippet": extract_snippet(description),
            "photos_count": num_photos,
            "seller_name": seller_name,
            "seller_created": seller_created,
        }

    def _evaluate_component_specific(
        self,
        desc_lower: str,
        title: str,
        description: str,
        price: float,
        min_market: float,
        max_market: float,
        spec_info: Dict[str, Any],
        red_flags: List[str],
        warnings: List[str],
        trust_signals: List[str],
        offer_summary: Dict[str, Any],
        offer_detail: Dict[str, Any],
    ) -> int:
        return 0
