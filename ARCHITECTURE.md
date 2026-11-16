# Knapset Architecture

This document describes how the core framework (`knapset`) and extension repository (`knapset-registry`) fit together and how data moves from normalized listing datasets to ranked builds.

---

## 1. Repository boundaries

| Repository | Responsibility | Runs network I/O? |
| :--- | :--- | :--- |
| **`knapset`** | Storage (`market_store.py`), generic beam-search solver (`set_synthesizer.py`), CLI/Web/MCP interfaces, plugin installer (`marketplace/client.py`). | Only for opt-in FX rates (`currency.py`), webhook alerts (`watchdog.py`), optional LLM APIs, and downloading plugins from `knapset-registry`. |
| **`knapset-registry`** | Out-of-tree plugins installed on demand: the `pc` domain extension, the `fishing` domain extension, and standalone modules. | No. |

---

## 2. Data flow

```
1. Local Dataset Input
   data/<category>_market_cache.json  -->  main.py sync  -->  data/knapset_market.db (listings, listings_fts, price_history)

2. Query & Optimization (knapset + installed domain extension)
   main.py / web_server.py / mcp_server.py
               -->  ExtensionRegistry.get(domain)
               -->  MarketClient / MarketStore (reads SQLite or JSON cache)
               -->  Domain screeners & databases (match model, filter defects, convert currency)
               -->  SetSynthesizer (beam search over DomainLayout sections & CompatibilityRules)
               -->  Ranked builds + risk notes + seller confirmation flags
```

---

## 3. Core modules (`knapset`)

### `domain_layout.py` and `set_synthesizer.py`

A domain describes its build problem by returning a `DomainLayout`:

- **`sections`**: Ordered list of `SectionSpec` objects (for example, `cpu`, `mobo`, `ram`, `gpu`, `psu`). Each section has a `budget_clearance_ratio` hint, a `required` flag, and a `scorer` callback `(item, context) -> float`.
- **`rules`**: List of `CompatibilityRule` objects. Each rule has a `check(partial_build) -> (bool, reason)` function.
- **`utility_fn`**: Optional function that scores a complete set of items.

`SetSynthesizer.compose(candidate_pool, budget)` sorts each section's candidates by score, keeps the top `beam_width` (default 25) partial builds at each step, prunes any partial build that exceeds `budget` or fails a `CompatibilityRule` whose required slots are already filled, and returns the top-scoring complete builds.

### `market_store.py` (`MarketStore`)

- Uses SQLite in `WAL` mode (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`).
- Tables:
  - `listings` — keyed by `(source, offer_id)`. Stores `category`, `domain`, `title`, `price`, `currency`, `condition`, `is_new`, `has_delivery`, `url`, `payload_json`, and `updated_at`.
  - `listings_fts` — FTS5 virtual table over `title`, `category`, and `source`, kept in sync by `AFTER INSERT`, `AFTER UPDATE`, and `AFTER DELETE` triggers.
  - `price_history` — append-only log `(source, offer_id, price, currency, recorded_at)`. `upsert_listings()` only inserts a new row into `price_history` when the listing is new or its price changed by at least `0.01`.
  - `alert_log` — records alerts emitted by `watchdog.py`.

### `currency.py` (`fetch_live_rates`, `convert_currency`)

- Uses opt-in public rate endpoints (`api.binance.com` for `USDT/UAH` and `open.er-api.com` for fiat rates).
- Writes cached rates to `data/.currency_cache.json` with a 24-hour TTL (`86400` seconds) by default.
- If network endpoints fail and no valid cache exists, falls back to `DEFAULT_EXCHANGE_RATES` relative to `1.0 USDT` (`USD=1.0`, `UAH=41.5`, `EUR=0.92`, `GBP=0.79`, `PLN=4.0`, `SEK=10.5`).

### `provider_template.py` (`ProviderFeatureAdapter`)

Normalizes raw marketplace item dictionaries (such as `olx`, `telemart`, `hotline`, `ebay`, and `blocket`) into a common schema:

- **`condition`**: `"new"`, `"refurbished"`, `"used"`, or `"for_parts"`.
- **`is_secure_deal`**: Boolean indicating whether platform buyer protection / escrow is active.
  - On OLX, checks `safedeal.status == "active"` or `delivery.rock.active`, then scans the title and description for localized phrases refusing shipping or escrow; if found, `is_secure_deal` is forced to `False`.
  - On Blocket, checks `shipping_available` / `blocketpaketet` / `kopsydd`, and forces `False` if the text indicates local pickup only or no shipping.
  - On eBay, marks `False` when `condition == "for_parts"` or the text contains `"NO RETURNS"`, `"AS-IS"`, `"LOCAL PICKUP ONLY"`, or `"NO SHIPPING"`.

### `extension_registry.py` and `marketplace/client.py`

- Installed domain extensions live under `extensions/<id>/` (with `extensions/template/` tracked in git and others gitignored).
- Other plugin types (`info_providers`, `scam_analyser`, `component_analyser`, `build_composer`, `ai_providers`) install into `<subsystem>/modules/`. Note: in the current codebase, domain extensions (`extensions/<id>/`) and `info_providers` are actively invoked by the CLI/Web/MCP pipelines, whereas `scam_analyser`, `component_analyser`, and `build_composer` registries are loaded into memory but not yet called by `SetSynthesizer` or the PC extension's internal optimizer.
- During `market install <id>`, `MarketplaceClient`:
  1. Fetches `registry.json` from `KNAPSET_REGISTRY_URL` (defaults to the `knapset-registry` GitHub raw URL, or a local `file://` path).
  2. Downloads each file listed in the manifest (or whole directory for multi-file packages).
  3. Verifies the SHA-256 digest of single-file modules against `manifest["sha256"]`.
  4. Parses every `.py` file with `ast.parse()` and rejects modules that import blocked system packages (`os`, `subprocess`, `socket`, `shutil`, `ctypes`, `pty`) unless the module ID is in `_TRUSTED_NETWORK_MODULES`.

---

## 4. Writing a new domain extension

A domain extension can be either:

1. **Declarative (`manifest.json` only)** — subclassed automatically via `JsonBlueprintDomainExtension` if you only need keyword matching and basic slot budgets.
2. **Programmatic (`__init__.py` + `DomainExtension` subclass)** — used when you need custom regex databases, multi-stage scam filtering, or custom CLI subcommands (like `pc` and `fishing`).

Minimal programmatic skeleton:

```python
from extensions.base import BaseDomainExtension
from domain_layout import DomainLayout, SectionSpec, CompatibilityRule

class MyDomainExtension(BaseDomainExtension):
    @property
    def domain_id(self) -> str:
        return "mydomain"

    @property
    def display_name(self) -> str:
        return "My Custom Domain"

    def get_layout(self) -> DomainLayout:
        return DomainLayout(
            domain_id="mydomain",
            display_name="My Custom Domain",
            sections=[
                SectionSpec("part_a", "Part A", budget_clearance_ratio=0.6),
                SectionSpec("part_b", "Part B", budget_clearance_ratio=0.4),
            ],
            rules=[
                CompatibilityRule(
                    rule_id="socket_match",
                    description="Part A and Part B must share the same mount",
                    required_sections=["part_a", "part_b"],
                    check_fn=lambda b: (b["part_a"].get("mount") == b["part_b"].get("mount"), "Mount mismatch"),
                )
            ],
        )

    def compose_build(self, budget: float, mode: str = "default", **kwargs):
        ...
```
