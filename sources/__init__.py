"""
Knapset v2 read-only data source adapters.
"""

from sources.base import BaseDataSource, ColumnInfo, SourceSchema, TableSchema, summarize_rows
from sources.registry import DataSourceRegistry, get_source_registry, inspect_source, open_source
from sources.sqlite_source import SQLiteDataSource, validate_readonly_sql
from sources.tabular_source import TabularDataSource

__all__ = [
    "BaseDataSource",
    "ColumnInfo",
    "DataSourceRegistry",
    "SQLiteDataSource",
    "SourceSchema",
    "TableSchema",
    "TabularDataSource",
    "get_source_registry",
    "inspect_source",
    "open_source",
    "summarize_rows",
    "validate_readonly_sql",
]
