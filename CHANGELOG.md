# Changelog

All notable changes to Knapset are documented in this file.

## [2.1.0] - 2026-11-19

### Added
- **Full 4-Surface Parity (`web_server.py`, `mcp_server.py`)**: Expanded MCP server to 9 tools (`knapset_compare_sources`, `knapset_prefs`, `knapset_registry`, `knapset_sensitivity`) and upgraded Web Studio with `/api/compare`, `/api/files`, `/api/prefs`, `/api/themes`, `/api/registry`, `/api/sensitivity`, separated `where` vs `rules`, live `-`/`+` weight sliders, local file picker, Save Preset, JSON/CSV/Markdown export, and header theme switcher.
- **Solver Core (`solver_engine.py`, `formula_engine.py`)**: Added outlier-resilient `robust` (Tukey IQR, default), `minmax`, and `rank` normalization (`--norm`), structured infeasibility diagnostics (`meta.diagnostics`), fractional-knapsack Dantzig upper-bound pruning in `solve_basket`, `BasketSolution` `--explain` breakdowns, multi-limit Pareto retention & `--diverse` Top-N sets in `solve_sets`, `ks frontier` Pareto tradeoff sweep, column typo suggestions, and new formula helpers (`if_else`, `startswith`, `endswith`, `regex_match`, `pct_diff`).
- **Real-World Ingestion & Matching (`sources/`, `enricher.py`)**: Added UTF-8 BOM stripping, CSV delimiter sniffing (`;` / `,` / `\t` / `|`), nested JSON `#path` loading, stdin (`-`) piping, `fuzzy` (`difflib.SequenceMatcher`) matching with provenance, and composite `+` match keys (`brand+model~make+name`).
- **Decision Workflows (`--pin`, `--exclude`, `ks sensitivity`)**: Lock specific slots/items, ban candidates, and run weight/limit perturbation stability analysis across CLI, curses TUI (`/norm`, `/diverse`, `/pin`, `/exclude`, `/sensitivity`), Web Studio, and MCP.
- **Modularization & Registry Ecosystem**: Split monolithic `tui.py` into `tui/{state,renderer,app}.py`, extracted `web_assets/studio.html`, updated packaging (`pyproject.toml`, `MANIFEST.in`, `PKGBUILD`), and added `meal_prep`, `cloud_vms`, and `camera_kit` presets to `knapset-registry`.

### Fixed
- Fixed Web Studio **Load** (clear stale source path, guide/open Registry when empty), **Inspect** (`Enter` key support, quote stripping, visible top-of-results error/status banner, `min..max` ranges), **Export** (reliable `document.body` download trigger, `basket` CSV item flattening, `.md` export), and safe `data-exclude-idx` event delegation.

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
- **Shared preference profiles**: `s` saves weights/formulas/filters/limits/rules to `~/.config/knapset/profiles/` (DB-independent); `l` / `/profiles` / `ks tui --prefs NAME_OR_FILE` apply them to any DB with matching column names (missing columns are skipped), or load from any profile/preset file. `ks prefs list|show|save|import|export|remove|path` manages the library and `--prefs NAME_OR_FILE` applies it to `ks rank|basket|solve` (explicit flags win). Reopening a source restores its last saved session (`--fresh` to skip).
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
