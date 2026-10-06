"""Append-only persistence for REV59-ACOUST2 authorities.

Six tables in one repository — fixture/observer scattering (#743),
spectral estimator (#749), evidence supersession (#765):

* ``cad_measurement_fixture_profiles`` /
  ``cad_fixture_scattering_observations``
* ``cad_spectral_estimator_profiles`` / ``cad_spectral_resolution_claims``
* ``cad_external_evidence_sources`` / ``cad_evidence_supersession_records``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_observer_scattering import (
    FixtureScatteringObservation,
    MeasurementFixtureProfile,
)
from .cad_spectral_estimator import (
    SpectralEstimatorProfile,
    SpectralResolutionClaim,
)
from .cad_evidence_supersession import (
    EvidenceSupersessionRecord,
    ExternalEvidenceSource,
)


class MeasurementSetupConflictError(ValueError):
    """A measurement-setup save violated append-only identity rules."""


class MeasurementSetupIntegrityError(ValueError):
    """A stored measurement-setup row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise MeasurementSetupIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MeasurementSetupIntegrityError(
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
            raise MeasurementSetupConflictError(
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
            raise MeasurementSetupIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise MeasurementSetupIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise MeasurementSetupIntegrityError(
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
                raise MeasurementSetupIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadMeasurementSetupRepository:
    """Native storage for the #743/#749/#765 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_measurement_fixture_profiles',
                'cad_fixture_scattering_observations',
                'cad_spectral_estimator_profiles',
                'cad_spectral_resolution_claims',
                'cad_external_evidence_sources',
                'cad_evidence_supersession_records',
            )
        self.fixture_profiles = _SealedStore(
            self._connect, 'cad_measurement_fixture_profiles',
            MeasurementFixtureProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
            ),
        )
        self.scattering_observations = _SealedStore(
            self._connect, 'cad_fixture_scattering_observations',
            FixtureScatteringObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('fixture_ref_id', 'fixture_ref'),
                ('contamination_detected', 'contamination_detected'),
            ),
        )
        self.estimator_profiles = _SealedStore(
            self._connect, 'cad_spectral_estimator_profiles',
            SpectralEstimatorProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('window_kind', 'window_kind'),
            ),
        )
        self.resolution_claims = _SealedStore(
            self._connect, 'cad_spectral_resolution_claims',
            SpectralResolutionClaim, 'claim_id', 'claim_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('estimator_ref_id', 'estimator_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.evidence_sources = _SealedStore(
            self._connect, 'cad_external_evidence_sources',
            ExternalEvidenceSource, 'source_id', 'source_sha256',
            (
                ('document_id', '__document_id__'),
                ('source_tier', 'source_tier'),
                ('scope', 'scope'),
            ),
        )
        self.supersession_records = _SealedStore(
            self._connect, 'cad_evidence_supersession_records',
            EvidenceSupersessionRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                ('resolution', 'resolution'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_fixture_profile(
        self, record: MeasurementFixtureProfile
    ) -> None:
        self.fixture_profiles.save(record)

    def get_fixture_profile(
        self, rid: str
    ) -> MeasurementFixtureProfile | None:
        return self.fixture_profiles.get(rid)

    def save_scattering_observation(
        self, record: FixtureScatteringObservation
    ) -> None:
        self.scattering_observations.save(record)

    def get_scattering_observation(
        self, rid: str
    ) -> FixtureScatteringObservation | None:
        return self.scattering_observations.get(rid)

    def save_estimator_profile(
        self, record: SpectralEstimatorProfile
    ) -> None:
        self.estimator_profiles.save(record)

    def get_estimator_profile(
        self, rid: str
    ) -> SpectralEstimatorProfile | None:
        return self.estimator_profiles.get(rid)

    def save_resolution_claim(
        self, record: SpectralResolutionClaim
    ) -> None:
        self.resolution_claims.save(record)

    def get_resolution_claim(
        self, rid: str
    ) -> SpectralResolutionClaim | None:
        return self.resolution_claims.get(rid)

    def save_evidence_source(
        self, record: ExternalEvidenceSource
    ) -> None:
        self.evidence_sources.save(record)

    def get_evidence_source(
        self, rid: str
    ) -> ExternalEvidenceSource | None:
        return self.evidence_sources.get(rid)

    def save_supersession_record(
        self, record: EvidenceSupersessionRecord
    ) -> None:
        self.supersession_records.save(record)

    def get_supersession_record(
        self, rid: str
    ) -> EvidenceSupersessionRecord | None:
        return self.supersession_records.get(rid)
