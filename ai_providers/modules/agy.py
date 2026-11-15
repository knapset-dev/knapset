"""
Antigravity CLI (agy) Local Coding Agent AI Provider Module.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from ai_providers.base import BaseAIProvider


def _find_agy_binary() -> Optional[str]:
    w = shutil.which("agy")
    if w:
        return w
    candidates = [
        Path.home() / ".local" / "bin" / "agy",
        Path.home() / "bin" / "agy",
        Path("/usr/local/bin/agy"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


class AGYProvider(BaseAIProvider):
    name = "agy"
    title = "Antigravity CLI (agy)"
    provider_type = "cli"
    default_model = "gemini-3.8-flash-low"
    description = "Local Antigravity CLI coding agent executing verified model inference."

    def is_ready(self) -> Tuple[bool, str]:
        p = _find_agy_binary()
        if p:
            return True, f"Binary: {p}"
        return False, "`agy` binary not found in PATH"

    def complete(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        timeout: int = 120,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        from ai_providers.engine import extract_json_object

        bin_p = _find_agy_binary()
        if not bin_p:
            raise RuntimeError("`agy` binary not found in PATH")

        target_model = model or self.default_model
        full_prompt = f"{system_prompt}\n\n{prompt}".strip() if system_prompt else prompt

        cmd = [
            bin_p,
            "--model", target_model,
            "--effort", "low",
            "--disable-slash-commands",
            "-p", full_prompt,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="ignore").strip()[:200]
            raise RuntimeError(f"agy exited {proc.returncode}: {err}")

        raw_text = proc.stdout.decode("utf-8", errors="ignore").strip()
        return extract_json_object(raw_text)
