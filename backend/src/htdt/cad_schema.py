from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
from typing import Literal

from .cad_schema_ddl import (
    NATIVE_BASELINE_DDL,
    NATIVE_COLUMN_ENSURES,
)
from .content_blobs import CONTENT_BLOB_DDL
from .clock import utc_now_iso as _utc_now


_LOGGER = logging.getLogger('htdt.native')


NATIVE_SCHEMA_VERSION = 90

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
            ('candidate_set_sha256', 'TEXT', 0, None, 0),
            ('input_authorities_json', 'TEXT', 0, None, 0),
        ),
        foreign_keys=frozenset({
            ('scene_revision_id', 'scene_revisions', 'revision_id'),
            ('search_spec_id', 'cad_search_specs', 'search_spec_id'),
        }),
        unique_sets=frozenset({
            frozenset({'evaluation_id'}),
        }),
        # Authority attestation is appended lazily by
        # CadObjectiveRepository._initialize: a pre-authority database may lack
        # the columns but must match the signature exactly when they are
        # present.
        optional_columns=frozenset({'candidate_set_sha256', 'input_authorities_json'}),
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
            ('provider_response_json', 'TEXT', 0, None, 0),
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
        optional_columns=frozenset({'result_sha256', 'provider_response_json'}),
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
            ('detached', 'INTEGER', 1, '0', 0),
            ('detached_reason', 'TEXT', 0, None, 0),
        ),
        foreign_keys=frozenset({
            ('parent_revision_id', 'scene_revisions', 'revision_id'),
        }),
        unique_sets=frozenset({
            frozenset({'revision_id'}),
        }),
        # Detached-lineage markers are appended by schema v5 / lazily by
        # SceneRepository._initialize: a pre-versioning database may lack the
        # columns but must match the signature exactly when they are present.
        optional_columns=frozenset({'detached', 'detached_reason'}),
    ),
}


class NativeSchemaError(RuntimeError):
    """Native CAD database schema is incompatible or cannot be adopted safely."""


def connect_sqlite(
    path: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open ``path`` with the shared row factory and FK enforcement on."""
    connection = sqlite3.connect(path, check_same_thread=check_same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


# Memoized ``ensure_native_schema`` successes keyed by database path and the
# file signature captured right after the migration authority ran. The native
# store never enables WAL, so every committed write mutates the main file and
# therefore bumps mtime_ns/ctime_ns (and usually size); a matching signature
# proves the stored version is still the one the earlier full check produced.
_ENSURED_SCHEMA_SIGNATURES: OrderedDict[
    str, tuple[int, int, int, int, int]
] = OrderedDict()

#: Signature memos are keyed by database path — a long session opening many
#: distinct project databases must not accumulate one entry per path ever
#: seen. Entries are invalidated by file signature anyway, so LRU eviction
#: only ever re-runs a cheap read-only check.
_SCHEMA_SIGNATURE_CACHE_LIMIT = 512


def _memoize_schema_signature(cache: OrderedDict, key: str, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _SCHEMA_SIGNATURE_CACHE_LIMIT:
        cache.popitem(last=False)


def _db_file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
    )


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


def _reject_empty_database_file(path: Path) -> None:
    """An existing zero-byte database file is never a valid SQLite store.

    A live HTDT database receives its first page inside the transaction
    that creates it, so a file that exists but is empty is a torn create
    or a truncation — evidence of data loss, not a legacy v0 database.
    Seeding a fresh schema here would silently convert corrupted state
    into an apparently-empty project.
    """

    try:
        size = path.stat().st_size
    except OSError:
        return
    if size == 0:
        raise NativeSchemaError(
            f'native database file exists but is empty: {path}'
        )


def read_native_schema_version(path: Path) -> int:
    """Read the native DB schema without modifying the database.

    Version 0 means a pre-versioning/legacy database. An existing
    zero-byte file is torn or truncated state and fails closed.
    """

    path = Path(path)
    if not path.is_file():
        return 0
    _reject_empty_database_file(path)
    try:
        with closing(
            sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        ) as connection:
            return _stored_version(connection)
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema could not be read: {exc}') from exc


NativeSchemaCompatibility = Literal[
    'current',
    'migration_required',
    'incompatible_newer',
    'legacy_unversioned',
]


def native_schema_compatibility(version: int) -> NativeSchemaCompatibility:
    """Classify a stored native schema version against this application.

    Version ``0`` is a pre-versioning/legacy (or empty) database; versions
    below ``NATIVE_SCHEMA_VERSION`` migrate on open; anything newer is
    rejected by the compatibility checks.
    """

    if version <= 0:
        return 'legacy_unversioned'
    if version > NATIVE_SCHEMA_VERSION:
        return 'incompatible_newer'
    if version < NATIVE_SCHEMA_VERSION:
        return 'migration_required'
    return 'current'


# Memoized ``check_native_schema_compatibility`` results keyed by database
# path and the file signature observed when the check ran — the same
# invalidation contract as ``_ENSURED_SCHEMA_SIGNATURES``: every committed
# write mutates mtime/size, so a stale entry can never outlive a write.
# Each repository method pays this read-only gate; at listing volume the
# per-call ro-connection mattered more than the version query itself.
_COMPATIBLE_SCHEMA_SIGNATURES: OrderedDict[
    str, tuple[tuple[int, int, int, int, int], int]
] = OrderedDict()


def check_native_schema_compatibility(path: Path) -> int:
    """Reject data created by a newer native schema while accepting legacy v0.

    Unversioned databases additionally must look like a pre-versioning HTDT
    database: every table needs a supported legacy signature. This keeps
    read-only gates (restore preflight, compatibility probes) aligned with the
    adoption authority instead of accepting arbitrary SQLite files.
    """

    path = Path(path)
    signature = _db_file_signature(path)
    if signature is not None:
        cached = _COMPATIBLE_SCHEMA_SIGNATURES.get(str(path))
        if cached is not None and cached[0] == signature:
            _COMPATIBLE_SCHEMA_SIGNATURES.move_to_end(str(path))
            return cached[1]
    if not path.is_file():
        return 0
    _reject_empty_database_file(path)
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
            if signature is not None:
                _memoize_schema_signature(
                    _COMPATIBLE_SCHEMA_SIGNATURES,
                    str(path),
                    (signature, version),
                )
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
    if len(integrity) != 1 or integrity[0][0] != 'ok':
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


_SCENE_DOCUMENT_HEADS_DDL = '''
CREATE TABLE IF NOT EXISTS scene_document_heads (
    document_id TEXT PRIMARY KEY,
    head_revision_id TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    generation INTEGER NOT NULL,
    FOREIGN KEY(head_revision_id) REFERENCES scene_revisions(revision_id)
)
'''


def ensure_scene_revision_lineage_columns(connection: sqlite3.Connection) -> None:
    """Lazily append detached-lineage marker columns to ``scene_revisions``.

    Schema v5 adds them for databases that already carried the table; fresh
    databases get them either from this ALTER path or from the repository's
    CREATE TABLE. Safe to call whenever ``scene_revisions`` exists.
    """
    columns = {
        str(row[1])
        for row in connection.execute('PRAGMA table_info(scene_revisions)')
    }
    if 'detached' not in columns:
        connection.execute(
            'ALTER TABLE scene_revisions '
            'ADD COLUMN detached INTEGER NOT NULL DEFAULT 0'
        )
    if 'detached_reason' not in columns:
        connection.execute(
            'ALTER TABLE scene_revisions ADD COLUMN detached_reason TEXT'
        )


def backfill_scene_document_heads(connection: sqlite3.Connection) -> None:
    """Initialize ``scene_document_heads`` for documents missing a head row.

    Replays every unheaded document's revisions in ``seq`` order while
    tracking the acknowledged mainline head: a row advances the head only
    when it is not marked detached and its parent is the running mainline
    head (equivalently, when it was a normal compare-and-swap save). Rows
    that violate that — pre-#626 ``allow_branch`` rows, duplicate roots, or
    children of detached lineage — are marked ``detached`` and can never
    become head. This is a deterministic, conservative reconstruction from
    single-head ancestry; it reports detached rows through the diagnostics
    log instead of silently choosing ``MAX(seq)``.
    """
    tables = _table_names(connection)
    if 'scene_revisions' not in tables or 'scene_document_heads' not in tables:
        return
    rows = connection.execute(
        '''
        SELECT r.seq, r.revision_id, r.document_id, r.parent_revision_id,
               r.created_at_utc, r.detached
        FROM scene_revisions r
        LEFT JOIN scene_document_heads h ON h.document_id = r.document_id
        WHERE h.document_id IS NULL
        ORDER BY r.seq
        ''',
    ).fetchall()
    if not rows:
        return
    # document_id -> (head_revision_id, updated_at_utc, generation)
    heads: dict[str, tuple[str, str, int]] = {}
    detached_seqs: list[int] = []
    detached_refs: list[str] = []
    for row in rows:
        # Index access: this helper also runs inside ensure_native_schema,
        # whose connection has no Row factory.
        seq, revision_id, document_id = int(row[0]), str(row[1]), str(row[2])
        parent_id, created_at = row[3], str(row[4])
        head = heads.get(document_id)
        mainline = not row[5] and (
            (parent_id is None and head is None)
            or (head is not None and parent_id == head[0])
        )
        if mainline:
            heads[document_id] = (
                revision_id,
                created_at,
                (head[2] if head is not None else 0) + 1,
            )
        else:
            detached_seqs.append(seq)
            detached_refs.append(f'{document_id}:{revision_id}')
    for document_id, (revision_id, updated_at, generation) in heads.items():
        connection.execute(
            '''
            INSERT INTO scene_document_heads(
                document_id, head_revision_id, updated_at_utc, generation
            ) VALUES (?, ?, ?, ?)
            ''',
            (document_id, revision_id, updated_at, generation),
        )
    for seq in detached_seqs:
        connection.execute(
            'UPDATE scene_revisions SET detached=1 WHERE seq=?',
            (seq,),
        )
    if detached_refs:
        _LOGGER.warning(
            'reconstructed scene document heads excluding detached '
            'SceneRevision lineage (revisions remain bound by exact id): %s',
            ', '.join(detached_refs),
        )


def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
    # Explicit current-head authority for SceneRevision documents (#626):
    # detached analytical/fixture lineage may be persisted but must never
    # redefine the document's current head by insertion order. Backfill
    # replays single-head ancestry so pre-existing branch rows are excluded
    # from — and flagged off — the reconstructed head.
    connection.execute(_SCENE_DOCUMENT_HEADS_DDL)
    if 'scene_revisions' in _table_names(connection):
        ensure_scene_revision_lineage_columns(connection)
        backfill_scene_document_heads(connection)


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    column_ddl: str,
) -> None:
    """Append ``column`` to ``table`` when an older build lacked it.

    Idempotent counterpart of the lazy ``ALTER TABLE ... ADD COLUMN`` calls
    repositories used to run at open. The canonical CREATE statements in
    :data:`NATIVE_BASELINE_DDL` already include the column; this converges
    tables that were created before it existed.
    """

    columns = {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info({table})')
    }
    if column not in columns:
        connection.execute(f'ALTER TABLE {table} ADD COLUMN {column_ddl}')


def _migrate_5_to_6(connection: sqlite3.Connection) -> None:
    # Centralize the complete persistent schema contract (#302): every table,
    # index and column the native database may contain is declared once in
    # cad_schema_ddl and applied here, so two builds reporting the same
    # schema version always understand the same on-disk contract.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)
    tables = _table_names(connection)
    for table, column, column_ddl in NATIVE_COLUMN_ENSURES:
        if table in tables:
            _ensure_column(connection, table, column, column_ddl)
    if 'scene_revisions' in tables:
        ensure_scene_revision_lineage_columns(connection)
        backfill_scene_document_heads(connection)
    # Domain modules whose persisted tables must be converged from older
    # lazy-DDL shapes (table rebuilds that depend on parsing persisted
    # payloads, ordering constraints around foreign-key rewrites, or data
    # backfills no plain CREATE can express). The imports stay deferred and
    # literal — these domain modules import cad_schema so module scope would
    # cycle, and PyInstaller's static analysis cannot follow string imports.
    # The same functions are re-verified idempotently by the owning
    # repository's ``_initialize`` so every supported open path converges
    # the same way.
    from . import cad_adaptive_extended_repository
    from . import capture_connected_space
    from . import capture_inbox
    from . import capture_ingestion_transaction
    from . import capture_semantic_promotion

    # The domain convergence helpers read rows by column name; give the
    # migration connection the same Row factory repositories open with.
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        capture_ingestion_transaction.run_capture_schema_convergence(connection)
        capture_inbox.run_capture_inbox_schema_convergence(connection)
        capture_connected_space.run_connected_space_schema_convergence(connection)
        capture_semantic_promotion.run_semantic_promotion_schema_convergence(
            connection
        )
        cad_adaptive_extended_repository.run_adaptive_extended_schema_convergence(
            connection
        )
    finally:
        connection.row_factory = previous_factory


def _migrate_6_to_7(connection: sqlite3.Connection) -> None:
    # Converge the project-identity authorities (#764): pre-merge
    # ``project_registry``/``project_tombstones`` lifecycle tables fold into
    # the canonical ``htdt_project_*`` store so one table owns project
    # identity, archive state, and clone lineage. Also installs every
    # repository-local CREATE moved under baseline ownership (#767).
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)
    tables = _table_names(connection)
    for table, column, column_ddl in NATIVE_COLUMN_ENSURES:
        if table in tables:
            _ensure_column(connection, table, column, column_ddl)
    if 'project_registry' in tables:
        connection.execute(
            '''
            INSERT INTO htdt_project_documents(
                project_id, document_id, display_name, description,
                created_at_utc, updated_at_utc, archived, archived_at_utc,
                cloned_from_project_id
            )
            SELECT r.project_id, r.document_id, r.display_name, NULL,
                   r.created_at_utc, r.updated_at_utc,
                   CASE r.status WHEN 'archived' THEN 1 ELSE 0 END,
                   r.archived_at_utc, r.cloned_from_project_id
            FROM project_registry r
            WHERE NOT EXISTS (
                SELECT 1 FROM htdt_project_documents d
                WHERE d.project_id = r.project_id
                   OR d.document_id = r.document_id
            )
            '''
        )
        connection.execute('DROP TABLE project_registry')
    if 'project_tombstones' in tables:
        connection.execute(
            '''
            INSERT OR IGNORE INTO htdt_project_tombstones
            SELECT * FROM project_tombstones
            '''
        )
        connection.execute('DROP TABLE project_tombstones')
    connection.execute(
        'UPDATE htdt_project_documents SET updated_at_utc=created_at_utc '
        'WHERE updated_at_utc IS NULL'
    )


def _migrate_7_to_8(connection: sqlite3.Connection) -> None:
    # Install the validation-corpus manifest/benchmark-spec tables (#773)
    # and the data acquisition registry tables (#779). All are new
    # append-only authorities; the idempotent baseline creates them.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_8_to_9(connection: sqlite3.Connection) -> None:
    # Install the field explorer session table (#953): a new append-only
    # authority the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_9_to_10(connection: sqlite3.Connection) -> None:
    # Take ownership of every table repositories used to create through
    # lazy convergence DDL at open (#767): the migration authority now
    # declares them once, so repository initialization verifies instead
    # of mutating. The column ensures converge the evidence semantic-hash
    # columns on tables created before they existed.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)
    tables = _table_names(connection)
    for table, column, column_ddl in NATIVE_COLUMN_ENSURES:
        if table in tables:
            _ensure_column(connection, table, column, column_ddl)
    if 'scene_revisions' in tables:
        backfill_scene_document_heads(connection)


def _migrate_10_to_11(connection: sqlite3.Connection) -> None:
    # Add the capture-disposition transition ledger. The table lives in
    # the capture-inbox domain's convergence script — the same authority
    # that creates the inbox tables when v6 lands — so the upgrade path
    # is that convergence, applied to an already-current schema. There is
    # no data backfill: transitions record changes from this schema
    # forward; a historical item's disposition is on its row, not in a
    # ledger that was never kept.
    from . import capture_inbox

    connection.row_factory = sqlite3.Row
    try:
        capture_inbox.run_capture_inbox_schema_convergence(connection)
    finally:
        connection.row_factory = None


def _migrate_11_to_12(connection: sqlite3.Connection) -> None:
    # Install the R150 late-field energy artifact table: a new append-only
    # authority the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def require_native_tables(
    connection: sqlite3.Connection,
    *tables: str,
) -> None:
    """Fail closed when a repository faces an unmigrated schema.

    Repositories verify — never evolve — the persistent contract (#302):
    ``ensure_native_schema`` runs before any repository opens, so a missing
    table means the migration authority was bypassed or the database is
    corrupted, not that the repository should create it.
    """

    present = _table_names(connection)
    missing = [table for table in tables if table not in present]
    if missing:
        raise NativeSchemaError(
            'native database is missing schema tables owned by the '
            'migration authority: ' + ', '.join(missing)
        )


def _migrate_12_to_13(connection: sqlite3.Connection) -> None:
    # Install the R160 union-band stitched response and bounded late-energy
    # decay artifact tables (R160 residual): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_13_to_14(connection: sqlite3.Connection) -> None:
    # Install the R150 stochastic-ray receiver-estimate artifact table: a new
    # append-only authority the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_14_to_15(connection: sqlite3.Connection) -> None:
    # Install the R150 late-decay estimate artifact table: a new append-only
    # authority the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_15_to_16(connection: sqlite3.Connection) -> None:
    # Install the acoustic geometry-derivation provenance record and the
    # per-solver-path capability manifest tables: new append-only authorities
    # the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_16_to_17(connection: sqlite3.Connection) -> None:
    # Install the #538 auralization evidence tables (capability records,
    # routing declarations, review packages, listening validations): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_17_to_18(connection: sqlite3.Connection) -> None:
    # Install the guided acceptance run tables (REV48): revisioned run
    # records plus the evidence asset manifest — both plain baseline DDL.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_18_to_19(connection: sqlite3.Connection) -> None:
    # Install the #541 guided video commissioning tables (sessions, status
    # events, readiness reports, diagnoses, action proposals, operator
    # adjustments, before/after comparisons, import batches): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_19_to_20(connection: sqlite3.Connection) -> None:
    # Install the #564 prediction<->measurement registration tables
    # (registrations plus sealed residual reports): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_20_to_21(connection: sqlite3.Connection) -> None:
    # Install the #568 correction qualification table and the #569
    # conventional multi-sub optimization tables (candidates,
    # seat-population evaluations, qualifications, staged comparisons,
    # deployment verifications): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_21_to_22(connection: sqlite3.Connection) -> None:
    # Install the REV56-MEASEV measurement-evidence authorities
    # (#572 uncertainty budgets + significance assessments, #573 state
    # policies/snapshots/verdicts, #575 transformation DAG nodes): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_22_to_23(connection: sqlite3.Connection) -> None:
    # Install the REV56 evidence-aware decision rule tables (#577:
    # decision rule specs + evidence verdicts) and the #604 uncertainty
    # propagation tables (uncertain input sets + robust design
    # assessments): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_23_to_24(connection: sqlite3.Connection) -> None:
    # Install the REV56-BASSSTIM authorities (#574 bass-management
    # qualification: splice evidence + qualification verdicts; #608
    # stimulus registry: assets + measurement pins + eligibility
    # verdicts): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_24_to_25(connection: sqlite3.Connection) -> None:
    # Install the REV56-SNAPSTD authorities (#592 device configuration
    # snapshot/restore: snapshots + known-good baselines + firmware
    # transitions + restore records + backup artifacts + replacement
    # assessments; #599 external standards registry: registered documents
    # + lifecycle observations + profile mappings + evaluation pins +
    # revision diffs): new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_25_to_26(connection: sqlite3.Connection) -> None:
    # Install the REV56-CAMPPROFILE authorities (#581 spatial campaign
    # designs/evaluations/capture bindings; #585 RP32 commissioning
    # profiles, reconciliations, readiness assessments, verification
    # plans/records, and reports): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_26_to_27(connection: sqlite3.Connection) -> None:
    # Install the REV56-TARGETS authorities (#579 RP22 standards
    # profile: parameter-declaration profiles + per-parameter
    # evaluations; #588 response-target authority: response-target
    # profiles + spectral-balance evaluations): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_27_to_28(connection: sqlite3.Connection) -> None:
    # Install the REV56-METRICS authorities (#580 background-noise metric
    # profiles/measurements/criterion evaluations; #605 STI profiles,
    # measurements, predictions and dialogue assessments; #607 content
    # loudness profiles, programme measurements, normalization
    # observations, playback gain states and matching records): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_28_to_29(connection: sqlite3.Connection) -> None:
    # Install the REV56-ELEC authorities (#593 electrical playback
    # qualification: amplifier↔loudspeaker compatibility verdicts;
    # #597 as-built wiring traceability: physical interconnects,
    # verifications, logical→physical bindings): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_29_to_30(connection: sqlite3.Connection) -> None:
    # Install the REV56-TRANSPORT authorities (#582 A/V latency paths,
    # sync profiles, measurements and qualifications; #583 HDMI signal
    # profiles, EDID/HDCP/link observations, verification records,
    # qualifications and the RP28 profile; #591 network AV paths, media
    # flows, transport/timing observations and qualifications): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_30_to_31(connection: sqlite3.Connection) -> None:
    # Install the REV56-LIFECYCLE authorities (#595 post-commissioning
    # health/drift monitoring: monitoring declarations, lifecycle
    # observations, change events, trend/symptom/drift assessments,
    # reverification triggers, restore confirmations; #596 substitution
    # impact: proposals, change-impact assessments, decisions, as-built
    # reconciliations, equipment schedule records): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_31_to_32(connection: sqlite3.Connection) -> None:
    # Install the REV56-BUILDING authorities (#576 inter-room
    # sound-isolation qualification: construction elements, ordered
    # source->receiving scenarios, banded field measurements,
    # predict<->measure calibrations, qualifications; #589 mechanical
    # rattle: noise stress tests, rattle events, remediation actions,
    # qualifications; #590 seating/occupancy: seat acoustic models,
    # occupancy scenarios, direct-sound clearance evaluations, seating
    # commissioning results): new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_32_to_33(connection: sqlite3.Connection) -> None:
    # Install the REV56-OPS authorities (#598 networked AV security:
    # asset overlays, credentials, management surfaces, observations,
    # risks, remote-service authorizations, security test evidence,
    # access reviews, review verdicts; #601 control/automation scenario
    # qualification: surfaces, scenario declarations, execution runs,
    # qualification verdicts; #602 safe-listening/test-exposure:
    # exposure limits, SPL capabilities, test plans, gate decisions,
    # assessments): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_33_to_34(connection: sqlite3.Connection) -> None:
    # Install the REV56-INTEROP authorities (#578 openBIM IFC 4.3
    # interoperability: import artifacts, entity mappings, revision
    # deltas, intake profiles/evaluations, export packages; #586 CEDIA
    # RP1 performance-facts ingestion: profiles, product identities,
    # facts, import runs, suitability evaluations, profile rebinds):
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_34_to_35(connection: sqlite3.Connection) -> None:
    # Install the REV56-INFRA authorities (#587 rack/power/thermal
    # qualification: rack enclosures, installed devices, branch
    # circuits, protection devices, PoE budgets, operating scenarios,
    # thermal measurements, qualification verdicts; #603 immersive
    # render-path qualification: content profiles, renderer
    # capabilities, the four distinct speaker-layout authorities,
    # render sessions, output observations, verdicts; #606 hum/buzz
    # grounding-EMC diagnosis: noise observations, interconnect
    # evidence, isolation tests, diagnostics, safe mitigations,
    # verdicts): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_35_to_36(connection: sqlite3.Connection) -> None:
    # Install the REV57-METRO authorities (#609 measurement timebase /
    # clock authority: clock domains, per-capture timebases, capability
    # assessments; #610 reproducible evidence bundle: bundle manifests,
    # artifact entries, derivation edges, attestations, validation
    # verdicts; #611 instrument calibration lifecycle: instrument
    # instances, calibration events, interval policies, verification
    # checks, service events, fitness assessments, out-of-tolerance
    # reviews): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_36_to_37(connection: sqlite3.Connection) -> None:
    # Install the REV57-PHYS authorities (#613 geometry survey: survey
    # instruments, campaigns, element geometry evidence, control
    # measurements, as-built reconciliations, task requirements,
    # qualifications; #614 installed-source boundary: source
    # measurement conditions, mounting conditions, boundary
    # corrections, installed measurements, qualifications; #615 porous
    # absorber: parameter evidence, material models, build-ups,
    # predictions, fit comparisons): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_37_to_38(connection: sqlite3.Connection) -> None:
    # Install the REV57-PROJ projector-domain authorities (#619 spatial
    # projection-image qualification: spatial measurement plans, sealed
    # measurement sets, derived maps, uniformity evaluations; #622
    # projection image-geometry / masking: presentation geometry
    # bindings, geometry measurements, lens-memory recall records,
    # geometry evaluations; #624 hush-box / enclosure co-design:
    # install constraints, enclosure plans, operating observations,
    # acoustic observations, qualifications; #627 optical-radiation
    # safety: safety identities, manufacturer safety constraints,
    # placements, safety evaluations): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_38_to_39(connection: sqlite3.Connection) -> None:
    # Install the REV57-DISP authorities (#625 direct-view display:
    # display states, stimulus contexts, photometric measurements,
    # temporal observations, spatial measurements, angle measurements,
    # qualifications; #626 observer metamerism: spectral states,
    # observer profiles, pair evaluations, perceptual matches,
    # qualifications; #633 viewing environment: observations, geometry
    # observations, lighting scenes, qualifications): new append-only
    # authorities the idempotent baseline creates. REV57-PROJ took v38
    # first, so REV57-DISP lands as v39.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)



def _migrate_39_to_40(connection: sqlite3.Connection) -> None:
    # Install the REV57-AUD authorities (#621 acoustic channel-identity /
    # polarity verification: identity chains, endpoint observations,
    # identity tests, per-layer polarity records, per-channel
    # evaluations; #634 listener-area coverage / acoustic-aim
    # qualification: aim states, listener areas, coverage predictions,
    # measurement sets, qualifications; #628 installed loudspeaker
    # instance variation: per-instance evidence, model→instance deltas,
    # matched-set declarations and qualifications; #632 media-playback
    # capability: stack identities, media profile requirements,
    # capability records, operation runs, qualifications): new
    # append-only authorities the idempotent baseline creates. REV57-DISP
    # took v39 first, so REV57-AUD lands as v40.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_40_to_41(connection: sqlite3.Connection) -> None:
    # Install the REV57-INST authorities (#616 theater HVAC
    # acoustic/airflow co-design: ventilation scenarios, duct/paths,
    # component evidence, field observations, qualifications; #618
    # playback reference calibration: reference profiles, calibration
    # stimuli, channel observations, qualifications; #631 as-built
    # acoustic-treatment qualification: install specs, as-built
    # observations, inspections, qualifications; #612 tactile/seat
    # vibration: tactile paths, vibration measurements, profiles,
    # qualifications): new append-only authorities the idempotent
    # baseline creates. REV57-AUD took v40 first, so REV57-INST lands as
    # v41.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_41_to_42(connection: sqlite3.Connection) -> None:
    # Install the REV57-MOUNT authority (#620 AV mounting /
    # structural-support evidence: assemblies, load evidence, support
    # elements, manufacturer requirements, structural approvals,
    # inspection records, qualifications): a new append-only authority
    # the idempotent baseline creates. REV57-INST took v41 first, so
    # REV57-MOUNT lands as v42.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_42_to_43(connection: sqlite3.Connection) -> None:
    # Install the REV58-MEASCHAIN authorities (#695 measurement-chain
    # linearity/overload, #697 swept-sine deconvolution/harmonic
    # separation, #668 room-acoustic excitation source): append-only
    # sealed authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_43_to_44(connection: sqlite3.Connection) -> None:
    # Install the REV58-DSPDECAY authorities (#679 DSP filter
    # realization, #676 decay-curve noise/truncation processing, #705
    # acoustic-impedance physical realizability): append-only sealed
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_44_to_45(connection: sqlite3.Connection) -> None:
    # Install the REV58-NUMERIC authorities (#683 wave-solver
    # numerical fidelity: profiles, convergence records,
    # qualifications; #685 geometrical-acoustics numerical fidelity:
    # profiles, ray-sampling convergences, path-enumeration
    # qualifications, qualifications; #687 wave↔geometrical hybrid
    # handoff: composition profiles, transition qualifications): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_45_to_46(connection: sqlite3.Connection) -> None:
    # Install the REV58-IDENT authorities (#691 typed logarithmic
    # quantity / dB reference: quantities, calibration bridges,
    # operations; #689 calibration-parameter identifiability:
    # parameter records, sensitivity evidence, correlation evidence,
    # equivalent-solution sets, assessments; #698 validation
    # sample-dependence / benchmark leakage: statistical designs,
    # dependence models, dataset role assignments, exposure ledger,
    # challenge qualifications): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_46_to_47(connection: sqlite3.Connection) -> None:
    # Install the REV58-AUDIOMODEL authorities (#654 acoustic-reference
    # origin/phase center: profiles + qualifications; #655 source
    # near/far-field applicability: profiles + qualifications; #656
    # directivity angular resolution: sampling profiles, interpolation
    # records, direction qualifications; #690 multi-source coherence:
    # profiles + combination qualifications; #684 geometric scattering
    # model: profiles + qualifications; #681 edge diffraction model:
    # profiles, benchmark results, qualifications): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_47_to_48(connection: sqlite3.Connection) -> None:
    # Install the REV58-VALIDMETH authorities (#675 optimizer
    # algorithm qualification: problems, run profiles,
    # qualifications, Pareto assessments; #674 acoustic eigenmode
    # validation: mode pairings, verdicts; #673 sound-field
    # diffuseness applicability: assessments, declarations; #671
    # coupled-room multi-slope decay: fits, adequacy gates,
    # qualifications; #677 predicted↔measured early-reflection
    # correspondence: pairings, sets, verdicts; #706 time-frequency
    # modal decay: observations, qualifications): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_48_to_49(connection: sqlite3.Connection) -> None:
    # Install the REV58-DISPLAYMEAS authorities (#682 pattern-generator
    # stimulus fidelity: generator instances, requested patches,
    # delivered observations, fidelity qualifications; #680 colorimeter
    # spectral mismatch / probe-matching: match profiles, match
    # observations, verifications, applicability verdicts; #686 display
    # additivity / RGB separation: additivity observations, separation
    # assessments, volumetric characterisations, holdout verifications,
    # model eligibility, characterisation plans; #647 temporal display
    # fidelity: temporal states, step responses, motion/flicker/retention
    # measurements, qualifications; #666 LUT closed-loop calibration:
    # artifacts, generation records, preflight verifications,
    # deployments, post-verifications, loop qualifications): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_49_to_50(connection: sqlite3.Connection) -> None:
    # Install the REV58-MEASELEC authorities (#699 audio-interface
    # transfer/loopback calibration: loopback observations, transfer
    # calibrations, correction qualifications; #651 gain structure /
    # noise floor / clipping margins: signal level references, noise
    # observations, clipping margins, gain-structure qualifications;
    # #649 playback dynamics / limiter: dynamics states, level-sweep
    # observations, dynamics qualifications; #665 active multi-way
    # crossover: speaker definitions, crossover plans, driver
    # alignment measurements, crossover qualifications; #693
    # measurement-method reproducibility: method procedures,
    # campaigns, precision models, qualifications): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_50_to_51(connection: sqlite3.Connection) -> None:
    # Install the REV59-APPLY authority (#723 device apply transaction /
    # rollback: capability profiles, apply plans, write records,
    # verifications, rollback plans, rollback executions, transactions):
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_51_to_52(connection: sqlite3.Connection) -> None:
    # Install the REV59-BENCH2 authorities (#763 band semantics, #764
    # mixing time, #755 field interpolation, #770 compute budget):
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_52_to_53(connection: sqlite3.Connection) -> None:
    # Install the REV59-DIGCHAIN authorities (#745 clock jitter, #744
    # word-length/dither path, #739 playback SRC, #650 crosstalk):
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_53_to_54(connection: sqlite3.Connection) -> None:
    # Install the REV59-VIDMETA authorities (#759 dynamic light, #756
    # low-luminance metrology, #760 display acoustic boundary,
    # #753/#747 codec fidelity): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_54_to_55(connection: sqlite3.Connection) -> None:
    # Install the REV59-POWEREV authorities (#736 power sequencing,
    # #738 AC power quality, #752 EMC evidence): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_55_to_56(connection: sqlite3.Connection) -> None:
    # Install the REV59-BUILDENV authorities (#751 product safety,
    # #740 occupied IAQ, #750 VOC emissions): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_56_to_57(connection: sqlite3.Connection) -> None:
    # Install the REV59-ACOUST2 authorities (#743 fixture scattering,
    # #749 spectral estimator, #765 evidence supersession): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_57_to_58(connection: sqlite3.Connection) -> None:
    # Install the REV59-DEPS authorities (#729 authority dependency /
    # staleness graph: edge declarations, change events, rule profiles,
    # staleness assessments, revalidation plans; #725 evidence
    # attestation / trusted timestamp: signed manifests, manifest
    # attestations, verification verdicts; #718 project archival /
    # schema-migration: archive snapshots, archive verifications,
    # migration records, migration verifications): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_58_to_59(connection: sqlite3.Connection) -> None:
    # Install the REV59-ROOMQ authorities (#761 sound strength G,
    # #704 resonant treatment, #707 serviceability): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)

def _migrate_59_to_60(connection: sqlite3.Connection) -> None:
    # Install the REV59-QUALNUM authorities (#703 numerical
    # reproducibility, #716 imaging chain, #717 wireless AV): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_60_to_61(connection: sqlite3.Connection) -> None:
    # Install the REV59-DRAWPROF authorities (#741 CEB23-B video
    # profile, #742 J-STD-710 symbols, #733 timed text): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_61_to_62(connection):
    # REV59-ACOUST3 (#694 finite absorber, #646 precedence/echo,
    # #648 fire evidence): new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_62_to_63(connection):
    # REV59-LISTENEXP (#696 listening experiments, #726 ALS,
    # #727 dynamic binaural): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_63_to_64(connection: sqlite3.Connection) -> None:
    # Install the REV59-CODEPOLICY authorities (#746 seating
    # circulation / egress / accessibility evidence, #748 lighting
    # temporal modulation (TLM/TLA), #722 project data privacy &
    # sharing): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_64_to_65(connection):
    # REV59-AUDIOMET (#774 receiver reference, #743 fixture
    # scattering, #773 echo diagnostics, #678 DRR): new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_65_to_66(connection):
    # REV59-INFRA2 (#736 power sequencing, #738 AC quality,
    # #740 occupied IAQ, #750 material emissions, #751 product
    # safety, #752 EMC): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)




def _migrate_66_to_67(connection):
    # REV59-DISPLAY3 (#660 gradation, #688 colour volume,
    # #672 spatial resolution, #756 low-luminance, #759
    # dynamic contrast, #760 wall boundary): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)




def _migrate_67_to_68(connection):
    # REV59-AUDIO2 (#652 panning continuity, #669 subwoofer
    # localization, #657 group-delay audibility, #702
    # headphone coupling, #653 structure-borne, #664
    # remapping): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)




def _migrate_68_to_69(connection):
    # REV59-SIGNAL (#747/#753 codec fidelity, #749 spectral
    # estimator, #670 clock domains, #765 fact claims,
    # #667 BOM): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_69_to_70(connection):
    # REV59-AUDIOMET-B (#661 adaptive identification, #663 live
    # dual-channel TF, #658 microphone arrays, #662 impedance/T-S):
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_70_to_71(connection):
    # REV59-CADREF (#777 cadence delivery, #778 reference
    # room): new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)




def _migrate_71_to_72(connection):
    # REV59-VERAUTO (device-verification requirement/closure):
    # new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_72_to_73(connection: sqlite3.Connection) -> None:
    # Install the REV59-UNITS authorities (#728 typed physical
    # quantity: quantities, operations; #730 engineering-assumption /
    # permissible-use ledger: assumptions, resolutions, assessments;
    # #720 perceptual relevance / audibility: model profiles,
    # assessments; #719 residual diagnostic-hypothesis: cases,
    # hypotheses, tests, verdicts): new append-only authorities the
    # idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_73_to_74(connection: sqlite3.Connection) -> None:
    # Install the REV59-LOUDSPK authorities (#754 large-signal mechanics,
    # #734 source normalization, #731 thermal compression, #732
    # microphone incidence, #737 same-channel arrays, #735 grille
    # transfer): new append-only authorities the idempotent baseline
    # creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)





def _migrate_74_to_75(connection):
    # REV59-CLOSEAUX (manifest->verification gate bridge):
    # new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_75_to_76(connection):
    # REV60-COLLABENV (collaboration/approval authority #721 and
    # material environmental/aging applicability #776): new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_76_to_77(connection):
    # REV60-EDGE (ULF + noise ingress + fire-safety evidence +
    # accessible playback): new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_77_to_78(connection: sqlite3.Connection) -> None:
    # Persist the native mission package alongside the projected wire
    # bytes (mission-return reconciliation): the pull lane serves the
    # app-grammar envelope while the project-side reconciliation path
    # needs the original CaptureMissionPackage to classify drift
    # against the issuing baseline.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)
    tables = _table_names(connection)
    for table, column, column_ddl in NATIVE_COLUMN_ENSURES:
        if table in tables:
            _ensure_column(connection, table, column, column_ddl)


def _migrate_78_to_79(connection: sqlite3.Connection) -> None:
    # Install the mission-rebase decision ledger (mission-return
    # reconciliation): explicit operator decisions mapping a drifted
    # task's pinned target onto a current entity, one per
    # (mission_id, task_id) — the '要調整' verdict becomes actionable.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_79_to_80(connection: sqlite3.Connection) -> None:
    # #806 calibration deployment/verification authority: four new
    # append-only tables the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_80_to_81(connection: sqlite3.Connection) -> None:
    # Install the field-return application ledger (mission-return
    # reconciliation): each row binds one returned task's evidence to a
    # current project entity — the destination the rebase decision names.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_81_to_82(connection: sqlite3.Connection) -> None:
    # #790 playback-electronics authority: electronic audio-path
    # profiles, electrical transfer measurements, linearity evidence
    # and qualifications — append-only sealed tables the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_82_to_83(connection: sqlite3.Connection) -> None:
    # Install the REV62 lifecycle/supportability authority (#792):
    # supportability profiles, function-level external dependencies,
    # time-stamped lifecycle observations, authorized offline-continuity
    # evidence, and replacement-readiness records — new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_83_to_84(connection: sqlite3.Connection) -> None:
    # REV62: #793 single-channel live-observation authority — real-time
    # spectrum/RTA, spectrograph, SPL/Leq history, peak hold, event
    # annotations and captured live traces: new append-only authorities
    # the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_84_to_85(connection: sqlite3.Connection) -> None:
    # REV63: #809 external-benchmark qualification authority — scene
    # mappings, frozen preregistrations and sealed qualification verdicts:
    # new append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_85_to_86(connection: sqlite3.Connection) -> None:
    # REV63: #810 uncertainty-aware validation authority — preregistered
    # validation-uncertainty protocols, per-observable band evaluations,
    # and whole-study validation verdicts: new append-only authorities
    # the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_86_to_87(connection: sqlite3.Connection) -> None:
    # REV63: #811 solver-confidence-bound authority — per-dimension
    # input authorities (material, source directivity, geometry, pose),
    # the solver input envelope and the per-claim bound record: new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_87_to_88(connection: sqlite3.Connection) -> None:
    # REV63: #789 surge/lightning transient-protection authority —
    # protected paths, declared plans, SPD evidence, health
    # observations, staling events and sealed assessments: new
    # append-only authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_88_to_89(connection: sqlite3.Connection) -> None:
    # REV63: #791 operational-energy authority — per-mode power
    # observations bound to exact device/firmware/network state,
    # networked-standby profile evidence, system scenarios and
    # reproducible annualized-energy derivations: new append-only
    # authorities the idempotent baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


def _migrate_89_to_90(connection: sqlite3.Connection) -> None:
    # REV63: #813 owned-room holdout campaign authority — hash-bound
    # preregistrations, role-declared field measurements and sealed
    # promotion verdicts: new append-only authorities the idempotent
    # baseline creates.
    for statement in NATIVE_BASELINE_DDL:
        connection.execute(statement)


_MIGRATIONS = {
    1: _migrate_0_to_1,
    2: _migrate_1_to_2,
    3: _migrate_2_to_3,
    4: _migrate_3_to_4,
    5: _migrate_4_to_5,
    6: _migrate_5_to_6,
    7: _migrate_6_to_7,
    8: _migrate_7_to_8,
    9: _migrate_8_to_9,
    10: _migrate_9_to_10,
    11: _migrate_10_to_11,
    12: _migrate_11_to_12,
    13: _migrate_12_to_13,
    14: _migrate_13_to_14,
    15: _migrate_14_to_15,
    16: _migrate_15_to_16,
    17: _migrate_16_to_17,
    18: _migrate_17_to_18,
    19: _migrate_18_to_19,
    20: _migrate_19_to_20,
    21: _migrate_20_to_21,
    22: _migrate_21_to_22,
    23: _migrate_22_to_23,
    24: _migrate_23_to_24,
    25: _migrate_24_to_25,
    26: _migrate_25_to_26,
    27: _migrate_26_to_27,
    28: _migrate_27_to_28,
    29: _migrate_28_to_29,
    30: _migrate_29_to_30,
    31: _migrate_30_to_31,
    32: _migrate_31_to_32,
    33: _migrate_32_to_33,
    34: _migrate_33_to_34,
    35: _migrate_34_to_35,
    36: _migrate_35_to_36,
    37: _migrate_36_to_37,
    38: _migrate_37_to_38,
    39: _migrate_38_to_39,
    40: _migrate_39_to_40,
    41: _migrate_40_to_41,
    42: _migrate_41_to_42,
    43: _migrate_42_to_43,
    44: _migrate_43_to_44,
    45: _migrate_44_to_45,
    46: _migrate_45_to_46,
    47: _migrate_46_to_47,
    48: _migrate_47_to_48,
    49: _migrate_48_to_49,
    50: _migrate_49_to_50,
    51: _migrate_50_to_51,
    52: _migrate_51_to_52,
    53: _migrate_52_to_53,
    54: _migrate_53_to_54,
    55: _migrate_54_to_55,
    56: _migrate_55_to_56,
    57: _migrate_56_to_57,
    58: _migrate_57_to_58,
    59: _migrate_58_to_59,
    60: _migrate_59_to_60,
    61: _migrate_60_to_61,
    62: _migrate_61_to_62,
    63: _migrate_62_to_63,
    64: _migrate_63_to_64,
    65: _migrate_64_to_65,
    66: _migrate_65_to_66,
    67: _migrate_66_to_67,
    68: _migrate_67_to_68,
    69: _migrate_68_to_69,
    70: _migrate_69_to_70,
    71: _migrate_70_to_71,
    72: _migrate_71_to_72,
    73: _migrate_72_to_73,
    74: _migrate_73_to_74,
    75: _migrate_74_to_75,
    76: _migrate_75_to_76,
    77: _migrate_76_to_77,
    78: _migrate_77_to_78,
    79: _migrate_78_to_79,
    80: _migrate_79_to_80,
    81: _migrate_80_to_81,
    82: _migrate_81_to_82,
    83: _migrate_82_to_83,
    84: _migrate_83_to_84,
    85: _migrate_84_to_85,
    86: _migrate_85_to_86,
    87: _migrate_86_to_87,
    88: _migrate_87_to_88,
    89: _migrate_88_to_89,
    90: _migrate_89_to_90,
}


def ensure_native_schema(path: Path) -> int:
    """Migrate a native CAD database to the supported schema.

    Existing 0.1.0-era databases have no central schema marker. They are adopted
    as v1 only after integrity/foreign-key checks and only when every table
    matches a known pre-versioning HTDT signature. A database from a newer
    application is never opened.

    Durability: each migration step writes the version ledger and
    ``schema_version`` marker last, but domain-convergence helpers commit at
    internal boundaries (``executescript``), so a crash mid-chain can leave
    the stored version at an earlier step boundary rather than inside one.
    Every step is idempotent, so the next open resumes from the last
    committed boundary — never a half-applied step reading as complete.
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
    _reject_empty_database_file(path)
    signature = _db_file_signature(path)
    if (
        signature is not None
        and _ENSURED_SCHEMA_SIGNATURES.get(str(path)) == signature
    ):
        _ENSURED_SCHEMA_SIGNATURES.move_to_end(str(path))
        # This exact file generation already completed the migration
        # authority in this process; any write since would have changed the
        # signature. ``ensure_native_schema`` only ever returns
        # NATIVE_SCHEMA_VERSION on success.
        return NATIVE_SCHEMA_VERSION
    try:
        with closing(connect_sqlite(path)) as connection, connection:
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
        signature = _db_file_signature(path)
        if signature is not None:
            _memoize_schema_signature(
                _ENSURED_SCHEMA_SIGNATURES, str(path), signature
            )
        return version
    except sqlite3.DatabaseError as exc:
        raise NativeSchemaError(f'native database schema migration failed: {exc}') from exc
