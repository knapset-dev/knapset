"""
Read-only SQLite data source adapter using Python standard library `sqlite3`.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sources.base import BaseDataSource, SourceSchema, TableSchema, summarize_rows

_READONLY_START_RE = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)
_LEADING_COMMENTS_RE = re.compile(r"^(?:\s*--[^\n]*\n|\s*/\*.*?\*/)*", re.DOTALL)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def validate_readonly_sql(query: str) -> str:
    """Ensures a custom SQL query is a single read-only SELECT/WITH statement."""
    cleaned = _LEADING_COMMENTS_RE.sub("", (query or "")).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        raise ValueError("SQL query must not be empty.")
    if ";" in cleaned:
        raise ValueError("Only a single read-only SELECT/WITH SQL statement is allowed.")
    if not _READONLY_START_RE.match(cleaned):
        raise ValueError("Custom SQL query must start with SELECT or WITH (read-only).")
    return cleaned


class SQLiteDataSource(BaseDataSource):
    """Read-only adapter for existing SQLite databases."""

    source_kind: str = "sqlite"

    def __init__(self, path: str | Path):
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"SQLite database not found: {path}")
        self.path = p

    def _connect_ro(self) -> sqlite3.Connection:
        uri = f"{self.path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _list_tables(self, conn: sqlite3.Connection) -> List[str]:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type = 'view', name
            """
        ).fetchall()
        return [str(r["name"]) for r in rows]

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> Tuple[List[str], Dict[str, str]]:
        info = conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
        col_names = [str(r["name"]) for r in info]
        declared = {str(r["name"]): str(r["type"] or "").upper() for r in info}
        return col_names, declared

    def inspect(self, sample_rows: int = 200) -> SourceSchema:
        with contextlib.closing(self._connect_ro()) as conn:
            table_names = self._list_tables(conn)
            tables: List[TableSchema] = []
            for tname in table_names:
                q_tname = _quote_ident(tname)
                count_row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {q_tname}").fetchone()
                total_rows = int(count_row["cnt"]) if count_row else 0
                col_names, declared = self._table_columns(conn, tname)
                sample_q = f"SELECT * FROM {q_tname} LIMIT ?"
                fetched = [dict(r) for r in conn.execute(sample_q, (max(1, int(sample_rows)),)).fetchall()]
                schema = summarize_rows(
                    table_name=tname,
                    rows=fetched,
                    col_order=col_names,
                    row_count_override=total_rows,
                )
                # Refine unknown/empty columns using declared SQLite affinity
                for col_info in schema.columns:
                    if col_info.kind == "unknown":
                        decl = declared.get(col_info.name, "")
                        if any(tok in decl for tok in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")):
                            col_info.kind = "number"
                        elif "BOOL" in decl:
                            col_info.kind = "boolean"
                        elif any(tok in decl for tok in ("CHAR", "TEXT", "CLOB")):
                            col_info.kind = "text"
                tables.append(schema)
            return SourceSchema(location=str(self.path), kind=self.source_kind, tables=tables)

    def load_rows(
        self,
        table: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with contextlib.closing(self._connect_ro()) as conn:
            if query:
                sql = validate_readonly_sql(query)
                if limit is not None and limit > 0:
                    sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
                return [dict(r) for r in conn.execute(sql).fetchall()]

            available = self._list_tables(conn)
            if not available:
                return []

            target = table
            if not target:
                if len(available) == 1:
                    target = available[0]
                else:
                    raise ValueError(
                        f"Multiple tables found in '{self.path.name}' ({', '.join(available)}). "
                        "Specify --table <name> or --query <SQL>."
                    )
            elif target not in available:
                raise ValueError(
                    f"Table '{target}' not found in '{self.path.name}'. Available: {', '.join(available)}"
                )

            sql = f"SELECT * FROM {_quote_ident(target)}"
            if limit is not None and limit > 0:
                sql += f" LIMIT {int(limit)}"
            return [dict(r) for r in conn.execute(sql).fetchall()]
