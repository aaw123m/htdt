"""Append-only persistence for the #809 benchmark-qualification authority.

Three tables in one repository — scene mappings
(``cad_benchmark_scene_mappings``), preregistrations
(``cad_benchmark_preregistrations``) and sealed qualification verdicts
(``cad_benchmark_qualifications``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_benchmark_qualification import (
    BenchmarkPreregistration,
    BenchmarkQualification,
    BenchmarkSceneMapping,
    QualificationIntegrityError,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class QualificationConflictError(ValueError):
    """A qualification-authority save violated append-only identity rules."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise QualificationIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise QualificationIntegrityError(
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
            raise QualificationConflictError(
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
            raise QualificationIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise QualificationIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise QualificationIntegrityError(
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
                raise QualificationIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadBenchmarkQualificationRepository:
    """Native storage for the #809 benchmark-qualification authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_benchmark_scene_mappings',
                'cad_benchmark_preregistrations',
                'cad_benchmark_qualifications',
            )
        self.scene_mappings = _SealedStore(
            self._connect,
            'cad_benchmark_scene_mappings',
            BenchmarkSceneMapping, 'mapping_id', 'mapping_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('asset_ref_id', 'asset_ref'),
                ('corpus_scene_id', 'corpus_scene_id'),
                ('solver_path', 'solver_path'),
                ('phenomenon_id', 'phenomenon_id'),
                ('curvature_class', 'curvature_class'),
            ),
        )
        self.preregistrations = _SealedStore(
            self._connect,
            'cad_benchmark_preregistrations',
            BenchmarkPreregistration,
            'preregistration_id', 'preregistration_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('mapping_ref_id', 'mapping_ref'),
                ('benchmark_sha256', 'benchmark_sha256'),
                ('provider_id', 'provider_id'),
                ('run_mode', 'run_mode'),
            ),
        )
        self.qualifications = _SealedStore(
            self._connect,
            'cad_benchmark_qualifications',
            BenchmarkQualification,
            'qualification_id', 'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('mapping_ref_id', 'mapping_ref'),
                _ref('preregistration_ref_id', 'preregistration_ref'),
                ('verdict', 'verdict'),
                ('level_attained', 'level_attained'),
                ('run_mode', 'run_mode'),
                ('predictive', 'predictive'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def get_mapping(self, mapping_id: str) -> BenchmarkSceneMapping | None:
        return self.scene_mappings.get(mapping_id)

    def get_preregistration(
            self, preregistration_id: str) -> BenchmarkPreregistration | None:
        return self.preregistrations.get(preregistration_id)

    def get_qualification(
            self, qualification_id: str) -> BenchmarkQualification | None:
        return self.qualifications.get(qualification_id)
