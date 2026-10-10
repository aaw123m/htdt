"""Append-only persistence for the #812 hybrid-composition validation
authority.

Three tables in one repository — the pinned validation spec
(``cad_hybrid_composition_validation_specs``), the sealed evidence rows
(``cad_hybrid_validation_evidence``) and the sealed verdicts
(``cad_hybrid_validation_verdicts``).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from ..domain.cad_hybrid_composition_validation import (
    HybridCompositionValidationIntegrityError,
    HybridCompositionValidationSpec,
    HybridValidationEvidence,
    HybridValidationVerdict,
)
from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256


class HybridCompositionValidationConflictError(ValueError):
    """A hybrid-validation save violated append-only identity rules."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise HybridCompositionValidationIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise HybridCompositionValidationIntegrityError(
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
            raise HybridCompositionValidationConflictError(
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
            raise HybridCompositionValidationIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise HybridCompositionValidationIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise HybridCompositionValidationIntegrityError(
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
                raise HybridCompositionValidationIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadHybridCompositionValidationRepository:
    """Native storage for the #812 hybrid-composition validation
    authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_hybrid_composition_validation_specs',
                'cad_hybrid_validation_evidence',
                'cad_hybrid_validation_verdicts',
            )
        self.specs = _SealedStore(
            self._connect,
            'cad_hybrid_composition_validation_specs',
            HybridCompositionValidationSpec, 'spec_id', 'spec_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('composition_semantics', 'composition_semantics'),
                ('late_field_composed', 'late_field_composed'),
                ('grid_reconciliation_required',
                 'grid_reconciliation_required'),
            ),
        )
        self.evidence = _SealedStore(
            self._connect,
            'cad_hybrid_validation_evidence',
            HybridValidationEvidence,
            'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('spec_ref_id', 'spec_ref'),
                ('evidence_kind', 'evidence_kind'),
                ('outcome', 'outcome'),
                ('domain_kind', 'domain_kind'),
                ('solver_path', 'solver_path'),
                ('reference_class', 'reference_class'),
            ),
        )
        self.verdicts = _SealedStore(
            self._connect,
            'cad_hybrid_validation_verdicts',
            HybridValidationVerdict,
            'verdict_id', 'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('spec_ref_id', 'spec_ref'),
                _ref('subject_ref_id', 'subject_ref'),
                ('verdict', 'verdict'),
                ('qualification_level', 'qualification_level'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def get_spec(
            self, spec_id: str,
    ) -> HybridCompositionValidationSpec | None:
        return self.specs.get(spec_id)

    def get_evidence(
            self, evidence_id: str,
    ) -> HybridValidationEvidence | None:
        return self.evidence.get(evidence_id)

    def get_verdict(
            self, verdict_id: str,
    ) -> HybridValidationVerdict | None:
        return self.verdicts.get(verdict_id)

    def list_specs(
        self, document_id: str | None = None,
    ) -> tuple[HybridCompositionValidationSpec, ...]:
        return self.specs.list(document_id)

    def list_evidence(
        self, document_id: str | None = None,
    ) -> tuple[HybridValidationEvidence, ...]:
        return self.evidence.list(document_id)

    def list_verdicts(
        self, document_id: str | None = None,
    ) -> tuple[HybridValidationVerdict, ...]:
        return self.verdicts.list(document_id)
