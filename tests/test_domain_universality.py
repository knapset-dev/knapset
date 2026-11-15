"""
End-to-End Cross-Domain Universality Tests.

Proves that OmniForge is completely domain-agnostic by implementing
a full non-PC photography equipment marketplace intelligence pipeline:
- Info Provider: B&H/Adorama camera listing adapter
- Scam Analyser: Camera shutter count & lens fungus detector
- Component Analyser: Lens spec extraction & mount identification
- Domain Layout & Compatibility: Camera Body + Lens mount validation
- Build Composer: Photography Kit Synthesizer (Body + Lens + Tripod + Card)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import unittest

from build_composer.base import BaseBuildComposer, ComposerSetResult
from component_analyser.base import BaseComponentAnalyser
from domain_layout import CompatibilityRule, DomainLayout, SectionDefinition
from info_providers.base import BaseInfoProvider, NormalizedListing
from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult


class CameraListingProvider(BaseInfoProvider):
    name = "bnh_photo"
    title = "B&H Photo Video"
    domain = "photo_video"
    source_domain = "bhphotovideo.com"
    default_currency = "USD"
    aliases = ["bnh", "adorama"]

    def adapt_item(self, raw_data: Dict[str, Any]) -> NormalizedListing:
        return NormalizedListing(
            id=str(raw_data.get("sku", "")),
            title=str(raw_data.get("name", "")),
            price=float(raw_data.get("price_usd", 0.0)),
            currency="USD",
            source=self.name,
            url=str(raw_data.get("link", "")),
            description=str(raw_data.get("desc", "")),
            condition=str(raw_data.get("grade", "used")),
            is_secure_deal=bool(raw_data.get("verified_dealer", False)),
            raw=raw_data,
        )


class CameraDefectAnalyser(BaseScamAnalyser):
    name = "camera_body"
    domain = "photo_video"
    aliases = ["dslr", "mirrorless", "camera"]

    def analyze(
        self,
        item: Dict[str, Any],
        detail: Optional[Dict[str, Any]] = None,
        spec: Optional[Dict[str, Any]] = None,
    ) -> ScamAnalysisResult:
        desc = (item.get("description", "") + " " + item.get("title", "")).lower()
        red_flags = []
        warnings = []
        trust_signals = []
        risk_score = 0

        if "fungus" in desc or "mold" in desc:
            red_flags.append("Optics/Sensor contamination: fungus or mold mentioned")
            risk_score += 60

        if "sensor scratch" in desc or "scratched sensor" in desc:
            red_flags.append("Physical defect: damaged sensor surface")
            risk_score += 80

        if "shutter count: 250" in desc or "shutter count > 200k" in desc:
            warnings.append("High actuation count: shutter near end of mechanical lifespan")
            risk_score += 30

        if "original box" in desc and "clean sensor" in desc:
            trust_signals.append("Complete kit with verified clean optics")

        return ScamAnalysisResult(
            category="camera_body",
            risk_score=min(risk_score, 100),
            red_flags=red_flags,
            warnings=warnings,
            trust_signals=trust_signals,
            is_defective=len(red_flags) > 0,
        )


class LensSpecAnalyser(BaseComponentAnalyser):
    name = "lens"
    domain = "photo_video"
    aliases = ["lenses", "optics"]

    def identify(self, title: str, description: str = "") -> Optional[Dict[str, Any]]:
        t = (title + " " + description).lower()
        mount = "Unknown"
        if "sony e" in t or "fe mount" in t:
            mount = "Sony E"
        elif "canon rf" in t:
            mount = "Canon RF"
        elif "nikon z" in t:
            mount = "Nikon Z"

        focal_length = "50mm" if "50mm" in t else ("24-70mm" if "24-70mm" in t else "Unknown")
        aperture = "f/1.8" if "f/1.8" in t or "1.8" in t else "f/2.8"

        return {
            "category": "lens",
            "mount": mount,
            "focal_length": focal_length,
            "max_aperture": aperture,
            "is_zoom": "24-70" in focal_length,
        }


class PhotoKitComposer(BaseBuildComposer):
    name = "photo_kit"
    title = "Photography Gear Synthesizer"
    domain = "photo_video"
    aliases = ["camera_rig", "studio_kit"]

    def compose(
        self,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        mode: str = "best",
        weights: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[ComposerSetResult]:
        bodies = candidate_pool.get("body", [])
        lenses = candidate_pool.get("lens", [])
        tripods = candidate_pool.get("tripod", [])
        cards = candidate_pool.get("card", [])

        results: List[ComposerSetResult] = []

        for b in bodies:
            for l in lenses:
                # Engineering constraint: Lens mount must match Body mount!
                b_mount = (b.get("spec") or {}).get("mount")
                l_mount = (l.get("spec") or {}).get("mount")
                if b_mount and l_mount and b_mount != l_mount:
                    continue  # Incompatible physical mount!

                for t in tripods or [{}]:
                    for c in cards or [{}]:
                        total_cost = (
                            float(b.get("price", 0))
                            + float(l.get("price", 0))
                            + float(t.get("price", 0))
                            + float(c.get("price", 0))
                        )
                        if total_cost > budget:
                            continue

                        surplus = budget - total_cost
                        score = 100.0 - (total_cost / budget * 20.0)

                        components = {
                            "body": b,
                            "lens": l,
                        }
                        if t:
                            components["tripod"] = t
                        if c:
                            components["card"] = c

                        results.append(
                            ComposerSetResult(
                                domain=self.domain,
                                mode=mode,
                                total_cost=total_cost,
                                budget=budget,
                                surplus=surplus,
                                score=round(score, 1),
                                components=components,
                                metrics={"mount": b_mount},
                            )
                        )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]


class TestDomainUniversality(unittest.TestCase):
    def setUp(self):
        self.provider = CameraListingProvider()
        self.defect_analyser = CameraDefectAnalyser()
        self.lens_analyser = LensSpecAnalyser()
        self.composer = PhotoKitComposer()

    def test_provider_listing_normalization(self):
        raw = {
            "sku": "CAM-9912",
            "name": "Sony Alpha A7 IV Mirrorless Body",
            "price_usd": 1899.99,
            "link": "https://bhphotovideo.com/c/product/123",
            "desc": "Shutter count: 1200, clean sensor, original box included.",
            "grade": "like_new",
            "verified_dealer": True,
        }
        listing = self.provider.adapt_item(raw)
        self.assertEqual(listing.id, "CAM-9912")
        self.assertEqual(listing.title, "Sony Alpha A7 IV Mirrorless Body")
        self.assertEqual(listing.price, 1899.99)
        self.assertEqual(listing.currency, "USD")
        self.assertTrue(listing.is_secure_deal)

    def test_defect_analyser_flags_sensor_damage_and_fungus(self):
        clean_item = {
            "title": "Sony A7 III",
            "description": "Mint condition, original box, clean sensor.",
        }
        clean_res = self.defect_analyser.analyze(clean_item)
        self.assertFalse(clean_res.is_defective)
        self.assertEqual(clean_res.risk_score, 0)
        self.assertIn("Complete kit with verified clean optics", clean_res.trust_signals)

        fungus_item = {
            "title": "Canon R6 with fungus in optics",
            "description": "Scratched sensor glass, mold visible on mount.",
        }
        bad_res = self.defect_analyser.analyze(fungus_item)
        self.assertTrue(bad_res.is_defective)
        self.assertGreaterEqual(bad_res.risk_score, 80)
        self.assertTrue(any("fungus" in f.lower() for f in bad_res.red_flags))
        self.assertTrue(any("sensor" in f.lower() for f in bad_res.red_flags))

    def test_component_spec_analyser_identifies_lens_mount(self):
        spec = self.lens_analyser.identify(
            title="Sigma 24-70mm f/2.8 DG DN Art",
            description="Premium standard zoom for Sony E mount cameras.",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["mount"], "Sony E")
        self.assertEqual(spec["focal_length"], "24-70mm")
        self.assertEqual(spec["max_aperture"], "f/2.8")
        self.assertTrue(spec["is_zoom"])

    def test_domain_layout_and_mount_compatibility(self):
        layout = DomainLayout(
            domain_id="photo_video",
            title="Photography Gear Setup",
            default_budget=2500.0,
            sections=[
                SectionDefinition(id="body", name="Camera Body", label="Body"),
                SectionDefinition(id="lens", name="Primary Lens", label="Lens"),
            ],
            compatibility_rules=[
                CompatibilityRule(
                    name="mount_matching",
                    description="Lens physical mount must match Camera Body mount",
                    validator=lambda s: (
                        (s["body"]["spec"]["mount"] == s["lens"]["spec"]["mount"], None)
                        if s["body"]["spec"]["mount"] == s["lens"]["spec"]["mount"]
                        else (False, "Mount mismatch: Body and Lens mounts incompatible")
                    ),
                    sections_involved=("body", "lens"),
                )
            ],
        )
        self.assertEqual(len(layout.sections), 2)
        rule = layout.compatibility_rules[0]

        compatible_set = {
            "body": {"spec": {"mount": "Sony E"}},
            "lens": {"spec": {"mount": "Sony E"}},
        }
        ok, reason = rule.evaluate(compatible_set)
        self.assertTrue(ok)
        self.assertIsNone(reason)

        incompatible_set = {
            "body": {"spec": {"mount": "Sony E"}},
            "lens": {"spec": {"mount": "Canon RF"}},
        }
        ok2, reason2 = rule.evaluate(incompatible_set)
        self.assertFalse(ok2)
        self.assertIn("Mount mismatch", reason2)

    def test_composer_synthesizes_compatible_photo_kit(self):
        candidate_pool = {
            "body": [
                {"name": "Sony A7 IV", "price": 1400.0, "spec": {"mount": "Sony E"}},
                {"name": "Canon R6", "price": 1300.0, "spec": {"mount": "Canon RF"}},
            ],
            "lens": [
                # Only Sony E lens available in pool
                {"name": "Sony 50mm f/1.8", "price": 250.0, "spec": {"mount": "Sony E"}},
            ],
            "tripod": [
                {"name": "Manfrotto BeFree", "price": 150.0},
            ],
            "card": [
                {"name": "SanDisk 128GB V90", "price": 80.0},
            ],
        }

        # Budget: 2000 USD
        kits = self.composer.compose(candidate_pool, budget=2000.0)
        self.assertGreaterEqual(len(kits), 1)

        best_kit = kits[0]
        # Must pair Sony A7 IV with Sony 50mm (Canon R6 rejected due to mount mismatch!)
        self.assertEqual(best_kit.components["body"]["name"], "Sony A7 IV")
        self.assertEqual(best_kit.components["lens"]["name"], "Sony 50mm f/1.8")
        self.assertEqual(best_kit.total_cost, 1880.0)
        self.assertLessEqual(best_kit.total_cost, 2000.0)


# ==============================================================================
# Domain 2: Smart Home & IoT Automation Presets (Zigbee/Matter/Thread)
# ==============================================================================

class SmartHomeListingProvider(BaseInfoProvider):
    name = "smarthome_depot"
    title = "Smart Home Depot"
    domain = "smart_home_iot"
    source_domain = "smarthomedepot.com"
    default_currency = "EUR"
    aliases = ["iot_shop", "zigbee_market"]

    def adapt_item(self, raw_data: Dict[str, Any]) -> NormalizedListing:
        return NormalizedListing(
            id=str(raw_data.get("device_id", "iot-0")),
            title=str(raw_data.get("title", "")),
            price=float(raw_data.get("price_eur", 0.0)),
            currency="EUR",
            source=self.name,
            description=str(raw_data.get("details", "")),
            condition=str(raw_data.get("state", "used")),
            is_secure_deal=True,
            raw=raw_data,
        )


class SmartDeviceDefectAnalyser(BaseScamAnalyser):
    name = "iot_device"
    domain = "smart_home_iot"
    aliases = ["sensor", "smart_plug", "coordinator"]

    def analyze(
        self,
        item: Dict[str, Any],
        detail: Optional[Dict[str, Any]] = None,
        spec: Optional[Dict[str, Any]] = None,
    ) -> ScamAnalysisResult:
        text = (item.get("description", "") + " " + item.get("title", "")).lower()
        red_flags = []
        warnings = []
        risk_score = 0

        if "cloud locked" in text or "bound to account" in text or "vendor locked" in text:
            red_flags.append("Cloud Lock: Device bound to previous owner account, bricked for adoption")
            risk_score += 90

        if "battery corrosion" in text or "acid leak" in text:
            red_flags.append("Hardware Damage: Battery terminal corrosion detected")
            risk_score += 70

        if "untested" in text or "no hub to test" in text:
            warnings.append("Functional Uncertainty: Seller cannot verify RF pairing")
            risk_score += 35

        return ScamAnalysisResult(
            category="iot_device",
            risk_score=min(risk_score, 100),
            red_flags=red_flags,
            warnings=warnings,
            is_defective=len(red_flags) > 0,
        )


class ProtocolSpecAnalyser(BaseComponentAnalyser):
    name = "iot_spec"
    domain = "smart_home_iot"
    aliases = ["rf_protocol", "iot_tech"]

    def identify(self, title: str, description: str = "") -> Optional[Dict[str, Any]]:
        combined = (title + " " + description).lower()
        protocol = "Unknown"
        if "zigbee" in combined or "zha" in combined or "z2m" in combined:
            protocol = "Zigbee"
        elif "matter" in combined or "thread" in combined:
            protocol = "Matter/Thread"
        elif "z-wave" in combined or "zwave" in combined:
            protocol = "Z-Wave"
        elif "wi-fi" in combined or "wifi" in combined:
            protocol = "Wi-Fi"

        return {
            "protocol": protocol,
            "ota_upgradeable": "ota" in combined or "upgradeable" in combined,
        }


class SmartHomeKitComposer(BaseBuildComposer):
    name = "smart_home_kit"
    title = "Smart Home Starter Kit Synthesizer"
    domain = "smart_home_iot"
    aliases = ["iot_composer", "automation_pack"]

    def __init__(self) -> None:
        self.protocol_rule = CompatibilityRule(
            name="Hub-Sensor Protocol Matching",
            description="Coordinator hub RF protocol must match all sensors in kit",
            validator=self._validate_rf_protocol,
            sections_involved=("hub", "sensor"),
        )
        self.layout = DomainLayout(
            domain_id="smart_home_iot",
            title="Smart Home Setup",
            sections=[
                SectionDefinition(id="hub", name="Coordinator", label="HUB", spec_fields=("protocol",)),
                SectionDefinition(id="sensor", name="Sensor", label="SENSOR", spec_fields=("protocol",)),
                SectionDefinition(id="plug", name="Smart Plug", label="PLUG", required=False),
            ],
            compatibility_rules=[self.protocol_rule],
            default_budget=150.0,
            currency="€",
        )

    def _validate_rf_protocol(self, set_dict: Dict[str, Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        hub_proto = set_dict.get("hub", {}).get("spec", {}).get("protocol")
        sensor_proto = set_dict.get("sensor", {}).get("spec", {}).get("protocol")
        if hub_proto and sensor_proto and hub_proto != sensor_proto:
            return False, f"RF Protocol Mismatch: Hub uses {hub_proto} but Sensor requires {sensor_proto}"
        return True, None

    def compose(
        self,
        candidate_pool: Dict[str, List[Dict[str, Any]]],
        budget: float,
        mode: str = "best",
        weights: Optional[str] = None,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[ComposerSetResult]:
        hubs = candidate_pool.get("hub", [])
        sensors = candidate_pool.get("sensor", [])
        plugs = candidate_pool.get("plug", [])

        results = []
        for h in hubs:
            for s in sensors:
                combo = {"hub": h, "sensor": s}
                valid, _ = self.layout.validate_combination(combo, strict=False)
                if not valid:
                    continue

                cost = h["price"] + s["price"]
                plug_chosen = None
                if plugs and (cost + plugs[0]["price"]) <= budget:
                    cost += plugs[0]["price"]
                    plug_chosen = plugs[0]
                    combo["plug"] = plug_chosen

                if cost <= budget:
                    results.append(
                        ComposerSetResult(
                            domain="smart_home_iot",
                            mode=mode,
                            total_cost=cost,
                            budget=budget,
                            surplus=budget - cost,
                            score=100.0 - (cost / budget * 20.0),
                            components=combo,
                        )
                    )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]


class TestSmartHomeDomainUniversality(unittest.TestCase):
    def setUp(self):
        self.provider = SmartHomeListingProvider()
        self.defect_analyser = SmartDeviceDefectAnalyser()
        self.spec_analyser = ProtocolSpecAnalyser()
        self.composer = SmartHomeKitComposer()

    def test_provider_normalizes_iot_listing(self):
        raw = {
            "device_id": "sonoff-snzb-02",
            "title": "Sonoff Temperature & Humidity Sensor",
            "price_eur": 12.50,
            "details": "Zigbee 3.0 sensor, works with Home Assistant and Zigbee2MQTT",
            "state": "like new",
        }
        item = self.provider.adapt_item(raw)
        self.assertEqual(item.id, "sonoff-snzb-02")
        self.assertEqual(item.currency, "EUR")
        self.assertEqual(item.price, 12.50)

    def test_defect_analyser_catches_cloud_locked_device(self):
        item = {
            "title": "Aqara Hub M2",
            "description": "Works fine but cloud locked to previous Apple HomeKit account. As-is.",
        }
        res = self.defect_analyser.analyze(item)
        self.assertTrue(res.is_defective)
        self.assertGreaterEqual(res.risk_score, 80)
        self.assertTrue(any("Cloud Lock" in f for f in res.red_flags))

    def test_protocol_spec_extraction(self):
        spec = self.spec_analyser.identify("Philips Hue Motion Sensor Zigbee 3.0 OTA")
        self.assertEqual(spec["protocol"], "Zigbee")
        self.assertTrue(spec["ota_upgradeable"])

    def test_composer_matches_rf_protocols(self):
        pool = {
            "hub": [
                {"name": "Sonoff Zigbee Dongle Plus", "price": 25.0, "spec": {"protocol": "Zigbee"}},
                {"name": "Aeotec Z-Wave Gen5+", "price": 45.0, "spec": {"protocol": "Z-Wave"}},
            ],
            "sensor": [
                {"name": "Aqara Door Contact", "price": 15.0, "spec": {"protocol": "Zigbee"}},
            ],
            "plug": [
                {"name": "IKEA Tradfri Smart Outlet", "price": 10.0},
            ],
        }

        kits = self.composer.compose(pool, budget=60.0)
        self.assertGreaterEqual(len(kits), 1)
        best = kits[0]
        # Zigbee sensor must match with Sonoff Zigbee Dongle, rejecting Aeotec Z-Wave
        self.assertEqual(best.components["hub"]["name"], "Sonoff Zigbee Dongle Plus")
        self.assertEqual(best.components["sensor"]["name"], "Aqara Door Contact")
        self.assertEqual(best.total_cost, 50.0)


if __name__ == "__main__":
    unittest.main()
