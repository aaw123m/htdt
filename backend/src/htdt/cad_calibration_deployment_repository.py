"""Append-only persistence for the #806 calibration-deployment authority.

Four tables in one repository — capability declarations
(``cad_deployment_capability_declarations``), sealed deployments
(``cad_calibration_deployments``), before/after effectiveness reports
(``cad_deployment_effectiveness_reports``) and scoped rollbacks
(``cad_deployment_rollbacks``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_calibration_deployment import (
    CalibrationDeployment,
    DeploymentCapabilityDeclaration,
    DeploymentEffectivenessReport,
    DeploymentRollbackRecord,
)


class DeploymentConflictError(ValueError):
    """A deployment-authority save violated append-only identity rules."""


class DeploymentIntegrityError(ValueError):
    """A stored deployment-authority row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DeploymentIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DeploymentIntegrityError(
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
            raise DeploymentConflictError(
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
            raise DeploymentIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise DeploymentIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise DeploymentIntegrityError(
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
                raise DeploymentIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCalibrationDeploymentRepository:
    """Native storage for the #806 deployment/verification authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_deployment_capability_declarations',
                'cad_calibration_deployments',
                'cad_deployment_effectiveness_reports',
                'cad_deployment_rollbacks',
            )
        self.capability_declarations = _SealedStore(
            self._connect,
            'cad_deployment_capability_declarations',
            DeploymentCapabilityDeclaration, 'declaration_id',
            'declaration_sha256',
            (
                ('document_id', '__document_id__'),
                ('adapter_id', 'adapter_id'),
                ('adapter_kind', 'adapter_kind'),
            ),
        )
        self.deployments = _SealedStore(
            self._connect, 'cad_calibration_deployments',
            CalibrationDeployment, 'deployment_id',
            'deployment_sha256',
            (
                ('document_id', '__document_id__'),
                ('deployment_state', 'deployment_state'),
                ('evidence_mode', 'evidence_mode'),
                ('target_class', 'target_class'),
            ),
        )
        self.effectiveness_reports = _SealedStore(
            self._connect, 'cad_deployment_effectiveness_reports',
            DeploymentEffectivenessReport, 'report_id',
            'report_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.rollbacks = _SealedStore(
            self._connect, 'cad_deployment_rollbacks',
            DeploymentRollbackRecord, 'rollback_id',
            'rollback_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # capability declarations --------------------------------------

    def save_capability_declaration(
        self, record: DeploymentCapabilityDeclaration,
    ) -> None:
        self.capability_declarations.save(record)

    def get_capability_declaration(
        self, declaration_id: str,
    ) -> DeploymentCapabilityDeclaration | None:
        return self.capability_declarations.get(declaration_id)

    def list_capability_declarations(
        self, document_id: str | None = None,
    ) -> tuple[DeploymentCapabilityDeclaration, ...]:
        return self.capability_declarations.list(document_id)

    # deployments ---------------------------------------------------

    def save_deployment(self, record: CalibrationDeployment) -> None:
        self.deployments.save(record)

    def get_deployment(
        self, deployment_id: str,
    ) -> CalibrationDeployment | None:
        return self.deployments.get(deployment_id)

    def list_deployments(
        self, document_id: str | None = None,
    ) -> tuple[CalibrationDeployment, ...]:
        return self.deployments.list(document_id)

    # effectiveness -------------------------------------------------

    def save_effectiveness_report(
        self, record: DeploymentEffectivenessReport,
    ) -> None:
        self.effectiveness_reports.save(record)

    def get_effectiveness_report(
        self, report_id: str,
    ) -> DeploymentEffectivenessReport | None:
        return self.effectiveness_reports.get(report_id)

    def list_effectiveness_reports(
        self, document_id: str | None = None,
    ) -> tuple[DeploymentEffectivenessReport, ...]:
        return self.effectiveness_reports.list(document_id)

    # rollbacks -----------------------------------------------------

    def save_rollback(self, record: DeploymentRollbackRecord) -> None:
        self.rollbacks.save(record)

    def get_rollback(
        self, rollback_id: str,
    ) -> DeploymentRollbackRecord | None:
        return self.rollbacks.get(rollback_id)

    def list_rollbacks(
        self, document_id: str | None = None,
    ) -> tuple[DeploymentRollbackRecord, ...]:
        return self.rollbacks.list(document_id)
