"""Append-only persistence for REV59-ACOUST3 authorities.

Six tables in one repository — finite absorber geometry (#694),
echo/perceptual risk (#646), fire/finish safety (#648):

* ``cad_finite_absorber_geometries``
  / ``cad_finite_treatment_boundary_models``
* ``cad_precedence_profiles`` / ``cad_echo_risk_observations``
* ``cad_reaction_to_fire_evidence``
  / ``cad_finish_assembly_evidence``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_finite_absorber import (
    FiniteAbsorberGeometry,
    FiniteTreatmentBoundaryModel,
)
from .cad_precedence_echo import (
    EchoRiskObservation,
    PrecedenceProfile,
)
from .cad_fire_evidence import (
    FinishAssemblySafetyEvidence,
    ReactionToFireEvidence,
)


class TreatmentSafetyConflictError(ValueError):
    """An ACOUST3 save violated append-only identity rules."""


class TreatmentSafetyIntegrityError(ValueError):
    """A stored ACOUST3 row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TreatmentSafetyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TreatmentSafetyIntegrityError(
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
            raise TreatmentSafetyConflictError(
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
            raise TreatmentSafetyIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise TreatmentSafetyIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise TreatmentSafetyIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadTreatmentSafetyRepository:
    """Native storage for the #694/#646/#648 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_finite_absorber_geometries',
                'cad_finite_treatment_boundary_models',
                'cad_precedence_profiles',
                'cad_echo_risk_observations',
                'cad_reaction_to_fire_evidence',
                'cad_finish_assembly_evidence',
            )
        self.finite_geometries = _SealedStore(
            self._connect, 'cad_finite_absorber_geometries',
            FiniteAbsorberGeometry, 'geometry_id', 'geometry_sha256',
            (
                ('document_id', '__document_id__'),
                ('edge_state', 'edge_state'),
                ('mounting_kind', 'mounting_kind'),
            ),
        )
        self.boundary_models = _SealedStore(
            self._connect, 'cad_finite_treatment_boundary_models',
            FiniteTreatmentBoundaryModel, 'model_id', 'model_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('geometry_ref_id', 'geometry_ref'),
                ('reaction_kind', 'reaction_kind'),
            ),
        )
        self.precedence_profiles = _SealedStore(
            self._connect, 'cad_precedence_profiles',
            PrecedenceProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('stimulus_kind', 'stimulus_kind'),
            ),
        )
        self.echo_observations = _SealedStore(
            self._connect, 'cad_echo_risk_observations',
            EchoRiskObservation, 'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('risk_verdict', 'risk_verdict'),
            ),
        )
        self.fire_evidence = _SealedStore(
            self._connect, 'cad_reaction_to_fire_evidence',
            ReactionToFireEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('test_standard', 'test_standard'),
            ),
        )
        self.assembly_evidence = _SealedStore(
            self._connect, 'cad_finish_assembly_evidence',
            FinishAssemblySafetyEvidence, 'assembly_id',
            'assembly_sha256',
            (
                ('document_id', '__document_id__'),
                ('installation_context', 'installation_context'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_finite_geometry(
        self, record: FiniteAbsorberGeometry
    ) -> None:
        self.finite_geometries.save(record)

    def get_finite_geometry(
        self, rid: str
    ) -> FiniteAbsorberGeometry | None:
        return self.finite_geometries.get(rid)

    def save_boundary_model(
        self, record: FiniteTreatmentBoundaryModel
    ) -> None:
        self.boundary_models.save(record)

    def get_boundary_model(
        self, rid: str
    ) -> FiniteTreatmentBoundaryModel | None:
        return self.boundary_models.get(rid)

    def save_precedence_profile(
        self, record: PrecedenceProfile
    ) -> None:
        self.precedence_profiles.save(record)

    def get_precedence_profile(
        self, rid: str
    ) -> PrecedenceProfile | None:
        return self.precedence_profiles.get(rid)

    def save_echo_observation(
        self, record: EchoRiskObservation
    ) -> None:
        self.echo_observations.save(record)

    def get_echo_observation(
        self, rid: str
    ) -> EchoRiskObservation | None:
        return self.echo_observations.get(rid)

    def save_fire_evidence(
        self, record: ReactionToFireEvidence
    ) -> None:
        self.fire_evidence.save(record)

    def get_fire_evidence(
        self, rid: str
    ) -> ReactionToFireEvidence | None:
        return self.fire_evidence.get(rid)

    def save_assembly_evidence(
        self, record: FinishAssemblySafetyEvidence
    ) -> None:
        self.assembly_evidence.save(record)

    def get_assembly_evidence(
        self, rid: str
    ) -> FinishAssemblySafetyEvidence | None:
        return self.assembly_evidence.get(rid)
