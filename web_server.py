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
    list_shared_profiles,
    resolve_prefs_for_pools,
    resolve_preset,
    save_preset,
    save_shared_profile,
)
from solver_engine import solve_basket, solve_rank, solve_sets
from sources.registry import inspect_source, open_source
from theme_config import (
    ThemeConfig,
    discover_themes,
    load_active_theme,
    reset_active_theme,
    set_active_theme,
)

APP_VERSION = "2.0.0"

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
      --accent: #38bdf8; --green: #34d399; --red: #f87171; --warn: #fbbf24;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.45; }
    header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    header h1 { font-size: 1.15rem; color: var(--accent); }
    .hdr-right { display: flex; align-items: center; gap: 10px; }
    .hdr-right select { width: auto; min-width: 150px; padding: 5px 8px; font-size: 0.82rem; }
    main { max-width: 1280px; margin: 18px auto; padding: 0 18px; display: grid; grid-template-columns: 420px 1fr; gap: 18px; }
    @media (max-width: 960px) { main { grid-template-columns: 1fr; } }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
    h2 { font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
    label { display: block; font-size: 0.78rem; color: var(--muted); margin: 8px 0 4px; }
    input, select, textarea { width: 100%; background: var(--panel-2); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px; font-family: inherit; font-size: 0.85rem; }
    textarea { font-family: monospace; min-height: 52px; }
    .row { display: flex; gap: 6px; align-items: center; }
    button { background: var(--accent); color: #041019; font-weight: 600; border: none; border-radius: 6px; padding: 7px 11px; cursor: pointer; font-size: 0.82rem; white-space: nowrap; }
    button.secondary { background: var(--panel-2); color: var(--text); border: 1px solid var(--border); }
    button.small { padding: 4px 8px; font-size: 0.78rem; }
    button.danger { background: rgba(248, 113, 113, 0.15); color: var(--red); border: 1px solid rgba(248, 113, 113, 0.35); }
    .weight-row { display: grid; grid-template-columns: 1fr 28px 66px 28px 28px; gap: 5px; align-items: center; margin: 5px 0; }
    .weight-row input[type=number] { text-align: center; padding: 4px; font-family: monospace; }
    .result-card { background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 10px; }
    .score-pill { display: inline-block; background: rgba(52, 211, 153, 0.16); color: var(--green); border: 1px solid rgba(52, 211, 153, 0.4); border-radius: 999px; padding: 2px 10px; font-weight: 700; font-size: 0.82rem; }
    .badge { display: inline-block; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 2px 7px; font-size: 0.74rem; color: var(--muted); margin: 2px 4px 2px 0; }
    .badge.pos { color: var(--green); border-color: rgba(52, 211, 153, 0.35); }
    .badge.neg { color: var(--red); border-color: rgba(248, 113, 113, 0.35); }
    .err { color: var(--red); margin-top: 8px; font-size: 0.83rem; }
    details { border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px; margin-top: 8px; background: rgba(255,255,255,0.01); }
    summary { cursor: pointer; font-size: 0.8rem; color: var(--muted); font-weight: 600; }
    pre { font-size: 0.76rem; color: var(--muted); white-space: pre-wrap; margin-top: 6px; }
    .reg-item { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.82rem; }
  </style>
</head>
<body>
<header>
  <h1>Knapset v2 Studio</h1>
  <div class="hdr-right">
    <span style="color:var(--muted);font-size:0.8rem;">Theme:</span>
    <select id="themeSelect" onchange="switchTheme()"></select>
  </div>
</header>
<main>
  <section class="card">
    <h2>Configuration</h2>
    <label>Load Installed Preset &amp; Profile</label>
    <div class="row">
      <select id="presetSelect" style="flex:2" onchange="onPresetChange()"><option value="">-- Custom / Source Path --</option></select>
      <select id="profileSelect" style="flex:1"><option value="">default</option></select>
      <button class="secondary" onclick="loadSelectedPreset()">Load</button>
    </div>

    <label>Saved Shared Prefs (<code>--prefs</code>)</label>
    <div class="row">
      <select id="prefsSelect" style="flex:1"><option value="">-- None --</option></select>
      <button class="secondary small" onclick="applySavedPref()">Apply</button>
      <button class="secondary small" onclick="saveCurrentPref()">Save</button>
      <button class="danger small" onclick="deleteSavedPref()">Del</button>
    </div>

    <label>Source File Path (.db / .csv / .json)</label>
    <div class="row">
      <input id="sourcePath" placeholder="/path/to/data.db or items.csv" />
      <button class="secondary" onclick="inspectSourcePath()">Inspect</button>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>Table / Slot (optional)</label>
        <select id="tableSelect"><option value="">-- All / Default --</option></select>
      </div>
      <div style="flex:1">
        <label>Mode</label>
        <select id="modeSelect">
          <option value="solve">solve (multi-slot set)</option>
          <option value="basket">basket (knapsack)</option>
          <option value="rank">rank (single table)</option>
        </select>
      </div>
      <div style="width:74px">
        <label>Top N</label>
        <input id="topN" type="number" value="5" min="1" max="50" />
      </div>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>Normalization</label>
        <select id="normSelect">
          <option value="robust">robust (P5-P95)</option>
          <option value="minmax">minmax (0..1)</option>
          <option value="rank">rank (percentile)</option>
        </select>
      </div>
      <div style="flex:1;padding-top:20px;">
        <label style="display:flex;align-items:center;gap:6px;margin:0;cursor:pointer;">
          <input id="diverseCheck" type="checkbox" style="width:auto;" />
          <span>Diverse Top-N sets</span>
        </label>
      </div>
    </div>

    <details id="refDetails">
      <summary>Reference DB Enrichment (<code>--ref</code> / Compare)</summary>
      <label>Reference Source (.db / .csv / .json)</label>
      <input id="refSource" placeholder="/path/to/benchmarks.csv" />
      <div class="row">
        <div style="flex:1">
          <label>Match Rule</label>
          <input id="refMatch" placeholder="title~model" />
        </div>
        <div style="width:92px">
          <label>Strategy</label>
          <select id="refStrategy">
            <option value="">auto</option>
            <option value="token">token</option>
            <option value="exact">exact</option>
            <option value="regex">regex</option>
          </select>
        </div>
        <div style="width:92px">
          <label>Unmatched</label>
          <select id="refUnmatched">
            <option value="drop">drop</option>
            <option value="keep">keep</option>
          </select>
        </div>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="secondary small" onclick="checkRefCompare()">Check Match Coverage</button>
        <span id="compareSummary" style="font-size:0.78rem;color:var(--muted)"></span>
      </div>
    </details>

    <label style="display:flex;justify-content:space-between;align-items:center;">
      <span>Weights (<code>expr:+w</code>)</span>
      <span>
        <button class="secondary small" type="button" onclick="addWeightRow('', 0.5)">+ Weight</button>
        <button class="secondary small" type="button" onclick="suggestWeights()">Suggest</button>
      </span>
    </label>
    <div id="weightSliders"></div>
    <textarea id="weightsBox" placeholder="score:+0.6, price:-0.4" oninput="syncSlidersFromBox()"></textarea>

    <label>Formulas (one per line: <code>name = expr</code>)</label>
    <textarea id="formulasBox" placeholder="deal_pct = (ref.msrp - price) / ref.msrp * 100"></textarea>
    <label>Where Filters (one per line)</label>
    <textarea id="whereBox" placeholder="price <= 600"></textarea>
    <label>Limits (one per line: <code>sum(col) &lt;= max</code>)</label>
    <textarea id="limitsBox" placeholder="sum(price) <= 950"></textarea>
    <label>Cross-Slot Rules (one per line, <code>solve</code> mode)</label>
    <textarea id="rulesBox" placeholder="cpu.socket == mobo.socket"></textarea>

    <div class="row" style="margin-top:12px">
      <button style="flex:1" onclick="runSolve()">Optimize Now</button>
      <button class="secondary" onclick="downloadJson()">JSON</button>
      <button class="secondary" onclick="downloadCsv()">CSV</button>
    </div>
    <div id="statusMsg" class="err" style="color:var(--muted)"></div>
  </section>

  <section class="card">
    <h2>
      <span>Results &amp; Schema Inspector</span>
      <button class="secondary small" onclick="toggleRegistry()">Registry</button>
    </h2>
    <div id="registryPanel" style="display:none;margin-bottom:12px;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--panel-2);">
      <div class="row" style="margin-bottom:8px;">
        <input id="regQuery" placeholder="Search registry presets & themes..." />
        <button class="small" onclick="searchRegistry()">Search</button>
      </div>
      <div id="regResults" style="max-height:200px;overflow:auto;"></div>
    </div>
    <div id="schemaPanel" style="margin-bottom:12px;color:var(--muted);font-size:0.82rem;"></div>
    <div id="resultsPanel"></div>
  </section>
</main>
<script>
let lastPayload = null;
let presetMap = {};
let inspectedSchema = null;

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
async function api(url, body) {
  const opts = body ? { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) } : {};
  const r = await fetch(url, opts);
  return await r.json();
}
function setStatus(text, colorVar) {
  const msg = document.getElementById('statusMsg');
  msg.textContent = text;
  msg.style.color = colorVar || 'var(--muted)';
}

function applyThemeCss(cssVars) {
  if (!cssVars) return;
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(cssVars)) {
    if (v) root.setProperty('--' + k, v);
  }
}
async function refreshThemes() {
  const data = await api('/api/themes');
  const sel = document.getElementById('themeSelect');
  sel.innerHTML = '';
  (data.themes || []).forEach(t => {
    const o = document.createElement('option');
    o.value = t.name;
    o.textContent = t.name + (t.active ? ' *' : '');
    if (t.active) {
      o.selected = true;
      applyThemeCss(t.css_vars);
    }
    sel.appendChild(o);
  });
}
async function switchTheme() {
  const name = document.getElementById('themeSelect').value;
  if (!name) return;
  const res = await api('/api/themes/use', { name });
  if (res.theme) applyThemeCss(res.theme.css_vars);
  await refreshThemes();
}

async function refreshPresets() {
  const data = await api('/api/presets');
  const sel = document.getElementById('presetSelect');
  const prev = sel.value;
  sel.innerHTML = '<option value="">-- Custom / Source Path --</option>';
  presetMap = {};
  (data.presets || []).forEach(p => {
    presetMap[p.name] = p;
    const o = document.createElement('option');
    o.value = p.name;
    o.textContent = `${p.name} (${p.mode}) - ${p.title}`;
    sel.appendChild(o);
  });
  if (prev && presetMap[prev]) sel.value = prev;
  onPresetChange();
}
function onPresetChange() {
  const name = document.getElementById('presetSelect').value;
  const p = presetMap[name];
  const profSel = document.getElementById('profileSelect');
  profSel.innerHTML = '';
  const profs = (p && p.profiles && p.profiles.length) ? p.profiles : ['default'];
  profs.forEach(pr => {
    const o = document.createElement('option');
    o.value = pr; o.textContent = pr;
    profSel.appendChild(o);
  });
}
async function loadSelectedPreset() {
  const name = document.getElementById('presetSelect').value;
  if (!name) return;
  const profile = document.getElementById('profileSelect').value || undefined;
  const res = await api('/api/solve', { preset: name, profile });
  renderResponse(res);
  if (res.mode) document.getElementById('modeSelect').value = res.mode;
  if (res.weights) {
    document.getElementById('weightsBox').value = Object.entries(res.weights).map(([k,v]) => `${k}:${v}`).join(', ');
    syncSlidersFromBox();
  }
  document.getElementById('formulasBox').value = (res.formulas || []).join('\n');
  document.getElementById('whereBox').value = (res.where || []).join('\n');
  document.getElementById('limitsBox').value = (res.limits || []).join('\n');
  document.getElementById('rulesBox').value = (res.rules || []).join('\n');
}

async function refreshPrefs() {
  const data = await api('/api/prefs');
  const sel = document.getElementById('prefsSelect');
  const prev = sel.value;
  sel.innerHTML = '<option value="">-- None --</option>';
  Object.entries(data.profiles || {}).forEach(([name, p]) => {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = `${name} [${p.mode || 'rank'}]`;
    sel.appendChild(o);
  });
  if (prev && (data.profiles || {})[prev]) sel.value = prev;
}
async function applySavedPref() {
  const name = document.getElementById('prefsSelect').value;
  if (!name) return;
  const data = await api('/api/prefs');
  const prof = (data.profiles || {})[name];
  if (!prof) return;
  if (prof.mode) document.getElementById('modeSelect').value = prof.mode;
  if (prof.weights) {
    document.getElementById('weightsBox').value = Object.entries(prof.weights).map(([k,v]) => `${k}:${v}`).join(', ');
    syncSlidersFromBox();
  }
  document.getElementById('formulasBox').value = (prof.formulas || []).join('\n');
  document.getElementById('whereBox').value = (prof.where || []).join('\n');
  document.getElementById('limitsBox').value = (prof.limits || []).join('\n');
  document.getElementById('rulesBox').value = (prof.rules || []).join('\n');
  if (document.getElementById('sourcePath').value.trim()) {
    await runSolve(name);
  } else {
    setStatus(`Loaded saved preference '${name}'.`, 'var(--green)');
  }
}
async function saveCurrentPref() {
  const name = prompt('Save current weights, formulas, filters, limits & rules as shared preference name:');
  if (!name || !name.trim()) return;
  const splitLines = id => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  const res = await api('/api/prefs/save', {
    name: name.trim(),
    mode: document.getElementById('modeSelect').value,
    weights: document.getElementById('weightsBox').value.trim(),
    formulas: splitLines('formulasBox'),
    where: splitLines('whereBox'),
    limits: splitLines('limitsBox'),
    rules: splitLines('rulesBox'),
  });
  if (res.error) { setStatus(res.error, 'var(--red)'); return; }
  await refreshPrefs();
  document.getElementById('prefsSelect').value = res.saved;
  setStatus(`Saved preference '${res.saved}' -> ${res.path}`, 'var(--green)');
}
async function deleteSavedPref() {
  const name = document.getElementById('prefsSelect').value;
  if (!name) return;
  const res = await api('/api/prefs/delete', { name });
  if (res.error) { setStatus(res.error, 'var(--red)'); return; }
  await refreshPrefs();
  setStatus(`Deleted saved preference '${name}'.`, 'var(--warn)');
}

function parseWeightsText(text) {
  return text.split(',').map(s => s.trim()).filter(Boolean).map(part => {
    const idx = part.lastIndexOf(':');
    if (idx <= 0) return null;
    const expr = part.slice(0, idx).trim();
    const val = parseFloat(part.slice(idx + 1).trim());
    return Number.isFinite(val) ? { expr, val } : null;
  }).filter(Boolean);
}
function renderWeightSliders(items) {
  const wrap = document.getElementById('weightSliders');
  wrap.innerHTML = '';
  items.forEach((it, idx) => {
    const row = document.createElement('div');
    row.className = 'weight-row';
    row.innerHTML = `
      <input value="${esc(it.expr)}" onchange="updateWeightRow(${idx}, this.value, null)" />
      <button type="button" class="secondary small" onclick="nudgeWeight(${idx}, -0.1)">-</button>
      <input type="number" step="0.1" value="${it.val}" onchange="updateWeightRow(${idx}, null, this.value)" />
      <button type="button" class="secondary small" onclick="nudgeWeight(${idx}, 0.1)">+</button>
      <button type="button" class="danger small" onclick="removeWeightRow(${idx})">×</button>
    `;
    wrap.appendChild(row);
  });
}
function syncBoxFromSliders(items) {
  document.getElementById('weightsBox').value = items.map(it => `${it.expr}:${it.val >= 0 ? '+' : ''}${Number(it.val.toFixed(3))}`).join(', ');
}
function syncSlidersFromBox() {
  renderWeightSliders(parseWeightsText(document.getElementById('weightsBox').value));
}
function addWeightRow(expr, val) {
  const items = parseWeightsText(document.getElementById('weightsBox').value);
  items.push({ expr: expr || 'price', val: val ?? -0.5 });
  syncBoxFromSliders(items);
  renderWeightSliders(items);
}
function updateWeightRow(idx, newExpr, newVal) {
  const items = parseWeightsText(document.getElementById('weightsBox').value);
  if (!items[idx]) return;
  if (newExpr !== null) items[idx].expr = newExpr.trim();
  if (newVal !== null) items[idx].val = parseFloat(newVal) || 0;
  syncBoxFromSliders(items);
  renderWeightSliders(items);
}
function nudgeWeight(idx, delta) {
  const items = parseWeightsText(document.getElementById('weightsBox').value);
  if (!items[idx]) return;
  items[idx].val = Math.round((items[idx].val + delta) * 100) / 100;
  syncBoxFromSliders(items);
  renderWeightSliders(items);
}
function removeWeightRow(idx) {
  const items = parseWeightsText(document.getElementById('weightsBox').value);
  items.splice(idx, 1);
  syncBoxFromSliders(items);
  renderWeightSliders(items);
}
function suggestWeights() {
  if (!inspectedSchema || !(inspectedSchema.tables || []).length) {
    setStatus('Inspect a source first to suggest weights.', 'var(--warn)');
    return;
  }
  const tblName = document.getElementById('tableSelect').value;
  const tables = tblName ? inspectedSchema.tables.filter(t => t.name === tblName) : inspectedSchema.tables;
  const costRe = /(price|cost|fee|weight|mass|watt|tdp|power|latency|ms|risk|calor)/i;
  const items = [];
  tables.forEach(t => {
    const prefix = tables.length > 1 ? `${t.name}.` : '';
    (t.columns || []).forEach(c => {
      if (c.kind === 'number' && !/^(id|count)$/i.test(c.name) && items.length < 4) {
        items.push({ expr: prefix + c.name, val: costRe.test(c.name) ? -0.4 : 0.6 });
      }
    });
  });
  syncBoxFromSliders(items);
  renderWeightSliders(items);
}

async function inspectSourcePath() {
  const src = document.getElementById('sourcePath').value.trim();
  if (!src) return;
  const data = await api('/api/inspect', { source: src });
  if (data.error) { setStatus(data.error, 'var(--red)'); return; }
  inspectedSchema = data.schema;
  const tblSel = document.getElementById('tableSelect');
  tblSel.innerHTML = '<option value="">-- All / Default --</option>';
  (data.schema.tables || []).forEach(t => {
    const o = document.createElement('option');
    o.value = t.name; o.textContent = `${t.name} (${t.row_count} rows)`;
    tblSel.appendChild(o);
  });
  if ((data.schema.tables || []).length > 1 && document.getElementById('modeSelect').value !== 'solve') {
    document.getElementById('modeSelect').value = 'solve';
  }
  const html = (data.schema.tables || []).map(t =>
    `<div><b>${esc(t.name)}</b> (${t.row_count} rows): ` +
    t.columns.map(c => `${esc(c.name)}<i>(${esc(c.kind)})</i>`).join(', ') + `</div>`
  ).join('');
  document.getElementById('schemaPanel').innerHTML = html;
  setStatus(`Inspected ${(data.schema.tables || []).length} table(s).`, 'var(--green)');
}

async function checkRefCompare() {
  const src = document.getElementById('sourcePath').value.trim();
  const refSrc = document.getElementById('refSource').value.trim();
  if (!src || !refSrc) { setStatus('Enter both Source and Reference Source first.', 'var(--warn)'); return; }
  const res = await api('/api/compare', {
    source: src,
    table: document.getElementById('tableSelect').value || undefined,
    ref_source: refSrc,
    match: document.getElementById('refMatch').value.trim() || undefined,
    strategy: document.getElementById('refStrategy').value || undefined,
    on_unmatched: document.getElementById('refUnmatched').value,
    limit: 5,
  });
  if (res.error) { setStatus(res.error, 'var(--red)'); return; }
  const m = res.match;
  const pct = (m.match_rate * 100).toFixed(1);
  document.getElementById('compareSummary').textContent = `${m.matched}/${m.total} matched (${pct}%) via ${m.left_field} (${m.strategy}) -> ${m.alias}.${m.right_field}`;
}

async function runSolve(prefsOverride) {
  const splitLines = id => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  const src = document.getElementById('sourcePath').value.trim();
  const refSrc = document.getElementById('refSource').value.trim();
  const body = {
    preset: (!src && document.getElementById('presetSelect').value) ? document.getElementById('presetSelect').value : undefined,
    profile: (!src && document.getElementById('profileSelect').value) ? document.getElementById('profileSelect').value : undefined,
    prefs: typeof prefsOverride === 'string' ? prefsOverride : undefined,
    source: src || undefined,
    table: document.getElementById('tableSelect').value || undefined,
    mode: document.getElementById('modeSelect').value,
    norm: document.getElementById('normSelect').value,
    diverse: document.getElementById('diverseCheck').checked,
    top_n: parseInt(document.getElementById('topN').value || '5', 10),
    weights: document.getElementById('weightsBox').value.trim(),
    formulas: splitLines('formulasBox'),
    where: splitLines('whereBox'),
    limits: splitLines('limitsBox'),
    rules: splitLines('rulesBox'),
    ref_source: refSrc || undefined,
    match: document.getElementById('refMatch').value.trim() || undefined,
    strategy: document.getElementById('refStrategy').value || undefined,
    on_unmatched: document.getElementById('refUnmatched').value,
  };
  const res = await api('/api/solve', body);
  renderResponse(res);
}

function renderResponse(res) {
  lastPayload = res;
  if (res.error) { setStatus(res.error, 'var(--red)'); return; }
  const skipped = (res.meta && res.meta.skipped_prefs_terms) ? ` (skipped missing cols: ${res.meta.skipped_prefs_terms.join(', ')})` : '';
  const diag = (res.meta && res.meta.diagnostics && res.meta.diagnostics.summary) ? ` — ${res.meta.diagnostics.summary}` : '';
  setStatus(`Solved ${(res.results || []).length} result(s) in mode=${res.mode}.${skipped}${diag}`, (res.results || []).length ? 'var(--green)' : 'var(--warn)');
  const panel = document.getElementById('resultsPanel');
  const emptyHtml = diag ? `<div class="result-card"><b>No feasible results</b><div style="margin-top:4px;color:var(--warn)">${esc(res.meta.diagnostics.summary)}</div></div>` : '<div>No feasible results found.</div>';
  panel.innerHTML = (res.results || []).map((r, i) => {
    const score = r.score ?? r.total_score ?? 0;
    const bdHtml = (r.breakdown || []).map(b => {
      const cls = b.weight >= 0 ? 'pos' : 'neg';
      return `<span class="badge ${cls}">${esc(b.expression)}: raw=${esc(b.raw_value)} n=${b.normalized} w=${b.weight} (+${b.contribution})</span>`;
    }).join('');
    return `<div class="result-card"><div class="row" style="justify-content:space-between"><b>#${i+1}</b><span class="score-pill">${esc(score)}</span></div>${bdHtml ? `<div style="margin-top:6px">${bdHtml}</div>` : ''}<pre>${esc(JSON.stringify(r, null, 2))}</pre></div>`;
  }).join('') || emptyHtml;
}

function downloadJson() {
  if (!lastPayload) return;
  const blob = new Blob([JSON.stringify(lastPayload, null, 2)], {type: 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'knapset_results.json'; a.click();
}
function downloadCsv() {
  if (!lastPayload || !(lastPayload.results || []).length) return;
  const rows = lastPayload.results.map((r, idx) => {
    const flat = { rank: idx + 1, score: r.score ?? r.total_score ?? 0 };
    if (r.row) Object.entries(r.row).forEach(([k, v]) => { if (typeof v !== 'object') flat[k] = v; });
    if (r.slots) Object.entries(r.slots).forEach(([s, obj]) => { flat[s] = obj.title || obj.name || obj.model || obj.id || JSON.stringify(obj); });
    if (r.metrics) Object.entries(r.metrics).forEach(([k, v]) => { flat['metric_' + k] = v; });
    return flat;
  });
  const cols = Array.from(new Set(rows.flatMap(Object.keys)));
  const csvEsc = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = [cols.map(csvEsc).join(','), ...rows.map(r => cols.map(c => csvEsc(r[c])).join(','))].join('\n');
  const blob = new Blob([csv], {type: 'text/csv'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'knapset_results.csv'; a.click();
}

function toggleRegistry() {
  const el = document.getElementById('registryPanel');
  const open = el.style.display === 'none';
  el.style.display = open ? 'block' : 'none';
  if (open) searchRegistry();
}
async function searchRegistry() {
  const q = encodeURIComponent(document.getElementById('regQuery').value.trim());
  const data = await api('/api/registry?q=' + q);
  const el = document.getElementById('regResults');
  if (data.error) { el.innerHTML = `<div class="err">${esc(data.error)}</div>`; return; }
  const items = data.modules || [];
  el.innerHTML = items.map(m => `
    <div class="reg-item">
      <div><b>${esc(m.name)}</b> <span class="badge">${esc(m.type)}</span><div style="color:var(--muted);font-size:0.75rem">${esc(m.description || '')}</div></div>
      <button class="secondary small" onclick="installRegItem('${esc(m.type)}:${esc(m.name)}', '${esc(m.type)}')">Install</button>
    </div>
  `).join('') || '<div style="color:var(--muted);font-size:0.8rem">No registry items found.</div>';
}
async function installRegItem(target, itemType) {
  const res = await api('/api/registry/install', { target, use: itemType === 'themes' });
  setStatus(res.message || res.error || 'Done', res.ok ? 'var(--green)' : 'var(--red)');
  await refreshPresets();
  await refreshThemes();
}

refreshThemes();
refreshPresets();
refreshPrefs();
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
            first_rows, weights=eff_weights, formulas=eff_formulas, where=eff_where, top_n=top_n, norm=eff_norm
        )
        if skipped:
            meta["skipped_prefs_terms"] = skipped
        return {"mode": "rank", "meta": meta, "results": [r.to_dict() for r in ranked]}
    if eff_mode == "basket":
        first_rows = next(iter(slot_pools.values()), [])
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
                self._send_html(DASHBOARD_HTML)
            elif route == "/api/health":
                self._send_json(HTTPStatus.OK, {"status": "ok", "version": APP_VERSION})
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
            elif self.path == "/api/save-preset":
                out_path = save_preset(PresetConfig.from_dict(body["preset"]), body["path"], overwrite=True)
                self._send_json(HTTPStatus.OK, {"saved": str(out_path)})
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
