"""Append-only persistence for the #811 solver-confidence-bound authority.

Six tables in one repository — per-dimension input authorities
(material, source directivity, geometry, pose), the solver input
envelope that binds them, and the per-claim bound record produced by
the evaluator:

* ``cad_material_input_authorities``
* ``cad_source_directivity_authorities``
* ``cad_geometry_input_authorities``
* ``cad_pose_input_authorities``
* ``cad_solver_input_envelopes``
* ``cad_claim_bound_records``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_solver_confidence_bound import (
    ClaimBoundRecord,
    GeometryInputAuthority,
    MaterialInputAuthority,
    PoseInputAuthority,
    SolverInputEnvelope,
    SourceDirectivityAuthority,
)


class ConfidenceBoundConflictError(ValueError):
    """A confidence-bound save violated append-only identity rules."""


class ConfidenceBoundIntegrityError(ValueError):
    """A stored confidence-bound row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ConfidenceBoundIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ConfidenceBoundIntegrityError(
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
            raise ConfidenceBoundConflictError(
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
            raise ConfidenceBoundIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ConfidenceBoundIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ConfidenceBoundIntegrityError(
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
                raise ConfidenceBoundIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadSolverConfidenceBoundRepository:
    """Native storage for the #811 solver-confidence-bound authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_material_input_authorities',
                'cad_source_directivity_authorities',
                'cad_geometry_input_authorities',
                'cad_pose_input_authorities',
                'cad_solver_input_envelopes',
                'cad_claim_bound_records',
            )
        self.material_authorities = _SealedStore(
            self._connect, 'cad_material_input_authorities',
            MaterialInputAuthority, 'material_id', 'material_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('authority_class', 'authority_class'),
                ('quantity', 'quantity'),
                ('method_class', 'method_class'),
            ),
        )
        self.directivity_authorities = _SealedStore(
            self._connect, 'cad_source_directivity_authorities',
            SourceDirectivityAuthority,
            'directivity_id', 'directivity_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('provenance_class', 'provenance_class'),
                ('format_compliance', 'format_compliance'),
                ('coverage', 'coverage'),
            ),
        )
        self.geometry_authorities = _SealedStore(
            self._connect, 'cad_geometry_input_authorities',
            GeometryInputAuthority, 'geometry_id', 'geometry_sha256',
            (
                ('document_id', '__document_id__'),
                ('fidelity_class', 'fidelity_class'),
                _ref('scene_revision_ref_id', 'scene_revision_ref'),
            ),
        )
        self.pose_authorities = _SealedStore(
            self._connect, 'cad_pose_input_authorities',
            PoseInputAuthority, 'pose_id', 'pose_sha256',
            (
                ('document_id', '__document_id__'),
                ('subject_kind', 'subject_kind'),
                _ref('subject_ref_id', 'subject_ref'),
                ('authority_class', 'authority_class'),
                ('modal_sensitivity', 'modal_sensitivity'),
            ),
        )
        self.input_envelopes = _SealedStore(
            self._connect, 'cad_solver_input_envelopes',
            SolverInputEnvelope, 'envelope_id', 'envelope_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('solver_request_ref_id', 'solver_request_ref'),
            ),
        )
        self.claim_bound_records = _SealedStore(
            self._connect, 'cad_claim_bound_records',
            ClaimBoundRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('input_envelope_ref_id', 'input_envelope_ref'),
                ('evaluator_version', 'evaluator_version'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # material input authorities
    def save_material_authority(self, record: MaterialInputAuthority) -> None:
        self.material_authorities.save(record)

    def get_material_authority(
        self, rid: str
    ) -> MaterialInputAuthority | None:
        return self.material_authorities.get(rid)

    def list_material_authorities(
        self, document_id: str | None = None
    ) -> tuple[MaterialInputAuthority, ...]:
        return self.material_authorities.list(document_id)

    # source directivity authorities
    def save_directivity_authority(
        self, record: SourceDirectivityAuthority
    ) -> None:
        self.directivity_authorities.save(record)

    def get_directivity_authority(
        self, rid: str
    ) -> SourceDirectivityAuthority | None:
        return self.directivity_authorities.get(rid)

    def list_directivity_authorities(
        self, document_id: str | None = None
    ) -> tuple[SourceDirectivityAuthority, ...]:
        return self.directivity_authorities.list(document_id)

    # geometry input authorities
    def save_geometry_authority(self, record: GeometryInputAuthority) -> None:
        self.geometry_authorities.save(record)

    def get_geometry_authority(
        self, rid: str
    ) -> GeometryInputAuthority | None:
        return self.geometry_authorities.get(rid)

    def list_geometry_authorities(
        self, document_id: str | None = None
    ) -> tuple[GeometryInputAuthority, ...]:
        return self.geometry_authorities.list(document_id)

    # pose input authorities
    def save_pose_authority(self, record: PoseInputAuthority) -> None:
        self.pose_authorities.save(record)

    def get_pose_authority(self, rid: str) -> PoseInputAuthority | None:
        return self.pose_authorities.get(rid)

    def list_pose_authorities(
        self, document_id: str | None = None
    ) -> tuple[PoseInputAuthority, ...]:
        return self.pose_authorities.list(document_id)

    # solver input envelopes
    def save_input_envelope(self, record: SolverInputEnvelope) -> None:
        self.input_envelopes.save(record)

    def get_input_envelope(self, rid: str) -> SolverInputEnvelope | None:
        return self.input_envelopes.get(rid)

    def list_input_envelopes(
        self, document_id: str | None = None
    ) -> tuple[SolverInputEnvelope, ...]:
        return self.input_envelopes.list(document_id)

    # claim bound records
    def save_claim_bound(self, record: ClaimBoundRecord) -> None:
        self.claim_bound_records.save(record)

    def get_claim_bound(self, rid: str) -> ClaimBoundRecord | None:
        return self.claim_bound_records.get(rid)

    def list_claim_bounds(
        self, document_id: str | None = None
    ) -> tuple[ClaimBoundRecord, ...]:
        return self.claim_bound_records.list(document_id)
