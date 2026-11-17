<img src="docs/logo.png" alt="Knapset logo" width="96" height="96" align="right" />

# Knapset

Knapset is a dependency-free Python framework for analyzing marketplace listings and optimizing compatible, budget-constrained sets. It reads normalized item datasets from a local SQLite database, filters out defective or mispriced candidates through pluggable domain rules, and synthesizes compatible multi-slot sets within a budget (CLI: `ks` / `knapset`).

The core repository is **domain-agnostic**. Domain schemas, scoring weights, compatibility rules, and defect analyzers can be declared via JSON blueprints (`DomainLayout`), scaffolded locally (`main.py module new`), or installed dynamically from an external extension registry (`knapset-registry`).

```
data/*_market_cache.json  --->  knapset_market.db  --->  knapset (core)  <---  knapset-registry
   (local JSON datasets)           (SQLite + FTS5)           (CLI / Web / MCP)        (domain plugins)
```

---

## Demo

- **Demo (`docs/demo.gif`)** shows the `ks` Unix CLI workflow (`domains`, anonymized market cache inspection, `sync`, and `value` vs `best` modes).

![Knapset CLI demo showing SQLite sync and Best-Value vs Best optimization modes](docs/demo.gif)

---

## What is in this repo

- **Constraint solver (`set_synthesizer.py`, `domain_layout.py`)**: A generic N-slot beam-search solver that maximizes a domain's utility function under a budget and hard compatibility rules.
- **Storage (`market_store.py`)**: SQLite database with WAL mode, an FTS5 full-text index on listing titles, and an append-only `price_history` table.
- **Currency converter (`currency.py`)**: Multi-currency engine (default `USDT`), with opt-in live rate synchronization (`api.binance.com` / `open.er-api.com`), 24-hour file caching (`data/.currency_cache.json`), and calibrated offline rates (`USD=1.0`, `UAH=41.5`, `EUR=0.92`, `SEK=10.5`, `PLN=4.0`, `GBP=0.79`).
- **Watchdog (`watchdog.py`)**: Scans the database for verified price drops in a domain (`--domain <id>`) and prints or POSTs alerts (Telegram / Discord webhooks). Run `--once` from cron or `--interval` as a foreground loop.
- **Interfaces**:
  - `main.py` — command-line entry point.
  - `web_server.py` — local HTTP server (`http.server`) serving a single-page dashboard and JSON endpoints.
  - `mcp_server.py` — JSON-RPC 2.0 server over `stdio` for LLM tool-use clients.
- **Template extension (`extensions/template/`)**: A minimal 3-slot domain (`core`, `addon`, `power`) shipped in-tree so the core test suite can exercise the solver and interfaces without installing external extensions.

---

## Requirements

- Python 3.10 or newer on Linux, macOS, or WSL.
- No `pip install` step for the core; it uses only the Python standard library (`sqlite3`, `http.server`, `urllib.request`, `json`, `re`, `dataclasses`, `unittest`).

---

## Quick start

### Installation & running

Install globally (`ks` and `knapset` commands in `$PATH`):

```bash
# Arch Linux / CachyOS (1-line package install from GitHub Release):
sudo pacman -U https://github.com/knapset-dev/knapset/releases/download/v1.0.1/knapset-1.0.1-1-any.pkg.tar.zst

# Or build locally from packaging/aur/PKGBUILD:
curl -fsSLO https://raw.githubusercontent.com/knapset-dev/knapset/main/packaging/aur/PKGBUILD && makepkg -si

# Cross-platform via PyPI / uv (zero external Python dependencies):
uv tool install knapset
# Or run ephemerally without installing:
uvx --from knapset ks --help

# Run from any directory once installed:
ks --help
ks template build -b 1000
```

When installed system-wide (AUR / `uv tool install`), `ks` / `knapset` automatically stores mutable databases, caches, configs, and installed plugins in `$XDG_DATA_HOME/knapset` and `$XDG_CONFIG_HOME/knapset` (`KNAPSET_HOME` overrides both).

From a local source checkout:

```bash
git clone https://github.com/knapset-dev/knapset.git
cd knapset
python3 -m unittest discover -s tests -v
```

### Built-in domains & installing extensions

Out of the box, the built-in `template` domain lets you test combinatorial synthesis and CLI exploration immediately:

```bash
python3 main.py domains --inspect template
python3 main.py template build -b 1000
python3 main.py template modes
python3 main.py template core --limit 5
```

You can also scaffold a custom domain locally (`python3 main.py module new --type extension --name my_domain`), or install domain packages (e.g., `fishing`, `pc`) from `knapset-registry`:

```bash
git clone https://github.com/knapset-dev/knapset-registry.git ../knapset-registry

# Point the marketplace client at your local registry checkout (or let it fetch from GitHub)
export KNAPSET_REGISTRY_URL="file://$(realpath ../knapset-registry)/registry.json"

python3 main.py module search
python3 main.py module install <extension-id>   # e.g. fishing, pc
```

Once a domain is installed, all commands are explicitly prefixed with `<domain>`:

```bash
python3 main.py <domain> build -b 1000
python3 main.py <domain> search -s <section>
python3 main.py <domain> modes
```

---

## Loading market data

Knapset is strictly offline-first and reads listing datasets from `data/knapset_market.db` and user-supplied `data/*_market_cache.json` files. To index local JSON datasets into SQLite and check cache freshness:

```bash
# Import existing data/*_market_cache.json files into SQLite:
python3 main.py sync <domain>

# Inspect freshness and counts of local JSON caches:
python3 main.py sync --status
```

If no database or cache files exist, `build` and `search` commands return empty results rather than fabricating listings.

---

## Command reference

| Command | Description |
| :--- | :--- |
| `python3 main.py domains` | List installed domain extensions (`template` by default). |
| `python3 main.py sync [<domain>] [--category <cat>] [--status]` | Import local `data/*_market_cache.json` files into SQLite and check cache freshness. |
| `python3 main.py <domain> build -b <budget> [-m <mode>]` | Run the constraint solver for `<domain>` under `<budget>` (defaults to `USDT`; override with `--currency`). |
| `python3 main.py <domain> modes` | List scoring modes defined by `<domain>`. |
| `python3 main.py <domain> search -s <section> [filters]` | Search candidates in a section of `<domain>` (e.g., `python3 main.py template search -s core`). |
| `python3 main.py <domain> <section> [filters]` | Direct shortcut for section search (e.g., `python3 main.py template core`). |
| `python3 main.py module search [query]` | Search extensions, modules, and bundles available in the registry. |
| `python3 main.py module list` | List currently installed marketplace modules. |
| `python3 main.py module install <id>` | Download, verify (SHA-256), and install a module, extension, or `bundle:<id>`. |
| `python3 main.py module remove <id>` | Remove an installed module. |
| `python3 main.py watch -D <domain> --once` | Evaluate price drops once for `<domain>` and exit. |
| `python3 main.py watch -D <domain> --interval 600` | Poll market data and alert every 600 seconds. |
| `python3 main.py fx [--refresh]` | Show current exchange rates or refresh cached live rates. |
| `python3 main.py serve [-p 8080]` | Start the local web UI on `http://127.0.0.1:8080`. |
| `python3 main.py mcp` | Start the MCP JSON-RPC server on `stdio`. |

---

## Web UI and HTTP API

`python3 main.py serve -p 8080` starts a local server with four tabs:

1. **Set & Build Synthesizer** — runs `GET /api/build?domain=<domain>&budget=<amount>&mode=<mode>&currency=<CODE>`.
2. **Section Search** — queries `GET /api/search?domain=<domain>&section=<section>&query=<text>`.
3. **Price Drops & History** — inspects historical drops via `GET /api/deals?min_drop_pct=3.0`.
4. **Prebuilt PC Evaluator** — calls `GET /api/prebuilt` (active when a domain extension provides `evaluate_prebuilt`, such as `pc`; returns HTTP 400 otherwise).

Additional endpoints:

- `GET /api/health` — server health and SQLite store readiness.
- `GET /api/domains` — list installed domain extensions, their sections, and modes.

---

## MCP server (LLM tool use)

`python3 main.py mcp` speaks JSON-RPC 2.0 over `stdio` (both `Content-Length` framed and newline-delimited JSON). It exposes four core tools (plus extension-contributed tools):

- `knapset_list_domains`
- `knapset_build_set`
- `knapset_search_section`
- `knapset_price_drops`
- `knapset_evaluate_prebuilt_pc` *(active when the optional `pc` extension is installed)*

Example Claude Desktop configuration:

```json
{
  "mcpServers": {
    "knapset": {
      "command": "python3",
      "args": ["/absolute/path/to/knapset/main.py", "mcp"]
    }
  }
}
```

---

## Repository layout

```
knapset/
├── main.py                  # CLI router
├── domain_layout.py         # DomainLayout / SectionSpec / CompatibilityRule dataclasses
├── set_synthesizer.py       # Beam-search constraint solver
├── extension_registry.py    # Dynamic loader for domain extensions and plugins
├── market_store.py          # SQLite + FTS5 storage and price_history tracking
├── market_client.py         # Unified reader over SQLite and JSON caches
├── currency.py              # Multi-currency engine with cached opt-in FX sync and offline defaults
├── watchdog.py              # Alert engine (CLI + Telegram/Discord webhook sender)
├── web_server.py            # Local HTTP server and embedded single-page UI
├── mcp_server.py            # stdio JSON-RPC 2.0 MCP server
├── provider_template.py     # Cross-provider condition and escrow normalization rules
├── analyzer_core.py         # Shared heuristic scoring helpers
├── scam_utils.py            # Shared defect/refusal keyword helpers
├── ai_inspector.py          # Optional LLM listing inspector and heuristic fallback
├── extensions/
│   ├── base.py              # DomainExtension ABC and JsonBlueprintDomainExtension
│   └── template/            # Built-in 3-slot reference domain used by tests
├── marketplace/
│   └── client.py            # Registry client with SHA-256 verification and AST audit
├── info_providers/          # Provider adapter registry
├── scam_analyser/           # Scam detector plugin interface
├── component_analyser/      # Component scorer plugin interface
├── build_composer/          # Build composer plugin interface
├── ai_providers/            # Optional LLM / vision inspection plugin interface
├── configs/                 # Currency defaults and mode weight presets
├── docs/                    # Domain guides, logo assets, and CLI demo GIF
├── packaging/               # Distribution manifests (AUR PKGBUILD)
└── tests/                   # unittest suite (170 tests)
```

---

## Known limitations

- **Offline dataset model**: `watch --interval` re-evaluates the local SQLite database without performing external web requests. External data feeds must be imported via local JSON datasets (`data/*_market_cache.json`) and `python3 main.py sync`.
- **Single-process HTTP server**: `web_server.py` uses Python's standard `HTTPServer` and is meant for local use on `127.0.0.1`, not public deployment.
- **AST audit is a smoke check**: `marketplace/client.py` rejects plugins that import `os`, `subprocess`, `socket`, `shutil`, or `ctypes` (unless listed in `_TRUSTED_NETWORK_MODULES`), and checks SHA-256 hashes against `registry.json`. It is not a sandbox; only install extensions from a registry you trust.
- **Shared legacy helpers**: While domain extensions are fully decoupled via `DomainExtension`, `analyzer_core.py`, `scam_utils.py`, and `ai_inspector.py` still include optional defaults used by hardware-oriented extensions.
- **Test fixtures**: Core unit tests run primarily against synthetic in-memory fixtures and non-PC example domains (`template`, photography equipment, smart-home devices in `tests/test_domain_universality.py`). Integration tests (`tests/test_e2e_extension_lifecycle.py`, `tests/test_prebuilt_evaluator.py`) also exercise installing extensions from a local `knapset-registry` checkout.

---

## Running tests

```bash
python3 -m unittest discover -s tests -v
```

On a clean checkout (without pre-installed extensions), the test suite runs 170 tests (`169` passed, `1` skipped when `pc` is not pre-installed).
