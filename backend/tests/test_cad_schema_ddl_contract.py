"""Structural contract tests for the versioned schema authority.

``cad_schema_ddl`` is the single declaration point for every table, index
and lazy column the native database may contain (#302), and
``NATIVE_SCHEMA_TABLES`` is the enumeration diagnostics and invariant
checks rely on. These tests pin the contract so the two cannot drift apart
silently: every table the baseline creates must be registered, every
registered table must be creatable by the migration chain, and lazy column
ensures must only target declared tables.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

import pytest

from htdt.cad_schema import ensure_native_schema
from htdt.cad_schema_ddl import (
    NATIVE_BASELINE_DDL,
    NATIVE_COLUMN_ENSURES,
    NATIVE_SCHEMA_TABLES,
)

_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\']?(\w+)', re.IGNORECASE
)


def _baseline_table_names() -> set[str]:
    names: set[str] = set()
    for statement in NATIVE_BASELINE_DDL:
        match = _CREATE_TABLE_RE.search(statement)
        if match is not None:
            names.add(match.group(1))
    return names


def test_baseline_ddl_only_creates_declared_tables() -> None:
    declared = set(NATIVE_SCHEMA_TABLES)
    unregistered = _baseline_table_names() - declared
    assert unregistered == set(), (
        'baseline DDL creates tables missing from NATIVE_SCHEMA_TABLES: '
        f'{sorted(unregistered)}'
    )


def test_every_declared_table_is_created_by_migration(tmp_path: Path) -> None:
    """The full migration chain must materialize every registered table —
    a registered name that no step creates is a contract lie."""
    db_path = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    missing = set(NATIVE_SCHEMA_TABLES) - existing
    assert missing == set(), (
        'NATIVE_SCHEMA_TABLES lists tables the migration never creates: '
        f'{sorted(missing)}'
    )


def test_column_ensures_only_target_declared_tables() -> None:
    declared = set(NATIVE_SCHEMA_TABLES)
    stray = {table for table, _, _ in NATIVE_COLUMN_ENSURES} - declared
    assert stray == set(), (
        'NATIVE_COLUMN_ENSURES references undeclared tables: '
        f'{sorted(stray)}'
    )


def test_schema_tables_registry_has_no_duplicates() -> None:
    assert len(NATIVE_SCHEMA_TABLES) == len(set(NATIVE_SCHEMA_TABLES)), (
        'NATIVE_SCHEMA_TABLES contains duplicate entries'
    )


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running the finished migration must converge without error and
    without changing the recorded schema version."""
    from htdt.cad_schema import read_native_schema_version

    db_path = tmp_path / 'cad.sqlite3'
    first = ensure_native_schema(db_path)
    second = ensure_native_schema(db_path)
    assert first == second
    assert read_native_schema_version(db_path) == first
