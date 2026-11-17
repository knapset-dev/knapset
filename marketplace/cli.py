"""
Marketplace CLI Command Router & UI Formatter.

Implements all user-facing `python3 main.py module <subcommand>` interactions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

from app_paths import get_cli_prog
from marketplace.client import MarketplaceClient


def format_trust_badge(level: str) -> str:
    norm = level.lower().strip()
    if norm == "official":
        return "[OFFICIAL]"
    elif norm == "verified":
        return "[VERIFIED]"
    return "[COMMUNITY]"


def _handle_install(client: MarketplaceClient, target: str, force: bool = False) -> None:
    print(f"Installing '{target}'...")
    ok, msg = client.install(target, force=force)
    if not ok:
        print(f"[FAIL] Failed to install '{target}':\n{msg}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] {msg}")
    prog = get_cli_prog()
    target_clean = target.lower().strip()
    if target_clean in ("pc", "extension:pc", "domain:pc"):
        print("\nQuick Start Guide for PC Extension:")
        print(f"  • Synthesize PC Build under budget : {prog} pc build -b 850")
        print(f"  • Explore optimization modes       : {prog} pc modes")
        print(f"  • View PC domain CLI help          : {prog} pc --help")
        print(f"  • Search individual hardware parts : {prog} pc gpu --help (or cpu, mobo, ram, psu)\n")
    elif target_clean in ("fishing", "extension:fishing", "domain:fishing"):
        print("\nQuick Start Guide for Fishing Extension:")
        print(f"  • Synthesize tackle set under budget : {prog} fishing build -b 150")
        print(f"  • Explore fishing weight profiles    : {prog} fishing modes")
        print(f"  • View Fishing domain CLI help       : {prog} fishing --help")
        print(f"  • Search individual tackle parts     : {prog} fishing rod --help (or reel, line, bait)\n")
    elif target_clean.startswith("bundle:") or "kit" in target_clean or "starter" in target_clean or "bundle" in msg.lower():
        print("\nQuick Start Guide for Bundle / Kit:")
        print(f"  • Inspect newly active modules : {prog} modules")
        print(f"  • Run combinatorial solver     : {prog} pc build -b 850\n")
    elif "extension" in msg.lower():
        ext_name = target_clean.split(":")[-1]
        print(f"\nQuick Start Guide for '{ext_name}':")
        print(f"  • Run optimal synthesis : {prog} {ext_name} build -b <budget>")
        print(f"  • View domain CLI help  : {prog} {ext_name} --help")
        print(f"  • Explore modes         : {prog} {ext_name} modes\n")
    else:
        print("\nQuick Start:")
        print(f"  • Inspect active modules : {prog} modules")
        print(f"  • View module config     : {prog} module config {target}\n")


def run_marketplace_cli(args: List[str], root_dir: Optional[Path] = None) -> None:
    prog = get_cli_prog()
    parser = argparse.ArgumentParser(
        prog=f"{prog} module",
        description="Hardware Market Intelligence Module Marketplace & Package Manager",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Module marketplace action")

    # Search
    search_p = subparsers.add_parser("search", help="Search available modules & bundles in marketplace")
    search_p.add_argument("query", nargs="?", default="", help="Search query string")
    search_p.add_argument("-d", "--domain", help="Filter by target domain (e.g. hardware_pc, photo_video)")
    search_p.add_argument("-s", "--subsystem", help="Filter by subsystem")
    search_p.add_argument("-t", "--tag", help="Filter by tag")
    search_p.add_argument("--registry", help="Custom registry index URL or directory path")
    search_p.add_argument("--json", action="store_true", help="Output results in JSON format")

    # Info
    info_p = subparsers.add_parser("info", help="Show detailed info about a module or bundle")
    info_p.add_argument("name", help="Name of module or bundle (e.g. sample_provider, bundle:pc-starter)")
    info_p.add_argument("--registry", help="Custom registry index URL or directory path")
    info_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # Install
    inst_p = subparsers.add_parser("install", help="Install a module, bundle, domain, or tag")
    inst_p.add_argument(
        "target", help="Module name, bundle:name, domain:name, or tag:name"
    )
    inst_p.add_argument("-f", "--force", action="store_true", help="Force overwrite if already installed")
    inst_p.add_argument("--registry", help="Custom registry index URL or directory path")

    # Install bundle (convenience subcommand)
    inst_b_p = subparsers.add_parser("install-bundle", help="Install a curated bundle of modules from the registry")
    inst_b_p.add_argument("target", help="Bundle name (e.g. pc-starter, photo-kit)")
    inst_b_p.add_argument("-f", "--force", action="store_true", help="Force overwrite if already installed")
    inst_b_p.add_argument("--registry", help="Custom registry index URL or directory path")

    # Update
    up_p = subparsers.add_parser("update", help="Update installed modules to the latest version")
    up_p.add_argument("name", nargs="?", default=None, help="Specific module name to update")
    up_p.add_argument("-a", "--all", action="store_true", help="Update all installed modules")
    up_p.add_argument("--registry", help="Custom registry index URL or directory path")

    # Remove
    rem_p = subparsers.add_parser("remove", aliases=["uninstall"], help="Uninstall and remove a module")
    rem_p.add_argument("name", help="Module name to remove")
    rem_p.add_argument("-f", "--force", action="store_true", help="Force removal even if other modules depend on it")

    # List
    list_p = subparsers.add_parser("list", help="List all currently installed marketplace modules")
    list_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # Config
    cfg_p = subparsers.add_parser("config", help="Inspect and check required API keys / environment variables")
    cfg_p.add_argument("name", help="Module name to check configuration for")
    cfg_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # New / Scaffold
    new_p = subparsers.add_parser("new", aliases=["create"], help="Scaffold a new module or domain extension boilerplate from templates")
    new_p.add_argument("--type", "-t", required=True, choices=["info_providers", "scam_analyser", "component_analyser", "build_composer", "ai_providers", "extension"], help="Type of module or extension to scaffold")
    new_p.add_argument("--name", "-n", required=True, help="Unique name/slug for the new module or extension (e.g. example_market, photo_sharpness, laptops)")
    new_p.add_argument("--domain", "-d", default="general", help="Target market domain (default: 'general')")
    new_p.add_argument("--dest", default=None, help="Optional custom destination directory or path")
    new_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # Verify / Audit
    ver_p = subparsers.add_parser("verify", help="Audit modules or local files/directories for AST security and protocol contracts")
    ver_p.add_argument("name", nargs="?", default=None, help="Module name or local file/directory path to verify")
    ver_p.add_argument("--json", action="store_true", help="Output in JSON format")

    parsed = parser.parse_args(args)

    if not parsed.subcommand:
        parser.print_help()
        return

    client = MarketplaceClient(
        registry_url_or_path=getattr(parsed, "registry", None),
        root_dir=root_dir,
    )

    if parsed.subcommand == "search":
        res = client.search(
            query=parsed.query,
            domain=parsed.domain,
            subsystem=parsed.subsystem,
            tag=parsed.tag,
        )
        if parsed.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return

        mods = res.get("modules", [])
        bundles = res.get("bundles", [])

        print("\nMarketplace Registry Search Results")
        print("═" * 84)
        if not mods and not bundles:
            print("No matching items found in registry.")
            return

        domain_exts = [m for m in mods if m.get("subsystem") == "extension"]
        plugins = [m for m in mods if m.get("subsystem") != "extension"]

        # 1. Domain Blueprints (Complete Systems)
        if domain_exts:
            print(f"1. Domain Blueprints ({len(domain_exts)} systems):")
            w_name = 20
            w_domain = 14
            w_trust = 12
            w_status = 20

            top = "  ┌" + "─" * (w_name + 2) + "┬" + "─" * (w_domain + 2) + "┬" + "─" * (w_trust + 2) + "┬" + "─" * (w_status + 2) + "┐"
            hdr = f"  │ {'Domain Name':<{w_name}} │ {'Domain ID':<{w_domain}} │ {'Trust':<{w_trust}} │ {'Version / Status':<{w_status}} │"
            div = "  ├" + "─" * (w_name + 2) + "┼" + "─" * (w_domain + 2) + "┼" + "─" * (w_trust + 2) + "┼" + "─" * (w_status + 2) + "┤"
            bot = "  └" + "─" * (w_name + 2) + "┴" + "─" * (w_domain + 2) + "┴" + "─" * (w_trust + 2) + "┴" + "─" * (w_status + 2) + "┘"

            print(top)
            print(hdr)
            print(div)
            for m in domain_exts:
                status_str = f"Installed v{m['installed_version']}" if m.get("is_installed") else f"v{m['version']} Ready"
                badge = format_trust_badge(m.get("trust_level", "community"))
                mod_name = m['name'][:w_name]
                mod_domain = m.get('domain', 'general')[:w_domain]
                print(f"  │ {mod_name:<{w_name}} │ {mod_domain:<{w_domain}} │ {badge:<{w_trust}} │ {status_str:<{w_status}} │")
            print(bot)

        # 2. Feature Bundles (Packaged Kits)
        if bundles:
            print(f"\n2. Feature Bundles ({len(bundles)} kits):")
            for b in bundles:
                status_str = "Installed" if b.get("all_installed") else "Available"
                print(f"  • {b['name']:<20} [{status_str:<9}] {b.get('description', '')}")
                print(f"    Included: {', '.join(b.get('modules', []))}")

        # 3. Subsystem Plugins (Analytical Modules)
        if plugins:
            print(f"\n3. Subsystem Plugins ({len(plugins)} modules):")
            w_name = 20
            w_domain = 14
            w_subsys = 16
            w_trust = 12
            w_status = 20

            top = "  ┌" + "─" * (w_name + 2) + "┬" + "─" * (w_domain + 2) + "┬" + "─" * (w_subsys + 2) + "┬" + "─" * (w_trust + 2) + "┬" + "─" * (w_status + 2) + "┐"
            hdr = f"  │ {'Plugin Name':<{w_name}} │ {'Domain':<{w_domain}} │ {'Subsystem':<{w_subsys}} │ {'Trust':<{w_trust}} │ {'Version / Status':<{w_status}} │"
            div = "  ├" + "─" * (w_name + 2) + "┼" + "─" * (w_domain + 2) + "┼" + "─" * (w_subsys + 2) + "┼" + "─" * (w_trust + 2) + "┼" + "─" * (w_status + 2) + "┤"
            bot = "  └" + "─" * (w_name + 2) + "┴" + "─" * (w_domain + 2) + "┴" + "─" * (w_subsys + 2) + "┴" + "─" * (w_trust + 2) + "┴" + "─" * (w_status + 2) + "┘"

            print(top)
            print(hdr)
            print(div)
            for m in plugins:
                status_str = f"Installed v{m['installed_version']}" if m.get("is_installed") else f"v{m['version']} Ready"
                badge = format_trust_badge(m.get("trust_level", "community"))
                mod_name = m['name'][:w_name]
                mod_domain = m.get('domain', 'general')[:w_domain]
                mod_subsys = m.get('subsystem', '')[:w_subsys]
                print(f"  │ {mod_name:<{w_name}} │ {mod_domain:<{w_domain}} │ {mod_subsys:<{w_subsys}} │ {badge:<{w_trust}} │ {status_str:<{w_status}} │")
            print(bot)

        print(f"\nInstall with: {get_cli_prog()} module install <name>\n")

    elif parsed.subcommand == "info":
        info = client.info(parsed.name)
        if not info:
            print(f"Error: Nothing found for '{parsed.name}' in registry.", file=sys.stderr)
            sys.exit(1)

        if parsed.json:
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return

        print("\nMarketplace Item Details")
        print("═" * 80)
        item_type = info.get("type", "module").upper()
        name = info.get("name", "")
        print(f"Type:        {item_type}")
        print(f"Name:        {name}")
        print(f"Description: {info.get('description', '')}")
        print(f"Domain:      {info.get('domain', 'general')}")

        if item_type == "MODULE":
            badge = format_trust_badge(info.get("trust_level", "community"))
            print(f"Subsystem:   {info.get('subsystem', '')}")
            print(f"Version:     {info.get('version', '')} (min app version: >={info.get('min_app_version', '1.0.0')})")
            print(f"Author:      {info.get('author', '')}")
            print(f"Trust Level: {badge}")
            print(f"Tags:        {', '.join(info.get('tags', []))}")
            if info.get("dependencies"):
                print(f"Dependencies:{', '.join(info['dependencies'])}")
            if info.get("env_keys"):
                print(f"API Keys:    {', '.join(info['env_keys'])}")
            print(f"Status:      {'Installed' if info.get('is_installed') else 'Not Installed'}")
        else:
            print(f"Tags:        {', '.join(info.get('tags', []))}")
            print("Bundled Modules:")
            for mod, st in info.get("modules_status", {}).items():
                st_str = f"Installed (v{st['version']})" if st["installed"] else "Available"
                print(f"  • {mod:<20} [{st_str}]")
        print()

    elif parsed.subcommand == "install":
        _handle_install(client, parsed.target, force=parsed.force)

    elif parsed.subcommand == "install-bundle":
        bundle_target = parsed.target if parsed.target.startswith("bundle:") else f"bundle:{parsed.target}"
        _handle_install(client, bundle_target, force=parsed.force)

    elif parsed.subcommand == "update":
        results = client.update(name=parsed.name, update_all=parsed.all)
        print("\nModule Update Summary:")
        for mod, ok, msg in results:
            icon = "[OK]" if ok else "[FAIL]"
            print(f"  {icon} {mod}: {msg}")

    elif parsed.subcommand in ("remove", "uninstall"):
        ok, msg = client.remove(parsed.name, force=parsed.force)
        if ok:
            print(f"[OK] {msg}")
        else:
            print(f"[FAIL] {msg}", file=sys.stderr)
            sys.exit(1)

    elif parsed.subcommand == "list":
        installed = client.list_installed()
        if parsed.json:
            print(json.dumps(installed, indent=2, ensure_ascii=False))
            return

        print(f"\nInstalled Modules ({len(installed)} active)")
        print("═" * 84)
        if not installed:
            print(f"No marketplace modules currently installed. Use '{get_cli_prog()} module search' to browse available packages.")
            return

        w_name = 20
        w_domain = 14
        w_subsys = 16
        w_ver = 10
        w_path = 30

        top = "  ┌" + "─" * (w_name + 2) + "┬" + "─" * (w_domain + 2) + "┬" + "─" * (w_subsys + 2) + "┬" + "─" * (w_ver + 2) + "┬" + "─" * (w_path + 2) + "┐"
        hdr = f"  │ {'Module Name':<{w_name}} │ {'Domain':<{w_domain}} │ {'Subsystem':<{w_subsys}} │ {'Version':<{w_ver}} │ {'Path':<{w_path}} │"
        div = "  ├" + "─" * (w_name + 2) + "┼" + "─" * (w_domain + 2) + "┼" + "─" * (w_subsys + 2) + "┼" + "─" * (w_ver + 2) + "┼" + "─" * (w_path + 2) + "┤"
        bot = "  └" + "─" * (w_name + 2) + "┴" + "─" * (w_domain + 2) + "┴" + "─" * (w_subsys + 2) + "┴" + "─" * (w_ver + 2) + "┴" + "─" * (w_path + 2) + "┘"

        print(top)
        print(hdr)
        print(div)
        for m in sorted(installed, key=lambda x: x["name"]):
            mod_name = m['name'][:w_name]
            mod_domain = m.get('domain', 'general')[:w_domain]
            mod_subsys = m.get('subsystem', '')[:w_subsys]
            mod_ver = m.get('version', '')[:w_ver]
            mod_path = m.get('path', '')[:w_path]
            print(f"  │ {mod_name:<{w_name}} │ {mod_domain:<{w_domain}} │ {mod_subsys:<{w_subsys}} │ {mod_ver:<{w_ver}} │ {mod_path:<{w_path}} │")
        print(bot + "\n")

    elif parsed.subcommand == "config":
        cfg = client.config(parsed.name)
        if parsed.json:
            print(json.dumps(cfg, indent=2, ensure_ascii=False))
            return

        print(f"\nConfiguration & API Keys for '{cfg['name']}':")
        if not cfg["required_keys"]:
            print("  This module does not require any special API keys or environment variables.")
            return

        for k, v in cfg["status"].items():
            status_badge = "CONFIGURED" if v["configured"] else "MISSING"
            print(f"  • {k:<25} [{status_badge}] (Value preview: {v['preview']})")

        if not cfg["all_configured"]:
            print("\n  To set missing keys, define them in your environment or .env file:")
            for k, v in cfg["status"].items():
                if not v["configured"]:
                    print(f"    export {k}=\"your_api_key_here\"")
        print()

    elif parsed.subcommand in ("new", "create"):
        ok, msg, path = client.scaffold_new(
            module_type=parsed.type,
            name=parsed.name,
            domain=parsed.domain,
            dest_dir=parsed.dest,
        )
        if parsed.json:
            print(json.dumps({
                "success": ok,
                "message": msg,
                "path": str(path) if path else None,
            }, indent=2))
            return

        if ok:
            print(f"\n[OK] {msg}")
            print("\nNext steps:")
            print(f"  1. Edit your new module at:\n     {path}")
            print(f"  2. Validate security & contract compliance:\n     {get_cli_prog()} module verify {path}")
            print("  3. When ready, submit a Pull Request to:\n     https://github.com/knapset-dev/knapset-registry\n")
        else:
            print(f"\n[FAIL] Failed to scaffold module:\n{msg}\n", file=sys.stderr)
            sys.exit(1)

    elif parsed.subcommand == "verify":
        results = client.verify(parsed.name)
        if parsed.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
            return

        print(f"\nModule Security & Contract Audit ({len(results)} inspected)")
        print("═" * 84)
        if not results:
            print("No modules or paths found to verify.")
            return

        all_healthy = True
        for r in results:
            badge = "HEALTHY" if r["status"] == "HEALTHY" else r["status"]
            if r["status"] != "HEALTHY":
                all_healthy = False
            sha_preview = f"`{r.get('current_sha', '')[:12]}...`" if r.get("current_sha") else "N/A"
            path_str = f" ({r['path']})" if r.get("path") else ""
            print(f"• {r['name']:<20} [{badge:<12}] (SHA: {sha_preview}){path_str}")
            if r.get("violations"):
                for v in r["violations"]:
                    print(f"    [VIOLATION] {v}")
            if r.get("warnings"):
                for w in r["warnings"]:
                    print(f"    [WARN] {w}")

        print()
        if all_healthy:
            print("[PASS] All inspected modules/files verified: checksums match and zero security violations.")
        else:
            print("[FAIL] Integrity/Security issues detected!", file=sys.stderr)
            sys.exit(1)
