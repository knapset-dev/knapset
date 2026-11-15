"""
Template Domain CLI.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from currency import canonical_currency, convert_currency, get_active_currency
from extensions.template.layout import create_template_layout
from extensions.template.search import template_search_instruction
from extensions.template.sorter import template_sorter
from extensions.template.weights import create_template_scorer, create_template_weights_registry
from set_synthesizer import export_domain_sets, print_domain_sets


def print_help() -> None:
    print("\nTemplate Domain Extension CLI\n")
    print("Usage: python main.py template <command> [options]\n")
    print("Commands:")
    print("  build    Synthesize optimal sets under budget (--budget, --mode, --min-new-ratio, --export)")
    print("  search   Search template candidates (--section, --query, --min-price, --max-price, --mode)")
    print("  core     Search core units")
    print("  addon    Search addon modules")
    print("  power    Search power supply units")
    print("  modes    List available optimization modes\n")


def _run_search_section(section_id: str, argv: List[str]) -> None:
    parser = argparse.ArgumentParser(prog=f"main.py template {section_id}")
    parser.add_argument("--query", "-q", type=str, default=None)
    parser.add_argument("--min-price", type=float, default=None)
    parser.add_argument("--max-price", type=float, default=None)
    parser.add_argument("--mode", "-M", type=str, default=None)
    parser.add_argument("--limit", "-n", type=int, default=15)
    args = parser.parse_args(argv)

    from extensions.template import TemplateDomainExtension
    ext = TemplateDomainExtension()
    res = ext.search(
        section_id=section_id,
        query=args.query,
        min_price=args.min_price,
        max_price=args.max_price,
        mode=args.mode,
        limit=args.limit,
    )
    ext.print_search_results(res, section_id=section_id, mode=args.mode)


def _run_modes(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(prog="main.py template modes")
    parser.add_argument("--inspect", "-i", type=str, default=None, help="Inspect weights of a specific mode")
    args = parser.parse_args(argv)

    w_reg = create_template_weights_registry()
    print("\nOptimization Modes for Template Domain")
    print("─" * 74)
    for prof in w_reg.list_profiles().values():
        top_w = ", ".join(f"{k}:{v:.2f}" for k, v in prof.weights.items())
        print(f"  {prof.name:<16} │ {prof.title:<24} │ {top_w}")
    print("─" * 74 + "\n")

    if args.inspect:
        prof = w_reg.resolve_profile(args.inspect)
        print(f"Mode Details: {prof.name} ({prof.title})")
        print(f"   Description      : {prof.description}")
        print(f"   Normalized Weights: {prof.weights}\n")


def _run_build(argv: List[str]) -> None:
    active_curr = get_active_currency()
    def_budget = 250.0 if canonical_currency(active_curr) in ("USDT", "USD") else convert_currency(250.0, "USDT", active_curr)
    parser = argparse.ArgumentParser(prog="main.py template build", description="Synthesize template domain sets")
    parser.add_argument("--budget", "-b", type=float, default=def_budget)
    parser.add_argument("--mode", "-M", type=str, default="balanced")
    parser.add_argument("--top", "-n", type=int, default=3)
    parser.add_argument("--weights", "-w", type=str, default=None)
    parser.add_argument("--min-new-ratio", type=float, default=0.0, help="Minimum fraction of new items (0.0..1.0)")
    parser.add_argument("--export", type=str, default=None, help="Export sets to .md or .json file")
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    layout = create_template_layout()
    w_reg = create_template_weights_registry()
    scorer = create_template_scorer()
    prof = w_reg.resolve_profile(args.mode, weights_override=args.weights)
    cands = template_search_instruction.instruct_load_all(budget=args.budget)
    sets = template_sorter.sort_and_synthesize(
        layout=layout,
        scorer=scorer,
        profile=prof,
        candidate_pool=cands,
        budget=args.budget,
        top_n=args.top,
        min_new_ratio=args.min_new_ratio,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    print_domain_sets(layout, prof, sets, elapsed_ms=elapsed_ms)
    if args.export:
        out_p = export_domain_sets(args.export, layout, prof, sets)
        print(f"   Exported {len(sets)} set(s) to {out_p}\n")


def main(argv: Optional[List[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return

    cmd = args[0].lower()
    extra = args[1:]
    if cmd == "build":
        _run_build(extra)
    elif cmd in ("search", "find"):
        from extensions.template import TemplateDomainExtension
        TemplateDomainExtension().run_cli_search(extra)
    elif cmd in ("core", "addon", "power"):
        _run_search_section(cmd, extra)
    elif cmd in ("modes", "mods"):
        _run_modes(extra)
    else:
        print(f"Error: Unknown template command '{cmd}'\n", file=sys.stderr)
        print_help()


if __name__ == "__main__":
    main()
