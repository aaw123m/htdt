from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    NativeSchemaError,
    read_native_schema_version,
)
from htdt.cad_scene import make_f1_scene


def test_new_native_database_records_schema_version(tmp_path: Path) -> None:
    path = tmp_path / 'cad.sqlite3'

    repository = SceneRepository(path)
    repository.save(make_f1_scene(), parent_revision_id=None)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            'SELECT schema_version, description '
            'FROM native_schema_migrations ORDER BY schema_version'
        ).fetchall()
    assert rows == [
        (1, 'adopt pre-versioned native CAD schema as baseline v1'),
        (2, 'migrate native schema to v2'),
        (3, 'migrate native schema to v3'),
        (4, 'migrate native schema to v4'),
    ]


def test_pre_versioned_native_database_is_adopted_without_rewriting_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''CREATE TABLE editor_view_states (
                document_id TEXT PRIMARY KEY,
                selected_id TEXT,
                hidden_ids_json TEXT NOT NULL,
                locked_ids_json TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )'''
        )
        connection.execute(
            'INSERT INTO editor_view_states VALUES (?, ?, ?, ?, ?)',
            ('doc', 'speaker-fl', '[]', '[]', '2026-09-17T00:00:00+00:00'),
        )

    repository = SceneRepository(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    state = repository.view_state('doc')
    assert state is not None
    assert state.selected_id == 'speaker-fl'


def test_newer_native_schema_is_rejected_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / 'future.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''CREATE TABLE native_schema_metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version INTEGER NOT NULL
            )'''
        )
        connection.execute(
            'INSERT INTO native_schema_metadata(singleton, schema_version) VALUES (1, ?)',
            (NATIVE_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(NativeSchemaError, match='newer than this application'):
        SceneRepository(path)


def test_unversioned_unrelated_database_is_not_claimed_as_native(tmp_path: Path) -> None:
    path = tmp_path / 'unrelated.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unrelated(value TEXT NOT NULL)')

    with pytest.raises(NativeSchemaError, match='unrelated tables'):
        SceneRepository(path)
