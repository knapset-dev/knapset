# Knapset v2 Architecture

Knapset v2 is a generic, zero-dependency preference, formula, and constraint optimizer operating directly on existing read-only data sources.

---

## 1. Repository Boundaries

| Component | Responsibility | Network I/O |
| :--- | :--- | :--- |
| **`knapset` core** | Universal data sources, cross-DB reference enricher, AST formula/rule/limit evaluator, multi-mode solver (`rank`, `basket`, `solve`), preset loader, interactive `curses` TUI, Web Studio (`http.server`), MCP server (`stdio`), and preset marketplace verifier. | Only explicit `ks preset search/install` calls against `knapset-registry`. Core solving & DB reads are strictly local and offline. |
| **`knapset-registry`** | Out-of-tree shareable `.json` presets (`pc_build`, `fishing_tackle`, etc.) and optional AST-verified Python helper plugins. | None. |

---

## 2. Core Pipeline

```
1. Ingestion (sources/)
   SQLite (mode=ro, PRAGMA query_only=ON) | CSV | TSV | JSON | JSONL | Custom URI schemes
          │
          ▼
2. Enrichment (enricher.py)
   Attach one or more Reference DBs (`exact`, normalized specificity `token`, `regex`)
   under configurable alias (`ref.*`) with `on_unmatched = drop | keep`.
          │
          ▼
3. Evaluation & Normalization (formula_engine.py + solver_engine.py)
   - Sequential row formulas (`deal_pct = (ref.msrp - price) / ref.msrp * 100`)
   - Row filters (`where`)
   - Signed weights (`+` maximize, `-` minimize) with `[0, 1]` min-max normalization + `--explain`
   - Aggregate constraints (`sum`, `count`, `avg`, `min`, `max` with `<=`, `<`, `>=`, `>`, `==`)
   - Cross-slot AST rules (`cpu.socket == mobo.socket`, `cpu.score / gpu.score < 1`)
          │
          ▼
4. Execution & Surfaces
   - `solve_rank` / `solve_basket` / `solve_sets`
   - CLI (`v2_cli.py`), TUI (`tui.py`), Web Studio (`web_server.py`), MCP (`mcp_server.py`), Exports (`.json`, `.csv`, `.md`)
```

---

## 3. Core Modules

- **`sources/` (`base.py`, `sqlite_source.py`, `tabular_source.py`, `registry.py`)**: Schema introspection (`TableSchema`, `ColumnInfo` with inferred `number`/`text`/`boolean` and `min..max` bounds), strict read-only SQLite connection wrapper, and pluggable `DataSourceRegistry`.
- **`enricher.py`**: Multi-strategy reference database matching (`exact`, sorted specificity `token` so `"RTX 3080 Ti"` wins over `"RTX 3080"`, and `regex`).
- **`formula_engine.py`**: Sandbox-safe `ast` evaluator supporting arithmetic, comparisons, conditionals, `in`/`not in`, safe math/string helpers (`min`, `max`, `abs`, `round`, `sqrt`, `log`, `clamp`, `coalesce`, `contains`, `lower`, `upper`), `FormulaSpec`, `WeightTerm`, `LimitSpec`, and `RuleSpec`.
- **`solver_engine.py`**: `solve_rank`, branch-and-bound `solve_basket`, and early-rule-pruned `solve_sets` with exact shared-term normalization and cross-slot weighting.
- **`preset_loader.py`**: Schema `2.0` JSON/TOML preset validation, discovery (`get_presets_dirs()`), CLI/TUI override execution, and optional Python plugin contracts (`BaseRowNormalizer`, `BaseScorer`, `BaseDataSource`).
- **`tui.py`**: Headless-testable `TuiSessionState` + stdlib `curses` 4-panel interactive UI with live `+`/`-` weight nudging, inline formula/limit/rule editing, and preset export.
- **`web_server.py` & `mcp_server.py`**: Pure-stdlib local HTTP dashboard/API and JSON-RPC 2.0 MCP server (`knapset_inspect_source`, `knapset_rank_items`, `knapset_solve_basket`, `knapset_solve_sets`, `knapset_list_presets`).
- **`marketplace/`**: Static AST security scanner (`SecurityScanner`) and SHA-256 registry installer (`MarketplaceClient`) supporting declarative `presets` and stdlib-only plugins.
