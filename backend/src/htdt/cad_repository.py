from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from uuid import uuid4

from .cad_scene import SceneDocument, canonical_scene_json, scene_content_hash
from .cad_schema import ensure_native_schema
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
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS scene_revisions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    revision_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            connection.execute(
                'CREATE INDEX IF NOT EXISTS idx_scene_revisions_document_seq '
                'ON scene_revisions(document_id, seq DESC)'
            )
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS scene_recovery_snapshots (
                    document_id TEXT PRIMARY KEY,
                    source_revision_id TEXT,
                    updated_at_utc TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(source_revision_id) REFERENCES scene_revisions(revision_id)
                )
                '''
            )
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS editor_view_states (
                    document_id TEXT PRIMARY KEY,
                    selected_id TEXT,
                    hidden_ids_json TEXT NOT NULL,
                    locked_ids_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                '''
            )
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(editor_view_states)')}
            if 'selected_ids_json' not in columns:
                connection.execute(
                    "ALTER TABLE editor_view_states ADD COLUMN selected_ids_json TEXT NOT NULL DEFAULT '[]'"
                )

    def latest(self, document_id: str) -> SceneRevision | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM scene_revisions WHERE document_id=? ORDER BY seq DESC LIMIT 1',
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_revision(row)

    def get(self, revision_id: str) -> SceneRevision | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM scene_revisions WHERE revision_id=?',
                (revision_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_revision(row)

    def save(
        self,
        document: SceneDocument,
        *,
        parent_revision_id: str | None,
        allow_branch: bool = False,
    ) -> SaveResult:
        """Persist one immutable SceneRevision as the document's current head.

        This is an optimistic compare-and-swap boundary on the single-head
        lineage: the first revision of a document requires
        ``parent_revision_id=None`` and no existing revision, and every later
        revision requires ``parent_revision_id`` to equal the document's latest
        revision. The head check runs inside the same ``BEGIN IMMEDIATE``
        transaction as the insert, so two writers racing from the same head
        cannot both advance it. Violations raise ``SceneRevisionConflictError``.

        ``allow_branch=True`` deliberately relaxes the head check for explicit
        non-head lineage; see ``_save_in_transaction`` for the full contract.
        """
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_in_transaction(
                connection,
                document,
                parent_revision_id=parent_revision_id,
                allow_branch=allow_branch,
            )

    def _save_in_transaction(
        self,
        connection: sqlite3.Connection,
        document: SceneDocument,
        *,
        parent_revision_id: str | None,
        revision_id: str | None = None,
        created_at_utc: str | None = None,
        allow_branch: bool = False,
    ) -> SaveResult:
        """Persist one immutable SceneRevision inside the caller's transaction.

        This is the single SceneRevision write authority used by both normal saves
        and higher-level operations that must commit related lineage atomically.
        The caller owns BEGIN/COMMIT/ROLLBACK when passing an existing connection;
        the head check below must run under a held BEGIN IMMEDIATE so that racing
        writers serialize before compare-and-swap evaluation.

        Lineage contract: the first revision of a document requires
        ``parent_revision_id=None`` and no existing revision; every later
        revision requires ``parent_revision_id`` to equal the document's latest
        (highest seq) revision. Stale-parent and duplicate-root saves raise
        ``SceneRevisionConflictError`` rather than silently branching history.

        ``allow_branch=True`` permits a non-head parent for deliberate detached
        lineage (for example O50 measurement-plan fixture revisions that must
        descend directly from the SearchSpec source revision). A branch row
        still wins ``latest()`` by insertion order, so callers must bind the
        returned revision by id and must not treat it as the document's current
        head. A second root (``parent_revision_id=None`` with existing history)
        is always rejected.
        """

        payload_json = canonical_scene_json(document)
        content_hash = scene_content_hash(document)
        head = connection.execute(
            'SELECT revision_id FROM scene_revisions '
            'WHERE document_id=? ORDER BY seq DESC LIMIT 1',
            (document.document_id,),
        ).fetchone()
        parent = None
        if parent_revision_id is None:
            if head is not None:
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
            if not allow_branch and (
                head is None or head['revision_id'] != parent_revision_id
            ):
                raise SceneRevisionConflictError(
                    f'stale parent SceneRevision: {parent_revision_id} is not the '
                    f'latest revision of document {document.document_id}'
                )
            if parent['content_hash'] == content_hash:
                connection.execute(
                    'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
                    (document.document_id,),
                )
                return SaveResult(self._row_to_revision(parent), created=False)

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
                revision_id, document_id, parent_revision_id, created_at_utc, content_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                revision_id,
                document.document_id,
                parent_revision_id,
                created_at,
                content_hash,
                payload_json,
            ),
        )
        connection.execute(
            'DELETE FROM scene_recovery_snapshots WHERE document_id=?',
            (document.document_id,),
        )
        row = connection.execute(
            'SELECT * FROM scene_revisions WHERE revision_id=?',
            (revision_id,),
        ).fetchone()
        return SaveResult(self._row_to_revision(row), created=True)

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
        payload_json = canonical_scene_json(document)
        content_hash = scene_content_hash(document)
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
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
    ) -> None:
        ordered_selected = list(dict.fromkeys(selected_ids or (() if selected_id is None else (selected_id,))))
        if selected_id is not None and selected_id not in ordered_selected:
            ordered_selected.append(selected_id)
        selected_json = json.dumps(ordered_selected, separators=(',', ':'))
        hidden_json = json.dumps(sorted(hidden_ids), separators=(',', ':'))
        locked_json = json.dumps(sorted(locked_ids), separators=(',', ':'))
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection, connection:
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
        return EditorViewRecord(
            document_id=row['document_id'],
            selected_id=selected_id,
            selected_ids=selected,
            hidden_ids=hidden,
            locked_ids=locked,
        )

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

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> SceneRevision:
        document = SceneDocument.model_validate(json.loads(row['payload_json']))
        content_hash = scene_content_hash(document)
        if content_hash != row['content_hash']:
            raise ValueError(f"scene revision hash mismatch: {row['revision_id']}")
        return SceneRevision(
            revision_id=row['revision_id'],
            document_id=row['document_id'],
            parent_revision_id=row['parent_revision_id'],
            created_at_utc=row['created_at_utc'],
            content_hash=row['content_hash'],
            document=document,
        )
