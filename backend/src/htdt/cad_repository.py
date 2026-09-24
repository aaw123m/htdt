from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .cad_body_mesh import (
    resolve_document_mesh_bodies,
    upgrade_document_mesh_bodies,
)
from .cad_scene import SceneDocument, canonical_scene_json, scene_content_hash
from .cad_schema import (
    backfill_scene_document_heads,
    ensure_native_schema,
    require_native_tables,
)
from .content_blobs import (
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)


_LOGGER = logging.getLogger('htdt.native')

# Editor view state is disposable UI convenience state (selection, hidden and
# locked ids), not project truth. Read validation bounds each persisted id list
# generously above any real scene so that a corrupt or hostile row can only
# ever reset UI state, never abort document open or force unbounded allocation.
MAX_VIEW_STATE_ID_COUNT = 1_000_000


class SceneRevisionConflictError(ValueError):
    """A SceneRevision save violated the document's single-head lineage contract."""


@dataclass(frozen=True)
class SceneRevision:
    revision_id: str
    document_id: str
    parent_revision_id: str | None
    created_at_utc: str
    content_hash: str
    document: SceneDocument
    #: True when the revision is deliberate non-head lineage: it was written
    #: by ``save_detached_revision`` (or reconstructed as off-mainline during
    #: head migration) and never became the document's current head.
    detached: bool = False
    #: Optional provenance note for detached lineage (fixture, analytical
    #: candidate materialization, historical comparison, ...).
    detached_reason: str | None = None


@dataclass(frozen=True)
class SceneRevisionSummary:
    """Compact revision metadata for history browsing (#663).

    Carries lineage and identity only — the payload column is never read,
    so listing several hundred revisions stays O(metadata) rather than
    O(total project bytes). ``payload_bytes`` reports the stored payload
    size so surfaces can show relative revision weight without decoding.
    """

    revision_id: str
    document_id: str
    parent_revision_id: str | None
    created_at_utc: str
    content_hash: str
    detached: bool
    detached_reason: str | None
    payload_bytes: int


@dataclass(frozen=True)
class SaveResult:
    revision: SceneRevision
    created: bool


@dataclass(frozen=True)
class RecoverySnapshot:
    document_id: str
    source_revision_id: str | None
    updated_at_utc: str
    content_hash: str
    document: SceneDocument


@dataclass(frozen=True)
class EditorViewRecord:
    document_id: str
    selected_id: str | None
    selected_ids: tuple[str, ...]
    hidden_ids: tuple[str, ...]
    locked_ids: tuple[str, ...]
    object_snap_enabled: bool = True
    grid_snap_enabled: bool = False
    grid_step_m: float = 0.05
    angle_snap_enabled: bool = False
    angle_step_deg: float = 15.0


@dataclass(frozen=True)
class RevisionLabel:
    """Non-mutating history metadata: a label/note bound to a revision id."""

    revision_id: str
    label: str
    note: str
    updated_at_utc: str


@dataclass(frozen=True)
class EditorPayloadRecord:
    """One row of a non-authoritative, document-scoped payload store.

    Used for editor-convenience state that is never part of a SceneRevision:
    camera records, named views, floor-plan underlays, seating-layout specs
    and authoring-constraint sets. ``record_id`` equals the document id for
    singleton stores and the per-record id (view id, underlay id, spec id)
    for keyed stores.
    """

    document_id: str
    record_id: str
    payload: dict
    updated_at_utc: str


#: Non-authoritative payload tables and their keyed record column (``None``
#: for singleton tables with one row per document).
_PAYLOAD_TABLES: dict[str, tuple[str, str | None]] = {
    'camera_state': ('editor_camera_states', None),
    'named_view': ('editor_named_views', 'view_id'),
    'underlay': ('floor_plan_underlays', 'underlay_id'),
    'seating_spec': ('seating_layout_specs', 'spec_id'),
    'authoring_constraints': ('authoring_constraint_sets', None),
}

_MAX_PAYLOAD_ROWS_PER_DOCUMENT = 512


@dataclass(frozen=True)
class SemanticGeometryBindingRecord:
    scene_revision_id: str
    source_scene_revision_id: str | None
    geometry_id: str
    geometry_semantic_hash: str
    input_raw_mesh_id: str
    input_asset_sha256: str
    conversion_request_id: str


def _decode_view_state_ids(payload: object, *, field: str) -> tuple[str, ...]:
    """Decode one ``editor_view_states`` ``*_ids_json`` column.

    Raises ``ValueError`` when the payload is not a JSON array of strings or is
    absurdly large, so the caller can treat the whole row as corrupt.
    """
    value = json.loads(payload)
    if not isinstance(value, list):
        raise ValueError(f'{field} must be a JSON array')
    if len(value) > MAX_VIEW_STATE_ID_COUNT:
        raise ValueError(f'{field} exceeds {MAX_VIEW_STATE_ID_COUNT} ids')
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f'{field} must contain only string ids')
    return tuple(value)


class SceneRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            # Reconstruct explicit heads for databases written before the
            # head authority existed; a no-op once every document has one.
            backfill_scene_document_heads(connection)
            require_native_tables(
                connection,
                'scene_revisions',
                'scene_document_heads',
                'scene_recovery_snapshots',
                'editor_view_states',
                'editor_camera_states',
                'editor_named_views',
                'floor_plan_underlays',
                'seating_layout_specs',
                'authoring_constraint_sets',
            )

    def current_head(self, document_id: str) -> SceneRevision | None:
        """Return the document's explicit current head SceneRevision.

        This is the product authority for "the current Scene": the head only
        advances when a normal ``save`` commits a new mainline revision.
        Detached lineage written by ``save_detached_revision`` never appears
        here regardless of insertion order.
        """
        with closing(self._connect()) as connection, connection:
            row = self._head_revision_row(connection, document_id)
            if row is None:
                return None
            return self._row_to_revision(row, read_blob=self.read_blob)

    def latest(self, document_id: str) -> SceneRevision | None:
        """Compatibility alias for :meth:`current_head`.

        Before #626 this resolved ``ORDER BY seq DESC LIMIT 1``, which let a
        detached branch row hijack the current-document position merely by
        being newest. It now resolves the explicit head authority; use
        :meth:`most_recently_created_revision` for pure insertion chronology.
        """
        return self.current_head(document_id)

    def most_recently_created_revision(self, document_id: str) -> SceneRevision | None:
        """Return the newest revision by insertion order (chronology only).

        This is NOT the current-head authority — a detached revision may be
        newer than the head. Product code must consume :meth:`current_head`;
        this exists for rare chronology/diagnostic queries.
        """
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM scene_revisions WHERE document_id=? ORDER BY seq DESC LIMIT 1',
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_revision(row, read_blob=self.read_blob)

    @staticmethod
    def _head_revision_row(
        connection: sqlite3.Connection,
        document_id: str,
    ) -> sqlite3.Row | None:
        """Return the ``scene_revisions`` row of the document's current head."""
        return connection.execute(
            'SELECT r.* FROM scene_document_heads h '
            'JOIN scene_revisions r ON r.revision_id = h.head_revision_id '
            'WHERE h.document_id=?',
            (document_id,),
        ).fetchone()

    def get(self, revision_id: str) -> SceneRevision | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM scene_revisions WHERE revision_id=?',
                (revision_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_revision(row, read_blob=self.read_blob)

    def save(
        self,
        document: SceneDocument,
        *,
        parent_revision_id: str | None,
    ) -> SaveResult:
        """Persist one immutable SceneRevision as the document's current head.

        This is an optimistic compare-and-swap boundary on the single-head
        lineage: the first revision of a document requires
        ``parent_revision_id=None`` and no existing revision, and every later
        revision requires ``parent_revision_id`` to equal the document's
        explicit current head. The head check runs inside the same
        ``BEGIN IMMEDIATE`` transaction as the insert and the head advance,
        so two writers racing from the same head cannot both advance it.
        Violations raise ``SceneRevisionConflictError``.

        For deliberate non-head lineage that must not become the current
        document, use ``save_detached_revision``.
        """
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_in_transaction(
                connection,
                document,
                parent_revision_id=parent_revision_id,
            )

    def save_detached_revision(
        self,
        document: SceneDocument,
        *,
        parent_revision_id: str,
        reason: str | None = None,
    ) -> SaveResult:
        """Persist one immutable SceneRevision as detached, non-head lineage.

        The parent must exist and belong to the document, but it does not
        have to be the current head — detached revisions may descend from a
        historical revision (for example O50 measurement-plan fixtures that
        must descend directly from the SearchSpec source revision). The
        insert NEVER advances ``scene_document_heads``: ``current_head``
        keeps returning the true editing head and the detached row stays
        retrievable only by exact id via ``get``. A detached save also
        leaves the document's recovery snapshot untouched, since it does not
        change the editing session's committed source.

        ``reason`` records why the detached lineage exists (analytical
        candidate materialization, fixture/test, historical comparison) and
        is persisted on the revision for history/audit surfaces.
        """
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_in_transaction(
                connection,
                document,
                parent_revision_id=parent_revision_id,
                detached=True,
                detached_reason=reason,
            )

    def _save_in_transaction(
        self,
        connection: sqlite3.Connection,
        document: SceneDocument,
        *,
        parent_revision_id: str | None,
        revision_id: str | None = None,
        created_at_utc: str | None = None,
        detached: bool = False,
        detached_reason: str | None = None,
    ) -> SaveResult:
        """Persist one immutable SceneRevision inside the caller's transaction.

        This is the single SceneRevision write authority used by both normal saves
        and higher-level operations that must commit related lineage atomically.
        The caller owns BEGIN/COMMIT/ROLLBACK when passing an existing connection;
        the head check below must run under a held BEGIN IMMEDIATE so that racing
        writers serialize before compare-and-swap evaluation.

        Lineage contract: the first revision of a document requires
        ``parent_revision_id=None`` and no existing revision; every later
        current revision requires ``parent_revision_id`` to equal the
        document's explicit head (``scene_document_heads``). Stale-parent and
        duplicate-root saves raise ``SceneRevisionConflictError`` rather than
        silently branching history.

        ``detached=True`` records deliberate non-head lineage: the
        head-equality check is relaxed, the inserted row is flagged
        ``detached``/``detached_reason``, and the document head is NOT
        advanced, so detached revisions never become the current document
        regardless of insertion order. Detached inserts also leave recovery
        snapshots alone — they are not the editing session's committed
        source. A second root (``parent_revision_id=None`` with existing
        history) is always rejected, and a detached save always requires a
        parent.
        """

        # Issue #653: mesh body geometry persists as a compact BodyMeshReference
        # into the content-addressed blob store, never as inline vertex arrays
        # inside every immutable revision payload.
        ensure_content_blob_store(connection)
        document = upgrade_document_mesh_bodies(
            document,
            lambda payload: store_content_blob(connection, payload),
        )
        payload_json = canonical_scene_json(document)
        content_hash = scene_content_hash(document)
        head = self._head_revision_row(connection, document.document_id)
        parent = None
        if parent_revision_id is None:
            if detached:
                raise ValueError('detached SceneRevision requires a parent revision')
            has_history = head is not None or connection.execute(
                'SELECT 1 FROM scene_revisions WHERE document_id=? LIMIT 1',
                (document.document_id,),
            ).fetchone() is not None
            if has_history:
                raise SceneRevisionConflictError(
                    'duplicate root SceneRevision rejected: document '
                    f'{document.document_id} already has revision history'
                )
        else:
            parent = connection.execute(
                'SELECT * FROM scene_revisions WHERE revision_id=?',
                (parent_revision_id,),
            ).fetchone()
            if parent is None:
                raise ValueError(f'unknown parent revision: {parent_revision_id}')
            if parent['document_id'] != document.document_id:
                raise ValueError('parent revision belongs to a different document')
            if not detached and (
                head is None or head['revision_id'] != parent_revision_id
            ):
                raise SceneRevisionConflictError(
                    f'stale parent SceneRevision: {parent_revision_id} is not the '
                    f'current head of document {document.document_id}'
                )
            if parent['content_hash'] == content_hash:
                if not detached:
                    connection.execute(
                        'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
                        (document.document_id,),
                    )
                return SaveResult(
                    self._row_to_revision(
                        parent,
                        read_blob=lambda sha: read_content_blob(connection, sha),
                    ),
                    created=False,
                )

        geometry = document.r120_semantic_geometry
        if geometry is not None:
            if parent is None:
                if geometry.source_scene_revision_id is not None:
                    raise ValueError('root SceneRevision semantic geometry must have no source revision')
            else:
                parent_document = SceneDocument.model_validate(json.loads(parent['payload_json']))
                parent_geometry = parent_document.r120_semantic_geometry
                geometry_changed = (
                    parent_geometry is None or parent_geometry.geometry_id != geometry.geometry_id
                )
                if geometry_changed and geometry.source_scene_revision_id != parent_revision_id:
                    raise ValueError(
                        'new R120 semantic geometry must bind to the exact parent SceneRevision'
                    )

        revision_id = revision_id or str(uuid4())
        created_at = created_at_utc or datetime.now(timezone.utc).isoformat()
        connection.execute(
            '''
            INSERT INTO scene_revisions(
                revision_id, document_id, parent_revision_id, created_at_utc,
                content_hash, payload_json, detached, detached_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                revision_id,
                document.document_id,
                parent_revision_id,
                created_at,
                content_hash,
                payload_json,
                1 if detached else 0,
                detached_reason if detached else None,
            ),
        )
        if detached:
            # Detached lineage never advances the document head and never
            # disturbs the editing session's recovery snapshot: it is not
            # the document's current state.
            pass
        else:
            connection.execute(
                'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
                (document.document_id,),
            )
            connection.execute(
                '''
                INSERT INTO scene_document_heads(
                    document_id, head_revision_id, updated_at_utc, generation
                ) VALUES (?, ?, ?, 1)
                ON CONFLICT(document_id) DO UPDATE SET
                    head_revision_id=excluded.head_revision_id,
                    updated_at_utc=excluded.updated_at_utc,
                    generation=scene_document_heads.generation + 1
                ''',
                (document.document_id, revision_id, created_at),
            )
        row = connection.execute(
            'SELECT * FROM scene_revisions WHERE revision_id=?',
            (revision_id,),
        ).fetchone()
        return SaveResult(
            self._row_to_revision(
                row,
                read_blob=lambda sha: read_content_blob(connection, sha),
            ),
            created=True,
        )

    def semantic_geometry_binding(self, revision_id: str) -> SemanticGeometryBindingRecord | None:
        revision = self.get(revision_id)
        if revision is None or revision.document.r120_semantic_geometry is None:
            return None
        geometry = revision.document.r120_semantic_geometry
        return SemanticGeometryBindingRecord(
            scene_revision_id=revision.revision_id,
            source_scene_revision_id=geometry.source_scene_revision_id,
            geometry_id=geometry.geometry_id,
            geometry_semantic_hash=geometry.semantic_hash_sha256,
            input_raw_mesh_id=geometry.input_raw_mesh_id,
            input_asset_sha256=geometry.input_asset_sha256,
            conversion_request_id=geometry.conversion_request_id,
        )

    def save_recovery(
        self,
        document: SceneDocument,
        *,
        source_revision_id: str | None,
    ) -> RecoverySnapshot | None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_content_blob_store(connection)
            document = upgrade_document_mesh_bodies(
                document,
                lambda payload: store_content_blob(connection, payload),
            )
            payload_json = canonical_scene_json(document)
            content_hash = scene_content_hash(document)
            if source_revision_id is not None:
                source = connection.execute(
                    'SELECT * FROM scene_revisions WHERE revision_id=?',
                    (source_revision_id,),
                ).fetchone()
                if source is None:
                    raise ValueError(f'unknown recovery source revision: {source_revision_id}')
                if source['document_id'] != document.document_id:
                    raise ValueError('recovery source belongs to a different document')
                if source['content_hash'] == content_hash:
                    connection.execute(
                        'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
                        (document.document_id,),
                    )
                    return None
            connection.execute(
                '''
                INSERT INTO scene_recovery_snapshots(
                    document_id, source_revision_id, updated_at_utc, content_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    source_revision_id=excluded.source_revision_id,
                    updated_at_utc=excluded.updated_at_utc,
                    content_hash=excluded.content_hash,
                    payload_json=excluded.payload_json
                ''',
                (document.document_id, source_revision_id, updated_at, content_hash, payload_json),
            )
        return RecoverySnapshot(
            document_id=document.document_id,
            source_revision_id=source_revision_id,
            updated_at_utc=updated_at,
            content_hash=content_hash,
            document=document,
        )

    def recovery(self, document_id: str) -> RecoverySnapshot | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM scene_recovery_snapshots WHERE document_id=?',
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        document = SceneDocument.model_validate(json.loads(row['payload_json']))
        content_hash = scene_content_hash(document)
        if content_hash != row['content_hash']:
            raise ValueError(f'recovery snapshot hash mismatch: {document_id}')
        document = resolve_document_mesh_bodies(document, self.read_blob)
        return RecoverySnapshot(
            document_id=row['document_id'],
            source_revision_id=row['source_revision_id'],
            updated_at_utc=row['updated_at_utc'],
            content_hash=row['content_hash'],
            document=document,
        )

    def clear_recovery(self, document_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
                (document_id,),
            )

    def store_blob(self, payload: bytes) -> str:
        """Persist immutable bytes in the project content-addressed blob store.

        Returns the SHA-256 digest that authoritatively identifies the stored
        payload (Issue-464 mesh body assets use this for exact provenance).
        """

        with closing(self._connect()) as connection, connection:
            ensure_content_blob_store(connection)
            return store_content_blob(connection, payload)

    def read_blob(self, payload_sha256: str) -> bytes | None:
        """Return canonical blob bytes for a digest, or ``None`` when absent."""

        with closing(self._connect()) as connection:
            ensure_content_blob_store(connection)
            return read_content_blob(connection, payload_sha256)

    def save_view_state(
        self,
        document_id: str,
        *,
        selected_id: str | None,
        selected_ids: tuple[str, ...] | list[str] | None = None,
        hidden_ids: set[str],
        locked_ids: set[str],
        object_snap_enabled: bool | None = None,
        grid_snap_enabled: bool | None = None,
        grid_step_m: float | None = None,
        angle_snap_enabled: bool | None = None,
        angle_step_deg: float | None = None,
    ) -> None:
        ordered_selected = list(dict.fromkeys(selected_ids or (() if selected_id is None else (selected_id,))))
        if selected_id is not None and selected_id not in ordered_selected:
            ordered_selected.append(selected_id)
        selected_json = json.dumps(ordered_selected, separators=(',', ':'))
        hidden_json = json.dumps(sorted(hidden_ids), separators=(',', ':'))
        locked_json = json.dumps(sorted(locked_ids), separators=(',', ':'))
        snap_json = None
        if any(
            value is not None
            for value in (
                object_snap_enabled,
                grid_snap_enabled,
                grid_step_m,
                angle_snap_enabled,
                angle_step_deg,
            )
        ):
            snap_json = json.dumps(
                {
                    'object_snap_enabled': bool(object_snap_enabled),
                    'grid_snap_enabled': bool(grid_snap_enabled),
                    'grid_step_m': float(grid_step_m),
                    'angle_snap_enabled': bool(angle_snap_enabled),
                    'angle_step_deg': float(angle_step_deg),
                },
                separators=(',', ':'),
                allow_nan=False,
            )
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            if snap_json is None:
                connection.execute(
                    '''
                    INSERT INTO editor_view_states(
                        document_id, selected_id, selected_ids_json, hidden_ids_json, locked_ids_json, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        selected_id=excluded.selected_id,
                        selected_ids_json=excluded.selected_ids_json,
                        hidden_ids_json=excluded.hidden_ids_json,
                        locked_ids_json=excluded.locked_ids_json,
                        updated_at_utc=excluded.updated_at_utc
                    ''',
                    (document_id, selected_id, selected_json, hidden_json, locked_json, updated_at),
                )
            else:
                connection.execute(
                    '''
                    INSERT INTO editor_view_states(
                        document_id, selected_id, selected_ids_json, hidden_ids_json, locked_ids_json, snap_json, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        selected_id=excluded.selected_id,
                        selected_ids_json=excluded.selected_ids_json,
                        hidden_ids_json=excluded.hidden_ids_json,
                        locked_ids_json=excluded.locked_ids_json,
                        snap_json=excluded.snap_json,
                        updated_at_utc=excluded.updated_at_utc
                    ''',
                    (
                        document_id,
                        selected_id,
                        selected_json,
                        hidden_json,
                        locked_json,
                        snap_json,
                        updated_at,
                    ),
                )

    def view_state(self, document_id: str) -> EditorViewRecord | None:
        """Return persisted editor view state, or ``None`` when absent or corrupt.

        ``editor_view_states`` is non-authoritative UI state, so this read
        boundary fails soft: a malformed row is discarded and reported through
        the diagnostics log, and callers see ``None`` (default view state) while
        the underlying SceneRevision stays untouched. Authoritative stores such
        as ``scene_revisions`` keep failing closed on corruption.
        """
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM editor_view_states WHERE document_id=?',
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                return self._row_to_view_state(row)
            except (TypeError, ValueError, RecursionError) as exc:
                _LOGGER.warning(
                    'discarding corrupt editor view state for document %s; '
                    'resetting UI state to defaults (%s)',
                    document_id,
                    exc,
                )
                self._discard_view_state(connection, document_id)
                return None

    @staticmethod
    def _row_to_view_state(row: sqlite3.Row) -> EditorViewRecord:
        selected_id = row['selected_id']
        if selected_id is not None and not isinstance(selected_id, str):
            raise ValueError('selected_id is not a string')
        selected = _decode_view_state_ids(row['selected_ids_json'], field='selected_ids_json')
        if not selected and selected_id is not None:
            selected = (selected_id,)
        hidden = _decode_view_state_ids(row['hidden_ids_json'], field='hidden_ids_json')
        locked = _decode_view_state_ids(row['locked_ids_json'], field='locked_ids_json')
        snap_raw = row['snap_json'] if 'snap_json' in row.keys() else None
        snap: dict[str, Any] = {}
        if snap_raw:
            try:
                payload = json.loads(snap_raw)
            except ValueError as exc:
                raise ValueError('snap_json is not valid JSON') from exc
            if isinstance(payload, dict):
                snap = payload
        return EditorViewRecord(
            document_id=row['document_id'],
            selected_id=selected_id,
            selected_ids=selected,
            hidden_ids=hidden,
            locked_ids=locked,
            object_snap_enabled=bool(snap.get('object_snap_enabled', True)),
            grid_snap_enabled=bool(snap.get('grid_snap_enabled', False)),
            grid_step_m=float(snap.get('grid_step_m', 0.05)),
            angle_snap_enabled=bool(snap.get('angle_snap_enabled', False)),
            angle_step_deg=float(snap.get('angle_step_deg', 15.0)),
        )

    def list_revisions(self, document_id: str) -> tuple[SceneRevision, ...]:
        """Return every revision of the document in chronological order.

        Includes detached lineage (marked via ``SceneRevision.detached``) so
        history surfaces can show the true recorded lineage rather than only
        the mainline chain. Head state is resolved by callers through
        ``current_head`` — this query is deliberately lineage-exhaustive.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM scene_revisions WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
            return tuple(
                self._row_to_revision(row, read_blob=self.read_blob) for row in rows
            )

    def list_revision_summaries(
        self, document_id: str
    ) -> tuple[SceneRevisionSummary, ...]:
        """Compact metadata index for every revision, chronological (#663).

        The payload column is deliberately not selected: history surfaces
        must not deserialize every full SceneDocument just to render a
        revision list — the same lineage-exhaustive row set
        :meth:`list_revisions` returns, minus the decode.
        """
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT revision_id, document_id, parent_revision_id, '
                'created_at_utc, content_hash, detached, detached_reason, '
                'length(payload_json) AS payload_bytes '
                'FROM scene_revisions WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
            return tuple(
                SceneRevisionSummary(
                    revision_id=row['revision_id'],
                    document_id=row['document_id'],
                    parent_revision_id=row['parent_revision_id'],
                    created_at_utc=row['created_at_utc'],
                    content_hash=row['content_hash'],
                    detached=bool(row['detached']),
                    detached_reason=row['detached_reason'],
                    payload_bytes=int(row['payload_bytes']),
                )
                for row in rows
            )

    def _ensure_revision_labels(self, connection) -> None:
        # The table is part of the canonical contract; verify instead of
        # lazily creating it (#302).
        require_native_tables(connection, 'scene_revision_labels')

    def set_revision_label(
        self,
        revision_id: str,
        *,
        label: str,
        note: str = '',
    ) -> RevisionLabel:
        """Attach or update a human label on a revision without mutating history.

        Labels live in ``scene_revision_labels`` — the revision payload, hash
        and lineage stay byte-exact, so labelling never changes what a
        revision IS, only what it is called (#485).
        """
        label = label.strip()
        if not label:
            raise ValueError('revision label must not be empty')
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT document_id FROM scene_revisions WHERE revision_id=?',
                (revision_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f'unknown revision: {revision_id}')
            self._ensure_revision_labels(connection)
            connection.execute(
                '''
                INSERT INTO scene_revision_labels(revision_id, document_id, label, note, updated_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(revision_id) DO UPDATE SET
                    label=excluded.label,
                    note=excluded.note,
                    updated_at_utc=excluded.updated_at_utc
                ''',
                (revision_id, row['document_id'], label, note, updated_at),
            )
        return RevisionLabel(
            revision_id=revision_id,
            label=label,
            note=note,
            updated_at_utc=updated_at,
        )

    def clear_revision_label(self, revision_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            self._ensure_revision_labels(connection)
            connection.execute(
                'DELETE FROM scene_revision_labels WHERE revision_id=?',
                (revision_id,),
            )

    def revision_labels(self, document_id: str) -> dict[str, RevisionLabel]:
        with closing(self._connect()) as connection, connection:
            self._ensure_revision_labels(connection)
            rows = connection.execute(
                'SELECT * FROM scene_revision_labels WHERE document_id=?',
                (document_id,),
            ).fetchall()
            return {
                row['revision_id']: RevisionLabel(
                    revision_id=row['revision_id'],
                    label=row['label'],
                    note=row['note'],
                    updated_at_utc=row['updated_at_utc'],
                )
                for row in rows
            }

    @staticmethod
    def _discard_view_state(connection: sqlite3.Connection, document_id: str) -> None:
        """Best-effort delete of a corrupt view-state row; never raises."""
        try:
            connection.execute(
                'DELETE FROM editor_view_states WHERE document_id=?',
                (document_id,),
            )
        except sqlite3.Error:
            _LOGGER.warning(
                'could not delete corrupt editor view state row for document %s',
                document_id,
            )

    # -- non-authoritative payload stores (camera, named views, underlays,
    # seating specs, authoring constraints) -------------------------------

    def _save_payload(
        self,
        store: str,
        document_id: str,
        payload: dict,
        *,
        record_id: str | None = None,
    ) -> None:
        table, key_column = _PAYLOAD_TABLES[store]
        if key_column is None and record_id is not None:
            raise ValueError(f'{store} store is singleton; no record_id allowed')
        if key_column is not None and record_id is None:
            raise ValueError(f'{store} store requires a record_id')
        key = document_id if key_column is None else record_id
        updated_at = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        with closing(self._connect()) as connection, connection:
            if key_column is None:
                existing = connection.execute(
                    f'SELECT COUNT(*) AS n FROM {table} WHERE document_id=?',
                    (document_id,),
                ).fetchone()['n']
                if existing:
                    connection.execute(
                        f'UPDATE {table} SET payload_json=?, updated_at_utc=? WHERE document_id=?',
                        (payload_json, updated_at, document_id),
                    )
                    return
            else:
                count_row = connection.execute(
                    f'SELECT COUNT(*) AS n FROM {table} WHERE document_id=? AND {key_column}!=?',
                    (document_id, key),
                ).fetchone()
                if count_row['n'] >= _MAX_PAYLOAD_ROWS_PER_DOCUMENT:
                    raise ValueError(
                        f'{store} store is at its {_MAX_PAYLOAD_ROWS_PER_DOCUMENT}-record bound'
                    )
            if key_column is None:
                connection.execute(
                    f'INSERT INTO {table}(document_id, payload_json, updated_at_utc) VALUES (?, ?, ?)',
                    (document_id, payload_json, updated_at),
                )
            else:
                connection.execute(
                    f'INSERT INTO {table}(document_id, {key_column}, payload_json, updated_at_utc) '
                    'VALUES (?, ?, ?, ?) '
                    f'ON CONFLICT(document_id, {key_column}) DO UPDATE SET '
                    'payload_json=excluded.payload_json, updated_at_utc=excluded.updated_at_utc',
                    (document_id, key, payload_json, updated_at),
                )

    def _payloads(self, store: str, document_id: str) -> tuple[EditorPayloadRecord, ...]:
        table, key_column = _PAYLOAD_TABLES[store]
        with closing(self._connect()) as connection, connection:
            if key_column is None:
                rows = connection.execute(
                    f'SELECT document_id, NULL AS record_id, payload_json, updated_at_utc '
                    f'FROM {table} WHERE document_id=?',
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f'SELECT document_id, {key_column} AS record_id, payload_json, updated_at_utc '
                    f'FROM {table} WHERE document_id=? ORDER BY updated_at_utc',
                    (document_id,),
                ).fetchall()
            records: list[EditorPayloadRecord] = []
            corrupt = False
            for row in rows:
                try:
                    payload = json.loads(row['payload_json'])
                    if not isinstance(payload, dict):
                        raise ValueError('payload is not a JSON object')
                except (TypeError, ValueError, RecursionError) as exc:
                    # Same contract as editor_view_states: a corrupt row of
                    # disposable editor state resets to defaults, never aborts.
                    _LOGGER.warning(
                        'discarding corrupt %s row for document %s (%s)',
                        store,
                        document_id,
                        exc,
                    )
                    corrupt = True
                    continue
                records.append(
                    EditorPayloadRecord(
                        document_id=row['document_id'],
                        record_id=row['record_id'] or row['document_id'],
                        payload=payload,
                        updated_at_utc=row['updated_at_utc'],
                    )
                )
            if corrupt:
                try:
                    connection.execute(
                        f'DELETE FROM {table} WHERE document_id=?',
                        (document_id,),
                    )
                except sqlite3.Error:
                    _LOGGER.warning(
                        'could not purge corrupt %s rows for document %s',
                        store,
                        document_id,
                    )
            return tuple(records)

    def _delete_payload(self, store: str, document_id: str, record_id: str) -> None:
        table, key_column = _PAYLOAD_TABLES[store]
        if key_column is None:
            raise ValueError(f'{store} store is singleton; use _clear_payload')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'DELETE FROM {table} WHERE document_id=? AND {key_column}=?',
                (document_id, record_id),
            )

    # Camera record --------------------------------------------------------

    def save_camera_state(self, document_id: str, payload: dict) -> None:
        self._save_payload('camera_state', document_id, payload)

    def camera_state(self, document_id: str) -> EditorPayloadRecord | None:
        records = self._payloads('camera_state', document_id)
        return records[0] if records else None

    # Named views ----------------------------------------------------------

    def save_named_view(self, document_id: str, view_id: str, payload: dict) -> None:
        self._save_payload('named_view', document_id, payload, record_id=view_id)

    def named_views(self, document_id: str) -> tuple[EditorPayloadRecord, ...]:
        return self._payloads('named_view', document_id)

    def delete_named_view(self, document_id: str, view_id: str) -> None:
        self._delete_payload('named_view', document_id, view_id)

    # Floor-plan underlays ---------------------------------------------------

    def save_underlay(self, document_id: str, underlay_id: str, payload: dict) -> None:
        self._save_payload('underlay', document_id, payload, record_id=underlay_id)

    def underlays(self, document_id: str) -> tuple[EditorPayloadRecord, ...]:
        return self._payloads('underlay', document_id)

    def delete_underlay(self, document_id: str, underlay_id: str) -> None:
        self._delete_payload('underlay', document_id, underlay_id)

    # Seating layout specs ---------------------------------------------------

    def save_seating_spec(self, document_id: str, spec_id: str, payload: dict) -> None:
        self._save_payload('seating_spec', document_id, payload, record_id=spec_id)

    def seating_specs(self, document_id: str) -> tuple[EditorPayloadRecord, ...]:
        return self._payloads('seating_spec', document_id)

    def delete_seating_spec(self, document_id: str, spec_id: str) -> None:
        self._delete_payload('seating_spec', document_id, spec_id)

    # Authoring constraints ---------------------------------------------------

    def save_authoring_constraints(self, document_id: str, payload: dict) -> None:
        self._save_payload('authoring_constraints', document_id, payload)

    def authoring_constraints(self, document_id: str) -> EditorPayloadRecord | None:
        records = self._payloads('authoring_constraints', document_id)
        return records[0] if records else None

    @staticmethod
    def _row_to_revision(
        row: sqlite3.Row,
        *,
        read_blob,
    ) -> SceneRevision:
        document = SceneDocument.model_validate(json.loads(row['payload_json']))
        content_hash = scene_content_hash(document)
        if content_hash != row['content_hash']:
            raise ValueError(f"scene revision hash mismatch: {row['revision_id']}")
        if document.document_id != row['document_id']:
            raise ValueError(
                f"scene revision document mismatch: {row['revision_id']}"
            )
        # Issue #653: rehydrate the in-memory mesh cache from the blob store;
        # the persisted payload only carries the compact reference. A missing
        # or corrupt blob fails closed rather than rendering silently.
        document = resolve_document_mesh_bodies(document, read_blob)
        return SceneRevision(
            revision_id=row['revision_id'],
            document_id=row['document_id'],
            parent_revision_id=row['parent_revision_id'],
            created_at_utc=row['created_at_utc'],
            content_hash=row['content_hash'],
            document=document,
            detached=bool(row['detached']),
            detached_reason=row['detached_reason'],
        )
