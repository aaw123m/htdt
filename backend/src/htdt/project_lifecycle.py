"""Project retirement and deletion lifecycle (#611).

The store's authority is ``document_id``-keyed (``cad_repository`` and the
other repositories all scope project data by document). This module adds the
project lifecycle layer on top:

- the canonical ``htdt_project_documents`` registry mapping
  ``project_id`` ↔ ``document_id`` with an ``active``/``archived`` state — archiving is the default retirement
  path and destroys nothing;
- ``plan_project_deletion`` producing a ``ProjectDeletionPlan`` preview:
  per-authority row counts and byte estimates, shared-vs-local managed
  assets, pending missions and inbox items, and the hard blockers that must
  be resolved first;
- ``delete_project`` removing the project's authorities atomically (single
  transaction, verified by ``PRAGMA foreign_key_check``) and recording a
  bounded tombstone.

Managed-asset garbage collection is deliberately NOT here — deleting a
project only marks its unshared assets GC-eligible. The separate #501 GC
authority owns actually reclaiming shared bytes.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .cad_schema import ensure_native_schema, require_native_tables
from .native_backup import DATABASE_NAME


_LOGGER = logging.getLogger('htdt.native')

PROJECT_REGISTRY_SCHEMA = 1
# Tombstones exist so support/diagnostics can still explain where a removed
# project went; a bounded ring keeps the registry from becoming its own
# retention problem.
KEEP_PROJECT_TOMBSTONES = 32
TOMBSTONE_SCHEMA_VERSION = 1

# Pending capture-inbox dispositions that still hold staged work for a scope.
PENDING_INBOX_DISPOSITIONS = ('pending', 'deferred', 'partially_promoted')

# Columns that carry user bytes; byte estimates sum their lengths.
_PAYLOAD_COLUMN_SUFFIXES = ('_json', '_blob', '_payload', '_bytes')


class ProjectLifecycleError(RuntimeError):
    pass


class ProjectNotFoundError(ProjectLifecycleError):
    pass


class ProjectDeletionBlockedError(ProjectLifecycleError):
    def __init__(self, plan: 'ProjectDeletionPlan') -> None:
        self.plan = plan
        super().__init__(
            'project deletion is blocked: '
            + '; '.join(blocker.detail for blocker in plan.hard_blockers)
        )


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    document_id: str
    display_name: str
    status: Literal['active', 'archived']
    cloned_from_project_id: str | None
    created_at_utc: str
    updated_at_utc: str
    archived_at_utc: str | None


class DeletedAuthorityCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)


class AssetDependencySummary(BaseModel):
    """Managed-asset reachability for the project being removed.

    ``shared`` assets are also referenced by other projects' authorities and
    are always retained. ``local`` assets become GC-eligible after removal —
    deletion itself never unlinks them (#501 owns that).
    """

    model_config = ConfigDict(frozen=True)

    shared_asset_count: int = Field(ge=0)
    shared_asset_bytes: int = Field(ge=0)
    local_asset_count: int = Field(ge=0)
    local_asset_bytes: int = Field(ge=0)
    gc_eligible_count: int = Field(ge=0)
    gc_eligible_bytes: int = Field(ge=0)


class DeletionBlocker(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal[
        'project_not_archived',
        'active_descendants',
        'pending_capture_missions',
        'pending_inbox_items',
        'unknown_project',
    ]
    detail: str = Field(min_length=1)


class ProjectDeletionPlan(BaseModel):
    """Preview of what ``delete_project`` would remove and retain."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = TOMBSTONE_SCHEMA_VERSION
    project_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_name: str
    created_at_utc: str
    authorities: tuple[DeletedAuthorityCount, ...]
    total_rows: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    assets: AssetDependencySummary
    pending_mission_count: int = Field(ge=0)
    pending_inbox_item_count: int = Field(ge=0)
    hard_blockers: tuple[DeletionBlocker, ...]

    @property
    def executable(self) -> bool:
        return not self.hard_blockers


class ProjectTombstone(BaseModel):
    """Bounded deletion marker — operational metadata, not project data."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = TOMBSTONE_SCHEMA_VERSION
    tombstone_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    display_name: str
    deleted_at_utc: str
    removed_rows: int = Field(ge=0)
    estimated_bytes: int = Field(ge=0)
    authorities_json: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(connection.execute(f'PRAGMA table_info({table})'))


def _foreign_keys(
    connection: sqlite3.Connection, table: str
) -> list[tuple[str, str, str]]:
    """(column, referenced_table, referenced_column) for each FK in table."""

    return [
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.execute(f'PRAGMA foreign_key_list({table})')
    ]


class ProjectLibrary:
    """Project registry + retirement/deletion lifecycle for the data dir."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # This class owns opening/creating the data dir's database, so it —
        # not a repository — invokes the migration authority (#302/#767).
        ensure_native_schema(self.path)
        with closing(self._connect()) as connection:
            require_native_tables(
                connection, 'htdt_project_documents', 'htdt_project_tombstones'
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    # -- registry -------------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=str(row['project_id']),
            document_id=str(row['document_id']),
            display_name=str(row['display_name']),
            status='archived' if row['archived'] else 'active',
            cloned_from_project_id=(
                None
                if row['cloned_from_project_id'] is None
                else str(row['cloned_from_project_id'])
            ),
            created_at_utc=str(row['created_at_utc']),
            updated_at_utc=str(row['updated_at_utc']),
            archived_at_utc=(
                None
                if row['archived_at_utc'] is None
                else str(row['archived_at_utc'])
            ),
        )

    def register_project(
        self,
        document_id: str,
        display_name: str = '',
        *,
        project_id: str | None = None,
        cloned_from_project_id: str | None = None,
    ) -> ProjectRecord:
        """Idempotently register one document as a managed project."""

        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE document_id=?',
                (document_id,),
            ).fetchone()
            if existing is not None:
                return self._row_to_record(existing)
            record = (
                project_id or uuid4().hex,
                document_id,
                display_name or document_id,
                cloned_from_project_id,
                now,
                now,
                0,
                None,
            )
            connection.execute(
                'INSERT INTO htdt_project_documents('
                'project_id, document_id, display_name, '
                'cloned_from_project_id, created_at_utc, updated_at_utc, '
                'archived, archived_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                record,
            )
        return ProjectRecord(
            project_id=record[0],
            document_id=document_id,
            display_name=record[2],
            status='active',
            cloned_from_project_id=cloned_from_project_id,
            created_at_utc=now,
            updated_at_utc=now,
            archived_at_utc=None,
        )

    def adopt_existing_documents(self) -> tuple[ProjectRecord, ...]:
        """Register every document that has revisions but no registry row.

        Backwards compatibility: data written before the registry existed is
        adopted as active projects rather than disappearing from lists.
        """

        adopted: list[ProjectRecord] = []
        with closing(self._connect()) as connection, connection:
            document_ids = [
                str(row[0])
                for row in connection.execute(
                    'SELECT DISTINCT document_id FROM scene_revisions'
                )
            ]
        for document_id in document_ids:
            record = self.register_project(document_id)
            adopted.append(record)
        return tuple(adopted)

    def get_project(self, project_id: str) -> ProjectRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE project_id=?',
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(f'unknown project: {project_id}')
        return self._row_to_record(row)

    def find_by_document(self, document_id: str) -> ProjectRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE document_id=?',
                (document_id,),
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def list_projects(
        self, *, include_archived: bool = False
    ) -> tuple[ProjectRecord, ...]:
        """Active projects by default; archived ones stay hidden (#611)."""

        sql = 'SELECT * FROM htdt_project_documents'
        args: tuple[object, ...] = ()
        if not include_archived:
            sql += " WHERE archived=0"
        sql += ' ORDER BY created_at_utc, project_id'
        with closing(self._connect()) as connection:
            return tuple(
                self._row_to_record(row)
                for row in connection.execute(sql, args)
            )

    def archive_project(self, project_id: str) -> ProjectRecord:
        """Retire a project: hide it from active lists, keep everything."""

        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                'UPDATE htdt_project_documents SET archived=1, '
                'archived_at_utc=?, updated_at_utc=? WHERE project_id=?',
                (now, now, project_id),
            )
            if cursor.rowcount == 0:
                raise ProjectNotFoundError(f'unknown project: {project_id}')
        return self.get_project(project_id)

    def unarchive_project(self, project_id: str) -> ProjectRecord:
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                'UPDATE htdt_project_documents SET archived=0, '
                'archived_at_utc=NULL, updated_at_utc=? WHERE project_id=?',
                (now, project_id),
            )
            if cursor.rowcount == 0:
                raise ProjectNotFoundError(f'unknown project: {project_id}')
        return self.get_project(project_id)

    # -- deletion planning ----------------------------------------------

    def _owned_predicates(
        self, connection: sqlite3.Connection, document_id: str
    ) -> dict[str, list[str]]:
        """Compute per-table ownership predicates for one document.

        Direct rule: any table with a ``document_id`` column owns rows where
        ``document_id = ?``. Chase rule: a table WITHOUT its own document_id
        whose foreign keys reference an already-owned table removes rows
        referencing owned parents (e.g. ``scene_revision_labels`` ->
        ``scene_revisions``, inbox child tables -> owned inbox items). Repeat
        to a fixpoint. Tables with their own document_id are never claimed by
        the chase rule — that bounds removal to this project's authorities.
        """

        tables = _table_names(connection) - {
            'htdt_project_documents',
            'htdt_project_tombstones',
            'htdt_project_imports',
            'htdt_legacy_imports',
            'capture_ingestion_lineages',
            'native_schema_metadata',
            'native_schema_migrations',
            'capture_receiver_config',
            'capture_receiver_pairings',
        }
        columns = {
            table: [str(col[1]) for col in _table_columns(connection, table)]
            for table in tables
        }
        owned: dict[str, list[str]] = {}
        for table, cols in columns.items():
            if 'document_id' in cols:
                owned[table] = ['document_id = ?']

        # Scope-keyed inbox items are owned by the project's document scope.
        if 'capture_inbox_items' in columns and 'scope' in columns['capture_inbox_items']:
            owned['capture_inbox_items'] = ['scope = ?']

        changed = True
        while changed:
            changed = False
            for table in sorted(tables):
                if table in owned:
                    continue
                predicates: list[str] = []
                for column, ref_table, ref_column in _foreign_keys(
                    connection, table
                ):
                    if ref_table in owned:
                        for predicate in owned[ref_table]:
                            predicates.append(
                                f'{column} IN (SELECT {ref_column} '
                                f'FROM {ref_table} WHERE {predicate})'
                            )
                if predicates:
                    owned[table] = predicates
                    changed = True
        return owned

    def _count_owned(
        self,
        connection: sqlite3.Connection,
        table: str,
        predicates: list[str],
        document_id: str,
    ) -> DeletedAuthorityCount:
        cols = _table_columns(connection, table)
        payload_columns = [
            str(col[1])
            for col in cols
            if str(col[2]).upper() in ('TEXT', 'BLOB')
            and str(col[1]).endswith(_PAYLOAD_COLUMN_SUFFIXES)
        ]
        byte_expr = (
            '+'.join(f'LENGTH(COALESCE({c}, ' + "''" + '))' for c in payload_columns)
            if payload_columns
            else '0'
        )
        where = ' OR '.join(f'({p})' for p in predicates)
        # Every predicate carries exactly one '?' bound to document_id.
        row_count, estimated = connection.execute(
            f'SELECT COUNT(*), COALESCE(SUM({byte_expr}), 0) '
            f'FROM {table} WHERE {where}',
            (document_id,) * len(predicates),
        ).fetchone()
        return DeletedAuthorityCount(
            table=table,
            row_count=int(row_count),
            estimated_bytes=int(estimated),
        )

    def _asset_summary(
        self, connection: sqlite3.Connection, document_id: str
    ) -> AssetDependencySummary:
        """Split the project's managed assets into shared vs local.

        An asset digest is "shared" when any referencing row belongs to a
        different document (or to no document — receiver-inbox staging).
        Local digests become GC-eligible; deletion never removes them.
        """

        # (sha_column, owning-document SQL) per table that references the
        # managed-assets manifest tables.
        reference_sources: list[str] = []
        tables = _table_names(connection)
        # direct document_id-keyed referencing tables
        for table in sorted(tables):
            cols = {str(col[1]) for col in _table_columns(connection, table)}
            sha_cols = [
                c
                for c in cols
                if c in ('sha256', 'source_sha256')
            ]
            if not sha_cols or 'document_id' not in cols:
                continue
            for sha_col in sha_cols:
                reference_sources.append(
                    f'SELECT {sha_col} AS sha256, document_id '
                    f'FROM {table} WHERE {sha_col} IS NOT NULL'
                )
        # measurement-scoped references resolve their owner via
        # cad_measurements.document_id
        if 'cad_frequency_responses' in tables and 'cad_measurements' in tables:
            reference_sources.append(
                'SELECT f.source_sha256 AS sha256, m.document_id '
                'FROM cad_frequency_responses f '
                'JOIN cad_measurements m ON f.measurement_id = m.measurement_id'
            )
        if 'cad_impulse_responses' in tables and 'cad_measurements' in tables:
            reference_sources.append(
                'SELECT f.source_sha256 AS sha256, m.document_id '
                'FROM cad_impulse_responses f '
                'JOIN cad_measurements m ON f.measurement_id = m.measurement_id'
            )

        referenced_by_doc: dict[str, set[str]] = {}
        if reference_sources:
            union = ' UNION ALL '.join(reference_sources)
            for sha, owner in connection.execute(union):
                referenced_by_doc.setdefault(str(sha), set()).add(
                    str(owner) if owner is not None else ''
                )

        asset_sizes: dict[str, int] = {}
        for manifest_table in (
            'cad_measurement_assets',
            'cad_quality_calibration_files',
        ):
            if manifest_table not in tables:
                continue
            for sha, size in connection.execute(
                f'SELECT sha256, size_bytes FROM {manifest_table}'
            ):
                asset_sizes[str(sha)] = int(size)

        shared: dict[str, int] = {}
        local: dict[str, int] = {}
        for sha, owners in referenced_by_doc.items():
            if document_id not in owners or sha not in asset_sizes:
                continue
            size = asset_sizes[sha]
            if owners - {document_id}:
                shared[sha] = size
            else:
                local[sha] = size
        return AssetDependencySummary(
            shared_asset_count=len(shared),
            shared_asset_bytes=sum(shared.values()),
            local_asset_count=len(local),
            local_asset_bytes=sum(local.values()),
            gc_eligible_count=len(local),
            gc_eligible_bytes=sum(local.values()),
        )

    def _pending_missions(
        self, connection: sqlite3.Connection, document_id: str
    ) -> int:
        tables = _table_names(connection)
        if 'capture_mission_packages' not in tables:
            return 0
        pending = connection.execute(
            "SELECT descriptor_json FROM capture_mission_packages "
            "WHERE status='pending'"
        ).fetchall()
        count = 0
        for (descriptor_json,) in pending:
            try:
                descriptor = json.loads(str(descriptor_json))
            except (TypeError, ValueError):
                continue
            if str(descriptor.get('document_id', '')) == document_id:
                count += 1
        return count

    def _pending_inbox_items(
        self, connection: sqlite3.Connection, document_id: str
    ) -> int:
        tables = _table_names(connection)
        if 'capture_inbox_items' not in tables:
            return 0
        placeholders = ','.join('?' for _ in PENDING_INBOX_DISPOSITIONS)
        (count,) = connection.execute(
            'SELECT COUNT(*) FROM capture_inbox_items '
            f'WHERE scope=? AND disposition IN ({placeholders})',
            (document_id, *PENDING_INBOX_DISPOSITIONS),
        ).fetchone()
        return int(count)

    def plan_project_deletion(self, project_id: str) -> ProjectDeletionPlan:
        """Read-only preview: what would be removed, retained, or blocked."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE project_id=?',
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f'unknown project: {project_id}')
            record = self._row_to_record(row)

            owned = self._owned_predicates(connection, record.document_id)
            authorities = tuple(
                count
                for count in (
                    self._count_owned(
                        connection, table, predicates, record.document_id
                    )
                    for table, predicates in sorted(owned.items())
                )
                if count.row_count > 0
            )
            pending_missions = self._pending_missions(
                connection, record.document_id
            )
            pending_inbox = self._pending_inbox_items(
                connection, record.document_id
            )
            descendants = connection.execute(
                'SELECT COUNT(*) FROM htdt_project_documents '
                'WHERE cloned_from_project_id=?',
                (project_id,),
            ).fetchone()[0]
            assets = self._asset_summary(connection, record.document_id)

        blockers: list[DeletionBlocker] = []
        if record.status != 'archived':
            blockers.append(
                DeletionBlocker(
                    kind='project_not_archived',
                    detail=(
                        'project is still active; archive it first — '
                        'retirement is the default and is reversible'
                    ),
                )
            )
        if descendants:
            blockers.append(
                DeletionBlocker(
                    kind='active_descendants',
                    detail=(
                        f'{descendants} project(s) were cloned from this '
                        'project; retire them first'
                    ),
                )
            )
        if pending_missions:
            blockers.append(
                DeletionBlocker(
                    kind='pending_capture_missions',
                    detail=(
                        f'{pending_missions} capture mission package(s) still '
                        'target this project; cancel or retire them first'
                    ),
                )
            )
        if pending_inbox:
            blockers.append(
                DeletionBlocker(
                    kind='pending_inbox_items',
                    detail=(
                        f'{pending_inbox} capture inbox item(s) are still '
                        'pending in this project scope'
                    ),
                )
            )

        return ProjectDeletionPlan(
            project_id=record.project_id,
            document_id=record.document_id,
            display_name=record.display_name,
            created_at_utc=_utc_now(),
            authorities=authorities,
            total_rows=sum(a.row_count for a in authorities),
            estimated_bytes=sum(a.estimated_bytes for a in authorities),
            assets=assets,
            pending_mission_count=pending_missions,
            pending_inbox_item_count=pending_inbox,
            hard_blockers=tuple(blockers),
        )

    # -- deletion --------------------------------------------------------

    def delete_project(self, project_id: str) -> ProjectTombstone:
        """Atomically remove the project's authorities and write a tombstone.

        The whole removal runs in one immediate transaction with foreign
        keys checked before commit — a partially deleted project is never
        observable. Managed assets themselves stay for the #501 GC.
        """

        plan = self.plan_project_deletion(project_id)
        if not plan.executable:
            raise ProjectDeletionBlockedError(plan)

        connection = self._connect()
        try:
            # FK enforcement off inside this transaction: self-referencing
            # and cyclic-owned deletes can't be ordered safely. A
            # foreign_key_check before commit proves no dangling references
            # were left behind.
            connection.execute('PRAGMA foreign_keys=OFF')
            connection.execute('BEGIN IMMEDIATE')
            try:
                # Re-verify the plan inside the transaction — the world may
                # have moved between preview and removal.
                owned = self._owned_predicates(connection, plan.document_id)
                for table in sorted(owned):
                    predicates = owned[table]
                    where = ' OR '.join(f'({p})' for p in predicates)
                    connection.execute(
                        f'DELETE FROM {table} WHERE {where}',
                        (plan.document_id,) * len(predicates),
                    )
                # Inbox lineages orphaned by this deletion are retained for
                # the separate GC — they are shared dedup anchors, not
                # project data.
                connection.execute(
                    'DELETE FROM htdt_project_documents WHERE project_id=?',
                    (project_id,),
                )
                connection.execute(
                    'INSERT INTO htdt_project_tombstones('
                    'tombstone_id, project_id, document_id, display_name, '
                    'deleted_at_utc, removed_rows, estimated_bytes, '
                    'authorities_json'
                    ') VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        uuid4().hex,
                        plan.project_id,
                        plan.document_id,
                        plan.display_name,
                        _utc_now(),
                        plan.total_rows,
                        plan.estimated_bytes,
                        json.dumps(
                            [a.model_dump() for a in plan.authorities],
                            sort_keys=True,
                        ),
                    ),
                )
                # Bound tombstone retention.
                connection.execute(
                    'DELETE FROM htdt_project_tombstones WHERE tombstone_id NOT IN ('
                    '  SELECT tombstone_id FROM htdt_project_tombstones '
                    '  ORDER BY deleted_at_utc DESC, tombstone_id DESC LIMIT ?'
                    ')',
                    (KEEP_PROJECT_TOMBSTONES,),
                )
                violations = connection.execute(
                    'PRAGMA foreign_key_check'
                ).fetchall()
                if violations:
                    raise ProjectLifecycleError(
                        f'project deletion would leave dangling references: '
                        f'{violations[:10]}'
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_tombstones WHERE project_id=? '
                'ORDER BY deleted_at_utc DESC LIMIT 1',
                (project_id,),
            ).fetchone()
        return ProjectTombstone(
            tombstone_id=str(row['tombstone_id']),
            project_id=str(row['project_id']),
            document_id=str(row['document_id']),
            display_name=str(row['display_name']),
            deleted_at_utc=str(row['deleted_at_utc']),
            removed_rows=int(row['removed_rows']),
            estimated_bytes=int(row['estimated_bytes']),
            authorities_json=str(row['authorities_json']),
        )

    def list_tombstones(self) -> tuple[ProjectTombstone, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM htdt_project_tombstones '
                'ORDER BY deleted_at_utc DESC, tombstone_id DESC'
            ).fetchall()
        return tuple(
            ProjectTombstone(
                tombstone_id=str(row['tombstone_id']),
                project_id=str(row['project_id']),
                document_id=str(row['document_id']),
                display_name=str(row['display_name']),
                deleted_at_utc=str(row['deleted_at_utc']),
                removed_rows=int(row['removed_rows']),
                estimated_bytes=int(row['estimated_bytes']),
                authorities_json=str(row['authorities_json']),
            )
            for row in rows
        )


__all__ = [
    'AssetDependencySummary',
    'DeletedAuthorityCount',
    'DeletionBlocker',
    'ProjectDeletionBlockedError',
    'ProjectDeletionPlan',
    'ProjectLibrary',
    'ProjectLifecycleError',
    'ProjectNotFoundError',
    'ProjectRecord',
    'ProjectTombstone',
]
