"""Append-only persistence for REV59-QUALNUM authorities.

Nine tables in one repository — numerical reproducibility (#703),
imaging measurement chain (#716), wireless AV transport (#717):

* ``cad_numerical_repro_profiles`` / ``cad_stochastic_realizations``
  / ``cad_numerical_comparisons``
* ``cad_imaging_measurement_chains`` / ``cad_camera_calibrations``
  / ``cad_camera_derived_observations``
* ``cad_wireless_av_links`` / ``cad_wireless_transport_observations``
  / ``cad_wireless_sync_evidence``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_solver_reproducibility import (
    CrossPlatformNumericalComparison,
    NumericalReproducibilityProfile,
    StochasticRealizationRecord,
)
from .cad_imaging_chain import (
    CameraCalibrationProfile,
    CameraDerivedObservation,
    ImagingMeasurementChain,
)
from .cad_wireless_av import (
    WirelessAVLink,
    WirelessSynchronizationEvidence,
    WirelessTransportObservation,
)


class NumericalTransportConflictError(ValueError):
    """A QUALNUM save violated append-only identity rules."""


class NumericalTransportIntegrityError(ValueError):
    """A stored QUALNUM row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise NumericalTransportIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise NumericalTransportIntegrityError(
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
            raise NumericalTransportConflictError(
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
            raise NumericalTransportIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise NumericalTransportIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise NumericalTransportIntegrityError(
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


class CadNumericalTransportRepository:
    """Native storage for the #703/#716/#717 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_numerical_repro_profiles',
                'cad_stochastic_realizations',
                'cad_numerical_comparisons',
                'cad_imaging_measurement_chains',
                'cad_camera_calibrations',
                'cad_camera_derived_observations',
                'cad_wireless_av_links',
                'cad_wireless_transport_observations',
                'cad_wireless_sync_evidence',
            )
        self.repro_profiles = _SealedStore(
            self._connect, 'cad_numerical_repro_profiles',
            NumericalReproducibilityProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('precision_kind', 'precision_kind'),
                ('parallelism_kind', 'parallelism_kind'),
            ),
        )
        self.realizations = _SealedStore(
            self._connect, 'cad_stochastic_realizations',
            StochasticRealizationRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
                ('realization_kind', 'realization_kind'),
            ),
        )
        self.comparisons = _SealedStore(
            self._connect, 'cad_numerical_comparisons',
            CrossPlatformNumericalComparison, 'comparison_id',
            'comparison_sha256',
            (
                ('document_id', '__document_id__'),
                ('domain', 'domain'),
            ),
        )
        self.imaging_chains = _SealedStore(
            self._connect, 'cad_imaging_measurement_chains',
            ImagingMeasurementChain, 'chain_id', 'chain_sha256',
            (
                ('document_id', '__document_id__'),
                ('chain_state', 'chain_state'),
                ('shutter_kind', 'shutter_kind'),
            ),
        )
        self.camera_calibrations = _SealedStore(
            self._connect, 'cad_camera_calibrations',
            CameraCalibrationProfile, 'calibration_id',
            'calibration_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('chain_ref_id', 'chain_ref'),
            ),
        )
        self.camera_observations = _SealedStore(
            self._connect, 'cad_camera_derived_observations',
            CameraDerivedObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('chain_ref_id', 'chain_ref'),
                ('measurand', 'measurand'),
                ('processing_state', 'processing_state'),
            ),
        )
        self.wireless_links = _SealedStore(
            self._connect, 'cad_wireless_av_links',
            WirelessAVLink, 'link_id', 'link_sha256',
            (
                ('document_id', '__document_id__'),
                ('transport_kind', 'transport_kind'),
            ),
        )
        self.wireless_observations = _SealedStore(
            self._connect, 'cad_wireless_transport_observations',
            WirelessTransportObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('link_ref_id', 'link_ref'),
                ('dropout_events', 'dropout_events'),
            ),
        )
        self.wireless_sync = _SealedStore(
            self._connect, 'cad_wireless_sync_evidence',
            WirelessSynchronizationEvidence, 'evidence_id',
            'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('link_ref_id', 'link_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_repro_profile(
        self, record: NumericalReproducibilityProfile
    ) -> None:
        self.repro_profiles.save(record)

    def get_repro_profile(
        self, rid: str
    ) -> NumericalReproducibilityProfile | None:
        return self.repro_profiles.get(rid)

    def save_realization(
        self, record: StochasticRealizationRecord
    ) -> None:
        self.realizations.save(record)

    def get_realization(
        self, rid: str
    ) -> StochasticRealizationRecord | None:
        return self.realizations.get(rid)

    def save_comparison(
        self, record: CrossPlatformNumericalComparison
    ) -> None:
        self.comparisons.save(record)

    def get_comparison(
        self, rid: str
    ) -> CrossPlatformNumericalComparison | None:
        return self.comparisons.get(rid)

    def save_imaging_chain(
        self, record: ImagingMeasurementChain
    ) -> None:
        self.imaging_chains.save(record)

    def get_imaging_chain(
        self, rid: str
    ) -> ImagingMeasurementChain | None:
        return self.imaging_chains.get(rid)

    def save_camera_calibration(
        self, record: CameraCalibrationProfile
    ) -> None:
        self.camera_calibrations.save(record)

    def get_camera_calibration(
        self, rid: str
    ) -> CameraCalibrationProfile | None:
        return self.camera_calibrations.get(rid)

    def save_camera_observation(
        self, record: CameraDerivedObservation
    ) -> None:
        self.camera_observations.save(record)

    def get_camera_observation(
        self, rid: str
    ) -> CameraDerivedObservation | None:
        return self.camera_observations.get(rid)

    def save_wireless_link(self, record: WirelessAVLink) -> None:
        self.wireless_links.save(record)

    def get_wireless_link(self, rid: str) -> WirelessAVLink | None:
        return self.wireless_links.get(rid)

    def save_wireless_observation(
        self, record: WirelessTransportObservation
    ) -> None:
        self.wireless_observations.save(record)

    def get_wireless_observation(
        self, rid: str
    ) -> WirelessTransportObservation | None:
        return self.wireless_observations.get(rid)

    def save_wireless_sync(
        self, record: WirelessSynchronizationEvidence
    ) -> None:
        self.wireless_sync.save(record)

    def get_wireless_sync(
        self, rid: str
    ) -> WirelessSynchronizationEvidence | None:
        return self.wireless_sync.get(rid)
