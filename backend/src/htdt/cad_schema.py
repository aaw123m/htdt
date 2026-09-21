from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .content_blobs import CONTENT_BLOB_DDL


NATIVE_SCHEMA_VERSION = 4

_METADATA_TABLE = 'native_schema_metadata'
_MIGRATION_TABLE = 'native_schema_migrations'


@dataclass(frozen=True)
class _LegacyTableSignature:
    """Expected on-disk shape of a table a pre-versioning release could create.

    ``columns`` are ordered ``PRAGMA table_info`` rows without ``cid``:
    ``(name, declared type, not-null flag, default value, primary-key flag)``.
    ``foreign_keys`` are ``(column, referenced table, referenced column)``
    triples and ``unique_sets`` are the column sets protected by UNIQUE
    constraints. ``optional_columns`` names columns appended by lazy
    pre-versioning ``ALTER TABLE`` migrations: a legacy database may lack them
    but must match the signature exactly when they are present.
    """

    columns: tuple[tuple[str, str, int, str | None, int], ...]
    foreign_keys: frozenset[tuple[str, str, str]]
    unique_sets: frozenset[frozenset[str]]
    optional_columns: frozenset[str] = frozenset()


# The complete set of tables pre-versioning (0.1.0-era) releases could create
# in the native CAD database, with the exact signatures their CREATE TABLE DDL
# produced. Grounded in the repository-local DDL that predates the schema
# authority (backend/src/htdt/*repository*.py before schema version 1 was
# introduced). Post-versioning repositories always stamp the database before
# writing, so an unversioned database containing anything else is not a HTDT
# native database.
_LEGACY_TABLE_SIGNATURES = {
    'cad_adaptive_plans': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('plan_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('validation_id', 'TEXT', 1, None, 0),
            ('execution_scope', 'TEXT', 1, None, 0),
            ('selected_candidate_id', 'TEXT', 1, None, 0),
            ('adaptive_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'adaptive_sha256'}),
            frozenset({'plan_id'}),
        }),
    ),
    'cad_constraint_workspaces': _LegacyTableSignature(
        columns=(
            ('document_id', 'TEXT', 0, None, 1),
            ('schema_version', 'INTEGER', 1, None, 0),
            ('updated_at_utc', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset(),
        unique_sets=frozenset(),
    ),
    'cad_extended_model_capabilities': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('capability_id', 'TEXT', 1, None, 0),
            ('model_id', 'TEXT', 1, None, 0),
            ('model_version', 'TEXT', 1, None, 0),
            ('evidence_scope', 'TEXT', 1, None, 0),
            ('capability_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset(),
        unique_sets=frozenset({
            frozenset({'capability_id'}),
            frozenset({'capability_sha256'}),
        }),
    ),
    'cad_extended_search_specs': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('extended_search_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('base_search_spec_id', 'TEXT', 1, None, 0),
            ('capability_id', 'TEXT', 1, None, 0),
            ('extended_search_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('base_search_spec_id', 'cad_search_specs', 'search_spec_id'),
            ('capability_id', 'cad_extended_model_capabilities', 'capability_id'),
        }),
        unique_sets=frozenset({
            frozenset({'extended_search_id'}),
            frozenset({'extended_search_sha256'}),
        }),
    ),
    'cad_frequency_responses': _LegacyTableSignature(
        columns=(
            ('dataset_id', 'TEXT', 0, None, 1),
            ('measurement_id', 'TEXT', 1, None, 0),
            ('frequency_blob', 'BLOB', 1, None, 0),
            ('level_blob', 'BLOB', 1, None, 0),
            ('phase_blob', 'BLOB', 0, None, 0),
            ('phase_status', 'TEXT', 1, None, 0),
            ('level_reference', 'TEXT', 1, None, 0),
            ('smoothing', 'TEXT', 0, None, 0),
            ('processing_json', 'TEXT', 1, None, 0),
            ('source_sha256', 'TEXT', 1, None, 0),
            ('importer_version', 'TEXT', 1, None, 0),
            ('dataset_sha256', 'TEXT', 0, None, 0),
            ('transformation_sha256', 'TEXT', 0, None, 0),
        ),
        foreign_keys=frozenset({
            ('measurement_id', 'cad_measurements', 'measurement_id'),
            ('source_sha256', 'cad_measurement_assets', 'sha256'),
        }),
        unique_sets=frozenset({
            frozenset({'measurement_id'}),
        }),
        # Import-transformation binding is appended lazily by
        # CadMeasurementRepository._initialize: a pre-versioning database may
        # lack the columns but must match exactly when they are present.
        optional_columns=frozenset({'dataset_sha256', 'transformation_sha256'}),
    ),
    'cad_measurement_assets': _LegacyTableSignature(
        columns=(
            ('sha256', 'TEXT', 0, None, 1),
            ('filename', 'TEXT', 1, None, 0),
            ('relative_path', 'TEXT', 1, None, 0),
            ('size_bytes', 'INTEGER', 1, None, 0),
        ),
        foreign_keys=frozenset(),
        unique_sets=frozenset(),
    ),
    'cad_measurement_comparisons': _LegacyTableSignature(
        columns=(
            ('comparison_id', 'TEXT', 0, None, 1),
            ('document_id', 'TEXT', 1, None, 0),
            ('dataset_a_id', 'TEXT', 1, None, 0),
            ('dataset_b_id', 'TEXT', 1, None, 0),
            ('scene_revision_a_id', 'TEXT', 1, None, 0),
            ('scene_revision_b_id', 'TEXT', 1, None, 0),
            ('created_at', 'TEXT', 1, None, 0),
            ('result_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('dataset_a_id', 'cad_frequency_responses', 'dataset_id'),
            ('dataset_b_id', 'cad_frequency_responses', 'dataset_id'),
            ('scene_revision_a_id', 'scene_revisions', 'revision_id'),
            ('scene_revision_b_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset(),
    ),
    'cad_measurement_plans': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('plan_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('candidate_id', 'TEXT', 1, None, 0),
            ('applied_scene_revision_id', 'TEXT', 1, None, 0),
            ('status', 'TEXT', 1, None, 0),
            ('plan_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('applied_scene_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset({
            frozenset({'plan_sha256'}),
        }),
    ),
    'cad_measurements': _LegacyTableSignature(
        columns=(
            ('measurement_id', 'TEXT', 0, None, 1),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('measurement_entity_id', 'TEXT', 1, None, 0),
            ('measurement_position_json', 'TEXT', 1, None, 0),
            ('measurement_direction_json', 'TEXT', 0, None, 0),
            ('evidence_type', 'TEXT', 1, None, 0),
            ('channel_role', 'TEXT', 1, None, 0),
            ('source_speaker_ids_json', 'TEXT', 1, None, 0),
            ('radiation_scope', 'TEXT', 1, None, 0),
            ('routing_evidence', 'TEXT', 1, None, 0),
            ('captured_at', 'TEXT', 0, None, 0),
            ('imported_at', 'TEXT', 1, None, 0),
            ('source_kind', 'TEXT', 1, None, 0),
            ('external_source_id', 'TEXT', 0, None, 0),
            ('quality_status', 'TEXT', 1, None, 0),
            ('quality_reasons_json', 'TEXT', 1, None, 0),
            ('quality_source', 'TEXT', 1, None, 0),
            ('provenance_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset(),
    ),
    'cad_model_validations': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('validation_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('model_id', 'TEXT', 1, None, 0),
            ('model_version', 'TEXT', 1, None, 0),
            ('recommendation_gate', 'TEXT', 1, None, 0),
            ('validation_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'validation_id'}),
            frozenset({'validation_sha256'}),
        }),
    ),
    'cad_objective_evaluations': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('evaluation_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('search_spec_sha256', 'TEXT', 1, None, 0),
            ('candidate_id', 'TEXT', 1, None, 0),
            ('evaluation_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'evaluation_id'}),
        }),
    ),
    'cad_pareto_sets': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('pareto_set_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('search_spec_sha256', 'TEXT', 1, None, 0),
            ('pareto_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'pareto_set_id'}),
        }),
    ),
    'cad_prediction_results': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('prediction_id', 'TEXT', 1, None, 0),
            ('run_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('constraint_workspace_hash', 'TEXT', 0, None, 0),
            ('model_id', 'TEXT', 1, None, 0),
            ('model_version', 'TEXT', 1, None, 0),
            ('result_kind', 'TEXT', 1, None, 0),
            ('geometry_compatibility', 'TEXT', 1, None, 0),
            ('parameters_json', 'TEXT', 1, None, 0),
            ('input_snapshot_json', 'TEXT', 1, None, 0),
            ('input_hash', 'TEXT', 1, None, 0),
            ('submitted_at_utc', 'TEXT', 1, None, 0),
            ('completed_at_utc', 'TEXT', 1, None, 0),
            ('status', 'TEXT', 1, None, 0),
            ('assumptions_json', 'TEXT', 1, None, 0),
            ('warnings_json', 'TEXT', 1, None, 0),
            ('modes_json', 'TEXT', 1, None, 0),
            ('reflections_json', 'TEXT', 1, None, 0),
            ('result_sha256', 'TEXT', 0, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset({
            frozenset({'prediction_id'}),
        }),
        # Output identity is appended lazily by
        # CadPredictionRepository._initialize: a pre-versioning database may
        # lack the column but must match exactly when it is present.
        optional_columns=frozenset({'result_sha256'}),
    ),
    'cad_roomsim_batch_specs': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('batch_run_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('search_spec_sha256', 'TEXT', 1, None, 0),
            ('candidate_set_sha256', 'TEXT', 1, None, 0),
            ('batch_spec_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'batch_run_id'}),
        }),
    ),
    'cad_roomsim_candidate_attempts': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('attempt_id', 'TEXT', 1, None, 0),
            ('batch_run_id', 'TEXT', 1, None, 0),
            ('candidate_id', 'TEXT', 1, None, 0),
            ('attempt_index', 'INTEGER', 1, None, 0),
            ('status', 'TEXT', 1, None, 0),
            ('attempt_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('completed_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('batch_run_id', 'cad_roomsim_batch_specs', 'batch_run_id'),
        }),
        unique_sets=frozenset({
            frozenset({'attempt_id'}),
            frozenset({'attempt_index', 'batch_run_id', 'candidate_id'}),
        }),
    ),
    'cad_search_specs': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('scene_revision_id', 'TEXT', 1, None, 0),
            ('scene_content_hash', 'TEXT', 1, None, 0),
            ('constraint_workspace_hash', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('search_spec_sha256', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset({
            frozenset({'search_spec_id'}),
        }),
    ),
    'cad_validation_campaigns': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('campaign_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('search_spec_id', 'TEXT', 1, None, 0),
            ('model_id', 'TEXT', 1, None, 0),
            ('model_version', 'TEXT', 1, None, 0),
            ('candidate_set_sha256', 'TEXT', 1, None, 0),
            ('campaign_sha256', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'campaign_id'}),
            frozenset({'campaign_sha256'}),
        }),
    ),
    'editor_view_states': _LegacyTableSignature(
        columns=(
            ('document_id', 'TEXT', 0, None, 1),
            ('selected_id', 'TEXT', 0, None, 0),
            ('hidden_ids_json', 'TEXT', 1, None, 0),
            ('locked_ids_json', 'TEXT', 1, None, 0),
            ('updated_at_utc', 'TEXT', 1, None, 0),
            ('selected_ids_json', 'TEXT', 1, "'[]'", 0),
        ),
        foreign_keys=frozenset(),
        unique_sets=frozenset(),
        optional_columns=frozenset({'selected_ids_json'}),
    ),
    'scene_recovery_snapshots': _LegacyTableSignature(
        columns=(
            ('document_id', 'TEXT', 0, None, 1),
            ('source_revision_id', 'TEXT', 0, None, 0),
            ('updated_at_utc', 'TEXT', 1, None, 0),
            ('content_hash', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('source_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset(),
    ),
    'scene_revisions': _LegacyTableSignature(
        columns=(
            ('seq', 'INTEGER', 0, None, 1),
            ('revision_id', 'TEXT', 1, None, 0),
            ('document_id', 'TEXT', 1, None, 0),
            ('parent_revision_id', 'TEXT', 0, None, 0),
            ('created_at_utc', 'TEXT', 1, None, 0),
            ('content_hash', 'TEXT', 1, None, 0),
            ('payload_json', 'TEXT', 1, None, 0),
        ),
        foreign_keys=frozenset({
            ('parent_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset({
            frozenset({'revision_id'}),
        }),
    ),
}


class NativeSchemaError(RuntimeError):
    """Native CAD database schema is incompatible or cannot be adopted safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _stored_version(connection: sqlite3.Connection) -> int:
    tables = _table_names(connection)
    if _METADATA_TABLE not in tables:
        return 0
    try:
        row = connection.execute(
            f'SELECT schema_version FROM {_METADATA_TABLE} WHERE singleton=1'
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native schema metadata is unreadable: {exc}') from exc
    if row is None:
        raise NativeSchemaError('native schema metadata row is missing')
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise NativeSchemaError('native schema version is invalid') from exc
    if version < 1:
        raise NativeSchemaError(f'native schema version must be positive: {version}')
    return version


def read_native_schema_version(path: Path) -> int:
    """Read the native DB schema without modifying the database.

    Version 0 means a pre-versioning/legacy database or an empty database.
    """

    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    try:
        with closing(
            sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        ) as connection:
            return _stored_version(connection)
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema could not be read: {exc}') from exc


def check_native_schema_compatibility(path: Path) -> int:
    """Reject data created by a newer native schema while accepting legacy v0.

    Unversioned databases additionally must look like a pre-versioning HTDT
    database: every table needs a supported legacy signature. This keeps
    read-only gates (restore preflight, compatibility probes) aligned with the
    adoption authority instead of accepting arbitrary SQLite files.
    """

    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    try:
        with closing(
            sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        ) as connection:
            version = _stored_version(connection)
            if version > NATIVE_SCHEMA_VERSION:
                raise NativeSchemaError(
                    f'native database schema v{version} is newer than this application '
                    f'supports (v{NATIVE_SCHEMA_VERSION})'
                )
            if version == 0:
                _validate_legacy_tables(connection)
            return version
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(
            f'native database schema could not be read: {exc}'
        ) from exc


def _legacy_signature_mismatch(
    connection: sqlite3.Connection,
    table: str,
    tables: set[str],
) -> str | None:
    """Return why ``table`` diverges from its legacy signature, or ``None``."""

    signature = _LEGACY_TABLE_SIGNATURES[table]
    actual_columns = tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in connection.execute(f'PRAGMA table_info({table})')
    )
    actual_names = {column[0] for column in actual_columns}
    expected_columns = tuple(
        column
        for column in signature.columns
        if column[0] in actual_names or column[0] not in signature.optional_columns
    )
    if actual_columns != expected_columns:
        return 'columns'
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.execute(f'PRAGMA foreign_key_list({table})')
    }
    if foreign_keys != signature.foreign_keys:
        return 'foreign keys'
    missing_targets = sorted(
        {referenced for _column, referenced, _ref_column in foreign_keys} - tables
    )
    if missing_targets:
        return f'missing referenced tables: {", ".join(missing_targets)}'
    unique_sets = {
        frozenset(
            str(column[2])
            for column in connection.execute(f'PRAGMA index_info({index[1]})')
        )
        for index in connection.execute(f'PRAGMA index_list({table})')
        if str(index[3]) == 'u'
    }
    if unique_sets != signature.unique_sets:
        return 'unique constraints'
    return None


def _validate_legacy_tables(connection: sqlite3.Connection) -> None:
    """Reject an unversioned database whose tables are not legacy HTDT tables."""

    tables = _table_names(connection)
    if not tables:
        return
    unexpected = sorted(tables - _LEGACY_TABLE_SIGNATURES.keys())
    if unexpected:
        raise NativeSchemaError(
            'refusing to adopt an unversioned database with unrelated tables: '
            + ', '.join(unexpected)
        )
    mismatched = []
    for table in sorted(tables):
        reason = _legacy_signature_mismatch(connection, table, tables)
        if reason is not None:
            mismatched.append(f'{table} ({reason})')
    if mismatched:
        raise NativeSchemaError(
            'refusing to adopt an unversioned database whose tables do not '
            'match supported pre-versioning signatures: ' + ', '.join(mismatched)
        )


def _validate_legacy_database(connection: sqlite3.Connection) -> None:
    _validate_legacy_tables(connection)

    integrity = connection.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise NativeSchemaError(f'legacy native database integrity check failed: {integrity!r}')
    foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_keys:
        raise NativeSchemaError(
            f'legacy native database foreign-key check failed: {foreign_keys!r}'
        )


def _migrate_0_to_1(connection: sqlite3.Connection) -> None:
    _validate_legacy_database(connection)
    connection.execute(
        f'''
        CREATE TABLE IF NOT EXISTS {_METADATA_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL
        )
        '''
    )
    connection.execute(
        f'''
        CREATE TABLE IF NOT EXISTS {_MIGRATION_TABLE} (
            schema_version INTEGER PRIMARY KEY,
            applied_at_utc TEXT NOT NULL,
            description TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        f'INSERT INTO {_METADATA_TABLE}(singleton, schema_version) VALUES (1, ?)',
        (1,),
    )
    connection.execute(
        f'''
        INSERT INTO {_MIGRATION_TABLE}(schema_version, applied_at_utc, description)
        VALUES (?, ?, ?)
        ''',
        (1, _utc_now(), 'adopt pre-versioned native CAD schema as baseline v1'),
    )


def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_observations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            extended_search_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            objective_id TEXT NOT NULL,
            observation_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_observation_search_seq
            ON cad_adaptive_extended_observations(extended_search_id, seq ASC)
        """,
        """
        CREATE TABLE IF NOT EXISTS cad_adaptive_extended_plans (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            extended_search_id TEXT NOT NULL,
            validation_id TEXT NOT NULL,
            execution_scope TEXT NOT NULL,
            selected_candidate_id TEXT NOT NULL,
            adaptive_extended_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_adaptive_extended_plan_search_seq
            ON cad_adaptive_extended_plans(extended_search_id, seq ASC)
        """,
    )
    for statement in statements:
        connection.execute(statement)



def _migrate_2_to_3(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cad_raw_mesh_repair_bundles (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            repaired_mesh_id TEXT NOT NULL UNIQUE,
            repaired_mesh_semantic_hash TEXT NOT NULL,
            raw_mesh_id TEXT NOT NULL,
            raw_mesh_semantic_hash TEXT NOT NULL,
            repair_plan_id TEXT NOT NULL,
            repair_plan_semantic_hash TEXT NOT NULL,
            post_diagnostic_id TEXT NOT NULL,
            post_diagnostic_semantic_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_mesh_repair_bundle_source
            ON cad_raw_mesh_repair_bundles(raw_mesh_id, seq ASC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_mesh_repair_bundle_plan
            ON cad_raw_mesh_repair_bundles(repair_plan_id, seq ASC)
        """
    )


def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
    # Canonical content-addressed blob authority shared by capture ingestion
    # and raw-mesh repair. Raw payloads are stored once under their SHA-256
    # and records reference them by digest instead of embedding Base64 copies.
    connection.execute(CONTENT_BLOB_DDL)


_MIGRATIONS = {
    1: _migrate_0_to_1,
    2: _migrate_1_to_2,
    3: _migrate_2_to_3,
    4: _migrate_3_to_4,
}



def ensure_native_schema(path: Path) -> int:
    """Atomically migrate a native CAD database to the supported schema.

    Existing 0.1.0-era databases have no central schema marker. They are adopted
    as v1 only after integrity/foreign-key checks and only when every table
    matches a known pre-versioning HTDT signature. A database from a newer
    application is never opened.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve any interrupted managed-data restore before sqlite3.connect can
    # create a fresh database at this path: a pending restore journal or
    # rollback sibling means the live data directory is mid-swap, and seeding
    # a new database here would silently discard that fact. Recovery either
    # restores a consistent generation or raises. Deferred import because
    # native_backup depends on this module for compatibility checks.
    from .native_backup import recover_interrupted_restore

    recover_interrupted_restore(path.parent)
    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            version = _stored_version(connection)
            if version > NATIVE_SCHEMA_VERSION:
                raise NativeSchemaError(
                    f'native database schema v{version} is newer than this application '
                    f'supports (v{NATIVE_SCHEMA_VERSION})'
                )
            while version < NATIVE_SCHEMA_VERSION:
                target = version + 1
                migration = _MIGRATIONS.get(target)
                if migration is None:
                    raise NativeSchemaError(
                        f'no native database migration is registered for v{version}->v{target}'
                    )
                migration(connection)
                version = target
                if target != 1:
                    connection.execute(
                        f'UPDATE {_METADATA_TABLE} SET schema_version=? WHERE singleton=1',
                        (version,),
                    )
                    connection.execute(
                        f'''
                        INSERT INTO {_MIGRATION_TABLE}(
                            schema_version, applied_at_utc, description
                        ) VALUES (?, ?, ?)
                        ''',
                        (version, _utc_now(), f'migrate native schema to v{version}'),
                    )
            return version
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema migration failed: {exc}') from exc
