"""
Data source factory and extensible connector registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from sources.base import BaseDataSource, SourceSchema
from sources.sqlite_source import SQLiteDataSource
from sources.tabular_source import TabularDataSource

_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db3"}
_TABULAR_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".ndjson"}


def _looks_like_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


class DataSourceRegistry:
    """Registry for built-in and plugin-supplied data source connectors."""

    def __init__(self) -> None:
        self._custom_schemes: Dict[str, Callable[[str], BaseDataSource]] = {}

    def register_scheme(self, scheme: str, factory: Callable[[str], BaseDataSource]) -> None:
        """Registers a URI scheme connector (e.g. 'postgres', 'duckdb', 'mysql')."""
        clean = scheme.lower().strip().rstrip(":/")
        if not clean:
            raise ValueError("Scheme must not be empty.")
        self._custom_schemes[clean] = factory

    def list_schemes(self) -> List[str]:
        return sorted(["sqlite", "csv", "tsv", "json", "jsonl"] + list(self._custom_schemes.keys()))

    def open(self, target: str | Path) -> BaseDataSource:
        """Opens a local file path or registered URI scheme as a `BaseDataSource`."""
        raw = str(target).strip()
        if "://" in raw and not raw.startswith("file://"):
            scheme = raw.split("://", 1)[0].lower().strip()
            factory = self._custom_schemes.get(scheme)
            if factory is None:
                raise ValueError(
                    f"No data source connector registered for scheme '{scheme}://'. "
                    f"Available: {', '.join(self.list_schemes())}"
                )
            return factory(raw)

        clean_path = raw[len("file://") :] if raw.startswith("file://") else raw
        p = Path(clean_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Data source not found: {target}")

        ext = p.suffix.lower()
        if ext in _SQLITE_SUFFIXES or _looks_like_sqlite(p):
            return SQLiteDataSource(p)
        if ext in _TABULAR_SUFFIXES:
            return TabularDataSource(p)
        raise ValueError(
            f"Could not detect source format for '{p.name}'. "
            "Supported built-in formats: SQLite (.db/.sqlite/.sqlite3), .csv, .tsv, .json, .jsonl."
        )


_DEFAULT_REGISTRY = DataSourceRegistry()


def get_source_registry() -> DataSourceRegistry:
    return _DEFAULT_REGISTRY


def open_source(target: str | Path) -> BaseDataSource:
    """Opens a data source using the default registry."""
    return _DEFAULT_REGISTRY.open(target)


def inspect_source(target: str | Path, sample_rows: int = 200) -> SourceSchema:
    """Opens and inspects a data source."""
    return open_source(target).inspect(sample_rows=sample_rows)
