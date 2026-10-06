"""Append-only persistence for REV59-AUDIOMET-B authorities.

Fifteen tables in one repository — adaptive identification (#661),
live dual-channel TF (#663), microphone arrays (#658) and electrical
impedance / T-S measurement (#662).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_adaptive_identification import (
    AdaptiveIdentificationProfile,
    AdaptiveTransferEstimate,
    ArbitraryStimulusMeasurement,
    ResidualEvidence,
)
from .cad_live_transfer_function import (
    CoherenceObservation,
    DualChannelTFObservation,
    LiveTransferFunctionSession,
    ReferenceDelayTrack,
)
from .cad_microphone_array import (
    BeamformingTransform,
    MicrophoneArrayGeometry,
    SpatialSamplingCapability,
)
from .cad_impedance_measurement import (
    ImpedanceCalibrationState,
    ImpedanceMeasurementProfile,
    MeasuredLoadEvidence,
    ThieleSmallDerivation,
)


class FieldMetrologyConflictError(ValueError):
    """An AUDIOMET-B save violated append-only identity rules."""


class FieldMetrologyIntegrityError(ValueError):
    """A stored AUDIOMET-B row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise FieldMetrologyIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise FieldMetrologyIntegrityError(
            'record id does not match its sealed sha256'
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


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
            raise FieldMetrologyConflictError(
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
            raise FieldMetrologyIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise FieldMetrologyIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise FieldMetrologyIntegrityError(
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


class CadFieldMetrologyRepository:
    """Persist/retrieve the AUDIOMET-B sealed authorities."""

    def __init__(self, scene: SceneRepository) -> None:
        self._scene = scene
        self.path = scene.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_adaptive_identification_profiles',
                'cad_arbitrary_stimulus_measurements',
                'cad_adaptive_transfer_estimates',
                'cad_adaptive_residual_evidence',
                'cad_live_tf_sessions',
                'cad_dual_channel_tf_observations',
                'cad_coherence_observations',
                'cad_reference_delay_tracks',
                'cad_microphone_array_geometries',
                'cad_spatial_sampling_capabilities',
                'cad_beamforming_transforms',
                'cad_impedance_measurement_profiles',
                'cad_impedance_calibration_states',
                'cad_measured_load_evidence',
                'cad_thiele_small_derivations',
            )

        self.identification_profiles = _SealedStore(
            self._connect, 'cad_adaptive_identification_profiles',
            AdaptiveIdentificationProfile, 'profile_id',
            'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
            ),
        )

        self.stimulus_measurements = _SealedStore(
            self._connect, 'cad_arbitrary_stimulus_measurements',
            ArbitraryStimulusMeasurement, 'stimulus_id',
            'stimulus_sha256',
            (
                ('document_id', '__document_id__'),
                ('defect_state', 'defect_state'),
            ),
        )

        self.transfer_estimates = _SealedStore(
            self._connect, 'cad_adaptive_transfer_estimates',
            AdaptiveTransferEstimate, 'estimate_id', 'estimate_sha256',
            (
                ('document_id', '__document_id__'),
                ('convergence_state', 'convergence_state'),
            ),
        )

        self.residual_evidence = _SealedStore(
            self._connect, 'cad_adaptive_residual_evidence',
            ResidualEvidence, 'residual_id', 'residual_sha256',
            (
                ('document_id', '__document_id__'),
                ('declared_quantity', 'declared_quantity'),
            ),
        )

        self.live_sessions = _SealedStore(
            self._connect, 'cad_live_tf_sessions',
            LiveTransferFunctionSession, 'session_id', 'session_sha256',
            (
                ('document_id', '__document_id__'),
                ('reference_kind', 'reference_kind'),
            ),
        )

        self.tf_observations = _SealedStore(
            self._connect, 'cad_dual_channel_tf_observations',
            DualChannelTFObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                ('capture_state', 'capture_state'),
            ),
        )

        self.coherence_observations = _SealedStore(
            self._connect, 'cad_coherence_observations',
            CoherenceObservation, 'coherence_id', 'coherence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('observation_ref_id', 'observation_ref'),
            ),
        )

        self.delay_tracks = _SealedStore(
            self._connect, 'cad_reference_delay_tracks',
            ReferenceDelayTrack, 'track_id', 'track_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
            ),
        )

        self.array_geometries = _SealedStore(
            self._connect, 'cad_microphone_array_geometries',
            MicrophoneArrayGeometry, 'geometry_id', 'geometry_sha256',
            (
                ('document_id', '__document_id__'),
                ('topology', 'topology'),
            ),
        )

        self.sampling_capabilities = _SealedStore(
            self._connect, 'cad_spatial_sampling_capabilities',
            SpatialSamplingCapability, 'capability_id',
            'capability_sha256',
            (
                ('document_id', '__document_id__'),
                ('sync_capability', 'sync_capability'),
            ),
        )

        self.beamforming_transforms = _SealedStore(
            self._connect, 'cad_beamforming_transforms',
            BeamformingTransform, 'transform_id', 'transform_sha256',
            (
                ('document_id', '__document_id__'),
                ('output_state', 'output_state'),
            ),
        )

        self.impedance_profiles = _SealedStore(
            self._connect, 'cad_impedance_measurement_profiles',
            ImpedanceMeasurementProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('method', 'method'),
            ),
        )

        self.impedance_calibrations = _SealedStore(
            self._connect, 'cad_impedance_calibration_states',
            ImpedanceCalibrationState, 'calibration_id',
            'calibration_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('profile_ref_id', 'profile_ref'),
            ),
        )

        self.load_evidence = _SealedStore(
            self._connect, 'cad_measured_load_evidence',
            MeasuredLoadEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('evidence_class', 'evidence_class'),
            ),
        )

        self.ts_derivations = _SealedStore(
            self._connect, 'cad_thiele_small_derivations',
            ThieleSmallDerivation, 'derivation_id', 'derivation_sha256',
            (
                ('document_id', '__document_id__'),
                ('model_fit_state', 'model_fit_state'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_identification_profile(
        self, record: AdaptiveIdentificationProfile
    ) -> None:
        self.identification_profiles.save(record)

    def get_identification_profile(
        self, rid: str
    ) -> AdaptiveIdentificationProfile | None:
        return self.identification_profiles.get(rid)

    def save_stimulus_measurement(
        self, record: ArbitraryStimulusMeasurement
    ) -> None:
        self.stimulus_measurements.save(record)

    def get_stimulus_measurement(
        self, rid: str
    ) -> ArbitraryStimulusMeasurement | None:
        return self.stimulus_measurements.get(rid)

    def save_transfer_estimate(
        self, record: AdaptiveTransferEstimate
    ) -> None:
        self.transfer_estimates.save(record)

    def get_transfer_estimate(
        self, rid: str
    ) -> AdaptiveTransferEstimate | None:
        return self.transfer_estimates.get(rid)

    def save_residual_evidence(self, record: ResidualEvidence) -> None:
        self.residual_evidence.save(record)

    def get_residual_evidence(self, rid: str) -> ResidualEvidence | None:
        return self.residual_evidence.get(rid)

    def save_live_session(
        self, record: LiveTransferFunctionSession
    ) -> None:
        self.live_sessions.save(record)

    def get_live_session(
        self, rid: str
    ) -> LiveTransferFunctionSession | None:
        return self.live_sessions.get(rid)

    def save_tf_observation(
        self, record: DualChannelTFObservation
    ) -> None:
        self.tf_observations.save(record)

    def get_tf_observation(
        self, rid: str
    ) -> DualChannelTFObservation | None:
        return self.tf_observations.get(rid)

    def save_coherence_observation(
        self, record: CoherenceObservation
    ) -> None:
        self.coherence_observations.save(record)

    def get_coherence_observation(
        self, rid: str
    ) -> CoherenceObservation | None:
        return self.coherence_observations.get(rid)

    def save_delay_track(self, record: ReferenceDelayTrack) -> None:
        self.delay_tracks.save(record)

    def get_delay_track(self, rid: str) -> ReferenceDelayTrack | None:
        return self.delay_tracks.get(rid)

    def save_array_geometry(
        self, record: MicrophoneArrayGeometry
    ) -> None:
        self.array_geometries.save(record)

    def get_array_geometry(
        self, rid: str
    ) -> MicrophoneArrayGeometry | None:
        return self.array_geometries.get(rid)

    def save_sampling_capability(
        self, record: SpatialSamplingCapability
    ) -> None:
        self.sampling_capabilities.save(record)

    def get_sampling_capability(
        self, rid: str
    ) -> SpatialSamplingCapability | None:
        return self.sampling_capabilities.get(rid)

    def save_beamforming_transform(
        self, record: BeamformingTransform
    ) -> None:
        self.beamforming_transforms.save(record)

    def get_beamforming_transform(
        self, rid: str
    ) -> BeamformingTransform | None:
        return self.beamforming_transforms.get(rid)

    def save_impedance_profile(
        self, record: ImpedanceMeasurementProfile
    ) -> None:
        self.impedance_profiles.save(record)

    def get_impedance_profile(
        self, rid: str
    ) -> ImpedanceMeasurementProfile | None:
        return self.impedance_profiles.get(rid)

    def save_impedance_calibration(
        self, record: ImpedanceCalibrationState
    ) -> None:
        self.impedance_calibrations.save(record)

    def get_impedance_calibration(
        self, rid: str
    ) -> ImpedanceCalibrationState | None:
        return self.impedance_calibrations.get(rid)

    def save_load_evidence(self, record: MeasuredLoadEvidence) -> None:
        self.load_evidence.save(record)

    def get_load_evidence(self, rid: str) -> MeasuredLoadEvidence | None:
        return self.load_evidence.get(rid)

    def save_ts_derivation(self, record: ThieleSmallDerivation) -> None:
        self.ts_derivations.save(record)

    def get_ts_derivation(
        self, rid: str
    ) -> ThieleSmallDerivation | None:
        return self.ts_derivations.get(rid)
