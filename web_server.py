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

from app_paths import get_cli_prog
from enricher import MatchSpec, enrich_rows, parse_match_expr
from preset_loader import PresetConfig, discover_presets, resolve_preset, save_preset
from solver_engine import solve_basket, solve_rank, solve_sets
from sources.registry import inspect_source, open_source

APP_VERSION = "2.0.0"

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Knapset v2 — Database Preference & Constraint Optimizer</title>
  <style>
    :root {
      --bg: #0b0f19; --panel: #111827; --panel-2: #1f2937;
      --border: #374151; --text: #f3f4f6; --muted: #9ca3af;
      --accent: #38bdf8; --green: #34d399; --red: #f87171;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.45; }
    header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; justify-content: space-between; align-items: center; }
    header h1 { font-size: 1.15rem; color: var(--accent); }
    main { max-width: 1220px; margin: 20px auto; padding: 0 18px; display: grid; grid-template-columns: 380px 1fr; gap: 18px; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
    h2 { font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 10px; }
    label { display: block; font-size: 0.8rem; color: var(--muted); margin: 8px 0 4px; }
    input, select, textarea { width: 100%; background: var(--panel-2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; font-family: inherit; font-size: 0.88rem; }
    textarea { font-family: monospace; min-height: 62px; }
    .row { display: flex; gap: 8px; align-items: center; }
    button { background: var(--accent); color: #041019; font-weight: 600; border: none; border-radius: 6px; padding: 8px 13px; cursor: pointer; font-size: 0.85rem; }
    button.secondary { background: var(--panel-2); color: var(--text); border: 1px solid var(--border); }
    .weight-row { display: grid; grid-template-columns: 1fr 78px auto; gap: 8px; align-items: center; margin: 6px 0; }
    .result-card { background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
    .score-pill { display: inline-block; background: rgba(52, 211, 153, 0.16); color: var(--green); border: 1px solid rgba(52, 211, 153, 0.4); border-radius: 999px; padding: 2px 10px; font-weight: 700; font-size: 0.82rem; }
    .err { color: var(--red); margin-top: 8px; font-size: 0.85rem; }
    pre { font-size: 0.78rem; color: var(--muted); white-space: pre-wrap; margin-top: 6px; }
  </style>
</head>
<body>
<header>
  <h1>Knapset v2 Studio</h1>
  <span style="color:var(--muted);font-size:0.82rem;">Zero-dependency DB Preference & Constraint Optimizer</span>
</header>
<main>
  <section class="card">
    <h2>Configuration</h2>
    <label>Load Installed Preset</label>
    <div class="row">
      <select id="presetSelect"><option value="">-- Custom / Source Path --</option></select>
      <button class="secondary" onclick="loadSelectedPreset()">Load</button>
    </div>
    <label>Source File Path (.db / .csv / .json)</label>
    <div class="row">
      <input id="sourcePath" placeholder="/path/to/data.db or items.csv" />
      <button class="secondary" onclick="inspectSourcePath()">Inspect</button>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>Mode</label>
        <select id="modeSelect">
          <option value="solve">solve (multi-slot set)</option>
          <option value="basket">basket (knapsack)</option>
          <option value="rank">rank (single table)</option>
        </select>
      </div>
      <div style="width:90px">
        <label>Top N</label>
        <input id="topN" type="number" value="5" min="1" max="50" />
      </div>
    </div>
    <label>Signed Weights (expr:+w, expr:-w)</label>
    <textarea id="weightsBox" placeholder="score:+0.6, price:-0.4"></textarea>
    <label>Formulas (one per line: name = expr)</label>
    <textarea id="formulasBox" placeholder="deal_pct = (ref.msrp - price) / ref.msrp * 100"></textarea>
    <label>Limits (one per line: sum(col) &lt;= max)</label>
    <textarea id="limitsBox" placeholder="sum(price) <= 950"></textarea>
    <label>Rules / Where Filters (one per line)</label>
    <textarea id="rulesBox" placeholder="cpu.socket == mobo.socket"></textarea>
    <div class="row" style="margin-top:12px">
      <button style="flex:1" onclick="runSolve()">Optimize Now</button>
      <button class="secondary" onclick="downloadJson()">Export JSON</button>
    </div>
    <div id="statusMsg" class="err" style="color:var(--muted)"></div>
  </section>
  <section class="card">
    <h2>Results & Schema Inspector</h2>
    <div id="schemaPanel" style="margin-bottom:12px;color:var(--muted);font-size:0.84rem;"></div>
    <div id="resultsPanel"></div>
  </section>
</main>
<script>
let lastPayload = null;
async function api(url, body) {
  const opts = body ? { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) } : {};
  const r = await fetch(url, opts);
  return await r.json();
}
async function refreshPresets() {
  const data = await api('/api/presets');
  const sel = document.getElementById('presetSelect');
  (data.presets || []).forEach(p => {
    const o = document.createElement('option');
    o.value = p.name; o.textContent = `${p.name} (${p.mode}) - ${p.title}`;
    sel.appendChild(o);
  });
}
async function loadSelectedPreset() {
  const name = document.getElementById('presetSelect').value;
  if (!name) return;
  const res = await api('/api/solve', { preset: name });
  renderResponse(res);
  if (res.mode) document.getElementById('modeSelect').value = res.mode;
  if (res.weights) document.getElementById('weightsBox').value = Object.entries(res.weights).map(([k,v]) => `${k}:${v}`).join(', ');
  if (res.limits) document.getElementById('limitsBox').value = (res.limits || []).join('\n');
  if (res.rules) document.getElementById('rulesBox').value = (res.rules || []).join('\n');
  if (res.formulas) document.getElementById('formulasBox').value = (res.formulas || []).join('\n');
}
async function inspectSourcePath() {
  const src = document.getElementById('sourcePath').value.trim();
  if (!src) return;
  const data = await api('/api/inspect', { source: src });
  if (data.error) { document.getElementById('statusMsg').textContent = data.error; return; }
  const html = (data.schema.tables || []).map(t =>
    `<div><b>${t.name}</b> (${t.row_count} rows): ` +
    t.columns.map(c => `${c.name}<i>(${c.kind})</i>`).join(', ') + `</div>`
  ).join('');
  document.getElementById('schemaPanel').innerHTML = html;
}
async function runSolve() {
  const splitLines = id => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  const body = {
    preset: document.getElementById('presetSelect').value || undefined,
    source: document.getElementById('sourcePath').value.trim() || undefined,
    mode: document.getElementById('modeSelect').value,
    top_n: parseInt(document.getElementById('topN').value || '5', 10),
    weights: document.getElementById('weightsBox').value.trim(),
    formulas: splitLines('formulasBox'),
    limits: splitLines('limitsBox'),
    rules: splitLines('rulesBox'),
  };
  const res = await api('/api/solve', body);
  renderResponse(res);
}
function renderResponse(res) {
  lastPayload = res;
  const msg = document.getElementById('statusMsg');
  if (res.error) { msg.textContent = res.error; msg.style.color = 'var(--red)'; return; }
  msg.textContent = `Solved ${(res.results || []).length} result(s) in mode=${res.mode}.`;
  msg.style.color = 'var(--green)';
  const panel = document.getElementById('resultsPanel');
  panel.innerHTML = (res.results || []).map((r, i) => {
    const score = r.score ?? r.total_score ?? 0;
    return `<div class="result-card"><div class="row" style="justify-content:space-between"><b>#${i+1}</b><span class="score-pill">${score}</span></div><pre>${JSON.stringify(r, null, 2)}</pre></div>`;
  }).join('') || '<div>No feasible results found.</div>';
}
function downloadJson() {
  if (!lastPayload) return;
  const blob = new Blob([JSON.stringify(lastPayload, null, 2)], {type: 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'knapset_results.json'; a.click();
}
refreshPresets();
</script>
</body>
</html>
"""


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

    if preset_ref and not payload.get("source"):
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

    # Optional inline references enrichment
    for ref_cfg in payload.get("references") or []:
        r_rows = open_source(ref_cfg["source"]).load_rows(table=ref_cfg.get("table"))
        lf, rf, inferred = parse_match_expr(ref_cfg["match"])
        spec = MatchSpec(
            left_field=lf,
            right_field=rf,
            strategy=ref_cfg.get("strategy") or inferred or "token",
            alias=ref_cfg.get("alias") or "ref",
            on_unmatched=ref_cfg.get("on_unmatched") or "drop",
        )
        target_slots = [ref_cfg["slot"]] if ref_cfg.get("slot") else list(slot_pools.keys())
        for sname in target_slots:
            if sname in slot_pools:
                slot_pools[sname] = enrich_rows(slot_pools[sname], r_rows, spec).rows

    eff_mode = mode or ("solve" if len(slot_pools) > 1 else "rank")
    if eff_mode == "rank":
        first_rows = next(iter(slot_pools.values()), [])
        ranked, meta = solve_rank(first_rows, weights=weights, formulas=formulas, where=where, top_n=top_n)
        return {"mode": "rank", "meta": meta, "results": [r.to_dict() for r in ranked]}
    if eff_mode == "basket":
        first_rows = next(iter(slot_pools.values()), [])
        sols, meta = solve_basket(
            first_rows,
            weights=weights,
            limits=limits,
            formulas=formulas,
            where=where,
            top_n=top_n,
            max_per_item=int(payload.get("max_per_item", 1)),
            qty_field=payload.get("qty_field"),
        )
        return {"mode": "basket", "meta": meta, "results": [s.to_dict() for s in sols]}
    sols, meta = solve_sets(
        slot_pools=slot_pools,
        weights=weights,
        limits=limits,
        rules=rules,
        formulas=formulas,
        where=where,
        top_n=top_n,
    )
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
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(DASHBOARD_HTML)
        elif self.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "version": APP_VERSION})
        elif self.path == "/api/presets":
            presets = discover_presets()
            self._send_json(
                HTTPStatus.OK,
                {
                    "presets": [
                        {
                            "name": cfg.name,
                            "title": cfg.title,
                            "mode": cfg.mode,
                            "profiles": cfg.profile_names(),
                            "path": str(cfg.source_path) if cfg.source_path else "",
                        }
                        for cfg in presets.values()
                    ]
                },
            )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {self.path}"})

    def do_POST(self) -> None:
        try:
            body = self._read_json_body()
            if self.path == "/api/inspect":
                schema = inspect_source(str(body["source"]), sample_rows=int(body.get("sample_rows", 200)))
                self._send_json(HTTPStatus.OK, {"schema": schema.to_dict()})
            elif self.path == "/api/solve":
                self._send_json(HTTPStatus.OK, _execute_solve_request(body))
            elif self.path == "/api/save-preset":
                out_path = save_preset(PresetConfig.from_dict(body["preset"]), body["path"], overwrite=True)
                self._send_json(HTTPStatus.OK, {"saved": str(out_path)})
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
