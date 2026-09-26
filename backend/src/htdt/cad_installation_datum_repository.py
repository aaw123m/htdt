"""Append-only persistence for installation datum records (#537)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_installation_datum import InstallationDatum
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


class InstallationDatumConflictError(ValueError):
    """A datum save violated append-only identity rules."""


class CadInstallationDatumRepository:
    """Native storage for versioned InstallationDatum records.

    ``(datum_id, version)`` is saved exactly once: redefining the datum means
    appending a new version row, never an UPDATE. Every save re-checks that
    the pinned SceneRevision exists with the recorded content hash, so a
    datum can never be written against room geometry that was never
    persisted.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_installation_datums')

    def save_datum(self, datum: InstallationDatum) -> None:
        if self.get_datum(datum.datum_id, datum.version) is not None:
            raise InstallationDatumConflictError(
                'InstallationDatum (datum_id, version) is append-only'
            )
        revision = self.scene_repository.get(datum.scene_revision_id)
        if revision is None:
            raise ValueError('datum pins a SceneRevision that is not persisted')
        if revision.content_hash != datum.scene_content_hash:
            raise ValueError('datum SceneRevision content hash mismatch')
        if revision.document_id != datum.document_id:
            raise ValueError('datum SceneRevision belongs to another document')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_installation_datums (
                    datum_id, version, document_id, scene_revision_id,
                    scene_content_hash, semantic_sha256, payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datum.datum_id,
                    datum.version,
                    datum.document_id,
                    datum.scene_revision_id,
                    datum.scene_content_hash,
                    datum.semantic_sha256,
                    datum.model_dump_json(),
                    datum.created_at_utc,
                ),
            )

    def get_datum(
        self,
        datum_id: str,
        version: str,
    ) -> InstallationDatum | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_installation_datums
                WHERE datum_id=? AND version=?
                """,
                (datum_id, version),
            ).fetchone()
        if row is None:
            return None
        return InstallationDatum.model_validate_json(row['payload_json'])

    def get_datum_by_hash(
        self,
        semantic_sha256: str,
    ) -> InstallationDatum | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_installation_datums
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return InstallationDatum.model_validate_json(row['payload_json'])

    def list_datums(
        self,
        document_id: str,
    ) -> tuple[InstallationDatum, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_installation_datums
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            InstallationDatum.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_datum_versions(
        self,
        datum_id: str,
    ) -> tuple[InstallationDatum, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_installation_datums
                WHERE datum_id=?
                ORDER BY seq ASC
                """,
                (datum_id,),
            ).fetchall()
        return tuple(
            InstallationDatum.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadInstallationDatumRepository',
    'InstallationDatumConflictError',
]
