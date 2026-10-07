"""Append-only persistence for the #801 production-readiness gate.

Two tables in one repository — adoption decisions
(``cad_production_readiness_decisions``) and issued surface
enablements (``cad_recommendation_surface_decisions``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_production_readiness import (
    ProductionReadinessDecision,
    ProductionReadinessIntegrityError,
    RecommendationSurfaceDecision,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class ProductionReadinessConflictError(ValueError):
    """A production-readiness save violated append-only rules."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ProductionReadinessIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ProductionReadinessIntegrityError(
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
            raise ProductionReadinessConflictError(
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
            raise ProductionReadinessIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ProductionReadinessIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ProductionReadinessIntegrityError(
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
                raise ProductionReadinessIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadProductionReadinessRepository:
    """Native storage for the #801 production-readiness gate."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_production_readiness_decisions',
                'cad_recommendation_surface_decisions',
            )
        self.decisions = _SealedStore(
            self._connect,
            'cad_production_readiness_decisions',
            ProductionReadinessDecision,
            'decision_id', 'decision_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('scene_ref_id', 'scene_ref'),
                _ref('solver_version_ref_id', 'solver_version_ref'),
                ('solver_path_kind', 'solver_path_kind'),
                ('outcome', 'outcome'),
                ('decided_at_utc', 'decided_at_utc'),
            ),
        )
        self.surface_decisions = _SealedStore(
            self._connect,
            'cad_recommendation_surface_decisions',
            RecommendationSurfaceDecision,
            'surface_decision_id', 'surface_decision_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('decision_ref_id', 'decision_ref'),
                ('outcome_at_issue', 'outcome_at_issue'),
                ('issued_at_utc', 'issued_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def get_decision(
            self, decision_id: str,
    ) -> ProductionReadinessDecision | None:
        return self.decisions.get(decision_id)

    def get_surface_decision(
            self, surface_decision_id: str,
    ) -> RecommendationSurfaceDecision | None:
        return self.surface_decisions.get(surface_decision_id)
