"""Append-only persistence for the #838 delegated-provider authority.

Three tables in one repository — provider capability manifests
(``cad_delegated_provider_manifests``), provider acquisition records
(``cad_provider_acquisitions``), and file-export deployment evidence
(``cad_file_deployments``). File deployments live here rather than in
the #806 store because they carry a strictly weaker truth ceiling:
``runtime_not_attested`` / ``post_measurement_verified`` only — a file
write is never a verified runtime state.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_delegated_provider import (
    DelegatedProviderManifest,
    ProviderAcquisitionRecord,
)
from .cad_file_export_deployment import FileExportDeployment


class DelegatedProviderConflictError(ValueError):
    """A delegated-authority save violated append-only identity rules."""


class DelegatedProviderIntegrityError(ValueError):
    """A stored delegated-authority row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DelegatedProviderIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DelegatedProviderIntegrityError(
            'record id does not match its sealed sha256')


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise DelegatedProviderConflictError(
                f'{self.table} records are append-only')
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise DelegatedProviderIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise DelegatedProviderIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise DelegatedProviderIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise DelegatedProviderIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadDelegatedProviderRepository:
    """Native storage for the #838 delegated-provider + file-export
    authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_delegated_provider_manifests',
                'cad_provider_acquisitions',
                'cad_file_deployments',
            )
        self.provider_manifests = _SealedStore(
            self._connect,
            'cad_delegated_provider_manifests',
            DelegatedProviderManifest, 'manifest_id',
            'manifest_sha256',
            (
                ('document_id', '__document_id__'),
                ('provider_class', 'provider_class'),
                ('provider_id', 'provider_id'),
                ('adapter_id', 'adapter_id'),
                ('endpoint_kind', 'endpoint_kind'),
                ('declared_at_utc', 'declared_at_utc'),
            ),
        )
        self.acquisitions = _SealedStore(
            self._connect, 'cad_provider_acquisitions',
            ProviderAcquisitionRecord, 'acquisition_id',
            'acquisition_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('manifest_ref_id', 'manifest_ref'),
                ('capability', 'capability'),
                ('outcome', 'outcome'),
                ('observed_at_utc', 'observed_at_utc'),
            ),
        )
        self.file_deployments = _SealedStore(
            self._connect, 'cad_file_deployments',
            FileExportDeployment, 'file_deployment_id',
            'file_deployment_sha256',
            (
                ('document_id', '__document_id__'),
                ('target_class', 'target_class'),
                ('file_state', 'file_state'),
                ('runtime_state', 'runtime_state'),
                ('evidence_mode', 'evidence_mode'),
                ('roundtrip_verdict', 'roundtrip_verdict'),
                ('evaluated_at_utc', 'evaluated_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # provider manifests ---------------------------------------------

    def save_provider_manifest(
        self, record: DelegatedProviderManifest,
    ) -> None:
        self.provider_manifests.save(record)

    def get_provider_manifest(
        self, manifest_id: str,
    ) -> DelegatedProviderManifest | None:
        return self.provider_manifests.get(manifest_id)

    def list_provider_manifests(
        self, document_id: str | None = None,
    ) -> tuple[DelegatedProviderManifest, ...]:
        return self.provider_manifests.list(document_id)

    # acquisitions ----------------------------------------------------

    def save_acquisition(
        self, record: ProviderAcquisitionRecord,
    ) -> None:
        self.acquisitions.save(record)

    def get_acquisition(
        self, acquisition_id: str,
    ) -> ProviderAcquisitionRecord | None:
        return self.acquisitions.get(acquisition_id)

    def list_acquisitions(
        self, document_id: str | None = None,
    ) -> tuple[ProviderAcquisitionRecord, ...]:
        return self.acquisitions.list(document_id)

    # file deployments ------------------------------------------------

    def save_file_deployment(
        self, record: FileExportDeployment,
    ) -> None:
        self.file_deployments.save(record)

    def get_file_deployment(
        self, file_deployment_id: str,
    ) -> FileExportDeployment | None:
        return self.file_deployments.get(file_deployment_id)

    def list_file_deployments(
        self, document_id: str | None = None,
    ) -> tuple[FileExportDeployment, ...]:
        return self.file_deployments.list(document_id)


__all__ = [
    'CadDelegatedProviderRepository',
    'DelegatedProviderConflictError',
    'DelegatedProviderIntegrityError',
]
