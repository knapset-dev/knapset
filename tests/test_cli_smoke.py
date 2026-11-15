"""
Smoke tests for CLI commands across core and installed domain extensions.
Verifies that <domain> search, <domain> modes, and section subcommands execute cleanly.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import main


class TestCliSmoke(unittest.TestCase):
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
