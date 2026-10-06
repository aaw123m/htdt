"""Append-only persistence for the direct-view display authority (#625).

Seven tables:

* ``cad_dv_display_states`` — sealed ``DirectViewDisplayState`` records
  keyed by ``display_state_id``.
* ``cad_dv_stimulus_contexts`` — sealed ``DisplayStimulusContext``
  stimulus identities keyed by ``stimulus_context_id``.
* ``cad_dv_photometric_measurements`` — sealed
  ``DisplayPhotometricMeasurement`` observations; both the bound display
  state and stimulus context must persist.
* ``cad_dv_temporal_observations`` — sealed ``DisplayTemporalObservation``
  records; the display state must persist, and a bound stimulus context
  must exist when declared.
* ``cad_dv_spatial_measurements`` — sealed ``DisplaySpatialMeasurement``
  uniformity point sets bound to state + stimulus.
* ``cad_dv_angle_measurements`` — sealed ``DisplayAngleMeasurement``
  off-axis observations bound to state + stimulus.
* ``cad_dv_qualifications`` — sealed ``DirectViewQualification``
  verdicts; the display state must persist.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict.  Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_direct_view_display import (
    DirectViewDisplayState,
    DirectViewQualification,
    DisplayAngleMeasurement,
    DisplayPhotometricMeasurement,
    DisplaySpatialMeasurement,
    DisplayStimulusContext,
    DisplayTemporalObservation,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class DirectViewDisplayConflictError(ValueError):
    """A direct-view save violated append-only identity rules."""


class DirectViewDisplayIntegrityError(ValueError):
    """A stored direct-view row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise DirectViewDisplayIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadDirectViewDisplayRepository:
    """Native storage for direct-view display qualification records."""

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
                'cad_dv_display_states',
                'cad_dv_stimulus_contexts',
                'cad_dv_photometric_measurements',
                'cad_dv_temporal_observations',
                'cad_dv_spatial_measurements',
                'cad_dv_angle_measurements',
                'cad_dv_qualifications',
            )

    # ------------------------------------------------------------------
    # Display states

    def save_display_state(self, state: DirectViewDisplayState) -> None:
        _assert_sealed(state, 'display_state_sha256', 'display_state_id')
        existing = self.get_display_state(state.display_state_id)
        if existing is not None:
            if existing.display_state_sha256 == state.display_state_sha256:
                return
            raise DirectViewDisplayConflictError(
                'display states are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_display_states (
                    display_state_id, display_state_sha256, document_id,
                    panel_technology, content_mode, local_dimming,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.display_state_id,
                    state.display_state_sha256,
                    state.document_id,
                    state.panel_technology,
                    state.content_mode,
                    state.local_dimming,
                    state.captured_at_utc,
                    state.model_dump_json(),
                ),
            )

    def get_display_state(
        self, display_state_id: str
    ) -> DirectViewDisplayState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT display_state_id, display_state_sha256, document_id,
                       panel_technology, content_mode, local_dimming,
                       captured_at_utc, payload_json
                FROM cad_dv_display_states
                WHERE display_state_id=?
                """,
                (display_state_id,),
            ).fetchone()
        if row is None:
            return None
        return self._state_from_row(row)

    def _state_from_row(
        self, row: sqlite3.Row
    ) -> DirectViewDisplayState:
        state = DirectViewDisplayState.model_validate_json(
            row['payload_json']
        )
        if (
            state.display_state_id != row['display_state_id']
            or state.display_state_sha256 != row['display_state_sha256']
            or state.document_id != row['document_id']
            or state.panel_technology != row['panel_technology']
            or state.content_mode != row['content_mode']
            or state.local_dimming != row['local_dimming']
            or state.captured_at_utc != row['captured_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'display state row disagrees with its payload'
            )
        return state

    # ------------------------------------------------------------------
    # Stimulus contexts

    def save_stimulus_context(
        self, context: DisplayStimulusContext
    ) -> None:
        _assert_sealed(
            context, 'stimulus_context_sha256', 'stimulus_context_id'
        )
        existing = self.get_stimulus_context(context.stimulus_context_id)
        if existing is not None:
            if (
                existing.stimulus_context_sha256
                == context.stimulus_context_sha256
            ):
                return
            raise DirectViewDisplayConflictError(
                'stimulus contexts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_stimulus_contexts (
                    stimulus_context_id, stimulus_context_sha256,
                    document_id, stimulus_ref, field_kind,
                    window_size_percent, apl_percent, content_kind,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.stimulus_context_id,
                    context.stimulus_context_sha256,
                    context.document_id,
                    context.stimulus_ref,
                    context.field_kind,
                    context.window_size_percent,
                    context.apl_percent,
                    context.content_kind,
                    context.model_dump_json(),
                ),
            )

    def get_stimulus_context(
        self, stimulus_context_id: str
    ) -> DisplayStimulusContext | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT stimulus_context_id, stimulus_context_sha256,
                       document_id, stimulus_ref, field_kind,
                       window_size_percent, apl_percent, content_kind,
                       payload_json
                FROM cad_dv_stimulus_contexts
                WHERE stimulus_context_id=?
                """,
                (stimulus_context_id,),
            ).fetchone()
        if row is None:
            return None
        context = DisplayStimulusContext.model_validate_json(
            row['payload_json']
        )
        if (
            context.stimulus_context_id != row['stimulus_context_id']
            or context.stimulus_context_sha256
            != row['stimulus_context_sha256']
            or context.document_id != row['document_id']
            or context.stimulus_ref != row['stimulus_ref']
            or context.field_kind != row['field_kind']
            or context.window_size_percent != row['window_size_percent']
            or context.apl_percent != row['apl_percent']
            or context.content_kind != row['content_kind']
        ):
            raise DirectViewDisplayIntegrityError(
                'stimulus context row disagrees with its payload'
            )
        return context

    def list_stimulus_contexts(
        self, document_id: str
    ) -> tuple[DisplayStimulusContext, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT stimulus_context_id, payload_json
                FROM cad_dv_stimulus_contexts
                WHERE document_id=?
                ORDER BY stimulus_context_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            DisplayStimulusContext.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Photometric measurements

    def save_measurement(
        self, measurement: DisplayPhotometricMeasurement
    ) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise DirectViewDisplayConflictError(
                'photometric measurements are append-only'
            )
        if self.get_display_state(measurement.display_state_id) is None:
            raise DirectViewDisplayIntegrityError(
                'a measurement must reference a persisted display state'
            )
        if (
            self.get_stimulus_context(measurement.stimulus_context_id)
            is None
        ):
            raise DirectViewDisplayIntegrityError(
                'a measurement must reference a persisted stimulus context'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_photometric_measurements (
                    measurement_id, measurement_sha256, document_id,
                    display_state_id, display_state_sha256,
                    stimulus_context_id, stimulus_context_sha256,
                    quantity, measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.display_state_id,
                    measurement.display_state_sha256,
                    measurement.stimulus_context_id,
                    measurement.stimulus_context_sha256,
                    measurement.quantity,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> DisplayPhotometricMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       display_state_id, display_state_sha256,
                       stimulus_context_id, stimulus_context_sha256,
                       quantity, measured_at_utc, payload_json
                FROM cad_dv_photometric_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def list_measurements(
        self, display_state_id: str
    ) -> tuple[DisplayPhotometricMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       display_state_id, display_state_sha256,
                       stimulus_context_id, stimulus_context_sha256,
                       quantity, measured_at_utc, payload_json
                FROM cad_dv_photometric_measurements
                WHERE display_state_id=?
                ORDER BY measured_at_utc, measurement_id
                """,
                (display_state_id,),
            ).fetchall()
        return tuple(self._measurement_from_row(row) for row in rows)

    def _measurement_from_row(
        self, row: sqlite3.Row
    ) -> DisplayPhotometricMeasurement:
        measurement = DisplayPhotometricMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256 != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.display_state_id != row['display_state_id']
            or measurement.display_state_sha256
            != row['display_state_sha256']
            or measurement.stimulus_context_id
            != row['stimulus_context_id']
            or measurement.stimulus_context_sha256
            != row['stimulus_context_sha256']
            or measurement.quantity != row['quantity']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'photometric measurement row disagrees with its payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Temporal observations

    def save_temporal_observation(
        self, observation: DisplayTemporalObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_temporal_observation(
            observation.observation_id
        )
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise DirectViewDisplayConflictError(
                'temporal observations are append-only'
            )
        if self.get_display_state(observation.display_state_id) is None:
            raise DirectViewDisplayIntegrityError(
                'a temporal observation must reference a persisted '
                'display state'
            )
        if (
            observation.stimulus_context_id is not None
            and self.get_stimulus_context(
                observation.stimulus_context_id
            )
            is None
        ):
            raise DirectViewDisplayIntegrityError(
                'a bound stimulus context must persist'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_temporal_observations (
                    observation_id, observation_sha256, document_id,
                    display_state_id, display_state_sha256, state,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.display_state_id,
                    observation.display_state_sha256,
                    observation.state,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_temporal_observation(
        self, observation_id: str
    ) -> DisplayTemporalObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT observation_id, observation_sha256, document_id,
                       display_state_id, display_state_sha256, state,
                       observed_at_utc, payload_json
                FROM cad_dv_temporal_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = DisplayTemporalObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.display_state_id != row['display_state_id']
            or observation.display_state_sha256
            != row['display_state_sha256']
            or observation.state != row['state']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'temporal observation row disagrees with its payload'
            )
        return observation

    def list_temporal_observations(
        self, display_state_id: str
    ) -> tuple[DisplayTemporalObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT observation_id, payload_json
                FROM cad_dv_temporal_observations
                WHERE display_state_id=?
                ORDER BY observed_at_utc, observation_id
                """,
                (display_state_id,),
            ).fetchall()
        return tuple(
            DisplayTemporalObservation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Spatial measurements

    def save_spatial_measurement(
        self, spatial: DisplaySpatialMeasurement
    ) -> None:
        _assert_sealed(spatial, 'spatial_sha256', 'spatial_id')
        existing = self.get_spatial_measurement(spatial.spatial_id)
        if existing is not None:
            if existing.spatial_sha256 == spatial.spatial_sha256:
                return
            raise DirectViewDisplayConflictError(
                'spatial measurements are append-only'
            )
        if self.get_display_state(spatial.display_state_id) is None:
            raise DirectViewDisplayIntegrityError(
                'a spatial measurement must reference a persisted '
                'display state'
            )
        if (
            self.get_stimulus_context(spatial.stimulus_context_id)
            is None
        ):
            raise DirectViewDisplayIntegrityError(
                'a spatial measurement must reference a persisted '
                'stimulus context'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_spatial_measurements (
                    spatial_id, spatial_sha256, document_id,
                    display_state_id, display_state_sha256,
                    stimulus_context_id, stimulus_context_sha256,
                    observable, point_count, measured_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spatial.spatial_id,
                    spatial.spatial_sha256,
                    spatial.document_id,
                    spatial.display_state_id,
                    spatial.display_state_sha256,
                    spatial.stimulus_context_id,
                    spatial.stimulus_context_sha256,
                    spatial.observable,
                    len(spatial.points),
                    spatial.measured_at_utc,
                    spatial.model_dump_json(),
                ),
            )

    def get_spatial_measurement(
        self, spatial_id: str
    ) -> DisplaySpatialMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT spatial_id, spatial_sha256, document_id,
                       display_state_id, display_state_sha256,
                       stimulus_context_id, stimulus_context_sha256,
                       observable, point_count, measured_at_utc,
                       payload_json
                FROM cad_dv_spatial_measurements
                WHERE spatial_id=?
                """,
                (spatial_id,),
            ).fetchone()
        if row is None:
            return None
        return self._spatial_from_row(row)

    def _spatial_from_row(
        self, row: sqlite3.Row
    ) -> DisplaySpatialMeasurement:
        spatial = DisplaySpatialMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            spatial.spatial_id != row['spatial_id']
            or spatial.spatial_sha256 != row['spatial_sha256']
            or spatial.document_id != row['document_id']
            or spatial.display_state_id != row['display_state_id']
            or spatial.display_state_sha256
            != row['display_state_sha256']
            or spatial.stimulus_context_id
            != row['stimulus_context_id']
            or spatial.stimulus_context_sha256
            != row['stimulus_context_sha256']
            or spatial.observable != row['observable']
            or len(spatial.points) != row['point_count']
            or spatial.measured_at_utc != row['measured_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'spatial measurement row disagrees with its payload'
            )
        return spatial

    # ------------------------------------------------------------------
    # Angle measurements

    def save_angle_measurement(
        self, angle: DisplayAngleMeasurement
    ) -> None:
        _assert_sealed(angle, 'angle_sha256', 'angle_id')
        existing = self.get_angle_measurement(angle.angle_id)
        if existing is not None:
            if existing.angle_sha256 == angle.angle_sha256:
                return
            raise DirectViewDisplayConflictError(
                'angle measurements are append-only'
            )
        if self.get_display_state(angle.display_state_id) is None:
            raise DirectViewDisplayIntegrityError(
                'an angle measurement must reference a persisted '
                'display state'
            )
        if (
            self.get_stimulus_context(angle.stimulus_context_id)
            is None
        ):
            raise DirectViewDisplayIntegrityError(
                'an angle measurement must reference a persisted '
                'stimulus context'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_angle_measurements (
                    angle_id, angle_sha256, document_id,
                    display_state_id, display_state_sha256,
                    stimulus_context_id, stimulus_context_sha256,
                    horizontal_angle_deg, vertical_angle_deg, seat_ref,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    angle.angle_id,
                    angle.angle_sha256,
                    angle.document_id,
                    angle.display_state_id,
                    angle.display_state_sha256,
                    angle.stimulus_context_id,
                    angle.stimulus_context_sha256,
                    angle.horizontal_angle_deg,
                    angle.vertical_angle_deg,
                    angle.seat_ref,
                    angle.measured_at_utc,
                    angle.model_dump_json(),
                ),
            )

    def get_angle_measurement(
        self, angle_id: str
    ) -> DisplayAngleMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT angle_id, angle_sha256, document_id,
                       display_state_id, display_state_sha256,
                       stimulus_context_id, stimulus_context_sha256,
                       horizontal_angle_deg, vertical_angle_deg,
                       seat_ref, measured_at_utc, payload_json
                FROM cad_dv_angle_measurements
                WHERE angle_id=?
                """,
                (angle_id,),
            ).fetchone()
        if row is None:
            return None
        angle = DisplayAngleMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            angle.angle_id != row['angle_id']
            or angle.angle_sha256 != row['angle_sha256']
            or angle.document_id != row['document_id']
            or angle.display_state_id != row['display_state_id']
            or angle.display_state_sha256
            != row['display_state_sha256']
            or angle.stimulus_context_id != row['stimulus_context_id']
            or angle.stimulus_context_sha256
            != row['stimulus_context_sha256']
            or angle.horizontal_angle_deg != row['horizontal_angle_deg']
            or angle.vertical_angle_deg != row['vertical_angle_deg']
            or angle.seat_ref != row['seat_ref']
            or angle.measured_at_utc != row['measured_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'angle measurement row disagrees with its payload'
            )
        return angle

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: DirectViewQualification
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
            raise DirectViewDisplayConflictError(
                'qualifications are append-only'
            )
        if (
            self.get_display_state(qualification.display_state_id)
            is None
        ):
            raise DirectViewDisplayIntegrityError(
                'a qualification must reference a persisted display state'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dv_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    display_state_id, display_state_sha256, state,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.display_state_id,
                    qualification.display_state_sha256,
                    qualification.state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> DirectViewQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, display_state_id,
                       display_state_sha256, state, evaluated_at_utc,
                       payload_json
                FROM cad_dv_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return self._qualification_from_row(row)

    def list_qualifications(
        self, display_state_id: str
    ) -> tuple[DirectViewQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, display_state_id,
                       display_state_sha256, state, evaluated_at_utc,
                       payload_json
                FROM cad_dv_qualifications
                WHERE display_state_id=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (display_state_id,),
            ).fetchall()
        return tuple(
            self._qualification_from_row(row) for row in rows
        )

    def _qualification_from_row(
        self, row: sqlite3.Row
    ) -> DirectViewQualification:
        qualification = DirectViewQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.display_state_id != row['display_state_id']
            or qualification.display_state_sha256
            != row['display_state_sha256']
            or qualification.state != row['state']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DirectViewDisplayIntegrityError(
                'qualification row disagrees with its payload'
            )
        return qualification
