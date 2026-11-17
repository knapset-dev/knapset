<img src="docs/logo.png" alt="Knapset logo" width="96" height="96" align="right" />

# Knapset v2

**Knapset** is a **zero-dependency, pure Python Standard Library** tool (`ks` / `knapset`) for turning your existing **SQLite databases, CSV/TSV sheets, and JSON/JSONL datasets** into ranked decisions, optimal baskets, or compatible multi-part sets using **signed preference weights**, **safe custom formulas**, **Reference DB matching**, and **multi-resource limits**.

No SQL query boilerplate, no vendor spreadsheets, no `pip install` dependency trees.

```
Existing DBs (.db / .csv / .json) ──► Reference DB Enricher (--ref) ──► Safe AST Formulas & Weights
                                                                              │
            ┌───────────────────────────┬──────────────────────────┬──────────┴─────────────┐
            ▼                           ▼                          ▼                        ▼
       ks rank (Top-K)        ks basket (Knapsack)       ks solve (Multi-Slot)     ks tui (curses) / ks serve / ks mcp
```

---

## Key Features

- **Read-Only Multi-Format Ingestion (`sources/`)**: Opens SQLite databases (`?mode=ro` + `PRAGMA query_only = ON`, single read-only `SELECT`/`WITH`), CSV, TSV, JSON, and JSONL automatically.
- **Cross-Database Matching & Enrichment (`enricher.py`, `--ref`, `ks compare`)**: Attach Reference DBs (benchmark tables, MSRP price sheets, calorie tables) onto candidate listings using `exact`, specificity-ordered `token`, or `regex` matching, exposed under `ref.*` (e.g. `ref.score`, `ref.msrp`).
- **Safe AST Formulas, Filters, Weights & Limits (`formula_engine.py`)**:
  - Derived formulas: `--formula "deal_pct = (ref.msrp - price) / ref.msrp * 100"`
  - Signed preference weights: `--prefer "ref.score:+0.7, price:-0.3"` with automatic min-max `[0, 1]` normalization and `--explain` breakdown
  - Multi-resource limits: `--limit "sum(price) <= 950"`, `--limit "sum(weight_g) <= 2000"`, `--limit "count <= 5"`
  - Cross-slot compatibility rules: `--rule "cpu.socket == mobo.socket"`, `--rule "cpu.score / gpu.ref.score < 1"`
- **Three Optimization Modes (`solver_engine.py`)**:
  - `ks rank`: Single-table Top-K ranking with `--explain` normalized breakdown.
  - `ks basket`: 0/1 or bounded multi-item knapsack under multiple resource caps.
  - `ks solve`: Multi-slot set composition across tables or separate files.
- **Interactive `curses` TUI (`ks tui`)**: Pick main & reference DBs, switch presets/profiles, switch live color themes via `/theme` in the command palette, nudge weights live with `←`/`→` or `-`/`+` (keyboard or mouse hold), type custom formulas/rules/limits inline with discovered column hints, see top results update live, and save customized `.json` presets.
- **Registry, Presets & TUI Themes (`ks reg`, `ks preset`, `configs/theme.json`)**: Save/share declarative `.json` (or `.toml` on Python 3.11+) presets, configure or switch TUI color themes in `configs/theme.json` (supporting named ANSI, 256-color indices, and `#RRGGBB` hex colors, with built-in `agy-dark` and `soft-dark-pink`), or search/install/verify community presets and themes via `knapset-registry` (`ks reg`).
- **Web Studio (`ks serve`) & MCP Server (`ks mcp`)**: Built-in local browser dashboard + JSON API (`http.server`) and zero-dependency JSON-RPC 2.0 MCP server over `stdio`.

---

## Requirements

- Python **3.10+** (Linux, macOS, WSL, Windows; on Windows, `ks tui` requires `windows-curses` or use `ks serve` / CLI flags).
- Zero external Python dependencies (100% Python Standard Library).

---

## Quick Start

```bash
# 1. Inspect tables, column types, and numeric ranges in any SQLite/CSV/JSON file
ks inspect market.db

# 2. Match candidates against a Reference DB and preview coverage
ks compare gpus.csv --ref gpu_benchmarks.csv --match title~model --on-unmatched drop

# 3. Rank rows with formulas, filters, signed weights, and normalized explanation
ks rank gpus.csv --ref gpu_benchmarks.csv --match title~model \
  --formula "val = ref.score / price" \
  --where "price <= 600" \
  --prefer "val:+0.7, price:-0.3" --explain

# 4. Pack a multi-item basket under resource limits (knapsack)
ks basket snacks.csv --limit "sum(price) <= 15" --limit "sum(weight_g) <= 500" --prefer "protein:+0.8, price:-0.2"

# 5. Synthesize multi-slot combinations across tables with AST rules
ks solve pc.db --tables cpu,mobo,gpu,psu \
  --rule "cpu.socket == mobo.socket" \
  --rule "psu.wattage >= (cpu.tdp + gpu.tdp) * 1.25" \
  --rule "cpu.score / gpu.score < 1" \
  --limit "sum(price) <= 950" \
  --prefer "gpu.score:+0.5, cpu.score:+0.3, price:-0.2" --export results.csv

# 6. Interactive Terminal UI (+ / - weights, custom rules/formulas, save preset)
ks tui pc.db

# 7. Scaffold & execute reusable JSON presets
ks init pc.db -o my_pc.json
ks run my_pc.json --prefer "price:-0.6" --limit "sum(price) <= 800"

# 8. Browse, install & activate TUI themes or presets from knapset-registry
ks reg search --type theme
ks reg install theme:nord --use
ks reg theme list
ks reg theme use soft-dark-pink
```

---

## Development & Tests

```bash
git clone https://github.com/knapset-dev/knapset.git
cd knapset
python3 -m unittest discover -s tests -v
```

Licensed under the MIT License (`LICENSE`).
