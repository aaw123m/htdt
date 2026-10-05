"""Append-only persistence for the speech-intelligibility authority
(#605).

Four tables:

* ``cad_speech_intelligibility_profiles`` — sealed STI profiles keyed by
  ``profile_id`` (exact IEC revision + method + weighting).
* ``cad_sti_measurements`` — sealed measured/derived STI evidence.
* ``cad_sti_predictions`` — sealed predicted STI; a distinct evidence
  class that never mints measured-equivalent status.
* ``cad_dialogue_intelligibility_assessments`` — sealed seat-distribution
  diagnostic views.

Every STI row binds its exact profile and its #580 noise measurement —
intelligibility evidence can never float free of the noise state that
produced it.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_sti_authority import (
    DialogueIntelligibilityAssessment,
    SpeechIntelligibilityProfile,
    STIMeasurement,
    STIPrediction,
)


class STIConflictError(ValueError):
    """An STI save violated append-only identity rules."""


class STIIntegrityError(ValueError):
    """A stored STI row disagreed with its payload or references."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    """Reject records whose id/sha no longer seal their payload."""
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise STIIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadSTIRepository:
    """Native storage for STI profiles/measurements/predictions."""

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
                'cad_speech_intelligibility_profiles',
                'cad_sti_measurements',
                'cad_sti_predictions',
                'cad_dialogue_intelligibility_assessments',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: SpeechIntelligibilityProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise STIConflictError(
                'speech intelligibility profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_speech_intelligibility_profiles (
                    profile_id, profile_sha256, document_id,
                    standard_id, standard_edition, method, voice_class,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.standard_id,
                    profile.standard_edition,
                    profile.method,
                    profile.voice_class,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> SpeechIntelligibilityProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       standard_id, standard_edition, method, voice_class,
                       created_at_utc, payload_json
                FROM cad_speech_intelligibility_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self, document_id: str
    ) -> tuple[SpeechIntelligibilityProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       standard_id, standard_edition, method, voice_class,
                       created_at_utc, payload_json
                FROM cad_speech_intelligibility_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(
        self, row: sqlite3.Row
    ) -> SpeechIntelligibilityProfile:
        profile = SpeechIntelligibilityProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.standard_id != row['standard_id']
            or profile.standard_edition != row['standard_edition']
            or profile.method != row['method']
            or profile.voice_class != row['voice_class']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise STIIntegrityError(
                'speech intelligibility profile row disagrees with payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Measurements

    def save_measurement(self, measurement: STIMeasurement) -> None:
        _assert_sealed(measurement, 'measurement_sha256', 'measurement_id')
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise STIConflictError('STI measurements are append-only')
        if self.get_profile(measurement.profile_id) is None:
            raise STIIntegrityError(
                'an STI measurement must reference a persisted profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_sti_measurements (
                    measurement_id, measurement_sha256, document_id,
                    profile_id, profile_sha256, method,
                    noise_measurement_id, sti_value, applicability,
                    seat_ref, measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.profile_id,
                    measurement.profile_sha256,
                    measurement.method,
                    measurement.noise_measurement_id,
                    measurement.sti_value,
                    measurement.applicability,
                    measurement.path.seat_ref,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> STIMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       profile_id, profile_sha256, method,
                       noise_measurement_id, sti_value, applicability,
                       seat_ref, measured_at_utc, payload_json
                FROM cad_sti_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def list_measurements(
        self, document_id: str
    ) -> tuple[STIMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       profile_id, profile_sha256, method,
                       noise_measurement_id, sti_value, applicability,
                       seat_ref, measured_at_utc, payload_json
                FROM cad_sti_measurements
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
    ) -> STIMeasurement:
        measurement = STIMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256 != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.profile_id != row['profile_id']
            or measurement.profile_sha256 != row['profile_sha256']
            or measurement.method != row['method']
            or measurement.noise_measurement_id
            != row['noise_measurement_id']
            or measurement.sti_value != row['sti_value']
            or measurement.applicability != row['applicability']
            or measurement.path.seat_ref != row['seat_ref']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise STIIntegrityError(
                'STI measurement row disagrees with its payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Predictions

    def save_prediction(self, prediction: STIPrediction) -> None:
        _assert_sealed(prediction, 'prediction_sha256', 'prediction_id')
        existing = self.get_prediction(prediction.prediction_id)
        if existing is not None:
            if existing.prediction_sha256 == prediction.prediction_sha256:
                return
            raise STIConflictError('STI predictions are append-only')
        if self.get_profile(prediction.profile_id) is None:
            raise STIIntegrityError(
                'an STI prediction must reference a persisted profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_sti_predictions (
                    prediction_id, prediction_sha256, document_id,
                    profile_id, profile_sha256, method, model_version,
                    validation_ref, noise_measurement_id, sti_value,
                    predicted_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.prediction_id,
                    prediction.prediction_sha256,
                    prediction.document_id,
                    prediction.profile_id,
                    prediction.profile_sha256,
                    prediction.method,
                    prediction.model_version,
                    prediction.validation_ref,
                    prediction.noise_measurement_id,
                    prediction.sti_value,
                    prediction.predicted_at_utc,
                    prediction.model_dump_json(),
                ),
            )

    def get_prediction(
        self, prediction_id: str
    ) -> STIPrediction | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT prediction_id, prediction_sha256, document_id,
                       profile_id, profile_sha256, method, model_version,
                       validation_ref, noise_measurement_id, sti_value,
                       predicted_at_utc, payload_json
                FROM cad_sti_predictions
                WHERE prediction_id=?
                """,
                (prediction_id,),
            ).fetchone()
        if row is None:
            return None
        return self._prediction_from_row(row)

    def _prediction_from_row(
        self, row: sqlite3.Row
    ) -> STIPrediction:
        prediction = STIPrediction.model_validate_json(
            row['payload_json']
        )
        if (
            prediction.prediction_id != row['prediction_id']
            or prediction.prediction_sha256 != row['prediction_sha256']
            or prediction.document_id != row['document_id']
            or prediction.profile_id != row['profile_id']
            or prediction.profile_sha256 != row['profile_sha256']
            or prediction.method != row['method']
            or prediction.model_version != row['model_version']
            or prediction.validation_ref != row['validation_ref']
            or prediction.noise_measurement_id
            != row['noise_measurement_id']
            or prediction.sti_value != row['sti_value']
            or prediction.predicted_at_utc != row['predicted_at_utc']
        ):
            raise STIIntegrityError(
                'STI prediction row disagrees with its payload'
            )
        return prediction

    # ------------------------------------------------------------------
    # Assessments

    def save_assessment(
        self, assessment: DialogueIntelligibilityAssessment
    ) -> None:
        _assert_sealed(assessment, 'assessment_sha256', 'assessment_id')
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise STIConflictError(
                'dialogue intelligibility assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dialogue_intelligibility_assessments (
                    assessment_id, assessment_sha256, document_id,
                    seat_count, worst_seat_label, assessed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    len(assessment.seat_results),
                    assessment.worst_seat_label,
                    assessment.assessed_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> DialogueIntelligibilityAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT assessment_id, assessment_sha256, document_id,
                       seat_count, worst_seat_label, assessed_at_utc,
                       payload_json
                FROM cad_dialogue_intelligibility_assessments
                WHERE assessment_id=?
                """,
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        return self._assessment_from_row(row)

    def _assessment_from_row(
        self, row: sqlite3.Row
    ) -> DialogueIntelligibilityAssessment:
        assessment = DialogueIntelligibilityAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or len(assessment.seat_results) != row['seat_count']
            or assessment.worst_seat_label != row['worst_seat_label']
            or assessment.assessed_at_utc != row['assessed_at_utc']
        ):
            raise STIIntegrityError(
                'dialogue intelligibility assessment row disagrees with '
                'its payload'
            )
        return assessment


__all__ = [
    'CadSTIRepository',
    'STIConflictError',
    'STIIntegrityError',
]
