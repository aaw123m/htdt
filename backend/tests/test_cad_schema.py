from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import (
    NATIVE_SCHEMA_VERSION,
    NativeSchemaError,
    check_native_schema_compatibility,
    ensure_native_schema,
    read_native_schema_version,
)
from htdt.cad_schema_ddl import NATIVE_SCHEMA_TABLES
from htdt.cad_scene import make_f1_scene


# The exact CREATE TABLE DDL that pre-versioning (0.1.0-era) releases ran in
# the native CAD database. Kept independent of htdt.cad_schema so the test
# proves the adoption gate accepts the shapes real legacy releases produced.
_LEGACY_DDL = (
    '''CREATE TABLE scene_revisions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        parent_revision_id TEXT,
        created_at_utc TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
    )''',
    '''CREATE TABLE scene_recovery_snapshots (
        document_id TEXT PRIMARY KEY,
        source_revision_id TEXT,
        updated_at_utc TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(source_revision_id) REFERENCES scene_revisions(revision_id)
    )''',
    '''CREATE TABLE editor_view_states (
        document_id TEXT PRIMARY KEY,
        selected_id TEXT,
        hidden_ids_json TEXT NOT NULL,
        locked_ids_json TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        selected_ids_json TEXT NOT NULL DEFAULT '[]'
    )''',
    '''CREATE TABLE cad_adaptive_plans (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        validation_id TEXT NOT NULL,
        execution_scope TEXT NOT NULL,
        selected_candidate_id TEXT NOT NULL,
        adaptive_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
    '''CREATE TABLE cad_constraint_workspaces (
        document_id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        updated_at_utc TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )''',
    '''CREATE TABLE cad_extended_model_capabilities (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        capability_id TEXT NOT NULL UNIQUE,
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        evidence_scope TEXT NOT NULL,
        capability_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL
    )''',
    '''CREATE TABLE cad_extended_search_specs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        extended_search_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        base_search_spec_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        extended_search_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(base_search_spec_id)
            REFERENCES cad_search_specs(search_spec_id),
        FOREIGN KEY(capability_id)
            REFERENCES cad_extended_model_capabilities(capability_id)
    )''',
    '''CREATE TABLE cad_measurement_assets (
        sha256 TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL
    )''',
    '''CREATE TABLE cad_measurements (
        measurement_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
        scene_content_hash TEXT NOT NULL,
        measurement_entity_id TEXT NOT NULL,
        measurement_position_json TEXT NOT NULL,
        measurement_direction_json TEXT,
        evidence_type TEXT NOT NULL,
        channel_role TEXT NOT NULL,
        source_speaker_ids_json TEXT NOT NULL,
        radiation_scope TEXT NOT NULL,
        routing_evidence TEXT NOT NULL,
        captured_at TEXT,
        imported_at TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        external_source_id TEXT,
        quality_status TEXT NOT NULL,
        quality_reasons_json TEXT NOT NULL,
        quality_source TEXT NOT NULL,
        provenance_json TEXT NOT NULL
    )''',
    '''CREATE TABLE cad_frequency_responses (
        dataset_id TEXT PRIMARY KEY,
        measurement_id TEXT NOT NULL UNIQUE REFERENCES cad_measurements(measurement_id),
        frequency_blob BLOB NOT NULL,
        level_blob BLOB NOT NULL,
        phase_blob BLOB,
        phase_status TEXT NOT NULL,
        level_reference TEXT NOT NULL,
        smoothing TEXT,
        processing_json TEXT NOT NULL,
        source_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256),
        importer_version TEXT NOT NULL
    )''',
    '''CREATE TABLE cad_measurement_comparisons (
        comparison_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        dataset_a_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id),
        dataset_b_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id),
        scene_revision_a_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
        scene_revision_b_id TEXT NOT NULL REFERENCES scene_revisions(revision_id),
        created_at TEXT NOT NULL,
        result_json TEXT NOT NULL
    )''',
    '''CREATE TABLE cad_measurement_plans (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        applied_scene_revision_id TEXT NOT NULL,
        status TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(applied_scene_revision_id) REFERENCES scene_revisions(revision_id)
    )''',
    '''CREATE TABLE cad_model_validations (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        validation_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        recommendation_gate TEXT NOT NULL,
        validation_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
    '''CREATE TABLE cad_objective_evaluations (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluation_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL,
        scene_content_hash TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        search_spec_sha256 TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        evaluation_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
    '''CREATE TABLE cad_pareto_sets (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        pareto_set_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL,
        scene_content_hash TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        search_spec_sha256 TEXT NOT NULL,
        pareto_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
    '''CREATE TABLE cad_prediction_results (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL,
        scene_content_hash TEXT NOT NULL,
        constraint_workspace_hash TEXT,
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        result_kind TEXT NOT NULL,
        geometry_compatibility TEXT NOT NULL,
        parameters_json TEXT NOT NULL,
        input_snapshot_json TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        submitted_at_utc TEXT NOT NULL,
        completed_at_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        assumptions_json TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        modes_json TEXT NOT NULL,
        reflections_json TEXT NOT NULL,
        FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id)
    )''',
    '''CREATE TABLE cad_roomsim_batch_specs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_run_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL,
        scene_content_hash TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        search_spec_sha256 TEXT NOT NULL,
        candidate_set_sha256 TEXT NOT NULL,
        batch_spec_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id),
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
    '''CREATE TABLE cad_roomsim_candidate_attempts (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL UNIQUE,
        batch_run_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        attempt_index INTEGER NOT NULL,
        status TEXT NOT NULL,
        attempt_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        completed_at_utc TEXT NOT NULL,
        UNIQUE(batch_run_id, candidate_id, attempt_index),
        FOREIGN KEY(batch_run_id) REFERENCES cad_roomsim_batch_specs(batch_run_id)
    )''',
    '''CREATE TABLE cad_search_specs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        search_spec_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        scene_revision_id TEXT NOT NULL,
        scene_content_hash TEXT NOT NULL,
        constraint_workspace_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        search_spec_sha256 TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(scene_revision_id) REFERENCES scene_revisions(revision_id)
    )''',
    '''CREATE TABLE cad_validation_campaigns (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        search_spec_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        candidate_set_sha256 TEXT NOT NULL,
        campaign_sha256 TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY(search_spec_id) REFERENCES cad_search_specs(search_spec_id)
    )''',
)


def _create_database(path: Path, *statements: str) -> bytes:
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
    return path.read_bytes()


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
        (5, 'migrate native schema to v5'),
        (6, 'migrate native schema to v6'),
        (7, 'migrate native schema to v7'),
        (8, 'migrate native schema to v8'),
        (9, 'migrate native schema to v9'),
        (10, 'migrate native schema to v10'),
        (11, 'migrate native schema to v11'),
        (12, 'migrate native schema to v12'),
        (13, 'migrate native schema to v13'),
        (14, 'migrate native schema to v14'),
        (15, 'migrate native schema to v15'),
        (16, 'migrate native schema to v16'),
        (17, 'migrate native schema to v17'),
        (18, 'migrate native schema to v18'),
        (19, 'migrate native schema to v19'),
        (20, 'migrate native schema to v20'),
        (21, 'migrate native schema to v21'),
        (22, 'migrate native schema to v22'),
        (23, 'migrate native schema to v23'),
        (24, 'migrate native schema to v24'),
        (25, 'migrate native schema to v25'),
        (26, 'migrate native schema to v26'),
        (27, 'migrate native schema to v27'),
        (28, 'migrate native schema to v28'),
        (29, 'migrate native schema to v29'),
        (30, 'migrate native schema to v30'),
        (31, 'migrate native schema to v31'),
        (32, 'migrate native schema to v32'),
        (33, 'migrate native schema to v33'),
        (34, 'migrate native schema to v34'),
        (35, 'migrate native schema to v35'),
        (36, 'migrate native schema to v36'),
        (37, 'migrate native schema to v37'),
        (38, 'migrate native schema to v38'),
        (39, 'migrate native schema to v39'),
        (40, 'migrate native schema to v40'),
        (41, 'migrate native schema to v41'),
        (42, 'migrate native schema to v42'),
        (43, 'migrate native schema to v43'),
        (44, 'migrate native schema to v44'),
        (45, 'migrate native schema to v45'),
        (46, 'migrate native schema to v46'),
        (47, 'migrate native schema to v47'),
        (48, 'migrate native schema to v48'),
        (49, 'migrate native schema to v49'),
        (50, 'migrate native schema to v50'),
        (51, 'migrate native schema to v51'),
        (52, 'migrate native schema to v52'),
        (53, 'migrate native schema to v53'),
        (54, 'migrate native schema to v54'),
        (55, 'migrate native schema to v55'),
        (56, 'migrate native schema to v56'),
        (57, 'migrate native schema to v57'),
        (58, 'migrate native schema to v58'),
        (59, 'migrate native schema to v59'),
        (60, 'migrate native schema to v60'),
        (61, 'migrate native schema to v61'),
        (62, 'migrate native schema to v62'),
        (63, 'migrate native schema to v63'),
        (64, 'migrate native schema to v64'),
        (65, 'migrate native schema to v65'),
        (66, 'migrate native schema to v66'),
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


def test_unversioned_database_with_only_unknown_cad_table_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'cad-only.sqlite3'
    before = _create_database(
        path,
        'CREATE TABLE cad_notes(note TEXT NOT NULL)',
        "INSERT INTO cad_notes VALUES ('not a htdt table')",
    )

    with pytest.raises(NativeSchemaError, match='unrelated tables'):
        SceneRepository(path)

    assert path.read_bytes() == before


def test_unversioned_database_mixing_legacy_and_unknown_cad_tables_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'mixed.sqlite3'
    before = _create_database(
        path,
        _LEGACY_DDL[0],
        'CREATE TABLE cad_notes(note TEXT NOT NULL)',
    )

    with pytest.raises(NativeSchemaError, match='unrelated tables'):
        SceneRepository(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    'ddl',
    (
        # recognized name, missing required column
        '''CREATE TABLE scene_revisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            parent_revision_id TEXT,
            created_at_utc TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
        )''',
        # recognized name, wrong column type
        '''CREATE TABLE scene_revisions (
            seq TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            parent_revision_id TEXT,
            created_at_utc TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
        )''',
        # recognized name, missing UNIQUE constraint
        '''CREATE TABLE scene_revisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            parent_revision_id TEXT,
            created_at_utc TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
        )''',
        # recognized name, missing foreign key
        '''CREATE TABLE scene_revisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            parent_revision_id TEXT,
            created_at_utc TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )''',
        # recognized name, unexpected extra column
        '''CREATE TABLE scene_revisions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            parent_revision_id TEXT,
            created_at_utc TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            injected TEXT,
            FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
        )''',
    ),
    ids=(
        'missing-column',
        'wrong-column-type',
        'missing-unique-constraint',
        'missing-foreign-key',
        'extra-column',
    ),
)
def test_unversioned_recognized_table_with_wrong_shape_is_rejected(
    tmp_path: Path,
    ddl: str,
) -> None:
    path = tmp_path / 'malformed.sqlite3'
    before = _create_database(path, ddl)

    with pytest.raises(NativeSchemaError, match='pre-versioning signatures'):
        SceneRepository(path)

    assert path.read_bytes() == before


def test_unversioned_database_missing_referenced_legacy_table_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'dangling-fk.sqlite3'
    # cad_measurements only ever existed beside scene_revisions; a database
    # containing the table without its foreign-key target is not adoptable.
    before = _create_database(path, _LEGACY_DDL[8])

    with pytest.raises(NativeSchemaError, match='missing referenced tables'):
        SceneRepository(path)

    assert path.read_bytes() == before


def test_unversioned_database_with_only_migration_table_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'migrations-only.sqlite3'
    before = _create_database(
        path,
        '''CREATE TABLE native_schema_migrations (
            schema_version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL,
            description TEXT NOT NULL
        )''',
    )

    with pytest.raises(NativeSchemaError, match='unrelated tables'):
        SceneRepository(path)

    assert path.read_bytes() == before


def test_pre_versioned_database_with_full_legacy_table_set_is_adopted(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'legacy-full.sqlite3'
    with sqlite3.connect(path) as connection:
        for statement in _LEGACY_DDL:
            connection.execute(statement)
        connection.execute(
            '''INSERT INTO scene_revisions(
                revision_id, document_id, parent_revision_id,
                created_at_utc, content_hash, payload_json
            ) VALUES ('rev-1', 'doc-1', NULL, '2026-09-17T00:00:00+00:00', 'hash', '{}')'''
        )

    repository = SceneRepository(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    assert repository.get(revision.revision_id) is not None
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            'SELECT revision_id, content_hash FROM scene_revisions ORDER BY seq'
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert rows[0] == ('rev-1', 'hash')
    assert {'scene_revisions', 'cad_search_specs', 'cad_roomsim_candidate_attempts'} <= tables


def test_pre_versioned_database_with_legacy_subset_is_adopted(tmp_path: Path) -> None:
    path = tmp_path / 'legacy-subset.sqlite3'
    _create_database(
        path,
        _LEGACY_DDL[0],
        _LEGACY_DDL[1],
        '''CREATE TABLE editor_view_states (
            document_id TEXT PRIMARY KEY,
            selected_id TEXT,
            hidden_ids_json TEXT NOT NULL,
            locked_ids_json TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )''',
        _LEGACY_DDL[4],
    )

    SceneRepository(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION


def test_unversioned_empty_database_is_adopted(tmp_path: Path) -> None:
    path = tmp_path / 'empty.sqlite3'
    _create_database(path, 'CREATE TABLE dropped(value TEXT)', 'DROP TABLE dropped')

    SceneRepository(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION


def test_check_native_schema_compatibility_rejects_unversioned_foreign_db(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'foreign.sqlite3'
    before = _create_database(path, 'CREATE TABLE cad_notes(note TEXT NOT NULL)')

    with pytest.raises(NativeSchemaError, match='unrelated tables'):
        check_native_schema_compatibility(path)

    assert path.read_bytes() == before


def test_check_native_schema_compatibility_accepts_genuine_legacy_db(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'legacy.sqlite3'
    _create_database(path, _LEGACY_DDL[0], _LEGACY_DDL[4])

    assert check_native_schema_compatibility(path) == 0


def test_legacy_prediction_table_with_lazy_result_identity_column_is_adopted(
    tmp_path: Path,
) -> None:
    """result_sha256 is an optional lazy-migration column on legacy rows."""
    path = tmp_path / 'legacy-predictions.sqlite3'
    _create_database(
        path,
        _LEGACY_DDL[0],  # scene_revisions (foreign-key target)
        _LEGACY_DDL[15],  # cad_prediction_results without result_sha256
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            'ALTER TABLE cad_prediction_results ADD COLUMN result_sha256 TEXT'
        )

    assert check_native_schema_compatibility(path) == 0
    SceneRepository(path)
    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION


def test_legacy_frequency_response_table_gains_lazy_identity_columns(
    tmp_path: Path,
) -> None:
    """dataset_sha256/transformation_sha256 are optional lazy-migration columns.

    A pre-versioning database may lack them entirely; adoption accepts the
    table and ``CadMeasurementRepository`` then appends both lazily. Rows
    written before the columns existed keep NULL and are non-authoritative.
    """
    path = tmp_path / 'legacy-fr.sqlite3'
    _create_database(
        path,
        _LEGACY_DDL[0],  # scene_revisions (foreign-key target)
        _LEGACY_DDL[7],  # cad_measurement_assets (foreign-key target)
        _LEGACY_DDL[8],  # cad_measurements (foreign-key target)
        _LEGACY_DDL[9],  # cad_frequency_responses without identity columns
    )

    assert check_native_schema_compatibility(path) == 0
    scene_repository = SceneRepository(path)
    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION

    CadMeasurementRepository(scene_repository)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(cad_frequency_responses)'
            )
        }
    assert {'dataset_sha256', 'transformation_sha256'} <= columns


# --- stamped intermediate-version fixtures --------------------------------
# Rewinding a current database's stamp (as test_native_upgrade's helper
# does) never exercises the migrations against era-accurate table shapes —
# a stamped-v10 DB already has every table. These fixtures rebuild the
# shapes real earlier releases left on disk.

_SCHEMA_AUTHORITY_DDL = (
    '''CREATE TABLE native_schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        schema_version INTEGER NOT NULL
    )''',
    '''CREATE TABLE native_schema_migrations (
        schema_version INTEGER PRIMARY KEY,
        applied_at_utc TEXT NOT NULL,
        description TEXT NOT NULL
    )''',
)

#: The lifecycle-registry tables a v5/v6-era build created before #764
#: folded them into the canonical ``htdt_project_*`` authority at v7.
_V6_PROJECT_REGISTRY_DDL = (
    '''CREATE TABLE project_registry (
        project_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        description TEXT,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        last_opened_at_utc TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        archived_at_utc TEXT,
        cloned_from_project_id TEXT
    )''',
    '''CREATE TABLE project_tombstones (
        tombstone_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL UNIQUE,
        document_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        deleted_at_utc TEXT NOT NULL,
        removed_rows INTEGER NOT NULL,
        estimated_bytes INTEGER NOT NULL,
        authorities_json TEXT NOT NULL
    )''',
)


def _stamp_native_version(path: Path, version: int) -> None:
    """Rewind a database's stamp the way an earlier release left it."""
    with sqlite3.connect(path) as connection, connection:
        connection.execute(
            'UPDATE native_schema_metadata SET schema_version = ?',
            (version,),
        )
        connection.execute(
            'DELETE FROM native_schema_migrations WHERE schema_version >= ?',
            (version + 1,),
        )


def test_v1_stamped_database_migrates_through_every_step(tmp_path: Path) -> None:
    """A v1-era database (signature table set + v1 stamp) replays the whole
    migration chain: intermediate versions create their era tables and the
    baseline replay converges every registered table."""
    path = tmp_path / 'v1.sqlite3'
    _create_database(path, *_LEGACY_DDL)
    with sqlite3.connect(path) as connection, connection:
        for statement in _SCHEMA_AUTHORITY_DDL:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO native_schema_metadata VALUES (1, 1)'
        )
        connection.execute(
            'INSERT INTO native_schema_migrations VALUES (1, ?, ?)',
            (
                '2026-09-18T00:00:00+00:00',
                'adopt pre-versioned native CAD schema as baseline v1',
            ),
        )
        connection.execute(
            'INSERT INTO scene_revisions('
            'revision_id, document_id, parent_revision_id, created_at_utc, '
            'content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)',
            ('r1', 'doc-a', None, '2026-01-01T00:00:00+00:00', 'h1', '{}'),
        )

    ensure_native_schema(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                'SELECT schema_version FROM native_schema_migrations '
                'ORDER BY schema_version'
            )
        ]
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        lineage_columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(scene_revisions)'
            )
        }
    assert versions == list(range(1, NATIVE_SCHEMA_VERSION + 1))
    assert set(NATIVE_SCHEMA_TABLES) <= names
    # The v5 lineage columns were converged even though the era table
    # predates them, and the pre-existing revision became the head.
    assert {'detached', 'detached_reason'} <= lineage_columns
    heads = sqlite3.connect(path).execute(
        'SELECT head_revision_id FROM scene_document_heads '
        'WHERE document_id=?',
        ('doc-a',),
    ).fetchone()
    assert heads == ('r1',)


def test_v4_stamped_revisions_backfill_heads_and_mark_branch_detached(
    tmp_path: Path,
) -> None:
    """The v4→v5 data derivation: scene_document_heads is backfilled from
    existing lineage — the mainline chain becomes head and a pre-#626
    branch row is marked detached rather than silently winning."""
    path = tmp_path / 'v4.sqlite3'
    _create_database(path, *_LEGACY_DDL)
    with sqlite3.connect(path) as connection, connection:
        for statement in _SCHEMA_AUTHORITY_DDL:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO native_schema_metadata VALUES (1, 4)'
        )
        for version in (1, 2, 3, 4):
            connection.execute(
                'INSERT INTO native_schema_migrations VALUES (?, ?, ?)',
                (
                    version,
                    '2026-09-18T00:00:00+00:00',
                    f'migrate native schema to v{version}',
                ),
            )
        revisions = (
            # r1 -> r2 is the mainline chain; r3 is a pre-#626 branch off r1.
            ('r1', 'doc-a', None, '2026-01-01T00:00:00+00:00', 'h1'),
            ('r2', 'doc-a', 'r1', '2026-01-02T00:00:00+00:00', 'h2'),
            ('r3', 'doc-a', 'r1', '2026-01-03T00:00:00+00:00', 'h3'),
        )
        connection.executemany(
            'INSERT INTO scene_revisions('
            'revision_id, document_id, parent_revision_id, created_at_utc, '
            'content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)',
            [(rid, did, pid, ts, ch, '{}') for rid, did, pid, ts, ch in revisions],
        )

    ensure_native_schema(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        head = connection.execute(
            'SELECT head_revision_id, generation FROM scene_document_heads '
            'WHERE document_id=?',
            ('doc-a',),
        ).fetchone()
        detached = dict(
            connection.execute(
                'SELECT revision_id, detached FROM scene_revisions'
            ).fetchall()
        )
    assert head == ('r2', 2)
    assert detached == {'r1': 0, 'r2': 0, 'r3': 1}


def test_v6_project_registry_rows_fold_into_htdt_authority(
    tmp_path: Path,
) -> None:
    """The only row-moving migration: v6→v7 folds the pre-merge lifecycle
    registry into htdt_project_* and drops the era tables. Status maps to
    the archived flag, clone lineage is preserved, and a project already
    registered in the canonical store wins the collision."""
    path = tmp_path / 'v6.sqlite3'
    SceneRepository(path)
    with sqlite3.connect(path) as connection, connection:
        for statement in _V6_PROJECT_REGISTRY_DDL:
            connection.execute(statement)
        connection.executemany(
            'INSERT INTO project_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (
                    'p-active', 'd-active', 'Listening Room', 'desc',
                    '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00',
                    '2026-01-03T00:00:00+00:00', 'active', None, None,
                ),
                (
                    'p-arch', 'd-arch', 'Old Room', None,
                    '2026-02-01T00:00:00+00:00', '2026-02-02T00:00:00+00:00',
                    None, 'archived', '2026-02-03T00:00:00+00:00', 'p-active',
                ),
                # Collides with the canonical row inserted below — the
                # registry row must lose, never overwrite.
                (
                    'p-clone', 'd-clone', 'Registry Name', None,
                    '2026-04-01T00:00:00+00:00', '2026-04-02T00:00:00+00:00',
                    None, 'active', None, None,
                ),
            ],
        )
        connection.execute(
            'INSERT INTO project_tombstones VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                't-1', 'p-gone', 'd-gone', 'Deleted Room',
                '2026-03-01T00:00:00+00:00', 12, 3456, '{"a":1}',
            ),
        )
        connection.execute(
            'INSERT INTO htdt_project_documents('
            'project_id, document_id, display_name, created_at_utc, '
            'updated_at_utc) VALUES (?, ?, ?, ?, ?)',
            (
                'p-canonical', 'd-clone', 'Canonical Name',
                '2026-04-03T00:00:00+00:00', '2026-04-03T00:00:00+00:00',
            ),
        )
    _stamp_native_version(path, 6)

    ensure_native_schema(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        projects = {
            row[0]: row[1:]
            for row in connection.execute(
                'SELECT project_id, document_id, display_name, archived, '
                'archived_at_utc, cloned_from_project_id, updated_at_utc '
                'FROM htdt_project_documents'
            )
        }
        tombstones = connection.execute(
            'SELECT * FROM htdt_project_tombstones'
        ).fetchall()
    assert 'project_registry' not in tables
    assert 'project_tombstones' not in tables
    assert projects['p-active'] == (
        'd-active', 'Listening Room', 0, None, None,
        '2026-01-02T00:00:00+00:00',
    )
    assert projects['p-arch'] == (
        'd-arch', 'Old Room', 1, '2026-02-03T00:00:00+00:00', 'p-active',
        '2026-02-02T00:00:00+00:00',
    )
    # The canonical registration survives; the colliding registry row is
    # dropped with its source table rather than overwriting it.
    assert projects['p-canonical'] == (
        'd-clone', 'Canonical Name', 0, None, None,
        '2026-04-03T00:00:00+00:00',
    )
    assert 'p-clone' not in projects
    assert tombstones == [
        (
            't-1', 'p-gone', 'd-gone', 'Deleted Room',
            '2026-03-01T00:00:00+00:00', 12, 3456, '{"a":1}',
        )
    ]


def test_v9_stamped_table_missing_ensured_column_is_converged(
    tmp_path: Path,
) -> None:
    """A stamped v9-era database whose lazily created table predates a
    column-ensure keeps its rows; the baseline replay + ensure converges
    the missing column instead of erroring."""
    path = tmp_path / 'v9.sqlite3'
    SceneRepository(path)
    with sqlite3.connect(path) as connection, connection:
        connection.execute('DROP TABLE editor_view_states')
        connection.execute(
            '''CREATE TABLE editor_view_states (
                document_id TEXT PRIMARY KEY,
                selected_id TEXT,
                hidden_ids_json TEXT NOT NULL,
                locked_ids_json TEXT NOT NULL,
                selected_ids_json TEXT NOT NULL DEFAULT '[]',
                updated_at_utc TEXT NOT NULL
            )'''
        )
        connection.execute(
            'INSERT INTO editor_view_states('
            'document_id, selected_id, hidden_ids_json, locked_ids_json, '
            "selected_ids_json, updated_at_utc) "
            "VALUES ('doc-a', 'speaker-fl', '[]', '[]', '[]', '2026-01-01T00:00:00+00:00')",
        )
    _stamp_native_version(path, 9)

    ensure_native_schema(path)

    assert read_native_schema_version(path) == NATIVE_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(editor_view_states)'
            )
        }
        row = connection.execute(
            'SELECT document_id, selected_id, snap_json '
            'FROM editor_view_states'
        ).fetchone()
    assert 'snap_json' in columns
    assert row == ('doc-a', 'speaker-fl', None)
