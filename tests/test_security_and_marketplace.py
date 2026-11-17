"""
Unit tests for v2 SecurityScanner AST checks and MarketplaceClient preset lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from marketplace.client import MarketplaceClient
from marketplace.security import SecurityScanner


class TestSecurityAndMarketplaceV2(unittest.TestCase):
    def test_security_scanner_blocks_dangerous_imports_and_calls(self) -> None:
        safe = SecurityScanner.scan_code(
            "class CleanPlugin:\n    def normalize(self, slot, rows):\n        return rows\n",
            filename="clean.py",
        )
        self.assertTrue(safe.is_safe)

        bad_sub = SecurityScanner.scan_code("import subprocess\nsubprocess.run(['id'])\n", filename="evil.py")
        self.assertFalse(bad_sub.is_safe)

        bad_eval = SecurityScanner.scan_code("def f(x):\n    return eval(x)\n", filename="eval_bad.py")
        self.assertFalse(bad_eval.is_safe)

    def test_marketplace_directory_preset_install_verify_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "home"
            reg_dir = root / "reg"
            pkg_dir = reg_dir / "modules" / "presets" / "demo_kit"
            pkg_dir.mkdir(parents=True)

            (pkg_dir / "items.csv").write_text("name,price,score\nA,10,90\nB,20,95\n", encoding="utf-8")
            (pkg_dir / "preset.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "name": "demo_kit",
                        "title": "Demo Kit",
                        "mode": "rank",
                        "sources": {"items": {"source": "items.csv"}},
                        "profiles": {"std": {"weights": {"score": 0.7, "price": -0.3}}},
                    }
                ),
                encoding="utf-8",
            )

            h = hashlib.sha256()
            for rel in ("items.csv", "preset.json"):
                h.update(rel.encode("utf-8"))
                h.update((pkg_dir / rel).read_bytes())

            (reg_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "modules": [
                            {
                                "name": "demo_kit",
                                "subsystem": "presets",
                                "domain": "general",
                                "version": "2.0.0",
                                "author": "test",
                                "description": "Directory preset test",
                                "source_type": "directory",
                                "download_url": "modules/presets/demo_kit",
                                "files": ["items.csv", "preset.json"],
                                "sha256": h.hexdigest(),
                            }
                        ],
                        "bundles": [],
                    }
                ),
                encoding="utf-8",
            )

            client = MarketplaceClient(registry_url_or_path=str(reg_dir), root_dir=home_dir)
            ok, msg = client.install("demo_kit")
            self.assertTrue(ok, msg)
            ver = client.verify("demo_kit")
            self.assertEqual(ver[0]["status"], "HEALTHY")
            self.assertTrue(ver[0]["checksum_match"])

            ok_rm, rm_msg = client.remove("demo_kit")
            self.assertTrue(ok_rm, rm_msg)


if __name__ == "__main__":
    unittest.main()
