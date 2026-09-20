from __future__ import annotations

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
from htdt.cad_schema import ensure_native_schema
from htdt.capture_mesh_ingestion import (
    CaptureMeshHandoff,
    CaptureRawVisualMeshBinding,
    adapt_capture_mesh_handoff,
)


UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
SOURCE_EVIDENCE_DOMAIN = 'htdt.capture.source-evidence.v1'
RAW_MESH_HANDOFF_DOMAIN = 'htdt.capture.raw-visual-mesh-handoff.v1'
AUTHORITY_HANDOFF_DOMAIN = 'htdt.capture.authority-record.v1'
INGESTOR_CONFIGURATION_DIGEST = (
    '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
)


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


def _hash_parts(prefix: str, *parts: str) -> str:
    digest = sha256(prefix.encode('utf-8'))
    for part in parts:
        digest.update(b'\x00')
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


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


class CaptureIngestorIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal['htdt-capture-reference-ingestor']
    version: Literal['1.0.0']
    configuration_digest: Literal[
        '3e27eec298714a04fc6b48d94b354168396e2c4eea0cf9aa8284fa552de562b3'
    ]


class CaptureBundleIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    model_config = ConfigDict(frozen=True)

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


class CaptureRoomPlanRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal['raw_scan', 'postprocessed_inference']
    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    path: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provenance_class: Literal[
        'apple_roomplan_raw_scan',
        'apple_roomplan_inference',
    ]
    source_refs: tuple[str, ...] = ()


class CaptureAuthorityRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_record_handoff_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    record_kind: Literal['annotation', 'measurement']
    record_id: str
    record_locator: str = Field(min_length=1)
    provenance_class: CaptureProvenance
    coordinate_space_id: str | None = None
    source_evidence_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_payload_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

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


class CaptureIngestionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema: Literal['htdt.capture.ingestion-plan']
    schema_version: Literal['1.0.0']
    ingestor: CaptureIngestorIdentity
    bundle: CaptureBundleIdentity
    source_evidence: tuple[CaptureSourceEvidence, ...]
    roomplan_records: tuple[CaptureRoomPlanRecord, ...]
    raw_visual_mesh_handoffs: tuple[CaptureMeshHandoff, ...]
    authority_records: tuple[CaptureAuthorityRecord, ...]
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
            for source_ref in item.source_refs:
                if source_ref.startswith('sha256:'):
                    target_hash = source_ref.removeprefix('sha256:')
                    if (
                        not HEX64_RE.fullmatch(target_hash)
                        or target_hash not in source_hashes
                    ):
                        raise ValueError('unresolved source evidence SHA-256 reference')
                elif source_ref.startswith('path:'):
                    target_path = source_ref.removeprefix('path:')
                    _validate_logical_path(target_path)
                    if target_path not in by_path:
                        raise ValueError('unresolved source evidence path reference')

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

        projection = {
            'bundle_digest': bundle_digest,
            'source_evidence_ids': sorted(source_ids),
            'raw_visual_mesh_ids': sorted(handoff_ids),
            'authority_record_ids': sorted(authority_ids),
        }
        expected_lineage = sha256(
            _canonical_json(projection).encode('utf-8')
        ).hexdigest()
        if self.lineage_digest != expected_lineage:
            raise ValueError('ingestion plan lineage digest mismatch')
        return self


@dataclass(frozen=True)
class CaptureIngestionCommitResult:
    lineage_digest: str
    bundle_digest: str
    source_evidence_count: int
    roomplan_record_count: int
    raw_mesh_binding_count: int
    authority_record_count: int
    created: bool


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
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS capture_ingestion_runs (
                    lineage_digest TEXT PRIMARY KEY,
                    bundle_digest TEXT NOT NULL,
                    capture_revision_id TEXT NOT NULL,
                    ingestor_name TEXT NOT NULL,
                    ingestor_version TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
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
                    payload_blob BLOB NOT NULL,
                    UNIQUE(bundle_digest, logical_path)
                );

                CREATE TABLE IF NOT EXISTS capture_ingestion_source_links (
                    lineage_digest TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    PRIMARY KEY(lineage_digest, source_evidence_id),
                    FOREIGN KEY(lineage_digest)
                        REFERENCES capture_ingestion_runs(lineage_digest),
                    FOREIGN KEY(source_evidence_id)
                        REFERENCES capture_source_evidence(source_evidence_id)
                );

                CREATE TABLE IF NOT EXISTS capture_roomplan_records (
                    lineage_digest TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(lineage_digest, kind, source_evidence_id),
                    FOREIGN KEY(lineage_digest)
                        REFERENCES capture_ingestion_runs(lineage_digest),
                    FOREIGN KEY(source_evidence_id)
                        REFERENCES capture_source_evidence(source_evidence_id)
                );

                CREATE TABLE IF NOT EXISTS capture_raw_visual_mesh_bindings (
                    binding_id TEXT PRIMARY KEY,
                    handoff_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capture_ingestion_mesh_links (
                    lineage_digest TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    PRIMARY KEY(lineage_digest, binding_id),
                    FOREIGN KEY(lineage_digest)
                        REFERENCES capture_ingestion_runs(lineage_digest),
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

                CREATE TABLE IF NOT EXISTS capture_ingestion_authority_links (
                    lineage_digest TEXT NOT NULL,
                    authority_record_handoff_id TEXT NOT NULL,
                    PRIMARY KEY(lineage_digest, authority_record_handoff_id),
                    FOREIGN KEY(lineage_digest)
                        REFERENCES capture_ingestion_runs(lineage_digest),
                    FOREIGN KEY(authority_record_handoff_id)
                        REFERENCES capture_authority_records(
                            authority_record_handoff_id
                        )
                );
                '''
            )

    def ingest(
        self,
        plan: CaptureIngestionPlan | Mapping[str, Any],
        payloads_by_path: Mapping[str, bytes],
    ) -> CaptureIngestionCommitResult:
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

        staged_bindings: list[CaptureRawVisualMeshBinding] = []
        for handoff in typed.raw_visual_mesh_handoffs:
            staged_bindings.append(
                adapt_capture_mesh_handoff(
                    handoff,
                    payloads[handoff.geometry_path],
                )
            )

        plan_json = _canonical_json(
            typed.model_dump(mode='json', by_alias=True)
        )

        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                existing = connection.execute(
                    '''
                    SELECT plan_json
                    FROM capture_ingestion_runs
                    WHERE lineage_digest=?
                    ''',
                    (typed.lineage_digest,),
                ).fetchone()
                if existing is not None:
                    if existing['plan_json'] != plan_json:
                        raise CaptureIngestionTransactionError(
                            'existing lineage digest has different plan semantics'
                        )
                    connection.rollback()
                    return self._result(typed, created=False)

                connection.execute(
                    '''
                    INSERT INTO capture_ingestion_runs(
                        lineage_digest, bundle_digest, capture_revision_id,
                        ingestor_name, ingestor_version,
                        configuration_digest, plan_json, recorded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        typed.lineage_digest,
                        typed.bundle.bundle_digest,
                        typed.bundle.capture_revision_id,
                        typed.ingestor.name,
                        typed.ingestor.version,
                        typed.ingestor.configuration_digest,
                        plan_json,
                        datetime.now(timezone.utc).isoformat(),
                    ),
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
                            lineage_digest, source_evidence_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            typed.lineage_digest,
                            record.source_evidence_id,
                        ),
                    )

                for record in typed.roomplan_records:
                    connection.execute(
                        '''
                        INSERT INTO capture_roomplan_records(
                            lineage_digest, kind,
                            source_evidence_id, payload_json
                        ) VALUES (?, ?, ?, ?)
                        ''',
                        (
                            typed.lineage_digest,
                            record.kind,
                            record.source_evidence_id,
                            _canonical_json(
                                record.model_dump(mode='json')
                            ),
                        ),
                    )

                for binding in staged_bindings:
                    self._upsert_mesh_binding(connection, binding)
                    connection.execute(
                        '''
                        INSERT INTO capture_ingestion_mesh_links(
                            lineage_digest, binding_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            typed.lineage_digest,
                            binding.binding_id,
                        ),
                    )

                for record in typed.authority_records:
                    self._upsert_authority_record(connection, record)
                    connection.execute(
                        '''
                        INSERT INTO capture_ingestion_authority_links(
                            lineage_digest,
                            authority_record_handoff_id
                        ) VALUES (?, ?)
                        ''',
                        (
                            typed.lineage_digest,
                            record.authority_record_handoff_id,
                        ),
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self._result(typed, created=True)

    def get_ingestion(self, lineage_digest: str) -> CaptureIngestionPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT plan_json
                FROM capture_ingestion_runs
                WHERE lineage_digest=?
                ''',
                (lineage_digest,),
            ).fetchone()
        if row is None:
            return None
        return CaptureIngestionPlan.model_validate_json(row['plan_json'])

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
        payload = bytes(row['payload_blob'])
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
                SELECT payload_json
                FROM capture_raw_visual_mesh_bindings
                WHERE binding_id=?
                ''',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureRawVisualMeshBinding.model_validate_json(
            row['payload_json']
        )

    def get_authority_record(
        self,
        authority_record_handoff_id: str,
    ) -> CaptureAuthorityRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                '''
                SELECT payload_json
                FROM capture_authority_records
                WHERE authority_record_handoff_id=?
                ''',
                (authority_record_handoff_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureAuthorityRecord.model_validate_json(
            row['payload_json']
        )

    def source_evidence_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute(
                    'SELECT COUNT(*) FROM capture_source_evidence'
                ).fetchone()[0]
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
                or bytes(existing['payload_blob']) != payload
            ):
                raise CaptureIngestionTransactionError(
                    'source evidence identity already exists with different semantics'
                )
            return

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
                payload,
            ),
        )

    def _upsert_mesh_binding(
        self,
        connection: sqlite3.Connection,
        binding: CaptureRawVisualMeshBinding,
    ) -> None:
        payload_json = _canonical_json(
            binding.model_dump(mode='json', by_alias=True)
        )
        existing = connection.execute(
            '''
            SELECT payload_json
            FROM capture_raw_visual_mesh_bindings
            WHERE binding_id=?
            ''',
            (binding.binding_id,),
        ).fetchone()
        if existing is not None:
            if existing['payload_json'] != payload_json:
                raise CaptureIngestionTransactionError(
                    'raw mesh binding identity already exists with different semantics'
                )
            return
        connection.execute(
            '''
            INSERT INTO capture_raw_visual_mesh_bindings(
                binding_id, handoff_id, payload_json
            ) VALUES (?, ?, ?)
            ''',
            (
                binding.binding_id,
                binding.handoff.raw_visual_mesh_handoff_id,
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
    def _result(
        plan: CaptureIngestionPlan,
        *,
        created: bool,
    ) -> CaptureIngestionCommitResult:
        return CaptureIngestionCommitResult(
            lineage_digest=plan.lineage_digest,
            bundle_digest=plan.bundle.bundle_digest,
            source_evidence_count=len(plan.source_evidence),
            roomplan_record_count=len(plan.roomplan_records),
            raw_mesh_binding_count=len(plan.raw_visual_mesh_handoffs),
            authority_record_count=len(plan.authority_records),
            created=created,
        )
