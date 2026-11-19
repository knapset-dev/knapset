#!/usr/bin/env python3
"""
Knapset v2 Built-in Web Dashboard & REST API Server (`ks serve`).

100% Python Standard Library (`http.server`, `json`, `urllib.parse`) generic interactive
optimizer studio & JSON API:
  - Source schema inspector & Reference DB (--ref) match coverage checker
  - Preset selector & live weight sliders (`+` / `-`)
  - Custom formula, filter, limit, and cross-slot rule editor
  - Multi-mode solver execution (`rank`, `basket`, `solve`) & CSV/JSON export
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app_paths import get_cli_prog
from enricher import collect_ref_specs, compare_sources, enrich_slot_pools
from formula_engine import parse_weights
from marketplace.client import MarketplaceClient
from preset_loader import (
    PresetConfig,
    delete_shared_profile,
    discover_presets,
    get_user_presets_dir,
    list_shared_profiles,
    resolve_prefs_for_pools,
    resolve_preset,
    save_preset,
    save_shared_profile,
)
from solver_engine import analyze_sensitivity, solve_basket, solve_rank, solve_sets
from sources.registry import inspect_source, open_source
from theme_config import (
    ThemeConfig,
    discover_themes,
    load_active_theme,
    reset_active_theme,
    set_active_theme,
)
from tui.state import discover_local_data_files

APP_VERSION = "2.1.0"

_ANSI_CSS_HEX: Dict[str, str] = {
    "black": "#111827",
    "red": "#f87171",
    "green": "#34d399",
    "yellow": "#fbbf24",
    "blue": "#60a5fa",
    "magenta": "#f472b6",
    "pink": "#f472b6",
    "cyan": "#38bdf8",
    "white": "#f3f4f6",
}


def _xterm_to_hex(idx: int) -> str:
    if idx < 0:
        return "#38bdf8"
    base16 = (
        "#111827", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
        "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
    )
    if idx < 16:
        return base16[idx]
    if idx < 232:
        n = idx - 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[n // 36], levels[(n % 36) // 6], levels[n % 6]
        return f"#{r:02x}{g:02x}{b:02x}"
    v = 8 + (idx - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _theme_color_to_css(val: Any, fallback: str) -> str:
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("#") and len(s) in (4, 7):
            return s
        if s.lower() in _ANSI_CSS_HEX:
            return _ANSI_CSS_HEX[s.lower()]
        if s.isdigit():
            val = int(s)
    if isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 255:
        return _xterm_to_hex(val)
    return fallback


def _theme_payload(theme: ThemeConfig) -> Dict[str, Any]:
    return {
        "name": theme.name,
        "title": theme.title,
        "description": theme.description,
        "colors": theme.colors,
        "css_vars": {
            "accent": _theme_color_to_css(theme.colors.get("accent"), "#38bdf8"),
            "green": _theme_color_to_css(theme.colors.get("positive"), "#34d399"),
            "red": _theme_color_to_css(theme.colors.get("minimize"), "#f87171"),
            "warn": _theme_color_to_css(theme.colors.get("warning"), "#fbbf24"),
            "muted": _theme_color_to_css(theme.colors.get("muted"), "#9ca3af"),
        },
    }


_STUDIO_HTML_PATH = Path(__file__).resolve().parent / "web_assets" / "studio.html"


def _load_dashboard_html() -> str:
    return _STUDIO_HTML_PATH.read_text(encoding="utf-8")


DASHBOARD_HTML = _load_dashboard_html()


def _execute_solve_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executes `/api/solve` cleanly for either a preset or direct DB/CSV/JSON source inputs."""
    preset_ref = payload.get("preset")
    mode = str(payload.get("mode") or "").lower().strip() or None
    weights = payload.get("weights") or ""
    formulas = payload.get("formulas") or []
    where = payload.get("where") or []
    limits = payload.get("limits") or []
    rules = payload.get("rules") or []
    top_n = int(payload.get("top_n", 10))

    if preset_ref and not payload.get("source") and not payload.get("slots"):
        cfg = resolve_preset(str(preset_ref))
        return cfg.execute(
            profile_name=payload.get("profile"),
            mode_override=mode,
            prefer_override=weights or None,
            limits_override=limits or None,
            where_override=where or None,
            formulas_override=formulas or None,
            rules_override=rules or None,
            top_n=top_n,
            norm_override=payload.get("norm"),
            diverse_override=payload.get("diverse") if "diverse" in payload else None,
            pin_override=payload.get("pin"),
            exclude_override=payload.get("exclude"),
        )

    source_target = payload.get("source")
    slots_payload = payload.get("slots")
    slot_pools: Dict[str, List[Dict[str, Any]]] = {}
    if isinstance(slots_payload, dict) and slots_payload:
        for sname, scfg in slots_payload.items():
            if isinstance(scfg, list):
                slot_pools[str(sname)] = list(scfg)
            elif isinstance(scfg, dict) and scfg.get("source"):
                slot_pools[str(sname)] = open_source(scfg["source"]).load_rows(
                    table=scfg.get("table"),
                    query=scfg.get("query"),
                )
    elif source_target:
        src = open_source(str(source_target))
        schema = src.inspect()
        requested_table = payload.get("table")
        if requested_table:
            slot_pools[str(requested_table)] = src.load_rows(table=str(requested_table), query=payload.get("query"))
        else:
            for tname in schema.table_names():
                slot_pools[tname] = src.load_rows(table=tname)
    else:
        raise ValueError("Specify 'preset', 'source', or 'slots'.")

    ref_specs = collect_ref_specs(payload)
    if ref_specs:
        enrich_slot_pools(slot_pools, ref_specs)

    merged, skipped = resolve_prefs_for_pools(payload, slot_pools)
    eff_mode = str(merged.get("mode") or mode or ("solve" if len(slot_pools) > 1 else "rank")).lower().strip()
    eff_norm = str(merged.get("norm") or "robust")
    eff_diverse = bool(merged.get("diverse", False))
    eff_weights = merged["weights"] or ""
    eff_formulas = merged["formulas"]
    eff_where = merged["where"]
    eff_limits = merged["limits"]
    eff_rules = merged["rules"]

    if eff_mode == "rank":
        first_rows = next(iter(slot_pools.values()), [])
        ranked, meta = solve_rank(
            first_rows,
            weights=eff_weights,
            formulas=eff_formulas,
            where=eff_where,
            top_n=top_n,
            norm=eff_norm,
            exclude=payload.get("exclude"),
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"mode": "rank", "meta": meta, "results": [r.to_dict() for r in ranked]}
    if eff_mode == "basket":
        first_rows = next(iter(slot_pools.values()), [])
        pin_raw = payload.get("pin")
        pin_list = list(pin_raw.values()) if isinstance(pin_raw, dict) else (list(pin_raw) if pin_raw else None)
        sols, meta = solve_basket(
            first_rows,
            weights=eff_weights,
            limits=eff_limits,
            formulas=eff_formulas,
            where=eff_where,
            top_n=top_n,
            max_per_item=int(payload.get("max_per_item", 1)),
            qty_field=payload.get("qty_field"),
            norm=eff_norm,
            pin=pin_list,
            exclude=payload.get("exclude"),
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"mode": "basket", "meta": meta, "results": [s.to_dict() for s in sols]}
    sols, meta = solve_sets(
        slot_pools=slot_pools,
        weights=eff_weights,
        limits=eff_limits,
        rules=eff_rules,
        formulas=eff_formulas,
        where=eff_where,
        top_n=top_n,
        norm=eff_norm,
        diverse=eff_diverse,
        pin=payload.get("pin"),
        exclude=payload.get("exclude"),
    )
    if skipped:
        meta["skipped_prefs_terms"] = skipped
    return {"mode": "solve", "meta": meta, "results": [s.to_dict() for s in sols]}


class KnapsetV2WebHandler(BaseHTTPRequestHandler):
    """HTTP handler for the zero-dependency Knapset v2 Web Studio and REST API."""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if route == "/":
                self._send_html(_load_dashboard_html())
            elif route == "/api/health":
                self._send_json(HTTPStatus.OK, {"status": "ok", "version": APP_VERSION})
            elif route == "/api/files":
                base = Path(qs.get("dir", [str(Path.cwd())])[0]).expanduser()
                files = discover_local_data_files(base if base.is_dir() else Path.cwd())
                self._send_json(
                    HTTPStatus.OK,
                    {"cwd": str(Path.cwd()), "files": [str(f) for f in files]},
                )
            elif route == "/api/presets":
                presets = discover_presets()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "presets": [
                            {
                                "name": cfg.name,
                                "title": cfg.title,
                                "description": cfg.description,
                                "mode": cfg.mode,
                                "profiles": cfg.profile_names(),
                                "path": str(cfg.source_path) if cfg.source_path else "",
                            }
                            for cfg in presets.values()
                        ]
                    },
                )
            elif route == "/api/prefs":
                self._send_json(HTTPStatus.OK, {"profiles": list_shared_profiles()})
            elif route == "/api/themes":
                active = load_active_theme()
                themes = discover_themes()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "active": active.name,
                        "themes": [
                            {**_theme_payload(t), "active": t.name == active.name}
                            for t in themes.values()
                        ],
                    },
                )
            elif route == "/api/registry":
                client = MarketplaceClient()
                q = qs.get("q", [""])[0]
                item_type = qs.get("type", [None])[0]
                refresh = qs.get("refresh", [""])[0] in ("1", "true", "yes")
                self._send_json(HTTPStatus.OK, client.search(query=q, subsystem=item_type, force_refresh=refresh))
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {self.path}"})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            body = self._read_json_body()
            if self.path == "/api/inspect":
                schema = inspect_source(str(body["source"]), sample_rows=int(body.get("sample_rows", 200)))
                self._send_json(HTTPStatus.OK, {"schema": schema.to_dict()})
            elif self.path == "/api/compare":
                res = compare_sources(
                    str(body["source"]),
                    str(body["ref_source"]),
                    table=body.get("table"),
                    query=body.get("query"),
                    ref_table=body.get("ref_table"),
                    ref_query=body.get("ref_query"),
                    match=body.get("match"),
                    strategy=body.get("strategy"),
                    alias=str(body.get("alias") or "ref"),
                    on_unmatched=str(body.get("on_unmatched") or "keep"),
                    limit=int(body.get("limit", 15)),
                )
                self._send_json(HTTPStatus.OK, res)
            elif self.path == "/api/solve":
                self._send_json(HTTPStatus.OK, _execute_solve_request(body))
            elif self.path == "/api/sensitivity":
                if body.get("preset") and not body.get("source"):
                    cfg = resolve_preset(str(body["preset"]))
                    rep = cfg.sensitivity(
                        profile_name=body.get("profile"),
                        mode_override=body.get("mode") or None,
                        prefer_override=body.get("weights") or None,
                        limits_override=body.get("limits") or None,
                        where_override=body.get("where") or None,
                        formulas_override=body.get("formulas") or None,
                        rules_override=body.get("rules") or None,
                        norm_override=body.get("norm"),
                        weight_delta=float(body.get("weight_delta", 0.15)),
                        limit_delta=float(body.get("limit_delta", 0.10)),
                        pin_override=body.get("pin"),
                        exclude_override=body.get("exclude"),
                    )
                else:
                    src = open_source(str(body["source"]))
                    schema = src.inspect()
                    req_table = body.get("table")
                    eff_mode = str(body.get("mode") or ("solve" if len(schema.table_names()) > 1 and not req_table else "rank")).lower()
                    if eff_mode == "solve":
                        pools = {t: src.load_rows(table=t) for t in schema.table_names()}
                        ref_specs = collect_ref_specs(body)
                        if ref_specs:
                            enrich_slot_pools(pools, ref_specs)
                        rep = analyze_sensitivity(
                            "solve",
                            slot_pools=pools,
                            weights=body.get("weights") or "",
                            limits=body.get("limits"),
                            rules=body.get("rules"),
                            formulas=body.get("formulas"),
                            where=body.get("where"),
                            norm=body.get("norm") or "robust",
                            weight_delta=float(body.get("weight_delta", 0.15)),
                            limit_delta=float(body.get("limit_delta", 0.10)),
                            pin=body.get("pin"),
                            exclude=body.get("exclude"),
                        )
                    else:
                        rows = src.load_rows(table=req_table, query=body.get("query"))
                        pools = {"_pool": rows}
                        ref_specs = collect_ref_specs(body)
                        if ref_specs:
                            enrich_slot_pools(pools, ref_specs)
                        rep = analyze_sensitivity(
                            eff_mode,
                            rows=pools["_pool"],
                            weights=body.get("weights") or "",
                            limits=body.get("limits"),
                            formulas=body.get("formulas"),
                            where=body.get("where"),
                            norm=body.get("norm") or "robust",
                            weight_delta=float(body.get("weight_delta", 0.15)),
                            limit_delta=float(body.get("limit_delta", 0.10)),
                            pin=body.get("pin"),
                            exclude=body.get("exclude"),
                        )
                self._send_json(HTTPStatus.OK, rep)
            elif self.path == "/api/save-preset":
                cfg = PresetConfig.from_dict(body["preset"])
                raw_path = body.get("path")
                target_path = Path(raw_path) if raw_path else (get_user_presets_dir() / f"{cfg.name}.json")
                out_path = save_preset(cfg, target_path, overwrite=True)
                self._send_json(HTTPStatus.OK, {"saved": str(out_path), "name": cfg.name})
            elif self.path == "/api/prefs/save":
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("'name' is required.")
                weights_raw = body.get("weights") or ""
                if isinstance(weights_raw, dict):
                    weights = {str(k): float(v) for k, v in weights_raw.items()}
                else:
                    weights = {t.expression: t.weight for t in parse_weights(weights_raw)} if weights_raw else {}
                data = {
                    "mode": body.get("mode") or "rank",
                    "norm": body.get("norm") or "robust",
                    "diverse": bool(body.get("diverse", False)),
                    "weights": weights,
                    "formulas": list(body.get("formulas") or []),
                    "where": list(body.get("where") or []),
                    "limits": list(body.get("limits") or []),
                    "rules": list(body.get("rules") or []),
                }
                saved_path = save_shared_profile(name, data)
                self._send_json(HTTPStatus.OK, {"saved": name.lower(), "path": str(saved_path), "profile": data})
            elif self.path == "/api/prefs/delete":
                name = str(body.get("name") or "").strip()
                removed = delete_shared_profile(name)
                self._send_json(HTTPStatus.OK, {"removed": name.lower(), "path": str(removed)})
            elif self.path == "/api/themes/use":
                target = str(body.get("name") or "").strip()
                theme, _ = reset_active_theme() if target == "reset" else set_active_theme(target)
                self._send_json(HTTPStatus.OK, {"active": theme.name, "theme": _theme_payload(theme)})
            elif self.path == "/api/registry/install":
                target = str(body.get("target") or "").strip()
                if not target:
                    raise ValueError("'target' is required.")
                client = MarketplaceClient()
                ok, msg = client.install(target, force=bool(body.get("force", False)))
                out: Dict[str, Any] = {"ok": ok, "message": msg}
                if ok and body.get("use"):
                    clean = target.split(":", 1)[-1]
                    if clean in discover_themes():
                        activated, _ = set_active_theme(clean)
                        out["active_theme"] = activated.name
                self._send_json(HTTPStatus.OK, out)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {self.path}"})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def create_web_server(host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), KnapsetV2WebHandler)


def run_web_server(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(
        prog=f"{get_cli_prog()} serve",
        description="Start the Knapset v2 zero-dependency Web Studio & REST API server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port number (default: 8080)")
    parser.add_argument("--open", action="store_true", help="Open Web Studio in default browser")
    args = parser.parse_args(argv)

    server = create_web_server(host=args.host, port=args.port)
    url = f"http://{args.host}:{server.server_port}"
    print(f"Knapset v2 Web Studio listening at {url} (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Web Studio.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_web_server(sys.argv[1:])
