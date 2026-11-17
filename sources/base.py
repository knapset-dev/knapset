"""
Base interfaces, schema metadata models, and shared column profiling helpers
for Knapset v2 read-only data sources.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class ColumnInfo:
    """Inferred metadata and summary statistics for a single column/field."""

    name: str
    kind: str = "text"  # "number" | "boolean" | "text" | "unknown"
    non_null_count: int = 0
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    sample_values: List[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "non_null_count": self.non_null_count,
            "min": self.min_val,
            "max": self.max_val,
            "samples": list(self.sample_values),
        }


@dataclass
class TableSchema:
    """Schema summary for a single table, sheet, or JSON collection."""

    name: str
    row_count: int
    columns: List[ColumnInfo] = field(default_factory=list)

    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def numeric_columns(self) -> List[str]:
        return [c.name for c in self.columns if c.kind == "number"]

    def text_columns(self) -> List[str]:
        return [c.name for c in self.columns if c.kind == "text"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "columns": [c.to_dict() for c in self.columns],
        }


@dataclass
class SourceSchema:
    """Top-level schema metadata returned by `BaseDataSource.inspect()`."""

    location: str
    kind: str
    tables: List[TableSchema] = field(default_factory=list)

    def table_names(self) -> List[str]:
        return [t.name for t in self.tables]

    def get_table(self, name: str) -> Optional[TableSchema]:
        for t in self.tables:
            if t.name == name:
                return t
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "location": self.location,
            "kind": self.kind,
            "tables": [t.to_dict() for t in self.tables],
        }


def infer_column_kind(values: Sequence[Any]) -> str:
    """Infers column kind ('number', 'boolean', 'text', or 'unknown') from non-null values."""
    non_null = [v for v in values if v is not None and v != ""]
    if not non_null:
        return "unknown"
    if all(isinstance(v, bool) for v in non_null):
        return "boolean"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null):
        return "number"
    return "text"


def summarize_rows(
    table_name: str,
    rows: Sequence[Dict[str, Any]],
    col_order: Optional[Sequence[str]] = None,
    row_count_override: Optional[int] = None,
) -> TableSchema:
    """Profiles a sequence of row dicts into a `TableSchema` with min/max and samples."""
    seen: List[str] = list(col_order) if col_order else []
    seen_set = set(seen)
    for row in rows:
        if isinstance(row, dict):
            for k in row.keys():
                if k not in seen_set:
                    seen.append(k)
                    seen_set.add(k)

    columns: List[ColumnInfo] = []
    for col in seen:
        vals = [r.get(col) for r in rows if isinstance(r, dict)]
        non_null = [v for v in vals if v is not None and v != ""]
        kind = infer_column_kind(non_null)
        min_v: Optional[float] = None
        max_v: Optional[float] = None
        if kind == "number" and non_null:
            nums = [float(v) for v in non_null]
            min_v = min(nums)
            max_v = max(nums)

        samples: List[Any] = []
        sample_seen = set()
        for v in non_null:
            key = repr(v)
            if key not in sample_seen:
                sample_seen.add(key)
                samples.append(v)
            if len(samples) >= 3:
                break

        columns.append(
            ColumnInfo(
                name=col,
                kind=kind,
                non_null_count=len(non_null),
                min_val=min_v,
                max_val=max_v,
                sample_values=samples,
            )
        )

    total = row_count_override if row_count_override is not None else len(rows)
    return TableSchema(name=table_name, row_count=total, columns=columns)


class BaseDataSource(ABC):
    """Abstract contract for read-only Knapset v2 data sources."""

    source_kind: str = "generic"

    @abstractmethod
    def inspect(self, sample_rows: int = 200) -> SourceSchema:
        """Inspects tables/collections and infers column metadata."""

    @abstractmethod
    def load_rows(
        self,
        table: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Loads rows as dictionaries from a table/collection or read-only query."""
