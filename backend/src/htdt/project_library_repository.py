"""Native project/document library repository (#450).

Manages the user-facing project layer over the shared native database:
``htdt_project_documents`` holds one row per project bound to exactly one
``document_id``. The repository also owns the migration contract — every
``document_id`` that already has scene content (``scene_document_heads``)
is registered as an existing project instead of being silently replaced,
with the legacy ``F1_DOCUMENT_ID`` labelled by its provenance
classification (#627).

Display names are presentation only and are never used as identity.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID
from .cad_schema import require_native_tables, connect_sqlite
from .default_document import (
    LEGACY_PROJECT_LABEL,
    SYNTHETIC_FIXTURE_LABEL,
    classify_default_document,
)
from .project_library import (
    ProjectArchivedError,
    ProjectLibraryEntry,
    ProjectLibraryError,
    ProjectNotFoundError,
)
from .clock import utc_now_iso as _utc_now


DEFAULT_PROJECT_NAME = 'My Home Theater'


class ProjectLibraryRepository:
    """Create, open, switch and manage HTDT projects in one data directory."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.repository = scene_repository
        self.path = Path(scene_repository.path)
        self._initialize()
        self._migrate_existing_documents()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection, 'htdt_project_documents', 'htdt_legacy_imports'
            )

    def _migrate_existing_documents(self) -> None:
        """Register every document that already carries scene content.

        Runs idempotently at open: any ``document_id`` present in
        ``scene_document_heads`` without a library row gets one, so
        pre-#450 databases surface their data as named projects.
        """

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT document_id FROM scene_document_heads ORDER BY document_id ASC'
            ).fetchall()
            known = {
                row['document_id']
                for row in connection.execute(
                    'SELECT document_id FROM htdt_project_documents'
                )
            }
            for row in rows:
                document_id = row['document_id']
                if document_id in known:
                    continue
                if document_id == F1_DOCUMENT_ID:
                    report = classify_default_document(
                        self.repository, document_id
                    )
                    display_name = report.label or LEGACY_PROJECT_LABEL
                else:
                    display_name = document_id
                now = _utc_now()
                connection.execute(
                    '''INSERT INTO htdt_project_documents(
                        project_id, document_id, display_name, description,
                        created_at_utc, updated_at_utc, archived
                    ) VALUES (?, ?, ?, NULL, ?, ?, 0)''',
                    (
                        str(uuid4()),
                        document_id,
                        display_name,
                        now,
                        now,
                    ),
                )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> ProjectLibraryEntry:
        return ProjectLibraryEntry(
            project_id=str(row['project_id']),
            document_id=str(row['document_id']),
            display_name=str(row['display_name']),
            description=row['description'],
            created_at_utc=str(row['created_at_utc']),
            last_opened_at_utc=row['last_opened_at_utc'],
            archived=bool(row['archived']),
            cloned_from_project_id=row['cloned_from_project_id'],
            source_revision_id=row['source_revision_id'],
        )

    def _get_row(self, project_id: str) -> sqlite3.Row:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE project_id=?',
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(f'unknown project: {project_id}')
        return row

    def get_project(self, project_id: str) -> ProjectLibraryEntry:
        return self._entry_from_row(self._get_row(project_id))

    def get_by_document_id(self, document_id: str) -> ProjectLibraryEntry | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM htdt_project_documents WHERE document_id=?',
                (document_id,),
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def ensure_document_registered(
        self, document_id: str, display_name: str | None = None
    ) -> ProjectLibraryEntry:
        """Bind a ``document_id`` to a project row, creating one if absent."""

        existing = self.get_by_document_id(document_id)
        if existing is not None:
            return existing
        return self.create_project(
            display_name or document_id, document_id=document_id
        )

    def list_projects(
        self, *, include_archived: bool = False
    ) -> tuple[ProjectLibraryEntry, ...]:
        """Library listing; archived rows are hidden unless requested."""

        query = (
            'SELECT * FROM htdt_project_documents'
            + ('' if include_archived else ' WHERE archived=0')
            # Opened projects outrank never-opened ones — a freshly created
            # but never-opened project must not become startup-most-recent.
            # rowid tiebreak: clock granularity can tie timestamps; prefer
            # the more recently created project, deterministically.
            + ' ORDER BY (last_opened_at_utc IS NULL) ASC,'
              ' last_opened_at_utc DESC, created_at_utc DESC, rowid DESC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query).fetchall()
        return tuple(self._entry_from_row(row) for row in rows)

    def recent_projects(self, limit: int = 10) -> tuple[ProjectLibraryEntry, ...]:
        return self.list_projects()[:limit]

    def most_recent_project(self) -> ProjectLibraryEntry | None:
        projects = self.recent_projects(limit=1)
        return projects[0] if projects else None

    def create_project(
        self,
        display_name: str,
        *,
        description: str | None = None,
        document_id: str | None = None,
        cloned_from_project_id: str | None = None,
        source_revision_id: str | None = None,
    ) -> ProjectLibraryEntry:
        display_name = display_name.strip()
        if not display_name:
            raise ProjectLibraryError('project display name must not be empty')
        document_id = document_id or str(uuid4())
        entry = ProjectLibraryEntry(
            project_id=str(uuid4()),
            document_id=document_id,
            display_name=display_name,
            description=description,
            created_at_utc=_utc_now(),
            cloned_from_project_id=cloned_from_project_id,
            source_revision_id=source_revision_id,
        )
        try:
            with closing(self._connect()) as connection, connection:
                self._insert_entry(connection, entry)
        except sqlite3.IntegrityError as exc:
            raise ProjectLibraryError(
                f'document already belongs to a project: {document_id}'
            ) from exc
        return entry

    def _insert_entry(
        self, connection: sqlite3.Connection, entry: ProjectLibraryEntry
    ) -> None:
        """Insert one project row on a caller-held connection.

        Used by ``create_project`` and by bulk flows (legacy migration,
        bundle import) that must commit several project rows plus their
        provenance markers in ONE transaction — never a partially imported
        set.
        """

        connection.execute(
            '''INSERT INTO htdt_project_documents(
                project_id, document_id, display_name, description,
                created_at_utc, updated_at_utc, last_opened_at_utc,
                archived, archived_at_utc, cloned_from_project_id,
                source_revision_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?)''',
            (
                entry.project_id,
                entry.document_id,
                entry.display_name,
                entry.description,
                entry.created_at_utc,
                entry.created_at_utc,
                entry.cloned_from_project_id,
                entry.source_revision_id,
            ),
        )

    def open_project(self, project_id: str) -> ProjectLibraryEntry:
        """Mark the project opened and return it; archived rows refuse."""

        row = self._get_row(project_id)
        if row['archived']:
            raise ProjectArchivedError(f'project is archived: {project_id}')
        with closing(self._connect()) as connection, connection:
            now = _utc_now()
            # The wall clock is not a sequence: rapid opens inside one tick
            # share a stamp, and a stale or slewed clock can even write an
            # earlier one — recency then silently re-orders on the rowid
            # tiebreak. Floor the stamp at previous-max + 1us so every open
            # lands strictly after every earlier open.
            latest = connection.execute(
                'SELECT MAX(last_opened_at_utc) FROM htdt_project_documents'
            ).fetchone()[0]
            if latest is not None:
                try:
                    latest_dt = datetime.fromisoformat(latest)
                    now_dt = datetime.fromisoformat(now)
                except ValueError:
                    latest_dt = now_dt = None
                if latest_dt is not None and now_dt <= latest_dt:
                    now = (latest_dt + timedelta(microseconds=1)).isoformat(
                        timespec='microseconds'
                    )
            connection.execute(
                'UPDATE htdt_project_documents SET last_opened_at_utc=?, '
                'updated_at_utc=? WHERE project_id=?',
                (now, now, project_id),
            )
        return self.get_project(project_id)

    def resolve_startup_document(
        self, document_id: str | None, *, skip_last_opened: bool = False
    ) -> ProjectLibraryEntry:
        """Resolve the document the shell should open without --document-id.

        An explicit ``document_id`` registers itself as a project; otherwise
        the most recently opened project wins. An empty library gets a
        freshly created project with its own document identity — startup
        never silently binds to the legacy default document id (#781),
        which only becomes a project through the content migration path
        when it actually carries a scene.

        ``skip_last_opened`` implements Safe Mode's
        ``auto_open_last_project=False``: the last-opened project is the
        prime suspect when the previous launch died, so the shell opens
        the next most recent project instead — or a fresh default project
        when the library holds only the suspect. The skipped project stays
        in the library untouched (and its ``last_opened`` ranking keeps it
        out of the auto-open slot until the user deliberately opens it).
        """

        if document_id is not None:
            entry = self.ensure_document_registered(document_id)
            return self.open_project(entry.project_id)
        recents = self.recent_projects()
        entry = recents[1] if (skip_last_opened and len(recents) > 1) else None
        if entry is None and not skip_last_opened:
            entry = recents[0] if recents else None
        if entry is None:
            entry = self.create_project(DEFAULT_PROJECT_NAME)
        return self.open_project(entry.project_id)

    def rename_project(
        self, project_id: str, display_name: str
    ) -> ProjectLibraryEntry:
        """Change presentation only — project/document identity is kept."""

        display_name = display_name.strip()
        if not display_name:
            raise ProjectLibraryError('project display name must not be empty')
        self._get_row(project_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE htdt_project_documents SET display_name=?, '
                'updated_at_utc=? WHERE project_id=?',
                (display_name, _utc_now(), project_id),
            )
        return self.get_project(project_id)

    def set_archived(
        self, project_id: str, archived: bool
    ) -> ProjectLibraryEntry:
        """Archive/unarchive; archived projects are hidden from recents."""

        self._get_row(project_id)
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE htdt_project_documents SET archived=?, '
                'archived_at_utc=?, updated_at_utc=? WHERE project_id=?',
                (1 if archived else 0, now if archived else None, now, project_id),
            )
        return self.get_project(project_id)

    def duplicate_project(
        self, project_id: str, display_name: str
    ) -> ProjectLibraryEntry:
        """Clone the project's whole document into a new independent one.

        The clone travels through the #488 bundle machinery — every
        document-scoped row (the full revision chain, not just the current
        head), closure dependencies, and managed assets are remapped with
        fresh identities — so the semantic identity is new and only
        provenance points back (``cloned_from_project_id`` + the source
        head ``revision_id``).
        """

        # Deferred import: project_bundle already depends on this module.
        from .project_bundle import (
            export_project_bundle,
            import_project_bundle,
        )

        source = self._entry_from_row(self._get_row(project_id))
        head = self.repository.current_head(source.document_id)
        if head is None:
            # Empty project: nothing for a bundle to carry — clone the
            # library row directly with the same lineage columns.
            return self.create_project(
                display_name,
                cloned_from_project_id=source.project_id,
            )
        data_dir = self.path.parent
        descriptor, temp_name = tempfile.mkstemp(
            dir=data_dir, prefix='.duplicate-', suffix='.htdtproject'
        )
        os.close(descriptor)
        bundle_path = Path(temp_name)
        try:
            export_project_bundle(
                self.repository,
                source.document_id,
                bundle_path,
                display_name=display_name,
            )
            result = import_project_bundle(
                self.repository, bundle_path, import_as_copy=True
            )
        finally:
            bundle_path.unlink(missing_ok=True)
        now = _utc_now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'UPDATE htdt_project_documents SET cloned_from_project_id=?, '
                'source_revision_id=?, updated_at_utc=? WHERE document_id=?',
                (
                    source.project_id,
                    head.revision_id if head is not None else None,
                    now,
                    result.document_id,
                ),
            )
        clone = self.get_by_document_id(result.document_id)
        if clone is None:
            raise ProjectLibraryError(
                'project clone import produced no library entry'
            )
        return clone
