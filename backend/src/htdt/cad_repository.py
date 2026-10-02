from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable
import weakref
from uuid import uuid4

from .cad_body_mesh import (
    resolve_document_mesh_bodies,
    upgrade_document_mesh_bodies,
)
from .cad_scene import SceneDocument, canonical_scene_json, scene_content_hash
from .canonical_json import canonical_sha256
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,
)
from .content_blobs import (
    content_blob_exists,
    ensure_content_blob_store,
    read_content_blob,
    store_content_blob,
)


_LOGGER = logging.getLogger('htdt.native')


class _SharedReadConnection:
    """Long-lived read connection proxy whose ``close()`` is a no-op.

    Read paths hold it through ``with closing(self._read())`` and the sqlite
    context-manager exit commits — both are harmless on a connection that
    only ever runs SELECTs (python's sqlite3 opens an implicit transaction
    only on writes). Keeping it open avoids the per-call ``sqlite3.connect``
    cost; it is used from a single thread, though a repository may close it
    from another thread when a file-swap layer needs the handle released.

    ``_borrow_lock`` serializes each ``with`` borrow against a cross-thread
    release: sqlite frees are not safe while another thread is inside
    ``execute``/``fetch`` on the same handle (use-after-free, observed as an
    interpreter access violation), so a release waits for the in-flight
    borrow to unwind instead of closing mid-read. A proxy already handed
    out when its slot is released re-opens lazily on next use — the pool
    slot was cleared, so the reopened handle is a one-off for that borrow.
    """

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect_factory = connect
        self._inner = connect()
        self._borrow_lock = threading.RLock()
        self._closed = False

    def __getattr__(self, name):
        if self._closed:
            with self._borrow_lock:
                if self._closed:
                    self._inner = self._connect_factory()
                    self._closed = False
        return getattr(self._inner, name)

    def __enter__(self):
        self._borrow_lock.acquire()
        try:
            if self._closed:
                self._inner = self._connect_factory()
                self._closed = False
            self._inner.__enter__()
        except BaseException:
            self._borrow_lock.release()
            raise
        return self._inner

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._inner.__exit__(exc_type, exc, tb)
        finally:
            self._borrow_lock.release()

    def close(self) -> None:
        pass

    def _close_for_release(self) -> None:
        """Close the inner handle once any in-flight borrow unwinds."""
        with self._borrow_lock:
            self._closed = True
            try:
                self._inner.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self._inner.close()
        except Exception:
            pass


# File-swap layers (backup restore, managed-data relocation) cannot
# replace or unlink a sqlite file while ANY connection holds it — on
# Windows the os.replace/unlink fails with WinError 32. Every live
# repository registers here so those layers can force pooled read
# handles closed before mutating the managed tree.
_LIVE_REPOSITORIES: weakref.WeakSet = weakref.WeakSet()


def release_read_handles_under(data_dir: Path) -> int:
    """Close every live repository's pooled read connections under ``data_dir``.

    Reads re-open lazily on the next call, so this is safe to run from a
    restore/relocation worker for connections another thread created.
    Returns the number of repositories released.
    """
    root = Path(data_dir).resolve()
    released = 0
    for repository in list(_LIVE_REPOSITORIES):
        try:
            repository_path = repository.path.resolve()
        except Exception:
            continue
        if repository_path == root or root in repository_path.parents:
            repository.close()
            released += 1
    return released


# Editor view state is disposable UI convenience state (selection, hidden and
# locked ids), not project truth. Read validation bounds each persisted id list
# generously above any real scene so that a corrupt or hostile row can only
# ever reset UI state, never abort document open or force unbounded allocation.
MAX_VIEW_STATE_ID_COUNT = 1_000_000


class SceneRevisionConflictError(ValueError):
    """A SceneRevision save violated the document's single-head lineage contract."""


class SceneDocumentHeadIntegrityError(ValueError):
    """The persisted head pointer references a revision that does not exist.

    Every write path updates ``scene_document_heads`` and
    ``scene_revisions`` inside the same transaction and every deletion
    path removes both, so a head row whose ``head_revision_id`` resolves
    to no revision is store corruption. Reading it as head-less would
    silently fork the document's lineage authority; fail closed instead.
    """


class SceneRevisionIntegrityError(ValueError):
    """A persisted scene revision row is unreadable or fails integrity.

    Scene revision payloads carry a stored content hash verified on every
    read; a row whose payload cannot be parsed or whose hash does not
    match must never hydrate into plausible-but-wrong scene data. Fail
    closed and name the revision instead of surfacing a bare decode error.
    """


class AuthoringConstraintIntegrityError(ValueError):
    """The persisted authoring-constraint authority is unreadable.

    Raised instead of silently substituting an empty set (#843): corrupt
    authority stays retained and blocks mutation until the user repairs it.
    """


class AuthoringConstraintConflictError(ValueError):
    """The constraint head moved between pre-lock resolution and commit.

    A save resolving a stale head would insert a revision that supersedes
    the wrong parent and overwrite a live head pointer, silently forking
    the lineage; the conflict refuses the write instead.
    """


def _constraint_revision_sha256(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)


@dataclass(frozen=True)
class AuthoringConstraintRevision:
    """One immutable version of the document's authoring-constraint set (#843).

    Each save appends a sealed revision that binds the constraint payload to
    the SceneRevision it was authored under and to its predecessor — a
    historical design state resolves the exact authority that governed it,
    never a mutable singleton.
    """

    constraint_revision_id: str
    document_id: str
    supersedes_id: str | None
    scene_revision_id: str | None
    payload: dict
    created_at_utc: str
    constraint_revision_sha256: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            'constraint_revision_id': self.constraint_revision_id,
            'document_id': self.document_id,
            'supersedes_id': self.supersedes_id,
            'scene_revision_id': self.scene_revision_id,
            'payload': self.payload,
            'created_at_utc': self.created_at_utc,
        }

    @classmethod
    def build(
        cls,
        *,
        document_id: str,
        payload: dict,
        supersedes_id: str | None,
        scene_revision_id: str | None,
        constraint_revision_id: str | None = None,
    ) -> 'AuthoringConstraintRevision':
        revision = cls(
            constraint_revision_id=constraint_revision_id or str(uuid4()),
            document_id=document_id,
            supersedes_id=supersedes_id,
            scene_revision_id=scene_revision_id,
            payload=payload,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            constraint_revision_sha256='0' * 64,
        )
        return cls(
            constraint_revision_id=revision.constraint_revision_id,
            document_id=revision.document_id,
            supersedes_id=revision.supersedes_id,
            scene_revision_id=revision.scene_revision_id,
            payload=revision.payload,
            created_at_utc=revision.created_at_utc,
            constraint_revision_sha256=_constraint_revision_sha256(
                revision.identity_payload()
            ),
        )


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
        # Bounded parse memo for get(): the row is re-fetched on every call
        # so deletes and writes stay observable — only the deserialization
        # (row bytes -> SceneRevision) is memoized, keyed by the full row
        # content. Immutable append-only rows make that a pure function, and
        # list surfaces re-hydrate the same rows per row. LRU past the cap.
        self._revision_cache: OrderedDict[
            str, tuple[tuple, SceneRevision]
        ] = OrderedDict()
        # Per-thread shared read connections: sqlite3.connect() costs ~2-4ms
        # per open on Windows (file open + page cache init), and the hot
        # read paths below open one per call. Reads never hold a
        # transaction (python's sqlite3 only auto-begins on writes), so the
        # `, connection:` commit on exit is a no-op on these. Writes keep
        # _connect() — a fresh BEGIN IMMEDIATE boundary each time.
        # The table is keyed by thread ident (not held on the thread) and
        # connections opt out of same-thread checking so close() — and
        # release_read_handles_under — can release them from a restore or
        # relocation worker when the swap needs the file handles back.
        self._read_connections: dict[int, _SharedReadConnection] = {}
        self._read_connections_lock = threading.Lock()
        _LIVE_REPOSITORIES.add(self)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _read(self) -> sqlite3.Connection:
        ident = threading.get_ident()
        with self._read_connections_lock:
            connection = self._read_connections.get(ident)
            if connection is None:
                connection = _SharedReadConnection(
                    lambda: connect_sqlite(self.path, check_same_thread=False)
                )
                self._read_connections[ident] = connection
        return connection

    def close(self) -> None:
        """Release every pooled read connection this repository holds.

        Owners that audit a throwaway database clone — and file-swap layers
        such as native backup restore — call this to get the file handles
        back before replacing or unlinking the database; Windows refuses
        either while a connection holds the file. It may be called from
        any thread and reads re-open lazily on next use.
        """
        with self._read_connections_lock:
            connections = list(self._read_connections.values())
            self._read_connections.clear()
        for connection in connections:
            connection._close_for_release()

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
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
                'authoring_constraint_revisions',
            )

    def current_head(self, document_id: str) -> SceneRevision | None:
        """Return the document's explicit current head SceneRevision.

        This is the product authority for "the current Scene": the head only
        advances when a normal ``save`` commits a new mainline revision.
        Detached lineage written by ``save_detached_revision`` never appears
        here regardless of insertion order.
        """
        with closing(self._read()) as connection, connection:
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
        with closing(self._read()) as connection, connection:
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
        """Return the ``scene_revisions`` row of the document's current head.

        A head row that joins to no revision can only be corruption —
        write and delete paths keep the two tables consistent in one
        transaction. Returning ``None`` there would read the document as
        head-less and let the next save fork a second root; raise instead.
        """
        row = connection.execute(
            'SELECT r.* FROM scene_document_heads h '
            'JOIN scene_revisions r ON r.revision_id = h.head_revision_id '
            'WHERE h.document_id=?',
            (document_id,),
        ).fetchone()
        if row is not None:
            return row
        head = connection.execute(
            'SELECT head_revision_id FROM scene_document_heads '
            'WHERE document_id=?',
            (document_id,),
        ).fetchone()
        if head is None:
            return None
        raise SceneDocumentHeadIntegrityError(
            f'scene_document_heads row for document {document_id} points at '
            f'missing scene_revision {head["head_revision_id"]}; the stored '
            'head authority is corrupt'
        )

    #: Upper bound on the per-instance revision memo in ``get``.
    _REVISION_CACHE_LIMIT = 1024

    def get(self, revision_id: str) -> SceneRevision | None:
        with closing(self._read()) as connection, connection:
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
        # scene_content_hash(document) is sha256 over this same canonical
        # string — hashing the payload directly avoids a second full
        # serialization.
        content_hash = hashlib.sha256(payload_json.encode('utf-8')).hexdigest()
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
                    # A geometry restored from history keeps the source pointer
                    # it was committed with — that pointer records where the
                    # geometry was derived, not this commit's parent. It stays
                    # truthful as long as the identical geometry already exists
                    # in this document's lineage (geometry_id hashes the whole
                    # self-verifying core, source pointer included). Only a
                    # genuinely NEW derivation must bind to the parent.
                    reintroduced = connection.execute(
                        'SELECT 1 FROM scene_revisions WHERE document_id=? '
                        "AND json_extract(payload_json, "
                        "'$.r120_semantic_geometry.geometry_id')=? LIMIT 1",
                        (document.document_id, geometry.geometry_id),
                    ).fetchone()
                    if reintroduced is None:
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
            content_hash = hashlib.sha256(payload_json.encode('utf-8')).hexdigest()
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
        with closing(self._read()) as connection, connection:
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

    def has_blob(self, payload_sha256: str) -> bool:
        """Cheap presence probe — callers use it to report missing data
        honestly without paying for a full verified read."""

        with closing(self._connect()) as connection:
            ensure_content_blob_store(connection)
            return content_blob_exists(connection, payload_sha256)

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
        selected_json = json.dumps(ordered_selected, separators=(',', ':'), allow_nan=False)
        hidden_json = json.dumps(sorted(hidden_ids), separators=(',', ':'), allow_nan=False)
        locked_json = json.dumps(sorted(locked_ids), separators=(',', ':'), allow_nan=False)
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
                    'object_snap_enabled': (
                        bool(object_snap_enabled)
                        if object_snap_enabled is not None
                        else True
                    ),
                    'grid_snap_enabled': (
                        bool(grid_snap_enabled)
                        if grid_snap_enabled is not None
                        else False
                    ),
                    'grid_step_m': (
                        float(grid_step_m)
                        if grid_step_m is not None
                        else 0.05
                    ),
                    'angle_snap_enabled': (
                        bool(angle_snap_enabled)
                        if angle_snap_enabled is not None
                        else False
                    ),
                    'angle_step_deg': (
                        float(angle_step_deg)
                        if angle_step_deg is not None
                        else 15.0
                    ),
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
        with closing(self._read()) as connection, connection:
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
        with closing(self._read()) as connection, connection:
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
        with closing(self._read()) as connection, connection:
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
        with closing(self._read()) as connection, connection:
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
        payload_json = json.dumps(payload, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
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
                    f'FROM {table} WHERE document_id=? ORDER BY updated_at_utc, {key_column}',
                    (document_id,),
                ).fetchall()
            records: list[EditorPayloadRecord] = []
            corrupt_record_ids: list[str] = []
            for row in rows:
                try:
                    payload = json.loads(row['payload_json'])
                    if not isinstance(payload, dict):
                        raise ValueError('payload is not a JSON object')
                except (TypeError, ValueError, RecursionError) as exc:
                    # Same contract as editor_view_states: a corrupt row of
                    # disposable editor state resets to defaults, never aborts.
                    record_id = row['record_id'] or row['document_id']
                    _LOGGER.warning(
                        'discarding corrupt %s record %s for document %s (%s)',
                        store,
                        record_id,
                        document_id,
                        exc,
                    )
                    corrupt_record_ids.append(record_id)
                    continue
                records.append(
                    EditorPayloadRecord(
                        document_id=row['document_id'],
                        record_id=row['record_id'] or row['document_id'],
                        payload=payload,
                        updated_at_utc=row['updated_at_utc'],
                    )
                )
            # Cleanup targets the exact corrupt record identity only; healthy
            # sibling rows in the same keyed store must survive.
            for record_id in corrupt_record_ids:
                try:
                    if key_column is None:
                        connection.execute(
                            f'DELETE FROM {table} WHERE document_id=?',
                            (document_id,),
                        )
                    else:
                        connection.execute(
                            f'DELETE FROM {table} WHERE document_id=? AND {key_column}=?',
                            (document_id, record_id),
                        )
                except sqlite3.Error:
                    _LOGGER.warning(
                        'could not purge corrupt %s record %s for document %s',
                        store,
                        record_id,
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
    #
    # Versioned design authority (#843): every save appends an immutable,
    # hash-sealed AuthoringConstraintRevision bound to the SceneRevision it
    # was authored under and to its predecessor. The singleton
    # ``authoring_constraint_sets`` row keeps only a head pointer — a
    # historical design state resolves the exact constraint set that
    # governed it, and a corrupt head fails closed instead of reading as an
    # empty set.

    _CONSTRAINT_HEAD_KEY = 'head_constraint_revision_id'

    def _row_to_constraint_revision(
        self, row: sqlite3.Row
    ) -> AuthoringConstraintRevision:
        try:
            payload = json.loads(row['payload_json'])
            if not isinstance(payload, dict):
                raise ValueError('payload is not a JSON object')
        except (TypeError, ValueError, RecursionError) as exc:
            raise AuthoringConstraintIntegrityError(
                f"authoring constraint revision "
                f"{row['constraint_revision_id']} is unreadable: {exc}"
            ) from exc
        revision = AuthoringConstraintRevision(
            constraint_revision_id=row['constraint_revision_id'],
            document_id=row['document_id'],
            supersedes_id=row['supersedes_id'],
            scene_revision_id=row['scene_revision_id'],
            payload=payload,
            created_at_utc=row['created_at_utc'],
            constraint_revision_sha256=row['constraint_revision_sha256'],
        )
        if (
            _constraint_revision_sha256(revision.identity_payload())
            != revision.constraint_revision_sha256
        ):
            raise AuthoringConstraintIntegrityError(
                'authoring constraint revision hash mismatch: '
                f"{revision.constraint_revision_id}"
            )
        return revision

    def _authoring_head_pointer_id(
        self, connection: sqlite3.Connection, document_id: str
    ) -> object:
        """Head revision id the pointer row claims, read on ``connection``.

        Returns the raw ``head_constraint_revision_id`` value (or ``None``
        when no pointer row exists or the row is a legacy singleton payload).
        An unparsable pointer means the row changed since the caller's
        pre-lock resolution — reported as a conflict, not corruption, since
        ``authoring_constraint_head`` already fails closed on retained
        corrupt state before the write lock is taken.
        """
        row = connection.execute(
            'SELECT payload_json FROM authoring_constraint_sets '
            'WHERE document_id=?',
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            stored = json.loads(row['payload_json'])
        except (TypeError, ValueError, RecursionError) as exc:
            raise AuthoringConstraintConflictError(
                'authoring constraint head pointer changed while saving '
                f'document {document_id}; re-resolve and retry'
            ) from exc
        if not isinstance(stored, dict):
            return None
        return stored.get(self._CONSTRAINT_HEAD_KEY)

    def _authoring_head_row(self, document_id: str) -> sqlite3.Row | None:
        """Raw singleton head-pointer row — never auto-purged on corruption.

        Unlike disposable editor payloads, a corrupt constraint head must
        stay retained and visible as an integrity problem (#843).
        """
        with closing(self._read()) as connection, connection:
            return connection.execute(
                'SELECT payload_json, updated_at_utc '
                'FROM authoring_constraint_sets WHERE document_id=?',
                (document_id,),
            ).fetchone()

    def authoring_constraint_head(
        self, document_id: str
    ) -> AuthoringConstraintRevision | None:
        """Resolve the current head of the constraint authority.

        Fails closed with ``AuthoringConstraintIntegrityError`` when the
        persisted head pointer or revision row is unreadable. Legacy
        singleton rows written before versioning (a bare constraint-set
        payload) are still readable as authority material.
        """
        row = self._authoring_head_row(document_id)
        if row is None:
            return None
        try:
            stored = json.loads(row['payload_json'])
        except (TypeError, ValueError, RecursionError) as exc:
            raise AuthoringConstraintIntegrityError(
                'authoring constraint authority is unreadable and was '
                f'retained: {exc}'
            ) from exc
        if not isinstance(stored, dict):
            raise AuthoringConstraintIntegrityError(
                'authoring constraint authority is unreadable and was retained'
            )
        head_id = stored.get(self._CONSTRAINT_HEAD_KEY)
        if head_id is None:
            # Legacy pre-versioned singleton: the payload itself is the set.
            if not stored:
                return None
            return AuthoringConstraintRevision(
                constraint_revision_id='',
                document_id=document_id,
                supersedes_id=None,
                scene_revision_id=None,
                payload=stored,
                created_at_utc=row['updated_at_utc'],
                constraint_revision_sha256='',
            )
        with closing(self._read()) as connection, connection:
            revision_row = connection.execute(
                'SELECT * FROM authoring_constraint_revisions '
                'WHERE constraint_revision_id=? AND document_id=?',
                (head_id, document_id),
            ).fetchone()
        if revision_row is None:
            raise AuthoringConstraintIntegrityError(
                f'authoring constraint head {head_id} is not a persisted '
                'revision; authority cannot be resolved'
            )
        revision = self._row_to_constraint_revision(revision_row)
        claimed_sha = stored.get('head_constraint_revision_sha256')
        if (
            claimed_sha is not None
            and claimed_sha != revision.constraint_revision_sha256
        ):
            raise AuthoringConstraintIntegrityError(
                'authoring constraint head pointer disagrees with the '
                'sealed revision it claims'
            )
        return revision

    def save_authoring_constraints(
        self,
        document_id: str,
        payload: dict,
        *,
        scene_revision_id: str | None = None,
    ) -> AuthoringConstraintRevision:
        """Append one immutable constraint revision and advance the head.

        The previous head resolves first — a corrupt authority refuses the
        write rather than letting a new set bury the only record of the old
        relationships (#843).
        """
        head = self.authoring_constraint_head(document_id)
        revision = AuthoringConstraintRevision.build(
            document_id=document_id,
            payload=payload,
            supersedes_id=(
                None if head is None else head.constraint_revision_id or None
            ),
            scene_revision_id=scene_revision_id,
        )
        pointer = {
            self._CONSTRAINT_HEAD_KEY: revision.constraint_revision_id,
            'head_constraint_revision_sha256': revision.constraint_revision_sha256,
        }
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            # Compare-and-swap under the write lock: the head resolved above
            # was a pre-lock hint. The pointer row is re-read on this
            # connection so a concurrent save can never be superseded out of
            # the lineage — the scene revision save applies the same rule.
            claimed = self._authoring_head_pointer_id(connection, document_id)
            expected = (
                None if head is None else head.constraint_revision_id or None
            )
            if claimed != expected:
                raise AuthoringConstraintConflictError(
                    'authoring constraint head moved while saving document '
                    f'{document_id}; re-resolve and retry'
                )
            connection.execute(
                'INSERT INTO authoring_constraint_revisions('
                'constraint_revision_id, document_id, supersedes_id, '
                'scene_revision_id, payload_json, '
                'constraint_revision_sha256, created_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    revision.constraint_revision_id,
                    revision.document_id,
                    revision.supersedes_id,
                    revision.scene_revision_id,
                    json.dumps(
                        revision.payload,
                        separators=(',', ':'),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    revision.constraint_revision_sha256,
                    revision.created_at_utc,
                ),
            )
            connection.execute(
                'INSERT INTO authoring_constraint_sets('
                'document_id, payload_json, updated_at_utc'
                ') VALUES (?, ?, ?) '
                'ON CONFLICT(document_id) DO UPDATE SET '
                'payload_json=excluded.payload_json, '
                'updated_at_utc=excluded.updated_at_utc',
                (
                    document_id,
                    json.dumps(pointer, separators=(',', ':'), ensure_ascii=False, allow_nan=False),
                    updated_at,
                ),
            )
            connection.commit()
        return revision

    def authoring_constraints(self, document_id: str) -> EditorPayloadRecord | None:
        """The document's current authoring-constraint set payload."""
        head = self.authoring_constraint_head(document_id)
        if head is None:
            return None
        return EditorPayloadRecord(
            document_id=document_id,
            record_id=head.constraint_revision_id or document_id,
            payload=head.payload,
            updated_at_utc=head.created_at_utc,
        )

    def list_authoring_constraint_revisions(
        self, document_id: str
    ) -> tuple[AuthoringConstraintRevision, ...]:
        """The full immutable constraint lineage, newest first (#843)."""
        with closing(self._read()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM authoring_constraint_revisions '
                'WHERE document_id=? '
                'ORDER BY created_at_utc DESC, constraint_revision_id',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_constraint_revision(row) for row in rows)

    def authoring_constraints_for_scene(
        self, document_id: str, scene_revision_id: str
    ) -> AuthoringConstraintRevision | None:
        """The constraint authority governing one historical SceneRevision.

        An exact binding wins; otherwise the newest revision written at or
        before that scene's commit still governs (constraints carry forward
        until the next constraint edit).
        """
        with closing(self._read()) as connection, connection:
            scene_row = connection.execute(
                'SELECT created_at_utc FROM scene_revisions '
                'WHERE revision_id=? AND document_id=?',
                (scene_revision_id, document_id),
            ).fetchone()
            if scene_row is None:
                return None
            exact = connection.execute(
                'SELECT * FROM authoring_constraint_revisions '
                'WHERE document_id=? AND scene_revision_id=? '
                'ORDER BY created_at_utc DESC, constraint_revision_id LIMIT 1',
                (document_id, scene_revision_id),
            ).fetchone()
            if exact is not None:
                return self._row_to_constraint_revision(exact)
            inherited = connection.execute(
                'SELECT * FROM authoring_constraint_revisions '
                'WHERE document_id=? AND created_at_utc<=? '
                'ORDER BY created_at_utc DESC, constraint_revision_id LIMIT 1',
                (document_id, scene_row['created_at_utc']),
            ).fetchone()
        if inherited is None:
            return None
        return self._row_to_constraint_revision(inherited)

    def repair_authoring_constraints(self, document_id: str) -> bool:
        """Discard the corrupt/legacy singleton head so a fresh set can save.

        Immutable revision history is never touched — the repair only clears
        the head pointer, an explicit user action (#843).
        """
        with closing(self._connect()) as connection, connection:
            deleted = connection.execute(
                'DELETE FROM authoring_constraint_sets WHERE document_id=?',
                (document_id,),
            ).rowcount
            connection.commit()
        return deleted > 0

    def _row_to_revision(
        self,
        row: sqlite3.Row,
        *,
        read_blob,
    ) -> SceneRevision:
        # Parse memo keyed by the full row content: every caller still
        # re-SELECTs rows per read (deletes and writes stay observable);
        # only the deserialization of an identical row is reused. Blob
        # references are row columns, so identical rows resolve identical
        # content-addressed blobs regardless of the read_blob callable.
        key = tuple(row)
        cached = self._revision_cache.pop(row['revision_id'], None)
        if cached is not None and cached[0] == key:
            self._revision_cache[row['revision_id']] = cached
            return cached[1]
        try:
            document = SceneDocument.model_validate(
                json.loads(row['payload_json'])
            )
        except ValueError as exc:
            raise SceneRevisionIntegrityError(
                'scene revision payload is unreadable: '
                f"{row['revision_id']}"
            ) from exc
        content_hash = scene_content_hash(document)
        if content_hash != row['content_hash']:
            raise SceneRevisionIntegrityError(
                f"scene revision hash mismatch: {row['revision_id']}"
            )
        if document.document_id != row['document_id']:
            raise SceneRevisionIntegrityError(
                f"scene revision document mismatch: {row['revision_id']}"
            )
        # Issue #653: rehydrate the in-memory mesh cache from the blob store;
        # the persisted payload only carries the compact reference. A missing
        # or corrupt blob fails closed rather than rendering silently.
        document = resolve_document_mesh_bodies(document, read_blob)
        revision = SceneRevision(
            revision_id=row['revision_id'],
            document_id=row['document_id'],
            parent_revision_id=row['parent_revision_id'],
            created_at_utc=row['created_at_utc'],
            content_hash=row['content_hash'],
            document=document,
            detached=bool(row['detached']),
            detached_reason=row['detached_reason'],
        )
        self._revision_cache[revision.revision_id] = (key, revision)
        while len(self._revision_cache) > self._REVISION_CACHE_LIMIT:
            self._revision_cache.popitem(last=False)
        return revision
