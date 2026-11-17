"""Tests for `ks prefs` and `--prefs` on rank/basket/solve."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch

import main as knapset_main


def _run(argv: List[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        knapset_main.main(argv)
    return buf.getvalue()


class TestPrefsCli(unittest.TestCase):
    def test_save_list_rank_export_import_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "d.csv"
            data.write_text("name,price,score\nA,10,50\nB,20,90\nC,15,70\n", encoding="utf-8")
            with patch.dict(os.environ, {"KNAPSET_HOME": str(root / "home")}):
                _run(["prefs", "save", "quality", "-p", "score:+0.8,price:-0.2", "-w", "price > 12"])
                listed = json.loads(_run(["prefs", "list", "--json"]))
                self.assertEqual(listed["quality"]["weights"], {"score": 0.8, "price": -0.2})

                out = json.loads(_run(["rank", str(data), "--prefs", "quality", "--json"]))
                names = [r["row"]["name"] for r in out["results"]]
                self.assertEqual(names, ["B", "C"])  # filter `price > 12` came from the profile

                # Explicit CLI flags win over the profile for the same group
                out2 = json.loads(_run(["rank", str(data), "--prefs", "quality", "-p", "price:-1", "--json"]))
                self.assertEqual(out2["results"][0]["row"]["name"], "C")

                exported = root / "q.json"
                _run(["prefs", "export", "quality", str(exported)])
                self.assertTrue(exported.is_file())
                _run(["prefs", "import", str(exported), "--name", "copy"])
                self.assertIn("copy", json.loads(_run(["prefs", "list", "--json"])))
                _run(["prefs", "remove", "copy"])
                self.assertNotIn("copy", json.loads(_run(["prefs", "list", "--json"])))

                with self.assertRaises(SystemExit):
                    _run(["rank", str(data), "--prefs", "missing"])


if __name__ == "__main__":
    unittest.main()
