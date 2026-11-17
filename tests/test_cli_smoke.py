"""Smoke tests for Knapset v2 CLI commands (`ks --help`, `--version`, `tui --help`, `serve --help`, `mcp --list-tools`, `preset list`)."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class TestCliSmokeV2(unittest.TestCase):
    def test_version_and_help(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.main(["--version"])
        self.assertIn("2.0.0", buf.getvalue())

        help_buf = io.StringIO()
        with redirect_stdout(help_buf):
            main.main(["--help"])
        out = help_buf.getvalue()
        for cmd in ("inspect", "init", "compare", "rank", "basket", "solve", "run", "preset", "reg", "tui", "serve", "mcp"):
            self.assertIn(cmd, out)

    def test_dynamic_cli_prog_in_subparser_help(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "argv", ["/usr/bin/ks", "rank", "--help"]), redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main.main(["rank", "--help"])
            self.assertEqual(cm.exception.code, 0)
        self.assertIn("usage: ks rank", buf.getvalue())

    def test_tui_serve_mcp_and_preset_smoke(self) -> None:
        for cmd in (["tui", "--help"], ["serve", "--help"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    main.main(cmd)
                self.assertEqual(cm.exception.code, 0)

        mcp_buf = io.StringIO()
        with redirect_stdout(mcp_buf):
            main.main(["mcp", "--list-tools"])
        payload = json.loads(mcp_buf.getvalue())
        self.assertEqual(payload["server"]["version"], "2.0.0")
        names = [t["name"] for t in payload["tools"]]
        self.assertEqual(
            names,
            [
                "knapset_inspect_source",
                "knapset_rank_items",
                "knapset_solve_basket",
                "knapset_solve_sets",
                "knapset_list_presets",
            ],
        )

        preset_buf = io.StringIO()
        with redirect_stdout(preset_buf):
            main.main(["preset", "list", "--json"])
        self.assertIsInstance(json.loads(preset_buf.getvalue()), list)

        reg_buf = io.StringIO()
        with redirect_stdout(reg_buf):
            main.main(["reg", "theme", "list", "--json"])
        themes = json.loads(reg_buf.getvalue())
        self.assertIsInstance(themes, list)
        self.assertTrue(any(t["name"] == "agy-dark" for t in themes))


if __name__ == "__main__":
    unittest.main()
