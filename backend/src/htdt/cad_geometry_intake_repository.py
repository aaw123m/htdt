"""Append-only persistence for the #866 geometry-intake authority.

Five sealed tables in one repository, mirroring the
``cad_delegated_provider_repository`` pattern:

* ``cad_geometry_intake_reports`` — health-check defect reports (gdr-);
* ``cad_geometry_repair_proposals`` — proposed repairs (grp-);
* ``cad_geometry_repair_acceptances`` — operator decision records (gra-);
* ``cad_derived_geometry_revisions`` — immutable derived geometry (gdv-);
* ``cad_geometry_solver_readiness`` — readiness verdicts (srv-).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_geometry_intake import (
    DerivedGeometryRevision,
    GeometryIntakeReport,
    GeometryRepairAcceptance,
    GeometryRepairProposal,
    GeometrySolverReadinessVerdict,
)


class GeometryIntakeConflictError(ValueError):
    """A geometry-intake save violated append-only identity rules."""


class GeometryIntakeIntegrityError(ValueError):
    """A stored geometry-intake row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(
        record.identity_payload()  # type: ignore[attr-defined]
    )
    if getattr(record, sha_field) != sha:
        raise GeometryIntakeIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise GeometryIntakeIntegrityError(
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
            raise GeometryIntakeConflictError(
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
            raise GeometryIntakeIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise GeometryIntakeIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise GeometryIntakeIntegrityError(
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
                raise GeometryIntakeIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadGeometryIntakeRepository:
    """Native storage for the #866 geometry-intake authority chain."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_geometry_intake_reports',
                'cad_geometry_repair_proposals',
                'cad_geometry_repair_acceptances',
                'cad_derived_geometry_revisions',
                'cad_geometry_solver_readiness',
            )
        self.intake_reports = _SealedStore(
            self._connect,
            'cad_geometry_intake_reports',
            GeometryIntakeReport, 'report_id',
            'report_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('subject_sha256', 'subject_sha256'),
                ('defect_count', 'defect_count'),
                ('critical_count', 'critical_count'),
                ('evaluated_at_utc', 'evaluated_at_utc'),
            ),
        )
        self.repair_proposals = _SealedStore(
            self._connect, 'cad_geometry_repair_proposals',
            GeometryRepairProposal, 'proposal_id',
            'proposal_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('report_ref_id', 'report_ref'),
                _ref('subject_ref_id', 'subject_ref'),
                ('generated_by', 'generated_by'),
                ('generated_at_utc', 'generated_at_utc'),
            ),
        )
        self.repair_acceptances = _SealedStore(
            self._connect, 'cad_geometry_repair_acceptances',
            GeometryRepairAcceptance, 'acceptance_id',
            'acceptance_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('proposal_ref_id', 'proposal_ref'),
                ('decided_by', 'decided_by'),
                ('decided_at_utc', 'decided_at_utc'),
            ),
        )
        self.derived_revisions = _SealedStore(
            self._connect, 'cad_derived_geometry_revisions',
            DerivedGeometryRevision, 'derived_revision_id',
            'derived_revision_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'source_subject_ref'),
                _ref('acceptance_ref_id', 'acceptance_ref'),
                ('derived_geometry_sha256', 'derived_geometry_sha256'),
                ('created_at_utc', 'created_at_utc'),
            ),
        )
        self.solver_readiness = _SealedStore(
            self._connect, 'cad_geometry_solver_readiness',
            GeometrySolverReadinessVerdict, 'verdict_id',
            'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                ('geometry_sha256', 'geometry_sha256'),
                _ref('adapter_ref_id', 'adapter_descriptor_ref'),
                ('verdict', 'verdict'),
                ('evaluated_at_utc', 'evaluated_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # intake reports ----------------------------------------------------

    def save_intake_report(self, record: GeometryIntakeReport) -> None:
        self.intake_reports.save(record)

    def get_intake_report(
        self, report_id: str,
    ) -> GeometryIntakeReport | None:
        return self.intake_reports.get(report_id)

    def list_intake_reports(
        self, document_id: str | None = None,
    ) -> tuple[GeometryIntakeReport, ...]:
        return self.intake_reports.list(document_id)

    # repair proposals ---------------------------------------------------

    def save_repair_proposal(self, record: GeometryRepairProposal) -> None:
        self.repair_proposals.save(record)

    def get_repair_proposal(
        self, proposal_id: str,
    ) -> GeometryRepairProposal | None:
        return self.repair_proposals.get(proposal_id)

    def list_repair_proposals(
        self, document_id: str | None = None,
    ) -> tuple[GeometryRepairProposal, ...]:
        return self.repair_proposals.list(document_id)

    # repair acceptances -------------------------------------------------

    def save_repair_acceptance(
        self, record: GeometryRepairAcceptance,
    ) -> None:
        self.repair_acceptances.save(record)

    def get_repair_acceptance(
        self, acceptance_id: str,
    ) -> GeometryRepairAcceptance | None:
        return self.repair_acceptances.get(acceptance_id)

    def list_repair_acceptances(
        self, document_id: str | None = None,
    ) -> tuple[GeometryRepairAcceptance, ...]:
        return self.repair_acceptances.list(document_id)

    # derived geometry revisions ----------------------------------------

    def save_derived_revision(
        self, record: DerivedGeometryRevision,
    ) -> None:
        self.derived_revisions.save(record)

    def get_derived_revision(
        self, derived_revision_id: str,
    ) -> DerivedGeometryRevision | None:
        return self.derived_revisions.get(derived_revision_id)

    def list_derived_revisions(
        self, document_id: str | None = None,
    ) -> tuple[DerivedGeometryRevision, ...]:
        return self.derived_revisions.list(document_id)

    # solver readiness ---------------------------------------------------

    def save_solver_readiness(
        self, record: GeometrySolverReadinessVerdict,
    ) -> None:
        self.solver_readiness.save(record)

    def get_solver_readiness(
        self, verdict_id: str,
    ) -> GeometrySolverReadinessVerdict | None:
        return self.solver_readiness.get(verdict_id)

    def list_solver_readiness(
        self, document_id: str | None = None,
    ) -> tuple[GeometrySolverReadinessVerdict, ...]:
        return self.solver_readiness.list(document_id)


__all__ = [
    'CadGeometryIntakeRepository',
    'GeometryIntakeConflictError',
    'GeometryIntakeIntegrityError',
]
