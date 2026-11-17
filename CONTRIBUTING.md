# Contributing to Knapset v2

Thank you for your interest in contributing to **Knapset**.
Our project is built on three core principles:

1. **Zero External Dependencies** — strictly Python `>=3.10` Standard Library (no `pip install` runtime dependencies).
2. **Read-Only Data Safety** — inspect and optimize user SQLite/CSV/TSV/JSON/JSONL datasets without mutating source databases.
3. **Composable Core & Registry** — declarative `.json` presets and TUI themes, plus optional stdlib-only plugins.

---

## Extending Knapset v2

### 1. Declarative Presets & Themes
- **Presets (`modules/presets/` in `knapset-registry`)**: Pure JSON (`schema_version: "2.0"`) defining sources, reference matches, formulas, resource limits, cross-slot AST rules, and weight profiles. Test locally with `ks run path/to/preset.json`.
- **TUI Themes (`configs/themes/` or `knapset-registry/modules/themes/`)**: Pure JSON theme manifests defining ANSI, 256-color (`0..255`), or `#RRGGBB` colors and UI glyphs. Validate and test locally with `ks reg theme use path/to/theme.json`.

### 2. Optional Python Plugins & Core Modules
- **Data Sources (`sources/registry.py`)**: Implement `BaseDataSource` (`sources/base.py`) to expose new read-only formats or URI schemes.
- **Row Normalizers & Scorers (`preset_loader.py`)**: Implement `BaseRowNormalizer` or `BaseScorer` using Python Standard Library only (verified by `marketplace/security.py`).

---

## Testing Guidelines

Before opening a pull request, run the full stdlib test suite and CLI checks:

```bash
python3 -m unittest discover -s tests -v
python3 main.py --help
python3 main.py reg theme show
```

To add tests, create or extend a `unittest` module in `tests/` (`tests/test_*.py`).

---

## Code Standards

- **Standard Library Only**: Do not introduce third-party runtime dependencies in `pyproject.toml` or source modules.
- **Sandbox Safety**: All user formulas, filters, rules, and plugins must pass AST validation (`formula_engine.py`, `marketplace/security.py`).
- **Path Portability**: Resolve paths cleanly via `app_paths.py` (`KNAPSET_HOME`, XDG base directories, or source checkout fallback).

---

## Submitting a Pull Request

1. Fork the repository and create your branch: `git checkout -b feat/my-change`.
2. Commit your changes with clear Conventional Commit messages.
3. Push to your fork and open a Pull Request against `main` (or `v2` during the v2 cycle).
