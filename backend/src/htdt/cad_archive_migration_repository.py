"""Append-only persistence for project archival / schema-migration
authority (#718, REV59-DEPS).

Four tables:

* ``cad_archive_snapshots`` — sealed archive units.
* ``cad_archive_verifications`` — sealed re-read verdicts.
* ``cad_migration_records`` — sealed migration identities.
* ``cad_migration_verifications`` — sealed preservation verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_archive_migration import (
    ArchiveSnapshot,
    ArchiveVerification,
    MigrationRecord,
    MigrationVerification,
)


class ArchiveMigrationConflictError(ValueError):
    """An archive/migration save violated append-only identity."""


class ArchiveMigrationIntegrityError(ValueError):
    """A stored archive/migration row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ArchiveMigrationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ArchiveMigrationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadArchiveMigrationRepository:
    """Native storage for the #718 archive/migration records."""

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
                'cad_archive_snapshots',
                'cad_archive_verifications',
                'cad_migration_records',
                'cad_migration_verifications',
            )

    # ------------------------------------------------------------------
    # Archive snapshots

    def save_snapshot(self, snapshot: ArchiveSnapshot) -> None:
        _assert_sealed(snapshot, 'archive_sha256', 'archive_id')
        existing = self.get_snapshot(snapshot.archive_id)
        if existing is not None:
            if existing.archive_sha256 == snapshot.archive_sha256:
                return
            raise ArchiveMigrationConflictError(
                'archive snapshots are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_archive_snapshots (
                    archive_id, archive_sha256, document_id,
                    archive_label, schema_version, content_hash,
                    preservation_scope, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.archive_id,
                    snapshot.archive_sha256,
                    snapshot.document_id,
                    snapshot.archive_label,
                    snapshot.schema_version,
                    snapshot.content_hash,
                    snapshot.preservation_scope,
                    snapshot.captured_at_utc,
                    snapshot.model_dump_json(),
                ),
            )

    def get_snapshot(
        self, archive_id: str
    ) -> ArchiveSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_archive_snapshots WHERE archive_id=?',
                (archive_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = ArchiveSnapshot.model_validate_json(
            row['payload_json']
        )
        if (
            snapshot.archive_id != row['archive_id']
            or snapshot.archive_sha256 != row['archive_sha256']
            or snapshot.document_id != row['document_id']
            or snapshot.archive_label != row['archive_label']
            or snapshot.schema_version != row['schema_version']
            or snapshot.content_hash != row['content_hash']
            or snapshot.preservation_scope != row['preservation_scope']
            or snapshot.captured_at_utc != row['captured_at_utc']
        ):
            raise ArchiveMigrationIntegrityError(
                'archive snapshot row disagrees with payload'
            )
        return snapshot

    def list_snapshots(
        self, document_id: str
    ) -> tuple[ArchiveSnapshot, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_archive_snapshots '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ArchiveSnapshot.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Archive verifications

    def save_archive_verification(
        self, verification: ArchiveVerification
    ) -> None:
        _assert_sealed(
            verification, 'verification_sha256', 'verification_id'
        )
        existing = self.get_archive_verification(
            verification.verification_id
        )
        if existing is not None:
            if (
                existing.verification_sha256
                == verification.verification_sha256
            ):
                return
            raise ArchiveMigrationConflictError(
                'archive verifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_archive_verifications (
                    verification_id, verification_sha256, document_id,
                    archive_ref_id, status, check_count, verified_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification.verification_id,
                    verification.verification_sha256,
                    verification.document_id,
                    verification.archive_ref.ref_id,
                    verification.status,
                    len(verification.checks),
                    verification.verified_at_utc,
                    verification.model_dump_json(),
                ),
            )

    def get_archive_verification(
        self, verification_id: str
    ) -> ArchiveVerification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_archive_verifications '
                'WHERE verification_id=?',
                (verification_id,),
            ).fetchone()
        if row is None:
            return None
        verification = ArchiveVerification.model_validate_json(
            row['payload_json']
        )
        if (
            verification.verification_id != row['verification_id']
            or verification.verification_sha256
            != row['verification_sha256']
            or verification.document_id != row['document_id']
            or verification.archive_ref.ref_id != row['archive_ref_id']
            or verification.status != row['status']
            or len(verification.checks) != row['check_count']
            or verification.verified_at_utc != row['verified_at_utc']
        ):
            raise ArchiveMigrationIntegrityError(
                'archive verification row disagrees with payload'
            )
        return verification

    def list_archive_verifications(
        self, document_id: str
    ) -> tuple[ArchiveVerification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_archive_verifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ArchiveVerification.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Migration records

    def save_migration(self, migration: MigrationRecord) -> None:
        _assert_sealed(migration, 'migration_sha256', 'migration_id')
        existing = self.get_migration(migration.migration_id)
        if existing is not None:
            if existing.migration_sha256 == migration.migration_sha256:
                return
            raise ArchiveMigrationConflictError(
                'migration records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_migration_records (
                    migration_id, migration_sha256, document_id, kind,
                    source_archive_ref_id, target_archive_ref_id,
                    from_schema_version, to_schema_version,
                    migration_status_at_write, migrated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration.migration_id,
                    migration.migration_sha256,
                    migration.document_id,
                    migration.kind,
                    migration.source_archive_ref.ref_id,
                    migration.target_archive_ref.ref_id,
                    migration.from_schema_version,
                    migration.to_schema_version,
                    migration.migration_status_at_write,
                    migration.migrated_at_utc,
                    migration.model_dump_json(),
                ),
            )

    def get_migration(
        self, migration_id: str
    ) -> MigrationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_migration_records WHERE migration_id=?',
                (migration_id,),
            ).fetchone()
        if row is None:
            return None
        migration = MigrationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            migration.migration_id != row['migration_id']
            or migration.migration_sha256 != row['migration_sha256']
            or migration.document_id != row['document_id']
            or migration.kind != row['kind']
            or migration.source_archive_ref.ref_id
            != row['source_archive_ref_id']
            or migration.target_archive_ref.ref_id
            != row['target_archive_ref_id']
            or migration.from_schema_version != row['from_schema_version']
            or migration.to_schema_version != row['to_schema_version']
            or migration.migration_status_at_write
            != row['migration_status_at_write']
            or migration.migrated_at_utc != row['migrated_at_utc']
        ):
            raise ArchiveMigrationIntegrityError(
                'migration record row disagrees with payload'
            )
        return migration

    def list_migrations(
        self, document_id: str
    ) -> tuple[MigrationRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_migration_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MigrationRecord.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Migration verifications

    def save_migration_verification(
        self, verification: MigrationVerification
    ) -> None:
        _assert_sealed(
            verification, 'verification_sha256', 'verification_id'
        )
        existing = self.get_migration_verification(
            verification.verification_id
        )
        if existing is not None:
            if (
                existing.verification_sha256
                == verification.verification_sha256
            ):
                return
            raise ArchiveMigrationConflictError(
                'migration verifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_migration_verifications (
                    verification_id, verification_sha256, document_id,
                    migration_ref_id, status, check_count,
                    verified_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verification.verification_id,
                    verification.verification_sha256,
                    verification.document_id,
                    verification.migration_ref.ref_id,
                    verification.status,
                    len(verification.checks),
                    verification.verified_at_utc,
                    verification.model_dump_json(),
                ),
            )

    def get_migration_verification(
        self, verification_id: str
    ) -> MigrationVerification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_migration_verifications '
                'WHERE verification_id=?',
                (verification_id,),
            ).fetchone()
        if row is None:
            return None
        verification = MigrationVerification.model_validate_json(
            row['payload_json']
        )
        if (
            verification.verification_id != row['verification_id']
            or verification.verification_sha256
            != row['verification_sha256']
            or verification.document_id != row['document_id']
            or verification.migration_ref.ref_id
            != row['migration_ref_id']
            or verification.status != row['status']
            or len(verification.checks) != row['check_count']
            or verification.verified_at_utc != row['verified_at_utc']
        ):
            raise ArchiveMigrationIntegrityError(
                'migration verification row disagrees with payload'
            )
        return verification

    def list_migration_verifications(
        self, document_id: str
    ) -> tuple[MigrationVerification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_migration_verifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MigrationVerification.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'ArchiveMigrationConflictError',
    'ArchiveMigrationIntegrityError',
    'CadArchiveMigrationRepository',
]
