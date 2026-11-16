#!/usr/bin/env python3
"""
Knapset Built-in Web Dashboard & REST API Server.

100% Python Standard Library (`http.server`, `urllib.parse`, `json`, `threading`)
zero-dependency interactive web interface and JSON API for Knapset:
  - Interactive Combinatorial Set & Build Synthesizer across all installed domains
  - Section Search & Filtering with live currency switching (USDT, USD, UAH, EUR, PLN, GBP, SEK)
  - Prebuilt PC Valuation & Head-to-Head Duel Comparator
  - SQLite + FTS5 MarketStore Price Drops & Historical Trend Explorer

Usage:
  python3 main.py serve --port 8080
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.parse
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from currency import (
    DEFAULT_EXCHANGE_RATES,
    canonical_currency,
    convert_currency,
    format_price,
    get_active_currency,
    set_active_currency,
)
from extension_registry import get_extension, list_extensions, rescan_extensions
from market_store import market_store

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Knapset — Universal Market Intelligence & Combinatorial Synthesizer</title>
  <style>
    :root {
      --bg: #0b0f19;
      --panel: #111827;
      --panel-alt: #1f2937;
      --border: #374151;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --green: #34d399;
      --amber: #fbbf24;
      --red: #f87171;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, monospace;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 48px;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand h1 {
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--accent);
    }
    .badge {
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.35);
      border-radius: 6px;
      padding: 2px 8px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-green {
      background: rgba(52, 211, 153, 0.15);
      color: var(--green);
      border: 1px solid rgba(52, 211, 153, 0.35);
    }
    .badge-amber {
      background: rgba(251, 191, 36, 0.15);
      color: var(--amber);
      border: 1px solid rgba(251, 191, 36, 0.35);
    }
    .top-controls {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    select, input, textarea, button {
      font: inherit;
      background: var(--panel-alt);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.9rem;
    }
    select:focus, input:focus, textarea:focus {
      outline: none;
      border-color: var(--accent);
    }
    button {
      background: var(--accent);
      color: #0b0f19;
      font-weight: 600;
      border: none;
      cursor: pointer;
      transition: background 0.15s;
    }
    button:hover { background: var(--accent-hover); color: #fff; }
    .container {
      max-width: 1260px;
      margin: 24px auto;
      padding: 0 24px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      flex-wrap: wrap;
    }
    .tab-btn {
      background: var(--panel);
      color: var(--muted);
      border: 1px solid var(--border);
      padding: 8px 16px;
    }
    .tab-btn.active {
      background: var(--accent);
      color: #0b0f19;
      border-color: var(--accent);
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .form-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
      align-items: end;
    }
    .field label {
      display: block;
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 5px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .field input, .field select, .field textarea { width: 100%; }
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 16px;
      margin-top: 16px;
    }
    .build-card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px;
    }
    .build-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
      margin-bottom: 12px;
    }
    .build-title { font-weight: 700; font-size: 1.05rem; color: var(--accent); }
    .part-row {
      display: flex;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px dashed rgba(255,255,255,0.07);
      font-size: 0.88rem;
      gap: 8px;
    }
    .part-sec {
      font-weight: 700;
      color: var(--muted);
      min-width: 58px;
      text-transform: uppercase;
    }
    .part-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .part-name a { color: var(--text); text-decoration: none; }
    .part-name a:hover { color: var(--accent); text-decoration: underline; }
    .part-price { font-weight: 600; color: var(--green); white-space: nowrap; }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 14px;
      font-size: 0.88rem;
    }
    th, td {
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
    }
    th { color: var(--muted); font-size: 0.76rem; text-transform: uppercase; }
    tr:hover td { background: rgba(255,255,255,0.02); }
    .hidden { display: none; }
    .status-line { color: var(--muted); font-size: 0.85rem; margin-top: 8px; }
    pre {
      background: #070a12;
      padding: 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
      overflow-x: auto;
      font-size: 0.84rem;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>Knapset</h1>
      <span class="badge">Python Stdlib</span>
      <span class="badge badge-green" id="store-badge">SQLite FTS5 Ready</span>
    </div>
    <div class="top-controls">
      <label for="global-currency" style="font-size:0.82rem;color:var(--muted);">Active Currency:</label>
      <select id="global-currency">
        <option value="USDT" selected>USDT (Default)</option>
        <option value="USD">USD ($)</option>
        <option value="UAH">UAH (₴)</option>
        <option value="EUR">EUR (€)</option>
        <option value="PLN">PLN (zł)</option>
        <option value="GBP">GBP (£)</option>
        <option value="SEK">SEK (kr)</option>
      </select>
    </div>
  </header>

  <div class="container">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('build')">Set & Build Synthesizer</button>
      <button class="tab-btn" onclick="switchTab('search')">Section Search</button>
      <button class="tab-btn" onclick="switchTab('deals')">Price Drops & History</button>
      <button class="tab-btn" onclick="switchTab('prebuilt')">Prebuilt PC Evaluator</button>
    </div>

    <!-- TAB 1: BUILD SYNTHESIZER -->
    <section id="tab-build">
      <div class="panel">
        <div class="form-row">
          <div class="field">
            <label>Domain Module</label>
            <select id="build-domain" onchange="onBuildDomainChange()"></select>
          </div>
          <div class="field">
            <label>Target Budget</label>
            <input type="number" id="build-budget" value="850" step="10" />
          </div>
          <div class="field">
            <label>Optimization Mode</label>
            <select id="build-mode"></select>
          </div>
          <div class="field">
            <label>Top N Sets</label>
            <input type="number" id="build-top" value="3" min="1" max="10" />
          </div>
          <div class="field">
            <button onclick="runBuild()">Synthesize Optimal Sets</button>
          </div>
        </div>
        <div class="status-line" id="build-status">Select a domain and budget to synthesize optimal combinations.</div>
      </div>
      <div class="cards-grid" id="build-results"></div>
    </section>

    <!-- TAB 2: SECTION SEARCH -->
    <section id="tab-search" class="hidden">
      <div class="panel">
        <div class="form-row">
          <div class="field">
            <label>Domain Module</label>
            <select id="search-domain" onchange="onSearchDomainChange()"></select>
          </div>
          <div class="field">
            <label>Section</label>
            <select id="search-section"></select>
          </div>
          <div class="field">
            <label>Keyword Query</label>
            <input type="text" id="search-query" placeholder="e.g. Model, brand, keyword..." />
          </div>
          <div class="field">
            <label>Max Price</label>
            <input type="number" id="search-max-price" placeholder="Optional" />
          </div>
          <div class="field">
            <button onclick="runSearch()">Search Section</button>
          </div>
        </div>
        <div class="status-line" id="search-status"></div>
        <div id="search-results"></div>
      </div>
    </section>

    <!-- TAB 3: PRICE DROPS & HISTORY -->
    <section id="tab-deals" class="hidden">
      <div class="panel">
        <div class="form-row">
          <div class="field">
            <label>Category Filter</label>
            <input type="text" id="deals-category" placeholder="All (or a section id)" />
          </div>
          <div class="field">
            <label>Min Drop %</label>
            <input type="number" id="deals-min-drop" value="3.0" step="0.5" />
          </div>
          <div class="field">
            <button onclick="runDeals()">Find Price Drops</button>
          </div>
        </div>
        <div class="status-line" id="deals-status"></div>
        <div id="deals-results"></div>
      </div>
    </section>

    <!-- TAB 4: PREBUILT EVALUATOR -->
    <section id="tab-prebuilt" class="hidden">
      <div class="panel">
        <div class="form-row">
          <div class="field" style="grid-column: span 2;">
            <label>Prebuilt PC Offer #1 (Specs & Price)</label>
            <input type="text" id="pre-a" value="Ryzen 5 5600, RTX 3060 12GB, B550, 16GB DDR4, 650W, 480 USDT" />
          </div>
          <div class="field" style="grid-column: span 2;">
            <label>Optional Offer #2 (For Head-to-Head Duel)</label>
            <input type="text" id="pre-b" placeholder="e.g. Core i5-12400F, RTX 4060 8GB, B760, 16GB, 550 USDT" />
          </div>
          <div class="field">
            <button onclick="runPrebuilt()">Evaluate / Compare</button>
          </div>
        </div>
        <div class="status-line" id="prebuilt-status"></div>
        <div id="prebuilt-results" style="margin-top:14px;"></div>
      </div>
    </section>
  </div>

  <script>
    let DOMAINS_META = [];

    function getCurr() {
      return document.getElementById('global-currency').value || 'USDT';
    }

    function switchTab(tab) {
      ['build', 'search', 'deals', 'prebuilt'].forEach((t, idx) => {
        document.getElementById('tab-' + t).classList.toggle('hidden', t !== tab);
        document.querySelectorAll('.tab-btn')[idx].classList.toggle('active', t === tab);
      });
      if (tab === 'deals') runDeals();
    }

    async function loadDomains() {
      const res = await fetch('/api/domains');
      const data = await res.json();
      DOMAINS_META = data.domains || [];
      const bDom = document.getElementById('build-domain');
      const sDom = document.getElementById('search-domain');
      bDom.innerHTML = '';
      sDom.innerHTML = '';
      DOMAINS_META.forEach(d => {
        bDom.innerHTML += `<option value="${d.domain_id}">${d.domain_id.toUpperCase()} — ${d.title}</option>`;
        sDom.innerHTML += `<option value="${d.domain_id}">${d.domain_id.toUpperCase()} — ${d.title}</option>`;
      });
      onBuildDomainChange();
      onSearchDomainChange();
    }

    function onBuildDomainChange() {
      const domId = document.getElementById('build-domain').value;
      const dom = DOMAINS_META.find(d => d.domain_id === domId);
      const modeSel = document.getElementById('build-mode');
      modeSel.innerHTML = '';
      if (!dom) return;
      (dom.modes || ['best']).forEach(m => {
        modeSel.innerHTML += `<option value="${m}">${m}</option>`;
      });
      if (dom.default_budget) {
        document.getElementById('build-budget').value = dom.default_budget;
      }
    }

    function onSearchDomainChange() {
      const domId = document.getElementById('search-domain').value;
      const dom = DOMAINS_META.find(d => d.domain_id === domId);
      const secSel = document.getElementById('search-section');
      secSel.innerHTML = '';
      if (!dom) return;
      (dom.sections || []).forEach(s => {
        secSel.innerHTML += `<option value="${s.id}">${s.label} — ${s.name}</option>`;
      });
    }

    async function runBuild() {
      const domain = document.getElementById('build-domain').value;
      const budget = document.getElementById('build-budget').value;
      const mode = document.getElementById('build-mode').value;
      const top_n = document.getElementById('build-top').value;
      const currency = getCurr();
      const st = document.getElementById('build-status');
      const container = document.getElementById('build-results');
      st.textContent = `Synthesizing ${domain.toUpperCase()} sets under ${budget} ${currency}...`;
      container.innerHTML = '';

      const qs = new URLSearchParams({ domain, budget, mode, top_n, currency });
      const res = await fetch('/api/build?' + qs.toString());
      const data = await res.json();
      if (data.error) {
        st.textContent = 'Error: ' + data.error;
        return;
      }
      st.textContent = `Solved ${data.count} optimal set(s) in ${data.elapsed_ms} ms (Mode: ${data.mode} | Currency: ${data.currency}).`;
      window._lastBuildSets = data.sets || [];
      (data.sets || []).forEach((s, idx) => {
        let partsHtml = '';
        Object.entries(s.components || {}).forEach(([secId, comp]) => {
          const nameHtml = comp.url ? `<a href="${comp.url}" target="_blank" rel="noopener">${comp.model}</a>` : comp.model;
          partsHtml += `
            <div class="part-row">
              <span class="part-sec">${secId}</span>
              <span class="part-name">${nameHtml}</span>
              <span class="part-price">${comp.formatted_price}</span>
            </div>`;
        });
        container.innerHTML += `
          <div class="build-card">
            <div class="build-header">
              <span class="build-title">Set #${s.rank} — ${s.formatted_total}</span>
              <div style="display:flex;gap:8px;align-items:center;">
                <span class="badge badge-green">Score: ${s.score}</span>
                <button class="btn-secondary" style="padding:4px 10px;font-size:12px;" onclick="copySetMarkdown(${idx}, this)">Copy Markdown</button>
              </div>
            </div>
            ${partsHtml}
          </div>`;
      });
    }

    function copySetMarkdown(idx, btn) {
      const s = (window._lastBuildSets || [])[idx];
      if (!s || !s.markdown) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(s.markdown);
      }
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }

    async function runSearch() {
      const domain = document.getElementById('search-domain').value;
      const section = document.getElementById('search-section').value;
      const query = document.getElementById('search-query').value;
      const max_price = document.getElementById('search-max-price').value;
      const currency = getCurr();
      const st = document.getElementById('search-status');
      const container = document.getElementById('search-results');
      st.textContent = 'Searching...';

      const params = { domain, section, currency, limit: 20 };
      if (query) params.query = query;
      if (max_price) params.max_price = max_price;
      const res = await fetch('/api/search?' + new URLSearchParams(params).toString());
      const data = await res.json();
      const items = data.results || [];
      st.textContent = `Found ${items.length} candidate(s) in ${domain.toUpperCase()} / ${section.toUpperCase()}.`;
      if (!items.length) {
        container.innerHTML = '';
        return;
      }
      let rows = items.map(it => `
        <tr>
          <td><strong>${it.model}</strong></td>
          <td>${it.title}</td>
          <td style="color:var(--green);font-weight:600;">${it.formatted_price}</td>
          <td>${it.discount_pct ? (it.discount_pct > 0 ? '-' + it.discount_pct + '%' : '+' + Math.abs(it.discount_pct) + '%') : '—'}</td>
          <td><span class="badge">${it.score}</span></td>
          <td>${it.url ? `<a href="${it.url}" target="_blank" style="color:var(--accent);">Open ↗</a>` : '—'}</td>
        </tr>`).join('');
      container.innerHTML = `
        <table>
          <thead><tr><th>Model</th><th>Listing Title</th><th>Price</th><th>vs Median</th><th>Score</th><th>Link</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    }

    async function runDeals() {
      const category = document.getElementById('deals-category').value;
      const min_drop_pct = document.getElementById('deals-min-drop').value;
      const currency = getCurr();
      const st = document.getElementById('deals-status');
      const container = document.getElementById('deals-results');
      const params = { min_drop_pct, currency, limit: 25 };
      if (category) params.category = category;
      const res = await fetch('/api/deals?' + new URLSearchParams(params).toString());
      const data = await res.json();
      const stats = data.stats || {};
      document.getElementById('store-badge').textContent =
        `SQLite: ${(stats.total_listings || 0).toLocaleString()} listings`;
      const drops = data.drops || [];
      st.textContent = `Indexed: ${(stats.total_listings || 0).toLocaleString()} listings | Multi-point tracked: ${stats.multi_observation_listings || 0} | Price drops found: ${drops.length}`;
      if (!drops.length) {
        container.innerHTML = '<p style="color:var(--muted);margin-top:12px;">No verified seller price drops recorded yet. Run <code>python3 main.py sync pc</code> periodically to accumulate price history.</p>';
        return;
      }
      let rows = drops.map(d => `
        <tr>
          <td><span class="badge">${d.category.toUpperCase()}</span></td>
          <td>${d.title}</td>
          <td style="color:var(--muted);text-decoration:line-through;">${d.formatted_peak_price}</td>
          <td style="color:var(--green);font-weight:600;">${d.formatted_current_price}</td>
          <td><span class="badge badge-amber">-${d.drop_pct}%</span></td>
          <td>${d.observations}x</td>
        </tr>`).join('');
      container.innerHTML = `
        <table>
          <thead><tr><th>Cat</th><th>Title</th><th>Peak Price</th><th>Now</th><th>Drop</th><th>Snapshots</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    }

    async function runPrebuilt() {
      const listing_a = document.getElementById('pre-a').value;
      const listing_b = document.getElementById('pre-b').value;
      const currency = getCurr();
      const st = document.getElementById('prebuilt-status');
      const container = document.getElementById('prebuilt-results');
      st.textContent = 'Evaluating prebuilt PC offer...';
      const params = { listing_a, currency };
      if (listing_b) params.listing_b = listing_b;
      const res = await fetch('/api/prebuilt?' + new URLSearchParams(params).toString());
      const data = await res.json();
      st.textContent = 'Evaluation complete.';
      container.innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
    }

    loadDomains();
  </script>
</body>
</html>
"""


def dispatch_api_request(path: str, query_params: Dict[str, List[str]]) -> Tuple[int, str, Any]:
    """
    Pure function router for HTTP requests so unit tests can verify all endpoints
    directly without needing to bind a TCP socket if desired, while ThreadingHTTPServer
    delegates to this exact function.
    Returns (status_code, content_type, body_str_or_dict).
    """
    def _param(key: str, default: Optional[str] = None) -> Optional[str]:
        vals = query_params.get(key)
        return vals[0] if vals else default

    curr_param = _param("currency")
    if curr_param:
        set_active_currency(curr_param)

    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", DASHBOARD_HTML

    if path == "/api/health":
        return 200, "application/json; charset=utf-8", {
            "status": "ok",
            "service": "knapset-web",
            "active_currency": get_active_currency(),
            "supported_currencies": sorted(list(DEFAULT_EXCHANGE_RATES.keys())),
        }

    if path == "/api/domains":
        rescan_extensions()
        exts = list_extensions()
        domains_out = []
        for d_id, ext in exts.items():
            layout = ext.get_layout()
            w_reg = ext.get_weights_registry()
            domains_out.append({
                "domain_id": d_id,
                "title": ext.title,
                "description": ext.description,
                "default_budget": layout.default_budget if layout else 800.0,
                "sections": [
                    {"id": s.id, "name": s.name, "label": s.label, "required": s.required}
                    for s in (layout.sections if layout else [])
                ],
                "modes": list(w_reg.list_profiles().keys()) if w_reg else ["best"],
            })
        return 200, "application/json; charset=utf-8", {
            "currency": get_active_currency(),
            "domains": domains_out,
        }

    if path == "/api/build":
        rescan_extensions()
        domain = (_param("domain", "template") or "template").lower().strip()
        try:
            ext = get_extension(domain, fallback=False)
        except KeyError as exc:
            return 404, "application/json; charset=utf-8", {"error": str(exc)}

        layout = ext.get_layout()
        w_reg = ext.get_weights_registry()
        scorer = ext.get_scorer()
        sorter = ext.get_sorter()
        curr = get_active_currency()

        raw_budget = _param("budget")
        budget = float(raw_budget) if raw_budget else (layout.default_budget if layout else 800.0)
        mode = _param("mode") or (w_reg.default_profile_name if w_reg else "best")
        top_n = max(1, min(10, int(_param("top_n", "3") or "3")))
        min_new_ratio = float(_param("min_new_ratio", "0.0") or "0.0")

        t0 = time.perf_counter()
        try:
            profile = w_reg.resolve_profile(mode)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                candidates = ext.load_candidates(budget=budget)
                sets = sorter.sort_and_synthesize(
                    layout=layout,
                    scorer=scorer,
                    profile=profile,
                    candidate_pool=candidates,
                    budget=budget,
                    top_n=top_n,
                    min_new_ratio=min_new_ratio,
                )
        except Exception as exc:
            return 400, "application/json; charset=utf-8", {"error": str(exc)}

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        formatted_sets = []
        for idx, s in enumerate(sets, 1):
            comps_out = {}
            md_lines = [
                f"### {layout.title if layout else domain.upper()} — Set #{idx} ({format_price(float(s.get('total_cost', 0.0)), curr)} | Score: {round(float(s.get('set_score', s.get('score', 0.0))), 1)})",
                "| Section | Component | Price | Link |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for sec_id, c in (s.get("components") or {}).items():
                m_name = c.get("model") or c.get("name") or c.get("title", "Unknown")
                f_price = format_price(float(c.get("price", 0.0)), curr)
                c_url = c.get("url") or (c.get("summary") or {}).get("url", "")
                c_spec = c.get("spec") or {}
                c_eval = c.get("eval") or c.get("heuristic") or {}
                confirm_flag = bool(
                    c.get("confirm_with_seller")
                    or c_eval.get("confirm_with_seller")
                    or "confirm with seller" in str(c_eval.get("warnings", [])).lower()
                    or str(c_spec.get("form_factor") or c.get("form_factor") or "").upper() == "UNKNOWN"
                )
                comps_out[sec_id] = {
                    "model": m_name,
                    "price": c.get("price", 0.0),
                    "formatted_price": f_price,
                    "discount_pct": c.get("discount_pct", 0.0),
                    "url": c_url,
                    "risk_score": int(c_eval.get("risk_score", 0)),
                    "confirm_with_seller": confirm_flag,
                }
                link_md = f"[Link]({c_url})" if c_url else "—"
                notice = " *(Confirm with seller)*" if confirm_flag else ""
                md_lines.append(f"| **{sec_id.upper()}** | {m_name}{notice} | {f_price} | {link_md} |")
            formatted_sets.append({
                "rank": idx,
                "score": round(float(s.get("set_score", s.get("score", 0.0))), 2),
                "total_cost": round(float(s.get("total_cost", 0.0)), 2),
                "formatted_total": format_price(float(s.get("total_cost", 0.0)), curr),
                "surplus": round(float(s.get("surplus", budget - float(s.get("total_cost", 0.0)))), 2),
                "components": comps_out,
                "markdown": "\n".join(md_lines),
            })

        return 200, "application/json; charset=utf-8", {
            "domain": domain,
            "mode": profile.name,
            "currency": curr,
            "budget": budget,
            "elapsed_ms": elapsed_ms,
            "count": len(formatted_sets),
            "sets": formatted_sets,
        }

    if path == "/api/search":
        rescan_extensions()
        domain = (_param("domain", "template") or "template").lower().strip()
        try:
            ext = get_extension(domain, fallback=False)
        except KeyError as exc:
            return 404, "application/json; charset=utf-8", {"error": str(exc)}

        layout = ext.get_layout()
        valid_sections = set(layout.section_ids()) if layout else set()
        default_sec = layout.sections[0].id if (layout and layout.sections) else ""
        section = (_param("section", default_sec) or default_sec).lower().strip()
        if valid_sections and section not in valid_sections:
            return 400, "application/json; charset=utf-8", {
                "error": f"Invalid section '{section}' for domain '{domain}'. Valid sections: {sorted(list(valid_sections))}"
            }
        query = _param("query")
        max_p_str = _param("max_price")
        max_price = float(max_p_str) if max_p_str else None
        mode = _param("mode")
        limit = max(1, min(50, int(_param("limit", "15") or "15")))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                results = ext.search(
                    section_id=section,
                    query=query,
                    max_price=max_price,
                    mode=mode,
                    limit=limit,
                )
            except (ValueError, KeyError) as exc:
                return 400, "application/json; charset=utf-8", {"error": str(exc)}

        curr = get_active_currency()
        items_out = []
        for item in results[:limit]:
            items_out.append({
                "id": item.get("id"),
                "model": item.get("model") or item.get("name") or item.get("title", ""),
                "title": item.get("title") or (item.get("summary") or {}).get("title", ""),
                "price": item.get("price", 0.0),
                "formatted_price": format_price(float(item.get("price", 0.0)), curr),
                "score": round(float(item.get("mode_score", item.get("score", 0.0))), 2),
                "discount_pct": item.get("discount_pct", 0.0),
                "url": item.get("url") or (item.get("summary") or {}).get("url", ""),
            })
        return 200, "application/json; charset=utf-8", {
            "domain": domain,
            "section": section,
            "currency": curr,
            "count": len(items_out),
            "results": items_out,
        }

    if path == "/api/deals":
        curr = get_active_currency()
        domain = _param("domain")
        category = _param("category")
        provider = _param("provider")
        min_drop = float(_param("min_drop_pct", "3.0") or "3.0")
        limit = max(1, min(100, int(_param("limit", "25") or "25")))

        drops = market_store.find_price_drops(
            category=category,
            provider=provider,
            domain=domain,
            min_drop_pct=min_drop,
            limit=limit,
        )
        formatted_drops = []
        for d in drops:
            raw_c = d.get("currency") or "UAH"
            peak_conv = (
                convert_currency(float(d["peak_price"]), raw_c, curr)
                if canonical_currency(raw_c) != canonical_currency(curr)
                else float(d["peak_price"])
            )
            curr_conv = (
                convert_currency(float(d["current_price"]), raw_c, curr)
                if canonical_currency(raw_c) != canonical_currency(curr)
                else float(d["current_price"])
            )
            d_copy = dict(d)
            d_copy["formatted_peak_price"] = format_price(peak_conv, curr)
            d_copy["formatted_current_price"] = format_price(curr_conv, curr)
            formatted_drops.append(d_copy)

        return 200, "application/json; charset=utf-8", {
            "currency": curr,
            "stats": market_store.stats(domain=domain),
            "count": len(formatted_drops),
            "drops": formatted_drops,
        }

    if path == "/api/prebuilt":
        listing_a = _param("listing_a", "") or ""
        listing_b = _param("listing_b")
        mode = _param("mode", "sweet-spot") or "sweet-spot"
        domain = _param("domain")
        if not listing_a.strip():
            return 400, "application/json; charset=utf-8", {"error": "Missing required parameter 'listing_a'."}

        target_ext = None
        exts = list_extensions()
        if domain and domain.lower().strip() in exts:
            target_ext = exts[domain.lower().strip()]
        else:
            for ext in exts.values():
                if type(ext).evaluate_prebuilt is not getattr(ext.__class__.__mro__[-2], "evaluate_prebuilt", None):
                    try:
                        # Pick the first extension that overrides evaluate_prebuilt
                        if "evaluate_prebuilt" in type(ext).__dict__:
                            target_ext = ext
                            break
                    except Exception:
                        pass

        if target_ext is None:
            return 404, "application/json; charset=utf-8", {
                "error": "No installed domain extension supports prebuilt evaluation. Install a domain extension first."
            }

        try:
            payload = target_ext.evaluate_prebuilt(listing_a, listing_b=listing_b, mode=mode)
            return 200, "application/json; charset=utf-8", payload
        except Exception as exc:
            return 400, "application/json; charset=utf-8", {"error": str(exc)}

    return 404, "application/json; charset=utf-8", {"error": f"Unknown endpoint: {path}"}


class KnapsetHTTPRequestHandler(BaseHTTPRequestHandler):
    """Zero-dependency HTTP handler serving the Knapset Web Dashboard and JSON REST API."""

    def log_message(self, format: str, *args: Any) -> None:
        # Clean, single-line request logging
        sys.stderr.write(f"[Knapset Web] {self.address_string()} - {format % args}\n")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        status, content_type, body = dispatch_api_request(parsed.path, qs)

        if isinstance(body, (dict, list)):
            raw_bytes = json.dumps(body, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        else:
            raw_bytes = str(body).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw_bytes)


def run_web_server(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="main.py serve",
        description="Start the built-in Knapset Web Dashboard & REST API Server (100% Python stdlib).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument("--open", action="store_true", help="Automatically open the dashboard in default web browser")
    args = parser.parse_args(argv or [])

    server = ThreadingHTTPServer((args.host, args.port), KnapsetHTTPRequestHandler)
    url = f"http://{args.host}:{args.port}"
    print("\n" + "═" * 78)
    print(f"  Knapset Web Dashboard & REST API Server running at: {url}")
    print("═" * 78)
    print(f"  • Interactive UI   : {url}/")
    print(f"  • Domains API      : {url}/api/domains")
    print(f"  • Set Synthesizer  : {url}/api/build?domain=<domain>&budget=<amount>&currency=USDT")
    print(f"  • Section Search   : {url}/api/search?domain=<domain>&section=<section>&query=<text>")
    print(f"  • SQLite Deals API : {url}/api/deals?min_drop_pct=3.0")
    print("─" * 78)
    print("  Press Ctrl+C to stop the server.\n")

    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Knapset Web Server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_web_server()
