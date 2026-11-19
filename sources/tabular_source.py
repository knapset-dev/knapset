"""
Read-only CSV, TSV, JSON, and JSONL tabular data source adapter.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sources.base import BaseDataSource, SourceSchema, TableSchema, summarize_rows


def _detect_csv_delimiter(sample: str, default: str = ",") -> str:
    """Detects CSV delimiter (comma, semicolon, tab, or pipe) from header/sample lines."""
    lines = [ln for ln in sample.splitlines() if ln.strip()][:10]
    if not lines:
        return default
    header = lines[0]
    # Fast check: if header has no commas but clear semicolons/tabs, pick directly
    if "," not in header:
        for cand in (";", "\t", "|"):
            if cand in header:
                return cand
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines), delimiters=",;\t|")
        if dialect.delimiter in (",", ";", "\t", "|"):
            return dialect.delimiter
    except csv.Error:
        pass
    return default


def _parse_csv_text(text: str, delim: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    use_delim = delim or _detect_csv_delimiter(text)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=use_delim)
    fieldnames = [str(f).strip() for f in (reader.fieldnames or [])]
    rows: List[Dict[str, Any]] = []
    for raw_row in reader:
        clean_row = {
            str(k).strip(): _coerce_scalar(v)
            for k, v in raw_row.items()
            if k is not None
        }
        rows.append(clean_row)
    return rows, fieldnames


def _resolve_json_path(payload: Any, json_path: str) -> Any:
    """Walks a dotted path (e.g. 'data.items' or '$.data.items') inside a JSON object."""
    clean = json_path.strip()
    if clean.startswith("$."):
        clean = clean[2:]
    elif clean.startswith("$"):
        clean = clean[1:].lstrip(".")
    curr = payload
    for part in [p for p in clean.split(".") if p]:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        elif isinstance(curr, list) and part.isdigit() and int(part) < len(curr):
            curr = curr[int(part)]
        else:
            raise KeyError(f"JSON path '{json_path}' not found at segment '{part}'.")
    return curr


def _coerce_scalar(raw: Any) -> Any:
    """Converts raw CSV/TSV string cells into int, float, bool, None, or stripped str."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        if any(ch in s for ch in (".", "e", "E")):
            val = float(s)
            return val if math.isfinite(val) else s
        return int(s)
    except ValueError:
        try:
            val = float(s)
            return val if math.isfinite(val) else s
        except ValueError:
            return s


class TabularDataSource(BaseDataSource):
    """Read-only adapter for CSV, TSV, JSON, and JSONL files."""

    def __init__(self, path: str | Path, json_path: Optional[str] = None):
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Data file not found: {path}")
        self.path = p
        self.json_path = json_path.strip() if json_path else None
        ext = p.suffix.lower()
        if ext in (".csv", ".tsv"):
            self.source_kind = ext.lstrip(".")
        elif ext == ".json":
            self.source_kind = "json"
        elif ext in (".jsonl", ".ndjson"):
            self.source_kind = "jsonl"
        else:
            raise ValueError(f"Unsupported tabular file format '{p.suffix}' for '{p.name}'")

    def _load_collections(self) -> Dict[str, tuple[List[Dict[str, Any]], Optional[List[str]]]]:
        ext = self.path.suffix.lower()
        default_name = self.path.stem or "rows"

        if ext in (".csv", ".tsv"):
            text = self.path.read_text(encoding="utf-8-sig")
            delim = "\t" if ext == ".tsv" else None
            rows, fieldnames = _parse_csv_text(text, delim=delim)
            return {default_name: (rows, fieldnames)}

        if ext in (".jsonl", ".ndjson"):
            rows = []
            with self.path.open("r", encoding="utf-8-sig") as f:
                for idx, line in enumerate(f, 1):
                    text = line.strip()
                    if not text:
                        continue
                    item = json.loads(text)
                    if not isinstance(item, dict):
                        raise ValueError(f"Expected JSON object on line {idx} of '{self.path.name}'")
                    rows.append(item)
            return {default_name: (rows, None)}

        # .json
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if self.json_path:
            target_val = _resolve_json_path(payload, self.json_path)
            col_name = self.json_path.split(".")[-1] or default_name
            if isinstance(target_val, list):
                return {col_name: ([r for r in target_val if isinstance(r, dict)], None)}
            if isinstance(target_val, dict):
                return {col_name: ([target_val], None)}
            raise ValueError(f"JSON path '{self.json_path}' resolved to non-tabular type '{type(target_val).__name__}'.")
        if isinstance(payload, list):
            dict_rows = [r for r in payload if isinstance(r, dict)]
            return {default_name: (dict_rows, None)}
        if isinstance(payload, dict):
            collections: Dict[str, tuple[List[Dict[str, Any]], Optional[List[str]]]] = {}
            for key, val in payload.items():
                if isinstance(val, list) and (not val or all(isinstance(r, dict) for r in val)):
                    collections[str(key)] = (list(val), None)
            if collections:
                return collections
            return {default_name: ([payload], None)}
        raise ValueError(f"Unsupported JSON top-level type '{type(payload).__name__}' in '{self.path.name}'")

    def inspect(self, sample_rows: int = 200) -> SourceSchema:
        collections = self._load_collections()
        tables: List[TableSchema] = []
        for name, (rows, col_order) in collections.items():
            sampled = rows[: max(1, int(sample_rows))] if rows else []
            tables.append(
                summarize_rows(
                    table_name=name,
                    rows=sampled,
                    col_order=col_order,
                    row_count_override=len(rows),
                )
            )
        return SourceSchema(location=str(self.path), kind=self.source_kind, tables=tables)

    def load_rows(
        self,
        table: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if query:
            raise ValueError(
                f"SQL queries are only supported on SQLite/DB sources, not '{self.source_kind}' files."
            )
        collections = self._load_collections()
        if not collections:
            return []

        if table is None:
            if len(collections) == 1:
                rows = next(iter(collections.values()))[0]
            else:
                raise ValueError(
                    f"Multiple collections found in '{self.path.name}' ({', '.join(collections.keys())}). "
                    "Specify --table <name>."
                )
        else:
            if table not in collections:
                raise ValueError(
                    f"Collection '{table}' not found in '{self.path.name}'. "
                    f"Available: {', '.join(collections.keys())}"
                )
            rows = collections[table][0]

        if limit is not None and limit > 0:
            return [dict(r) for r in rows[: int(limit)]]
        return [dict(r) for r in rows]


class StdinDataSource(BaseDataSource):
    """Read-only adapter reading CSV, TSV, JSON, or JSONL piped from stdin (`-`)."""

    source_kind: str = "stdin"

    def __init__(self, raw_text: Optional[str] = None, json_path: Optional[str] = None):
        text = sys.stdin.read() if raw_text is None else raw_text
        self._raw = text.lstrip("\ufeff")
        self.json_path = json_path.strip() if json_path else None
        self._cached: Optional[Dict[str, tuple[List[Dict[str, Any]], Optional[List[str]]]]] = None

    def _load_collections(self) -> Dict[str, tuple[List[Dict[str, Any]], Optional[List[str]]]]:
        if self._cached is not None:
            return self._cached
        stripped = self._raw.strip()
        if not stripped:
            self._cached = {"stdin": ([], None)}
            return self._cached

        if stripped[0] in ("{", "["):
            try:
                payload = json.loads(stripped)
                if self.json_path:
                    val = _resolve_json_path(payload, self.json_path)
                    col_name = self.json_path.split(".")[-1] or "stdin"
                    rows = [r for r in val if isinstance(r, dict)] if isinstance(val, list) else ([val] if isinstance(val, dict) else [])
                    self._cached = {col_name: (rows, None)}
                    return self._cached
                if isinstance(payload, list):
                    self._cached = {"stdin": ([r for r in payload if isinstance(r, dict)], None)}
                    return self._cached
                if isinstance(payload, dict):
                    colls = {
                        str(k): (list(v), None)
                        for k, v in payload.items()
                        if isinstance(v, list) and (not v or all(isinstance(r, dict) for r in v))
                    }
                    self._cached = colls if colls else {"stdin": ([payload], None)}
                    return self._cached
            except json.JSONDecodeError:
                # Try JSONL
                rows = []
                for line in stripped.splitlines():
                    ln = line.strip()
                    if not ln:
                        continue
                    obj = json.loads(ln)
                    if isinstance(obj, dict):
                        rows.append(obj)
                self._cached = {"stdin": (rows, None)}
                return self._cached

        rows, fieldnames = _parse_csv_text(self._raw)
        self._cached = {"stdin": (rows, fieldnames)}
        return self._cached

    def inspect(self, sample_rows: int = 200) -> SourceSchema:
        collections = self._load_collections()
        tables: List[TableSchema] = []
        for name, (rows, col_order) in collections.items():
            sampled = rows[: max(1, int(sample_rows))] if rows else []
            tables.append(summarize_rows(name, sampled, col_order=col_order, row_count_override=len(rows)))
        return SourceSchema(location="stdin", kind=self.source_kind, tables=tables)

    def load_rows(
        self,
        table: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if query:
            raise ValueError("SQL queries are only supported on SQLite/DB sources, not stdin.")
        collections = self._load_collections()
        if table is None:
            rows = next(iter(collections.values()))[0] if collections else []
        else:
            if table not in collections:
                raise ValueError(f"Collection '{table}' not found in stdin. Available: {', '.join(collections.keys())}")
            rows = collections[table][0]
        if limit is not None and limit > 0:
            return [dict(r) for r in rows[: int(limit)]]
        return [dict(r) for r in rows]
