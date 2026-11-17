"""Tests for `configs/theme.json`, `theme_config`, and `ks reg` theme registry support."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import List

import main as knapset_main
from marketplace.client import MarketplaceClient
from theme_config import (
    DEFAULT_THEME_NAME,
    ThemeConfig,
    discover_themes,
    get_theme_config_path,
    load_active_theme,
    reset_active_theme,
    resolve_color_value,
    set_active_theme,
    validate_theme_dict,
)
from tui import TuiSessionState, _build_workspace_items

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT.parent / "knapset-registry"


class TestThemeConfigAndRegistry(unittest.TestCase):
    def _run_cli(self, argv: List[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            knapset_main.main(argv)
        return buf.getvalue()

    def test_bundled_theme_config_and_color_resolution(self) -> None:
        cfg_file = REPO_ROOT / "configs" / "theme.json"
        self.assertTrue(cfg_file.is_file())
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        validate_theme_dict(raw)
        self.assertEqual(raw["active_theme"], DEFAULT_THEME_NAME)

        # Named colors, 256-color indices, and #RRGGBB hex quantization
        self.assertEqual(resolve_color_value("cyan", support_256=False), 6)
        self.assertEqual(resolve_color_value("default", support_256=True), -1)
        c256 = resolve_color_value("#88c0d0", support_256=True)
        self.assertTrue(16 <= c256 <= 255)
        c8 = resolve_color_value("#88c0d0", support_256=False)
        self.assertTrue(0 <= c8 <= 7)

        with self.assertRaises(ValueError):
            validate_theme_dict({"name": "bad theme", "colors": {"accent": 999}})
        with self.assertRaises(ValueError):
            validate_theme_dict({"name": "bad", "colors": {"unknown_role": "red"}})

    def test_ks_reg_install_activate_verify_and_remove_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_dir = root / "knapset_home"
            reg_dir = root / "mock_reg"
            (reg_dir / "modules" / "themes" / "files").mkdir(parents=True)

            nord_payload = {
                "schema_version": "2.0",
                "name": "nord_test",
                "title": "Nord Test",
                "author": "tester",
                "description": "Arctic theme test",
                "colors": {
                    "accent": "#88c0d0",
                    "positive": "#a3be8c",
                    "minimize": "#b48ead",
                    "warning": "#ebcb8b",
                    "muted": "#81a1c1",
                    "selection_fg": "#8fbcbb",
                    "selection_bg": "#3b4252",
                    "bar_fg": "#eceff4",
                    "bar_bg": "#2e3440",
                },
                "symbols": {"fill": "#", "half": "=", "empty": "-", "cursor": ">"},
            }
            payload_bytes = json.dumps(nord_payload).encode("utf-8")
            (reg_dir / "modules" / "themes" / "files" / "nord_test.json").write_bytes(payload_bytes)
            sha = hashlib.sha256(payload_bytes).hexdigest()

            manifest = {
                "name": "nord_test",
                "subsystem": "themes",
                "domain": "ui",
                "version": "2.0.0",
                "author": "tester",
                "description": "Arctic theme test",
                "source_type": "single_file",
                "download_url": "modules/themes/files/nord_test.json",
                "sha256": sha,
                "tags": ["theme", "nord"],
            }
            (reg_dir / "modules" / "themes" / "nord_test.json").write_text(json.dumps(manifest), encoding="utf-8")
            (reg_dir / "registry.json").write_text(
                json.dumps({"version": "2.0.0", "modules": [manifest], "bundles": []}),
                encoding="utf-8",
            )

            old_home = os.environ.get("KNAPSET_HOME")
            os.environ["KNAPSET_HOME"] = str(home_dir)
            try:
                # 1. Search via `ks reg search --type theme`
                search_out = json.loads(
                    self._run_cli(["reg", "search", "--type", "theme", "--registry", str(reg_dir), "--json"])
                )
                self.assertEqual(len(search_out["modules"]), 1)
                self.assertEqual(search_out["modules"][0]["name"], "nord_test")

                # 2. Install & activate via `ks reg install theme:nord_test --use`
                inst_out = self._run_cli(
                    ["reg", "install", "theme:nord_test", "--use", "--registry", str(reg_dir)]
                )
                self.assertIn("Activated theme 'nord_test'", inst_out)
                self.assertTrue((home_dir / "themes" / "nord_test.json").is_file())
                self.assertEqual(get_theme_config_path(), (home_dir / "configs" / "theme.json").resolve())

                # 3. Verify theme via `ks reg verify`
                ver_out = json.loads(self._run_cli(["reg", "verify", "nord_test", "--json"]))
                self.assertEqual(len(ver_out), 1)
                self.assertEqual(ver_out[0]["status"], "HEALTHY")
                self.assertTrue(ver_out[0]["security_safe"])

                # 4. Active theme reflected in `load_active_theme()` and TUI bar symbols
                active = load_active_theme()
                self.assertEqual(active.name, "nord_test")
                self.assertEqual(active.symbols["fill"], "#")

                st = TuiSessionState()
                st.weights = [("price", -0.5)]
                ws = _build_workspace_items(st, theme=active)
                weight_row = next(it for it in ws if it.kind == "weight")
                self.assertIn("#####-----", weight_row.label)

                # 5. List & show & remove resets active theme safely to default
                listed = json.loads(self._run_cli(["reg", "list", "--type", "theme", "--json"]))
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["name"], "nord_test")

                self._run_cli(["reg", "remove", "nord_test"])
                self.assertFalse((home_dir / "themes" / "nord_test.json").is_file())
                self.assertEqual(load_active_theme().name, DEFAULT_THEME_NAME)
                reset_active_theme()
            finally:
                if old_home is None:
                    os.environ.pop("KNAPSET_HOME", None)
                else:
                    os.environ["KNAPSET_HOME"] = old_home

    def test_knapset_registry_official_themes_install(self) -> None:
        themes_dir = REGISTRY_DIR / "modules" / "themes"
        if not themes_dir.is_dir():
            self.skipTest("knapset-registry themes not found on disk")

        with tempfile.TemporaryDirectory() as tmp:
            home_dir = Path(tmp) / "knapset_home"
            old_home = os.environ.get("KNAPSET_HOME")
            os.environ["KNAPSET_HOME"] = str(home_dir)
            try:
                client = MarketplaceClient(registry_url_or_path=str(REGISTRY_DIR), root_dir=home_dir)
                res = client.search(subsystem="themes")
                names = {m["name"] for m in res["modules"]}
                self.assertTrue({"nord", "catppuccin-mocha", "gruvbox-dark", "tokyo-night"}.issubset(names))

                ok, msg = client.install("theme:tokyo-night")
                self.assertTrue(ok, msg)
                theme, _ = set_active_theme("tokyo-night")
                self.assertEqual(theme.name, "tokyo-night")
                self.assertIn("tokyo-night", discover_themes())
            finally:
                if old_home is None:
                    os.environ.pop("KNAPSET_HOME", None)
                else:
                    os.environ["KNAPSET_HOME"] = old_home


if __name__ == "__main__":
    unittest.main()
