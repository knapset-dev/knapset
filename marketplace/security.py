"""
AST Security Scanner for Marketplace Modules.

Performs static analysis of Python source code using the standard library `ast`
module without executing code. Rejects modules containing banned imports,
dangerous system calls, process spawning, destructive filesystem calls,
reflection escapes, external third-party pip dependencies, or arbitrary dynamic execution.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import List, Optional, Set, Union


BANNED_MODULES: Set[str] = {
    # System process execution & sandboxing escapes
    "subprocess",
    "pty",
    "posix",
    "ctypes",
    "winreg",
    "msvcrt",
    "multiprocessing",
    "importlib",
    "runpy",
    "code",
    "codeop",
    "webbrowser",
    "builtins",
    "sysconfig",
    "compileall",
    "pkgutil",
    # Unsafe serialization & bytecode manipulation
    "pickle",
    "shelve",
    "marshal",
    # Heap, stack frame & debugger escapes
    "inspect",
    "gc",
    "dis",
    "pdb",
    # Host filesystem mutation & manipulation
    "shutil",
    "tempfile",
    # Raw networking & listener backdoors (only urllib/http.client allowed in online providers)
    "socket",
    "socketserver",
    "http.server",
    "xmlrpc",
    "ftplib",
    "telnetlib",
    "smtplib",
    "poplib",
    "imaplib",
}

BANNED_OS_ATTRS: Set[str] = {
    # System process execution
    "system",
    "popen",
    "kill",
    "killpg",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    # Destructive & mutative filesystem modifications
    "rmdir",
    "removedirs",
    "remove",
    "unlink",
    "chmod",
    "chown",
    "lchmod",
    "lchown",
    "rename",
    "replace",
    "truncate",
    "symlink",
    "symlink_to",
    "link",
    "hardlink_to",
    "mkdir",
    "makedirs",
    "touch",
    # Privilege escalation & environment alteration
    "setuid",
    "seteuid",
    "setgid",
    "setegid",
    "putenv",
    # Process exit & debugging callbacks
    "exit",
    "_getframe",
    "settrace",
    "setprofile",
    # Shutil destruction & copying
    "rmtree",
    "move",
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "copymode",
    "copystat",
    "unpack_archive",
}

BANNED_BUILTIN_CALLS: Set[str] = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "input",
    "_getframe",
    "exit",
    "quit",
}

BANNED_DUNDERS: Set[str] = {
    # Python object reflection
    "__class__",
    "__subclasses__",
    "__mro__",
    "__bases__",
    "__base__",
    "__globals__",
    "__builtins__",
    "__code__",
    "__closure__",
    "__func__",
    "__self__",
    "__loader__",
    "__spec__",
    "__dict__",
    "__getattribute__",
    "__reduce__",
    "__reduce_ex__",
    # Frame introspection attributes
    "f_globals",
    "f_builtins",
    "f_locals",
    "f_back",
    "f_code",
}

OFFLINE_SUBSYSTEMS: Set[str] = {
    "scam_analyser",
    "component_analyser",
    "build_composer",
}

NETWORK_MODULES: Set[str] = {
    "socket",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "telnetlib",
    "asyncio",
    "xmlrpc",
    "ssl",
    "poplib",
    "imaplib",
    "nntplib",
    "selectors",
}

CORE_ALLOWED_MODULES: Set[str] = {
    "app_paths",
    "enricher",
    "formula_engine",
    "marketplace",
    "preset_loader",
    "solver_engine",
    "sources",
}


def _resolve_const_str(node: ast.AST) -> Optional[str]:
    """Statically resolves constant strings, concatenation, and basic f-strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_const_str(node.left)
        right = _resolve_const_str(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for val in node.values:
            s = _resolve_const_str(val)
            if s is not None:
                parts.append(s)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.FormattedValue):
        return _resolve_const_str(node.value)
    return None


@dataclass
class SecurityReport:
    is_safe: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.is_safe:
            return "Passed: No security violations found."
        return f"Failed ({len(self.violations)} violations):\n" + "\n".join(
            f"  - {v}" for v in self.violations
        )


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self, subsystem: Optional[str] = None) -> None:
        self.subsystem = (subsystem or "").strip().lower()
        self.violations: List[str] = []
        self.warnings: List[str] = []
        self.stdlib_names = getattr(sys, "stdlib_module_names", frozenset())

    def _check_module_name(self, mod_name: str, lineno: int) -> None:
        base_mod = mod_name.split(".")[0]
        if not base_mod:
            return

        # 1. Check banned modules
        if base_mod in BANNED_MODULES:
            self.violations.append(
                f"Line {lineno}: Banned module import '{mod_name}'"
            )
            return

        # 2. Check offline subsystem network boundary
        if self.subsystem in OFFLINE_SUBSYSTEMS and base_mod in NETWORK_MODULES:
            self.violations.append(
                f"Line {lineno}: Subsystem '{self.subsystem}' must be offline; network module '{base_mod}' is prohibited"
            )
            return

        # 3. Check for external non-stdlib pip dependencies
        if self.stdlib_names:
            if base_mod not in self.stdlib_names and base_mod not in CORE_ALLOWED_MODULES:
                self.violations.append(
                    f"Line {lineno}: External dependency '{base_mod}' detected. Marketplace modules must use Python Standard Library only"
                )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module_name(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Relative imports (e.g. from .helpers import ...) are internal to the package
        if node.level > 0:
            self.generic_visit(node)
            return

        if node.module:
            self._check_module_name(node.module, node.lineno)
            # Check prohibited imports from os, sys, shutil
            if node.module in ("os", "sys", "shutil"):
                for alias in node.names:
                    if (
                        alias.name in BANNED_OS_ATTRS
                        or alias.name in BANNED_BUILTIN_CALLS
                        or alias.name in BANNED_DUNDERS
                        or alias.name in ("modules", "path", "_getframe", "settrace", "setprofile", "exit")
                    ):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited direct import of '{alias.name}' from {node.module}"
                        )
                    elif alias.name == "environ" and self.subsystem in OFFLINE_SUBSYSTEMS:
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited direct import of 'environ' from os in offline subsystem '{self.subsystem}'"
                        )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            val = node.value.strip().lower()
            # 1. Prohibited dangerous URL schemes
            if any(val.startswith(p) for p in ("file://", "ftp://", "gopher://", "dict://", "ldap://")):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited dangerous URL scheme in literal '{node.value}'"
                )
            # 2. Prohibited cloud metadata exfiltration IP
            elif any(p in val for p in ("169.254.169.254", "http://169.254.", "https://169.254.")):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited cloud metadata service endpoint in string literal"
                )
            # 3. Prohibited loopback in market providers (SSRF defense)
            elif self.subsystem == "info_providers" and any(
                val.startswith(p) for p in ("http://127.", "http://localhost", "http://0.0.0.0", "http://[::1]")
            ):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited loopback address in marketplace info provider"
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check open() calls whether as a builtin Name('open') or Attribute(attr='open')
        is_open_call = False
        mode_node = None
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            is_open_call = True
            if len(node.args) >= 2:
                mode_node = node.args[1]
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "open":
            is_open_call = True
            if len(node.args) >= 1:
                mode_node = node.args[0]

        if is_open_call:
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode_node = kw.value
                    break

            if mode_node is not None:
                mode_str = _resolve_const_str(mode_node)
                if mode_str is None:
                    if self.subsystem not in ("extension", "extensions"):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited dynamic/non-constant mode in open() call. Marketplace plugins must specify constant read-only modes."
                        )
                elif any(c in mode_str for c in ("w", "a", "x", "+")):
                    if self.subsystem not in ("extension", "extensions"):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited write mode '{mode_str}' in open() call. Marketplace plugins are read-only."
                        )

        # 1. Check direct calls to banned builtins or special functions
        if isinstance(node.func, ast.Name):
            func_id = node.func.id
            if func_id in BANNED_BUILTIN_CALLS:
                if func_id == "input" and self.subsystem in ("extension", "extensions"):
                    pass
                else:
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited builtin call '{func_id}()'"
                    )
            elif func_id in ("setattr", "delattr"):
                # Detect tampering with system modules or builtins
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in ("os", "sys", "builtins", "__builtins__"):
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited dynamic tampering with '{node.args[0].id}' via {func_id}()"
                    )
            elif func_id == "getattr" and len(node.args) >= 2:
                # Dynamic attribute dispatch: getattr(obj, "system")
                resolved_attr = _resolve_const_str(node.args[1])
                if resolved_attr:
                    if (
                        resolved_attr in BANNED_OS_ATTRS
                        or resolved_attr in BANNED_BUILTIN_CALLS
                        or resolved_attr in BANNED_DUNDERS
                    ):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited dynamic call '{resolved_attr}' via getattr"
                        )
                else:
                    if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in ("os", "sys", "builtins", "__builtins__"):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited dynamic obfuscated attribute access on '{node.args[0].id}' via getattr()"
                        )
                    else:
                        self.warnings.append(
                            f"Line {node.lineno}: Dynamic call to getattr() with non-constant attribute name requires scrutiny"
                        )

        # 2. Check dangerous attribute calls
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in BANNED_OS_ATTRS:
                # Disambiguate str.replace() from os.replace() / Path.replace()
                if attr_name == "replace":
                    is_fs_replace = False
                    if isinstance(node.func.value, ast.Name) and node.func.value.id in ("os", "Path"):
                        is_fs_replace = True
                    elif isinstance(node.func.value, ast.Attribute) and node.func.value.attr in ("os", "Path"):
                        is_fs_replace = True
                    if is_fs_replace:
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited filesystem call 'replace()'"
                        )
                elif attr_name in ("mkdir", "makedirs") and self.subsystem in ("extension", "extensions"):
                    pass
                else:
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited system/filesystem call '{attr_name}()'"
                    )
            elif attr_name in BANNED_BUILTIN_CALLS:
                # Allow standard re.compile()
                if attr_name == "compile" and isinstance(node.func.value, ast.Name) and node.func.value.id == "re":
                    pass
                else:
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited dynamic execution '{attr_name}()'"
                    )
            elif attr_name in ("write_text", "write_bytes"):
                if self.subsystem not in ("extension", "extensions"):
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited filesystem modification '{attr_name}()'. Marketplace plugins are read-only."
                    )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in BANNED_DUNDERS:
            self.violations.append(
                f"Line {node.lineno}: Prohibited access to reflection attribute '{node.attr}'"
            )

        # Prohibit access to sys.modules or sys.path
        if isinstance(node.value, ast.Name) and node.value.id == "sys":
            if node.attr in ("modules", "path"):
                self.violations.append(
                    f"Line {node.lineno}: Prohibited inspection or tampering with 'sys.{node.attr}'"
                )

        # Prohibit access to os.environ in offline subsystems
        if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr == "environ":
            if self.subsystem in OFFLINE_SUBSYSTEMS:
                self.violations.append(
                    f"Line {node.lineno}: Prohibited access to 'os.environ' in offline subsystem '{self.subsystem}'"
                )

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Detect attempts to mutate os.environ or reassign sys.stdout / sys.stderr / sys.modules
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                if (
                    isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "os"
                    and target.value.attr == "environ"
                ):
                    self.violations.append(
                        f"Line {node.lineno}: Prohibited mutation of environment variable through 'os.environ'"
                    )
            elif isinstance(target, ast.Attribute):
                if isinstance(target.value, ast.Name):
                    if target.value.id == "os" and target.attr == "environ":
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited reassignment of 'os.environ'"
                        )
                    elif target.value.id == "sys" and target.attr in (
                        "stdout", "stderr", "stdin", "modules", "path"
                    ):
                        self.violations.append(
                            f"Line {node.lineno}: Prohibited reassignment of 'sys.{target.attr}'"
                        )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_DUNDERS:
            self.violations.append(
                f"Line {node.lineno}: Prohibited access to '{node.id}'"
            )
        self.generic_visit(node)


class SecurityScanner:
    """Static AST security scanner for third-party marketplace modules."""

    @classmethod
    def scan_code(
        cls,
        source_code: str,
        filename: str = "<module>",
        subsystem: Optional[str] = None,
    ) -> SecurityReport:
        """Analyzes Python source code and returns a SecurityReport."""
        try:
            tree = ast.parse(source_code, filename=filename)
        except SyntaxError as e:
            return SecurityReport(
                is_safe=False,
                violations=[f"SyntaxError in {filename}:{e.lineno}: {e.msg}"],
            )

        visitor = _SecurityVisitor(subsystem=subsystem)
        visitor.visit(tree)
        is_safe = len(visitor.violations) == 0
        return SecurityReport(
            is_safe=is_safe,
            violations=visitor.violations,
            warnings=visitor.warnings,
        )

    @classmethod
    def scan_file(
        cls,
        path: Union[str, Path],
        subsystem: Optional[str] = None,
    ) -> SecurityReport:
        """Reads and scans a single Python file."""
        file_path = Path(path)
        if not file_path.is_file():
            return SecurityReport(
                is_safe=False, violations=[f"File not found: {file_path}"]
            )
        try:
            code = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return SecurityReport(
                is_safe=False, violations=[f"Could not read {file_path}: {e}"]
            )
        return cls.scan_code(code, filename=file_path.name, subsystem=subsystem)

    @classmethod
    def scan_directory(
        cls,
        dir_path: Union[str, Path],
        subsystem: Optional[str] = None,
    ) -> SecurityReport:
        """Recursively scans all .py files in a directory package."""
        p = Path(dir_path)
        if not p.is_dir():
            return SecurityReport(
                is_safe=False, violations=[f"Directory not found: {p}"]
            )

        all_violations = []
        all_warnings = []
        for py_file in p.rglob("*.py"):
            rep = cls.scan_file(py_file, subsystem=subsystem)
            all_violations.extend(
                [f"[{py_file.name}] {v}" for v in rep.violations]
            )
            all_warnings.extend(
                [f"[{py_file.name}] {w}" for w in rep.warnings]
            )

        return SecurityReport(
            is_safe=len(all_violations) == 0,
            violations=all_violations,
            warnings=all_warnings,
        )
