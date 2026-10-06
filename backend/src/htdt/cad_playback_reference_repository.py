"""Append-only persistence for the playback reference-calibration
authority (#618).

Four tables:

* ``cad_ref_cal_profiles`` — sealed ``CadReferenceProfile`` identities.
* ``cad_ref_cal_stimuli`` — sealed ``CadCalibrationStimulus`` records.
* ``cad_ref_cal_observations`` — sealed
  ``CadChannelCalibrationObservation`` channel measurements.
* ``cad_ref_cal_qualifications`` — sealed
  ``CadReferenceCalibrationQualification`` verdicts; the profile must
  persist first.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_playback_reference_authority import (
    CadCalibrationStimulus,
    CadChannelCalibrationObservation,
    CadReferenceCalibrationQualification,
    CadReferenceProfile,
)


class RefCalConflictError(ValueError):
    """A reference-calibration save violated append-only rules."""


class RefCalIntegrityError(ValueError):
    """A stored reference-calibration row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise RefCalIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise RefCalIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadPlaybackReferenceRepository:
    """Native storage for the #618 reference-calibration records."""

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
                'cad_ref_cal_profiles',
                'cad_ref_cal_stimuli',
                'cad_ref_cal_observations',
                'cad_ref_cal_qualifications',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: CadReferenceProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise RefCalConflictError(
                'reference profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ref_cal_profiles (
                    profile_id, profile_sha256, document_id,
                    profile_kind, profile_document, lifecycle_state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.profile_kind,
                    profile.profile_document,
                    profile.lifecycle_state,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadReferenceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ref_cal_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadReferenceProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.profile_kind != row['profile_kind']
            or profile.profile_document != row['profile_document']
            or profile.lifecycle_state != row['lifecycle_state']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise RefCalIntegrityError(
                'reference profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadReferenceProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ref_cal_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadReferenceProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Stimuli

    def save_stimulus(self, stimulus: CadCalibrationStimulus) -> None:
        _assert_sealed(stimulus, 'stimulus_sha256', 'stimulus_id')
        existing = self.get_stimulus(stimulus.stimulus_id)
        if existing is not None:
            if existing.stimulus_sha256 == stimulus.stimulus_sha256:
                return
            raise RefCalConflictError(
                'calibration stimuli are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ref_cal_stimuli (
                    stimulus_id, stimulus_sha256, document_id,
                    source_kind, signal_class, digital_level_dbfs,
                    device_identity, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stimulus.stimulus_id,
                    stimulus.stimulus_sha256,
                    stimulus.document_id,
                    stimulus.source_kind,
                    stimulus.signal_class,
                    stimulus.digital_level_dbfs,
                    stimulus.device_identity,
                    stimulus.declared_at_utc,
                    stimulus.model_dump_json(),
                ),
            )

    def get_stimulus(
        self, stimulus_id: str
    ) -> CadCalibrationStimulus | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ref_cal_stimuli '
                'WHERE stimulus_id=?',
                (stimulus_id,),
            ).fetchone()
        if row is None:
            return None
        stimulus = CadCalibrationStimulus.model_validate_json(
            row['payload_json']
        )
        if (
            stimulus.stimulus_id != row['stimulus_id']
            or stimulus.stimulus_sha256 != row['stimulus_sha256']
            or stimulus.document_id != row['document_id']
            or stimulus.source_kind != row['source_kind']
            or stimulus.signal_class != row['signal_class']
            or stimulus.digital_level_dbfs != row['digital_level_dbfs']
            or stimulus.device_identity != row['device_identity']
            or stimulus.declared_at_utc != row['declared_at_utc']
        ):
            raise RefCalIntegrityError(
                'calibration stimulus row disagrees with payload'
            )
        return stimulus

    def list_stimuli(
        self, document_id: str
    ) -> tuple[CadCalibrationStimulus, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ref_cal_stimuli '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadCalibrationStimulus.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Channel observations

    def save_observation(
        self, observation: CadChannelCalibrationObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise RefCalConflictError(
                'channel calibration observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ref_cal_observations (
                    observation_id, observation_sha256, document_id,
                    channel_role, signal_class, stimulus_ref_id,
                    quantity, measured_spl_db, weighting,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.channel_role,
                    observation.signal_class,
                    (
                        None if observation.stimulus_ref is None
                        else observation.stimulus_ref.ref_id
                    ),
                    observation.quantity,
                    observation.measured_spl_db,
                    observation.weighting,
                    observation.measured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadChannelCalibrationObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ref_cal_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = (
            CadChannelCalibrationObservation.model_validate_json(
                row['payload_json']
            )
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.channel_role != row['channel_role']
            or observation.signal_class != row['signal_class']
            or (
                None if observation.stimulus_ref is None
                else observation.stimulus_ref.ref_id
            ) != row['stimulus_ref_id']
            or observation.quantity != row['quantity']
            or observation.measured_spl_db != row['measured_spl_db']
            or observation.weighting != row['weighting']
            or observation.measured_at_utc != row['measured_at_utc']
        ):
            raise RefCalIntegrityError(
                'channel observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[CadChannelCalibrationObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ref_cal_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadChannelCalibrationObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadReferenceCalibrationQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise RefCalConflictError(
                'reference-calibration qualifications are append-only'
            )
        if self.get_profile(qualification.profile_ref.ref_id) is None:
            raise RefCalConflictError(
                'the bound profile must persist before qualifications'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ref_cal_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, stimulus_state,
                    measurement_state, lfe_state, alignment_state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.stimulus_state,
                    qualification.measurement_state,
                    qualification.lfe_state,
                    qualification.alignment_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadReferenceCalibrationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ref_cal_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadReferenceCalibrationQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.stimulus_state != row['stimulus_state']
            or qualification.measurement_state
            != row['measurement_state']
            or qualification.lfe_state != row['lfe_state']
            or qualification.alignment_state != row['alignment_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise RefCalIntegrityError(
                'reference-calibration row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadReferenceCalibrationQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ref_cal_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadReferenceCalibrationQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadPlaybackReferenceRepository',
    'RefCalConflictError',
    'RefCalIntegrityError',
]
