"""Append-only persistence for the AV signal-path authority (#817).

Two authorities live here:

* ``cad_signal_paths`` — immutable versioned signal paths, keyed by
  ``(document_id, path_id, version)`` with the semantic hash carried as an
  indexed column. Saves are append-only: re-saving an identical row is a
  no-op, a divergent hash for the same key is a conflict.
* ``cad_signal_path_selections`` — an append-only log of the project's
  explicit current-path selections. ``current_selection`` returns the
  newest recorded selection; "current" is therefore an explicit persisted
  record, never a latest-created inference.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_signal_path import AVSignalPath
from .clock import utc_now_iso as _utc_now


class SignalPathConflictError(ValueError):
    """A signal-path save violated append-only identity rules."""


class SignalPathIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class SignalPathSelection(BaseModel):
    """One recorded current-path selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    path_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadSignalPathRepository:
    """Native storage for signal paths and current-path selections."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_signal_paths',
                'cad_signal_path_selections',
            )

    # ------------------------------------------------------------------
    # Signal paths

    def save_path(
        self,
        path: AVSignalPath,
        document_id: str,
    ) -> None:
        existing = self.get_path(document_id, path.path_id, path.version)
        if existing is not None:
            if existing.path_sha256 == path.path_sha256:
                return
            raise SignalPathConflictError(
                'signal path (document_id, path_id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_signal_paths (
                    document_id, path_id, version, path_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    path.path_id,
                    path.version,
                    path.path_sha256,
                    _utc_now(),
                    path.model_dump_json(),
                ),
            )

    def get_path(
        self,
        document_id: str,
        path_id: str,
        version: str,
    ) -> AVSignalPath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT path_id, version, path_sha256, payload_json
                FROM cad_signal_paths
                WHERE document_id=? AND path_id=? AND version=?
                """,
                (document_id, path_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def get_path_by_hash(
        self,
        path_sha256: str,
    ) -> AVSignalPath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT path_id, version, path_sha256, payload_json
                FROM cad_signal_paths
                WHERE path_sha256=?
                """,
                (path_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def list_paths(
        self,
        document_id: str,
    ) -> tuple[AVSignalPath, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT path_id, version, path_sha256, payload_json
                FROM cad_signal_paths
                WHERE document_id=?
                ORDER BY created_at_utc, path_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._path_from_row(row) for row in rows)

    def _path_from_row(self, row: sqlite3.Row) -> AVSignalPath:
        path = AVSignalPath.model_validate_json(row['payload_json'])
        if (
            path.path_id != row['path_id']
            or path.version != row['version']
            or path.path_sha256 != row['path_sha256']
        ):
            raise SignalPathIntegrityError(
                'signal path row disagrees with its payload'
            )
        return path

    # ------------------------------------------------------------------
    # Current selections

    def select_path(
        self,
        document_id: str,
        path: AVSignalPath,
        selected_at_utc: str | None = None,
    ) -> SignalPathSelection:
        persisted = self.get_path(document_id, path.path_id, path.version)
        if persisted is None or persisted.path_sha256 != path.path_sha256:
            raise SignalPathIntegrityError(
                'selection must reference a persisted signal path'
            )
        selection = SignalPathSelection(
            document_id=document_id,
            path_id=path.path_id,
            version=path.version,
            path_sha256=path.path_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_signal_path_selections (
                    document_id, path_id, version, path_sha256,
                    selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
                    selection.path_id,
                    selection.version,
                    selection.path_sha256,
                    selection.selected_at_utc,
                    selection.model_dump_json(),
                ),
            )
        return selection

    def current_selection(
        self,
        document_id: str,
    ) -> SignalPathSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, path_id, version, path_sha256,
                       selected_at_utc, payload_json
                FROM cad_signal_path_selections
                WHERE document_id=?
                ORDER BY selection_seq DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return self._selection_from_row(row)

    def list_selections(
        self,
        document_id: str,
    ) -> tuple[SignalPathSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, path_id, version, path_sha256,
                       selected_at_utc, payload_json
                FROM cad_signal_path_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_path(
        self,
        document_id: str,
    ) -> AVSignalPath | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        path = self.get_path(document_id, selection.path_id, selection.version)
        if path is None or path.path_sha256 != selection.path_sha256:
            raise SignalPathIntegrityError(
                'current selection does not resolve to its persisted signal path'
            )
        return path

    def _selection_from_row(self, row: sqlite3.Row) -> SignalPathSelection:
        selection = SignalPathSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.path_id != row['path_id']
            or selection.version != row['version']
            or selection.path_sha256 != row['path_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise SignalPathIntegrityError(
                'signal path selection row disagrees with its payload'
            )
        return selection
