"""
Smoke tests for CLI commands across core and installed domain extensions.
Verifies that <domain> search, <domain> modes, and section subcommands execute cleanly.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main


class TestCliSmoke(unittest.TestCase):
    def test_dynamic_cli_prog_in_subparser_help(self):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["/usr/bin/ks", "module", "--help"]), redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main.main(["module", "--help"])
            self.assertEqual(cm.exception.code, 0)
        self.assertIn("usage: ks module", buf.getvalue())

    def test_module_install_bundle_and_uninstall_aliases(self):
        from marketplace.client import locate_registry_dir

        reg_path = str(locate_registry_dir() / "registry.json")
        with tempfile.TemporaryDirectory(prefix="ks_bundle_cli_") as tmp_dir:
            env = {"KNAPSET_HOME": tmp_dir, "KNAPSET_REGISTRY_URL": f"file://{reg_path}"}
            with patch.dict("os.environ", env):
                buf_inst = io.StringIO()
                with redirect_stdout(buf_inst):
                    main.main(["module", "install-bundle", "photo-kit"])
                self.assertIn("Installed bundle 'photo-kit'", buf_inst.getvalue())
                self.assertTrue((Path(tmp_dir) / "info_providers/modules/sample_provider.py").is_file())

                buf_rm = io.StringIO()
                with redirect_stdout(buf_rm):
                    main.main(["module", "uninstall", "sample_provider"])
                self.assertIn("successfully removed", buf_rm.getvalue())
                self.assertFalse((Path(tmp_dir) / "info_providers/modules/sample_provider.py").exists())
    def test_template_modes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.main(["template", "modes"])
        out = buf.getvalue().lower()
        self.assertIn("optimization modes for template domain", out)
        self.assertIn("balanced", out)

    def test_template_section_search(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.main(["template", "core", "--limit", "2"])
        out = buf.getvalue()
        self.assertIn("Search Results for [CORE]", out)
        self.assertIn("Core Unit", out)

    def test_template_search_command(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.main(["template", "search", "-s", "core", "-q", "alpha"])
        out = buf.getvalue()
        self.assertIn("Search Results for [CORE]", out)
        self.assertIn("Core Unit Alpha", out)

    def test_pc_search_command_if_installed(self):
        from extension_registry import list_domain_ids
        if "pc" not in list_domain_ids():
            self.skipTest("PC extension not installed in extensions/pc")

        buf = io.StringIO()
        with redirect_stdout(buf):
            main.main(["pc", "search", "-s", "gpu", "--limit", "2"])
        out = buf.getvalue()
        self.assertIn("GPU candidates", out)


if __name__ == "__main__":
    unittest.main()
