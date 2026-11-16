"""Tests runtime XDG and source-checkout path resolution in `app_paths`."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_paths


class TestAppPaths(unittest.TestCase):
    def test_checkout_mode_uses_repo_root(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KNAPSET_HOME", None)
            os.environ.pop("KNAPSET_DATA_DIR", None)
            os.environ.pop("KNAPSET_CONFIG_DIR", None)
            self.assertTrue(app_paths.is_checkout_mode())
            self.assertEqual(app_paths.get_data_dir(), app_paths.REPO_ROOT / "data")
            self.assertEqual(app_paths.get_config_dir(), app_paths.REPO_ROOT / "configs")
            self.assertEqual(app_paths.get_plugins_root(), app_paths.REPO_ROOT)

    def test_knapset_home_overrides_data_config_and_plugins(self):
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td).resolve()
            with mock.patch.dict(os.environ, {"KNAPSET_HOME": str(home_dir)}):
                self.assertFalse(app_paths.is_checkout_mode())
                self.assertEqual(app_paths.get_data_dir(), home_dir / "data")
                self.assertEqual(app_paths.get_config_dir(), home_dir / "configs")
                self.assertEqual(app_paths.get_plugins_root(), home_dir)

                # Shipped defaults remain reachable when user config dir has no override
                cfg_file = app_paths.get_config_file("currency.json")
                self.assertTrue(cfg_file.is_file())
                self.assertEqual(cfg_file, app_paths.REPO_ROOT / "configs" / "currency.json")

                # User config override takes precedence over shipped defaults
                user_cfg = home_dir / "configs" / "currency.json"
                user_cfg.parent.mkdir(parents=True, exist_ok=True)
                user_cfg.write_text('{"default_currency": "USD"}', encoding="utf-8")
                self.assertEqual(app_paths.get_config_file("currency.json"), user_cfg)

                ext_dirs = app_paths.get_subsystem_dirs("extensions")
                self.assertIn((app_paths.REPO_ROOT / "extensions").resolve(), ext_dirs)
                self.assertIn((home_dir / "extensions").resolve(), ext_dirs)

    def test_readonly_install_falls_back_to_xdg_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            xdg_data = base / "xdg_data"
            xdg_cfg = base / "xdg_cfg"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("KNAPSET_HOME", "KNAPSET_DATA_DIR", "KNAPSET_CONFIG_DIR")
            }
            clean_env["XDG_DATA_HOME"] = str(xdg_data)
            clean_env["XDG_CONFIG_HOME"] = str(xdg_cfg)
            with mock.patch.dict(os.environ, clean_env, clear=True):
                with mock.patch.object(app_paths, "_is_writable_dir", return_value=False):
                    self.assertFalse(app_paths.is_checkout_mode())
                    self.assertEqual(app_paths.get_data_dir(), xdg_data / "knapset")
                    self.assertEqual(app_paths.get_config_dir(), xdg_cfg / "knapset")
                    self.assertEqual(app_paths.get_plugins_root(), xdg_data / "knapset")


if __name__ == "__main__":
    unittest.main()
