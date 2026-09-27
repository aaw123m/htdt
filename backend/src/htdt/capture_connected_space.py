"""Connected-space promotion contract (issue #658).

Bridges a Capture ``ConnectedSpaceDocument`` (capture regions + topological
portals) into a durable HTDT ``PhysicalSpaceModel`` without flattening the
connected set into a single RoomPrism:

- capture regions promote to ``PhysicalSpaceRegion`` records that keep the
  capture kind (``stairwell``/``open_plan_area``/... never silently becomes
  a different physical class) plus an explicit workflow ``role``;
- portals promote to ``PhysicalSpacePortal`` records that start as topology
  only — an acoustic opening is *resolved* later by an explicit physical
  resolution step, never fabricated from topology;
- one shared Capture→Scene alignment authority binds the whole connected
  set (no per-segment best fit); a later change is an explicit recorded
  reconciliation;
- stair openings and open-plan merges stay first-class and reversible;
- acoustic compilation reads a readiness report that fails closed on
  partial geometry.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables
from .capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from .canonical_json import canonical_json as _canonical_json, canonical_sha256


CONNECTED_SPACE_SCHEMA = 'htdt.capture.connected-spaces'
CONNECTED_SPACE_SCHEMA_VERSION = '1.0.0'
CONNECTED_SPACE_PATH_SUFFIX = 'connected-spaces.json'

CONNECTED_DOC_DOMAIN = 'htdt.capture.connected-space-document.v1'
PHYSICAL_SPACE_DOMAIN = 'htdt.physical-space-model.v1'

UUID4_RE = (
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)


class ConnectedSpacePromotionError(ValueError):
    pass




def _hash(domain: str, payload: object) -> str:
    return canonical_sha256({'domain': domain, 'payload': payload})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(value: str, field: str) -> str:
    normalized = unicodedata.normalize('NFC', value).strip()
    if not normalized:
        raise ValueError(f'{field} must be non-empty')
    if len(normalized) > 240:
        raise ValueError(f'{field} exceeds 240 characters')
    return normalized


# ---------------------------------------------------------------------------
# wire model — mirrors HTDT-Capture's ConnectedSpaceDocument byte-for-byte
# ---------------------------------------------------------------------------

CaptureRegionKind = Literal[
    'room', 'open_plan_area', 'hallway', 'stairwell', 'alcove', 'other'
]
CaptureRegionState = Literal['active', 'completed']
CapturePortalKind = Literal[
    'doorway', 'open_passage', 'stair_opening', 'other'
]


class CaptureRegionSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    region_id: str = Field(pattern=UUID4_RE)
    label: str
    kind: CaptureRegionKind
    state: CaptureRegionState
    coordinate_space_id: str = Field(pattern=UUID4_RE)
    capture_session_id: str = Field(pattern=UUID4_RE)
    evidence_refs: tuple[str, ...] = ()
    revisit_count: int = Field(ge=0)

    @field_validator('label')
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return _label(value, 'region label')


class CaptureRegionPortal(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    portal_id: str = Field(pattern=UUID4_RE)
    region_a_id: str = Field(pattern=UUID4_RE)
    region_b_id: str = Field(pattern=UUID4_RE)
    kind: CapturePortalKind
    coordinate_space_id: str = Field(pattern=UUID4_RE)
    label: str = ''
    evidence_refs: tuple[str, ...] = ()

    @field_validator('label')
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = unicodedata.normalize('NFC', value).strip()
        if len(normalized) > 240:
            raise ValueError('portal label exceeds 240 characters')
        return normalized


class CaptureConnectedSpaceDocument(BaseModel):
    """Capture-side connected-space document (``session/connected-spaces.json``)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_: str = Field(default=CONNECTED_SPACE_SCHEMA, alias='schema')
    schema_version: str = Field(
        default=CONNECTED_SPACE_SCHEMA_VERSION, alias='schema_version'
    )
    capture_revision_id: str = Field(pattern=UUID4_RE)
    capture_session_id: str = Field(pattern=UUID4_RE)
    coordinate_space_id: str = Field(pattern=UUID4_RE)
    segments: tuple[CaptureRegionSegment, ...]
    portals: tuple[CaptureRegionPortal, ...] = ()

    model_config = ConfigDict(
        frozen=True, extra='forbid', populate_by_name=True
    )

    @model_validator(mode='after')
    def validate_document(self) -> 'CaptureConnectedSpaceDocument':
        if self.schema_ != CONNECTED_SPACE_SCHEMA:
            raise ValueError(
                f'connected space schema must be {CONNECTED_SPACE_SCHEMA}'
            )
        if self.schema_version != CONNECTED_SPACE_SCHEMA_VERSION:
            raise ValueError(
                'connected space schema_version must be '
                f'{CONNECTED_SPACE_SCHEMA_VERSION}'
            )
        if not self.segments:
            raise ValueError('connected space document has no segments')
        region_ids = [segment.region_id for segment in self.segments]
        if len(set(region_ids)) != len(region_ids):
            raise ValueError('duplicate capture region ids')
        portal_ids = [portal.portal_id for portal in self.portals]
        if len(set(portal_ids)) != len(portal_ids):
            raise ValueError('duplicate capture portal ids')
        for segment in self.segments:
            if segment.coordinate_space_id != self.coordinate_space_id:
                raise ValueError(
                    'segment outside the document coordinate space'
                )
        for portal in self.portals:
            if portal.coordinate_space_id != self.coordinate_space_id:
                raise ValueError(
                    'portal outside the document coordinate space'
                )
            if portal.region_a_id == portal.region_b_id:
                raise ValueError('portal endpoints must be distinct regions')
            if (
                portal.region_a_id not in region_ids
                or portal.region_b_id not in region_ids
            ):
                raise ValueError('portal endpoints must be known regions')
        return self

    @property
    def connected_document_id(self) -> str:
        return 'capture-connected-space:' + _hash(
            CONNECTED_DOC_DOMAIN, self.model_dump(mode='json', by_alias=True)
        )


# ---------------------------------------------------------------------------
# HTDT physical space model — the promoted, editable artifact
# ---------------------------------------------------------------------------

PhysicalRegionState = Literal[
    'candidate',
    'partially_bounded',
    'physically_resolved',
    'unresolved',
    'conflicting',
]

PhysicalRegionRole = Literal[
    'none',
    'primary_theater',
    'adjacent_acoustic_region',
    'adjacent_isolation_target',
    'support_equipment_area',
]

PortalGeometryState = Literal[
    'topology_known',
    'geometry_resolved',
    'acoustic_compiled',
]

PortalOpeningState = Literal['open', 'closed']

MembershipTargetKind = Literal['region', 'portal', 'global']


class PhysicalSpaceRegion(BaseModel):
    """One Capture region promoted into HTDT physical-space authority."""

    model_config = ConfigDict(frozen=True)

    physical_region_id: str = Field(pattern=r'^physical-region:[0-9a-f]{64}$')
    # raw capture region UUID for promoted regions; a '+'-joined lineage
    # list for regions produced by an explicit merge
    capture_region_id: str = Field(min_length=1)
    kind: CaptureRegionKind
    label: str = Field(min_length=1)
    state: PhysicalRegionState = 'candidate'
    role: PhysicalRegionRole = 'none'
    capture_state: CaptureRegionState
    evidence_refs: tuple[str, ...] = ()
    revisit_count: int = Field(ge=0)
    merged_from_region_ids: tuple[str, ...] = ()
    merged_into_region_id: str | None = None

    @property
    def preserves_vertical(self) -> bool:
        """Stairwell/double-height regions stay vertical, never 2D doorways."""
        return self.kind == 'stairwell'


class PhysicalSpacePortal(BaseModel):
    """One topological portal between physical regions.

    Topology (which regions connect) is separate from physical opening
    geometry (the resolved opening used by acoustic compilation) — a
    ``stair_opening`` stays vertical and is never flattened to a doorway.
    """

    model_config = ConfigDict(frozen=True)

    physical_portal_id: str = Field(pattern=r'^physical-portal:[0-9a-f]{64}$')
    capture_portal_id: str = Field(pattern=UUID4_RE)
    physical_region_a_id: str = Field(pattern=r'^physical-region:[0-9a-f]{64}$')
    physical_region_b_id: str = Field(pattern=r'^physical-region:[0-9a-f]{64}$')
    kind: CapturePortalKind
    label: str = ''
    evidence_refs: tuple[str, ...] = ()
    geometry_state: PortalGeometryState = 'topology_known'
    opening_state: PortalOpeningState = 'open'
    resolved_opening_id: str | None = None

    @property
    def preserves_vertical(self) -> bool:
        return self.kind == 'stair_opening'


class PhysicalSpaceMembership(BaseModel):
    """Entity/measurement region membership by semantic identity.

    Never point-in-polygon: membership is an explicit assignment to a
    physical region, a portal, or the global space.
    """

    model_config = ConfigDict(frozen=True)

    entity_ref: str = Field(min_length=1)
    target_kind: MembershipTargetKind
    target_id: str | None = None


class PhysicalSpaceModel(BaseModel):
    """One revision of a project's promoted connected physical space."""

    model_config = ConfigDict(frozen=True)

    physical_space_model_id: str = Field(
        pattern=r'^physical-space-model:[0-9a-f]{64}$'
    )
    document_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    parent_model_id: str | None = None
    source_connected_document_id: str = Field(min_length=1)
    capture_revision_id: str = Field(pattern=UUID4_RE)
    coordinate_space_id: str = Field(pattern=UUID4_RE)
    world_to_scene_authority_id: str | None = None
    regions: tuple[PhysicalSpaceRegion, ...]
    portals: tuple[PhysicalSpacePortal, ...] = ()
    memberships: tuple[PhysicalSpaceMembership, ...] = ()
    created_at_utc: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @property
    def simple_path(self) -> bool:
        """Single-region projects keep the trivial path."""
        return len(self.regions) == 1 and not self.portals

    @model_validator(mode='after')
    def validate_model(self) -> 'PhysicalSpaceModel':
        region_ids = [r.physical_region_id for r in self.regions]
        if len(set(region_ids)) != len(region_ids):
            raise ValueError('duplicate physical region ids')
        if not self.regions:
            raise ValueError('physical space model has no regions')
        live_ids = {
            r.physical_region_id for r in self.regions if r.merged_into_region_id is None
        }
        known_ids = set(region_ids)
        for region in self.regions:
            if (
                region.merged_into_region_id is not None
                and region.merged_into_region_id not in known_ids
            ):
                raise ValueError('merge target is not a known region')
        for portal in self.portals:
            if (
                portal.physical_region_a_id not in live_ids
                or portal.physical_region_b_id not in live_ids
            ):
                raise ValueError(
                    'portal endpoints must be live (unmerged) regions'
                )
            if (
                portal.geometry_state != 'topology_known'
                and portal.resolved_opening_id is None
            ):
                raise ValueError(
                    'resolved/compiled portal requires a resolved opening id'
                )
        for membership in self.memberships:
            if membership.target_kind == 'global':
                if membership.target_id is not None:
                    raise ValueError('global membership carries no target id')
            elif membership.target_id is None:
                raise ValueError('membership requires a target id')
            elif membership.target_kind == 'region' and (
                membership.target_id not in live_ids
            ):
                raise ValueError('membership targets a live region')
            elif membership.target_kind == 'portal' and membership.target_id not in {
                p.physical_portal_id for p in self.portals
            }:
                raise ValueError('membership targets a known portal')
        return self


class PhysicalSpaceRegionReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    physical_region_id: str
    kind: CaptureRegionKind
    role: PhysicalRegionRole
    state: PhysicalRegionState
    geometry_available: bool
    solver_compatible: bool


class PhysicalSpacePortalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    physical_portal_id: str
    kind: CapturePortalKind
    geometry_state: PortalGeometryState
    opening_state: PortalOpeningState
    topology_only: bool
    preserves_vertical: bool


class PhysicalSpaceCompilationReport(BaseModel):
    """Readiness report an acoustic compilation consumes.

    Compilation fails closed on partial geometry: any region without
    resolved geometry, or any portal that is still topology-only, blocks
    connected-space compilation and is named here.
    """

    model_config = ConfigDict(frozen=True)

    physical_space_model_id: str
    document_id: str
    revision: int
    compilable: bool
    blocked_reasons: tuple[str, ...]
    regions: tuple[PhysicalSpaceRegionReport, ...]
    portals: tuple[PhysicalSpacePortalReport, ...]


class ConnectedSpaceStagedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    connected_document_id: str
    lineage_digest: str
    document: CaptureConnectedSpaceDocument
    staged_at_utc: str
    created: bool


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


def _region_id(connected_document_id: str, capture_region_id: str) -> str:
    return 'physical-region:' + _hash(
        PHYSICAL_SPACE_DOMAIN,
        {
            'kind': 'region',
            'connected_document_id': connected_document_id,
            'capture_region_id': capture_region_id,
        },
    )


def _portal_id(connected_document_id: str, capture_portal_id: str) -> str:
    return 'physical-portal:' + _hash(
        PHYSICAL_SPACE_DOMAIN,
        {
            'kind': 'portal',
            'connected_document_id': connected_document_id,
            'capture_portal_id': capture_portal_id,
        },
    )


def _model_id(model: PhysicalSpaceModel) -> str:
    payload = model.model_dump(mode='json')
    payload.pop('physical_space_model_id')
    payload.pop('created_at_utc')
    return 'physical-space-model:' + _hash(PHYSICAL_SPACE_DOMAIN, payload)


class ConnectedSpacePromotionRepository:
    """Persists staged connected-space documents and physical space models."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        ingestion_repository: CaptureIngestionRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.ingestion_repository = ingestion_repository or (
            CaptureIngestionRepository(scene_repository)
        )
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
            require_native_tables(
                connection,
                'capture_ingestion_lineages',
                'capture_connected_space_documents',
                'physical_space_models',
            )

    def _converge_schema(self, connection: sqlite3.Connection) -> None:
        """Legacy-shape tail of the schema-authority migration (#302).

        Plain ``CREATE TABLE`` lives in ``cad_schema_ddl`` and runs inside
        the versioned migration; this sequence converges databases whose
        persisted shapes predate the canonical contract (the lineage
        foreign-key repoint rebuilds the documents table) and installs
        the connected-space tables so every supported open path converges
        the same way.
        """
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS capture_ingestion_lineages (
                lineage_digest TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS capture_connected_space_documents (
                connected_document_id TEXT PRIMARY KEY,
                lineage_digest TEXT NOT NULL
                    REFERENCES capture_ingestion_lineages(lineage_digest),
                document_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                staged_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_connected_doc_lineage
            ON capture_connected_space_documents(lineage_digest);

            CREATE TABLE IF NOT EXISTS physical_space_models (
                physical_space_model_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                parent_model_id TEXT,
                source_connected_document_id TEXT NOT NULL
                    REFERENCES capture_connected_space_documents(
                        connected_document_id
                    ),
                world_to_scene_authority_id TEXT,
                payload_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                reason TEXT NOT NULL,
                UNIQUE(document_id, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_physical_space_document
            ON physical_space_models(document_id);
            '''
        )
        self._repoint_documents_lineage_parent(connection)
        # The repoint rebuild drops and recreates the documents
        # table; re-install its index after.
        connection.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_connected_doc_lineage
            ON capture_connected_space_documents(lineage_digest)
            '''
        )

    def _repoint_documents_lineage_parent(
        self, connection: sqlite3.Connection
    ) -> None:
        """Retarget the documents table's lineage foreign key.

        #413 demoted ``capture_ingestion_runs.lineage_digest`` from the
        runs primary key to a non-unique projection (several processing
        runs may share one lineage), so it can no longer parent a foreign
        key. The shared ``capture_ingestion_lineages`` table is the unique
        lineage parent; rebuilds this table when its stored key still
        targets the runs table or the dropped migration rename.
        """
        # Seed lineage rows from whatever run shape is present so child
        # rows retain a valid parent; needed even when no rebuild runs,
        # since a database opened before this contract may hold runs its
        # lineage table never knew about.
        run_tables = {
            str(row['name'])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in ('capture_ingestion_runs',
                     'capture_ingestion_runs_legacy'):
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
            for row in connection.execute(
                'PRAGMA foreign_key_list('
                'capture_connected_space_documents)'
            )
            if str(row['from']) == 'lineage_digest'
        }
        if parents == {'capture_ingestion_lineages'}:
            return
        if connection.in_transaction:
            connection.commit()
        # foreign_keys must be OFF during the rebuild: renaming the table
        # otherwise rewrites references in physical_space_models to the
        # dropped legacy name.
        connection.execute('PRAGMA foreign_keys=OFF')
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''
                ALTER TABLE capture_connected_space_documents
                RENAME TO capture_connected_space_documents_legacy
                '''
            )
            connection.execute(
                '''
                CREATE TABLE capture_connected_space_documents (
                    connected_document_id TEXT PRIMARY KEY,
                    lineage_digest TEXT NOT NULL
                        REFERENCES capture_ingestion_lineages(lineage_digest),
                    document_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    staged_at_utc TEXT NOT NULL
                )
                '''
            )
            connection.execute(
                '''
                INSERT INTO capture_connected_space_documents(
                    connected_document_id, lineage_digest,
                    document_sha256, payload_json, staged_at_utc
                )
                SELECT connected_document_id, lineage_digest,
                    document_sha256, payload_json, staged_at_utc
                FROM capture_connected_space_documents_legacy
                '''
            )
            connection.execute(
                'DROP TABLE capture_connected_space_documents_legacy'
            )
            orphans = connection.execute(
                'PRAGMA foreign_key_check('
                'capture_connected_space_documents)'
            ).fetchall()
            if orphans:
                connection.rollback()
                raise ConnectedSpacePromotionError(
                    'cannot retarget connected space documents to the '
                    'lineages table: unresolved foreign keys remain'
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute('PRAGMA foreign_keys=ON')

    # ------------------------------------------------------------------
    # staging
    # ------------------------------------------------------------------

    def stage_connected_document(
        self,
        plan: CaptureIngestionPlan,
        payload: bytes,
    ) -> ConnectedSpaceStagedDocument:
        """Stage a ``session/connected-spaces.json`` payload for a delivery."""
        try:
            document = CaptureConnectedSpaceDocument.model_validate_json(payload)
        except ValueError as exc:
            raise ConnectedSpacePromotionError(
                f'connected space document failed validation: {exc}'
            ) from exc
        bundle = plan.bundle
        if document.capture_revision_id != bundle.capture_revision_id:
            raise ConnectedSpacePromotionError(
                'connected space document belongs to a different capture '
                'revision than the staged bundle'
            )
        if document.coordinate_space_id not in bundle.coordinate_space_ids:
            raise ConnectedSpacePromotionError(
                'connected space document coordinate space is not bound '
                'by the staged bundle'
            )
        if document.capture_session_id not in bundle.capture_session_ids:
            raise ConnectedSpacePromotionError(
                'connected space document session is not bound by the '
                'staged bundle'
            )
        connected_document_id = document.connected_document_id
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                ingested = connection.execute(
                    'SELECT lineage_digest FROM capture_ingestion_runs '
                    'WHERE lineage_digest=?',
                    (plan.lineage_digest,),
                ).fetchone()
                if ingested is None:
                    raise ConnectedSpacePromotionError(
                        'connected space documents stage only persisted '
                        'ingestions; ingest the bundle first'
                    )
                existing = connection.execute(
                    'SELECT document_sha256, payload_json, staged_at_utc '
                    'FROM capture_connected_space_documents '
                    'WHERE connected_document_id=?',
                    (connected_document_id,),
                ).fetchone()
                if existing is not None:
                    if existing['document_sha256'] != sha256(payload).hexdigest():
                        raise ConnectedSpacePromotionError(
                            'connected document identity collision with '
                            'different payload bytes'
                        )
                    if (
                        json.loads(existing['payload_json'])
                        != document.model_dump(mode='json', by_alias=True)
                    ):
                        raise ConnectedSpacePromotionError(
                            'persisted connected document payload diverged'
                        )
                    connection.commit()
                    return ConnectedSpaceStagedDocument(
                        connected_document_id=connected_document_id,
                        lineage_digest=plan.lineage_digest,
                        document=document,
                        staged_at_utc=existing['staged_at_utc'],
                        created=False,
                    )
                connection.execute(
                    '''
                    INSERT INTO capture_connected_space_documents(
                        connected_document_id, lineage_digest,
                        document_sha256, payload_json, staged_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    ''',
                    (
                        connected_document_id,
                        plan.lineage_digest,
                        sha256(payload).hexdigest(),
                        _canonical_json(
                            document.model_dump(mode='json', by_alias=True)
                        ),
                        _utc_now(),
                    ),
                )
                staged_at = connection.execute(
                    'SELECT staged_at_utc FROM capture_connected_space_documents '
                    'WHERE connected_document_id=?',
                    (connected_document_id,),
                ).fetchone()['staged_at_utc']
                connection.commit()
                return ConnectedSpaceStagedDocument(
                    connected_document_id=connected_document_id,
                    lineage_digest=plan.lineage_digest,
                    document=document,
                    staged_at_utc=staged_at,
                    created=True,
                )
            except Exception:
                connection.rollback()
                raise

    def get_connected_document(
        self, connected_document_id: str
    ) -> CaptureConnectedSpaceDocument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM capture_connected_space_documents '
                'WHERE connected_document_id=?',
                (connected_document_id,),
            ).fetchone()
        if row is None:
            return None
        return CaptureConnectedSpaceDocument.model_validate(
            json.loads(row['payload_json'])
        )

    def connected_documents_for_lineage(
        self, lineage_digest: str
    ) -> tuple[ConnectedSpaceStagedDocument, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM capture_connected_space_documents '
                'WHERE lineage_digest=? ORDER BY connected_document_id',
                (lineage_digest,),
            ).fetchall()
        return tuple(
            ConnectedSpaceStagedDocument(
                connected_document_id=row['connected_document_id'],
                lineage_digest=row['lineage_digest'],
                document=CaptureConnectedSpaceDocument.model_validate(
                    json.loads(row['payload_json'])
                ),
                staged_at_utc=row['staged_at_utc'],
                created=False,
            )
            for row in rows
        )

    def connected_space_summary(self, lineage_digest: str) -> dict:
        """Human-readable summary the Capture Inbox surfaces (#589 hook)."""
        documents = self.connected_documents_for_lineage(lineage_digest)
        if not documents:
            return {'has_connected_space_document': False}
        segments = sum(len(d.document.segments) for d in documents)
        portals = sum(len(d.document.portals) for d in documents)
        topology_only = sum(
            1
            for d in documents
            for portal in d.document.portals
        )
        return {
            'has_connected_space_document': True,
            'document_count': len(documents),
            'region_count': segments,
            'portal_count': portals,
            'topology_only_portals': topology_only,
            'single_region': segments == 1 and portals == 0,
        }

    # ------------------------------------------------------------------
    # promotion
    # ------------------------------------------------------------------

    def promote_connected_space(
        self,
        *,
        document_id: str,
        connected_document_id: str,
        world_to_scene_authority_id: str | None = None,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Promote a staged connected-space document into a project.

        Exactly one shared Capture→Scene alignment authority binds the
        whole connected set; there is no per-segment best fit. The first
        segment is never assumed to be the primary theater.
        """
        if not reason:
            raise ConnectedSpacePromotionError('promotion requires a reason')
        document = self.get_connected_document(connected_document_id)
        if document is None:
            raise ConnectedSpacePromotionError('unknown connected document')
        with closing(self._connect()) as connection:
            latest = connection.execute(
                'SELECT revision FROM physical_space_models WHERE document_id=? '
                'ORDER BY revision DESC LIMIT 1',
                (document_id,),
            ).fetchone()
            if latest is not None:
                raise ConnectedSpacePromotionError(
                    'document already has a physical space model; partial '
                    'recapture goes through reconcile_connected_space'
                )
        regions = tuple(
            PhysicalSpaceRegion(
                physical_region_id=_region_id(
                    connected_document_id, segment.region_id
                ),
                capture_region_id=segment.region_id,
                kind=segment.kind,
                label=segment.label,
                state='candidate',
                role='none',
                capture_state=segment.state,
                evidence_refs=segment.evidence_refs,
                revisit_count=segment.revisit_count,
            )
            for segment in document.segments
        )
        by_capture_id = {
            region.capture_region_id: region.physical_region_id
            for region in regions
        }
        portals = tuple(
            PhysicalSpacePortal(
                physical_portal_id=_portal_id(
                    connected_document_id, portal.portal_id
                ),
                capture_portal_id=portal.portal_id,
                physical_region_a_id=by_capture_id[portal.region_a_id],
                physical_region_b_id=by_capture_id[portal.region_b_id],
                kind=portal.kind,
                label=portal.label,
                evidence_refs=portal.evidence_refs,
            )
            for portal in document.portals
        )
        draft = PhysicalSpaceModel(
            physical_space_model_id='physical-space-model:' + '0' * 64,
            document_id=document_id,
            revision=1,
            parent_model_id=None,
            source_connected_document_id=connected_document_id,
            capture_revision_id=document.capture_revision_id,
            coordinate_space_id=document.coordinate_space_id,
            world_to_scene_authority_id=world_to_scene_authority_id,
            regions=regions,
            portals=portals,
            memberships=(),
            created_at_utc=_utc_now(),
            reason=reason,
        )
        model = draft.model_copy(
            update={'physical_space_model_id': _model_id(draft)}
        )
        return self._persist_model(connection=None, model=model)

    def _persist_model(
        self,
        connection: sqlite3.Connection | None,
        model: PhysicalSpaceModel,
    ) -> PhysicalSpaceModel:
        owns = connection is None
        if owns:
            connection = self._connect()
        assert connection is not None
        try:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT physical_space_model_id, payload_json '
                'FROM physical_space_models WHERE physical_space_model_id=?',
                (model.physical_space_model_id,),
            ).fetchone()
            if existing is not None:
                persisted = PhysicalSpaceModel.model_validate(
                    json.loads(existing['payload_json'])
                )
                comparable = persisted.model_dump(mode='json')
                comparable['created_at_utc'] = model.created_at_utc
                if comparable != model.model_dump(mode='json'):
                    raise ConnectedSpacePromotionError(
                        'physical space model identity collision'
                    )
                connection.commit()
                return persisted
            connection.execute(
                '''
                INSERT INTO physical_space_models(
                    physical_space_model_id, document_id, revision,
                    parent_model_id, source_connected_document_id,
                    world_to_scene_authority_id, payload_json,
                    created_at_utc, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    model.physical_space_model_id,
                    model.document_id,
                    model.revision,
                    model.parent_model_id,
                    model.source_connected_document_id,
                    model.world_to_scene_authority_id,
                    _canonical_json(model.model_dump(mode='json')),
                    model.created_at_utc,
                    model.reason,
                ),
            )
            connection.commit()
            return model
        except Exception:
            connection.rollback()
            raise
        finally:
            if owns:
                connection.close()

    def get_model(
        self, physical_space_model_id: str
    ) -> PhysicalSpaceModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM physical_space_models '
                'WHERE physical_space_model_id=?',
                (physical_space_model_id,),
            ).fetchone()
        if row is None:
            return None
        return PhysicalSpaceModel.model_validate(json.loads(row['payload_json']))

    def latest_model(self, document_id: str) -> PhysicalSpaceModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM physical_space_models '
                'WHERE document_id=? ORDER BY revision DESC LIMIT 1',
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return PhysicalSpaceModel.model_validate(json.loads(row['payload_json']))

    def model_history(self, document_id: str) -> tuple[PhysicalSpaceModel, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM physical_space_models '
                'WHERE document_id=? ORDER BY revision',
                (document_id,),
            ).fetchall()
        return tuple(
            PhysicalSpaceModel.model_validate(json.loads(row['payload_json']))
            for row in rows
        )

    def _next_revision(
        self,
        current: PhysicalSpaceModel,
        *,
        regions: tuple[PhysicalSpaceRegion, ...] | None = None,
        portals: tuple[PhysicalSpacePortal, ...] | None = None,
        memberships: tuple[PhysicalSpaceMembership, ...] | None = None,
        world_to_scene_authority_id: str | None | Literal['__keep__'] = '__keep__',
        reason: str,
    ) -> PhysicalSpaceModel:
        if not reason:
            raise ConnectedSpacePromotionError('revision requires a reason')
        draft = current.model_copy(
            update={
                'physical_space_model_id': 'physical-space-model:' + '0' * 64,
                'revision': current.revision + 1,
                'parent_model_id': current.physical_space_model_id,
                'regions': regions if regions is not None else current.regions,
                'portals': portals if portals is not None else current.portals,
                'memberships': (
                    memberships if memberships is not None else current.memberships
                ),
                'world_to_scene_authority_id': (
                    current.world_to_scene_authority_id
                    if world_to_scene_authority_id == '__keep__'
                    else world_to_scene_authority_id
                ),
                'created_at_utc': _utc_now(),
                'reason': reason,
            }
        )
        # model_copy skips validators — re-validate so every persisted
        # revision satisfies the same invariants as a fresh promotion
        draft = PhysicalSpaceModel.model_validate(
            draft.model_dump(mode='python')
        )
        model = draft.model_copy(
            update={'physical_space_model_id': _model_id(draft)}
        )
        return self._persist_model(connection=None, model=model)

    # ------------------------------------------------------------------
    # explicit model edits — each produces a new persisted revision
    # ------------------------------------------------------------------

    def set_region_role(
        self,
        physical_space_model_id: str,
        physical_region_id: str,
        role: PhysicalRegionRole,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Set a workflow role. Role is workflow truth, never geometry truth."""
        model = self._require_model(physical_space_model_id)
        if not any(
            r.physical_region_id == physical_region_id
            for r in model.regions
            if r.merged_into_region_id is None
        ):
            raise ConnectedSpacePromotionError('unknown live region')
        regions = tuple(
            r.model_copy(update={'role': role})
            if r.physical_region_id == physical_region_id
            else r
            for r in model.regions
        )
        return self._next_revision(
            model, regions=regions, reason=reason
        )

    def set_region_state(
        self,
        physical_space_model_id: str,
        physical_region_id: str,
        state: PhysicalRegionState,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        model = self._require_model(physical_space_model_id)
        if not any(
            r.physical_region_id == physical_region_id for r in model.regions
        ):
            raise ConnectedSpacePromotionError('unknown region')
        regions = tuple(
            r.model_copy(update={'state': state})
            if r.physical_region_id == physical_region_id
            else r
            for r in model.regions
        )
        return self._next_revision(model, regions=regions, reason=reason)

    def resolve_portal_geometry(
        self,
        physical_space_model_id: str,
        physical_portal_id: str,
        resolved_opening_id: str,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Attach resolved physical opening geometry to a portal.

        Topology alone never fabricates dimensions: the opening identity is
        an explicit typed reference supplied by the operator, never a label.
        """
        if not resolved_opening_id:
            raise ConnectedSpacePromotionError(
                'portal resolution requires an explicit opening id'
            )
        model = self._require_model(physical_space_model_id)
        if not any(
            p.physical_portal_id == physical_portal_id for p in model.portals
        ):
            raise ConnectedSpacePromotionError('unknown portal')
        portals = tuple(
            p.model_copy(
                update={
                    'geometry_state': 'geometry_resolved',
                    'resolved_opening_id': resolved_opening_id,
                }
            )
            if p.physical_portal_id == physical_portal_id
            else p
            for p in model.portals
        )
        return self._next_revision(model, portals=portals, reason=reason)

    def set_portal_opening_state(
        self,
        physical_space_model_id: str,
        physical_portal_id: str,
        opening_state: PortalOpeningState,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Operating state composes with geometry: an open doorway may still
        compile as one acoustic space; closed it is treated as two."""
        model = self._require_model(physical_space_model_id)
        if not any(
            p.physical_portal_id == physical_portal_id for p in model.portals
        ):
            raise ConnectedSpacePromotionError('unknown portal')
        portals = tuple(
            p.model_copy(update={'opening_state': opening_state})
            if p.physical_portal_id == physical_portal_id
            else p
            for p in model.portals
        )
        return self._next_revision(model, portals=portals, reason=reason)

    def assign_membership(
        self,
        physical_space_model_id: str,
        entity_ref: str,
        target_kind: MembershipTargetKind,
        target_id: str | None = None,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Assign entity/measurement membership by semantic identity."""
        model = self._require_model(physical_space_model_id)
        membership = PhysicalSpaceMembership(
            entity_ref=entity_ref,
            target_kind=target_kind,
            target_id=target_id,
        )
        # validate via a draft model so target existence checks run once
        draft = model.model_copy(
            update={
                'memberships': tuple(
                    m
                    for m in model.memberships
                    if m.entity_ref != entity_ref
                )
                + (membership,)
            }
        )
        return self._next_revision(
            model, memberships=draft.memberships, reason=reason
        )

    def merge_regions(
        self,
        physical_space_model_id: str,
        source_region_ids: tuple[str, ...],
        *,
        kind: CaptureRegionKind = 'open_plan_area',
        label: str | None = None,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Explicit, reversible merge of regions (open-plan reinterpretation).

        The merged region records its lineage via ``merged_from_region_ids``;
        the originals stay in the model with a ``merged_into`` pointer, so a
        later revision can unmerge them.
        """
        model = self._require_model(physical_space_model_id)
        live = {r.physical_region_id: r for r in model.regions if r.merged_into_region_id is None}
        missing = [rid for rid in source_region_ids if rid not in live]
        if len(source_region_ids) < 2 or missing:
            raise ConnectedSpacePromotionError(
                f'merge requires 2+ live regions; missing: {missing}'
            )
        merged_label = label or ' / '.join(
            live[rid].label for rid in source_region_ids
        )
        merged_capture_id = '+'.join(sorted(source_region_ids))
        merged = PhysicalSpaceRegion(
            physical_region_id='physical-region:' + _hash(
                PHYSICAL_SPACE_DOMAIN,
                {
                    'kind': 'merged-region',
                    'model': physical_space_model_id,
                    'sources': sorted(source_region_ids),
                    'revision': model.revision + 1,
                },
            ),
            capture_region_id=merged_capture_id,
            kind=kind,
            label=merged_label,
            state='candidate',
            role='none',
            capture_state='completed',
            evidence_refs=tuple(
                dict.fromkeys(
                    ref
                    for rid in source_region_ids
                    for ref in live[rid].evidence_refs
                )
            ),
            revisit_count=sum(live[rid].revisit_count for rid in source_region_ids),
            merged_from_region_ids=tuple(sorted(source_region_ids)),
        )
        regions = tuple(
            r.model_copy(update={'merged_into_region_id': merged.physical_region_id})
            if r.physical_region_id in source_region_ids
            else r
            for r in model.regions
        ) + (merged,)
        # memberships follow the merge so entities stay attached
        memberships = tuple(
            m.model_copy(update={'target_id': merged.physical_region_id})
            if m.target_kind == 'region' and m.target_id in source_region_ids
            else m
            for m in model.memberships
        )
        # portals internal to the merge disappear; portals crossing the
        # merge boundary are retargeted to the merged region
        portals = []
        for portal in model.portals:
            a_in = portal.physical_region_a_id in source_region_ids
            b_in = portal.physical_region_b_id in source_region_ids
            if a_in and b_in:
                continue  # internal portal absorbed by the merge
            portals.append(
                portal.model_copy(
                    update={
                        'physical_region_a_id': (
                            merged.physical_region_id
                            if a_in
                            else portal.physical_region_a_id
                        ),
                        'physical_region_b_id': (
                            merged.physical_region_id
                            if b_in
                            else portal.physical_region_b_id
                        ),
                    }
                )
            )
        return self._next_revision(
            model,
            regions=regions,
            portals=tuple(portals),
            memberships=memberships,
            reason=reason,
        )

    def unmerge_region(
        self,
        physical_space_model_id: str,
        merged_region_id: str,
        *,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Reverse a merge: originals return to live, merged region retires."""
        model = self._require_model(physical_space_model_id)
        merged = next(
            (r for r in model.regions if r.physical_region_id == merged_region_id),
            None,
        )
        if merged is None or not merged.merged_from_region_ids:
            raise ConnectedSpacePromotionError('not a merged region')
        sources = set(merged.merged_from_region_ids)
        regions = tuple(
            r.model_copy(update={'merged_into_region_id': None})
            if r.physical_region_id in sources
            else r
            for r in model.regions
            if r.physical_region_id != merged_region_id
        )
        memberships = tuple(
            m.model_copy(update={'target_id': merged.merged_from_region_ids[0]})
            if m.target_kind == 'region' and m.target_id == merged_region_id
            else m
            for m in model.memberships
        )
        # portals retargeted at merge time cannot be reconstructed —
        # reverse-merge restores region identity; portals dropped at merge
        # stay dropped unless a later recapture reintroduces them
        portals = tuple(
            portal.model_copy(
                update={
                    'physical_region_a_id': (
                        merged.merged_from_region_ids[0]
                        if portal.physical_region_a_id == merged_region_id
                        else portal.physical_region_a_id
                    ),
                    'physical_region_b_id': (
                        merged.merged_from_region_ids[0]
                        if portal.physical_region_b_id == merged_region_id
                        else portal.physical_region_b_id
                    ),
                }
            )
            for portal in model.portals
        )
        # validate endpoint liveness before persisting
        live_ids = {r.physical_region_id for r in regions if r.merged_into_region_id is None}
        portals = tuple(
            portal
            for portal in portals
            if portal.physical_region_a_id in live_ids
            and portal.physical_region_b_id in live_ids
            and portal.physical_region_a_id != portal.physical_region_b_id
        )
        return self._next_revision(
            model,
            regions=regions,
            portals=portals,
            memberships=memberships,
            reason=reason,
        )

    def reconcile_connected_space(
        self,
        physical_space_model_id: str,
        new_connected_document_id: str,
        *,
        world_to_scene_authority_id: str | None = None,
        reason: str,
    ) -> PhysicalSpaceModel:
        """Apply a partial recapture without replacing unrelated regions.

        Regions/portals matched by exact capture identity update evidence
        and revisit counts; new captures append; regions absent from the
        recapture are preserved untouched — a hallway refresh never
        rewrites the theater.
        """
        model = self._require_model(physical_space_model_id)
        document = self.get_connected_document(new_connected_document_id)
        if document is None:
            raise ConnectedSpacePromotionError('unknown connected document')
        if document.coordinate_space_id != model.coordinate_space_id:
            raise ConnectedSpacePromotionError(
                'recapture must share the model coordinate space; a new '
                'space requires its own model'
            )

        regions = list(model.regions)
        live_by_capture = {
            r.capture_region_id: i
            for i, r in enumerate(regions)
            if r.merged_into_region_id is None and '+' not in r.capture_region_id
        }
        for segment in document.segments:
            idx = live_by_capture.get(segment.region_id)
            if idx is not None:
                regions[idx] = regions[idx].model_copy(
                    update={
                        'capture_state': segment.state,
                        'evidence_refs': segment.evidence_refs,
                        'revisit_count': segment.revisit_count,
                    }
                )
            else:
                regions.append(
                    PhysicalSpaceRegion(
                        physical_region_id=_region_id(
                            new_connected_document_id, segment.region_id
                        ),
                        capture_region_id=segment.region_id,
                        kind=segment.kind,
                        label=segment.label,
                        state='candidate',
                        role='none',
                        capture_state=segment.state,
                        evidence_refs=segment.evidence_refs,
                        revisit_count=segment.revisit_count,
                    )
                )
        # refresh live index with appended regions before portals resolve
        live_by_capture = {
            r.capture_region_id: i
            for i, r in enumerate(regions)
            if r.merged_into_region_id is None and '+' not in r.capture_region_id
        }
        known_portal_captures = {
            p.capture_portal_id for p in model.portals
        }
        portals = list(model.portals)
        for portal in document.portals:
            if portal.capture_portal_id in known_portal_captures:
                portals = [
                    p.model_copy(
                        update={'evidence_refs': portal.evidence_refs}
                    )
                    if p.capture_portal_id == portal.capture_portal_id
                    else p
                    for p in portals
                ]
            else:
                a = live_by_capture.get(portal.region_a_id)
                b = live_by_capture.get(portal.region_b_id)
                if a is None or b is None:
                    raise ConnectedSpacePromotionError(
                        'recapture portal references a region outside the '
                        'model; reconcile regions first'
                    )
                portals.append(
                    PhysicalSpacePortal(
                        physical_portal_id=_portal_id(
                            new_connected_document_id, portal.portal_id
                        ),
                        capture_portal_id=portal.capture_portal_id,
                        physical_region_a_id=regions[a].physical_region_id,
                        physical_region_b_id=regions[b].physical_region_id,
                        kind=portal.kind,
                        label=portal.label,
                        evidence_refs=portal.evidence_refs,
                    )
                )
        update: dict[str, object] = {
            'regions': tuple(regions),
            'portals': tuple(portals),
        }
        if world_to_scene_authority_id is not None:
            update['world_to_scene_authority_id'] = world_to_scene_authority_id
        return self._next_revision(model, reason=reason, **update)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def compilation_report(
        self, physical_space_model_id: str
    ) -> PhysicalSpaceCompilationReport:
        """Readiness report — compilation fails closed on partial geometry."""
        model = self._require_model(physical_space_model_id)
        live_regions = [
            r for r in model.regions if r.merged_into_region_id is None
        ]
        region_reports = tuple(
            PhysicalSpaceRegionReport(
                physical_region_id=r.physical_region_id,
                kind=r.kind,
                role=r.role,
                state=r.state,
                geometry_available=r.state == 'physically_resolved',
                solver_compatible=r.state in (
                    'physically_resolved',
                    'partially_bounded',
                ),
            )
            for r in live_regions
        )
        portal_reports = tuple(
            PhysicalSpacePortalReport(
                physical_portal_id=p.physical_portal_id,
                kind=p.kind,
                geometry_state=p.geometry_state,
                opening_state=p.opening_state,
                topology_only=p.geometry_state == 'topology_known',
                preserves_vertical=p.preserves_vertical,
            )
            for p in model.portals
        )
        blocked = [
            f'region {r.label!r} has no resolved geometry ({r.state})'
            for r in live_regions
            if r.state not in ('physically_resolved', 'partially_bounded')
        ]
        blocked += [
            f'portal {p.label or p.physical_portal_id} is topology-only'
            for p in model.portals
            if p.geometry_state == 'topology_known'
            and p.opening_state == 'open'
        ]
        blocked += [
            f'region {r.label!r} is in a conflicting state'
            for r in live_regions
            if r.state == 'conflicting'
        ]
        return PhysicalSpaceCompilationReport(
            physical_space_model_id=model.physical_space_model_id,
            document_id=model.document_id,
            revision=model.revision,
            compilable=not blocked,
            blocked_reasons=tuple(blocked),
            regions=region_reports,
            portals=portal_reports,
        )

    def _require_model(self, physical_space_model_id: str) -> PhysicalSpaceModel:
        model = self.get_model(physical_space_model_id)
        if model is None:
            raise ConnectedSpacePromotionError('unknown physical space model')
        return model


def run_connected_space_schema_convergence(
    connection: sqlite3.Connection,
) -> None:
    """Legacy-shape tail of the schema-authority migration (#302).

    ``ensure_native_schema`` invokes this while converging databases whose
    connected-space documents table still points its lineage key at the
    runs table; it runs the same sequence
    ``ConnectedSpacePromotionRepository._initialize`` applies, without
    constructing a repository instance.
    """

    repository = ConnectedSpacePromotionRepository.__new__(
        ConnectedSpacePromotionRepository
    )
    repository._converge_schema(connection)
