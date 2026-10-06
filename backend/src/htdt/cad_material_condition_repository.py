"""Append-only persistence for REV60-COLLABENV material-condition
authority (issue #776):

* ``cad_material_condition_states`` — specimen + installed state
* ``cad_material_durability_evidence`` — typed durability evidence
* ``cad_material_evidence_applicability`` — applicability verdicts
* ``cad_material_reinspections`` — drift-triggered reinspection verdicts
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_material_condition import (
    AcousticMaterialConditionState,
    MaterialDurabilityEvidence,
    MaterialEvidenceApplicability,
    ReinspectionAssessment,
)


class MaterialConditionConflictError(ValueError):
    """A material-condition save violated append-only identity rules."""


class MaterialConditionIntegrityError(ValueError):
    """A stored material-condition row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(
        record.identity_payload()  # type: ignore[attr-defined]
    )
    if getattr(record, sha_field) != sha:
        raise MaterialConditionIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MaterialConditionIntegrityError(
            'record id does not match its sealed sha256'
        )


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
                record, self.sha_field
            ):
                return
            raise MaterialConditionConflictError(
                f'{self.table} records are append-only'
            )
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
            raise MaterialConditionIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise MaterialConditionIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise MaterialConditionIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
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
                raise MaterialConditionIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadMaterialConditionRepository:
    """Native storage for the #776 environmental/aging authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_material_condition_states',
                'cad_material_durability_evidence',
                'cad_material_evidence_applicability',
                'cad_material_reinspections',
            )
        self.condition_states = _SealedStore(
            self._connect, 'cad_material_condition_states',
            AcousticMaterialConditionState,
            'condition_id', 'condition_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('material_ref_id', 'material_ref'),
                ('context', 'context'),
                ('condition_state', 'condition_state'),
            ),
        )
        self.durability_evidence = _SealedStore(
            self._connect, 'cad_material_durability_evidence',
            MaterialDurabilityEvidence,
            'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('material_family_ref_id', 'material_family_ref'),
                ('evidence_class', 'evidence_class'),
                ('change_direction', 'change_direction'),
            ),
        )
        self.applicability = _SealedStore(
            self._connect, 'cad_material_evidence_applicability',
            MaterialEvidenceApplicability,
            'applicability_id', 'applicability_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('material_ref_id', 'material_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.reinspections = _SealedStore(
            self._connect, 'cad_material_reinspections',
            ReinspectionAssessment,
            'assessment_id', 'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('installed_state_ref_id', 'installed_state_ref'),
                ('trigger', 'trigger'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_condition_state(
        self, record: AcousticMaterialConditionState
    ) -> None:
        self.condition_states.save(record)

    def get_condition_state(
        self, condition_id: str
    ) -> AcousticMaterialConditionState | None:
        return self.condition_states.get(condition_id)

    def save_durability_evidence(
        self, record: MaterialDurabilityEvidence
    ) -> None:
        self.durability_evidence.save(record)

    def get_durability_evidence(
        self, evidence_id: str
    ) -> MaterialDurabilityEvidence | None:
        return self.durability_evidence.get(evidence_id)

    def save_applicability(
        self, record: MaterialEvidenceApplicability
    ) -> None:
        self.applicability.save(record)

    def get_applicability(
        self, applicability_id: str
    ) -> MaterialEvidenceApplicability | None:
        return self.applicability.get(applicability_id)

    def save_reinspection(
        self, record: ReinspectionAssessment
    ) -> None:
        self.reinspections.save(record)

    def get_reinspection(
        self, assessment_id: str
    ) -> ReinspectionAssessment | None:
        return self.reinspections.get(assessment_id)
