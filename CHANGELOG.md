# Changelog

All notable changes to Knapset are documented in this file.

## [2.0.0] - 2026-11-17

### Architecture & Product Pivot
- Refocused Knapset from a marketplace-specific scraper/scam framework into a **zero-dependency, general-purpose database preference, formula, and constraint optimizer** sitting directly on top of existing user databases (`SQLite` read-only, `CSV`, `TSV`, `JSON`, `JSONL`, and custom `DataSourceRegistry` URI schemes).
- Removed v1 marketplace scraping, OLX/domain verticals, currency conversion, scam/AI heuristics, watchdog, and MarketStore bloat from core (`knapset`).

### Added
- **`sources/` (`sqlite_source.py`, `tabular_source.py`, `registry.py`)**: Automatic schema/type/range introspection (`ks inspect`), strict read-only SQLite URI connections (`?mode=ro` + `PRAGMA query_only = ON`), single-statement `SELECT`/`WITH` validation, and tabular type coercion.
- **`enricher.py` (`ks compare`, `--ref`)**: Cross-database matching against Reference DBs (`exact`, specificity-ordered `token`, `regex`) with `--on-unmatched drop|keep` and configurable alias (`ref.*`).
- **`formula_engine.py`**: Sandbox-safe AST evaluator for row formulas (`name = expr`), filters (`--where`), signed preference weights (`--prefer "score:+0.7,price:-0.3"`), multi-resource limits (`sum(...)`, `count`, `avg`, `min`, `max`), and cross-slot rules (`cpu.socket == mobo.socket`, `cpu.score / gpu.score < 1`).
- **`solver_engine.py` (`ks rank`, `ks basket`, `ks solve`)**: Signed-weight min-max `[0, 1]` normalization with `--explain` breakdowns across Top-K ranking, 0/1 & bounded knapsack, and multi-slot combinatorial synthesis, plus `.json`/`.csv`/`.md` export.
- **`preset_loader.py` (`ks init`, `ks run`, `ks preset`)**: Declarative `schema_version: "2.0"` JSON/TOML presets with CLI overrides, `knapset-registry` `presets` integration (`pc_build`, `fishing_tackle`), and optional Python plugin contracts (`BaseRowNormalizer`, `BaseScorer`, `BaseDataSource`).
- **`tui.py` (`ks tui`)**: Interactive stdlib `curses` TUI to load DBs/Reference DBs/Presets, adjust weights live with `←`/`→`/`+`/`-` or mouse press-and-hold, create custom formulas/limits/rules with discovered column hints, cycle optimization modes, switch live themes via `/theme`, and save customized presets.
- **`theme_config.py` & `ks reg` (`v2_cli.py`, `configs/theme.json`)**: JSON theme configuration (`configs/theme.json` + bundled `soft-dark-pink` and `agy-dark` themes), `themes` subsystem support in `MarketplaceClient`, and unified `ks reg` CLI (`search`, `info`, `install [--use]`, `list`, `remove`, `verify`, `theme list|use|show|reset`).
- **`web_server.py` & `mcp_server.py`**: Rebuilt pure-stdlib Web Studio (`/api/inspect`, `/api/solve`, `/api/presets`) and MCP JSON-RPC 2.0 server (`knapset_inspect_source`, `knapset_rank_items`, `knapset_solve_basket`, `knapset_solve_sets`, `knapset_list_presets`).

## [1.0.2] - 2026-11-17

- Added a 1-hour TTL (`KNAPSET_REGISTRY_CACHE_TTL`, default `3600`s) to `marketplace_cache.json` in `MarketplaceClient`, automatic index refresh on top-level `install()` with stale-cache offline fallback, and `--refresh` flag on `module search`, `info`, `install`, and `install-bundle`.
- Added Python `3.14` to the GitHub Actions CI matrix in both `knapset` and `knapset-registry` and upgraded workflow actions to `v7`.
- Made `TestMarketplaceClient` deterministic offline via `locate_registry_dir()` and added mocked HTTP tests for remote registry directory downloads.

## [1.0.1] - 2026-11-17

- Wired `KNAPSET_REGISTRY_URL` in `MarketplaceClient` and unified dynamic `ks` / `knapset` CLI prefix.

## [1.0.0] - 2026-11-16

- Initial public release of Knapset.
