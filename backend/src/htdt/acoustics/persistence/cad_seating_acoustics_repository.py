"""Append-only persistence for the seating/occupancy authority (#590).

Four tables:

* ``cad_seat_acoustic_models`` — sealed ``SeatAcousticModel`` records
  keyed by ``seat_model_id``.
* ``cad_occupancy_scenarios`` — sealed ``OccupancyScenario`` records;
  the ``comparability_key`` mirrored column lets callers find the
  occupancy state a measurement was taken under.
* ``cad_clearance_evaluations`` — sealed ``DirectSoundClearanceEvaluation``
  records bound to a stored occupancy scenario.
* ``cad_seating_commissioning_results`` — sealed as-built verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ..domain.cad_seating_acoustics import (
    DirectSoundClearanceEvaluation,
    OccupancyScenario,
    SeatAcousticModel,
    SeatingCommissioningResult,
)
from ...canonical_json import canonical_sha256


class SeatingAcousticsConflictError(ValueError):
    """A seating-acoustics save violated append-only identity rules."""


class SeatingAcousticsIntegrityError(ValueError):
    """A stored seating-acoustics row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise SeatingAcousticsIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadSeatingAcousticsRepository:
    """Native storage for seating/occupancy acoustic authority."""

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
                'cad_seat_acoustic_models',
                'cad_occupancy_scenarios',
                'cad_clearance_evaluations',
                'cad_seating_commissioning_results',
            )

    # ------------------------------------------------------------------
    # Seat acoustic models

    def save_seat_model(self, model: SeatAcousticModel) -> None:
        _assert_sealed(model, 'seat_model_sha256', 'seat_model_id')
        existing = self.get_seat_model(model.seat_model_id)
        if existing is not None:
            if existing.seat_model_sha256 == model.seat_model_sha256:
                return
            raise SeatingAcousticsConflictError(
                'seat acoustic models are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_seat_acoustic_models (
                    seat_model_id, seat_model_sha256, document_id,
                    seat_entity_id, geometry_source, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    model.seat_model_id,
                    model.seat_model_sha256,
                    model.document_id,
                    model.seat_entity_id,
                    model.geometry.source,
                    model.model_dump_json(),
                ),
            )

    def get_seat_model(
        self, seat_model_id: str
    ) -> SeatAcousticModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT seat_model_id, seat_model_sha256, document_id,
                       seat_entity_id, geometry_source, payload_json
                FROM cad_seat_acoustic_models
                WHERE seat_model_id=?
                """,
                (seat_model_id,),
            ).fetchone()
        if row is None:
            return None
        return self._seat_model_from_row(row)

    def seat_models_for_entity(
        self, seat_entity_id: str
    ) -> tuple[SeatAcousticModel, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT seat_model_id, seat_model_sha256, document_id,
                       seat_entity_id, geometry_source, payload_json
                FROM cad_seat_acoustic_models
                WHERE seat_entity_id=?
                ORDER BY seat_model_id
                """,
                (seat_entity_id,),
            ).fetchall()
        return tuple(self._seat_model_from_row(row) for row in rows)

    def _seat_model_from_row(
        self, row: sqlite3.Row
    ) -> SeatAcousticModel:
        model = SeatAcousticModel.model_validate_json(row['payload_json'])
        if (
            model.seat_model_id != row['seat_model_id']
            or model.seat_model_sha256 != row['seat_model_sha256']
            or model.document_id != row['document_id']
            or model.seat_entity_id != row['seat_entity_id']
            or model.geometry.source != row['geometry_source']
        ):
            raise SeatingAcousticsIntegrityError(
                'seat acoustic model row disagrees with its payload'
            )
        return model

    # ------------------------------------------------------------------
    # Occupancy scenarios

    def save_occupancy_scenario(self, scenario: OccupancyScenario) -> None:
        _assert_sealed(
            scenario, 'occupancy_sha256', 'occupancy_scenario_id'
        )
        existing = self.get_occupancy_scenario(
            scenario.occupancy_scenario_id
        )
        if existing is not None:
            if existing.occupancy_sha256 == scenario.occupancy_sha256:
                return
            raise SeatingAcousticsConflictError(
                'occupancy scenarios are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_occupancy_scenarios (
                    occupancy_scenario_id, occupancy_sha256, document_id,
                    label, state, comparability_key, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.occupancy_scenario_id,
                    scenario.occupancy_sha256,
                    scenario.document_id,
                    scenario.label,
                    scenario.state,
                    scenario.comparability_key,
                    scenario.created_at_utc,
                    scenario.model_dump_json(),
                ),
            )

    def get_occupancy_scenario(
        self, occupancy_scenario_id: str
    ) -> OccupancyScenario | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT occupancy_scenario_id, occupancy_sha256,
                       document_id, label, state, comparability_key,
                       created_at_utc, payload_json
                FROM cad_occupancy_scenarios
                WHERE occupancy_scenario_id=?
                """,
                (occupancy_scenario_id,),
            ).fetchone()
        if row is None:
            return None
        return self._occupancy_from_row(row)

    def list_occupancy_scenarios(
        self, document_id: str
    ) -> tuple[OccupancyScenario, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT occupancy_scenario_id, occupancy_sha256,
                       document_id, label, state, comparability_key,
                       created_at_utc, payload_json
                FROM cad_occupancy_scenarios
                WHERE document_id=?
                ORDER BY created_at_utc, occupancy_scenario_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._occupancy_from_row(row) for row in rows)

    def _occupancy_from_row(self, row: sqlite3.Row) -> OccupancyScenario:
        scenario = OccupancyScenario.model_validate_json(row['payload_json'])
        if (
            scenario.occupancy_scenario_id != row['occupancy_scenario_id']
            or scenario.occupancy_sha256 != row['occupancy_sha256']
            or scenario.document_id != row['document_id']
            or scenario.label != row['label']
            or scenario.state != row['state']
            or scenario.comparability_key != row['comparability_key']
            or scenario.created_at_utc != row['created_at_utc']
        ):
            raise SeatingAcousticsIntegrityError(
                'occupancy scenario row disagrees with its payload'
            )
        return scenario

    # ------------------------------------------------------------------
    # Clearance evaluations

    def save_clearance_evaluation(
        self, evaluation: DirectSoundClearanceEvaluation
    ) -> None:
        _assert_sealed(evaluation, 'evaluation_sha256', 'evaluation_id')
        existing = self.get_clearance_evaluation(evaluation.evaluation_id)
        if existing is not None:
            if existing.evaluation_sha256 == evaluation.evaluation_sha256:
                return
            raise SeatingAcousticsConflictError(
                'clearance evaluations are append-only'
            )
        scenario = self.get_occupancy_scenario(
            evaluation.occupancy_scenario_id
        )
        if scenario is None:
            raise SeatingAcousticsIntegrityError(
                'a clearance evaluation must reference a persisted '
                'occupancy scenario'
            )
        if scenario.occupancy_sha256 != evaluation.occupancy_scenario_sha256:
            raise SeatingAcousticsIntegrityError(
                'clearance evaluation scenario hash does not match the '
                'stored scenario'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_clearance_evaluations (
                    evaluation_id, evaluation_sha256, document_id,
                    occupancy_scenario_id, occupancy_scenario_sha256,
                    listener_ref, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.document_id,
                    evaluation.occupancy_scenario_id,
                    evaluation.occupancy_scenario_sha256,
                    evaluation.listener_ref,
                    evaluation.evaluated_at_utc,
                    evaluation.model_dump_json(),
                ),
            )

    def get_clearance_evaluation(
        self, evaluation_id: str
    ) -> DirectSoundClearanceEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evaluation_id, evaluation_sha256, document_id,
                       occupancy_scenario_id, occupancy_scenario_sha256,
                       listener_ref, evaluated_at_utc, payload_json
                FROM cad_clearance_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        evaluation = DirectSoundClearanceEvaluation.model_validate_json(
            row['payload_json']
        )
        if (
            evaluation.evaluation_id != row['evaluation_id']
            or evaluation.evaluation_sha256 != row['evaluation_sha256']
            or evaluation.document_id != row['document_id']
            or evaluation.occupancy_scenario_id
            != row['occupancy_scenario_id']
            or evaluation.occupancy_scenario_sha256
            != row['occupancy_scenario_sha256']
            or evaluation.listener_ref != row['listener_ref']
            or evaluation.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SeatingAcousticsIntegrityError(
                'clearance evaluation row disagrees with its payload'
            )
        return evaluation

    # ------------------------------------------------------------------
    # Commissioning results

    def save_commissioning_result(
        self, result: SeatingCommissioningResult
    ) -> None:
        _assert_sealed(result, 'result_sha256', 'result_id')
        existing = self.get_commissioning_result(result.result_id)
        if existing is not None:
            if existing.result_sha256 == result.result_sha256:
                return
            raise SeatingAcousticsConflictError(
                'seating commissioning results are append-only'
            )
        for seat_model_id, seat_model_sha in result.seat_model_refs:
            stored = self.get_seat_model(seat_model_id)
            if stored is None:
                raise SeatingAcousticsIntegrityError(
                    'a commissioning result must reference persisted '
                    'seat models'
                )
            if stored.seat_model_sha256 != seat_model_sha:
                raise SeatingAcousticsIntegrityError(
                    'commissioning seat-model hash does not match the '
                    'stored model'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_seating_commissioning_results (
                    result_id, result_sha256, document_id, verdict,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.result_sha256,
                    result.document_id,
                    result.verdict,
                    result.measured_at_utc,
                    result.model_dump_json(),
                ),
            )

    def get_commissioning_result(
        self, result_id: str
    ) -> SeatingCommissioningResult | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT result_id, result_sha256, document_id, verdict,
                       measured_at_utc, payload_json
                FROM cad_seating_commissioning_results
                WHERE result_id=?
                """,
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        result = SeatingCommissioningResult.model_validate_json(
            row['payload_json']
        )
        if (
            result.result_id != row['result_id']
            or result.result_sha256 != row['result_sha256']
            or result.document_id != row['document_id']
            or result.verdict != row['verdict']
            or result.measured_at_utc != row['measured_at_utc']
        ):
            raise SeatingAcousticsIntegrityError(
                'seating commissioning row disagrees with its payload'
            )
        return result


__all__ = [
    'CadSeatingAcousticsRepository',
    'SeatingAcousticsConflictError',
    'SeatingAcousticsIntegrityError',
]
