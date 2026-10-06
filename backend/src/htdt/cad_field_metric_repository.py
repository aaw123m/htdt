"""Append-only persistence for REV59-BENCH2 authorities.

Ten tables in one repository — band semantics (#763), mixing time
(#764), field interpolation (#755) and solver compute budget (#770):

* ``cad_fractional_octave_profiles`` / ``cad_band_integrations``
* ``cad_echo_density_profiles`` / ``cad_mixing_time_estimates`` /
  ``cad_late_field_assessments``
* ``cad_interpolation_profiles`` / ``cad_field_surface_records``
* ``cad_solver_budget_profiles`` / ``cad_compute_observations`` /
  ``cad_accuracy_cost_envelopes``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_fractional_octave import (
    BandIntegrationRecord,
    FractionalOctaveProfile,
)
from .cad_mixing_time import (
    EchoDensityProfile,
    LateFieldTransitionAssessment,
    MixingTimeEstimate,
)
from .cad_field_interpolation import (
    FieldSurfaceRecord,
    InterpolationProfile,
)
from .cad_compute_budget import (
    AccuracyCostEnvelope,
    ComputeObservation,
    SolverBudgetProfile,
)


class FieldMetricConflictError(ValueError):
    """A field-metric save violated append-only identity rules."""


class FieldMetricIntegrityError(ValueError):
    """A stored field-metric row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise FieldMetricIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise FieldMetricIntegrityError(
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
            raise FieldMetricConflictError(
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
            raise FieldMetricIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise FieldMetricIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise FieldMetricIntegrityError(
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


class CadFieldMetricRepository:
    """Native storage for the #763/#764/#755/#770 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_fractional_octave_profiles',
                'cad_band_integrations',
                'cad_echo_density_profiles',
                'cad_mixing_time_estimates',
                'cad_late_field_assessments',
                'cad_interpolation_profiles',
                'cad_field_surface_records',
                'cad_solver_budget_profiles',
                'cad_compute_observations',
                'cad_accuracy_cost_envelopes',
            )
        self.band_profiles = _SealedStore(
            self._connect, 'cad_fractional_octave_profiles',
            FractionalOctaveProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('band_kind', 'band_kind'),
                ('frequency_standard', 'frequency_standard'),
                ('filter_class', 'filter_class'),
            ),
        )
        self.band_integrations = _SealedStore(
            self._connect, 'cad_band_integrations',
            BandIntegrationRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.echo_profiles = _SealedStore(
            self._connect, 'cad_echo_density_profiles',
            EchoDensityProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('estimator_kind', 'estimator_kind'),
            ),
        )
        self.mixing_estimates = _SealedStore(
            self._connect, 'cad_mixing_time_estimates',
            MixingTimeEstimate, 'estimate_id', 'estimate_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('basis', 'basis'),
            ),
        )
        self.late_assessments = _SealedStore(
            self._connect, 'cad_late_field_assessments',
            LateFieldTransitionAssessment, 'assessment_id',
            'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                ('verdict', 'verdict'),
            ),
        )
        self.interp_profiles = _SealedStore(
            self._connect, 'cad_interpolation_profiles',
            InterpolationProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
                ('quantity', 'quantity'),
            ),
        )
        self.field_surfaces = _SealedStore(
            self._connect, 'cad_field_surface_records',
            FieldSurfaceRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.budget_profiles = _SealedStore(
            self._connect, 'cad_solver_budget_profiles',
            SolverBudgetProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.compute_observations = _SealedStore(
            self._connect, 'cad_compute_observations',
            ComputeObservation, 'observation_id', 'observation_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.cost_envelopes = _SealedStore(
            self._connect, 'cad_accuracy_cost_envelopes',
            AccuracyCostEnvelope, 'envelope_id', 'envelope_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_band_profile(self, record: FractionalOctaveProfile) -> None:
        self.band_profiles.save(record)

    def get_band_profile(
        self, profile_id: str
    ) -> FractionalOctaveProfile | None:
        return self.band_profiles.get(profile_id)

    def save_band_integration(self, record: BandIntegrationRecord) -> None:
        self.band_integrations.save(record)

    def get_band_integration(
        self, record_id: str
    ) -> BandIntegrationRecord | None:
        return self.band_integrations.get(record_id)

    def save_echo_profile(self, record: EchoDensityProfile) -> None:
        self.echo_profiles.save(record)

    def get_echo_profile(
        self, profile_id: str
    ) -> EchoDensityProfile | None:
        return self.echo_profiles.get(profile_id)

    def save_mixing_estimate(self, record: MixingTimeEstimate) -> None:
        self.mixing_estimates.save(record)

    def get_mixing_estimate(
        self, estimate_id: str
    ) -> MixingTimeEstimate | None:
        return self.mixing_estimates.get(estimate_id)

    def save_late_assessment(
        self, record: LateFieldTransitionAssessment
    ) -> None:
        self.late_assessments.save(record)

    def get_late_assessment(
        self, assessment_id: str
    ) -> LateFieldTransitionAssessment | None:
        return self.late_assessments.get(assessment_id)

    def save_interp_profile(self, record: InterpolationProfile) -> None:
        self.interp_profiles.save(record)

    def get_interp_profile(
        self, profile_id: str
    ) -> InterpolationProfile | None:
        return self.interp_profiles.get(profile_id)

    def save_field_surface(self, record: FieldSurfaceRecord) -> None:
        self.field_surfaces.save(record)

    def get_field_surface(
        self, record_id: str
    ) -> FieldSurfaceRecord | None:
        return self.field_surfaces.get(record_id)

    def save_budget_profile(self, record: SolverBudgetProfile) -> None:
        self.budget_profiles.save(record)

    def get_budget_profile(
        self, profile_id: str
    ) -> SolverBudgetProfile | None:
        return self.budget_profiles.get(profile_id)

    def save_compute_observation(
        self, record: ComputeObservation
    ) -> None:
        self.compute_observations.save(record)

    def get_compute_observation(
        self, observation_id: str
    ) -> ComputeObservation | None:
        return self.compute_observations.get(observation_id)

    def save_cost_envelope(self, record: AccuracyCostEnvelope) -> None:
        self.cost_envelopes.save(record)

    def get_cost_envelope(
        self, envelope_id: str
    ) -> AccuracyCostEnvelope | None:
        return self.cost_envelopes.get(envelope_id)
