from __future__ import annotations

from base64 import b64decode
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal, Mapping
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema, require_native_tables, connect_sqlite
from htdt.capture_bundle import (
    MAX_MANIFEST_BYTES,
    MAX_SOURCE_REF_BYTES,
    MAX_SOURCE_REFS_PER_ENTRY,
    MAX_SOURCE_REFS_TOTAL,
    SOURCE_REF_SENTINELS,
    CaptureBundleError,
    _enforce_reserved_path_metadata,
    _family_for_path,
    _load_schema,
    _schema_document_for_version,
    _validate_source_refs as _validate_bundle_source_refs,
    canonical_json_bytes as _canonical_bundle_bytes,
    parse_json_bytes as _parse_bundle_json,
    validate_manifest_shape,
    validate_payload_set,
)
from htdt.capture_schema_eval import (
    SchemaError as _CaptureSchemaError,
    validate as _schema_validate,
)
from htdt import capture_reference
from htdt.capture_mesh_ingestion import (
    CAPTURE_MESH_BINDING_RECORD_SCHEMA,
    CAPTURE_MESH_BINDING_RECORD_VERSION,
    CaptureMeshHandoff,
    CaptureMeshIngestionError,
    CaptureRawVisualMeshBinding,
    adapt_capture_mesh_handoff,
    parse_mesh_binding_record,
    serialize_mesh_binding_record,
)
from htdt.content_blobs import (
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)
from htdt.limits import (
    MAX_CAPTURE_INGEST_FACE_COUNT,
    MAX_CAPTURE_INGEST_MESH_COUNT,
    MAX_CAPTURE_INGEST_SOURCE_BYTES,
    MAX_CAPTURE_INGEST_SOURCE_EVIDENCE_COUNT,
    MAX_CAPTURE_INGEST_VERTEX_COUNT,
    MAX_CAPTURE_INGEST_WORKING_BYTES,
)
from .canonical_json import canonical_json as _canonical_json, hash_parts as _hash_parts


UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
RUN_ID_RE = re.compile(r'^capture-ingestion-run:[0-9a-f]{64}$')
COORDINATE_AUTHORITY_ID_RE = re.compile(
    r'^capture-coordinate-authority:[0-9a-f]{64}$'
)
SOURCE_EVIDENCE_DOMAIN = 'htdt.capture.source-evidence.v1'
RAW_MESH_HANDOFF_DOMAIN = 'htdt.capture.raw-visual-mesh-handoff.v1'
AUTHORITY_HANDOFF_DOMAIN = 'htdt.capture.authority-record.v1'
SUPPLEMENTAL_DOCUMENT_DOMAIN = 'htdt.capture.supplemental-document.v1'
INGESTION_RUN_DOMAIN = 'htdt.capture.ingestion-run.v1'
COORDINATE_AUTHORITY_DOMAIN = 'htdt.capture.coordinate-authority.v1'
INGESTOR_CONFIGURATION_DIGEST = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)
# The backend contract is explicitly versioned: only these
# (name, version) -> configuration_digest ingestor identities may ingest.
# Adding a new supported ingestor version is a deliberate contract edit,
# never an implicit consequence of the plan payload.
SUPPORTED_INGESTOR_IDENTITIES: Mapping[tuple[str, str], str] = {
    ('htdt-capture-reference-ingestor', '1.0.0'): INGESTOR_CONFIGURATION_DIGEST,
}
# Every byte HTDT persists through this repository arrived via an
# unsigned local .htdtcapture bundle import. That verified import origin is
# deliberately distinct from whatever provenance class the bundle asserts:
# an unsigned bundle may claim ``backend_derived`` and it is still recorded
# under this same untrusted-but-verified origin.
CAPTURE_IMPORT_ORIGIN_UNSIGNED_BUNDLE = 'unsigned_capture_bundle_import'
PERSISTED_INGESTION_INTEGRITY_MISMATCH = (
    'persisted_ingestion_integrity_mismatch'
)

# Estimated transient bytes held while one decoded mesh element is staged as
# frozen pydantic models (model instance, field objects, container slots) and
# while a geometry payload exists both as immutable bytes and as the Base64
# copy embedded in RawVisualMesh. These weights are deliberately generous
# upper bounds; the ingestion budget exists to bound allocation, not to meter
# it exactly.
_VERTEX_WORKING_BYTES = 640
_FACE_WORKING_BYTES = 640
_GEOMETRY_WORKING_FACTOR = 4


CaptureProvenance = Literal[
    'arkit_frame_observation',
    'arkit_scene_depth_observation',
    'arkit_mesh_reconstruction',
    'apple_roomplan_raw_scan',
    'apple_roomplan_inference',
    'user_attested_measurement',
    'user_annotation',
    'imported_reference',
    'capture_app_derived',
    'backend_derived',
]


class CaptureIngestionTransactionError(ValueError):
    pass


class CapturePayloadContractError(CaptureIngestionTransactionError):
    """The exact source payload bytes violate the pinned Capture contract."""


class CaptureRevisionConflictError(CaptureIngestionTransactionError):
    """A capture_revision_id was reused with a different immutable identity."""


class CaptureQualityGateError(CaptureIngestionTransactionError):
    """The persisted quality authority is missing, unsupported or not ready."""


class CaptureIdentityReferenceError(CaptureIngestionTransactionError):
    """Session/frame/record identity or reference claims disagree with the
    accepted bundle identity registry."""


class PersistedIngestionIntegrityError(CaptureIngestionTransactionError):
    """An existing run's persisted materialization is incomplete or corrupt.

    Raised instead of reporting idempotent success when a re-import finds the
    surviving ``capture_ingestion_runs`` row but part of the persisted
    evidence or derived materialization is missing or no longer matches the
    plan authority. The ``diagnostic`` attribute carries the stable
    machine-readable code so operators can distinguish damaged persisted
    state from an invalid incoming archive or plan and route the run to a
    repair/reimport operation.
    """

    diagnostic = PERSISTED_INGESTION_INTEGRITY_MISMATCH

    def __init__(self, detail: str) -> None:
        super().__init__(
            f'{PERSISTED_INGESTION_INTEGRITY_MISMATCH}: {detail}'
        )


def _source_evidence_id(bundle_digest: str, path: str, payload_sha256: str) -> str:
    return _hash_parts(
        SOURCE_EVIDENCE_DOMAIN,
        bundle_digest,
        path,
        payload_sha256,
    )


def _raw_mesh_handoff_id(bundle_digest: str, anchor_id: str, geometry_sha256: str) -> str:
    return _hash_parts(
        RAW_MESH_HANDOFF_DOMAIN,
        bundle_digest,
        anchor_id,
        geometry_sha256,
    )


def _authority_handoff_id(
    bundle_digest: str,
    source_payload_sha256: str,
    record_kind: str,
    record_id: str,
) -> str:
    return _hash_parts(
        AUTHORITY_HANDOFF_DOMAIN,
        bundle_digest,
        source_payload_sha256,
        record_kind,
        record_id,
    )


def _supplemental_handoff_id(
    bundle_digest: str,
    path: str,
    payload_sha256: str,
) -> str:
    return _hash_parts(
        SUPPLEMENTAL_DOCUMENT_DOMAIN,
        bundle_digest,
        path,
        payload_sha256,
    )


def _ingestion_run_id(
    lineage_digest: str,
    ingestor_name: str,
    ingestor_version: str,
    configuration_digest: str,
    plan_sha256: str,
) -> str:
    """Deterministic processing-run identity (#413).

    The run identity is deliberately distinct from ``lineage_digest``: the
    lineage digest is the stable projection of source/handoff identities,
    while the run id additionally binds the exact ingestor software and the
    canonical plan bytes that produced the materialization. Two supported
    ingestor versions (or any changed canonical plan) on the same immutable
    lineage therefore produce two distinct persisted runs without colliding
    on the shared lineage projection.
    """

    return 'capture-ingestion-run:' + _hash_parts(
        INGESTION_RUN_DOMAIN,
        lineage_digest,
        ingestor_name,
        ingestor_version,
        configuration_digest,
        plan_sha256,
    )


def _coordinate_authority_id(
    bundle_digest: str,
    coordinate_space_id: str,
) -> str:
    """Scoped coordinate authority identity (#365).

    A bare ``coordinate_space_id`` UUID is only meaningful inside the exact
    immutable Capture bundle that declared it. Scoping the authority to
    (bundle_digest, coordinate_space_id) keeps two independent bundles that
    claim the same UUID as distinct registered frames instead of letting
    the string collision imply a shared physical frame.
    """

    return 'capture-coordinate-authority:' + _hash_parts(
        COORDINATE_AUTHORITY_DOMAIN,
        bundle_digest,
        coordinate_space_id,
    )


def _validate_plan_source_ref_grammar(plan: 'CaptureIngestionPlan') -> None:
    """Enforce the complete Capture Bundle v1 source_ref grammar on every
    declared record reference.

    Grammar: ``path:`` / ``sha256:`` (must name exactly one declared
    payload, no self-reference), ``capture_session:<uuidv4>`` (must be a
    manifest member), the ``roomplan_raw_serialization:unavailable``
    sentinel, and bounded budgets (per-entry, per-ref-bytes, total).
    Path references must additionally be acyclic and unambiguous — a ref
    that resolves to more than one payload, or to none, is rejected.
    Unknown namespaces fail closed.
    """
    source_hashes = {item.payload_sha256 for item in plan.source_evidence}
    session_ids = set(plan.bundle.capture_session_ids)
    by_path: dict[str, list[str]] = {}
    for item in plan.source_evidence:
        by_path.setdefault(item.path, []).append(item.payload_sha256)

    entries: list[tuple[str, tuple[str, ...]]] = [
        (item.path, item.source_refs) for item in plan.source_evidence
    ]
    entries += [
        (record.path, record.source_refs)
        for record in plan.roomplan_records
    ]

    total_refs = 0
    adjacency: dict[str, set[str]] = {}
    for owner, refs in entries:
        if len(refs) > MAX_SOURCE_REFS_PER_ENTRY:
            raise ValueError(
                f'source_refs exceeds per-entry budget: {owner}'
            )
        total_refs += len(refs)
        for ref in refs:
            if len(ref.encode('utf-8')) > MAX_SOURCE_REF_BYTES:
                raise ValueError('source_ref exceeds byte budget')
            if ref in SOURCE_REF_SENTINELS:
                continue
            if ref.startswith('sha256:'):
                target_hash = ref.removeprefix('sha256:')
                if (
                    not HEX64_RE.fullmatch(target_hash)
                    or target_hash not in source_hashes
                ):
                    raise ValueError(
                        'unresolved source evidence SHA-256 reference'
                    )
                matching = [
                    item.path
                    for item in plan.source_evidence
                    if item.payload_sha256 == target_hash
                ]
                if len(matching) != 1:
                    raise ValueError(
                        'SHA-256 source_ref must name exactly one '
                        'payload'
                    )
                if matching[0] == owner:
                    raise ValueError('source_ref self-reference')
            elif ref.startswith('path:'):
                target_path = ref.removeprefix('path:')
                _validate_logical_path(target_path)
                if target_path == owner:
                    raise ValueError('source_ref self-reference')
                if target_path not in by_path:
                    raise ValueError(
                        'unresolved source evidence path reference'
                    )
                adjacency.setdefault(owner, set()).add(target_path)
            elif ref.startswith('capture_session:'):
                session_id = ref.removeprefix('capture_session:')
                if session_id not in session_ids:
                    raise ValueError(
                        'capture_session source_ref is not a manifest '
                        'session member'
                    )
            else:
                raise ValueError(
                    f'unknown source_ref namespace: {ref.split(":", 1)[0]}'
                )
    if total_refs > MAX_SOURCE_REFS_TOTAL:
        raise ValueError('total source_refs budget exceeded')

    # Path-reference acyclicity (iterative DFS with GRAY marking).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {path: WHITE for path in adjacency}
    for root in adjacency:
        if color[root] != WHITE:
            continue
        stack = [(root, iter(adjacency.get(root, ())))]
        color[root] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color.get(nxt, WHITE) == GRAY:
                    raise ValueError(
                        'path source_ref cycle detected'
                    )
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adjacency.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()




def _repoint_lineage_parent(
    connection: sqlite3.Connection,
    table: str,
    *,
    column_defs: str,
    insert_columns: str,
    error_type: type[Exception],
    label: str,
) -> None:
    """Retarget ``table``'s lineage-digest foreign key to the lineages table.

    #413 demoted ``capture_ingestion_runs.lineage_digest`` from the runs
    primary key to a non-unique projection (several processing runs may
    share one lineage), so it can no longer parent a foreign key. The
    shared ``capture_ingestion_lineages`` table is the unique lineage
    parent; rebuilds ``table`` when its stored key still targets the runs
    table or the dropped migration rename.
    """
    # Seed lineage rows from whatever run shape is present so child rows
    # retain a valid parent; needed even when no rebuild runs, since a
    # database opened before this contract may hold runs its lineage
    # table never knew about.
    run_tables = {
        str(row['name'])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for name in ('capture_ingestion_runs', 'capture_ingestion_runs_legacy'):
        if name in run_tables:
            connection.execute(
                f'''
                INSERT OR IGNORE INTO capture_ingestion_lineages(
                    lineage_digest
                )
                SELECT DISTINCT lineage_digest FROM {name}
                '''
            )
    parents = {
        str(row['table'])
        for row in connection.execute(f'PRAGMA foreign_key_list({table})')
        if str(row['from']) == 'lineage_digest'
    }
    if parents == {'capture_ingestion_lineages'}:
        return
    if connection.in_transaction:
        connection.commit()
    # foreign_keys must be OFF during the rebuild: renaming the table
    # otherwise rewrites the references other tables hold on it to the
    # dropped legacy name.
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(f'ALTER TABLE {table} RENAME TO {table}_legacy')
        connection.execute(f'CREATE TABLE {table} (\n{column_defs}\n)')
        connection.execute(
            f'''
            INSERT INTO {table}({insert_columns})
            SELECT {insert_columns}
            FROM {table}_legacy
            '''
        )
        connection.execute(f'DROP TABLE {table}_legacy')
        orphans = connection.execute(
            f'PRAGMA foreign_key_check({table})'
        ).fetchall()
        if orphans:
            connection.rollback()
            raise error_type(
                f'cannot retarget {label} to the lineages table: '
                'unresolved foreign keys remain'
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute('PRAGMA foreign_keys=ON')


def _validate_logical_path(value: str) -> str:
    if unicodedata.normalize('NFC', value) != value:
        raise ValueError('capture logical path must be NFC-normalized')
    if (
        not value
        or value.startswith('/')
        or '\\' in value
        or '\x00' in value
    ):
        raise ValueError('capture logical path must be a relative POSIX path')
    parts = value.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise ValueError('capture logical path contains invalid segment')
    return value


def _binding_handoff_source_ids(
    payload_json: str,
    *,
    binding_id: str,
) -> tuple[str, str]:
    """Extract the canonical source authorities from a persisted binding.

    Both persisted representations (legacy embedded binding dump and the
    compact handoff record) carry the validated handoff under ``handoff``;
    the pair is revalidated through the typed model so a payload that cannot
    produce the canonical ids fails migration instead of guessing.
    """

    try:
        data = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise CaptureIngestionTransactionError(
            f'persisted mesh binding {binding_id} payload is not valid JSON; '
            'cannot normalize provenance columns'
        ) from exc
    handoff = data.get('handoff') if isinstance(data, dict) else None
    try:
        typed = CaptureMeshHandoff.model_validate(handoff)
    except ValueError as exc:
        raise CaptureIngestionTransactionError(
            f'persisted mesh binding {binding_id} handoff record is invalid; '
            'cannot normalize provenance columns'
        ) from exc
    return (
        typed.anchor_index_source_evidence_id,
        typed.geometry_source_evidence_id,
    )


class CaptureIngestorIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    name: str
    version: str
    configuration_digest: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def _validate_supported(self) -> 'CaptureIngestorIdentity':
        expected = SUPPORTED_INGESTOR_IDENTITIES.get((self.name, self.version))
        if expected is None or expected != self.configuration_digest:
            raise ValueError('unsupported capture ingestor identity')
        return self


class CaptureBundleIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    bundle_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    capture_schema: Literal['htdt.capture.bundle']
    capture_schema_version: Literal['1.0.0']
    capture_series_id: str
    capture_revision_id: str
    parent_revision_id: str | None
    capture_session_ids: tuple[str, ...]
    coordinate_space_ids: tuple[str, ...]

    @model_validator(mode='after')
    def validate_identity(self) -> 'CaptureBundleIdentity':
        for label, value in (
            ('capture_series_id', self.capture_series_id),
            ('capture_revision_id', self.capture_revision_id),
        ):
            if not UUID4_RE.fullmatch(value):
                raise ValueError(f'{label} must be lowercase UUIDv4')
        if self.parent_revision_id is not None and not UUID4_RE.fullmatch(
            self.parent_revision_id
        ):
            raise ValueError('parent_revision_id must be lowercase UUIDv4')
        if self.parent_revision_id == self.capture_revision_id:
            raise ValueError(
                'parent_revision_id must differ from capture_revision_id'
            )
        if not self.capture_session_ids or len(set(self.capture_session_ids)) != len(
            self.capture_session_ids
        ):
            raise ValueError('capture_session_ids must be non-empty and unique')
        if not self.coordinate_space_ids or len(set(self.coordinate_space_ids)) != len(
            self.coordinate_space_ids
        ):
            raise ValueError('coordinate_space_ids must be non-empty and unique')
        if any(not UUID4_RE.fullmatch(value) for value in self.capture_session_ids):
            raise ValueError('capture_session_ids must contain lowercase UUIDv4 values')
        if any(not UUID4_RE.fullmatch(value) for value in self.coordinate_space_ids):
            raise ValueError('coordinate_space_ids must contain lowercase UUIDv4 values')
        return self


class CaptureSourceEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    bundle_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    capture_revision_id: str
    path: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    provenance_class: CaptureProvenance
    role: Literal['canonical', 'derived']
    source_refs: tuple[str, ...] = ()

    @field_validator('capture_revision_id')
    @classmethod
    def validate_revision_id(cls, value: str) -> str:
        if not UUID4_RE.fullmatch(value):
            raise ValueError('capture_revision_id must be lowercase UUIDv4')
        return value

    @field_validator('source_refs')
    @classmethod
    def validate_source_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item for item in value):
            raise ValueError('source_refs must be non-empty and unique')
        return value


class CaptureRoomPlanCaptureMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.captured-room-metadata']
    schema_version: Literal['1.0.0']
    capture_revision_id: str
    capture_session_id: str
    coordinate_space_id: str
    raw_payload_path: str
    raw_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    processed_payload_path: str | None = None
    processed_sha256: str | None = None
    surface_count: int | None = None
    object_count: int | None = None
    dimensions: dict[str, float] | None = None


class CaptureRoomPlanRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['raw_scan', 'postprocessed_inference']
    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    path: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provenance_class: Literal[
        'apple_roomplan_raw_scan',
        'apple_roomplan_inference',
    ]
    source_refs: tuple[str, ...] = ()
    roomplan_capture_metadata: CaptureRoomPlanCaptureMetadata | None = None


class CaptureResolvedReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal[
        'annotation_authored',
        'source_evidence',
        'raw_visual_mesh_handoff',
        'authority_record',
    ]
    ref: str = Field(min_length=1)
    target: str | None = None


class CaptureAuthorityRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_record_handoff_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    record_kind: Literal['annotation', 'measurement']
    record_id: str
    record_locator: str = Field(min_length=1)
    provenance_class: CaptureProvenance
    coordinate_space_id: str | None = None
    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_refs: tuple[str, ...] = ()
    endpoint_refs: tuple[str, ...] = ()
    resolved_evidence: tuple[CaptureResolvedReference, ...] = ()
    resolved_endpoints: tuple[CaptureResolvedReference, ...] = ()

    @field_validator('record_id')
    @classmethod
    def validate_record_id(cls, value: str) -> str:
        if not UUID4_RE.fullmatch(value):
            raise ValueError('record_id must be lowercase UUIDv4')
        return value

    @field_validator('coordinate_space_id')
    @classmethod
    def validate_coordinate_space_id(cls, value: str | None) -> str | None:
        if value is not None and not UUID4_RE.fullmatch(value):
            raise ValueError('coordinate_space_id must be lowercase UUIDv4')
        return value


# Supplemental semantic/workflow authorities newer Capture bundles persist
# alongside annotations/measurements (#564). A *supported* kind gets a typed
# handoff; any other declared kind is retained verbatim as 'unsupported' so a
# newer producer's payload never silently disappears at the ingestion
# boundary. Preservation does not require widening a record_kind enum.
SUPPORTED_SUPPLEMENTAL_KINDS = frozenset({
    'capture_task_plan',
    'task_plan_status',
    'connected_spaces',
    'reference_targets',
    'derived_geometry_candidates',
    'as_built_verification',
})

SUPPLEMENTAL_DOCUMENT_PATHS = {
    'capture_task_plan': 'session/capture-task-plan.json',
    'task_plan_status': 'session/task-plan-status.json',
    'connected_spaces': 'session/connected-spaces.json',
    'reference_targets': 'evidence/reference-targets.json',
    'derived_geometry_candidates': 'derived/geometry-candidates.json',
    'as_built_verification': 'verification/as-built.json',
}


class CaptureSupplementalDocument(BaseModel):
    """Typed preservation contract for one supplemental authority document.

    Carries source path, exact payload hash, declared schema/version and the
    semantic document kind with explicit coordinate/session dependency refs.
    ``validation_state='unsupported'`` preserves a valid-but-unknown document
    instead of dropping it; malformed declared payloads fail at plan
    validation when they cannot satisfy this contract.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    supplemental_document_handoff_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_kind: str = Field(min_length=1)
    validation_state: Literal['supported', 'unsupported']
    schema: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    path: str = Field(min_length=1)
    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    coordinate_space_ids: tuple[str, ...] = ()
    capture_session_ids: tuple[str, ...] = ()
    plan_payload_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @field_validator('coordinate_space_ids', 'capture_session_ids')
    @classmethod
    def validate_uuid_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError('dependency references must be unique')
        for item in value:
            if not UUID4_RE.fullmatch(item):
                raise ValueError('dependency references must be lowercase UUIDv4')
        return value

    @model_validator(mode='after')
    def validate_support(self) -> 'CaptureSupplementalDocument':
        supported = self.document_kind in SUPPORTED_SUPPLEMENTAL_KINDS
        if supported and self.validation_state != 'supported':
            raise ValueError(
                'supported supplemental kind requires supported state'
            )
        if not supported and self.validation_state != 'unsupported':
            raise ValueError(
                'unknown supplemental kind must be preserved as unsupported'
            )
        expected_path = SUPPLEMENTAL_DOCUMENT_PATHS.get(self.document_kind)
        if expected_path is not None and self.path != expected_path:
            raise ValueError(
                f'supplemental {self.document_kind} must use path '
                f'{expected_path}'
            )
        return self


class CaptureIngestionPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema: Literal['htdt.capture.ingestion-plan']
    schema_version: Literal['1.0.0']
    ingestor: CaptureIngestorIdentity
    bundle: CaptureBundleIdentity
    source_evidence: tuple[CaptureSourceEvidence, ...]
    roomplan_records: tuple[CaptureRoomPlanRecord, ...]
    roomplan_capture_metadata: CaptureRoomPlanCaptureMetadata | None = None
    raw_visual_mesh_handoffs: tuple[CaptureMeshHandoff, ...]
    authority_records: tuple[CaptureAuthorityRecord, ...]
    supplemental_documents: tuple[CaptureSupplementalDocument, ...] = ()
    lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_lineage(self) -> 'CaptureIngestionPlan':
        bundle_digest = self.bundle.bundle_digest
        revision_id = self.bundle.capture_revision_id

        source_ids = [item.source_evidence_id for item in self.source_evidence]
        source_paths = [item.path for item in self.source_evidence]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError('duplicate source_evidence_id')
        if len(set(source_paths)) != len(source_paths):
            raise ValueError('duplicate source evidence path')
        for path in source_paths:
            _validate_logical_path(path)
        casefolded_paths = [path.casefold() for path in source_paths]
        if len(set(casefolded_paths)) != len(casefolded_paths):
            raise ValueError('case-colliding source evidence path')

        by_id = {item.source_evidence_id: item for item in self.source_evidence}
        by_path = {item.path: item for item in self.source_evidence}
        source_hashes = {item.payload_sha256 for item in self.source_evidence}

        for item in self.source_evidence:
            if item.bundle_digest != bundle_digest:
                raise ValueError('source evidence bundle digest mismatch')
            if item.capture_revision_id != revision_id:
                raise ValueError('source evidence capture revision mismatch')
            expected = _source_evidence_id(
                bundle_digest,
                item.path,
                item.payload_sha256,
            )
            if item.source_evidence_id != expected:
                raise ValueError('source evidence deterministic identity mismatch')
        _validate_plan_source_ref_grammar(self)

        roomplan_keys: set[tuple[str, str]] = set()
        for record in self.roomplan_records:
            key = (record.kind, record.source_evidence_id)
            if key in roomplan_keys:
                raise ValueError('duplicate RoomPlan record')
            roomplan_keys.add(key)
            source = by_id.get(record.source_evidence_id)
            if source is None:
                raise ValueError('RoomPlan source evidence missing')
            if source.path != record.path or source.payload_sha256 != record.payload_sha256:
                raise ValueError('RoomPlan source evidence mismatch')
            if source.provenance_class != record.provenance_class:
                raise ValueError('RoomPlan provenance mismatch')
            expected_roomplan = {
                'raw_scan': (
                    'apple_roomplan_raw_scan',
                    'roomplan/captured-room-data.json',
                ),
                'postprocessed_inference': (
                    'apple_roomplan_inference',
                    'roomplan/captured-room.json',
                ),
            }[record.kind]
            if (
                record.provenance_class != expected_roomplan[0]
                or record.path != expected_roomplan[1]
            ):
                raise ValueError('RoomPlan kind/path/provenance mismatch')
            if record.source_refs != source.source_refs:
                raise ValueError('RoomPlan source refs mismatch')

        handoff_ids: list[str] = []
        for handoff in self.raw_visual_mesh_handoffs:
            if handoff.bundle_digest != bundle_digest:
                raise ValueError('raw mesh handoff bundle digest mismatch')
            if handoff.capture_session_id not in self.bundle.capture_session_ids:
                raise ValueError('raw mesh handoff capture session mismatch')
            if handoff.coordinate_space_id not in self.bundle.coordinate_space_ids:
                raise ValueError('raw mesh handoff coordinate space mismatch')
            expected = _raw_mesh_handoff_id(
                bundle_digest,
                handoff.anchor_id,
                handoff.geometry_sha256,
            )
            if handoff.raw_visual_mesh_handoff_id != expected:
                raise ValueError('raw mesh handoff deterministic identity mismatch')
            index_source = by_id.get(handoff.anchor_index_source_evidence_id)
            geometry_source = by_id.get(handoff.geometry_source_evidence_id)
            if index_source is None or geometry_source is None:
                raise ValueError('raw mesh handoff source evidence missing')
            if (
                index_source.path != 'mesh/anchors.json'
                or index_source.provenance_class != 'arkit_mesh_reconstruction'
            ):
                raise ValueError('raw mesh anchor-index authority mismatch')
            if geometry_source.path != handoff.geometry_path:
                raise ValueError('raw mesh geometry path mismatch')
            if geometry_source.provenance_class != 'arkit_mesh_reconstruction':
                raise ValueError('raw mesh geometry provenance mismatch')
            if geometry_source.payload_sha256 != handoff.geometry_sha256:
                raise ValueError('raw mesh geometry hash mismatch')
            expected_locator = (
                f'mesh/anchors.json#anchor:{handoff.anchor_id}'
            )
            if handoff.anchor_record_locator != expected_locator:
                raise ValueError('raw mesh anchor locator mismatch')
            handoff_ids.append(handoff.raw_visual_mesh_handoff_id)
        if len(set(handoff_ids)) != len(handoff_ids):
            raise ValueError('duplicate raw mesh handoff identity')

        authority_ids: list[str] = []
        for record in self.authority_records:
            source = by_id.get(record.source_evidence_id)
            if source is None:
                raise ValueError('authority record source evidence missing')
            if source.payload_sha256 != record.source_payload_sha256:
                raise ValueError('authority record source hash mismatch')
            expected_source_path = {
                'annotation': 'annotations/entities.json',
                'measurement': 'annotations/measurements.json',
            }[record.record_kind]
            if source.path != expected_source_path:
                raise ValueError('authority record source path mismatch')
            expected_locator = (
                f'{expected_source_path}#'
                f'{record.record_kind}:{record.record_id}'
            )
            if record.record_locator != expected_locator:
                raise ValueError('authority record locator mismatch')
            if (
                record.coordinate_space_id is not None
                and record.coordinate_space_id not in self.bundle.coordinate_space_ids
            ):
                raise ValueError('authority record coordinate space mismatch')
            expected = _authority_handoff_id(
                bundle_digest,
                record.source_payload_sha256,
                record.record_kind,
                record.record_id,
            )
            if record.authority_record_handoff_id != expected:
                raise ValueError('authority record deterministic identity mismatch')
            authority_ids.append(record.authority_record_handoff_id)
        if len(set(authority_ids)) != len(authority_ids):
            raise ValueError('duplicate authority record handoff identity')

        supplemental_ids: list[str] = []
        source_hash_by_path = {item.path: item.payload_sha256 for item in self.source_evidence}
        for document in self.supplemental_documents:
            source = by_id.get(document.source_evidence_id)
            if source is None:
                raise ValueError('supplemental document source evidence missing')
            if source.path != document.path:
                raise ValueError('supplemental document source path mismatch')
            if source.payload_sha256 != document.source_payload_sha256:
                raise ValueError('supplemental document source hash mismatch')
            for space_id in document.coordinate_space_ids:
                if space_id not in self.bundle.coordinate_space_ids:
                    raise ValueError(
                        'supplemental document coordinate space mismatch'
                    )
            for session_id in document.capture_session_ids:
                if session_id not in self.bundle.capture_session_ids:
                    raise ValueError(
                        'supplemental document capture session mismatch'
                    )
            if (
                document.plan_payload_sha256 is not None
                and document.plan_payload_sha256
                not in set(source_hash_by_path.values())
            ):
                raise ValueError(
                    'supplemental document plan reference does not resolve to '
                    'a bundle payload'
                )
            expected = _supplemental_handoff_id(
                bundle_digest,
                document.path,
                document.source_payload_sha256,
            )
            if document.supplemental_document_handoff_id != expected:
                raise ValueError(
                    'supplemental document deterministic identity mismatch'
                )
            supplemental_ids.append(document.supplemental_document_handoff_id)
        if len(set(supplemental_ids)) != len(supplemental_ids):
            raise ValueError('duplicate supplemental document handoff identity')

        projection = {
            'bundle_digest': bundle_digest,
            'source_evidence_ids': sorted(source_ids),
            'raw_visual_mesh_ids': sorted(handoff_ids),
            'authority_record_ids': sorted(authority_ids),
        }
        # Older plans carry no supplemental set; including the key only when
        # documents exist preserves their recorded lineage digests.
        if supplemental_ids:
            projection['supplemental_document_ids'] = sorted(supplemental_ids)
        expected_lineage = sha256(
            _canonical_json(projection).encode('utf-8')
        ).hexdigest()
        if self.lineage_digest != expected_lineage:
            raise ValueError('ingestion plan lineage digest mismatch')
        return self


@dataclass(frozen=True)
class CaptureIngestionCommitResult:
    ingestion_run_id: str
    lineage_digest: str
    bundle_digest: str
    source_evidence_count: int
    roomplan_record_count: int
    raw_mesh_binding_count: int
    authority_record_count: int
    created: bool
    capture_revision_id: str = ''
    capture_series_id: str = ''
    quality_state: str = 'unresolved'
    quality_ruleset_version: str | None = None
    quality_payload_sha256: str | None = None
    supplemental_document_count: int = 0


@dataclass(frozen=True)
class CaptureRevisionRecord:
    """One immutable registered Capture revision (#335)."""

    capture_revision_id: str
    capture_series_id: str
    parent_revision_id: str | None
    bundle_digest: str
    capture_schema: str
    capture_schema_version: str
    topology_state: str
    first_lineage_digest: str
    registered_at_utc: str


@dataclass(frozen=True)
class CaptureRevisionConflict:
    """A pre-existing or newly detected revision-identity conflict."""

    capture_revision_id: str
    detail: str


@dataclass(frozen=True)
class CaptureBundleRecord:
    """The retained canonical manifest authority for one bundle (#338)."""

    bundle_digest: str
    capture_revision_id: str
    app_name: str
    app_version: str
    app_build: str
    created_at: str
    finalized_at: str
    manifest_bytes: bytes


@dataclass(frozen=True)
class CaptureQualityState:
    """Persisted quality-gate state for one ingestion run (#337)."""

    state: str
    payload_sha256: str | None
    ruleset_version: str | None


@dataclass(frozen=True)
class CaptureIngestionRun:
    """Persisted processing-run record (#413).

    The run is one exact (ingestor identity, canonical plan) execution over
    an immutable lineage. ``lineage_digest`` stays the shared projection of
    source/handoff identities; several runs may legitimately share it when
    the contract later supports a second ingestor version.
    """

    ingestion_run_id: str
    lineage_digest: str
    plan_sha256: str
    bundle_digest: str
    capture_revision_id: str
    capture_series_id: str
    parent_revision_id: str | None
    capture_session_ids: tuple[str, ...]
    coordinate_space_ids: tuple[str, ...]
    ingestor_name: str
    ingestor_version: str
    configuration_digest: str
    recorded_at_utc: str


@dataclass(frozen=True)
class CaptureCoordinateAuthority:
    """Scoped coordinate authority for one immutable imported frame (#365).

    The authority id binds a coordinate-space UUID to the exact bundle
    (therefore revision) that declared it. Two bundles claiming the same
    UUID hold two different authorities, so a world-to-scene alignment made
    for one can never silently satisfy the other.
    """

    coordinate_authority_id: str
    bundle_digest: str
    capture_revision_id: str
    coordinate_space_id: str
    registered_by_run_id: str
    recorded_at_utc: str


@dataclass(frozen=True)
class CaptureRevisionSummary:
    """Library view of one imported Capture revision (#353).

    Derived entirely from normalized run/link rows — listing it never
    loads source payload BLOBs. ``parent_known=False`` marks a revision
    whose declared parent is not persisted (imported mid-series), which is
    legitimate state, not corruption. ``promotion_ids`` / scene ids expose
    the promotion dependency edges a retention policy must honor.
    """

    capture_revision_id: str
    capture_series_id: str
    bundle_digest: str
    parent_revision_id: str | None
    parent_known: bool
    capture_session_ids: tuple[str, ...]
    coordinate_space_ids: tuple[str, ...]
    lineage_digests: tuple[str, ...]
    ingestion_run_ids: tuple[str, ...]
    ingestor_versions: tuple[str, ...]
    source_evidence_count: int
    raw_mesh_binding_ids: tuple[str, ...]
    authority_record_count: int
    roomplan_record_count: int
    promotion_ids: tuple[str, ...]
    promoted_scene_revision_ids: tuple[str, ...]
    first_recorded_at_utc: str
    last_recorded_at_utc: str


@dataclass(frozen=True)
class CaptureProvenanceTrust:
    """Trust view separating asserted labels from verified origin (#412).

    ``asserted_provenance_class`` and ``asserted_producer`` are labels the
    (unsigned) bundle claimed about itself — ``backend_derived`` included.
    ``verified_import_origin`` is how HTDT actually obtained the bytes;
    ``producer_authenticated`` stays False until a signed import path
    exists, so nothing downstream can mistake an asserted label for a
    verified one.
    """

    source_evidence_id: str
    asserted_provenance_class: str
    asserted_producer: str
    verified_import_origin: str
    producer_authenticated: bool


@dataclass(frozen=True)
class CaptureIngestionBudget:
    """Aggregate per-ingest bounds enforced on declared plan values.

    All checks run before any payload is hashed, parsed, or staged so that
    oversized or adversarial manifests fail before large allocations. The
    defaults come from htdt.limits and stay well inside the native backup
    member ceiling once payloads are deduplicated.
    """

    max_source_evidence_count: int = MAX_CAPTURE_INGEST_SOURCE_EVIDENCE_COUNT
    max_source_payload_bytes: int = MAX_CAPTURE_INGEST_SOURCE_BYTES
    max_mesh_count: int = MAX_CAPTURE_INGEST_MESH_COUNT
    max_vertex_count: int = MAX_CAPTURE_INGEST_VERTEX_COUNT
    max_face_count: int = MAX_CAPTURE_INGEST_FACE_COUNT
    max_working_bytes: int = MAX_CAPTURE_INGEST_WORKING_BYTES


DEFAULT_CAPTURE_INGESTION_BUDGET = CaptureIngestionBudget()


@dataclass(frozen=True)
class PersistedCaptureSourceEvidence:
    record: CaptureSourceEvidence
    payload: bytes


class CaptureIngestionRepository:
    """Atomic production boundary for validated HTDT-Capture ingestion plans."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = Path(scene_repository.path)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'capture_ingestion_lineages',
                'capture_ingestion_runs',
                'capture_source_evidence',
                'capture_ingestion_source_links',
                'capture_roomplan_records',
                'capture_raw_visual_mesh_bindings',
                'capture_ingestion_mesh_links',
                'capture_authority_records',
                'capture_revisions',
                'capture_bundles',
                'capture_revision_conflicts',
                'capture_ingestion_authority_links',
                'capture_coordinate_authorities',
                'htdt_content_blobs',
            )

    def _converge_schema(self, connection: sqlite3.Connection) -> None:
        """Legacy-shape tail of the schema-authority migration (#302).

        Plain ``CREATE TABLE`` lives in ``cad_schema_ddl`` and runs inside
        the versioned migration; this sequence converges databases whose
        persisted shapes predate the canonical contract (rebuilds ordered
        around foreign-key rewrites, payload-driven backfills) and then
        installs the capture tables so every supported open path converges
        the same way. Invoked by ``ensure_native_schema`` and re-verified
        idempotently here.
        """
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS capture_ingestion_lineages (
                lineage_digest TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS capture_ingestion_runs (
                ingestion_run_id TEXT PRIMARY KEY,
                lineage_digest TEXT NOT NULL,
                plan_sha256 TEXT NOT NULL,
                bundle_digest TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                capture_series_id TEXT NOT NULL,
                parent_revision_id TEXT,
                capture_session_ids_json TEXT NOT NULL,
                coordinate_space_ids_json TEXT NOT NULL,
                ingestor_name TEXT NOT NULL,
                ingestor_version TEXT NOT NULL,
                configuration_digest TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                FOREIGN KEY(lineage_digest)
                    REFERENCES capture_ingestion_lineages(lineage_digest)
            );

            CREATE TABLE IF NOT EXISTS capture_source_evidence (
                source_evidence_id TEXT PRIMARY KEY,
                bundle_digest TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                logical_path TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                producer TEXT NOT NULL,
                provenance_class TEXT NOT NULL,
                role TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                import_origin TEXT NOT NULL
                    DEFAULT 'unsigned_capture_bundle_import',
                payload_blob BLOB NOT NULL,
                UNIQUE(bundle_digest, logical_path)
            );

            CREATE TABLE IF NOT EXISTS capture_ingestion_source_links (
                ingestion_run_id TEXT NOT NULL,
                source_evidence_id TEXT NOT NULL,
                PRIMARY KEY(ingestion_run_id, source_evidence_id),
                FOREIGN KEY(ingestion_run_id)
                    REFERENCES capture_ingestion_runs(ingestion_run_id),
                FOREIGN KEY(source_evidence_id)
                    REFERENCES capture_source_evidence(source_evidence_id)
            );

            CREATE TABLE IF NOT EXISTS capture_roomplan_records (
                ingestion_run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_evidence_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(
                    ingestion_run_id, kind, source_evidence_id
                ),
                FOREIGN KEY(ingestion_run_id)
                    REFERENCES capture_ingestion_runs(ingestion_run_id),
                FOREIGN KEY(source_evidence_id)
                    REFERENCES capture_source_evidence(source_evidence_id)
            );

            CREATE TABLE IF NOT EXISTS capture_raw_visual_mesh_bindings (
                binding_id TEXT PRIMARY KEY,
                handoff_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                anchor_index_source_evidence_id TEXT
                    REFERENCES capture_source_evidence(source_evidence_id)
                    ON DELETE RESTRICT,
                geometry_source_evidence_id TEXT
                    REFERENCES capture_source_evidence(source_evidence_id)
                    ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS capture_ingestion_mesh_links (
                ingestion_run_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                PRIMARY KEY(ingestion_run_id, binding_id),
                FOREIGN KEY(ingestion_run_id)
                    REFERENCES capture_ingestion_runs(ingestion_run_id),
                FOREIGN KEY(binding_id)
                    REFERENCES capture_raw_visual_mesh_bindings(binding_id)
            );

            CREATE TABLE IF NOT EXISTS capture_authority_records (
                authority_record_handoff_id TEXT PRIMARY KEY,
                source_evidence_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(source_evidence_id)
                    REFERENCES capture_source_evidence(source_evidence_id)
            );

            CREATE TABLE IF NOT EXISTS capture_revisions (
                capture_revision_id TEXT PRIMARY KEY,
                capture_series_id TEXT NOT NULL,
                parent_revision_id TEXT,
                bundle_digest TEXT NOT NULL,
                capture_schema TEXT NOT NULL,
                capture_schema_version TEXT NOT NULL,
                topology_state TEXT NOT NULL,
                first_lineage_digest TEXT NOT NULL,
                registered_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS capture_bundles (
                bundle_digest TEXT PRIMARY KEY,
                capture_revision_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                app_name TEXT NOT NULL,
                app_version TEXT NOT NULL,
                app_build TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finalized_at TEXT NOT NULL,
                manifest_blob BLOB NOT NULL DEFAULT X''
            );

            CREATE TABLE IF NOT EXISTS capture_revision_conflicts (
                capture_revision_id TEXT NOT NULL,
                detail TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL,
                PRIMARY KEY(capture_revision_id, detail)
            );

            CREATE TABLE IF NOT EXISTS capture_ingestion_authority_links (
                ingestion_run_id TEXT NOT NULL,
                authority_record_handoff_id TEXT NOT NULL,
                PRIMARY KEY(
                    ingestion_run_id, authority_record_handoff_id
                ),
                FOREIGN KEY(ingestion_run_id)
                    REFERENCES capture_ingestion_runs(ingestion_run_id),
                FOREIGN KEY(authority_record_handoff_id)
                    REFERENCES capture_authority_records(
                        authority_record_handoff_id
                    )
            );

            '''
        )
        self._ensure_run_quality_columns(connection)
        self._migrate_revision_registry(connection)
        ensure_content_blob_store(connection)
        self._migrate_run_identity(connection)
        # The run-identity rebuild swaps in a fresh runs table; re-add
        # the persisted quality columns to it.
        self._ensure_run_quality_columns(connection)
        # Lineage rows are the unique foreign-key parent shared by the
        # run-scoped schema and the lineage-keyed capture tables; seed
        # them for databases already holding new-shape run rows.
        connection.execute(
            '''
            INSERT OR IGNORE INTO capture_ingestion_lineages(
                lineage_digest
            )
            SELECT DISTINCT lineage_digest FROM capture_ingestion_runs
            '''
        )
        # Created after the run-identity rebuild: under foreign_keys=ON
        # ALTER TABLE ... RENAME rewrites references to the dropped
        # _legacy name, so a table that persists must not reference the
        # renamed capture_ingestion_runs until the new one exists.
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS capture_coordinate_authorities (
                coordinate_authority_id TEXT PRIMARY KEY,
                bundle_digest TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                coordinate_space_id TEXT NOT NULL,
                registered_by_run_id TEXT NOT NULL
                    REFERENCES capture_ingestion_runs(ingestion_run_id),
                recorded_at_utc TEXT NOT NULL,
                UNIQUE(bundle_digest, coordinate_space_id)
            )
            '''
        )
        self._migrate_source_import_origin(connection)
        self._register_persisted_coordinate_authorities(connection)
        self._externalize_inline_source_payloads(connection)
        self._compact_legacy_mesh_bindings(connection)
        self._normalize_mesh_binding_sources(connection)
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS
                idx_capture_ingestion_runs_lineage
            ON capture_ingestion_runs(lineage_digest)
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS
                idx_capture_ingestion_runs_revision
            ON capture_ingestion_runs(capture_revision_id)
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS
                idx_capture_ingestion_runs_series
            ON capture_ingestion_runs(capture_series_id)
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS
                idx_capture_coordinate_authority_scope
            ON capture_coordinate_authorities(
                bundle_digest, coordinate_space_id
            )
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_capture_mesh_binding_anchor_source
            ON capture_raw_visual_mesh_bindings(
                anchor_index_source_evidence_id
            )
            '''
        )
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_capture_mesh_binding_geometry_source
            ON capture_raw_visual_mesh_bindings(
                geometry_source_evidence_id
            )
            '''
        )

    def _migrate_run_identity(self, connection: sqlite3.Connection) -> None:
        """Split persisted run identity from the stable lineage digest (#413).

        Databases written before this contract stored
        ``capture_ingestion_runs(lineage_digest PRIMARY KEY)`` and keyed
        every link table by that projection. Rebuilding is provenance-safe:
        the deterministic run id derives only from values the legacy row
        already persisted (lineage digest, ingestor name/version,
        configuration digest, canonical plan bytes), so the migration maps
        each legacy row to the exact run id the current ingest path would
        have assigned. Since the legacy primary key guaranteed at most one
        run per lineage, every legacy link row rebinds to that one run.
        """

        run_columns = {
            str(row['name'])
            for row in connection.execute(
                'PRAGMA table_info(capture_ingestion_runs)'
            )
        }
        if 'ingestion_run_id' in run_columns:
            return
        legacy = connection.execute(
            '''
            SELECT lineage_digest, bundle_digest, capture_revision_id,
                   ingestor_name, ingestor_version, configuration_digest,
                   plan_json, recorded_at_utc
            FROM capture_ingestion_runs
            ORDER BY lineage_digest
            '''
        ).fetchall()
        rows: list[tuple[str, ...]] = []
        for row in legacy:
            try:
                typed = CaptureIngestionPlan.model_validate_json(
                    row['plan_json']
                )
            except (ValueError, TypeError) as exc:
                raise CaptureIngestionTransactionError(
                    'cannot migrate ingestion run identity: persisted plan '
                    f"for lineage {row['lineage_digest']} does not validate "
                    f'({exc})'
                ) from exc
            if typed.lineage_digest != row['lineage_digest']:
                raise CaptureIngestionTransactionError(
                    'cannot migrate ingestion run identity: persisted run '
                    'lineage disagrees with its plan'
                )
            plan_sha256 = sha256(
                str(row['plan_json']).encode('utf-8')
            ).hexdigest()
            run_id = _ingestion_run_id(
                typed.lineage_digest,
                str(row['ingestor_name']),
                str(row['ingestor_version']),
                str(row['configuration_digest']),
                plan_sha256,
            )
            rows.append(
                (
                    run_id,
                    typed.lineage_digest,
                    plan_sha256,
                    typed.bundle.bundle_digest,
                    typed.bundle.capture_revision_id,
                    typed.bundle.capture_series_id,
                    typed.bundle.parent_revision_id,
                    _canonical_json(list(typed.bundle.capture_session_ids)),
                    _canonical_json(list(typed.bundle.coordinate_space_ids)),
                    typed.ingestor.name,
                    typed.ingestor.version,
                    typed.ingestor.configuration_digest,
                    str(row['plan_json']),
                    str(row['recorded_at_utc']),
                )
            )

        # RENAME/CREATE/DROP are autocommitted when no transaction is open,
        # so the rebuild runs inside an explicit transaction: either the
        # whole run-scoped schema replaces the lineage-scoped one or
        # nothing changes and a later open retries deterministically. Any
        # table that survives the migration must not reference the renamed
        # table at rename time — foreign_keys=ON rewrites such references
        # to the dropped _legacy name — so capture_coordinate_authorities
        # is created after this migration, not in the DDL above it.
        if connection.in_transaction:
            connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute(
                '''
                ALTER TABLE capture_ingestion_runs
                RENAME TO capture_ingestion_runs_legacy
                '''
            )
            connection.execute(
                '''
                CREATE TABLE capture_ingestion_runs (
                    ingestion_run_id TEXT PRIMARY KEY,
                    lineage_digest TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    capture_revision_id TEXT NOT NULL,
                    capture_series_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    capture_session_ids_json TEXT NOT NULL,
                    coordinate_space_ids_json TEXT NOT NULL,
                    ingestor_name TEXT NOT NULL,
                    ingestor_version TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    FOREIGN KEY(lineage_digest)
                        REFERENCES capture_ingestion_lineages(lineage_digest)
                )
                '''
            )
            connection.executemany(
                '''
                INSERT OR IGNORE INTO capture_ingestion_lineages(
                    lineage_digest
                ) VALUES (?)
                ''',
                [(row[1],) for row in rows],
            )
            connection.executemany(
                '''
                INSERT INTO capture_ingestion_runs(
                    ingestion_run_id, lineage_digest, plan_sha256,
                    bundle_digest, capture_revision_id, capture_series_id,
                    parent_revision_id, capture_session_ids_json,
                    coordinate_space_ids_json, ingestor_name,
                    ingestor_version, configuration_digest, plan_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                rows,
            )
            self._rebind_link_table(
                connection,
                'capture_ingestion_source_links',
                column_defs='''
                    ingestion_run_id TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    PRIMARY KEY(ingestion_run_id, source_evidence_id),
                    FOREIGN KEY(ingestion_run_id)
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    FOREIGN KEY(source_evidence_id)
                        REFERENCES capture_source_evidence(source_evidence_id)
                ''',
                insert_columns=(
                    'ingestion_run_id, source_evidence_id'
                ),
                select_columns='source_evidence_id',
            )
            self._rebind_link_table(
                connection,
                'capture_roomplan_records',
                column_defs='''
                    ingestion_run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(
                        ingestion_run_id, kind, source_evidence_id
                    ),
                    FOREIGN KEY(ingestion_run_id)
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    FOREIGN KEY(source_evidence_id)
                        REFERENCES capture_source_evidence(source_evidence_id)
                ''',
                insert_columns=(
                    'ingestion_run_id, kind, source_evidence_id, '
                    'payload_json'
                ),
                select_columns='kind, source_evidence_id, payload_json',
            )
            self._rebind_link_table(
                connection,
                'capture_ingestion_mesh_links',
                column_defs='''
                    ingestion_run_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    PRIMARY KEY(ingestion_run_id, binding_id),
                    FOREIGN KEY(ingestion_run_id)
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    FOREIGN KEY(binding_id)
                        REFERENCES capture_raw_visual_mesh_bindings(
                            binding_id
                        )
                ''',
                insert_columns='ingestion_run_id, binding_id',
                select_columns='binding_id',
            )
            self._rebind_link_table(
                connection,
                'capture_ingestion_authority_links',
                column_defs='''
                    ingestion_run_id TEXT NOT NULL,
                    authority_record_handoff_id TEXT NOT NULL,
                    PRIMARY KEY(
                        ingestion_run_id, authority_record_handoff_id
                    ),
                    FOREIGN KEY(ingestion_run_id)
                        REFERENCES capture_ingestion_runs(ingestion_run_id),
                    FOREIGN KEY(authority_record_handoff_id)
                        REFERENCES capture_authority_records(
                            authority_record_handoff_id
                        )
                ''',
                insert_columns=(
                    'ingestion_run_id, authority_record_handoff_id'
                ),
                select_columns='authority_record_handoff_id',
            )
            connection.execute(
                'DROP TABLE capture_ingestion_runs_legacy'
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _rebind_link_table(
        connection: sqlite3.Connection,
        table: str,
        *,
        column_defs: str,
        insert_columns: str,
        select_columns: str,
    ) -> None:
        """Rebuild one legacy lineage-keyed link table keyed by run id.

        The legacy primary key guaranteed exactly one run per lineage
        digest, so joining on it maps every persisted link to its unique
        run. A link whose lineage has no run row would violate the new
        foreign key and abort the migration loudly instead of silently
        dropping provenance. Tables already written in the run-scoped
        shape — including the empty ones the DDL just created — have no
        ``lineage_digest`` column and nothing to rebind.
        """

        columns = {
            str(row['name'])
            for row in connection.execute(f'PRAGMA table_info({table})')
        }
        if 'lineage_digest' not in columns:
            return
        connection.execute(f'ALTER TABLE {table} RENAME TO {table}_legacy')
        connection.execute(f'CREATE TABLE {table} ({column_defs})')
        connection.execute(
            f'INSERT INTO {table}({insert_columns}) '
            f'SELECT run.ingestion_run_id, {select_columns} '
            f'FROM {table}_legacy AS legacy '
            'JOIN capture_ingestion_runs AS run '
            '  ON run.lineage_digest = legacy.lineage_digest'
        )
        connection.execute(f'DROP TABLE {table}_legacy')

    @staticmethod
    def _migrate_source_import_origin(connection: sqlite3.Connection) -> None:
        """Record the verified import origin on persisted evidence (#412).

        Every row written by this repository arrived through the unsigned
        local-bundle import path, so the backfill is exact rather than a
        guess. The column is deliberately separated from ``provenance_class``:
        the latter is the bundle's self-asserted label, this one records how
        HTDT actually obtained the bytes.
        """

        columns = {
            str(row['name'])
            for row in connection.execute(
                'PRAGMA table_info(capture_source_evidence)'
            )
        }
        if 'import_origin' not in columns:
            connection.execute(
                '''
                ALTER TABLE capture_source_evidence
                ADD COLUMN import_origin TEXT NOT NULL
                    DEFAULT 'unsigned_capture_bundle_import'
                '''
            )

    def _register_persisted_coordinate_authorities(
        self, connection: sqlite3.Connection
    ) -> None:
        """Backfill scoped coordinate authorities from persisted runs (#365).

        Every persisted run's plan declares the immutable bundle and the
        coordinate spaces it claims; registering (bundle_digest,
        coordinate_space_id) per declared space reproduces the authority
        rows the current ingest path would have written. Rows already
        registered are re-checked for equality rather than rewritten.
        """

        rows = connection.execute(
            '''
            SELECT ingestion_run_id, plan_json, recorded_at_utc
            FROM capture_ingestion_runs
            '''
        ).fetchall()
        for row in rows:
            try:
                typed = CaptureIngestionPlan.model_validate_json(
                    row['plan_json']
                )
            except (ValueError, TypeError) as exc:
                raise CaptureIngestionTransactionError(
                    'cannot register coordinate authority scope: persisted '
                    f"plan for run {row['ingestion_run_id']} does not "
                    f'validate ({exc})'
                ) from exc
            for space_id in typed.bundle.coordinate_space_ids:
                self._register_coordinate_authority(
                    connection,
                    bundle_digest=typed.bundle.bundle_digest,
                    capture_revision_id=typed.bundle.capture_revision_id,
                    coordinate_space_id=space_id,
                    registered_by_run_id=str(row['ingestion_run_id']),
                    recorded_at_utc=str(row['recorded_at_utc']),
                )

    @staticmethod
    def _register_coordinate_authority(
        connection: sqlite3.Connection,
        *,
        bundle_digest: str,
        capture_revision_id: str,
        coordinate_space_id: str,
        registered_by_run_id: str,
        recorded_at_utc: str,
    ) -> None:
        authority_id = _coordinate_authority_id(
            bundle_digest, coordinate_space_id
        )
        existing = connection.execute(
            '''
            SELECT bundle_digest, capture_revision_id, coordinate_space_id
            FROM capture_coordinate_authorities
            WHERE coordinate_authority_id=?
            ''',
            (authority_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing['bundle_digest']) != bundle_digest
                or str(existing['coordinate_space_id'])
                != coordinate_space_id
            ):
                raise CaptureIngestionTransactionError(
                    'registered coordinate authority scope conflicts: '
                    f'{authority_id}'
                )
            return
        connection.execute(
            '''
            INSERT INTO capture_coordinate_authorities(
                coordinate_authority_id, bundle_digest, capture_revision_id,
                coordinate_space_id, registered_by_run_id, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                authority_id,
                bundle_digest,
                capture_revision_id,
                coordinate_space_id,
                registered_by_run_id,
                recorded_at_utc,
            ),
        )

    @staticmethod
    def _externalize_inline_source_payloads(
        connection: sqlite3.Connection,
    ) -> None:
        """Migrate legacy inline evidence blobs into the canonical blob store.

        Databases written before the content-addressed store existed keep raw
        payload bytes in capture_source_evidence.payload_blob. This one-time,
        idempotent migration copies each distinct payload into
        htdt_content_blobs keyed by its SHA-256 and leaves an empty inline
        blob behind; readers resolve externalized payloads by digest. Lossless
        by construction: the evidence row keeps payload_sha256/byte_count and
        every read re-verifies both.
        """

        rows = connection.execute(
            '''
            SELECT source_evidence_id, payload_sha256, byte_count, payload_blob
            FROM capture_source_evidence
            WHERE length(payload_blob) > 0
            '''
        ).fetchall()
        for row in rows:
            payload = bytes(row['payload_blob'])
            # Only a payload that hashes back to its recorded digest may be
            # externalized — the same check readers apply to inline bytes. A
            # bulk copy under the unverified key could let one corrupt row
            # poison a digest shared with healthy rows, and the blanket wipe
            # would then erase the losing row's only readable bytes. Rows
            # that fail verification keep their inline payload untouched.
            if sha256(payload).hexdigest() != row['payload_sha256']:
                continue
            store_content_blob(
                connection, payload, expected_sha256=row['payload_sha256']
            )
            connection.execute(
                '''
                UPDATE capture_source_evidence
                SET payload_blob = X''
                WHERE source_evidence_id=?
                ''',
                (row['source_evidence_id'],),
            )

    @staticmethod
    def _compact_legacy_mesh_bindings(
        connection: sqlite3.Connection,
    ) -> None:
        """Rewrite pre-dedup binding rows into compact handoff records.

        Legacy payload_json embeds the complete RawVisualMesh — a second copy
        of the canonical geometry bytes as Base64 plus expanded vertex/face
        arrays. A row is rewritten only when the rewrite is provably lossless:

        - the embedded Base64 bytes hash back to handoff.geometry_sha256,
        - the canonical blob for that digest exists after migration,
        - the recorded provenance matches what the current deterministic
          HTDTMSH1 importer reproduces (source name, format, importer
          version), so re-adapting the canonical bytes yields the identical
          mesh, binding id, and semantic hash.

        Anything else is preserved verbatim: readers still accept the legacy
        embedded representation, so no evidence is ever lost in migration.
        """

        rows = connection.execute(
            '''
            SELECT binding_id, payload_json
            FROM capture_raw_visual_mesh_bindings
            '''
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row['payload_json'])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or 'raw_mesh' not in data:
                continue
            mesh = data.get('raw_mesh')
            handoff = data.get('handoff')
            if not isinstance(mesh, dict) or not isinstance(handoff, dict):
                continue
            provenance = mesh.get('provenance')
            if not isinstance(provenance, dict):
                continue
            encoded = mesh.get('original_asset_base64')
            geometry_sha256 = handoff.get('geometry_sha256')
            if (
                not isinstance(encoded, str)
                or not isinstance(geometry_sha256, str)
                or provenance.get('asset_format') != 'htdt_meshbin_v1'
                or provenance.get('importer_version') != '2'
                or provenance.get('original_asset_sha256') != geometry_sha256
                or provenance.get('source_name') != handoff.get('geometry_path')
            ):
                continue
            try:
                embedded = b64decode(encoded.encode('ascii'), validate=True)
            except (ValueError, UnicodeEncodeError):
                continue
            if sha256(embedded).hexdigest() != geometry_sha256:
                continue
            canonical = connection.execute(
                '''
                SELECT length(payload_blob)
                FROM htdt_content_blobs
                WHERE payload_sha256=?
                ''',
                (geometry_sha256,),
            ).fetchone()
            if canonical is None or int(canonical[0]) != len(embedded):
                continue
            compact = _canonical_json(
                {
                    'schema': CAPTURE_MESH_BINDING_RECORD_SCHEMA,
                    'schema_version': CAPTURE_MESH_BINDING_RECORD_VERSION,
                    'binding_id': data.get('binding_id') or row['binding_id'],
                    'handoff': handoff,
                }
            )
            connection.execute(
                '''
                UPDATE capture_raw_visual_mesh_bindings
                SET payload_json=?
                WHERE binding_id=?
                ''',
                (compact, row['binding_id']),
            )

    @staticmethod
    def _normalize_mesh_binding_sources(
        connection: sqlite3.Connection,
    ) -> None:
        """Backfill normalized source-authority edges on mesh bindings.

        Rows written before the normalized columns existed carry the exact
        anchor-index / geometry source evidence ids only inside payload_json.
        The columns are appended lazily (matching the fresh CREATE TABLE
        shape) and reparsed from each persisted payload. Columns stay
        NULLable at the schema level — ``ALTER TABLE ADD COLUMN`` cannot add
        a NOT NULL column without a default — so reads fail closed on NULL
        or on any divergence between the columns and the typed payload. A
        row whose payload cannot produce the canonical pair, or whose
        recorded pair contradicts the payload, fails migration rather than
        guessing a provenance edge.
        """

        columns = {
            row['name']
            for row in connection.execute(
                'PRAGMA table_info(capture_raw_visual_mesh_bindings)'
            )
        }
        if 'anchor_index_source_evidence_id' not in columns:
            connection.execute(
                '''
                ALTER TABLE capture_raw_visual_mesh_bindings
                ADD COLUMN anchor_index_source_evidence_id TEXT
                    REFERENCES capture_source_evidence(source_evidence_id)
                    ON DELETE RESTRICT
                '''
            )
        if 'geometry_source_evidence_id' not in columns:
            connection.execute(
                '''
                ALTER TABLE capture_raw_visual_mesh_bindings
                ADD COLUMN geometry_source_evidence_id TEXT
                    REFERENCES capture_source_evidence(source_evidence_id)
                    ON DELETE RESTRICT
                '''
            )
        rows = connection.execute(
            '''
            SELECT binding_id, payload_json,
                   anchor_index_source_evidence_id,
                   geometry_source_evidence_id
            FROM capture_raw_visual_mesh_bindings
            WHERE anchor_index_source_evidence_id IS NULL
               OR geometry_source_evidence_id IS NULL
            '''
        ).fetchall()
        for row in rows:
            anchor_id, geometry_id = _binding_handoff_source_ids(
                row['payload_json'],
                binding_id=row['binding_id'],
            )
            if (
                (
                    row['anchor_index_source_evidence_id'] is not None
                    and row['anchor_index_source_evidence_id'] != anchor_id
                )
                or (
                    row['geometry_source_evidence_id'] is not None
                    and row['geometry_source_evidence_id'] != geometry_id
                )
            ):
                raise CaptureIngestionTransactionError(
                    f'persisted mesh binding {row["binding_id"]} normalized '
                    'source authorities disagree with its payload; refusing '
                    'to rewrite provenance'
                )
            try:
                connection.execute(
                    '''
                    UPDATE capture_raw_visual_mesh_bindings
                    SET anchor_index_source_evidence_id=?,
                        geometry_source_evidence_id=?
                    WHERE binding_id=?
                    ''',
                    (anchor_id, geometry_id, row['binding_id']),
                )
            except sqlite3.IntegrityError as exc:
                raise CaptureIngestionTransactionError(
                    f'persisted mesh binding {row["binding_id"]} references '
                    'source evidence that is not persisted; refusing to '
                    'normalize provenance columns'
                ) from exc
        try:
            mismatched = connection.execute(
                '''
                SELECT binding_id
                FROM capture_raw_visual_mesh_bindings
                WHERE anchor_index_source_evidence_id IS NULL
                   OR geometry_source_evidence_id IS NULL
                   OR anchor_index_source_evidence_id
                      != json_extract(
                          payload_json,
                          '$.handoff.anchor_index_source_evidence_id'
                      )
                   OR geometry_source_evidence_id
                      != json_extract(
                          payload_json,
                          '$.handoff.geometry_source_evidence_id'
                      )
                ORDER BY binding_id
                '''
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise CaptureIngestionTransactionError(
                'persisted mesh binding payloads could not be verified '
                f'against normalized provenance columns: {exc}'
            ) from exc
        if mismatched:
            ids = ', '.join(str(row['binding_id']) for row in mismatched)
            raise CaptureIngestionTransactionError(
                'persisted mesh binding normalized source authorities do '
                f'not match their payloads: {ids}'
            )

    def ingest(
        self,
        plan: CaptureIngestionPlan | Mapping[str, Any],
        payloads_by_path: Mapping[str, bytes],
        *,
        budget: CaptureIngestionBudget | None = None,
        manifest: bytes | None = None,
    ) -> CaptureIngestionCommitResult:
        """Atomically commit one validated ingestion plan.

        ``plan`` is an untrusted producer claim: before commit, every
        declared source payload is validated against the pinned Capture
        Bundle v1 schema/binary/metadata contract and every handoff
        semantic is rederived from the exact payload bytes
        (#337/#343/#345/#369). ``manifest`` — when supplied by the
        production import path — must be the canonical manifest.json
        bytes whose SHA-256 equals the plan's bundle digest; it is
        retained as immutable bundle evidence (#338). Capture revision
        identity/topology is registered fail-closed (#335) inside the
        same transaction.
        """
        typed = (
            plan
            if isinstance(plan, CaptureIngestionPlan)
            else CaptureIngestionPlan.model_validate(plan)
        )
        payloads = dict(payloads_by_path)
        expected_paths = {item.path for item in typed.source_evidence}
        if set(payloads) != expected_paths:
            missing = sorted(expected_paths - set(payloads))
            extra = sorted(set(payloads) - expected_paths)
            raise CaptureIngestionTransactionError(
                f'payload path set mismatch: missing={missing}, extra={extra}'
            )

        self._enforce_budget(
            typed,
            payloads,
            budget or DEFAULT_CAPTURE_INGESTION_BUDGET,
        )

        source_by_path = {item.path: item for item in typed.source_evidence}
        for path, payload in payloads.items():
            if not isinstance(payload, bytes):
                raise TypeError('capture source payloads must be immutable bytes')
            record = source_by_path[path]
            if len(payload) != record.bytes:
                raise CaptureIngestionTransactionError(
                    f'capture payload byte-count mismatch: {path}'
                )
            if sha256(payload).hexdigest() != record.payload_sha256:
                raise CaptureIngestionTransactionError(
                    f'capture payload SHA-256 mismatch: {path}'
                )

        quality_state = self._validate_source_payload_contract(
            typed, payloads
        )
        manifest_document = (
            self._validate_manifest_for_plan(typed, manifest, payloads)
            if manifest is not None
            else None
        )

        plan_json = _canonical_json(
            typed.model_dump(mode='json', by_alias=True)
        )
        plan_sha256 = sha256(plan_json.encode('utf-8')).hexdigest()
        run_id = _ingestion_run_id(
            typed.lineage_digest,
            typed.ingestor.name,
            typed.ingestor.version,
            typed.ingestor.configuration_digest,
            plan_sha256,
        )

        revision_conflict: tuple[str, str] | None = None
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    self._register_capture_revision(connection, typed)
                except CaptureRevisionConflictError as exc:
                    revision_conflict = (
                        typed.bundle.capture_revision_id, str(exc)
                    )
                    raise
                if manifest_document is not None:
                    self._persist_capture_bundle(
                        connection, typed, manifest, manifest_document
                    )
                existing = connection.execute(
                    '''
                    SELECT *
                    FROM capture_ingestion_runs
                    WHERE ingestion_run_id=?
                    ''',
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    # A surviving run row is not proof that the persisted
                    # materialization is still complete: links, payloads, and
                    # derived records can be lost to faulty migrations,
                    # restore bugs, or foreign-key-disabled writes. Re-verify
                    # the full materialization — including persisted plan
                    # identity — and fail closed on damage rather than
                    # claiming idempotent success over a partial or corrupt
                    # earlier ingest.
                    verified = self._verify_persisted_materialization(
                        connection,
                        typed,
                        existing,
                    )
                    connection.rollback()
                    return verified

                recorded_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    '''
                    INSERT OR IGNORE INTO capture_ingestion_lineages(
                        lineage_digest
                    ) VALUES (?)
                    ''',
                    (typed.lineage_digest,),
                )
                connection.execute(
                    '''
                    INSERT INTO capture_ingestion_runs(
                        ingestion_run_id, lineage_digest, plan_sha256,
                        bundle_digest, capture_revision_id,
                        capture_series_id, parent_revision_id,
                        capture_session_ids_json,
                        coordinate_space_ids_json, ingestor_name,
                        ingestor_version, configuration_digest,
                        plan_json, recorded_at_utc,
                        quality_state, quality_payload_sha256,
                        quality_ruleset_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        run_id,
                        typed.lineage_digest,
                        plan_sha256,
                        typed.bundle.bundle_digest,
                        typed.bundle.capture_revision_id,
                        typed.bundle.capture_series_id,
                        typed.bundle.parent_revision_id,
                        _canonical_json(
                            list(typed.bundle.capture_session_ids)
                        ),
                        _canonical_json(
                            list(typed.bundle.coordinate_space_ids)
                        ),
                        typed.ingestor.name,
                        typed.ingestor.version,
                        typed.ingestor.configuration_digest,
                        plan_json,
                        recorded_at,
                        quality_state.state,
                        quality_state.payload_sha256,
                        quality_state.ruleset_version,
                    ),
                )

                # Register the scoped coordinate authorities this bundle
                # declares before any derived record references them (#365).
                for space_id in typed.bundle.coordinate_space_ids:
                    self._register_coordinate_authority(
                        connection,
                        bundle_digest=typed.bundle.bundle_digest,
                        capture_revision_id=typed.bundle.capture_revision_id,
                        coordinate_space_id=space_id,
                        registered_by_run_id=run_id,
                        recorded_at_utc=recorded_at,
                    )

                for record in typed.source_evidence:
                    payload = payloads[record.path]
                    self._upsert_source_evidence(
                        connection,
                        record,
                        payload,
                    )
                    connection.execute(
                        '''
                        INSERT INTO capture_ingestion_source_links(
                            ingestion_run_id, source_evidence_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            run_id,
                            record.source_evidence_id,
                        ),
                    )

                for record in typed.roomplan_records:
                    connection.execute(
                        '''
                        INSERT INTO capture_roomplan_records(
                            ingestion_run_id, kind,
                            source_evidence_id, payload_json
                        ) VALUES (?, ?, ?, ?)
                        ''',
                        (
                            run_id,
                            record.kind,
                            record.source_evidence_id,
                            _canonical_json(
                                record.model_dump(mode='json')
                            ),
                        ),
                    )

                # Adapt and persist one binding at a time inside the
                # transaction so peak memory tracks a single decoded mesh
                # rather than the whole staged set; the canonical geometry
                # bytes were already verified against the plan above.
                for handoff in typed.raw_visual_mesh_handoffs:
                    binding = adapt_capture_mesh_handoff(
                        handoff,
                        payloads[handoff.geometry_path],
                    )
                    self._upsert_mesh_binding(connection, binding)
                    connection.execute(
                        '''
                        INSERT INTO capture_ingestion_mesh_links(
                            ingestion_run_id, binding_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            run_id,
                            binding.binding_id,
                        ),
                    )

                for record in typed.authority_records:
                    self._upsert_authority_record(connection, record)
                    connection.execute(
                        '''
                        INSERT INTO capture_ingestion_authority_links(
                            ingestion_run_id,
                            authority_record_handoff_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            run_id,
                            record.authority_record_handoff_id,
                        ),
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                if revision_conflict is not None:
                    self._persist_revision_conflict_outcome(
                        *revision_conflict
                    )
                raise

        return self._result(
            typed, run_id, created=True, quality_state=quality_state
        )

    def _persist_revision_conflict_outcome(
        self,
        revision_id: str,
        detail: str,
    ) -> None:
        """Record a revision conflict in its own committed transaction.

        The rejection rolled the ingest transaction back; the audit trail
        must still survive (#335: pre-existing conflicts are surfaced and
        recorded, never guessed at).
        """
        with closing(self._connect()) as connection:
            self._record_revision_conflict(connection, revision_id, detail)
            connection.commit()

    def verify_persisted_ingestion(
        self,
        plan: CaptureIngestionPlan | Mapping[str, Any],
    ) -> CaptureIngestionCommitResult:
        """Verify one run's complete persisted materialization.

        Re-checks every row the original ingest wrote for ``plan`` against
        the supplied plan authority: persisted run metadata and plan
        identity, the exact source-evidence link set, each evidence row's
        canonical metadata and payload SHA-256/length, the exact RoomPlan
        record set, and the mesh-binding and authority-record link sets
        including each record's deterministic identity and source authority.
        No unexpected linked record may exist for the run.

        The same routine backs re-import idempotency and is reusable by
        backup/restore validation and the Capture library. It raises
        PersistedIngestionIntegrityError rather than reporting success over
        partial or corrupt persisted state; the returned counts come from
        the verified persisted sets.
        """

        typed = (
            plan
            if isinstance(plan, CaptureIngestionPlan)
            else CaptureIngestionPlan.model_validate(plan)
        )
        plan_json = _canonical_json(
            typed.model_dump(mode='json', by_alias=True)
        )
        run_id = _ingestion_run_id(
            typed.lineage_digest,
            typed.ingestor.name,
            typed.ingestor.version,
            typed.ingestor.configuration_digest,
            sha256(plan_json.encode('utf-8')).hexdigest(),
        )
        with closing(self._connect()) as connection:
            run = connection.execute(
                '''
                SELECT *
                FROM capture_ingestion_runs
                WHERE ingestion_run_id=?
                ''',
                (run_id,),
            ).fetchone()
            if run is None:
                raise PersistedIngestionIntegrityError(
                    'ingestion run is not persisted: '
                    f'{run_id}'
                )
            return self._verify_persisted_materialization(
                connection,
                typed,
                run,
            )

    def get_ingestion_run(
        self, ingestion_run_id: str
    ) -> CaptureIngestionRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_ingestion_runs
                WHERE ingestion_run_id=?
                ''',
                (ingestion_run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    def get_ingestion_run_plan(
        self, ingestion_run_id: str
    ) -> CaptureIngestionPlan | None:
        """Load the exact canonical plan one persisted run consumed."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_ingestion_runs
                WHERE ingestion_run_id=?
                ''',
                (ingestion_run_id,),
            ).fetchone()
        if row is None:
            return None
        plan = CaptureIngestionPlan.model_validate_json(row['plan_json'])
        self._verify_run_row_plan(row, plan)
        return plan

    def get_ingestion(
        self, lineage_digest: str
    ) -> CaptureIngestionPlan | None:
        """Load the canonical plan of the latest processing run for a lineage.

        Several runs may share one ``lineage_digest`` (#413); lineage-keyed
        callers (Inbox inspection, connected-space staging) act on the most
        recently recorded run.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_ingestion_runs
                WHERE lineage_digest=?
                ORDER BY recorded_at_utc DESC, ingestion_run_id DESC
                LIMIT 1
                ''',
                (lineage_digest,),
            ).fetchone()
        if row is None:
            return None
        plan = CaptureIngestionPlan.model_validate_json(row['plan_json'])
        self._verify_run_row_plan(row, plan)
        return plan

    def get_source_evidence(
        self,
        source_evidence_id: str,
    ) -> PersistedCaptureSourceEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_source_evidence
                WHERE source_evidence_id=?
                ''',
                (source_evidence_id,),
            ).fetchone()
            if row is None:
                return None
            record = CaptureSourceEvidence(
                source_evidence_id=row['source_evidence_id'],
                bundle_digest=row['bundle_digest'],
                capture_revision_id=row['capture_revision_id'],
                path=row['logical_path'],
                payload_sha256=row['payload_sha256'],
                bytes=row['byte_count'],
                media_type=row['media_type'],
                producer=row['producer'],
                provenance_class=row['provenance_class'],
                role=row['role'],
                source_refs=tuple(json.loads(row['source_refs_json'])),
            )
            payload = self._evidence_payload(connection, row)
        if len(payload) != record.bytes or sha256(payload).hexdigest() != record.payload_sha256:
            raise CaptureIngestionTransactionError(
                f'persisted source evidence integrity mismatch: {source_evidence_id}'
            )
        return PersistedCaptureSourceEvidence(record=record, payload=payload)

    def get_mesh_binding(
        self,
        binding_id: str,
    ) -> CaptureRawVisualMeshBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT payload_json,
                       handoff_id,
                       anchor_index_source_evidence_id,
                       geometry_source_evidence_id
                FROM capture_raw_visual_mesh_bindings
                WHERE binding_id=?
                ''',
                (binding_id,),
            ).fetchone()
            if row is None:
                return None
            return self._binding_from_payload_json(
                connection,
                row['payload_json'],
                binding_id=binding_id,
                expected_handoff_id=row['handoff_id'],
                normalized_source_ids=(
                    row['anchor_index_source_evidence_id'],
                    row['geometry_source_evidence_id'],
                ),
            )

    def mesh_binding_ids_for_run(
        self,
        ingestion_run_id: str,
    ) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''
                SELECT binding_id
                FROM capture_ingestion_mesh_links
                WHERE ingestion_run_id=?
                ORDER BY binding_id ASC
                ''',
                (ingestion_run_id,),
            ).fetchall()
        return tuple(str(row['binding_id']) for row in rows)

    def mesh_binding_ids_for_lineage(
        self,
        lineage_digest: str,
    ) -> tuple[str, ...]:
        """All bindings linked by any run materializing this lineage."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''
                SELECT DISTINCT l.binding_id
                FROM capture_ingestion_mesh_links AS l
                JOIN capture_ingestion_runs AS r
                  ON r.ingestion_run_id = l.ingestion_run_id
                WHERE r.lineage_digest=?
                ORDER BY l.binding_id ASC
                ''',
                (lineage_digest,),
            ).fetchall()
        return tuple(str(row['binding_id']) for row in rows)

    def roomplan_records_for_ingestion(
        self,
        lineage_digest: str,
    ) -> tuple[CaptureRoomPlanRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT rr.payload_json
                FROM capture_roomplan_records rr
                JOIN capture_ingestion_runs r
                  ON r.ingestion_run_id = rr.ingestion_run_id
                WHERE r.lineage_digest=?
                ORDER BY rr.kind ASC, rr.source_evidence_id ASC
                """,
                (lineage_digest,),
            ).fetchall()
        return tuple(
            CaptureRoomPlanRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    def authority_records_for_ingestion(
        self,
        lineage_digest: str,
    ) -> tuple[CaptureAuthorityRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT rec.payload_json
                FROM capture_ingestion_authority_links l
                JOIN capture_authority_records rec
                  ON rec.authority_record_handoff_id
                     = l.authority_record_handoff_id
                JOIN capture_ingestion_runs r
                  ON r.ingestion_run_id = l.ingestion_run_id
                WHERE r.lineage_digest=?
                ORDER BY l.authority_record_handoff_id ASC
                """,
                (lineage_digest,),
            ).fetchall()
        return tuple(
            CaptureAuthorityRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    def get_authority_record(
        self,
        authority_record_handoff_id: str,
    ) -> CaptureAuthorityRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT authority_record_handoff_id, source_evidence_id,
                       payload_json
                FROM capture_authority_records
                WHERE authority_record_handoff_id=?
                ''',
                (authority_record_handoff_id,),
            ).fetchone()
        if row is None:
            return None
        record = CaptureAuthorityRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.authority_record_handoff_id
            != row['authority_record_handoff_id']
            or record.source_evidence_id != row['source_evidence_id']
        ):
            raise CaptureIngestionTransactionError(
                'persisted authority record row disagrees with its payload: '
                f'{authority_record_handoff_id}'
            )
        return record

    def source_evidence_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute(
                    'SELECT COUNT(*) FROM capture_source_evidence'
                ).fetchone()[0]
            )

    @staticmethod
    def _verify_run_row_plan(
        row: sqlite3.Row, plan: CaptureIngestionPlan
    ) -> None:
        """Row/payload invariant (#313): duplicated run columns must equal
        the canonical plan's lineage, bundle, revision and ingestor fields."""

        if sha256(str(row['plan_json']).encode('utf-8')).hexdigest() != str(
            row['plan_sha256']
        ):
            raise CaptureIngestionTransactionError(
                'persisted ingestion run row disagrees with its plan: '
                f'{row["ingestion_run_id"]}'
            )
        parent_revision_id = row['parent_revision_id']
        if (
            str(row['lineage_digest']) != plan.lineage_digest
            or str(row['bundle_digest']) != plan.bundle.bundle_digest
            or str(row['capture_revision_id'])
            != plan.bundle.capture_revision_id
            or str(row['capture_series_id'])
            != plan.bundle.capture_series_id
            or (
                None if parent_revision_id is None else str(parent_revision_id)
            ) != plan.bundle.parent_revision_id
            or str(row['ingestor_name']) != plan.ingestor.name
            or str(row['ingestor_version']) != plan.ingestor.version
            or str(row['configuration_digest'])
            != plan.ingestor.configuration_digest
        ):
            raise CaptureIngestionTransactionError(
                'persisted ingestion run row disagrees with its plan: '
                f'{row["ingestion_run_id"]}'
            )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> CaptureIngestionRun:
        plan = CaptureIngestionPlan.model_validate_json(row['plan_json'])
        CaptureIngestionRepository._verify_run_row_plan(row, plan)
        return CaptureIngestionRun(
            ingestion_run_id=str(row['ingestion_run_id']),
            lineage_digest=str(row['lineage_digest']),
            plan_sha256=str(row['plan_sha256']),
            bundle_digest=str(row['bundle_digest']),
            capture_revision_id=str(row['capture_revision_id']),
            capture_series_id=str(row['capture_series_id']),
            parent_revision_id=(
                None
                if row['parent_revision_id'] is None
                else str(row['parent_revision_id'])
            ),
            capture_session_ids=tuple(
                str(value)
                for value in json.loads(row['capture_session_ids_json'])
            ),
            coordinate_space_ids=tuple(
                str(value)
                for value in json.loads(
                    row['coordinate_space_ids_json']
                )
            ),
            ingestor_name=str(row['ingestor_name']),
            ingestor_version=str(row['ingestor_version']),
            configuration_digest=str(row['configuration_digest']),
            recorded_at_utc=str(row['recorded_at_utc']),
        )

    # ---- scoped coordinate authorities (#365) ---------------------------

    def get_coordinate_authority(
        self, coordinate_authority_id: str
    ) -> CaptureCoordinateAuthority | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_coordinate_authorities
                WHERE coordinate_authority_id=?
                ''',
                (coordinate_authority_id,),
            ).fetchone()
        if row is None:
            return None
        return self._authority_from_row(row)

    def coordinate_authority_for(
        self, bundle_digest: str, coordinate_space_id: str
    ) -> CaptureCoordinateAuthority | None:
        """The registered authority for one bundle-scoped frame, or None."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT *
                FROM capture_coordinate_authorities
                WHERE bundle_digest=? AND coordinate_space_id=?
                ''',
                (bundle_digest, coordinate_space_id),
            ).fetchone()
        if row is None:
            return None
        return self._authority_from_row(row)

    def list_coordinate_authorities(
        self, *, bundle_digest: str | None = None
    ) -> tuple[CaptureCoordinateAuthority, ...]:
        query = 'SELECT * FROM capture_coordinate_authorities'
        params: tuple[str, ...] = ()
        if bundle_digest is not None:
            query += ' WHERE bundle_digest=?'
            params = (bundle_digest,)
        query += ' ORDER BY coordinate_authority_id ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._authority_from_row(row) for row in rows)

    @staticmethod
    def _authority_from_row(
        row: sqlite3.Row,
    ) -> CaptureCoordinateAuthority:
        return CaptureCoordinateAuthority(
            coordinate_authority_id=str(row['coordinate_authority_id']),
            bundle_digest=str(row['bundle_digest']),
            capture_revision_id=str(row['capture_revision_id']),
            coordinate_space_id=str(row['coordinate_space_id']),
            registered_by_run_id=str(row['registered_by_run_id']),
            recorded_at_utc=str(row['recorded_at_utc']),
        )

    # ---- Capture library query surface (#353) ---------------------------

    def list_ingestion_runs(
        self,
        *,
        lineage_digest: str | None = None,
        bundle_digest: str | None = None,
        capture_revision_id: str | None = None,
        limit: int = 100,
        after_run_id: str | None = None,
    ) -> tuple[CaptureIngestionRun, ...]:
        """Deterministically ordered, bounded listing of persisted runs.

        Ordering is by the immutable run identity, so results are stable
        regardless of import order; ``after_run_id`` is a keyset cursor for
        pagination. No source payload is loaded.
        """

        clauses: list[str] = []
        params: list[str] = []
        if lineage_digest is not None:
            clauses.append('lineage_digest=?')
            params.append(lineage_digest)
        if bundle_digest is not None:
            clauses.append('bundle_digest=?')
            params.append(bundle_digest)
        if capture_revision_id is not None:
            clauses.append('capture_revision_id=?')
            params.append(capture_revision_id)
        if after_run_id is not None:
            clauses.append('ingestion_run_id>?')
            params.append(after_run_id)
        query = 'SELECT * FROM capture_ingestion_runs'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY ingestion_run_id ASC LIMIT ?'
        with closing(self._connect()) as connection:
            rows = connection.execute(
                query, (*params, int(limit))
            ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def list_capture_revisions(
        self,
        *,
        capture_series_id: str | None = None,
        limit: int = 100,
        after_revision_id: str | None = None,
    ) -> tuple[CaptureRevisionSummary, ...]:
        """Imported Capture revisions discoverable after restart.

        Revisions are enumerated from the persisted run set, ordered by the
        immutable revision id with a keyset cursor, so listing is bounded
        and deterministic. Only normalized run/link tables are read — no
        source payload BLOBs are loaded.
        """

        params: list[str] = []
        query = '''
            SELECT DISTINCT capture_revision_id
            FROM capture_ingestion_runs
        '''
        clauses: list[str] = []
        if capture_series_id is not None:
            clauses.append('capture_series_id=?')
            params.append(capture_series_id)
        if after_revision_id is not None:
            clauses.append('capture_revision_id>?')
            params.append(after_revision_id)
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY capture_revision_id ASC LIMIT ?'
        with closing(self._connect()) as connection:
            ids = [
                str(row['capture_revision_id'])
                for row in connection.execute(
                    query, (*params, int(limit))
                ).fetchall()
            ]
            return tuple(
                self._revision_summary(connection, revision_id)
                for revision_id in ids
            )

    def get_capture_revision(
        self, capture_revision_id: str
    ) -> 'CaptureRevisionSummary | None':
        with closing(self._connect()) as connection:
            if not self._revision_exists(connection, capture_revision_id):
                return None
            return self._revision_summary(connection, capture_revision_id)

    def list_series_revisions(
        self, capture_series_id: str
    ) -> tuple['CaptureRevisionSummary', ...]:
        """Parent/child topology for one capture series.

        Every revision whose bundle declares this series is returned in
        order, each carrying ``parent_revision_id`` and whether that parent
        is itself recorded — an unresolved parent stays explicit rather
        than silently marking the revision a root.
        """

        return self.list_capture_revisions(
            capture_series_id=capture_series_id, limit=10000
        )

    def _revision_exists(
        self, connection: sqlite3.Connection, capture_revision_id: str
    ) -> bool:
        return (
            connection.execute(
                '''
                SELECT 1
                FROM capture_ingestion_runs
                WHERE capture_revision_id=?
                LIMIT 1
                ''',
                (capture_revision_id,),
            ).fetchone()
            is not None
        )

    def _revision_summary(
        self, connection: sqlite3.Connection, capture_revision_id: str
    ) -> 'CaptureRevisionSummary':
        runs = [
            self._run_from_row(row)
            for row in connection.execute(
                '''
                SELECT *
                FROM capture_ingestion_runs
                WHERE capture_revision_id=?
                ORDER BY ingestion_run_id ASC
                ''',
                (capture_revision_id,),
            ).fetchall()
        ]
        first = runs[0]
        run_ids = tuple(run.ingestion_run_id for run in runs)
        placeholders = ','.join('?' for _ in run_ids)

        def count(query: str, *args: str) -> int:
            return int(
                connection.execute(query, args).fetchone()[0]
            )

        source_count = count(
            f'''
            SELECT COUNT(*) FROM capture_ingestion_source_links
            WHERE ingestion_run_id IN ({placeholders})
            ''',
            *run_ids,
        )
        mesh_links = connection.execute(
            f'''
            SELECT binding_id FROM capture_ingestion_mesh_links
            WHERE ingestion_run_id IN ({placeholders})
            ORDER BY binding_id ASC
            ''',
            run_ids,
        ).fetchall()
        binding_ids = tuple(str(row['binding_id']) for row in mesh_links)
        authority_count = count(
            f'''
            SELECT COUNT(*) FROM capture_ingestion_authority_links
            WHERE ingestion_run_id IN ({placeholders})
            ''',
            *run_ids,
        )
        roomplan_count = count(
            f'''
            SELECT COUNT(*) FROM capture_roomplan_records
            WHERE ingestion_run_id IN ({placeholders})
            ''',
            *run_ids,
        )
        promotion_table = {
            str(row['name'])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        promotion_columns = (
            {
                str(row['name'])
                for row in connection.execute(
                    'PRAGMA table_info(capture_semantic_promotions)'
                )
            }
            if 'capture_semantic_promotions' in promotion_table
            else set()
        )
        if 'ingestion_run_id' in promotion_columns:
            promoted = connection.execute(
                '''
                SELECT promotion_id, scene_revision_id
                FROM capture_semantic_promotions
                WHERE ingestion_run_id IN (
                    SELECT ingestion_run_id FROM capture_ingestion_runs
                    WHERE capture_revision_id=?
                )
                ORDER BY promotion_id ASC
                ''',
                (capture_revision_id,),
            ).fetchall()
        else:
            promoted = []
        promoted_scene_ids = tuple(
            str(row['scene_revision_id']) for row in promoted
        )
        parent_known = (
            first.parent_revision_id is None
            or self._revision_exists(connection, first.parent_revision_id)
        )
        return CaptureRevisionSummary(
            capture_revision_id=capture_revision_id,
            capture_series_id=first.capture_series_id,
            bundle_digest=first.bundle_digest,
            parent_revision_id=first.parent_revision_id,
            parent_known=parent_known,
            capture_session_ids=first.capture_session_ids,
            coordinate_space_ids=first.coordinate_space_ids,
            lineage_digests=tuple(
                dict.fromkeys(run.lineage_digest for run in runs)
            ),
            ingestion_run_ids=run_ids,
            ingestor_versions=tuple(
                dict.fromkeys(
                    f'{run.ingestor_name}@{run.ingestor_version}'
                    for run in runs
                )
            ),
            source_evidence_count=source_count,
            raw_mesh_binding_ids=binding_ids,
            authority_record_count=authority_count,
            roomplan_record_count=roomplan_count,
            promotion_ids=tuple(
                str(row['promotion_id']) for row in promoted
            ),
            promoted_scene_revision_ids=promoted_scene_ids,
            first_recorded_at_utc=min(
                run.recorded_at_utc for run in runs
            ),
            last_recorded_at_utc=max(
                run.recorded_at_utc for run in runs
            ),
        )

    # ---- provenance trust view (#412) -----------------------------------

    def provenance_trust(
        self, source_evidence_id: str
    ) -> 'CaptureProvenanceTrust | None':
        """Split asserted bundle labels from verified import origin.

        ``provenance_class`` (including ``backend_derived``) and
        ``producer`` are the bundle's self-asserted labels — an unsigned
        import cannot authenticate them. ``import_origin`` records how HTDT
        actually obtained the bytes, and ``producer_authenticated`` is
        always False for the unsigned bundle path.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT source_evidence_id, producer, provenance_class,
                       import_origin
                FROM capture_source_evidence
                WHERE source_evidence_id=?
                ''',
                (source_evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureProvenanceTrust(
            source_evidence_id=str(row['source_evidence_id']),
            asserted_provenance_class=str(row['provenance_class']),
            asserted_producer=str(row['producer']),
            verified_import_origin=str(row['import_origin']),
            producer_authenticated=False,
        )

    def provenance_trust_for_authority_record(
        self, authority_record_handoff_id: str
    ) -> 'CaptureProvenanceTrust | None':
        """Trust view of the source evidence an authority record cites."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT e.source_evidence_id, e.producer,
                       e.provenance_class, e.import_origin
                FROM capture_authority_records AS a
                JOIN capture_source_evidence AS e
                  ON e.source_evidence_id = a.source_evidence_id
                WHERE a.authority_record_handoff_id=?
                ''',
                (authority_record_handoff_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureProvenanceTrust(
            source_evidence_id=str(row['source_evidence_id']),
            asserted_provenance_class=str(row['provenance_class']),
            asserted_producer=str(row['producer']),
            verified_import_origin=str(row['import_origin']),
            producer_authenticated=False,
        )

    def _upsert_source_evidence(
        self,
        connection: sqlite3.Connection,
        record: CaptureSourceEvidence,
        payload: bytes,
    ) -> None:
        existing = connection.execute(
            '''
            SELECT *
            FROM capture_source_evidence
            WHERE source_evidence_id=?
            ''',
            (record.source_evidence_id,),
        ).fetchone()
        record_json = _canonical_json(record.model_dump(mode='json'))
        if existing is not None:
            existing_record = CaptureSourceEvidence(
                source_evidence_id=existing['source_evidence_id'],
                bundle_digest=existing['bundle_digest'],
                capture_revision_id=existing['capture_revision_id'],
                path=existing['logical_path'],
                payload_sha256=existing['payload_sha256'],
                bytes=existing['byte_count'],
                media_type=existing['media_type'],
                producer=existing['producer'],
                provenance_class=existing['provenance_class'],
                role=existing['role'],
                source_refs=tuple(json.loads(existing['source_refs_json'])),
            )
            if (
                _canonical_json(existing_record.model_dump(mode='json'))
                != record_json
                or self._evidence_payload(connection, existing) != payload
            ):
                raise CaptureIngestionTransactionError(
                    'source evidence identity already exists with different semantics'
                )
            return

        # Raw bytes live once in the content-addressed blob store keyed by
        # payload_sha256; the evidence row keeps an empty inline blob as the
        # marker that its payload is externalized.
        store_content_blob(
            connection,
            payload,
            expected_sha256=record.payload_sha256,
        )
        connection.execute(
            '''
            INSERT INTO capture_source_evidence(
                source_evidence_id, bundle_digest, capture_revision_id,
                logical_path, payload_sha256, byte_count, media_type,
                producer, provenance_class, role, source_refs_json,
                payload_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                record.source_evidence_id,
                record.bundle_digest,
                record.capture_revision_id,
                record.path,
                record.payload_sha256,
                record.bytes,
                record.media_type,
                record.producer,
                record.provenance_class,
                record.role,
                _canonical_json(list(record.source_refs)),
                b'',
            ),
        )

    def _upsert_mesh_binding(
        self,
        connection: sqlite3.Connection,
        binding: CaptureRawVisualMeshBinding,
    ) -> None:
        payload_json = serialize_mesh_binding_record(binding)
        existing = connection.execute(
            '''
            SELECT payload_json,
                   anchor_index_source_evidence_id,
                   geometry_source_evidence_id
            FROM capture_raw_visual_mesh_bindings
            WHERE binding_id=?
            ''',
            (binding.binding_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing['anchor_index_source_evidence_id']
                != binding.handoff.anchor_index_source_evidence_id
                or existing['geometry_source_evidence_id']
                != binding.handoff.geometry_source_evidence_id
            ):
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding normalized source authorities do '
                    'not match the ingestion plan'
                )
            if existing['payload_json'] != payload_json:
                # The persisted representation changed from an embedded mesh
                # dump to a compact handoff record. Compare semantic identity
                # instead of bytes so mixed-format rows still deduplicate:
                # binding_id already pins handoff_id and the mesh semantic
                # hash, so equal handoffs mean equal bindings.
                persisted = self._binding_from_payload_json(
                    connection,
                    existing['payload_json'],
                    binding_id=binding.binding_id,
                    normalized_source_ids=(
                        existing['anchor_index_source_evidence_id'],
                        existing['geometry_source_evidence_id'],
                    ),
                )
                if persisted.handoff != binding.handoff:
                    raise CaptureIngestionTransactionError(
                        'raw mesh binding identity already exists with different semantics'
                    )
            return
        connection.execute(
            '''
            INSERT INTO capture_raw_visual_mesh_bindings(
                binding_id, handoff_id,
                anchor_index_source_evidence_id,
                geometry_source_evidence_id,
                payload_json
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            (
                binding.binding_id,
                binding.handoff.raw_visual_mesh_handoff_id,
                binding.handoff.anchor_index_source_evidence_id,
                binding.handoff.geometry_source_evidence_id,
                payload_json,
            ),
        )

    def _upsert_authority_record(
        self,
        connection: sqlite3.Connection,
        record: CaptureAuthorityRecord,
    ) -> None:
        payload_json = _canonical_json(record.model_dump(mode='json'))
        existing = connection.execute(
            '''
            SELECT payload_json
            FROM capture_authority_records
            WHERE authority_record_handoff_id=?
            ''',
            (record.authority_record_handoff_id,),
        ).fetchone()
        if existing is not None:
            if existing['payload_json'] != payload_json:
                raise CaptureIngestionTransactionError(
                    'authority record identity already exists with different semantics'
                )
            return
        connection.execute(
            '''
            INSERT INTO capture_authority_records(
                authority_record_handoff_id,
                source_evidence_id,
                payload_json
            ) VALUES (?, ?, ?)
            ''',
            (
                record.authority_record_handoff_id,
                record.source_evidence_id,
                payload_json,
            ),
        )

    @staticmethod
    def _enforce_budget(
        plan: CaptureIngestionPlan,
        payloads: dict[str, bytes],
        budget: CaptureIngestionBudget,
    ) -> None:
        """Reject oversized/adversarial manifests before large allocations.

        Everything here is evaluated on declared plan fields and payload
        lengths — no payload is hashed, parsed, or decoded yet.
        """

        def over(kind: str, actual: int, limit: int) -> None:
            raise CaptureIngestionTransactionError(
                f'capture ingestion budget exceeded: {kind} '
                f'{actual} > {limit}'
            )

        if len(plan.source_evidence) > budget.max_source_evidence_count:
            over(
                'source evidence count',
                len(plan.source_evidence),
                budget.max_source_evidence_count,
            )
        declared_bytes = sum(item.bytes for item in plan.source_evidence)
        if declared_bytes > budget.max_source_payload_bytes:
            over(
                'declared source payload bytes',
                declared_bytes,
                budget.max_source_payload_bytes,
            )
        actual_bytes = 0
        for payload in payloads.values():
            if not isinstance(payload, bytes):
                raise TypeError(
                    'capture source payloads must be immutable bytes'
                )
            actual_bytes += len(payload)
        if actual_bytes > budget.max_source_payload_bytes:
            over(
                'source payload bytes',
                actual_bytes,
                budget.max_source_payload_bytes,
            )
        handoffs = plan.raw_visual_mesh_handoffs
        if len(handoffs) > budget.max_mesh_count:
            over('raw mesh count', len(handoffs), budget.max_mesh_count)
        declared_vertices = sum(handoff.vertex_count for handoff in handoffs)
        if declared_vertices > budget.max_vertex_count:
            over(
                'declared vertex count',
                declared_vertices,
                budget.max_vertex_count,
            )
        declared_faces = sum(handoff.face_count for handoff in handoffs)
        if declared_faces > budget.max_face_count:
            over(
                'declared face count',
                declared_faces,
                budget.max_face_count,
            )
        declared_by_path = {item.path: item.bytes for item in plan.source_evidence}
        working = actual_bytes
        for handoff in handoffs:
            geometry_bytes = declared_by_path[handoff.geometry_path]
            working += (
                geometry_bytes * _GEOMETRY_WORKING_FACTOR
                + handoff.vertex_count * _VERTEX_WORKING_BYTES
                + handoff.face_count * _FACE_WORKING_BYTES
            )
        if working > budget.max_working_bytes:
            over(
                'decoded working bytes',
                working,
                budget.max_working_bytes,
            )

    @staticmethod
    def _evidence_payload(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> bytes:
        """Resolve evidence payload bytes from inline or canonical storage.

        Legacy rows carry the payload in payload_blob; deduplicated rows leave
        it empty and reference htdt_content_blobs by payload_sha256. An inline
        blob is authoritative only when it verifies against the recorded
        byte_count/SHA-256, otherwise the canonical blob is consulted.
        """

        inline = row['payload_blob']
        payload = bytes(inline) if inline else b''
        if (
            len(payload) == int(row['byte_count'])
            and sha256(payload).hexdigest() == row['payload_sha256']
        ):
            return payload
        blob = read_content_blob(connection, row['payload_sha256'])
        if blob is None:
            raise CaptureIngestionTransactionError(
                'persisted source evidence payload is missing from the '
                f'content blob store: {row["payload_sha256"]}'
            )
        return blob

    @staticmethod
    def _canonical_geometry_bytes(
        connection: sqlite3.Connection,
        handoff: CaptureMeshHandoff,
    ) -> bytes:
        """Fetch the canonical geometry payload for a persisted binding."""

        blob = read_content_blob(connection, handoff.geometry_sha256)
        if blob is not None:
            return blob
        row = connection.execute(
            '''
            SELECT payload_sha256, byte_count, payload_blob
            FROM capture_source_evidence
            WHERE source_evidence_id=?
            ''',
            (handoff.geometry_source_evidence_id,),
        ).fetchone()
        if row is not None:
            payload = bytes(row['payload_blob']) if row['payload_blob'] else b''
            if (
                row['payload_sha256'] == handoff.geometry_sha256
                and len(payload) == int(row['byte_count'])
                and sha256(payload).hexdigest() == handoff.geometry_sha256
            ):
                return payload
        raise CaptureIngestionTransactionError(
            'canonical capture geometry payload is missing or inconsistent '
            f'for evidence {handoff.geometry_source_evidence_id}'
        )

    def _binding_from_payload_json(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        *,
        binding_id: str | None = None,
        expected_handoff_id: str | None = None,
        normalized_source_ids: tuple[object, object] | None = None,
    ) -> CaptureRawVisualMeshBinding:
        """Rebuild a persisted binding from either storage representation.

        Legacy rows embed the complete RawVisualMesh (including a Base64 copy
        of the source bytes). Compact rows keep only the handoff; the mesh is
        re-adapted from the canonical content-addressed source bytes, which
        re-validates the hash and declared counts and reproduces the identical
        binding identity. When the row's normalized source-authority columns
        are supplied they must equal the canonical ids inside the typed
        payload — a missing or divergent edge fails closed.
        """

        try:
            record = parse_mesh_binding_record(payload_json)
        except ValueError as exc:
            raise CaptureIngestionTransactionError(
                f'persisted mesh binding record is invalid: {exc}'
            ) from exc
        if record is None:
            binding = CaptureRawVisualMeshBinding.model_validate_json(
                payload_json
            )
        else:
            stored_binding_id, handoff = record
            if binding_id is not None and stored_binding_id != binding_id:
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding record identity mismatch'
                )
            asset = self._canonical_geometry_bytes(connection, handoff)
            try:
                binding = adapt_capture_mesh_handoff(handoff, asset)
            except (CaptureMeshIngestionError, ValueError) as exc:
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding cannot be rebuilt from the '
                    f'canonical geometry payload: {exc}'
                ) from exc
            if binding.binding_id != stored_binding_id:
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding does not reproduce its recorded '
                    'identity'
                )
        if (
            expected_handoff_id is not None
            and binding.handoff.raw_visual_mesh_handoff_id
            != expected_handoff_id
        ):
            raise CaptureIngestionTransactionError(
                'persisted mesh binding row disagrees with its payload: '
                f'{binding.binding_id}'
            )
        if normalized_source_ids is not None:
            anchor_index_id, geometry_id = normalized_source_ids
            if not isinstance(anchor_index_id, str) or not isinstance(
                geometry_id, str
            ):
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding is missing its normalized '
                    'source-evidence authorities'
                )
            if (
                binding.handoff.anchor_index_source_evidence_id
                != anchor_index_id
                or binding.handoff.geometry_source_evidence_id != geometry_id
            ):
                raise CaptureIngestionTransactionError(
                    'persisted mesh binding normalized source authorities do '
                    'not match its payload'
                )
        return binding

    def _verify_persisted_materialization(
        self,
        connection: sqlite3.Connection,
        plan: CaptureIngestionPlan,
        run: sqlite3.Row,
    ) -> CaptureIngestionCommitResult:
        """Fail closed unless the run's persisted state is complete.

        The run row alone is not proof of a healthy ingestion, so the
        persisted plan identity and every expected row are re-verified
        against the supplied plan authority and no unexpected linked record
        may exist. The returned counts come from the verified persisted
        sets, not the plan's declared fields.
        """

        lineage = plan.lineage_digest
        run_id = run['ingestion_run_id']
        plan_json = _canonical_json(plan.model_dump(mode='json', by_alias=True))
        plan_sha256 = sha256(plan_json.encode('utf-8')).hexdigest()
        expected_run_id = _ingestion_run_id(
            lineage,
            plan.ingestor.name,
            plan.ingestor.version,
            plan.ingestor.configuration_digest,
            plan_sha256,
        )
        if run_id != expected_run_id:
            raise PersistedIngestionIntegrityError(
                'persisted run identity differs from the supplied plan: '
                f'{run_id}'
            )
        if run['plan_json'] != plan_json:
            raise PersistedIngestionIntegrityError(
                'persisted plan identity differs from the supplied plan: '
                f'{run_id}'
            )
        for column, expected in (
            ('lineage_digest', lineage),
            ('plan_sha256', plan_sha256),
            ('bundle_digest', plan.bundle.bundle_digest),
            ('capture_revision_id', plan.bundle.capture_revision_id),
            ('capture_series_id', plan.bundle.capture_series_id),
            ('parent_revision_id', plan.bundle.parent_revision_id),
            (
                'capture_session_ids_json',
                _canonical_json(list(plan.bundle.capture_session_ids)),
            ),
            (
                'coordinate_space_ids_json',
                _canonical_json(list(plan.bundle.coordinate_space_ids)),
            ),
            ('ingestor_name', plan.ingestor.name),
            ('ingestor_version', plan.ingestor.version),
            ('configuration_digest', plan.ingestor.configuration_digest),
        ):
            if run[column] != expected:
                raise PersistedIngestionIntegrityError(
                    f'run metadata mismatch ({column}): {run_id}'
                )

        # The scoped coordinate authority for every declared space must be
        # registered exactly once with the expected identity (#365).
        for space_id in plan.bundle.coordinate_space_ids:
            authority_id = _coordinate_authority_id(
                plan.bundle.bundle_digest, space_id
            )
            authority = connection.execute(
                '''
                SELECT bundle_digest, capture_revision_id,
                       coordinate_space_id
                FROM capture_coordinate_authorities
                WHERE coordinate_authority_id=?
                ''',
                (authority_id,),
            ).fetchone()
            if authority is None:
                raise PersistedIngestionIntegrityError(
                    'scoped coordinate authority is missing: '
                    f'{authority_id}'
                )
            if (
                authority['bundle_digest']
                != plan.bundle.bundle_digest
                or authority['capture_revision_id']
                != plan.bundle.capture_revision_id
                or authority['coordinate_space_id'] != space_id
            ):
                raise PersistedIngestionIntegrityError(
                    'scoped coordinate authority metadata mismatch: '
                    f'{authority_id}'
                )

        expected_source_ids = {
            item.source_evidence_id for item in plan.source_evidence
        }
        linked_source_ids = {
            row['source_evidence_id']
            for row in connection.execute(
                '''
                SELECT source_evidence_id
                FROM capture_ingestion_source_links
                WHERE ingestion_run_id=?
                ''',
                (run_id,),
            )
        }
        if linked_source_ids != expected_source_ids:
            raise PersistedIngestionIntegrityError(
                'source evidence link set mismatch: '
                f'missing={sorted(expected_source_ids - linked_source_ids)}, '
                f'unexpected={sorted(linked_source_ids - expected_source_ids)}'
            )
        for record in plan.source_evidence:
            self._verify_persisted_source_evidence(connection, record)

        roomplan_rows = connection.execute(
            '''
            SELECT kind, source_evidence_id, payload_json
            FROM capture_roomplan_records
            WHERE ingestion_run_id=?
            ''',
            (run_id,),
        ).fetchall()
        expected_roomplan = {
            (record.kind, record.source_evidence_id): record
            for record in plan.roomplan_records
        }
        persisted_roomplan = {
            (row['kind'], row['source_evidence_id']): row['payload_json']
            for row in roomplan_rows
        }
        if set(persisted_roomplan) != set(expected_roomplan):
            raise PersistedIngestionIntegrityError(
                'RoomPlan record set mismatch: '
                f'missing={sorted(set(expected_roomplan) - set(persisted_roomplan))}, '
                f'unexpected={sorted(set(persisted_roomplan) - set(expected_roomplan))}'
            )
        for key, payload_json in persisted_roomplan.items():
            try:
                persisted = CaptureRoomPlanRecord.model_validate_json(
                    payload_json
                )
            except ValueError as exc:
                raise PersistedIngestionIntegrityError(
                    f'RoomPlan record payload is unreadable: {key}'
                ) from exc
            if persisted != expected_roomplan[key]:
                raise PersistedIngestionIntegrityError(
                    f'RoomPlan record payload mismatch: {key}'
                )

        expected_binding_ids: set[str] = set()
        for handoff in plan.raw_visual_mesh_handoffs:
            row = connection.execute(
                '''
                SELECT binding_id, payload_json,
                       anchor_index_source_evidence_id,
                       geometry_source_evidence_id
                FROM capture_raw_visual_mesh_bindings
                WHERE handoff_id=?
                ''',
                (handoff.raw_visual_mesh_handoff_id,),
            ).fetchone()
            if row is None:
                raise PersistedIngestionIntegrityError(
                    'raw mesh binding is missing for handoff: '
                    f'{handoff.raw_visual_mesh_handoff_id}'
                )
            try:
                binding = self._binding_from_payload_json(
                    connection,
                    row['payload_json'],
                    binding_id=row['binding_id'],
                    normalized_source_ids=(
                        row['anchor_index_source_evidence_id'],
                        row['geometry_source_evidence_id'],
                    ),
                )
            except ValueError as exc:
                raise PersistedIngestionIntegrityError(
                    'raw mesh binding cannot be verified: '
                    f'{row["binding_id"]}: {exc}'
                ) from exc
            if (
                binding.binding_id != row['binding_id']
                or binding.handoff != handoff
            ):
                raise PersistedIngestionIntegrityError(
                    'raw mesh binding identity or source authority mismatch: '
                    f'{row["binding_id"]}'
                )
            expected_binding_ids.add(row['binding_id'])
        linked_binding_ids = {
            row['binding_id']
            for row in connection.execute(
                '''
                SELECT binding_id
                FROM capture_ingestion_mesh_links
                WHERE ingestion_run_id=?
                ''',
                (run_id,),
            )
        }
        if linked_binding_ids != expected_binding_ids:
            raise PersistedIngestionIntegrityError(
                'raw mesh binding link set mismatch: '
                f'missing={sorted(expected_binding_ids - linked_binding_ids)}, '
                f'unexpected={sorted(linked_binding_ids - expected_binding_ids)}'
            )

        expected_authority_ids = {
            record.authority_record_handoff_id
            for record in plan.authority_records
        }
        linked_authority_ids = {
            row['authority_record_handoff_id']
            for row in connection.execute(
                '''
                SELECT authority_record_handoff_id
                FROM capture_ingestion_authority_links
                WHERE ingestion_run_id=?
                ''',
                (run_id,),
            )
        }
        if linked_authority_ids != expected_authority_ids:
            raise PersistedIngestionIntegrityError(
                'authority record link set mismatch: '
                f'missing={sorted(expected_authority_ids - linked_authority_ids)}, '
                f'unexpected={sorted(linked_authority_ids - expected_authority_ids)}'
            )
        for record in plan.authority_records:
            row = connection.execute(
                '''
                SELECT source_evidence_id, payload_json
                FROM capture_authority_records
                WHERE authority_record_handoff_id=?
                ''',
                (record.authority_record_handoff_id,),
            ).fetchone()
            if row is None:
                raise PersistedIngestionIntegrityError(
                    'authority record is missing: '
                    f'{record.authority_record_handoff_id}'
                )
            try:
                persisted = CaptureAuthorityRecord.model_validate_json(
                    row['payload_json']
                )
            except ValueError as exc:
                raise PersistedIngestionIntegrityError(
                    'authority record payload is unreadable: '
                    f'{record.authority_record_handoff_id}'
                ) from exc
            if (
                persisted != record
                or row['source_evidence_id'] != record.source_evidence_id
            ):
                raise PersistedIngestionIntegrityError(
                    'authority record identity or source authority mismatch: '
                    f'{record.authority_record_handoff_id}'
                )

        quality_state = self._resolve_persisted_quality(connection, plan)

        return CaptureIngestionCommitResult(
            ingestion_run_id=str(run_id),
            lineage_digest=lineage,
            bundle_digest=plan.bundle.bundle_digest,
            source_evidence_count=len(linked_source_ids),
            roomplan_record_count=len(roomplan_rows),
            raw_mesh_binding_count=len(linked_binding_ids),
            authority_record_count=len(linked_authority_ids),
            supplemental_document_count=len(plan.supplemental_documents),
            created=False,
            capture_revision_id=plan.bundle.capture_revision_id,
            capture_series_id=plan.bundle.capture_series_id,
            quality_state=quality_state.state,
            quality_ruleset_version=quality_state.ruleset_version,
            quality_payload_sha256=quality_state.payload_sha256,
        )

    def _verify_persisted_source_evidence(
        self,
        connection: sqlite3.Connection,
        record: CaptureSourceEvidence,
    ) -> None:
        """Re-verify one evidence row's metadata and payload bytes."""

        row = connection.execute(
            '''
            SELECT *
            FROM capture_source_evidence
            WHERE source_evidence_id=?
            ''',
            (record.source_evidence_id,),
        ).fetchone()
        if row is None:
            raise PersistedIngestionIntegrityError(
                'source evidence row is missing: '
                f'{record.source_evidence_id}'
            )
        try:
            persisted = CaptureSourceEvidence(
                source_evidence_id=row['source_evidence_id'],
                bundle_digest=row['bundle_digest'],
                capture_revision_id=row['capture_revision_id'],
                path=row['logical_path'],
                payload_sha256=row['payload_sha256'],
                bytes=row['byte_count'],
                media_type=row['media_type'],
                producer=row['producer'],
                provenance_class=row['provenance_class'],
                role=row['role'],
                source_refs=tuple(json.loads(row['source_refs_json'])),
            )
        except (ValueError, TypeError) as exc:
            raise PersistedIngestionIntegrityError(
                'source evidence metadata is unreadable: '
                f'{record.source_evidence_id}'
            ) from exc
        if (
            _canonical_json(persisted.model_dump(mode='json'))
            != _canonical_json(record.model_dump(mode='json'))
        ):
            raise PersistedIngestionIntegrityError(
                'source evidence metadata mismatch: '
                f'{record.source_evidence_id}'
            )
        try:
            payload = self._evidence_payload(connection, row)
        except (ValueError, sqlite3.Error) as exc:
            raise PersistedIngestionIntegrityError(
                'source evidence payload is missing or corrupt: '
                f'{record.source_evidence_id}: {exc}'
            ) from exc
        if (
            len(payload) != record.bytes
            or sha256(payload).hexdigest() != record.payload_sha256
        ):
            raise PersistedIngestionIntegrityError(
                'source evidence payload mismatch: '
                f'{record.source_evidence_id}'
            )
    # ------------------------------------------------------------------
    # Capture Bundle v1 contract rederivation (#343/#345/#369/#337)
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_manifest_projection(
        typed: CaptureIngestionPlan,
    ) -> dict:
        """Project the plan's bundle/source declarations into the manifest
        shape the shared contract validators consume."""
        return {
            'bundle_digest': typed.bundle.bundle_digest,
            'schema': typed.bundle.capture_schema,
            'schema_version': typed.bundle.capture_schema_version,
            'capture_series_id': typed.bundle.capture_series_id,
            'capture_revision_id': typed.bundle.capture_revision_id,
            'parent_revision_id': typed.bundle.parent_revision_id,
            'capture_session_ids': list(typed.bundle.capture_session_ids),
            'coordinate_space_ids': list(
                typed.bundle.coordinate_space_ids
            ),
            'files': [
                {
                    'path': item.path,
                    'bytes': item.bytes,
                    'media_type': item.media_type,
                    'sha256': item.payload_sha256,
                    'producer': item.producer,
                    'provenance_class': item.provenance_class,
                    'role': item.role,
                    'source_refs': list(item.source_refs),
                }
                for item in typed.source_evidence
            ],
        }

    def _validate_source_payload_contract(
        self,
        typed: CaptureIngestionPlan,
        payloads: Mapping[str, bytes],
    ) -> CaptureQualityState:
        """Schema/binary/metadata-validate then semantically rederive every
        handoff claim from the exact payload bytes.

        The shared bounded pipeline runs the common schema layer first
        (#369), then session/frame identity (#345), the quality gate
        (#337) and mesh/authority handoff rederivation (#343). A mismatch
        between the declared plan and the recomputed contract fails
        closed before any source-authority commit.
        """
        manifest = self._plan_manifest_projection(typed)
        try:
            sections = capture_reference.rederive_plan_sections(
                manifest, dict(payloads)
            )
        except CaptureBundleError as exc:
            raise CapturePayloadContractError(str(exc)) from exc
        except capture_reference.CaptureIngestionContractError as exc:
            raise CapturePayloadContractError(str(exc)) from exc

        def _norm_numbers(value):
            """Normalize JSON numbers for comparison: pydantic dumps coerce
            integral floats to ``1.0`` while exact payload bytes keep
            ``1`` — semantically identical numbers."""
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, dict):
                return {k: _norm_numbers(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_norm_numbers(v) for v in value]
            return value

        def _canonical_rows(rows):
            return sorted(
                _canonical_json(_norm_numbers(json.loads(_canonical_json(row))))
                for row in rows
            )

        expected_sources = _canonical_rows(
            item.model_dump(mode='json') for item in typed.source_evidence
        )
        recomputed_sources = _canonical_rows(sections['source_records'])
        if expected_sources != recomputed_sources:
            raise CapturePayloadContractError(
                'declared source evidence does not match the rederived '
                'exact payload contract'
            )

        expected_roomplan = _canonical_rows(
            item.model_dump(mode='json') for item in typed.roomplan_records
        )
        recomputed_roomplan = _canonical_rows(sections['roomplan_records'])
        if expected_roomplan != recomputed_roomplan:
            raise CapturePayloadContractError(
                'declared RoomPlan records do not match the rederived '
                'exact payload contract'
            )
        expected_metadata = _canonical_rows(
            [
                typed.roomplan_capture_metadata.model_dump(mode='json')
                if typed.roomplan_capture_metadata is not None
                else None
            ]
        )[0]
        recomputed_metadata = _canonical_rows(
            [sections['roomplan_capture_metadata']]
        )[0]
        if expected_metadata != recomputed_metadata:
            raise CapturePayloadContractError(
                'declared RoomPlan capture metadata does not match the '
                'exact payload contract'
            )

        expected_handoffs = _canonical_rows(
            item.model_dump(mode='json', by_alias=True)
            for item in typed.raw_visual_mesh_handoffs
        )
        recomputed_handoffs = _canonical_rows(
            sections['raw_visual_mesh_handoffs']
        )
        if expected_handoffs != recomputed_handoffs:
            raise CapturePayloadContractError(
                'declared raw mesh handoffs do not match the rederived '
                'exact mesh/anchors.json contract'
            )

        expected_authority = _canonical_rows(
            item.model_dump(mode='json') for item in typed.authority_records
        )
        recomputed_authority = _canonical_rows(
            sections['authority_records']
        )
        if expected_authority != recomputed_authority:
            raise CapturePayloadContractError(
                'declared authority records do not match the rederived '
                'exact annotation/measurement contract'
            )

        quality_document = sections['quality_document']
        quality_sha = next(
            entry['sha256']
            for entry in manifest['files']
            if entry['path'] == 'quality/capture-quality.json'
        )
        return CaptureQualityState(
            'validated',
            quality_sha,
            quality_document['ruleset_version'],
        )

    # ------------------------------------------------------------------
    # Canonical manifest retention (#338)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_manifest_for_plan(
        typed: CaptureIngestionPlan,
        manifest_bytes: bytes,
        payloads: Mapping[str, bytes],
    ) -> dict:
        """Validate exact canonical manifest bytes against the plan/bundle
        identity they claim to authorize."""
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CapturePayloadContractError(
                'manifest.json exceeds the manifest byte limit'
            )
        try:
            manifest = _parse_bundle_json(manifest_bytes)
            validate_manifest_shape(manifest)
        except CaptureBundleError as exc:
            raise CapturePayloadContractError(
                f'manifest.json is invalid: {exc}'
            ) from exc
        if _canonical_bundle_bytes(manifest) != manifest_bytes:
            raise CapturePayloadContractError(
                'manifest.json is not Capture Bundle v1 canonical JSON'
            )
        family, contract = _family_for_path('manifest.json')
        if contract is None:
            raise CapturePayloadContractError(
                'manifest.json is not covered by the support matrix'
            )
        try:
            document_key = _schema_document_for_version(
                'manifest.json', manifest, family, contract
            )
            _schema_validate(
                manifest, _load_schema(f'{document_key}.schema.json')
            )
        except (CaptureBundleError, _CaptureSchemaError) as exc:
            raise CapturePayloadContractError(
                f'manifest.json violates the pinned manifest schema: {exc}'
            ) from exc

        digest = sha256(manifest_bytes).hexdigest()
        if digest != typed.bundle.bundle_digest:
            raise CapturePayloadContractError(
                'manifest.json SHA-256 does not equal the plan bundle digest'
            )
        bundle = typed.bundle
        expected_identity = {
            'schema': bundle.capture_schema,
            'schema_version': bundle.capture_schema_version,
            'capture_series_id': bundle.capture_series_id,
            'capture_revision_id': bundle.capture_revision_id,
            'parent_revision_id': bundle.parent_revision_id,
            'capture_session_ids': list(bundle.capture_session_ids),
            'coordinate_space_ids': list(bundle.coordinate_space_ids),
        }
        for field, expected in expected_identity.items():
            if manifest[field] != expected:
                raise CaptureIdentityReferenceError(
                    f'manifest.json {field} disagrees with the accepted '
                    'plan bundle identity'
                )

        declared_entries = {
            entry['path']: entry for entry in manifest['files']
        }
        if set(declared_entries) != set(payloads):
            raise CapturePayloadContractError(
                'manifest declared payload set does not match the plan '
                'source evidence set'
            )
        try:
            _enforce_reserved_path_metadata(manifest, declared_entries)
            _validate_bundle_source_refs(manifest, declared_entries)
        except CaptureBundleError as exc:
            raise CapturePayloadContractError(str(exc)) from exc

        by_path = {item.path: item for item in typed.source_evidence}
        for path, entry in declared_entries.items():
            record = by_path[path]
            declared_pair = (
                entry['media_type'],
                entry['producer'],
                entry['provenance_class'],
                entry['role'],
                entry['sha256'],
                entry['bytes'],
                tuple(entry.get('source_refs', [])),
            )
            plan_pair = (
                record.media_type,
                record.producer,
                record.provenance_class,
                record.role,
                record.payload_sha256,
                record.bytes,
                record.source_refs,
            )
            if declared_pair != plan_pair:
                raise CaptureIdentityReferenceError(
                    f'manifest entry metadata for {path} disagrees with '
                    'the accepted plan source evidence'
                )
        return manifest

    def _persist_capture_bundle(
        self,
        connection: sqlite3.Connection,
        typed: CaptureIngestionPlan,
        manifest_bytes: bytes,
        manifest: dict,
    ) -> None:
        """Retain the canonical manifest as immutable bundle evidence."""
        bundle_digest = typed.bundle.bundle_digest
        row = connection.execute(
            '''
            SELECT manifest_sha256, capture_revision_id
            FROM capture_bundles WHERE bundle_digest=?
            ''',
            (bundle_digest,),
        ).fetchone()
        if row is not None:
            if (
                row['manifest_sha256'] != sha256(manifest_bytes).hexdigest()
                or row['capture_revision_id']
                != typed.bundle.capture_revision_id
            ):
                raise PersistedIngestionIntegrityError(
                    'persisted capture bundle manifest conflicts with the '
                    'incoming canonical manifest'
                )
            return

        app = manifest['app']
        store_content_blob(connection, manifest_bytes)
        connection.execute(
            '''
            INSERT INTO capture_bundles(
                bundle_digest, capture_revision_id, manifest_sha256,
                app_name, app_version, app_build,
                created_at, finalized_at, manifest_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, X'')
            ''',
            (
                bundle_digest,
                typed.bundle.capture_revision_id,
                sha256(manifest_bytes).hexdigest(),
                app['name'],
                app['version'],
                app['build'],
                manifest['created_at'],
                manifest['finalized_at'],
            ),
        )

    # ------------------------------------------------------------------
    # Immutable capture revision registry (#335)
    # ------------------------------------------------------------------

    def _register_capture_revision(
        self,
        connection: sqlite3.Connection,
        typed: CaptureIngestionPlan,
    ) -> None:
        """Register/enforce the immutable revision identity and topology."""
        bundle = typed.bundle
        revision_id = bundle.capture_revision_id
        row = connection.execute(
            '''
            SELECT * FROM capture_revisions WHERE capture_revision_id=?
            ''',
            (revision_id,),
        ).fetchone()
        if row is not None:
            mismatched = [
                column
                for column, expected in (
                    ('capture_series_id', bundle.capture_series_id),
                    ('parent_revision_id', bundle.parent_revision_id),
                    ('bundle_digest', bundle.bundle_digest),
                    ('capture_schema', bundle.capture_schema),
                    (
                        'capture_schema_version',
                        bundle.capture_schema_version,
                    ),
                )
                if row[column] != expected
            ]
            if mismatched:
                raise CaptureRevisionConflictError(
                    f'capture_revision_id {revision_id} was previously '
                    f'registered with different immutable fields: '
                    f'{mismatched}'
                )
            return

        if bundle.parent_revision_id is None:
            topology = 'root'
        else:
            parent = connection.execute(
                '''
                SELECT capture_series_id FROM capture_revisions
                WHERE capture_revision_id=?
                ''',
                (bundle.parent_revision_id,),
            ).fetchone()
            if parent is None:
                topology = 'pending_parent'
            elif (
                parent['capture_series_id'] != bundle.capture_series_id
            ):
                raise CaptureRevisionConflictError(
                    f'capture_revision_id {revision_id} names parent '
                    f'{bundle.parent_revision_id} from a different '
                    'capture series'
                )
            else:
                topology = 'linked'

        # Cycle prevention: a chain from the declared parent that reaches
        # this revision means registering it would close a loop.
        ancestor = bundle.parent_revision_id
        seen: set[str] = set()
        while ancestor is not None:
            if ancestor == revision_id:
                raise CaptureRevisionConflictError(
                    'capture revision topology cycle through '
                    f'{revision_id}'
                )
            if ancestor in seen:
                break
            seen.add(ancestor)
            ancestor_row = connection.execute(
                '''
                SELECT parent_revision_id FROM capture_revisions
                WHERE capture_revision_id=?
                ''',
                (ancestor,),
            ).fetchone()
            ancestor = (
                ancestor_row['parent_revision_id']
                if ancestor_row is not None
                else None
            )

        connection.execute(
            '''
            INSERT INTO capture_revisions(
                capture_revision_id, capture_series_id,
                parent_revision_id, bundle_digest,
                capture_schema, capture_schema_version,
                topology_state, first_lineage_digest, registered_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                revision_id,
                bundle.capture_series_id,
                bundle.parent_revision_id,
                bundle.bundle_digest,
                bundle.capture_schema,
                bundle.capture_schema_version,
                topology,
                typed.lineage_digest,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # Reconcile children that arrived before their parent: any
        # pending_parent child of this revision now links — a child whose
        # series disagrees is surfaced as a recorded conflict rather than
        # silently accepted.
        pending_children = connection.execute(
            '''
            SELECT capture_revision_id, capture_series_id
            FROM capture_revisions
            WHERE parent_revision_id=? AND topology_state='pending_parent'
            ''',
            (revision_id,),
        ).fetchall()
        for child in pending_children:
            if child['capture_series_id'] == bundle.capture_series_id:
                connection.execute(
                    '''
                    UPDATE capture_revisions SET topology_state='linked'
                    WHERE capture_revision_id=?
                    ''',
                    (child['capture_revision_id'],),
                )
            else:
                connection.execute(
                    '''
                    UPDATE capture_revisions
                    SET topology_state='conflict'
                    WHERE capture_revision_id=?
                    ''',
                    (child['capture_revision_id'],),
                )
                self._record_revision_conflict(
                    connection,
                    child['capture_revision_id'],
                    'parent revision belongs to a different capture series',
                )

    @staticmethod
    def _record_revision_conflict(
        connection: sqlite3.Connection,
        revision_id: str,
        detail: str,
    ) -> None:
        connection.execute(
            '''
            INSERT OR IGNORE INTO capture_revision_conflicts(
                capture_revision_id, detail, recorded_at_utc
            ) VALUES (?, ?, ?)
            ''',
            (
                revision_id,
                detail,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _migrate_revision_registry(
        self, connection: sqlite3.Connection
    ) -> None:
        """Backfill the revision registry from pre-existing runs.

        Existing consistent ingestion records migrate without identity
        loss; a pre-existing conflict is recorded in
        capture_revision_conflicts rather than guessed at.
        """
        runs = connection.execute(
            '''
            SELECT lineage_digest, plan_json, recorded_at_utc
            FROM capture_ingestion_runs
            ORDER BY recorded_at_utc
            '''
        ).fetchall()
        for run in runs:
            try:
                plan = CaptureIngestionPlan.model_validate_json(
                    run['plan_json']
                )
            except ValueError:
                self._record_revision_conflict(
                    connection,
                    'unknown',
                    'persisted ingestion plan is unreadable during '
                    'revision-registry migration',
                )
                continue
            existing = connection.execute(
                '''
                SELECT capture_revision_id FROM capture_revisions
                WHERE capture_revision_id=?
                ''',
                (plan.bundle.capture_revision_id,),
            ).fetchone()
            if existing is not None:
                stored = connection.execute(
                    '''
                    SELECT * FROM capture_revisions
                    WHERE capture_revision_id=?
                    ''',
                    (plan.bundle.capture_revision_id,),
                ).fetchone()
                mismatched = [
                    column
                    for column, expected in (
                        ('capture_series_id', plan.bundle.capture_series_id),
                        (
                            'parent_revision_id',
                            plan.bundle.parent_revision_id,
                        ),
                        ('bundle_digest', plan.bundle.bundle_digest),
                        ('capture_schema', plan.bundle.capture_schema),
                        (
                            'capture_schema_version',
                            plan.bundle.capture_schema_version,
                        ),
                    )
                    if stored[column] != expected
                ]
                if mismatched:
                    self._record_revision_conflict(
                        connection,
                        plan.bundle.capture_revision_id,
                        'pre-existing ingestion runs disagree on '
                        f'immutable revision fields: {mismatched}',
                    )
                continue
            try:
                self._register_capture_revision(connection, plan)
            except CaptureRevisionConflictError as exc:
                self._record_revision_conflict(
                    connection,
                    plan.bundle.capture_revision_id,
                    str(exc),
                )

    # ------------------------------------------------------------------
    # Persisted quality-gate state (#337)
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_run_quality_columns(
        connection: sqlite3.Connection
    ) -> None:
        columns = {
            row['name']
            for row in connection.execute(
                'PRAGMA table_info(capture_ingestion_runs)'
            )
        }
        for column, ddl in (
            ('quality_state', "TEXT NOT NULL DEFAULT 'unresolved'"),
            ('quality_payload_sha256', 'TEXT'),
            ('quality_ruleset_version', 'TEXT'),
        ):
            if column not in columns:
                connection.execute(
                    f'ALTER TABLE capture_ingestion_runs '
                    f'ADD COLUMN {column} {ddl}'
                )

    def _resolve_persisted_quality(
        self,
        connection: sqlite3.Connection,
        plan: CaptureIngestionPlan,
    ) -> CaptureQualityState:
        """Establish the persisted quality state for one run.

        Rows written before the gate carry NULL/'unresolved' quality
        columns: the persisted quality payload is re-read from retained
        exact evidence and revalidated — a pass backfills the columns,
        while missing or failing evidence leaves the run unresolved
        rather than guessing.
        """
        row = connection.execute(
            '''
            SELECT quality_state, quality_payload_sha256,
                   quality_ruleset_version
            FROM capture_ingestion_runs WHERE lineage_digest=?
            ''',
            (plan.lineage_digest,),
        ).fetchone()
        if row is None:
            raise PersistedIngestionIntegrityError(
                'ingestion run is not persisted: ' + plan.lineage_digest
            )
        if row['quality_state'] == 'validated':
            return CaptureQualityState(
                'validated',
                row['quality_payload_sha256'],
                row['quality_ruleset_version'],
            )

        quality_record = next(
            (
                item
                for item in plan.source_evidence
                if item.path == 'quality/capture-quality.json'
            ),
            None,
        )
        state = CaptureQualityState('unresolved', None, None)
        if quality_record is not None:
            evidence_row = connection.execute(
                '''
                SELECT * FROM capture_source_evidence
                WHERE source_evidence_id=?
                ''',
                (quality_record.source_evidence_id,),
            ).fetchone()
            if evidence_row is not None:
                try:
                    payload = self._evidence_payload(
                        connection, evidence_row
                    )
                    document = _parse_bundle_json(payload)
                    capture_reference._validate_quality_document(
                        document, 'quality/capture-quality.json'
                    )
                    if (
                        document['ready_for_htdt_ingestion'] is True
                        and document['integrity_status'] == 'pass'
                        and document['ruleset_version']
                        in capture_reference.SUPPORTED_QUALITY_RULESETS
                        and not any(
                            d['severity'] == 'error'
                            for d in document['diagnostics']
                        )
                        and not any(
                            e['severity'] == 'error'
                            for e in document['resource_events']
                        )
                    ):
                        state = CaptureQualityState(
                            'validated',
                            quality_record.payload_sha256,
                            document['ruleset_version'],
                        )
                except (
                    ValueError,
                    CaptureBundleError,
                    capture_reference.CaptureIngestionContractError,
                ):
                    state = CaptureQualityState('unresolved', None, None)
        connection.execute(
            '''
            UPDATE capture_ingestion_runs
            SET quality_state=?,
                quality_payload_sha256=?,
                quality_ruleset_version=?
            WHERE lineage_digest=?
            ''',
            (
                state.state,
                state.payload_sha256,
                state.ruleset_version,
                plan.lineage_digest,
            ),
        )
        return state

    def get_capture_quality_state(
        self, lineage_digest: str
    ) -> CaptureQualityState | None:
        """Queryable persisted quality state for one ingestion run."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT quality_state, quality_payload_sha256,
                       quality_ruleset_version
                FROM capture_ingestion_runs WHERE lineage_digest=?
                ''',
                (lineage_digest,),
            ).fetchone()
        if row is None:
            return None
        return CaptureQualityState(
            row['quality_state'],
            row['quality_payload_sha256'],
            row['quality_ruleset_version'],
        )

    def require_quality_state(
        self, lineage_digest: str
    ) -> CaptureQualityState:
        """Fail closed unless the run's quality gate was validated."""
        state = self.get_capture_quality_state(lineage_digest)
        if state is None:
            raise CaptureQualityGateError(
                'ingestion run is not persisted: ' + lineage_digest
            )
        if state.state != 'validated':
            raise CaptureQualityGateError(
                'ingestion run has no validated quality authority under '
                'the supported ruleset: ' + lineage_digest
            )
        return state

    # ------------------------------------------------------------------
    # Revision/bundle accessors
    # ------------------------------------------------------------------

    @staticmethod
    def _revision_record(row: sqlite3.Row) -> CaptureRevisionRecord:
        return CaptureRevisionRecord(
            capture_revision_id=row['capture_revision_id'],
            capture_series_id=row['capture_series_id'],
            parent_revision_id=row['parent_revision_id'],
            bundle_digest=row['bundle_digest'],
            capture_schema=row['capture_schema'],
            capture_schema_version=row['capture_schema_version'],
            topology_state=row['topology_state'],
            first_lineage_digest=row['first_lineage_digest'],
            registered_at_utc=row['registered_at_utc'],
        )

    def get_registered_revision(
        self, capture_revision_id: str
    ) -> CaptureRevisionRecord | None:
        """The immutable registry row for one capture revision, or None."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT * FROM capture_revisions WHERE capture_revision_id=?
                ''',
                (capture_revision_id,),
            ).fetchone()
        return self._revision_record(row) if row is not None else None

    def list_registered_revisions(
        self,
    ) -> tuple[CaptureRevisionRecord, ...]:
        """Every revision the registry holds — including ones whose
        declared parent is not persisted yet (``pending_parent``)."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''
                SELECT * FROM capture_revisions
                ORDER BY capture_series_id, registered_at_utc
                '''
            ).fetchall()
        return tuple(self._revision_record(row) for row in rows)

    def list_revision_conflicts(self) -> tuple[CaptureRevisionConflict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''
                SELECT capture_revision_id, detail
                FROM capture_revision_conflicts
                ORDER BY recorded_at_utc
                '''
            ).fetchall()
        return tuple(
            CaptureRevisionConflict(
                capture_revision_id=row['capture_revision_id'],
                detail=row['detail'],
            )
            for row in rows
        )

    def get_capture_bundle(
        self, bundle_digest: str
    ) -> CaptureBundleRecord | None:
        """Reopen the exact canonical manifest retained for a bundle."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT * FROM capture_bundles WHERE bundle_digest=?
                ''',
                (bundle_digest,),
            ).fetchone()
            if row is None:
                return None
            payload = read_content_blob(
                connection, row['manifest_sha256']
            )
            if payload is None:
                inline = (
                    bytes(row['manifest_blob']) if row['manifest_blob']
                    else b''
                )
                payload = inline
        if not payload:
            raise PersistedIngestionIntegrityError(
                'persisted capture bundle manifest bytes are missing: '
                f'{bundle_digest}'
            )
        if sha256(payload).hexdigest() != row['manifest_sha256']:
            raise PersistedIngestionIntegrityError(
                'persisted capture bundle manifest fails its SHA-256: '
                f'{bundle_digest}'
            )
        return CaptureBundleRecord(
            bundle_digest=row['bundle_digest'],
            capture_revision_id=row['capture_revision_id'],
            app_name=row['app_name'],
            app_version=row['app_version'],
            app_build=row['app_build'],
            created_at=row['created_at'],
            finalized_at=row['finalized_at'],
            manifest_bytes=payload,
        )

    def list_capture_bundles(self) -> tuple[CaptureBundleRecord, ...]:
        with closing(self._connect()) as connection:
            digests = [
                row['bundle_digest']
                for row in connection.execute(
                    'SELECT bundle_digest FROM capture_bundles '
                    'ORDER BY created_at'
                ).fetchall()
            ]
        return tuple(
            record
            for digest in digests
            if (record := self.get_capture_bundle(digest)) is not None
        )


    def get_supplemental_document(
        self,
        lineage_digest: str,
        supplemental_document_handoff_id: str,
    ) -> CaptureSupplementalDocument | None:
        """Re-read one persisted supplemental authority by handoff id.

        Supplemental documents persist verbatim inside the run's plan
        authority (``plan_json``) like every other plan member — the
        persisted-materialization check therefore covers them without a
        separate row family.
        """

        plan = self.get_ingestion(lineage_digest)
        if plan is None:
            return None
        for document in plan.supplemental_documents:
            if (
                document.supplemental_document_handoff_id
                == supplemental_document_handoff_id
            ):
                return document
        return None

    @staticmethod
    def _result(
        plan: CaptureIngestionPlan,
        ingestion_run_id: str,
        *,
        created: bool,
        quality_state: CaptureQualityState | None = None,
    ) -> CaptureIngestionCommitResult:
        state = quality_state or CaptureQualityState(
            'unresolved', None, None
        )
        return CaptureIngestionCommitResult(
            ingestion_run_id=ingestion_run_id,
            lineage_digest=plan.lineage_digest,
            bundle_digest=plan.bundle.bundle_digest,
            source_evidence_count=len(plan.source_evidence),
            roomplan_record_count=len(plan.roomplan_records),
            raw_mesh_binding_count=len(plan.raw_visual_mesh_handoffs),
            authority_record_count=len(plan.authority_records),
            created=created,
            capture_revision_id=plan.bundle.capture_revision_id,
            capture_series_id=plan.bundle.capture_series_id,
            quality_state=state.state,
            quality_ruleset_version=state.ruleset_version,
            quality_payload_sha256=state.payload_sha256,
            supplemental_document_count=len(plan.supplemental_documents),
        )


def run_capture_schema_convergence(connection: sqlite3.Connection) -> None:
    """Legacy-shape tail of the schema-authority migration (#302).

    ``ensure_native_schema`` invokes this while converging databases whose
    persisted shapes predate the canonical contract; it runs the same
    sequence ``CaptureIngestionRepository._initialize`` applies, without
    constructing a repository instance (the helpers it reaches use only
    ``connection`` and parameter-free class helpers).
    """

    repository = CaptureIngestionRepository.__new__(CaptureIngestionRepository)
    repository._converge_schema(connection)
