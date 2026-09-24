"""Capture Inbox: staged, project-scoped intake for HTDT-Capture deliveries.

Issue #589. The Inbox receives validated ``CaptureIngestionPlan`` records
(file import or the paired receiver — both funnel through the same staging
entry point), classifies each delivery against the capture revision *graph*
(series is a graph, not a total order — out-of-order arrival is legal),
exposes read-only inspection and pairwise comparison, and applies promotion
granularly per authority kind. Nothing here mutates ``SceneDocument`` or any
scene-head CAS: promotion produces authority records whose ids are stored
back on the item.

Gate facets (bundle validation, dependencies, alignment, evidence
conflicts, disposition) are evaluated and stored independently — they are
never collapsed into a single badge, and network receipt never counts as
gates passed.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import ensure_native_schema, require_native_tables
from .capture_ingestion_transaction import (
    CaptureIngestionPlan,
    CaptureIngestionRepository,
)
from .capture_semantic_promotion import (
    CaptureAlignmentMethod,
    CaptureAlignmentScalePolicy,
    CaptureWorldToSceneAuthority,
    validate_capture_alignment,
)
from .semantic_geometry import SemanticCoordinateTransform


INBOX_ITEM_DOMAIN = 'htdt.capture.inbox-item.v1'
INBOX_PROMOTION_DOMAIN = 'htdt.capture.inbox-promotion.v1'
INBOX_SUPERSESSION_DOMAIN = 'htdt.capture.inbox-supersession.v1'
INBOX_REGISTRATION_DOMAIN = 'htdt.capture.cross-revision-registration.v1'

# Deliveries that arrive without an explicit project destination land in the
# shared inbox scope; the receipt names this destination so no routing is
# guessed. Promotion out of this scope still requires an explicit operator
# assign to a document.
CAPTURE_INBOX_UNASSIGNED_SCOPE = 'capture-inbox-unassigned'


class CaptureInboxError(ValueError):
    pass


InboxDisposition = Literal[
    'pending',
    'deferred',
    'partially_promoted',
    'promoted',
    'rejected',
    'superseded',
]

# Revision-graph arrival classification. Multiple flags may apply; the
# primary classification is the highest-priority one and is what the
# listing groups by.
InboxClassification = Literal[
    'exact_duplicate',
    'identity_digest_conflict',
    'revision_variant',
    'fills_missing_predecessor',
    'extends_known_head',
    'continues_branch',
    'parallel_branch_head',
    'new_series',
]

_CLASSIFICATION_PRIORITY: tuple[InboxClassification, ...] = (
    'identity_digest_conflict',
    'exact_duplicate',
    'revision_variant',
    'fills_missing_predecessor',
    'extends_known_head',
    'continues_branch',
    'parallel_branch_head',
    'new_series',
)

PromotionAuthorityKind = Literal[
    'raw_visual_evidence',
    'semantic_geometry',
    'annotations',
    'measurements',
    'as_built_observations',
    'connected_space',
    'reference_targets',
    'supplemental_authority',
]

PROMOTION_AUTHORITY_KINDS: frozenset[str] = frozenset(
    {
        'raw_visual_evidence',
        'semantic_geometry',
        'annotations',
        'measurements',
        'as_built_observations',
        'connected_space',
        'reference_targets',
        'supplemental_authority',
    }
)

PromotionOutcome = Literal['promoted', 'blocked']

Promotability = Literal[
    'promotable',
    'partially_promotable',
    'blocked',
    'complete',
]

InboxComparisonRelationship = Literal[
    'no_predecessor',
    'exact_identity',
    'direct_child',
    'same_series_unlinked',
    'unrelated_series',
]

SpatialComparisonState = Literal[
    'disabled_unaligned',
    'diagnostic_only',
]


class CaptureInboxItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    inbox_item_id: str = Field(pattern=r'^capture-inbox-item:[0-9a-f]{64}$')
    lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    scope: str = Field(min_length=1)
    capture_series_id: str = Field(min_length=1)
    capture_revision_id: str = Field(min_length=1)
    bundle_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_revision_id: str | None = None
    capture_session_ids: tuple[str, ...] = ()
    coordinate_space_ids: tuple[str, ...] = ()
    arrival_source: str = Field(min_length=1)
    source_detail: str = ''
    first_arrived_at_utc: str = Field(min_length=1)
    arrival_count: int = Field(ge=1)
    primary_classification: InboxClassification
    classification_flags: tuple[InboxClassification, ...] = ()
    conflict_lineage_digest: str | None = None
    bundle_validation: Literal['validated', 'rejected']
    validation_detail: str = ''
    dependency_state: Literal['not_evaluated', 'satisfied', 'unresolved'] = 'not_evaluated'
    dependency_detail: str = ''
    alignment_state: Literal['not_required', 'pending', 'resolved', 'blocked'] = (
        'not_required'
    )
    alignment_detail: str = ''
    world_alignment_authority_id: str | None = None
    evidence_conflict_state: Literal['none', 'open', 'resolved'] = 'none'
    evidence_conflict_detail: str = ''
    disposition: InboxDisposition = 'pending'
    disposition_reason: str = ''
    disposition_at_utc: str | None = None
    operator_notes: str = ''
    has_connected_space_document: bool = False


class CaptureInboxPromotionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    promotion_record_id: str = Field(
        pattern=r'^capture-inbox-promotion:[0-9a-f]{64}$'
    )
    lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_kind: PromotionAuthorityKind
    outcome: PromotionOutcome
    created_authority_id: str | None = None
    detail: str = ''
    promoted_at_utc: str = Field(min_length=1)


class CaptureInboxSupersession(BaseModel):
    model_config = ConfigDict(frozen=True)

    supersession_id: str = Field(
        pattern=r'^capture-inbox-supersession:[0-9a-f]{64}$'
    )
    superseded_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    superseding_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_kind: PromotionAuthorityKind
    reason: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)


class CaptureCrossRevisionRegistration(BaseModel):
    """Accepted explicit alignment between two capture revisions (#395 route)."""

    model_config = ConfigDict(frozen=True)

    registration_id: str = Field(
        pattern=r'^capture-revision-registration:[0-9a-f]{64}$'
    )
    older_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    newer_lineage_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    transform: SemanticCoordinateTransform
    transform_class: str
    alignment_method: CaptureAlignmentMethod
    uniform_scale_m_per_capture_m: float | None = None
    note: str = ''
    created_at_utc: str = Field(min_length=1)


class CaptureInboxInspection(BaseModel):
    """Read-only inspection surface for one staged delivery."""

    model_config = ConfigDict(frozen=True)

    item: CaptureInboxItem
    promotability: Promotability
    promoted_authority_kinds: tuple[PromotionAuthorityKind, ...]
    blocked_authority_kinds: tuple[PromotionAuthorityKind, ...]
    available_authority_kinds: tuple[PromotionAuthorityKind, ...]
    source_evidence_count: int
    roomplan_record_count: int
    raw_mesh_count: int
    authority_record_count: int
    promotions: tuple[CaptureInboxPromotionRecord, ...]
    supersessions_of_this: tuple[CaptureInboxSupersession, ...]
    supersessions_by_this: tuple[CaptureInboxSupersession, ...]
    registrations: tuple[CaptureCrossRevisionRegistration, ...]


class CaptureInboxComparison(BaseModel):
    """Pairwise staged-vs-predecessor comparison.

    Deliberately never a spatial semantic diff: geometry comparison is
    disabled until an explicit world-to-scene alignment (#347) or an
    accepted cross-revision registration (#395) exists for the pair, and
    even then only diagnostic (structure/count-level) comparison is
    reported here.
    """

    model_config = ConfigDict(frozen=True)

    lineage_digest: str
    predecessor_lineage_digest: str | None
    relationship: InboxComparisonRelationship
    capture_revision_id: str
    predecessor_capture_revision_id: str | None
    bundle_digest_changed: bool | None
    source_evidence_count_self: int
    source_evidence_count_predecessor: int | None
    raw_mesh_count_self: int
    raw_mesh_count_predecessor: int | None
    authority_record_count_self: int
    authority_record_count_predecessor: int | None
    alignment_state: Literal[
        'explicit_alignment_present',
        'cross_revision_registration_present',
        'unavailable',
    ]
    spatial_comparison: SpatialComparisonState
    detail: str


class CaptureInboxStageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: CaptureInboxItem
    created: bool
    outcome: Literal['staged', 'already_staged']

    @property
    def lineage_digest(self) -> str:
        return self.item.lineage_digest


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _inbox_hash(domain: str, value: object) -> str:
    return sha256(
        _canonical_json({'domain': domain, 'payload': value}).encode('utf-8')
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_authority_kinds(
    plan: CaptureIngestionPlan,
) -> frozenset[PromotionAuthorityKind]:
    """Authority kinds actually present in a delivery's plan."""
    kinds: set[PromotionAuthorityKind] = set()
    if plan.source_evidence:
        kinds.add('raw_visual_evidence')
    if plan.raw_visual_mesh_handoffs:
        kinds.add('semantic_geometry')
    if plan.roomplan_records:
        kinds.add('as_built_observations')
    record_kinds = {record.record_kind for record in plan.authority_records}
    if 'annotation' in record_kinds:
        kinds.add('annotations')
    if 'measurement' in record_kinds:
        kinds.add('measurements')
    if any(
        evidence.path.endswith('connected-spaces.json')
        for evidence in plan.source_evidence
    ):
        kinds.add('connected_space')
    return frozenset(kinds)


class CaptureInboxRepository:
    """Durable Capture Inbox records on the shared cad.sqlite3 database."""

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
                'capture_inbox_items',
                'capture_inbox_promotions',
                'capture_inbox_supersessions',
                'capture_inbox_registrations',
            )

    def _converge_schema(self, connection: sqlite3.Connection) -> None:
        """Legacy-shape tail of the schema-authority migration (#302).

        Plain ``CREATE TABLE`` lives in ``cad_schema_ddl`` and runs inside
        the versioned migration; this sequence converges databases whose
        persisted shapes predate the canonical contract (the lineage
        foreign-key repoint rebuilds the items table) and installs the
        inbox tables so every supported open path converges the same way.
        """
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS capture_ingestion_lineages (
                lineage_digest TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS capture_inbox_items (
                lineage_digest TEXT PRIMARY KEY
                    REFERENCES capture_ingestion_lineages(lineage_digest),
                inbox_item_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                capture_series_id TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                bundle_digest TEXT NOT NULL,
                parent_revision_id TEXT,
                capture_session_ids_json TEXT NOT NULL,
                coordinate_space_ids_json TEXT NOT NULL,
                arrival_source TEXT NOT NULL,
                source_detail TEXT NOT NULL,
                first_arrived_at_utc TEXT NOT NULL,
                arrival_count INTEGER NOT NULL,
                primary_classification TEXT NOT NULL,
                classification_flags_json TEXT NOT NULL,
                conflict_lineage_digest TEXT,
                bundle_validation TEXT NOT NULL,
                validation_detail TEXT NOT NULL,
                dependency_state TEXT NOT NULL,
                dependency_detail TEXT NOT NULL,
                alignment_state TEXT NOT NULL,
                alignment_detail TEXT NOT NULL,
                world_alignment_authority_id TEXT,
                evidence_conflict_state TEXT NOT NULL,
                evidence_conflict_detail TEXT NOT NULL,
                disposition TEXT NOT NULL,
                disposition_reason TEXT NOT NULL,
                disposition_at_utc TEXT,
                operator_notes TEXT NOT NULL,
                has_connected_space_document INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_series
            ON capture_inbox_items(capture_series_id);
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_revision
            ON capture_inbox_items(capture_revision_id);
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_scope
            ON capture_inbox_items(scope);

            CREATE TABLE IF NOT EXISTS capture_inbox_promotions (
                promotion_record_id TEXT PRIMARY KEY,
                lineage_digest TEXT NOT NULL
                    REFERENCES capture_inbox_items(lineage_digest),
                authority_kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_authority_id TEXT,
                detail TEXT NOT NULL,
                promoted_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_promotions_item
            ON capture_inbox_promotions(lineage_digest);

            CREATE TABLE IF NOT EXISTS capture_inbox_supersessions (
                supersession_id TEXT PRIMARY KEY,
                superseded_lineage_digest TEXT NOT NULL
                    REFERENCES capture_inbox_items(lineage_digest),
                superseding_lineage_digest TEXT NOT NULL
                    REFERENCES capture_inbox_items(lineage_digest),
                authority_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS capture_inbox_registrations (
                registration_id TEXT PRIMARY KEY,
                older_lineage_digest TEXT NOT NULL
                    REFERENCES capture_inbox_items(lineage_digest),
                newer_lineage_digest TEXT NOT NULL
                    REFERENCES capture_inbox_items(lineage_digest),
                transform_json TEXT NOT NULL,
                transform_class TEXT NOT NULL,
                alignment_method TEXT NOT NULL,
                uniform_scale_m_per_capture_m REAL,
                note TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(older_lineage_digest, newer_lineage_digest)
            );
            '''
        )
        self._repoint_items_lineage_parent(connection)
        # The repoint rebuild drops and recreates the items table;
        # re-install its indexes after (IF NOT EXISTS keeps the
        # already-converged path cheap).
        connection.executescript(
            '''
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_series
            ON capture_inbox_items(capture_series_id);
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_revision
            ON capture_inbox_items(capture_revision_id);
            CREATE INDEX IF NOT EXISTS idx_capture_inbox_scope
            ON capture_inbox_items(scope);
            '''
            )

    def _repoint_items_lineage_parent(
        self, connection: sqlite3.Connection
    ) -> None:
        """Retarget the inbox items table's lineage foreign key.

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
                'PRAGMA foreign_key_list(capture_inbox_items)'
            )
            if str(row['from']) == 'lineage_digest'
        }
        if parents == {'capture_ingestion_lineages'}:
            return
        if connection.in_transaction:
            connection.commit()
        # foreign_keys must be OFF during the rebuild: renaming the items
        # table otherwise rewrites the references the promotion,
        # supersession, and registration tables hold on it to the dropped
        # legacy name.
        connection.execute('PRAGMA foreign_keys=OFF')
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''
                ALTER TABLE capture_inbox_items
                RENAME TO capture_inbox_items_legacy
                '''
            )
            connection.execute(
                '''
                CREATE TABLE capture_inbox_items (
                    lineage_digest TEXT PRIMARY KEY
                        REFERENCES capture_ingestion_lineages(lineage_digest),
                    inbox_item_id TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL,
                    capture_series_id TEXT NOT NULL,
                    capture_revision_id TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    parent_revision_id TEXT,
                    capture_session_ids_json TEXT NOT NULL,
                    coordinate_space_ids_json TEXT NOT NULL,
                    arrival_source TEXT NOT NULL,
                    source_detail TEXT NOT NULL,
                    first_arrived_at_utc TEXT NOT NULL,
                    arrival_count INTEGER NOT NULL,
                    primary_classification TEXT NOT NULL,
                    classification_flags_json TEXT NOT NULL,
                    conflict_lineage_digest TEXT,
                    bundle_validation TEXT NOT NULL,
                    validation_detail TEXT NOT NULL,
                    dependency_state TEXT NOT NULL,
                    dependency_detail TEXT NOT NULL,
                    alignment_state TEXT NOT NULL,
                    alignment_detail TEXT NOT NULL,
                    world_alignment_authority_id TEXT,
                    evidence_conflict_state TEXT NOT NULL,
                    evidence_conflict_detail TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    disposition_reason TEXT NOT NULL,
                    disposition_at_utc TEXT,
                    operator_notes TEXT NOT NULL,
                    has_connected_space_document INTEGER NOT NULL
                )
                '''
            )
            connection.execute(
                '''
                INSERT INTO capture_inbox_items(
                    lineage_digest, inbox_item_id, scope,
                    capture_series_id, capture_revision_id, bundle_digest,
                    parent_revision_id, capture_session_ids_json,
                    coordinate_space_ids_json, arrival_source,
                    source_detail, first_arrived_at_utc, arrival_count,
                    primary_classification, classification_flags_json,
                    conflict_lineage_digest, bundle_validation,
                    validation_detail, dependency_state, dependency_detail,
                    alignment_state, alignment_detail,
                    world_alignment_authority_id, evidence_conflict_state,
                    evidence_conflict_detail, disposition,
                    disposition_reason, disposition_at_utc, operator_notes,
                    has_connected_space_document
                )
                SELECT lineage_digest, inbox_item_id, scope,
                    capture_series_id, capture_revision_id, bundle_digest,
                    parent_revision_id, capture_session_ids_json,
                    coordinate_space_ids_json, arrival_source,
                    source_detail, first_arrived_at_utc, arrival_count,
                    primary_classification, classification_flags_json,
                    conflict_lineage_digest, bundle_validation,
                    validation_detail, dependency_state, dependency_detail,
                    alignment_state, alignment_detail,
                    world_alignment_authority_id, evidence_conflict_state,
                    evidence_conflict_detail, disposition,
                    disposition_reason, disposition_at_utc, operator_notes,
                    has_connected_space_document
                FROM capture_inbox_items_legacy
                '''
            )
            connection.execute('DROP TABLE capture_inbox_items_legacy')
            orphans = connection.execute(
                'PRAGMA foreign_key_check(capture_inbox_items)'
            ).fetchall()
            if orphans:
                connection.rollback()
                raise CaptureInboxError(
                    'cannot retarget inbox items to the lineages table: '
                    'unresolved foreign keys remain'
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

    def stage(
        self,
        plan: CaptureIngestionPlan,
        *,
        arrival_source: str,
        scope: str = CAPTURE_INBOX_UNASSIGNED_SCOPE,
        source_detail: str = '',
        bundle_validation: Literal['validated', 'rejected'] = 'validated',
        validation_detail: str = '',
        arrived_at_utc: str | None = None,
    ) -> CaptureInboxStageResult:
        """Stage a validated ingestion into the Inbox.

        Idempotent: re-delivery of an identical lineage reports
        ``already_staged`` and bumps the arrival counter. The same
        capture revision arriving under a different bundle digest is a
        hard conflict — staged for audit with classification
        ``identity_digest_conflict`` and promotability blocked.
        """
        bundle = plan.bundle
        inbox_item_id = (
            'capture-inbox-item:' + _inbox_hash(INBOX_ITEM_DOMAIN, plan.lineage_digest)
        )
        arrived = arrived_at_utc or _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                ingested = connection.execute(
                    'SELECT lineage_digest FROM capture_ingestion_runs '
                    'WHERE lineage_digest=?',
                    (plan.lineage_digest,),
                ).fetchone()
                if ingested is None:
                    raise CaptureInboxError(
                        'capture inbox stages only persisted ingestions; '
                        'ingest the bundle first'
                    )
                existing = self._row(connection, plan.lineage_digest)
                if existing is not None:
                    connection.execute(
                        'UPDATE capture_inbox_items SET arrival_count=? '
                        'WHERE lineage_digest=?',
                        (int(existing['arrival_count']) + 1, plan.lineage_digest),
                    )
                    item = self._row(connection, plan.lineage_digest)
                    connection.commit()
                    return CaptureInboxStageResult(
                        item=self._item_from_row(item),
                        created=False,
                        outcome='already_staged',
                    )

                classification, flags, conflict_lineage = self._classify(
                    connection, plan
                )
                connection.execute(
                    '''
                    INSERT INTO capture_inbox_items(
                        lineage_digest, inbox_item_id, scope,
                        capture_series_id, capture_revision_id, bundle_digest,
                        parent_revision_id, capture_session_ids_json,
                        coordinate_space_ids_json, arrival_source,
                        source_detail, first_arrived_at_utc, arrival_count,
                        primary_classification, classification_flags_json,
                        conflict_lineage_digest, bundle_validation,
                        validation_detail, dependency_state, dependency_detail,
                        alignment_state, alignment_detail,
                        world_alignment_authority_id, evidence_conflict_state,
                        evidence_conflict_detail, disposition,
                        disposition_reason, disposition_at_utc,
                        operator_notes, has_connected_space_document
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ''',
                    (
                        plan.lineage_digest,
                        inbox_item_id,
                        scope,
                        bundle.capture_series_id,
                        bundle.capture_revision_id,
                        bundle.bundle_digest,
                        bundle.parent_revision_id,
                        _canonical_json(list(bundle.capture_session_ids)),
                        _canonical_json(list(bundle.coordinate_space_ids)),
                        arrival_source,
                        source_detail,
                        arrived,
                        1,
                        classification,
                        _canonical_json(list(flags)),
                        conflict_lineage,
                        bundle_validation,
                        validation_detail,
                        'not_evaluated',
                        '',
                        'not_required',
                        '',
                        None,
                        'none',
                        '',
                        'pending',
                        '',
                        None,
                        '',
                        1
                        if 'connected_space' in plan_authority_kinds(plan)
                        else 0,
                    ),
                )
                row = self._row(connection, plan.lineage_digest)
                connection.commit()
                return CaptureInboxStageResult(
                    item=self._item_from_row(row),
                    created=True,
                    outcome='staged',
                )
            except Exception:
                connection.rollback()
                raise

    def _classify(
        self, connection: sqlite3.Connection, plan: CaptureIngestionPlan
    ) -> tuple[InboxClassification, tuple[InboxClassification, ...], str | None]:
        bundle = plan.bundle
        flags: set[InboxClassification] = set()

        conflict = connection.execute(
            'SELECT lineage_digest FROM capture_inbox_items '
            'WHERE capture_revision_id=? AND bundle_digest<>?',
            (bundle.capture_revision_id, bundle.bundle_digest),
        ).fetchall()
        if conflict:
            flags.add('identity_digest_conflict')
            return (
                'identity_digest_conflict',
                tuple(flags),
                str(conflict[0]['lineage_digest']),
            )

        variant = connection.execute(
            'SELECT lineage_digest FROM capture_inbox_items '
            'WHERE capture_revision_id=? AND lineage_digest<>?',
            (bundle.capture_revision_id, plan.lineage_digest),
        ).fetchall()
        if variant:
            flags.add('revision_variant')

        series_rows = connection.execute(
            'SELECT capture_revision_id, parent_revision_id '
            'FROM capture_inbox_items WHERE capture_series_id=?',
            (bundle.capture_series_id,),
        ).fetchall()
        known_revisions = {row['capture_revision_id'] for row in series_rows}
        children = [
            row
            for row in series_rows
            if row['parent_revision_id'] == bundle.capture_revision_id
        ]
        parent_rows = [
            row
            for row in series_rows
            if row['capture_revision_id'] == bundle.parent_revision_id
        ]
        has_parent_link = (
            bundle.parent_revision_id is not None
            and bundle.parent_revision_id in known_revisions
        )
        if children:
            flags.add('fills_missing_predecessor')
        if has_parent_link:
            parented = {
                row['parent_revision_id'] for row in series_rows if row['parent_revision_id']
            }
            head_parents = [
                row
                for row in parent_rows
                if row['capture_revision_id'] not in parented
            ]
            if head_parents:
                flags.add('extends_known_head')
            else:
                flags.add('continues_branch')
        if not series_rows:
            if bundle.parent_revision_id:
                # first contact of a series that already claims a
                # predecessor we have never seen: a branch head awaiting
                # its parent — not a root arrival
                flags.add('parallel_branch_head')
            else:
                flags.add('new_series')
        elif not has_parent_link and not children:
            flags.add('parallel_branch_head')

        for candidate in _CLASSIFICATION_PRIORITY:
            if candidate in flags:
                return candidate, tuple(sorted(flags)), None
        return 'new_series', tuple(sorted(flags)), None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def _row(
        self, connection: sqlite3.Connection, lineage_digest: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            'SELECT * FROM capture_inbox_items WHERE lineage_digest=?',
            (lineage_digest,),
        ).fetchone()

    def _item_from_row(self, row: sqlite3.Row) -> CaptureInboxItem:
        return CaptureInboxItem(
            inbox_item_id=row['inbox_item_id'],
            lineage_digest=row['lineage_digest'],
            scope=row['scope'],
            capture_series_id=row['capture_series_id'],
            capture_revision_id=row['capture_revision_id'],
            bundle_digest=row['bundle_digest'],
            parent_revision_id=row['parent_revision_id'],
            capture_session_ids=tuple(
                json.loads(row['capture_session_ids_json'])
            ),
            coordinate_space_ids=tuple(
                json.loads(row['coordinate_space_ids_json'])
            ),
            arrival_source=row['arrival_source'],
            source_detail=row['source_detail'],
            first_arrived_at_utc=row['first_arrived_at_utc'],
            arrival_count=int(row['arrival_count']),
            primary_classification=row['primary_classification'],
            classification_flags=tuple(
                json.loads(row['classification_flags_json'])
            ),
            conflict_lineage_digest=row['conflict_lineage_digest'],
            bundle_validation=row['bundle_validation'],
            validation_detail=row['validation_detail'],
            dependency_state=row['dependency_state'],
            dependency_detail=row['dependency_detail'],
            alignment_state=row['alignment_state'],
            alignment_detail=row['alignment_detail'],
            world_alignment_authority_id=row['world_alignment_authority_id'],
            evidence_conflict_state=row['evidence_conflict_state'],
            evidence_conflict_detail=row['evidence_conflict_detail'],
            disposition=row['disposition'],
            disposition_reason=row['disposition_reason'],
            disposition_at_utc=row['disposition_at_utc'],
            operator_notes=row['operator_notes'],
            has_connected_space_document=bool(
                row['has_connected_space_document']
            ),
        )

    def get(self, lineage_digest: str) -> CaptureInboxItem | None:
        with closing(self._connect()) as connection:
            row = self._row(connection, lineage_digest)
            return self._item_from_row(row) if row is not None else None

    def list_items(
        self,
        *,
        scope: str | None = None,
        disposition: InboxDisposition | None = None,
    ) -> tuple[CaptureInboxItem, ...]:
        sql = 'SELECT * FROM capture_inbox_items WHERE 1=1'
        args: list[str] = []
        if scope is not None:
            sql += ' AND scope=?'
            args.append(scope)
        if disposition is not None:
            sql += ' AND disposition=?'
            args.append(disposition)
        sql += ' ORDER BY first_arrived_at_utc, lineage_digest'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, args).fetchall()
            return tuple(self._item_from_row(row) for row in rows)

    def promotions_for(
        self, lineage_digest: str
    ) -> tuple[CaptureInboxPromotionRecord, ...]:
        with closing(self._connect()) as connection:
            return self._promotions_for(connection, lineage_digest)

    def _promotions_for(
        self, connection: sqlite3.Connection, lineage_digest: str
    ) -> tuple[CaptureInboxPromotionRecord, ...]:
        rows = connection.execute(
            'SELECT * FROM capture_inbox_promotions WHERE lineage_digest=? '
            'ORDER BY promoted_at_utc, promotion_record_id',
            (lineage_digest,),
        ).fetchall()
        return tuple(
            CaptureInboxPromotionRecord(
                promotion_record_id=row['promotion_record_id'],
                lineage_digest=row['lineage_digest'],
                authority_kind=row['authority_kind'],
                outcome=row['outcome'],
                created_authority_id=row['created_authority_id'],
                detail=row['detail'],
                promoted_at_utc=row['promoted_at_utc'],
            )
            for row in rows
        )

    def _supersessions(
        self, connection: sqlite3.Connection, column: str, lineage_digest: str
    ) -> tuple[CaptureInboxSupersession, ...]:
        rows = connection.execute(
            f'SELECT * FROM capture_inbox_supersessions WHERE {column}=?',
            (lineage_digest,),
        ).fetchall()
        return tuple(
            CaptureInboxSupersession(
                supersession_id=row['supersession_id'],
                superseded_lineage_digest=row['superseded_lineage_digest'],
                superseding_lineage_digest=row['superseding_lineage_digest'],
                authority_kind=row['authority_kind'],
                reason=row['reason'],
                created_at_utc=row['created_at_utc'],
            )
            for row in rows
        )

    def _registrations_for(
        self, connection: sqlite3.Connection, lineage_digest: str
    ) -> tuple[CaptureCrossRevisionRegistration, ...]:
        rows = connection.execute(
            'SELECT * FROM capture_inbox_registrations '
            'WHERE older_lineage_digest=? OR newer_lineage_digest=?',
            (lineage_digest, lineage_digest),
        ).fetchall()
        return tuple(
            CaptureCrossRevisionRegistration(
                registration_id=row['registration_id'],
                older_lineage_digest=row['older_lineage_digest'],
                newer_lineage_digest=row['newer_lineage_digest'],
                transform=SemanticCoordinateTransform.model_validate(
                    json.loads(row['transform_json'])
                ),
                transform_class=row['transform_class'],
                alignment_method=row['alignment_method'],
                uniform_scale_m_per_capture_m=(
                    row['uniform_scale_m_per_capture_m']
                ),
                note=row['note'],
                created_at_utc=row['created_at_utc'],
            )
            for row in rows
        )

    def inspect(self, lineage_digest: str) -> CaptureInboxInspection | None:
        """Read-only inspection: item + plan summary + gate facets + records."""
        with closing(self._connect()) as connection:
            row = self._row(connection, lineage_digest)
            if row is None:
                return None
            item = self._item_from_row(row)
            plan = self.ingestion_repository.get_ingestion(lineage_digest)
            if plan is None:
                raise CaptureInboxError(
                    'inbox item survives without its ingestion run; '
                    'the store is inconsistent'
                )
            promotions = self._promotions_for(connection, lineage_digest)
            promoted_kinds = tuple(
                sorted(
                    {
                        record.authority_kind
                        for record in promotions
                        if record.outcome == 'promoted'
                    }
                )
            )
            blocked_kinds = tuple(
                sorted(
                    {
                        record.authority_kind
                        for record in promotions
                        if record.outcome == 'blocked'
                        and record.authority_kind not in promoted_kinds
                    }
                )
            )
            return CaptureInboxInspection(
                item=item,
                promotability=self._promotability(item, promotions, plan),
                promoted_authority_kinds=promoted_kinds,
                blocked_authority_kinds=blocked_kinds,
                available_authority_kinds=tuple(
                    sorted(plan_authority_kinds(plan))
                ),
                source_evidence_count=len(plan.source_evidence),
                roomplan_record_count=len(plan.roomplan_records),
                raw_mesh_count=len(plan.raw_visual_mesh_handoffs),
                authority_record_count=len(plan.authority_records),
                promotions=promotions,
                supersessions_of_this=self._supersessions(
                    connection, 'superseded_lineage_digest', lineage_digest
                ),
                supersessions_by_this=self._supersessions(
                    connection, 'superseding_lineage_digest', lineage_digest
                ),
                registrations=self._registrations_for(
                    connection, lineage_digest
                ),
            )

    def _promotability(
        self,
        item: CaptureInboxItem,
        promotions: tuple[CaptureInboxPromotionRecord, ...],
        plan: CaptureIngestionPlan,
    ) -> Promotability:
        if item.disposition in ('rejected', 'superseded'):
            return 'blocked'
        promoted = {
            record.authority_kind
            for record in promotions
            if record.outcome == 'promoted'
        }
        if item.disposition == 'promoted':
            return 'complete'
        facets_bad = (
            item.bundle_validation != 'validated'
            or item.primary_classification == 'identity_digest_conflict'
            or item.dependency_state == 'unresolved'
            or item.alignment_state == 'blocked'
            or item.evidence_conflict_state == 'open'
        )
        if facets_bad:
            return 'blocked'
        if promoted and promoted < plan_authority_kinds(plan):
            return 'partially_promotable'
        if promoted >= plan_authority_kinds(plan) and promoted:
            return 'complete'
        return 'promotable'

    # ------------------------------------------------------------------
    # gate facets — evaluated independently, never collapsed
    # ------------------------------------------------------------------

    def _update_facets(
        self,
        lineage_digest: str,
        updates: Mapping[str, object],
    ) -> CaptureInboxItem:
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                row = self._row(connection, lineage_digest)
                if row is None:
                    raise CaptureInboxError('unknown inbox item')
                for column, value in updates.items():
                    connection.execute(
                        f'UPDATE capture_inbox_items SET {column}=? '
                        'WHERE lineage_digest=?',
                        (value, lineage_digest),
                    )
                updated = self._row(connection, lineage_digest)
                connection.commit()
                return self._item_from_row(updated)
            except Exception:
                connection.rollback()
                raise

    def set_dependency_state(
        self,
        lineage_digest: str,
        state: Literal['satisfied', 'unresolved', 'not_evaluated'],
        detail: str = '',
    ) -> CaptureInboxItem:
        return self._update_facets(
            lineage_digest,
            {'dependency_state': state, 'dependency_detail': detail},
        )

    def set_alignment_state(
        self,
        lineage_digest: str,
        state: Literal['not_required', 'pending', 'resolved', 'blocked'],
        detail: str = '',
    ) -> CaptureInboxItem:
        return self._update_facets(
            lineage_digest,
            {'alignment_state': state, 'alignment_detail': detail},
        )

    def set_evidence_conflict_state(
        self,
        lineage_digest: str,
        state: Literal['none', 'open', 'resolved'],
        detail: str = '',
    ) -> CaptureInboxItem:
        return self._update_facets(
            lineage_digest,
            {'evidence_conflict_state': state, 'evidence_conflict_detail': detail},
        )

    def attach_world_alignment(
        self,
        lineage_digest: str,
        authority: CaptureWorldToSceneAuthority,
        detail: str = '',
    ) -> CaptureInboxItem:
        """Bind an explicit world-to-scene authority (#347) to a staged item."""
        with closing(self._connect()) as connection:
            row = self._row(connection, lineage_digest)
            if row is None:
                raise CaptureInboxError('unknown inbox item')
            space_ids = set(json.loads(row['coordinate_space_ids_json']))
            if authority.coordinate_space_id not in space_ids:
                raise CaptureInboxError(
                    'alignment authority coordinate space is not part of '
                    'this delivery'
                )
        return self._update_facets(
            lineage_digest,
            {
                'world_alignment_authority_id': authority.authority_id,
                'alignment_state': 'resolved',
                'alignment_detail': detail
                or f'world alignment bound: {authority.authority_id}',
            },
        )

    def assign_scope(self, lineage_digest: str, scope: str) -> CaptureInboxItem:
        if not scope:
            raise CaptureInboxError('scope must be non-empty')
        return self._update_facets(lineage_digest, {'scope': scope})

    def add_operator_note(
        self, lineage_digest: str, note: str
    ) -> CaptureInboxItem:
        if not note.strip():
            raise CaptureInboxError('operator note must be non-empty')
        current = self.get(lineage_digest)
        if current is None:
            raise CaptureInboxError('unknown inbox item')
        combined = (
            f'{current.operator_notes}\n[{_utc_now()}] {note}'
            if current.operator_notes
            else f'[{_utc_now()}] {note}'
        )
        return self._update_facets(
            lineage_digest, {'operator_notes': combined}
        )

    def defer(self, lineage_digest: str, reason: str) -> CaptureInboxItem:
        if not reason:
            raise CaptureInboxError('defer requires a reason')
        return self._update_facets(
            lineage_digest,
            {
                'disposition': 'deferred',
                'disposition_reason': reason,
                'disposition_at_utc': _utc_now(),
            },
        )

    def resume(self, lineage_digest: str) -> CaptureInboxItem:
        item = self.get(lineage_digest)
        if item is None:
            raise CaptureInboxError('unknown inbox item')
        if item.disposition not in ('deferred', 'rejected'):
            raise CaptureInboxError(
                'only deferred or rejected items can resume to pending'
            )
        return self._update_facets(
            lineage_digest,
            {
                'disposition': 'pending',
                'disposition_reason': '',
                'disposition_at_utc': _utc_now(),
            },
        )

    def reject(self, lineage_digest: str, reason: str) -> CaptureInboxItem:
        if not reason:
            raise CaptureInboxError('rejection requires a reason')
        return self._update_facets(
            lineage_digest,
            {
                'disposition': 'rejected',
                'disposition_reason': reason,
                'disposition_at_utc': _utc_now(),
            },
        )

    # ------------------------------------------------------------------
    # promotion — explicit, granular, never whole-item implicit
    # ------------------------------------------------------------------

    def _check_promotable(self, item: CaptureInboxItem) -> None:
        if item.disposition in ('rejected', 'superseded'):
            raise CaptureInboxError(
                f'item is {item.disposition}; resume it before promoting'
            )
        if item.bundle_validation != 'validated':
            raise CaptureInboxError(
                'bundle validation did not pass; promotion is blocked'
            )
        if item.primary_classification == 'identity_digest_conflict':
            raise CaptureInboxError(
                'identity/digest conflict is unresolved; promotion is blocked'
            )
        if item.dependency_state == 'unresolved':
            raise CaptureInboxError('dependencies unresolved; promotion blocked')
        if item.evidence_conflict_state == 'open':
            raise CaptureInboxError(
                'open evidence conflicts; promotion is blocked'
            )
        if item.alignment_state == 'blocked':
            raise CaptureInboxError('alignment blocked; promotion is blocked')

    def record_promotion(
        self,
        lineage_digest: str,
        authority_kind: PromotionAuthorityKind,
        created_authority_id: str,
        *,
        detail: str = '',
    ) -> CaptureInboxPromotionRecord:
        """Record that one authority kind was promoted through its pipeline.

        The caller supplies the authority id produced by the owning
        pipeline (e.g. a semantic-promotion id). Records are idempotent:
        an identical (kind, authority) pair replays to the same record.
        """
        return self._record_outcome(
            lineage_digest,
            authority_kind,
            'promoted',
            created_authority_id=created_authority_id,
            detail=detail,
        )

    def record_blocked(
        self,
        lineage_digest: str,
        authority_kind: PromotionAuthorityKind,
        reason: str,
    ) -> CaptureInboxPromotionRecord:
        """Record a promotion attempt that could not complete, with why."""
        if not reason:
            raise CaptureInboxError('blocked record requires a reason')
        return self._record_outcome(
            lineage_digest,
            authority_kind,
            'blocked',
            created_authority_id=None,
            detail=reason,
        )

    def _record_outcome(
        self,
        lineage_digest: str,
        authority_kind: PromotionAuthorityKind,
        outcome: PromotionOutcome,
        *,
        created_authority_id: str | None,
        detail: str,
    ) -> CaptureInboxPromotionRecord:
        record_id = 'capture-inbox-promotion:' + _inbox_hash(
            INBOX_PROMOTION_DOMAIN,
            {
                'lineage_digest': lineage_digest,
                'authority_kind': authority_kind,
                'outcome': outcome,
                'created_authority_id': created_authority_id,
            },
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                row = self._row(connection, lineage_digest)
                if row is None:
                    raise CaptureInboxError('unknown inbox item')
                item = self._item_from_row(row)
                self._check_promotable(item)
                plan = self.ingestion_repository.get_ingestion(lineage_digest)
                if plan is None:
                    raise CaptureInboxError('ingestion run missing')
                if outcome == 'promoted' and (
                    authority_kind not in plan_authority_kinds(plan)
                    and authority_kind != 'supplemental_authority'
                ):
                    raise CaptureInboxError(
                        f'authority kind {authority_kind} is not present '
                        'in this delivery; record a blocked outcome instead'
                    )
                existing = connection.execute(
                    'SELECT promotion_record_id FROM capture_inbox_promotions '
                    'WHERE promotion_record_id=?',
                    (record_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        '''
                        INSERT INTO capture_inbox_promotions(
                            promotion_record_id, lineage_digest,
                            authority_kind, outcome, created_authority_id,
                            detail, promoted_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            record_id,
                            lineage_digest,
                            authority_kind,
                            outcome,
                            created_authority_id,
                            detail,
                            _utc_now(),
                        ),
                    )
                promotions = self._promotions_for(connection, lineage_digest)
                disposition = self._derive_disposition(
                    item, promotions, plan
                )
                connection.execute(
                    'UPDATE capture_inbox_items SET disposition=?, '
                    'disposition_at_utc=? WHERE lineage_digest=?',
                    (disposition, _utc_now(), lineage_digest),
                )
                record = connection.execute(
                    'SELECT * FROM capture_inbox_promotions '
                    'WHERE promotion_record_id=?',
                    (record_id,),
                ).fetchone()
                connection.commit()
                return CaptureInboxPromotionRecord(
                    promotion_record_id=record['promotion_record_id'],
                    lineage_digest=record['lineage_digest'],
                    authority_kind=record['authority_kind'],
                    outcome=record['outcome'],
                    created_authority_id=record['created_authority_id'],
                    detail=record['detail'],
                    promoted_at_utc=record['promoted_at_utc'],
                )
            except Exception:
                connection.rollback()
                raise

    def _derive_disposition(
        self,
        item: CaptureInboxItem,
        promotions: tuple[CaptureInboxPromotionRecord, ...],
        plan: CaptureIngestionPlan,
    ) -> InboxDisposition:
        if item.disposition in ('rejected', 'deferred'):
            return item.disposition
        promoted = {
            record.authority_kind
            for record in promotions
            if record.outcome == 'promoted'
        }
        if not promoted:
            return 'pending'
        available = plan_authority_kinds(plan)
        if promoted >= available and available:
            return 'promoted'
        return 'partially_promoted'

    def promote(
        self,
        lineage_digest: str,
        authority_kinds: Iterable[PromotionAuthorityKind],
        *,
        reason: str,
        executor: (
            Callable[[CaptureIngestionPlan, PromotionAuthorityKind], str] | None
        ) = None,
        created_authorities: Mapping[PromotionAuthorityKind, str] | None = None,
    ) -> tuple[CaptureInboxPromotionRecord, ...]:
        """Granularly promote the selected authority kinds.

        Two driving modes: an ``executor`` callable producing the created
        authority id per kind (its exceptions become blocked records), or a
        caller-supplied ``created_authorities`` map for kinds already
        promoted through their owning pipelines. Promotion is partial-safe:
        kinds that fail are recorded as blocked with their reason while
        the rest still promote.
        """
        if not reason:
            raise CaptureInboxError('promotion requires a reason')
        item = self.get(lineage_digest)
        if item is None:
            raise CaptureInboxError('unknown inbox item')
        self._check_promotable(item)
        plan = self.ingestion_repository.get_ingestion(lineage_digest)
        if plan is None:
            raise CaptureInboxError('ingestion run missing')
        records: list[CaptureInboxPromotionRecord] = []
        for kind in authority_kinds:
            if executor is not None:
                try:
                    created_id = executor(plan, kind)
                except Exception as exc:  # record the block, keep going
                    records.append(
                        self.record_blocked(lineage_digest, kind, str(exc))
                    )
                    continue
            else:
                if not created_authorities or kind not in created_authorities:
                    raise CaptureInboxError(
                        f'no created authority supplied for {kind}'
                    )
                created_id = created_authorities[kind]
            records.append(
                self.record_promotion(
                    lineage_digest, kind, created_id, detail=reason
                )
            )
        return tuple(records)

    def supersede(
        self,
        superseded_lineage_digest: str,
        superseding_lineage_digest: str,
        authority_kind: PromotionAuthorityKind,
        *,
        reason: str,
    ) -> CaptureInboxSupersession:
        """Record authority-specific supersession of one staged item by another.

        Supersession is explicit and per-authority: the superseded item's
        disposition flips to ``superseded`` only once every promoted kind
        has been superseded.
        """
        if not reason:
            raise CaptureInboxError('supersession requires a reason')
        supersession_id = 'capture-inbox-supersession:' + _inbox_hash(
            INBOX_SUPERSESSION_DOMAIN,
            {
                'superseded': superseded_lineage_digest,
                'superseding': superseding_lineage_digest,
                'authority_kind': authority_kind,
            },
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                older_row = self._row(connection, superseded_lineage_digest)
                newer_row = self._row(connection, superseding_lineage_digest)
                if older_row is None or newer_row is None:
                    raise CaptureInboxError(
                        'supersession requires both items staged'
                    )
                existing = connection.execute(
                    'SELECT supersession_id FROM capture_inbox_supersessions '
                    'WHERE supersession_id=?',
                    (supersession_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        '''
                        INSERT INTO capture_inbox_supersessions(
                            supersession_id, superseded_lineage_digest,
                            superseding_lineage_digest, authority_kind,
                            reason, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            supersession_id,
                            superseded_lineage_digest,
                            superseding_lineage_digest,
                            authority_kind,
                            reason,
                            _utc_now(),
                        ),
                    )
                # flip the superseded item only once every promoted kind
                # is covered by a supersession record
                item = self._item_from_row(older_row)
                promotions = self._promotions_for(
                    connection, superseded_lineage_digest
                )
                promoted = {
                    record.authority_kind
                    for record in promotions
                    if record.outcome == 'promoted'
                }
                covered = {
                    row['authority_kind']
                    for row in connection.execute(
                        'SELECT authority_kind FROM capture_inbox_supersessions '
                        'WHERE superseded_lineage_digest=?',
                        (superseded_lineage_digest,),
                    ).fetchall()
                }
                if promoted and promoted <= covered:
                    connection.execute(
                        'UPDATE capture_inbox_items SET disposition=?, '
                        'disposition_reason=?, disposition_at_utc=? '
                        'WHERE lineage_digest=?',
                        (
                            'superseded',
                            f'superseded by {superseding_lineage_digest}: {reason}',
                            _utc_now(),
                            superseded_lineage_digest,
                        ),
                    )
                row = connection.execute(
                    'SELECT * FROM capture_inbox_supersessions '
                    'WHERE supersession_id=?',
                    (supersession_id,),
                ).fetchone()
                connection.commit()
                return CaptureInboxSupersession(
                    supersession_id=row['supersession_id'],
                    superseded_lineage_digest=row['superseded_lineage_digest'],
                    superseding_lineage_digest=row['superseding_lineage_digest'],
                    authority_kind=row['authority_kind'],
                    reason=row['reason'],
                    created_at_utc=row['created_at_utc'],
                )
            except Exception:
                connection.rollback()
                raise

    # ------------------------------------------------------------------
    # cross-revision registration (#395) and comparison
    # ------------------------------------------------------------------

    def register_cross_revision_alignment(
        self,
        older_lineage_digest: str,
        newer_lineage_digest: str,
        transform: SemanticCoordinateTransform,
        *,
        alignment_method: CaptureAlignmentMethod = 'rigid_registration',
        scale_policy: CaptureAlignmentScalePolicy | None = None,
        note: str = '',
    ) -> CaptureCrossRevisionRegistration:
        """Accept an explicit registration between two staged revisions.

        The transform obeys the same class contract as world-to-scene
        authorities (#347): rigid by default, similarity via declared
        method, shear only via explicit override, reflections never.
        """
        inspection = validate_capture_alignment(
            transform,
            alignment_method=alignment_method,
            scale_policy=scale_policy,
        )
        registration_id = 'capture-revision-registration:' + _inbox_hash(
            INBOX_REGISTRATION_DOMAIN,
            {
                'older': older_lineage_digest,
                'newer': newer_lineage_digest,
                'transform': transform.model_dump(mode='json'),
                'alignment_method': alignment_method,
            },
        )
        with closing(self._connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                if self._row(connection, older_lineage_digest) is None or (
                    self._row(connection, newer_lineage_digest) is None
                ):
                    raise CaptureInboxError(
                        'cross-revision registration requires both items staged'
                    )
                existing = connection.execute(
                    'SELECT registration_id FROM capture_inbox_registrations '
                    'WHERE older_lineage_digest=? AND newer_lineage_digest=?',
                    (older_lineage_digest, newer_lineage_digest),
                ).fetchone()
                if existing is not None:
                    raise CaptureInboxError(
                        'a registration already exists for this ordered pair'
                    )
                connection.execute(
                    '''
                    INSERT INTO capture_inbox_registrations(
                        registration_id, older_lineage_digest,
                        newer_lineage_digest, transform_json,
                        transform_class, alignment_method,
                        uniform_scale_m_per_capture_m, note, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        registration_id,
                        older_lineage_digest,
                        newer_lineage_digest,
                        _canonical_json(transform.model_dump(mode='json')),
                        inspection.transform_class,
                        alignment_method,
                        inspection.uniform_scale_m_per_capture_m,
                        note,
                        _utc_now(),
                    ),
                )
                row = connection.execute(
                    'SELECT * FROM capture_inbox_registrations '
                    'WHERE registration_id=?',
                    (registration_id,),
                ).fetchone()
                connection.commit()
                return CaptureCrossRevisionRegistration(
                    registration_id=row['registration_id'],
                    older_lineage_digest=row['older_lineage_digest'],
                    newer_lineage_digest=row['newer_lineage_digest'],
                    transform=transform,
                    transform_class=row['transform_class'],
                    alignment_method=row['alignment_method'],
                    uniform_scale_m_per_capture_m=(
                        row['uniform_scale_m_per_capture_m']
                    ),
                    note=row['note'],
                    created_at_utc=row['created_at_utc'],
                )
            except Exception:
                connection.rollback()
                raise

    def compare(
        self,
        lineage_digest: str,
        predecessor_lineage_digest: str | None = None,
    ) -> CaptureInboxComparison:
        """Compare a staged item against its predecessor (or an explicit peer)."""
        item = self.get(lineage_digest)
        if item is None:
            raise CaptureInboxError('unknown inbox item')
        plan = self.ingestion_repository.get_ingestion(lineage_digest)
        if plan is None:
            raise CaptureInboxError('ingestion run missing')

        if predecessor_lineage_digest is None:
            with closing(self._connect()) as connection:
                candidate = connection.execute(
                    'SELECT lineage_digest FROM capture_inbox_items '
                    'WHERE capture_revision_id=? AND capture_series_id=?',
                    (item.parent_revision_id or '', item.capture_series_id),
                ).fetchone()
            predecessor_lineage_digest = (
                str(candidate['lineage_digest']) if candidate is not None else None
            )
        predecessor = (
            self.get(predecessor_lineage_digest)
            if predecessor_lineage_digest
            else None
        )
        predecessor_plan = (
            self.ingestion_repository.get_ingestion(predecessor_lineage_digest)
            if predecessor is not None
            else None
        )
        if predecessor is not None and predecessor_plan is None:
            raise CaptureInboxError('predecessor ingestion run missing')

        if predecessor is None:
            relationship: InboxComparisonRelationship = 'no_predecessor'
        elif predecessor.lineage_digest == item.lineage_digest:
            relationship = 'exact_identity'
        elif predecessor.capture_series_id != item.capture_series_id:
            relationship = 'unrelated_series'
        elif item.parent_revision_id == predecessor.capture_revision_id:
            relationship = 'direct_child'
        else:
            relationship = 'same_series_unlinked'

        registration_present = False
        if predecessor is not None:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    'SELECT 1 FROM capture_inbox_registrations WHERE '
                    '(older_lineage_digest=? AND newer_lineage_digest=?) OR '
                    '(older_lineage_digest=? AND newer_lineage_digest=?)',
                    (
                        predecessor.lineage_digest,
                        item.lineage_digest,
                        item.lineage_digest,
                        predecessor.lineage_digest,
                    ),
                ).fetchall()
            registration_present = bool(rows)

        has_world_alignment = item.world_alignment_authority_id is not None
        if registration_present:
            alignment_state = 'cross_revision_registration_present'
        elif has_world_alignment:
            alignment_state = 'explicit_alignment_present'
        else:
            alignment_state = 'unavailable'
        spatial: SpatialComparisonState = (
            'diagnostic_only' if alignment_state != 'unavailable' else 'disabled_unaligned'
        )
        detail = (
            'diagnostic structure comparison only; geometry diff requires '
            'alignment-resolved coordinates'
            if spatial == 'diagnostic_only'
            else 'spatial comparison disabled: no explicit alignment or '
            'cross-revision registration exists for this pair'
        )
        return CaptureInboxComparison(
            lineage_digest=item.lineage_digest,
            predecessor_lineage_digest=(
                predecessor.lineage_digest if predecessor else None
            ),
            relationship=relationship,
            capture_revision_id=item.capture_revision_id,
            predecessor_capture_revision_id=(
                predecessor.capture_revision_id if predecessor else None
            ),
            bundle_digest_changed=(
                (predecessor.bundle_digest != item.bundle_digest)
                if predecessor
                else None
            ),
            source_evidence_count_self=len(plan.source_evidence),
            source_evidence_count_predecessor=(
                len(predecessor_plan.source_evidence)
                if predecessor_plan
                else None
            ),
            raw_mesh_count_self=len(plan.raw_visual_mesh_handoffs),
            raw_mesh_count_predecessor=(
                len(predecessor_plan.raw_visual_mesh_handoffs)
                if predecessor_plan
                else None
            ),
            authority_record_count_self=len(plan.authority_records),
            authority_record_count_predecessor=(
                len(predecessor_plan.authority_records)
                if predecessor_plan
                else None
            ),
            alignment_state=alignment_state,
            spatial_comparison=spatial,
            detail=detail,
        )


def run_capture_inbox_schema_convergence(
    connection: sqlite3.Connection,
) -> None:
    """Legacy-shape tail of the schema-authority migration (#302).

    ``ensure_native_schema`` invokes this while converging databases whose
    inbox items table still points its lineage key at the runs table; it
    runs the same sequence ``CaptureInboxRepository._initialize`` applies,
    without constructing a repository instance.
    """

    repository = CaptureInboxRepository.__new__(CaptureInboxRepository)
    repository._converge_schema(connection)
