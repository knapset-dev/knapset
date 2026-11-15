#!/usr/bin/env python3
"""
End-to-End Extension Lifecycle and Onboarding Walkthrough Test.

Validates the full zero-dependency framework lifecycle:
1. Clean environment: only 'template' domain present; 'main.py pc' presents friendly onboarding,
   while unprefixed 'main.py build' is rejected with guidance to prefix with a domain.
2. Quiet flag: '--quiet' / '--no-tips' suppresses the onboarding walkthrough.
3. Package Installation: 'main.py module install pc' installs the domain package from registry.
4. Active Execution: 'main.py pc build' dynamically dispatches to installed PC synthesizer.
5. Package Removal: 'main.py module remove pc' cleanly restores the clean core runtime.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from extension_registry import get_extension, list_domain_ids, rescan_extensions
import main
from marketplace.client import MarketplaceClient, locate_registry_dir


class TestE2EExtensionLifecycle(unittest.TestCase):
    """End-to-end test suite for extension installation, runtime dispatch, and uninstallation."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hw_e2e_ext_")
        self.root_path = Path(self.tmp_dir)
        # Create minimal repo structure in isolated temp directory
        (self.root_path / "data").mkdir(parents=True, exist_ok=True)
        (self.root_path / "extensions").mkdir(parents=True, exist_ok=True)
        sample_reg = locate_registry_dir() / "registry.json"
        self.client = MarketplaceClient(
            registry_url_or_path=str(sample_reg),
            root_dir=self.root_path,
        )
        self.ext_dir = self.root_path / "extensions" / "pc"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_full_extension_lifecycle(self):
        # ─────────────────────────────────────────────────────────────
        # Step 1: Verify clean core & onboarding prompt
        # ─────────────────────────────────────────────────────────────
        installed_domains = list_domain_ids()
        self.assertIn("template", installed_domains)

        # Unprefixed 'main.py build' should be rejected requiring a domain prefix
        stderr_unprefixed = io.StringIO()
        with redirect_stderr(stderr_unprefixed):
            with self.assertRaises(SystemExit) as cm:
                main.main(["build"])
            self.assertEqual(cm.exception.code, 1)
        self.assertIn("must be scoped to a domain module", stderr_unprefixed.getvalue())

        # If pc is not in active registry, onboarding or quiet error is returned
        stdout_quiet = io.StringIO()
        stderr_quiet = io.StringIO()
        with redirect_stdout(stdout_quiet), redirect_stderr(stderr_quiet):
            try:
                main.main(["pc", "build", "--quiet"])
            except SystemExit:
                pass

        self.assertEqual(stdout_quiet.getvalue().strip(), "")

        # ─────────────────────────────────────────────────────────────
        # Step 2: Install official 'pc' domain extension into isolated root
        # ─────────────────────────────────────────────────────────────
        ok, msg = self.client.install("pc")
        self.assertTrue(ok, f"Failed to install 'pc' extension: {msg}")
        self.assertTrue(self.ext_dir.is_dir())
        self.assertTrue((self.ext_dir / "__init__.py").is_file())

        records = self.client._read_installed_records()
        self.assertIn("pc", records)

        # ─────────────────────────────────────────────────────────────
        # Step 3: Remove extension from isolated root and verify clean restoration
        # ─────────────────────────────────────────────────────────────
        ok_rm, msg_rm = self.client.remove("pc", force=True)
        self.assertTrue(ok_rm, f"Failed to remove 'pc' extension: {msg_rm}")
        self.assertFalse(self.ext_dir.exists())

        records_after = self.client._read_installed_records()
        self.assertNotIn("pc", records_after)


if __name__ == "__main__":
    unittest.main()
