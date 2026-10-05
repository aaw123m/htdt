"""Append-only persistence for the background-noise metric authority
(#580).

Three tables:

* ``cad_room_noise_metric_profiles`` — sealed ``RoomNoiseMetricProfile``
  authorities keyed by ``profile_id``. Re-saving an identical row is a
  no-op; a divergent hash for the same id is a conflict — a criterion
  profile can never be silently revised.
* ``cad_background_noise_measurements`` — canonical banded evidence
  keyed by ``measurement_id``.
* ``cad_noise_criterion_evaluations`` — derived ratings. An evaluation
  may only be persisted against a stored measurement AND a stored
  profile: a rating can never float free of its raw evidence or claim a
  profile that was never registered.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_room_noise_metrics import (
    BackgroundNoiseMeasurement,
    NoiseCriterionEvaluation,
    RoomNoiseMetricProfile,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class RoomNoiseMetricConflictError(ValueError):
    """A room-noise-metric save violated append-only identity rules."""


class RoomNoiseMetricIntegrityError(ValueError):
    """A stored room-noise-metric row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    """Reject records whose id/sha no longer seal their payload —
    ``model_copy`` bypasses validation, so a forged reuse must be caught
    here rather than silently appended under a stale identity."""
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise RoomNoiseMetricIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadRoomNoiseMetricRepository:
    """Native storage for noise metric profiles/measurements/ratings."""

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
                'cad_room_noise_metric_profiles',
                'cad_background_noise_measurements',
                'cad_noise_criterion_evaluations',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: RoomNoiseMetricProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise RoomNoiseMetricConflictError(
                'room noise metric profiles are append-only — a profile '
                'revision mints a new profile_id'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_room_noise_metric_profiles (
                    profile_id, profile_sha256, document_id,
                    metric_family, standard_id, standard_edition, status,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.metric_family,
                    profile.standard_id,
                    profile.standard_edition,
                    profile.status,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> RoomNoiseMetricProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       metric_family, standard_id, standard_edition,
                       status, created_at_utc, payload_json
                FROM cad_room_noise_metric_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self, document_id: str
    ) -> tuple[RoomNoiseMetricProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       metric_family, standard_id, standard_edition,
                       status, created_at_utc, payload_json
                FROM cad_room_noise_metric_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(
        self, row: sqlite3.Row
    ) -> RoomNoiseMetricProfile:
        profile = RoomNoiseMetricProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.metric_family != row['metric_family']
            or profile.standard_id != row['standard_id']
            or profile.standard_edition != row['standard_edition']
            or profile.status != row['status']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise RoomNoiseMetricIntegrityError(
                'room noise metric profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Measurements

    def save_measurement(
        self, measurement: BackgroundNoiseMeasurement
    ) -> None:
        _assert_sealed(measurement, 'measurement_sha256', 'measurement_id')
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise RoomNoiseMetricConflictError(
                'background noise measurements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_background_noise_measurements (
                    measurement_id, measurement_sha256, document_id,
                    temporal_class, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.temporal_class,
                    measurement.captured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> BackgroundNoiseMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       temporal_class, captured_at_utc, payload_json
                FROM cad_background_noise_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def list_measurements(
        self, document_id: str
    ) -> tuple[BackgroundNoiseMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       temporal_class, captured_at_utc, payload_json
                FROM cad_background_noise_measurements
                WHERE document_id=?
                ORDER BY captured_at_utc, measurement_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._measurement_from_row(row) for row in rows
        )

    def _measurement_from_row(
        self, row: sqlite3.Row
    ) -> BackgroundNoiseMeasurement:
        measurement = BackgroundNoiseMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.temporal_class != row['temporal_class']
            or measurement.captured_at_utc != row['captured_at_utc']
        ):
            raise RoomNoiseMetricIntegrityError(
                'background noise measurement row disagrees with payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(
        self, evaluation: NoiseCriterionEvaluation
    ) -> None:
        _assert_sealed(evaluation, 'evaluation_sha256', 'evaluation_id')
        existing = self.get_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise RoomNoiseMetricConflictError(
                'noise criterion evaluations are append-only — a revised '
                'rating is a new derived record, never an edit'
            )
        measurement = self.get_measurement(evaluation.measurement_id)
        if measurement is None:
            raise RoomNoiseMetricIntegrityError(
                'an evaluation must reference a persisted measurement'
            )
        if measurement.measurement_sha256 != evaluation.measurement_sha256:
            raise RoomNoiseMetricIntegrityError(
                'evaluation measurement hash does not match the stored '
                'measurement'
            )
        profile = self.get_profile(evaluation.profile_id)
        if profile is None:
            raise RoomNoiseMetricIntegrityError(
                'an evaluation must reference a persisted profile — a '
                'rating cannot cite a criterion profile that was never '
                'registered'
            )
        if profile.profile_sha256 != evaluation.profile_sha256:
            raise RoomNoiseMetricIntegrityError(
                'evaluation profile hash does not match the stored profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_noise_criterion_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    measurement_id, measurement_sha256, profile_id,
                    profile_sha256, metric_family, applicability,
                    rating_label, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.measurement_id,
                    evaluation.measurement_sha256,
                    evaluation.profile_id,
                    evaluation.profile_sha256,
                    evaluation.metric_family,
                    evaluation.applicability,
                    evaluation.rating_label,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_evaluation(
        self, evaluation_id: str
    ) -> NoiseCriterionEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       measurement_id, measurement_sha256, profile_id,
                       profile_sha256, metric_family, applicability,
                       rating_label, evaluated_at_utc, payload_json
                FROM cad_noise_criterion_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def evaluations_for_measurement(
        self, measurement_id: str
    ) -> tuple[NoiseCriterionEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       measurement_id, measurement_sha256, profile_id,
                       profile_sha256, metric_family, applicability,
                       rating_label, evaluated_at_utc, payload_json
                FROM cad_noise_criterion_evaluations
                WHERE measurement_id=?
                ORDER BY evaluated_at_utc, evaluation_id
                """,
                (measurement_id,),
            ).fetchall()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def _evaluation_from_row(
        self, row: sqlite3.Row
    ) -> NoiseCriterionEvaluation:
        evaluation = NoiseCriterionEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.measurement_id != row['measurement_id']
            or evaluation.measurement_sha256 != row['measurement_sha256']
            or evaluation.profile_id != row['profile_id']
            or evaluation.profile_sha256 != row['profile_sha256']
            or evaluation.metric_family != row['metric_family']
            or evaluation.applicability != row['applicability']
            or evaluation.rating_label != row['rating_label']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise RoomNoiseMetricIntegrityError(
                'noise criterion evaluation row disagrees with payload'
            )
        return evaluation


__all__ = [
    'CadRoomNoiseMetricRepository',
    'RoomNoiseMetricConflictError',
    'RoomNoiseMetricIntegrityError',
]
