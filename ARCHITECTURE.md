# Knapset v2 Architecture

Knapset v2 is a generic, zero-dependency preference, formula, and constraint optimizer operating directly on existing read-only data sources.

---

## 1. Repository Boundaries

| Component | Responsibility | Network I/O |
| :--- | :--- | :--- |
| **`knapset` core** | Data sources, cross-DB reference enricher, AST formula/rule/limit evaluator, solver (`rank`, `basket`, `solve`), preset & profile loader, curses TUI + themes, Web Studio (`http.server`), MCP server (`stdio`), registry client/verifier. | Only explicit `ks reg` / `ks preset` calls against `knapset-registry`. Solving and DB reads are strictly local and offline. |
| **`knapset-registry`** | Out-of-tree shareable items: declarative `presets` (JSON + sample data), `themes` (single JSON), bundles, and optional AST-verified Python plugins. Indexed in `registry.json` with SHA-256 per item. | None. |

---

## 2. Core Pipeline

```
1. Ingestion (sources/)
   SQLite (mode=ro, PRAGMA query_only=ON) | CSV | TSV | JSON | JSONL | custom URI schemes
          │
          ▼
2. Enrichment (enricher.py)
   Attach Reference DBs (`exact`, specificity-ordered `token`, `regex`)
   under an alias (`ref.*`) with `on_unmatched = drop | keep`.
          │
          ▼
3. Evaluation & Normalization (formula_engine.py + solver_engine.py)
   - Sequential row formulas (`deal_pct = (ref.msrp - price) / ref.msrp * 100`)
   - Row filters (`where`)
   - Signed weights (`+` maximize, `-` minimize), min-max `[0, 1]` normalization, `--explain`
   - Aggregate limits (`sum`, `count`, `avg`, `min`, `max` with `<=`, `<`, `>=`, `>`, `==`)
   - Cross-slot AST rules (`cpu.socket == mobo.socket`, `cpu.score / gpu.score < 1`)
          │
          ▼
4. Execution & Surfaces
   solve_rank / solve_basket / solve_sets
   CLI (v2_cli.py) · TUI (tui.py) · Web Studio (web_server.py) · MCP (mcp_server.py) · exports (.json/.csv/.md)
```

---

## 3. Core Modules

- **`sources/`** (`base.py`, `sqlite_source.py`, `tabular_source.py`, `registry.py`): schema introspection (`TableSchema`, `ColumnInfo` with inferred `number`/`text`/`boolean` and ranges), strict read-only SQLite wrapper, CSV BOM & delimiter sniffing, JSON `#path` selection, stdin (`-`) piping, and pluggable `DataSourceRegistry` (`open_source`, `inspect_source`).
- **`enricher.py`**: reference matching (`enrich_rows`, `enrich_from_source`, `compare_sources`, `collect_ref_specs`, `enrich_slot_pools`, `parse_match_expr`); supports `exact`, specificity-sorted `token` (so `"RTX 3080 Ti"` beats `"RTX 3080"`), `regex`, `fuzzy` (`difflib.SequenceMatcher`), composite `+` keys, and match provenance.
- **`formula_engine.py`**: sandboxed `ast` evaluator (`SafeExpression`) with arithmetic, comparisons, conditionals, `in`/`not in`, and safe helpers (`min`, `max`, `abs`, `round`, `sqrt`, `log`, `clamp`, `coalesce`, `contains`, `startswith`, `endswith`, `regex_match`, `if_else`, `pct_diff`, `lower`, `upper`); `FormulaSpec`, `WeightTerm`, `LimitSpec`, `RuleSpec`.
- **`solver_engine.py`**: `solve_rank`, fractional-bound `solve_basket`, early-rule-pruned `solve_sets` (with `--diverse`, `--pin`, `--exclude`, multi-limit pruning, and infeasibility diagnostics), outlier-resilient `robust`/`minmax`/`rank` normalization (`score_rows_normalized`), and `analyze_sensitivity`.
- **`preset_loader.py`**: schema `2.0` JSON/TOML presets (`PresetConfig`, `load_preset`, `save_preset`, `discover_presets`, `resolve_preset`), optional plugin contracts (`BaseRowNormalizer`, `BaseScorer`, `BaseDataSource`), and shared profile helpers (`save_shared_profile`, `read_profile_file`, `list_shared_profiles`, `resolve_shared_profile`, `delete_shared_profile`, `filter_profile_for_columns`, `resolve_prefs_for_pools`).
- **`theme_config.py`**: theme schema and validation (named ANSI, `0..255`, `#RRGGBB` quantized to the terminal), discovery (`discover_themes`), and the active theme in `theme.json` (`load_active_theme`, `set_active_theme`, `reset_active_theme`).
- **`tui/`** (`state.py`, `renderer.py`, `app.py`, `__init__.py`): modularized headless-testable `TuiSessionState`, formatting helpers, and stdlib `curses` UI (weights with arrows/`+`/`-` and mouse hold, formulas/limits/rules, presets, themes, saved profiles, pins/excludes, sensitivity).
- **`v2_cli.py` / `main.py`**: command router and implementations (`inspect`, `init`, `compare`, `rank`, `basket`, `solve`, `sensitivity`, `run`, `preset`, `prefs`, `reg`, plus `tui`, `serve`, `mcp`).
- **`web_server.py` + `web_assets/studio.html` / `mcp_server.py`**: pure-stdlib full-parity Web Studio (`/api/inspect`, `/api/compare`, `/api/solve`, `/api/sensitivity`, `/api/presets`, `/api/prefs`, `/api/themes`, `/api/registry`) and 9-tool JSON-RPC 2.0 MCP server (`knapset_inspect_source`, `knapset_compare_sources`, `knapset_rank_items`, `knapset_solve_basket`, `knapset_solve_sets`, `knapset_list_presets`, `knapset_prefs`, `knapset_registry`, `knapset_sensitivity`).
- **`marketplace/`**: `MarketplaceClient` (search/info/install/remove/verify for `presets`, `themes`, plugins and bundles; SHA-256 checks; cached index with TTL) and `SecurityScanner` (AST checks for Python plugins).
- **`app_paths.py`**: resolves paths for a source checkout, `KNAPSET_HOME`, or XDG directories.

---

## 4. Persistence & User Data

| Data | Location (XDG layout) | Written by |
| :--- | :--- | :--- |
| Workspace presets (sources, references, profiles) | `~/.config/knapset/presets/<name>.json` | TUI `s`, `export_preset` |
| **Shared preference profiles** (DB-independent: weights, formulas, filters, limits, rules, mode) | `~/.config/knapset/profiles/<name>.json` | TUI `s`, `ks prefs save/import` |
| Active theme | `configs/theme.json` (checkout) or `~/.config/knapset/theme.json` | `ks reg theme use`, TUI `/theme` |
| Installed registry items | `<plugins root>/presets`, `<plugins root>/themes` | `ks reg install` |
| Registry index cache | `~/.local/share/knapset/marketplace_cache.json` | `ks reg search/install` |

`KNAPSET_HOME` redirects all of the above under one directory (`configs/`, `presets/`, `profiles/`, ...).

**Reuse across databases.** A profile stores only column expressions, so it applies to any DB whose columns match (`ks tui other.db --prefs NAME`, TUI `l` / `/profiles`, or `ks rank|basket|solve --prefs NAME_OR_FILE`). Weights on missing columns are skipped and reported; a profile that breaks the solve is rejected without changing the current setup. Reopening the same source restores its last saved workspace preset (`--fresh` to skip).

---

## 5. Registry & Themes

`ks reg` is the single entry point for the registry:

```
ks reg search [--type theme|preset]    ks reg install theme:nord --use
ks reg info <name>                     ks reg list | remove | verify
ks reg theme list|use|show|reset
```

Themes are single JSON files (`modules/themes/files/<name>.json` plus a manifest); presets are directory packages. `scripts/validate_submission.py` and `scripts/build_index.py` in `knapset-registry` validate schemas, AST-scan any Python, and compile `registry.json`.
</Content>
