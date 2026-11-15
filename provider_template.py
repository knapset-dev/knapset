"""
Universal Provider Template / Contract.

Defines the standard interface that any marketplace data provider extension
(OLX, Telemart, eBay, Hotline, Allegro, etc.) implements, enabling domain search
instructions to interact with arbitrary data sources through a single template.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import re


@dataclass
class ProviderItem:
    """Standardized representation of a listing returned by any provider."""

    id: str
    source: str
    title: str
    price: float
    currency: str = "USDT"
    url: str = ""
    description: str = ""
    category_hint: str = ""
    has_buyer_protection: bool = False
    city: str = ""
    seller_name: str = ""
    seller_id: str = ""
    created_at: str = ""
    condition: str = "used"
    is_new: bool = False
    condition_label: str = "used"
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        res = dict(self.raw)
        res.update(
            {
                "id": self.id,
                "source": self.source,
                "title": self.title,
                "model": res.get("model") or self.title,
                "price": self.price,
                "currency": self.currency,
                "url": self.url,
                "description": self.description,
                "category_hint": self.category_hint,
                "is_secure_deal": self.has_buyer_protection,
                "has_buyer_protection": self.has_buyer_protection,
                "has_olx_delivery": self.has_buyer_protection,
                "condition": self.condition,
                "is_new": self.is_new,
                "condition_label": self.condition_label,
                "city": self.city,
                "seller_name": self.seller_name,
                "seller_id": self.seller_id,
                "created_at": self.created_at,
            }
        )
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderItem:
        title = str(data.get("title") or data.get("model") or "")
        is_n = bool(data.get("is_new", False))
        cond = str(data.get("condition", "new" if is_n else "used"))
        lbl = str(data.get("condition_label", "new" if is_n else "used"))
        return cls(
            id=str(data.get("id") or ""),
            source=str(data.get("source") or ""),
            title=title,
            price=float(data.get("price") or 0.0),
            currency=str(data.get("currency") or "USDT"),
            url=str(data.get("url") or ""),
            description=str(data.get("description") or ""),
            category_hint=str(data.get("category_hint") or data.get("category") or ""),
            has_buyer_protection=bool(
                data.get("is_secure_deal")
                or data.get("has_buyer_protection")
                or data.get("has_olx_delivery")
                or False
            ),
            city=str(data.get("city") or ""),
            seller_name=str(data.get("seller_name") or ""),
            seller_id=str(data.get("seller_id") or ""),
            created_at=str(data.get("created_at") or ""),
            condition=cond,
            is_new=is_n,
            condition_label=lbl,
            raw=dict(data),
        )


class ProviderFeatureAdapter:
    """
    Marketplace Feature Normalization Adapter (cross-site normalization wrapper).
    Translates site-specific features (OLX Delivery, Blocket Köpskydd, eBay Money Back Guarantee,
    Allegro Protect, official store warranties) into universal domain concepts:
      - 'is_secure_deal' (buyer protection boolean)
      - 'deal_type' (human-readable safety category)
      - 'seller_verified' (BankID, verified merchant, top-rated seller)
      - 'seller_score' (0-100 reputation score)
    This allows the Sorter extension to know ZERO about OLX, Blocket, eBay, or any specific website.
    """

    # Multilingual refusal patterns:
    # 1. Ukrainian & Russian (OLX)
    _UA_RU_REFUSAL_RE = re.compile(
        r"\b(?:без\s+(?:почт\w*|пошт\w*|відправок|отправок))\b|"
        r"\b(?:не\s+(?:отправляю|відправляю))\b|"
        r"\b(?:тільки\s+(?:самовивіз|з\s+рук|в\s+руки)|только\s+(?:самовывоз|из\s+рук|в\s+руки)|лише\s+самовивіз)\b|"
        r"\b(?:без\s+(?:олх|olx)\s*доставк\w*|без\s+(?:олх|olx))\b|"
        r"\b(?:не\s+відправля\w*\s+(?:олх|olx)|не\s+отправля\w*\s+(?:олх|olx))\b|"
        r"\b(?:(?:олх|olx)\s*доставк\w*\s+не\s+(?:відправля\w*|отправля\w*|працю\w*|работа\w*|користу\w*))\b|"
        r"\b(?:(?:олх|olx)\s+не\s+(?:пропонувати|предлагать))\b|"
        r"\b(?:тільки\s+повна\s+передплата|только\s+полная\s+предоплата)\b",
        re.IGNORECASE,
    )

    # 2. Swedish (Blocket)
    _SE_REFUSAL_RE = re.compile(
        r"\b(?:endast\s+swish\s+i\s+f[öo]rskott|f[öo]rskottsbetalning)\b|"
        r"\b(?:skickas\s+ej|skickar\s+inte|ingen\s+frakt)\b|"
        r"\b(?:endast\s+avh[äa]mtning|h[äa]mtas\s+p[åa]\s+plats)\b|"
        r"\b(?:inte\s+blocketpaketet|ej\s+blocketpaketet|utan\s+blocketpaketet)\b",
        re.IGNORECASE,
    )

    # 3. English (eBay / Global)
    _EN_REFUSAL_RE = re.compile(
        r"\b(?:no\s+returns|sold\s+as\s+is|as-is|for\s+parts\s+or\s+not\s+working)\b|"
        r"\b(?:wire\s+transfer\s+only|local\s+pickup\s+only|no\s+shipping)\b",
        re.IGNORECASE,
    )

    # 4. Polish (Allegro)
    _PL_REFUSAL_RE = re.compile(
        r"\b(?:odbi[óo]r\s+osobisty\s+tylko|nie\s+wysy[łl]am)\b|"
        r"\b(?:p[łl]atno[śs][ćc]\s+z\s+g[óo]ry|tylko\s+przedp[łl]ata)\b",
        re.IGNORECASE,
    )

    # Multilingual Condition Patterns (New vs Refurbished vs Used vs For Parts)
    _NEW_RE = re.compile(
        r"\b(?:новий|нова|нове|нові|запакован\w*|запечатан\w*|пломб\w*|не\s+розпаков\w*|не\s+відкривав\w*|нова\s+з\s+магазину|"
        r"новый|новое|новые|запечатанный|в\s+пленке|на\s+пломбах|не\s+вскрывался|"
        r"brand\s+new|factory\s+sealed|never\s+used|unopened|nib|new\s+in\s+box|"
        r"fabriksny|oöppnad|oöppnat|aldrig\s+använd|helt\s+ny|"
        r"nowy|nowe|nowa|fabrycznie\s+nowy|zafoliowany|nieużywany)\b",
        re.IGNORECASE,
    )

    _CLEAN_NEW_RE = re.compile(
        r"\b(?:нова\s+пошта|нової\s+пошти|новій\s+пошті|нову\s+пошту|новою\s+пошто[юе]|як\s+нов\w*|как\s+нов\w*)\b",
        re.IGNORECASE,
    )

    _REFURBISHED_RE = re.compile(
        r"\b(?:refurbished|remanufactured|outlet|open\s*box|b-stock|"
        r"відновлен\w*|уцін\w*|вітринн\w*|після\s+сервісу|"
        r"восстановлен\w*|уцен\w*|витринн\w*|renoverad)\b",
        re.IGNORECASE,
    )

    _EXCLUSIONS_PARTS_RE = re.compile(
        r"\b(?:без|жодних|ніяких|нет)\s+(?:[\w-]+\s+){0,2}(?:несправност\w*|дефект\w*|артефакт\w*)|"
        r"\b(?:артефакт\w*|дефект\w*|проблем\w*)[^.!?\n]{0,30}\b(?:нема\w*|ні|нет|відсутн\w*)\b|"
        r"\b(?:замінити|заміна)\s+несправн\w*|замена\s+неисправн\w*|"
        r"\b(?:не\s+на\s+запчастини|на\s+запчастини\s+не\s+розбира\w*|не\s+розбираю\s+на\s+запчастини)\b",
        re.IGNORECASE,
    )

    _FOR_PARTS_RE = re.compile(
        r"\b(?:на\s+(?:запчастин\w*|запчасти|детал\w*|розборк\w*|разборк\w*)|"
        r"під\s+(?:відновлення|ремонт)|под\s+(?:восстановление|ремонт)|"
        r"(?:під|под)\s+заміну\s+(?:дрмос\w*|drmos|чіп\w*|чип\w*|пам'ят\w*|памят\w*|сокет\w*|хаб\w*)|"
        r"донор|"
        r"не\s*робоч\w*|не\s*рабоч\w*|неробоч\w*|нерабоч\w*|"
        r"несправн\w*|неисправн\w*|"
        r"(?:не\s*ма\w*|нет)\s+(?:картинк\w*|з?о+бражен\w*|из?о+бражен\w*|сигнал\w*)|"
        r"(?:картинк\w*|з?о+бражен\w*|из?о+бражен\w*|сигнал\w*)\s+(?:не\s*ма\w*|нет)|"
        r"(?:відвал|отвал)\s+(?:чіп\w*|чип\w*|гпу|gpu|ядр\w*|пам'ят\w*|памят\w*)|"
        r"(?:по\s+причині|по\s+причине|після|после)\s+кз|пробит\w*\s+(?:дрмос\w*|drmos|мосфет\w*)|"
        r"(?:post|пост)\s+не\s+проход\w*|"
        r"не\s+(?:вмикається|ввімкнется|увімкнеться|включается|стартує|стартует|(?:вивод\w*|вида\w*|вывод\w*|выда\w*|дає|дает)\s+(?:картинк\w*|з?о+бражен\w*|из?о+бражен\w*|сигнал\w*))|"
        r"(?:показує|показывает|працює|работает)\s+(?:себе\s+)?не\s*стабільн\w*|не\s*стабільн\w*\s+(?:працює|работает|показує|показывает)|"
        r"(?:потрібно|нужно|надо)\s+(?:зафіксувати|зафиксировать|андервольт\w*|знизити\s+частот\w*|понизить\s+частот\w*)|"
        r"(?:видає|дает|полоси|полосы|артефач\w*)\s+артефакт\w*|артефачить|"
        r"for\s+parts|not\s+working|faulty|donor|as-is|defekt)\b",
        re.IGNORECASE,
    )

    _USED_RE = re.compile(
        r"\b(?:б/в|б\s*в|бу|вживан\w*|колишній\s+у\s+вжитку|після\s+майнінг\w*|стояла\s+в\s+(?:домашньому\s+)?пк|"
        r"бывший\s+в\s+употреблении|подержанный|с\s+пробегом|"
        r"used|pre-owned|second-hand|gently\s+used|"
        r"begagnad|använd|begagnat|używany|używane)\b",
        re.IGNORECASE,
    )

    @classmethod
    def check_delivery_refusal(cls, text: str, source: str = "") -> bool:
        """Scans description and title for marketplace-specific refusal of delivery/escrow."""
        if not text:
            return False
        src = (source or "").lower()
        if "blocket" in src or "se" in src:
            if cls._SE_REFUSAL_RE.search(text):
                return True
        elif "ebay" in src or "com" in src:
            if cls._EN_REFUSAL_RE.search(text):
                return True
        elif "allegro" in src or "pl" in src:
            if cls._PL_REFUSAL_RE.search(text):
                return True
        return bool(cls._UA_RU_REFUSAL_RE.search(text))

    @classmethod
    def detect_secure_deal(cls, data: Dict[str, Any], source: str = "") -> Tuple[bool, str]:
        """
        Translates provider-specific safety flags into the universal 'secure deal' (buyer protection) concept.
        Returns: (is_secure_deal, deal_type_label)
        """
        src = (str(data.get("source") or "") or str(source or "")).lower()
        title = str(data.get("title") or "")
        desc = str(data.get("description") or (data.get("raw") or {}).get("description", ""))
        text_haystack = f"{title} {desc}"
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}

        # 1. Retail Stores (Telemart, Hotline, etc.) -> official warranty is inherently a secure deal
        if "telemart" in src or "hotline" in src or data.get("warranty_months") or raw.get("warranty"):
            return True, "Secure Deal (Store Warranty)"

        # 2. Check site-specific refusal in text
        if cls.check_delivery_refusal(text_haystack, src):
            return False, "Escrow Refused by Seller"

        # 3. Blocket (Sweden: Köpskydd / Blocketpaketet / shipping_available)
        if "blocket" in src or data.get("kopsydd") or data.get("blocketpaketet"):
            has_kopsydd = bool(
                data.get("kopsydd")
                or data.get("blocketpaketet")
                or data.get("shipping_available")
                or raw.get("shipping_available")
                or raw.get("blocketpaketet")
                or (raw.get("flags") or {}).get("shipping")
                or data.get("has_buyer_protection")
            )
            if has_kopsydd:
                return True, "Secure Deal (Blocket Köpskydd)"
            return False, "No Buyer Protection (Blocket)"

        # 4. eBay (Global: eBay Money Back Guarantee / Buyer Protection / returnsAccepted)
        if "ebay" in src or data.get("buyerProtection") or data.get("returnsAccepted"):
            has_ebay = bool(
                data.get("buyerProtection")
                or data.get("returnsAccepted")
                or raw.get("returnsAccepted")
                or data.get("has_buyer_protection", True)
            )
            if has_ebay:
                return True, "Secure Deal (eBay Protection)"
            return False, "As-Is / No Buyer Protection"

        # 5. Allegro (Poland: Allegro Protect / SMART)
        if "allegro" in src or data.get("allegro_protect"):
            has_allegro = bool(
                data.get("allegro_protect")
                or data.get("smart")
                or raw.get("smart")
                or data.get("has_buyer_protection")
            )
            if has_allegro:
                return True, "Secure Deal (Allegro Protect)"
            return False, "No Buyer Protection (Allegro)"

        # 6. OLX (OLX Delivery / safedeal / rock)
        safedeal = raw.get("safedeal") or {}
        delivery = raw.get("delivery") or {}
        has_olx = (
            safedeal.get("status") == "active"
            or delivery.get("rock", {}).get("active", False)
            or bool(data.get("has_olx_delivery"))
            or bool(data.get("has_buyer_protection"))
        )
        if has_olx:
            return True, "Secure Deal (OLX Delivery)"
        if "olx" in src:
            return False, "No Buyer Protection (OLX)"

        # 7. Generic fallback
        is_safe = bool(
            data.get("is_secure_deal")
            or data.get("has_buyer_protection")
            or raw.get("has_buyer_protection")
        )
        return is_safe, "Secure Deal" if is_safe else "No Buyer Protection"

    @classmethod
    def detect_seller_trust(cls, data: Dict[str, Any], source: str = "") -> Tuple[bool, float]:
        """Normalizes seller verification status and trust rating (0-100)."""
        src = (str(data.get("source") or "") or str(source or "")).lower()
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}

        if "telemart" in src or "hotline" in src:
            return True, 100.0

        if "blocket" in src or data.get("is_bankid_verified") or raw.get("bankid"):
            is_bankid = bool(
                data.get("is_bankid_verified")
                or raw.get("bankid")
                or (raw.get("advertiser") or {}).get("bankid")
            )
            return is_bankid, 90.0 if is_bankid else 70.0

        if "ebay" in src or raw.get("seller", {}).get("feedbackPercentage"):
            fb = data.get("seller_feedback") or raw.get("seller", {}).get("feedbackPercentage")
            try:
                score = float(fb) if fb is not None else 85.0
            except (ValueError, TypeError):
                score = 85.0
            is_top = bool(raw.get("seller", {}).get("topRatedSeller") or score >= 98.0)
            return is_top, score

        # OLX
        rating = raw.get("user", {}).get("rating_status")
        is_verified = rating in ("excellent", "good")
        score = 90.0 if rating == "excellent" else (80.0 if rating == "good" else 75.0)
        return is_verified, score

    @classmethod
    def detect_item_condition(cls, data: Dict[str, Any], source: str = "") -> Tuple[bool, str, str]:
        """
        Extracts item condition (new vs used / refurbished / for_parts) from
        provider-specific flags, attributes, or multilingual listing text.
        Returns: (is_new: bool, condition: str, condition_label: str)
        """
        src = (str(data.get("source") or "") or str(source or "")).lower()
        title = str(data.get("title") or "")
        desc = str(data.get("description") or (data.get("raw") or {}).get("description", ""))
        text_full = f"{title} {desc}"
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}

        # 1. Text-based FOR_PARTS detection takes absolute priority
        # Sellers often mark broken or donor items as 'used' or mistakenly as 'new'.
        text_clean_parts = cls._EXCLUSIONS_PARTS_RE.sub(" ", text_full)
        if cls._FOR_PARTS_RE.search(text_clean_parts):
            return False, "for_parts", "for parts / defective"

        # 2. Direct explicit boolean or string flags
        if "is_new" in data and isinstance(data["is_new"], bool):
            is_n = data["is_new"]
            cond = str(data.get("condition") or ("new" if is_n else "used"))
            lbl = "new" if is_n else ("refurbished / outlet" if cond == "refurbished" else "used")
            return is_n, cond, lbl

        # 3. Check structured state in raw params (OLX)
        params = raw.get("params") or []
        for p in params:
            if isinstance(p, dict) and (p.get("key") == "state" or "стан" in str(p.get("name", "")).lower()):
                val = p.get("value") or {}
                val_k = str(val.get("key", "")).lower() if isinstance(val, dict) else str(val).lower()
                val_lbl = str(val.get("label", "")).lower() if isinstance(val, dict) else ""
                if "new" in val_k or "нов" in val_lbl:
                    return True, "new", "new"
                elif "used" in val_k or "б/в" in val_lbl or "вжив" in val_lbl:
                    return False, "used", "used"

        # 4. Check eBay conditionId / condition
        raw_cond = str(data.get("condition") or raw.get("condition") or raw.get("itemCondition") or raw.get("conditionId") or "").upper()
        if any(k in raw_cond for k in ("BRAND NEW", "NEW_OTHER", "1000", "1500", "SEALED", "NEW IN BOX")):
            return True, "new", "new"
        if any(k in raw_cond for k in ("REFURBISHED", "2000", "2500")):
            return False, "refurbished", "refurbished"
        if any(k in raw_cond for k in ("FOR PARTS", "7000", "NOT WORKING")):
            return False, "for_parts", "for parts / defective"

        # 5. Check Telemart Outlet / warranty
        if "telemart" in src:
            if (
                data.get("outlet_reason")
                or raw.get("outlet_reason")
                or data.get("outlet_price")
                or raw.get("outlet_price")
                or "outlet" in str(data.get("url", "")).lower()
                or "уцін" in title.lower()
            ):
                return False, "refurbished", "refurbished / outlet"
            return True, "new", "new"

        # 6. Check Hotline aggregate
        if "hotline" in src:
            if "б/в" in title.lower() or "б/у" in title.lower():
                return False, "used", "used"
            return True, "new", "new"

        # 7. Blocket attributes
        if "blocket" in src:
            blocket_cond = str(raw.get("condition") or raw.get("state") or (raw.get("attributes") or {}).get("condition") or "").lower()
            if any(k in blocket_cond for k in ("nytt", "nyskick", "fabriksny")):
                return True, "new", "new"
            if any(k in blocket_cond for k in ("begagnad", "använd")):
                return False, "used", "used"

        # 8. Text-based detection heuristics
        text_clean_new = cls._CLEAN_NEW_RE.sub(" ", text_full)
        if cls._REFURBISHED_RE.search(text_full):
            return False, "refurbished", "refurbished / outlet"
        if cls._NEW_RE.search(text_clean_new) and not cls._USED_RE.search(text_full):
            return True, "new", "new"
        if cls._USED_RE.search(text_full):
            return False, "used", "used"

        # 9. Marketplace-aware default: peer-to-peer markets default to used; retail aggregators to new
        if "hotline" in src:
            return True, "new", "new"
        return False, "used", "used"

    @classmethod
    def adapt_item(cls, item: Any, source_hint: str = "") -> Dict[str, Any]:
        """
        Takes any listing item or dictionary from any provider/site and wraps it
        into a standardized Candidate dictionary that the Sorter extension can consume
        without knowing the underlying marketplace site.
        """
        if hasattr(item, "to_dict"):
            d = item.to_dict()
        else:
            d = dict(item)

        src = str(d.get("source") or source_hint or "").lower()

        # 0. Check modular info_providers registry for custom third-party sources
        known_builtins = ("olx", "blocket", "ebay", "telemart", "hotline", "allegro")
        if not any(b in src for b in known_builtins):
            try:
                from info_providers import get_info_registry
                custom_prov = get_info_registry().get(src)
                if custom_prov:
                    orig_raw = d.get("raw")
                    norm = custom_prov.adapt_item(d)
                    if hasattr(norm, "to_dict"):
                        res_dict = norm.to_dict()
                        if orig_raw and isinstance(orig_raw, dict) and (not res_dict.get("raw") or res_dict.get("raw") == d):
                            res_dict["raw"] = orig_raw
                        d.update(res_dict)
                        return d
            except Exception:
                pass

        is_secure, deal_label = cls.detect_secure_deal(d, src)
        is_verified, seller_score = cls.detect_seller_trust(d, src)
        is_new, condition, cond_label = cls.detect_item_condition(d, src)

        # Standardized attributes for the Sorter
        d["is_secure_deal"] = is_secure
        d["has_buyer_protection"] = is_secure
        d["has_olx_delivery"] = is_secure  # Backward compatibility alias
        d["deal_type"] = deal_label
        d["seller_verified"] = is_verified
        d["seller_score"] = seller_score
        d["is_new"] = is_new
        d["condition"] = condition
        d["condition_label"] = cond_label

        return d


class BaseMarketProvider(ABC):
    """
    Universal Provider Template.
    Every marketplace data provider extension implements this single contract.
    """

    name: str
    source_name: str
    default_currency: str = "USDT"

    @abstractmethod
    def search_items(
        self,
        query: str = "",
        category_hint: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> List[ProviderItem]:
        """Queries the underlying marketplace and yields standardized ProviderItem objects."""
        pass

    def get_item_details(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Optional full listing metadata retrieval."""
        return None


class LocalCacheProvider(BaseMarketProvider):
    """
    Offline-first provider that reads from partitioned local JSON market caches.
    """

    def __init__(self, provider_id: str = "olx"):
        self.name = provider_id.lower().strip()
        self.source_name = f"cache:{self.name}"
        self.default_currency = "USDT"

    def search_items(
        self,
        query: str = "",
        category_hint: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> List[ProviderItem]:
        from market_client import load_market_cache

        cat = category_hint or "gpu"
        offers, _, _ = load_market_cache(cat, provider=self.name)

        q_norm = query.lower().strip() if query else ""
        filtered: List[ProviderItem] = []

        for o in offers:
            p = float(o.get("price") or 0.0)
            if min_price is not None and p < min_price:
                continue
            if max_price is not None and p > max_price:
                continue
            if q_norm:
                t = str(o.get("title") or "").lower()
                m = str(o.get("model") or "").lower()
                if q_norm not in t and q_norm not in m:
                    continue

            adapted = ProviderFeatureAdapter.adapt_item(o, self.source_name)
            filtered.append(ProviderItem.from_dict(adapted))

        if limit is not None:
            return filtered[offset : offset + limit]
        return filtered[offset:]


class MockMarketProvider(BaseMarketProvider):
    """
    Synthetic / in-memory provider used for tests and non-PC domains (e.g. Fishing).
    """

    def __init__(self, name: str = "mock", catalog: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self.name = name
        self.source_name = f"{name}.mock"
        self.default_currency = "USDT"
        self.catalog = catalog or {}

    def search_items(
        self,
        query: str = "",
        category_hint: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> List[ProviderItem]:
        cat = category_hint or ""
        pool = self.catalog.get(cat, [])
        if not pool and not cat:
            pool = [item for sub in self.catalog.values() for item in sub]

        q_norm = query.lower().strip() if query else ""
        results: List[ProviderItem] = []
        for o in pool:
            p = float(o.get("price") or 0.0)
            if min_price is not None and p < min_price:
                continue
            if max_price is not None and p > max_price:
                continue
            if q_norm:
                t = str(o.get("title") or o.get("model") or "").lower()
                if q_norm not in t:
                    continue
            adapted = ProviderFeatureAdapter.adapt_item(o, self.source_name)
            results.append(ProviderItem.from_dict(adapted))

        if limit is not None:
            return results[offset : offset + limit]
        return results[offset:]


def get_market_provider(provider_name: str = "olx", live: bool = False) -> BaseMarketProvider:
    """
    Factory resolving provider instances according to the BaseMarketProvider contract.
    Returns LocalCacheProvider (or MockMarketProvider for test/mock sources).
    """
    clean_p = (provider_name or "olx").lower().strip()
    if clean_p in ("mock", "sample", "test"):
        return MockMarketProvider(name=clean_p)
    return LocalCacheProvider(provider_id=clean_p)
