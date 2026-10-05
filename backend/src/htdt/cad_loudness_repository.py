"""Append-only persistence for the content loudness authority (#607).

Five tables:

* ``cad_content_loudness_profiles`` — sealed algorithm/profile identities.
* ``cad_programme_loudness_measurements`` — sealed per-asset loudness and
  true-peak evidence under an exact algorithm revision.
* ``cad_normalization_observations`` — observed service/metadata
  normalization state.
* ``cad_playback_gain_states`` — full playback gain-chain snapshots.
* ``cad_loudness_matching_records`` — explicit level-matching evidence
  for A/B comparisons.

Quantity separation is structural: these tables never hold a room-SPL
claim or a capability verdict — those belong to #579/#593/#602.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_loudness_authority import (
    ContentLoudnessProfile,
    LoudnessMatchingRecord,
    NormalizationObservation,
    PlaybackGainState,
    ProgrammeLoudnessMeasurement,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class LoudnessConflictError(ValueError):
    """A loudness save violated append-only identity rules."""


class LoudnessIntegrityError(ValueError):
    """A stored loudness row disagreed with its payload or references."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    """Reject records whose id/sha no longer seal their payload."""
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise LoudnessIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadLoudnessRepository:
    """Native storage for loudness profiles/measurements/gain states."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_content_loudness_profiles',
                'cad_programme_loudness_measurements',
                'cad_normalization_observations',
                'cad_playback_gain_states',
                'cad_loudness_matching_records',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: ContentLoudnessProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise LoudnessConflictError(
                'content loudness profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_content_loudness_profiles (
                    profile_id, profile_sha256, document_id,
                    standard_id, standard_edition, eligibility,
                    algorithm_version, channel_config, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.standard_id,
                    profile.standard_edition,
                    profile.eligibility,
                    profile.algorithm_version,
                    profile.channel_config,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> ContentLoudnessProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       standard_id, standard_edition, eligibility,
                       algorithm_version, channel_config, created_at_utc,
                       payload_json
                FROM cad_content_loudness_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self, document_id: str
    ) -> tuple[ContentLoudnessProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       standard_id, standard_edition, eligibility,
                       algorithm_version, channel_config, created_at_utc,
                       payload_json
                FROM cad_content_loudness_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(
        self, row: sqlite3.Row
    ) -> ContentLoudnessProfile:
        profile = ContentLoudnessProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.standard_id != row['standard_id']
            or profile.standard_edition != row['standard_edition']
            or profile.eligibility != row['eligibility']
            or profile.algorithm_version != row['algorithm_version']
            or profile.channel_config != row['channel_config']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise LoudnessIntegrityError(
                'content loudness profile row disagrees with payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Measurements

    def save_measurement(
        self, measurement: ProgrammeLoudnessMeasurement
    ) -> None:
        _assert_sealed(measurement, 'measurement_sha256', 'measurement_id')
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise LoudnessConflictError(
                'programme loudness measurements are append-only — a new '
                'algorithm revision mints a new derived record'
            )
        if self.get_profile(measurement.profile_id) is None:
            raise LoudnessIntegrityError(
                'a loudness measurement must reference a persisted profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_programme_loudness_measurements (
                    measurement_id, measurement_sha256, document_id,
                    profile_id, profile_sha256, source_class,
                    channel_config, integrated_loudness_lufs,
                    true_peak_dbtp, measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.profile_id,
                    measurement.profile_sha256,
                    measurement.source_class,
                    measurement.channel_config,
                    measurement.integrated_loudness_lufs,
                    measurement.true_peak_dbtp,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> ProgrammeLoudnessMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       profile_id, profile_sha256, source_class,
                       channel_config, integrated_loudness_lufs,
                       true_peak_dbtp, measured_at_utc, payload_json
                FROM cad_programme_loudness_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def list_measurements(
        self, document_id: str
    ) -> tuple[ProgrammeLoudnessMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       profile_id, profile_sha256, source_class,
                       channel_config, integrated_loudness_lufs,
                       true_peak_dbtp, measured_at_utc, payload_json
                FROM cad_programme_loudness_measurements
                WHERE document_id=?
                ORDER BY measured_at_utc, measurement_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._measurement_from_row(row) for row in rows
        )

    def _measurement_from_row(
        self, row: sqlite3.Row
    ) -> ProgrammeLoudnessMeasurement:
        measurement = ProgrammeLoudnessMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.profile_id != row['profile_id']
            or measurement.profile_sha256 != row['profile_sha256']
            or measurement.source_class != row['source_class']
            or measurement.channel_config != row['channel_config']
            or measurement.integrated_loudness_lufs
            != row['integrated_loudness_lufs']
            or measurement.true_peak_dbtp != row['true_peak_dbtp']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise LoudnessIntegrityError(
                'programme loudness measurement row disagrees with payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Normalization observations

    def save_observation(
        self, observation: NormalizationObservation
    ) -> None:
        _assert_sealed(observation, 'observation_sha256', 'observation_id')
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise LoudnessConflictError(
                'normalization observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_normalization_observations (
                    observation_id, observation_sha256, document_id,
                    source_class, mode, target_lufs, applied_gain_db,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.source_class,
                    observation.mode,
                    observation.target_lufs,
                    observation.applied_gain_db,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> NormalizationObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT observation_id, observation_sha256, document_id,
                       source_class, mode, target_lufs, applied_gain_db,
                       observed_at_utc, payload_json
                FROM cad_normalization_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._observation_from_row(row)

    def _observation_from_row(
        self, row: sqlite3.Row
    ) -> NormalizationObservation:
        observation = NormalizationObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.source_class != row['source_class']
            or observation.mode != row['mode']
            or observation.target_lufs != row['target_lufs']
            or observation.applied_gain_db != row['applied_gain_db']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise LoudnessIntegrityError(
                'normalization observation row disagrees with payload'
            )
        return observation

    # ------------------------------------------------------------------
    # Playback gain states

    def save_gain_state(self, state: PlaybackGainState) -> None:
        _assert_sealed(state, 'state_sha256', 'state_id')
        existing = self.get_gain_state(state.state_id)
        if existing is not None:
            if existing.state_sha256 == state.state_sha256:
                return
            raise LoudnessConflictError(
                'playback gain states are append-only'
            )
        if state.normalization_observation_id is not None and (
            self.get_observation(state.normalization_observation_id)
            is None
        ):
            raise LoudnessIntegrityError(
                'a gain state must reference a persisted normalization '
                'observation when it names one'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_gain_states (
                    state_id, state_sha256, document_id,
                    normalization_observation_id, master_volume_db,
                    measured_in_room_spl_db, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.state_id,
                    state.state_sha256,
                    state.document_id,
                    state.normalization_observation_id,
                    state.master_volume_db,
                    state.measured_in_room_spl_db,
                    state.captured_at_utc,
                    state.model_dump_json(),
                ),
            )

    def get_gain_state(
        self, state_id: str
    ) -> PlaybackGainState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT state_id, state_sha256, document_id,
                       normalization_observation_id, master_volume_db,
                       measured_in_room_spl_db, captured_at_utc,
                       payload_json
                FROM cad_playback_gain_states
                WHERE state_id=?
                """,
                (state_id,),
            ).fetchone()
        if row is None:
            return None
        return self._gain_state_from_row(row)

    def _gain_state_from_row(
        self, row: sqlite3.Row
    ) -> PlaybackGainState:
        state = PlaybackGainState.model_validate_json(row['payload_json'])
        if (
            state.state_id != row['state_id']
            or state.state_sha256 != row['state_sha256']
            or state.document_id != row['document_id']
            or state.normalization_observation_id
            != row['normalization_observation_id']
            or state.master_volume_db != row['master_volume_db']
            or state.measured_in_room_spl_db
            != row['measured_in_room_spl_db']
            or state.captured_at_utc != row['captured_at_utc']
        ):
            raise LoudnessIntegrityError(
                'playback gain state row disagrees with its payload'
            )
        return state

    # ------------------------------------------------------------------
    # Matching records

    def save_matching_record(self, record: LoudnessMatchingRecord) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_matching_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise LoudnessConflictError(
                'loudness matching records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_loudness_matching_records (
                    record_id, record_sha256, document_id,
                    comparison_label, target_quantity,
                    residual_mismatch_db, recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.comparison_label,
                    record.target_quantity,
                    record.residual_mismatch_db,
                    record.recorded_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_matching_record(
        self, record_id: str
    ) -> LoudnessMatchingRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT record_id, record_sha256, document_id,
                       comparison_label, target_quantity,
                       residual_mismatch_db, recorded_at_utc, payload_json
                FROM cad_loudness_matching_records
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        return self._matching_from_row(row)

    def _matching_from_row(
        self, row: sqlite3.Row
    ) -> LoudnessMatchingRecord:
        record = LoudnessMatchingRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.comparison_label != row['comparison_label']
            or record.target_quantity != row['target_quantity']
            or record.residual_mismatch_db != row['residual_mismatch_db']
            or record.recorded_at_utc != row['recorded_at_utc']
        ):
            raise LoudnessIntegrityError(
                'loudness matching record row disagrees with payload'
            )
        return record


__all__ = [
    'CadLoudnessRepository',
    'LoudnessConflictError',
    'LoudnessIntegrityError',
]
