#!/usr/bin/env python3
"""
Knapset — Autonomous Combinatorial Market Intelligence & Optimization Framework.

Zero-dependency combinatorial engine providing:
  - Combinatorial Set & Build Synthesizer (`build`)
  - Pluggable Module Marketplace & Scaffolder (`module` / `modules`)
  - Parallel Market Cache Synchronizer (`sync`)
  - Multi-Provider AI Inspector Engine (`providers`)
  - Optimization Modes & Custom Weights Engine (`modes` / `mods`)
  - Persistent SHA-256 AI Audit Ledger (`ledger`)
"""

import argparse
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

APP_VERSION = "1.0.0"

SUBCOMMANDS = {
    "domains": "List registered domain extensions (Layouts & Weights)",
    "modules": "Inspect pluggable modules across all 5 decoupled subsystems",
    "plugins": "Alias for 'modules'",
    "module": "Marketplace package manager: search, info, install, new, verify, list, update, remove, config",
    "sync": "Harvest/refresh local market caches (e.g. 'main.py sync <domain>')",
    "history": "Inspect SQLite MarketStore statistics, price drops, and listing price history (aliases: deals)",
    "deals": "Alias for 'history' — list listings with verified historical price drops",
    "providers": "Inspect configured AI providers (Gemini, OpenAI, Claude, Groq, Ollama, CLI) & run --test",
    "ledger": "Inspect persistent SHA-256 AI AuditLedger statistics and cached verdicts",
    "mcp": "Start built-in Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio",
    "serve": "Start built-in Web Dashboard & REST API server (aliases: web)",
    "watch": "Deal watchdog: refresh markets and alert (Telegram/Discord) on verified price drops",
    "fx": "Show exchange rates; 'fx --refresh' fetches live rates into the local cache",
}


def _cli_prog() -> str:
    exe = Path(sys.argv[0]).name if sys.argv else ""
    if exe in ("ks", "knapset"):
        return exe
    return "python3 main.py"


def show_onboarding_tutorial(requested_domain: Optional[str] = None, quiet: bool = False) -> None:
    """Displays a welcoming onboarding walkthrough when no domain extensions are installed."""
    if quiet:
        if requested_domain:
            sys.stderr.write(f"Domain extension '{requested_domain}' is not installed.\n")
        else:
            sys.stderr.write("No domain extensions installed.\n")
        return

    print("\n" + "═" * 78)
    print("  Knapset — Zero-Dependency Combinatorial Market Analysis Framework")
    print("═" * 78)
    if requested_domain and requested_domain not in ("template", "default"):
        print(f"\n  Notice: The '{requested_domain}' domain extension is not installed.")
    else:
        print("\n  Notice: No domain extensions installed yet.")
        print("  Knapset is domain-agnostic by default. Choose a domain package to begin:")

    print("\n  Getting Started — Choose an Option:")
    print("  ────────────────────────────────────────────────────────────────────────────")
    print("  1. Browse and install domain packages from the registry:")
    print("     • python main.py module search")
    print("     • python main.py module info <name>")
    print(f"     • python main.py module install {requested_domain or '<name>'}")
    print()
    print("  2. Or declare a declarative domain in configs/blueprints/<domain>.json")
    print()
    print("  3. Scaffold a new custom domain extension:")
    print("     • python main.py module new --type extension --name my_domain")
    print()
    print("  4. Inspect the built-in reference template domain:")
    print("     • python main.py domains --inspect template")
    print("     • python main.py template build -b 1000")
    print("  ────────────────────────────────────────────────────────────────────────────")
    print()
    print("  Usage After Installing a Domain Package:")
    print("  ────────────────────────────────────────────────────────────────────────────")
    prog = _cli_prog()
    print("\n  All domain commands are prefixed with the domain name:")
    print(f"     • Optimal Set Synthesis    : {prog} <domain> build -b <budget> (e.g. {prog} <domain> build -b 850)")
    print(f"     • Domain Help & Commands   : {prog} <domain> --help")
    print(f"     • Individual Section Search: {prog} <domain> <section> (e.g. {prog} <domain> <section> -q text)")
    print(f"     • Explore Build Modes      : {prog} <domain> modes")
    print(f"     • Inspect Active Subsystems: {prog} modules")
    print("  ────────────────────────────────────────────────────────────────────────────")
    print("  Note: You can suppress onboarding messages by passing --quiet or --no-tips.")
    print("═" * 78 + "\n")


def print_help() -> None:
    from extension_registry import list_extensions

    prog = _cli_prog()
    print("\nKnapset — Combinatorial Market Analysis & Set Optimization Framework\n")
    print("Usage:")
    print(f"  {prog} <domain> <command> [options]   (Domain-scoped commands, e.g. '<domain> build')")
    print(f"  {prog} <system_command> [options]     (Core framework & package manager commands)\n")
    print("Global Currency Flags:")
    print("  --currency <CODE>   Set active target currency (default: USDT, e.g. USDT, USD, EUR, UAH)")
    print("  --rate <SPEC>       Override exchange rate(s), e.g. 'UAH:41.5' or 'EUR:0.92,UAH:42'\n")
    print("Core System Commands:")
    print("  module      Package manager & dev tools: search, info, install, update, remove, new, verify, list, config")
    print("  modules     Inspect pluggable modules across all 5 decoupled subsystems (aliases: plugins)")
    print("  domains     List registered domain extensions and layouts (aliases: extensions)")
    print("  sync        Index local JSON market caches into SQLite and inspect cache status")
    print("  history     Inspect SQLite MarketStore price drops & listing price history (aliases: deals)")
    print("  ledger      Inspect persistent SHA-256 AI AuditLedger statistics and cached verdicts")
    print("  providers   Inspect configured AI providers & run --test")
    print("  mcp         Start built-in Model Context Protocol (MCP) JSON-RPC 2.0 server over stdio")
    print("  serve       Start built-in Web Dashboard & REST API server (aliases: web)")
    print("  watch       Deal watchdog: alert on verified price drops (Telegram/Discord)")
    print("  fx          Show exchange rates ('fx --refresh' updates the cache)")

    extensions = list_extensions()
    installed = [d for d in extensions.keys() if d != "template"]

    print("\nRegistered Domain Extensions (Prefix all domain commands with <domain>):")
    for d_id, ext in extensions.items():
        layout = ext.get_layout()
        sec_str = ", ".join(layout.section_ids()) if layout else "N/A"
        print(f"  {d_id:<11} {ext.title} (commands: build, modes, {sec_str})")

    if not installed:
        print(f"\n  Note: No domain extensions installed yet. Run '{prog} module search' to browse available packages.")

    print("\nExamples:")
    print(f"  {prog} module search                             # Search the registry for extensions")
    print(f"  {prog} module install <name>                     # Install a domain extension")
    print(f"  {prog} template build -b 1000                    # Test the solver on the built-in template")
    installed_examples = [d for d in extensions if d != "template"]
    if installed_examples:
        d0 = installed_examples[0]
        print(f"  {prog} {d0} build -b 850                          # Build synthesis in the '{d0}' domain")
        print(f"  {prog} --currency USD {d0} build -b 850           # Same, priced in USD")
    print()


def _run_sync(argv: List[str]) -> None:
    from market_client import load_market_cache, list_market_providers
    from market_store import market_store
    from extension_registry import list_extensions, find_extension_by_section

    parser = argparse.ArgumentParser(
        prog="main.py sync",
        description="Synchronize local JSON market caches into SQLite MarketStore and inspect cache status.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Target domain to sync (an installed domain id)",
    )
    parser.add_argument(
        "--domain", "-D",
        type=str,
        default=None,
        help="Target domain name (alternative to positional argument)",
    )
    parser.add_argument(
        "--category", "-c",
        type=str,
        default=None,
        help="Specific category or comma-separated list of categories to sync (section ids from the domain layout)",
    )
    parser.add_argument(
        "--provider", "-p",
        type=str,
        default="olx",
        help="Cache provider partition to sync from (default: olx; use 'all' to index all partitions)",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List all registered marketplace data adapter providers and exit",
    )
    parser.add_argument(
        "--fx",
        action="store_true",
        help="Refresh cached FX exchange rates alongside SQLite synchronization",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display current local cache freshness and counts",
    )
    args = parser.parse_args(argv)

    if args.list_providers:
        providers = list_market_providers()
        print("\nRegistered Marketplace Information Providers:\n")
        print("┌" + "─" * 16 + "┬" + "─" * 18 + "┬" + "─" * 10 + "┬" + "─" * 44 + "┐")
        print(f"│ {'Provider':<14} │ {'Source':<16} │ {'Currency':<8} │ {'Description':<42} │")
        print("├" + "─" * 16 + "┼" + "─" * 18 + "┼" + "─" * 10 + "┼" + "─" * 44 + "┤")
        for name, meta in sorted(providers.items()):
            prov_s = f"{name:<14}"
            src_s = f"{meta['source_name']:<16}"
            curr_s = f"{meta['default_currency']:<8}"
            desc = meta['description'][:42]
            desc_s = f"{desc:<42}"
            print(f"│ {prov_s} │ {src_s} │ {curr_s} │ {desc_s} │")
        print("└" + "─" * 16 + "┴" + "─" * 18 + "┴" + "─" * 10 + "┴" + "─" * 44 + "┘\n")
        return

    extensions = list_extensions()
    target_domain = (args.target or args.domain or "").lower().strip() or None
    prov = (args.provider or "olx").lower().strip()

    if args.status:
        providers_to_show = ["olx", "telemart", "all"] if prov in ("all", "multi") else [prov]
        domains_to_show = [target_domain] if target_domain else [d for d in extensions.keys() if d != "template"]
        if not domains_to_show:
            print("No domain extensions installed. Install one via 'main.py module install <domain>'.")
            return

        for d_name in domains_to_show:
            ext = extensions.get(d_name)
            layout = ext.get_layout() if ext else None
            if not layout:
                print(f"\nDomain '{d_name}' is not installed - nothing to show.")
                continue
            domain_cats = layout.section_ids()

            for p_name in providers_to_show:
                print(f"\nLocal Market Cache Status [{d_name.upper()} | {p_name.upper()}]:")
                print("─" * 78)
                total = 0
                for cat in domain_cats:
                    offers, updated_at, path = load_market_cache(cat, provider=p_name, domain=d_name)
                    total += len(offers)
                    ts = updated_at[:19] if updated_at else "missing"
                    p_display = str(path) if path else "missing / not cached"
                    print(f"  • {cat.upper():<6} │ {len(offers):>6,} listings │ Updated: {ts:<19} │ {p_display}")
                print("─" * 78)
                print(f"  Total cached listings across {len(domain_cats)} categories [{d_name.upper()} | {p_name.upper()}]: {total:,}\n")
        return

    if args.fx:
        from currency import refresh_live_exchange_rates
        refresh_live_exchange_rates()

    # Resolve target categories and domain
    cats: List[str] = []
    if target_domain:
        if target_domain not in extensions:
            print(f"Error: Unknown domain '{target_domain}'. Registered: {', '.join(extensions.keys())}")
            sys.exit(1)
        ext = extensions[target_domain]
        layout = ext.get_layout()
        domain_cats = layout.section_ids() if layout else []
        if args.category:
            cats = [c.strip().lower() for c in args.category.split(",") if c.strip()]
        else:
            cats = list(domain_cats)
    elif args.category:
        cats = [c.strip().lower() for c in args.category.split(",") if c.strip()]
        if cats:
            owner = find_extension_by_section(cats[0])
            if owner:
                target_domain = owner.domain_id

    if not cats:
        print("\nKnapset Local Market Store Synchronizer")
        print("═" * 80)
        print("Error: Missing sync target. Please specify which domain or category to index.\n")
        active_domains = {k: v for k, v in extensions.items() if k != "template"}
        print("Registered Domains Available to Sync:")
        if active_domains:
            for d_id, ext in active_domains.items():
                layout = ext.get_layout()
                secs = ", ".join(layout.section_ids()) if layout else "N/A"
                print(f"  • {d_id:<12} (Sections: {secs})")
        else:
            print("  (No domain packages installed yet. Install one via 'main.py module install <name>' or sync with -c)")
        print("\nExamples:")
        print("  python main.py sync <domain>             # Index local JSON caches into SQLite")
        print("  python main.py sync -c gpu,cpu           # Index specific sections")
        print("  python main.py sync --status             # View freshness of existing caches\n")
        return

    domain_tag = f" [{target_domain.upper()}]" if target_domain else ""
    print(f"\nIndexing {len(cats)} local category cache(s) [{', '.join(c.upper() for c in cats)}]{domain_tag} into SQLite MarketStore...")
    t0 = time.perf_counter()
    total_indexed = 0
    for cat in cats:
        offers, _, _ = load_market_cache(cat, provider=prov, domain=target_domain)
        if offers:
            upserted = market_store.upsert_offers(
                cat,
                offers,
                provider=prov if prov not in ("all", "multi", "unified") else "olx",
                domain=target_domain or "pc",
            )
            total_indexed += upserted
            print(f"  {cat.upper():<6}: {upserted:>5,} listings indexed into SQLite")
        else:
            print(f"  {cat.upper():<6}:     0 listings (no local cache file found)")
    elapsed = time.perf_counter() - t0
    print(f"Sync complete: {total_indexed:,} total listings indexed in {elapsed:.2f}s.\n")
    return


def _run_modes(argv: List[str]) -> None:
    from extension_registry import get_extension, list_domain_ids

    parser = argparse.ArgumentParser(prog="main.py modes", description="List Optimization Modes ('Mods')")
    parser.add_argument("--domain", "-D", type=str, default=None, help="Target domain extension (e.g. pc, template)")
    parser.add_argument("--inspect", "-i", type=str, default=None, help="Inspect weights & rules of a specific mode")
    args = parser.parse_args(argv)

    installed = [d for d in list_domain_ids() if d != "template"]
    target_domain = (args.domain or (installed[0] if installed else "template")).lower().strip()

    try:
        ext = get_extension(target_domain)
    except KeyError:
        print(f"\n[WARN] Domain '{target_domain}' not found.", file=sys.stderr)
        print("Install an extension via 'python main.py module install <name>' or inspect 'template'.\n", file=sys.stderr)
        return

    w_reg = ext.get_weights_registry()
    if not w_reg:
        print(f"\nDomain '{ext.domain_id}' does not define a weights registry.\n")
        return

    print(f"\nOptimization Modes for Domain: {ext.title} [{ext.domain_id}]")
    print("─" * 82)
    print(f"  {'Mode':<18} │ {'Title':<28} │ Key Criteria")
    print("─" * 82)
    for prof in w_reg.list_profiles().values():
        top_weights = sorted(prof.weights.items(), key=lambda x: x[1], reverse=True)[:3]
        top_str = ", ".join(f"{k}:{v:.2f}" for k, v in top_weights)
        print(f"  {prof.name:<18} │ {prof.title:<28} │ {top_str}")
    print("─" * 82)
    print(f"Total modes available: {len(w_reg.list_profiles())}\n")

    if args.inspect:
        prof = w_reg.resolve_profile(args.inspect)
        print(f"Mode Details: {prof.name} ({prof.title})")
        print(f"   Description       : {prof.description}")
        if prof.constraints:
            print(f"   Constraints       : {prof.constraints}")
        print(f"   Normalized Weights: {prof.weights}\n")


def _run_domains(argv: List[str]) -> None:
    from extension_registry import list_extensions

    parser = argparse.ArgumentParser(prog="main.py domains", description="List Registered Domain Extensions")
    parser.add_argument("--inspect", "-i", type=str, default=None, help="Inspect detailed layout & weights for a domain")
    args = parser.parse_args(argv)

    extensions = list_extensions()
    if args.inspect:
        target = args.inspect.lower().strip()
        if target not in extensions:
            print(f"Error: Unknown domain extension '{target}'. Available: {', '.join(extensions.keys())}")
            sys.exit(1)
        ext = extensions[target]
        layout = ext.get_layout()
        w_reg = ext.get_weights_registry()
        print(f"\nDomain Extension: {ext.title} [{ext.domain_id}]")
        print("─" * 82)
        print(f"Description: {ext.description}\n")
        print("Layout Sections (Domain Layout):")
        print(f"  {'ID':<10} │ {'Name':<20} │ {'Share Range':<14} │ Required")
        print("  " + "─" * 60)
        for sec in layout.sections:
            req_str = "Yes" if sec.required else "No"
            share_str = f"{sec.budget_share_min * 100:.0f}%-{sec.budget_share_max * 100:.0f}%"
            print(f"  {sec.id:<10} │ {sec.name:<20} │ {share_str:<14} │ {req_str}")

        print(f"\nCompatibility Rules: {len(layout.compatibility_rules)} rule(s)")
        for r in layout.compatibility_rules:
            crit = "Critical (Hard)" if r.critical else "Advisory (Soft)"
            print(f"  • {r.name:<25} [{crit}]: {r.description}")

        print(f"\nWeight Profiles (Domain Weights): {len(w_reg.list_profiles())} profile(s)")
        for p in w_reg.list_profiles().values():
            print(f"  • {p.name:<20} : {p.title} — {p.description}")
        print()
        return

    print("\nRegistered Domain Extensions (Layout & Weights Architecture)")
    print("─" * 82)
    print(f"  {'Domain ID':<12} │ {'Title':<34} │ Sections")
    print("─" * 82)
    for d_id, ext in extensions.items():
        layout = ext.get_layout()
        sec_keys = ", ".join(s.id for s in layout.sections) if layout and hasattr(layout, "sections") else "N/A"
        print(f"  {d_id:<12} │ {ext.title:<34} │ {sec_keys}")
    print("─" * 82)
    print("Use 'python main.py <domain> build --budget <amount>' to synthesize sets.")
    print("Use 'python main.py domains --inspect <id>' to view layout & compatibility rules.\n")


def _run_search(argv: List[str]) -> None:
    from extension_registry import get_extension

    parser = argparse.ArgumentParser(prog="main.py search", description="Search & filter items by domain criteria")
    parser.add_argument("--domain", "-D", type=str, required=True, help="Target domain extension id")
    parser.add_argument("--section", "-s", type=str, default=None, help="Target component section (e.g. rod, reel, gpu, cpu)")
    parser.add_argument("--query", "-q", type=str, default=None, help="Text search query")
    parser.add_argument("--mode", "-M", type=str, default=None, help="Rank and score items using optimization mode")
    parser.add_argument("--criteria", "-c", type=str, default=None, help="Domain criteria (e.g. 'power=Light,test_max=10' or 'socket=AM5,threads=16')")
    parser.add_argument("--min-price", type=float, default=None, help="Minimum price in currency")
    parser.add_argument("--max-price", type=float, default=None, help="Maximum price in currency")
    parser.add_argument("--provider", "-p", type=str, default="all", help="Marketplace information provider (default: all)")
    parser.add_argument("--limit", "-n", type=int, default=15, help="Maximum results to display (default: 15)")
    parser.add_argument("--list-criteria", action="store_true", help="List all supported criteria for this section/domain")
    args = parser.parse_args(argv)

    ext = get_extension(args.domain)
    layout = ext.get_layout()

    # Determine target section
    sec_id = args.section
    if not sec_id:
        sec_id = layout.sections[0].id if layout.sections else "default"

    if args.list_criteria:
        crits = ext.get_supported_criteria(sec_id)
        sec_obj = layout.get_section(sec_id)
        sec_name = sec_obj.name if sec_obj else sec_id.upper()
        print(f"\nSupported Search Criteria for {ext.title} — {sec_name}:")
        print("─" * 82)
        if not crits:
            print("  No specific criteria declared for this section.")
        for k, desc in sorted(crits.items()):
            print(f"  • {k:<18} : {desc}")
        print("─" * 82)
        print("Pass criteria as: --criteria \"key1=value,key2=value\"\n")
        return

    # Parse criteria string
    crit_dict: Dict[str, Any] = {}
    if args.criteria:
        for part in args.criteria.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            crit_dict[k.strip()] = v.strip()

    from provider_template import get_market_provider
    prov = get_market_provider(args.provider)

    results = ext.search(
        section_id=sec_id,
        query=args.query,
        min_price=args.min_price,
        max_price=args.max_price,
        criteria=crit_dict,
        mode=args.mode,
        limit=args.limit,
        provider=prov,
    )
    ext.print_search_results(
        results=results,
        section_id=sec_id,
        mode=args.mode,
    )


def _run_history(argv: List[str], default_domain: Optional[str] = None, prog: str = "main.py history") -> None:
    import json
    from analyzer_core import make_terminal_link, pad_ansi
    from currency import canonical_currency, convert_currency, format_price, get_active_currency
    from market_store import market_store

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect SQLite MarketStore statistics, verified seller price drops, and chronological price history.",
    )
    parser.add_argument("--domain", "-D", type=str, default=default_domain, help="Filter by domain (e.g. pc, fishing)")
    parser.add_argument("--category", "-c", type=str, default=None, help="Filter by category/section (a section id)")
    parser.add_argument("--provider", "-p", type=str, default=None, help="Filter by marketplace provider (e.g. olx, telemart)")
    parser.add_argument("--offer", "-o", "--id", dest="offer_id", type=str, default=None, help="Inspect full chronological price history for a specific offer ID")
    parser.add_argument("--min-drop", type=float, default=3.0, help="Minimum price drop percentage from peak (default: 3.0%%)")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Maximum price drop entries to display (default: 20)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args(argv)

    active_curr = get_active_currency()

    if args.offer_id:
        prov = (args.provider or "olx").lower().strip()
        trend = market_store.get_price_trend(args.offer_id, provider=prov)
        history = market_store.get_price_history(args.offer_id, provider=prov)
        if args.json:
            print(json.dumps({"offer_id": args.offer_id, "provider": prov, "trend": trend, "history": history}, indent=2, ensure_ascii=False))
            return
        print(f"\nChronological Price History for Offer #{args.offer_id} [{prov.upper()}]")
        print("─" * 74)
        if not history:
            print("  No price history records found for this offer ID.\n")
            return
        for idx, pt in enumerate(history, 1):
            raw_p = float(pt["price"])
            raw_c = str(pt["currency"] or "UAH")
            conv_p = convert_currency(raw_p, raw_c, active_curr) if canonical_currency(raw_c) != canonical_currency(active_curr) else raw_p
            p_str = format_price(conv_p, active_curr)
            orig_str = f" ({format_price(raw_p, raw_c)})" if canonical_currency(raw_c) != canonical_currency(active_curr) else ""
            print(f"  {idx:>2}. {pt['recorded_at'][:19]} │ {p_str}{orig_str}")
        if trend:
            print("─" * 74)
            print(f"  Delta vs Initial : {trend['delta_pct']:+.1f}%  │  Drop from Peak: {trend['drop_from_peak_pct']:+.1f}%  ({trend['observations']} observations)")
        print()
        return

    st = market_store.stats(domain=args.domain)
    drops = market_store.find_price_drops(
        category=args.category,
        provider=args.provider,
        domain=args.domain,
        min_drop_pct=args.min_drop,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps({"stats": st, "currency": active_curr, "drops": drops}, indent=2, ensure_ascii=False))
        return

    dom_label = f" [{args.domain.upper()}]" if args.domain else ""
    print(f"\nSQLite + FTS5 MarketStore & Price History Summary{dom_label}")
    print("─" * 78)
    print(f"  Database File       : {st['db_path']}")
    print(f"  FTS5 Search Index   : {'Enabled' if st['fts5_enabled'] else 'Fallback (LIKE)'}")
    print(f"  Indexed Listings    : {st['total_listings']:,}  │  Price Snapshots: {st['total_history_points']:,}  │  Tracked Multi-Point: {st['multi_observation_listings']:,}")
    if st["by_category"]:
        c_str = " | ".join(f"{k.upper()}: {v:,}" for k, v in sorted(st["by_category"].items()))
        print(f"  By Category         : {c_str}")
    print("─" * 78)

    if not drops:
        print(f"\n  No verified price drops >= {args.min_drop:.1f}% recorded yet.")
        print("  Tip: Run 'python main.py sync <domain>' periodically to accumulate time-series price snapshots.\n")
        return

    print(f"\nTop {len(drops)} Verified Seller Price Drops (>= {args.min_drop:.1f}% below peak, in {active_curr}):\n")
    headers = ["Cat", "Title", "Peak Price", "Now Price", "Drop", "Obs", "Link"]
    widths = [6, 30, 12, 12, 8, 4, 14]
    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    head = "│ " + " │ ".join(f"{h:<{w}}" for h, w in zip(headers, widths)) + " │"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    print(top)
    print(head)
    print(mid)
    for d in drops:
        raw_c = d["currency"]
        peak_conv = convert_currency(d["peak_price"], raw_c, active_curr)
        curr_conv = convert_currency(d["current_price"], raw_c, active_curr)
        cat_s = pad_ansi(d["category"].upper()[: widths[0]], widths[0])
        title_s = pad_ansi(d["title"][: widths[1]], widths[1])
        peak_s = pad_ansi(format_price(peak_conv, active_curr), widths[2])
        now_s = pad_ansi(format_price(curr_conv, active_curr), widths[3])
        drop_s = pad_ansi(f"-{d['drop_pct']:.1f}%", widths[4])
        obs_s = pad_ansi(f"{d['observations']}x", widths[5])
        url = d.get("url", "")
        raw_lk = f"id:{d['offer_id']}"
        lk_s = pad_ansi(make_terminal_link(url, raw_lk) if url else raw_lk, widths[6])
        print(f"│ {cat_s} │ {title_s} │ {peak_s} │ {now_s} │ {drop_s} │ {obs_s} │ {lk_s} │")
    print(bot)
    print()


def _run_fx(argv: List[str]) -> None:
    import currency

    parser = argparse.ArgumentParser(prog="main.py fx", description="Inspect or refresh exchange rates (units per 1 USDT)")
    parser.add_argument("--refresh", "-r", action="store_true", help="Fetch live rates (network) and update the local cache")
    parser.add_argument("--timeout", type=float, default=5.0, help="Network timeout in seconds (default: 5)")
    args = parser.parse_args(argv)

    if args.refresh:
        res = currency.refresh_live_rates(timeout_sec=args.timeout)
        before, after = res["before"], res["after"]
        if not res["updated"]:
            print("Live refresh unavailable (offline?) - showing cached/default rates.")
        print(f"\n  {'Currency':<10} │ {'Before':>10} │ {'After':>10}")
        print("  " + "─" * 36)
        for code in sorted(after):
            print(f"  {code:<10} │ {before.get(code, 0.0):>10.4f} │ {after[code]:>10.4f}")
        print()
        return

    rates = currency.get_active_rates()
    src = "live cache + defaults" if currency.live_rates_enabled() and currency.load_cached_rates() else "built-in defaults"
    print(f"\nExchange rates per 1 USDT ({src}):")
    for code in sorted(rates):
        print(f"  {code:<6} {rates[code]:>10.4f}")
    print("\nTip: run 'main.py fx --refresh' then use --live-rates (or KNAPSET_LIVE_RATES=1) to apply cached live rates.\n")


def _run_ledger(argv: List[str]) -> None:
    from audit_ledger import ledger

    parser = argparse.ArgumentParser(prog="main.py ledger", description="Inspect persistent SHA-256 AI AuditLedger")
    parser.add_argument("--category", "-c", type=str, default=None, help="Filter records by category")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Number of recent audit entries to display (default: 10)")
    parser.add_argument("--clear", action="store_true", help="Clear cached audit records (for --category or entire ledger)")
    parser.add_argument("--clear-approved", action="store_true", help="Clear only APPROVED records (preserves blacklisted scam/defect entries)")
    parser.add_argument("--clear-human", action="store_true", help="Clear only HUMAN approved records (preserves AI evaluations and blacklists)")
    parser.add_argument("--delete", type=str, default=None, help="Delete a specific audit record by offer ID")
    parser.add_argument("--block", type=str, default=None, help="Block / blacklist a specific offer ID")
    parser.add_argument("--reason", type=str, default="User blacklist", help="Reason for blocking (used with --block)")
    args = parser.parse_args(argv)

    if args.block:
        target_id = args.block.strip("<>").strip()
        cat = (args.category or "gpu").lower()
        ledger.record_audit(
            offer_id=target_id,
            category=cat,
            verdict="REJECTED",
            confidence_score=100,
            reasons=[args.reason],
            provider="human",
        )
        print(f"Offer {target_id} has been blocked and blacklisted in AuditLedger.")
        return

    if args.delete:
        ok = ledger.delete_audit(args.delete)
        if ok:
            print(f"Removed record {args.delete} from AuditLedger.")
        else:
            print(f"Notice: Offer ID {args.delete} not found in AuditLedger.")
        return

    if args.clear_approved or args.clear_human:
        prov = "human" if args.clear_human else None
        cnt = ledger.clear_approved(category=args.category, provider=prov)
        scope = f"{args.category.upper()}" if args.category else "ALL categories"
        label = "human approved" if args.clear_human else "approved"
        print(f"Cleared {cnt} {label} record(s) from AuditLedger for {scope} (blacklists preserved).")
        return

    if args.clear:
        ledger.clear(category=args.category)
        scope = args.category.upper() if args.category else "ALL categories"
        print(f"Cleared AuditLedger records for {scope}.")
        return

    st = ledger.stats()
    print("\nPersistent SHA-256 AuditLedger Summary")
    print("─" * 68)
    print(f"  Ledger File : {ledger.ledger_path}")
    print(f"  Total Audits: {st['total']:,}")
    if st["by_verdict"]:
        v_str = " | ".join(f"{k}: {v}" for k, v in sorted(st["by_verdict"].items()))
        print(f"  By Verdict  : {v_str}")
    if st["by_category"]:
        c_str = " | ".join(f"{k.upper()}: {v}" for k, v in sorted(st["by_category"].items()))
        print(f"  By Category : {c_str}")
    print("─" * 68)

    records = ledger.get_all_audits(category=args.category)
    records.sort(key=lambda r: r.get("audited_at", ""), reverse=True)
    if records and args.limit > 0:
        print(f"\nRecent {min(args.limit, len(records))} Cached Verdicts:")
        for r in records[: args.limit]:
            prov = f" [{r.get('provider')}]" if r.get("provider") else ""
            from currency import format_price
            p_fmt = format_price(r.get('price', 0), r.get('currency'))
            print(
                f"  • [{r.get('category', '?').upper():<4}] {r.get('verdict', '?'):<8} "
                f"({r.get('confidence_score', 0):>2}%){prov} │ {p_fmt:>12} │ {r.get('title', '')[:48]}"
            )
    print()


def _run_modules(argv: List[str]) -> None:
    from info_providers import get_info_registry
    from scam_analyser import get_scam_registry
    from build_composer import get_composer_registry
    from ai_providers import get_ai_registry
    from component_analyser import get_component_registry
    import json

    parser = argparse.ArgumentParser(prog="main.py modules", description="Inspect pluggable modules across all 5 decoupled subsystems")
    parser.add_argument(
        "--subsystem", "-s",
        type=str,
        default="all",
        choices=["all", "info", "scam", "composer", "ai", "component"],
        help="Filter modules by subsystem (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="Output module inventory in JSON format")
    args = parser.parse_args(argv)

    root_dir = Path(__file__).resolve().parent

    subsystems_config = [
        ("info", "info_providers/modules", "Market Information Providers", get_info_registry()),
        ("scam", "scam_analyser/modules", "Category Defect & Scam Analysers", get_scam_registry()),
        ("composer", "build_composer/modules", "Domain Build & Set Composers", get_composer_registry()),
        ("ai", "ai_providers/modules", "AI Reasoning & Agent Providers", get_ai_registry()),
        ("component", "component_analyser/modules", "Component Hardware Analysers", get_component_registry()),
    ]

    selected = [s for s in subsystems_config if args.subsystem == "all" or s[0] == args.subsystem]

    if args.json:
        payload = {}
        for s_id, s_dir, s_title, reg in selected:
            items = getattr(reg, "list_providers", getattr(reg, "list_analysers", getattr(reg, "list_composers", lambda: {})))()
            errors = reg.get_errors() if hasattr(reg, "get_errors") else {}
            modules_payload = {}
            for k, v in sorted(items.items()):
                raw_file = ""
                try:
                    import inspect
                    raw_file = inspect.getfile(v.__class__)
                except Exception:
                    pass
                rel_file = ""
                if raw_file:
                    try:
                        rel_file = str(Path(raw_file).resolve().relative_to(root_dir))
                    except Exception:
                        rel_file = Path(raw_file).name

                manifest = v.get_manifest() if hasattr(v, "get_manifest") else {
                    "name": k,
                    "title": getattr(v, "title", getattr(v, "label", k)),
                    "version": getattr(v, "version", "1.0.0"),
                    "author": getattr(v, "author", "Community"),
                    "description": getattr(v, "description", getattr(v, "notes", "")),
                    "min_app_version": getattr(v, "min_app_version", "1.0.0"),
                    "category": s_id,
                    "tags": list(getattr(v, "tags", [])),
                    "url": getattr(v, "url", ""),
                }

                status = "Ready"
                status_detail = ""
                if hasattr(v, "is_ready"):
                    try:
                        ok, detail = v.is_ready()
                        status = "Ready" if ok else "Missing Key"
                        status_detail = detail
                    except Exception as e:
                        status = "Ready"
                        status_detail = str(e)

                modules_payload[k] = {
                    "manifest": manifest,
                    "module_file": rel_file,
                    "status": status,
                    "status_detail": status_detail,
                }

            payload[s_id] = {
                "directory": s_dir,
                "title": s_title,
                "count": len(items),
                "error_count": len(errors),
                "errors": errors,
                "modules": modules_payload,
            }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    total_modules = 0
    total_errors = 0
    for _, _, _, reg in selected:
        items = getattr(reg, "list_providers", getattr(reg, "list_analysers", getattr(reg, "list_composers", lambda: {})))()
        total_modules += len(items)
        if hasattr(reg, "get_errors"):
            total_errors += len(reg.get_errors())

    print("\nModular Subsystem Architecture & Pluggable Registry")
    print("═" * 84)
    status_summary = f"Subsystems Discovered: {len(selected)} | Total Active Modules: {total_modules}"
    if total_errors > 0:
        status_summary += f" | Load Errors: {total_errors} (isolated)"
    print(status_summary)
    print("═" * 84)

    for idx, (s_id, s_dir, s_title, reg) in enumerate(selected, 1):
        items = getattr(reg, "list_providers", getattr(reg, "list_analysers", getattr(reg, "list_composers", lambda: {})))()
        errors = reg.get_errors() if hasattr(reg, "get_errors") else {}
        rel_dir = f"/{s_dir}"
        print(f"\n{idx}. Subsystem: {s_title}")
        print(f"   Directory: {rel_dir} ({len(items)} module(s) loaded)")
        w_mod = 20
        w_stat = 12
        w_desc = 46
        print("   ┌" + "─" * (w_mod + 2) + "┬" + "─" * (w_stat + 2) + "┬" + "─" * (w_desc + 2) + "┐")
        print(f"   │ {'Module Key':<{w_mod}} │ {'Status':<{w_stat}} │ {'Description / Source':<{w_desc}} │")
        print("   ├" + "─" * (w_mod + 2) + "┼" + "─" * (w_stat + 2) + "┼" + "─" * (w_desc + 2) + "┤")
        for k, v in sorted(items.items()):
            status = "Ready"
            if hasattr(v, "is_ready"):
                try:
                    ok, detail = v.is_ready()
                    status = "Ready" if ok else "Missing Key"
                except Exception:
                    status = "Ready"

            desc = getattr(v, "title", "")
            if not desc:
                desc = getattr(v, "description", getattr(v, "notes", getattr(v, "source_name", "")))
            desc = (desc or "")[:w_desc]

            print(f"   │ {k[:w_mod]:<{w_mod}} │ {status[:w_stat]:<{w_stat}} │ {desc:<{w_desc}} │")
        print("   └" + "─" * (w_mod + 2) + "┴" + "─" * (w_stat + 2) + "┴" + "─" * (w_desc + 2) + "┘")

        if errors:
            print("   [WARN] Failed/broken modules in this subsystem:")
            for fname, err in sorted(errors.items()):
                print(f"     • {fname}: {err}")

    print("\nExtensibility Guide:")
    print("  To add a new module, simply drop a Python file into any `/<subsystem>/modules/` directory.")
    print("  Subclasses of BaseInfoProvider, BaseScamAnalyser, BaseBuildComposer, BaseAIProvider,")
    print("  or BaseComponentAnalyser are automatically discovered with ZERO configuration!\n")


def main(argv: Optional[List[str]] = None) -> None:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    from currency import set_active_currency, parse_rate_string, set_live_rates_enabled
    filtered_args = []
    i = 0
    while i < len(raw_args):
        arg = raw_args[i]
        if arg in ("--currency",) and i + 1 < len(raw_args):
            set_active_currency(raw_args[i + 1])
            i += 2
        elif arg.startswith("--currency="):
            set_active_currency(arg.split("=", 1)[1])
            i += 1
        elif arg == "--live-rates":
            set_live_rates_enabled(True)
            i += 1
        elif arg == "--rate" and i + 1 < len(raw_args):
            parse_rate_string(raw_args[i + 1])
            i += 2
        elif arg.startswith("--rate="):
            parse_rate_string(arg.split("=", 1)[1])
            i += 1
        else:
            filtered_args.append(arg)
            i += 1
    raw_args = filtered_args

    if raw_args and raw_args[0] in ("-V", "--version", "version"):
        print(f"knapset {APP_VERSION}")
        return

    if not raw_args or raw_args[0] in ("-h", "--help", "help"):
        print_help()
        return

    cmd = raw_args[0].lower()
    extra = raw_args[1:]

    try:
        from extension_registry import (
            find_extension_by_section,
            get_extension,
            list_domain_ids,
        )

        # 1. Direct domain invocation (e.g. 'python main.py pc ...', 'python main.py fishing ...', 'python main.py template ...')
        if cmd in list_domain_ids():
            if extra and extra[0].lower() in ("history", "deals"):
                _run_history(extra[1:], default_domain=cmd, prog=f"main.py {cmd} {extra[0].lower()}")
                return
            ext = get_extension(cmd)
            ext.run_cli(extra)
            return

        # 2. Reject unprefixed domain commands or bare section names — enforce strict <domain> prefixing
        owner_ext = find_extension_by_section(cmd)
        if owner_ext is not None:
            dom = owner_ext.domain_id
            print(
                f"Error: Section command '{cmd}' must be prefixed with its domain module name '{dom}'.",
                file=sys.stderr,
            )
            print(f"Usage: {_cli_prog()} {dom} {cmd} {' '.join(extra)}".rstrip() + "\n", file=sys.stderr)
            sys.exit(1)

        if cmd in ("build", "modes", "mods", "search", "find", "eval", "vs", "compare"):
            sub_cmd = cmd
            installed_domains = [d for d in list_domain_ids() if d != "template"]
            example_dom = installed_domains[0] if installed_domains else "<domain>"
            print(
                f"Error: Top-level '{cmd}' command must be scoped to a domain module (e.g. '{example_dom}').",
                file=sys.stderr,
            )
            print(f"Usage: {_cli_prog()} {example_dom} {sub_cmd} <args>\n", file=sys.stderr)
            sys.exit(1)

        # 3. Core system subcommands
        if cmd in ("domains", "extensions"):
            _run_domains(extra)
        elif cmd == "sync":
            _run_sync(extra)
        elif cmd in ("history", "deals"):
            _run_history(extra, prog=f"main.py {cmd}")
        elif cmd == "providers":
            import ai_providers
            ai_providers.main(extra)
        elif cmd == "ledger":
            _run_ledger(extra)
        elif cmd == "fx":
            _run_fx(extra)
        elif cmd in ("watch", "watchdog"):
            from watchdog import main as watchdog_main
            watchdog_main(extra)
        elif cmd == "mcp":
            from mcp_server import run_mcp_stdio_server
            run_mcp_stdio_server(extra)
        elif cmd in ("serve", "web"):
            from web_server import run_web_server
            run_web_server(extra)
        elif cmd in ("module", "market") or (
            cmd in ("modules", "plugins")
            and extra
            and extra[0] in ("search", "install", "update", "remove", "list", "info", "config", "new", "create", "verify", "install-bundle", "uninstall")
        ):
            from marketplace.cli import run_marketplace_cli
            run_marketplace_cli(extra)
        elif cmd in ("modules", "plugins"):
            _run_modules(extra)
        else:
            is_quiet = any(arg in extra for arg in ("--quiet", "-q", "--no-tips"))
            registry_domains = set()
            registry_sections = {}
            try:
                from marketplace.client import MarketplaceClient
                idx = MarketplaceClient().fetch_registry_index()
                for m in idx.get("modules", []):
                    if m.get("subsystem") in ("extension", "extensions"):
                        m_dom = str(m.get("name") or m.get("domain") or "").lower().strip()
                        if m_dom:
                            registry_domains.add(m_dom)
                            for tag in (m.get("tags") or []):
                                t_clean = str(tag).lower().strip()
                                if t_clean and t_clean != m_dom:
                                    registry_sections[t_clean] = m_dom
            except Exception:
                pass

            if cmd in registry_domains:
                show_onboarding_tutorial(cmd, quiet=is_quiet)
                return

            if cmd in registry_sections:
                pkg = registry_sections[cmd]
                print(
                    f"Error: Section command '{cmd}' belongs to domain '{pkg}' (not installed) and must be prefixed with '{pkg}'.",
                    file=sys.stderr,
                )
                print(f"Install via: python main.py module install {pkg}", file=sys.stderr)
                print(f"Then run   : python main.py {pkg} {cmd} <args>\n", file=sys.stderr)
                sys.exit(1)

            print(f"Error: Unknown subcommand '{cmd}'\n", file=sys.stderr)
            print_help()
            sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
