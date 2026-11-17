"""
Read-only CSV, TSV, JSON, and JSONL tabular data source adapter.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sources.base import BaseDataSource, SourceSchema, TableSchema, summarize_rows


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

    def __init__(self, path: str | Path):
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Data file not found: {path}")
        self.path = p
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
            delim = "\t" if ext == ".tsv" else ","
            with self.path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f, delimiter=delim)
                fieldnames = list(reader.fieldnames or [])
                rows: List[Dict[str, Any]] = []
                for raw_row in reader:
                    clean_row = {
                        k: _coerce_scalar(v)
                        for k, v in raw_row.items()
                        if k is not None
                    }
                    rows.append(clean_row)
            return {default_name: (rows, fieldnames)}

        if ext in (".jsonl", ".ndjson"):
            rows = []
            with self.path.open("r", encoding="utf-8") as f:
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
        payload = json.loads(self.path.read_text(encoding="utf-8"))
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
