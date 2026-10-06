"""Append-only persistence for the coverage / acoustic-aim authority
(#634, REV57-AUD).

Five tables:

* ``cad_acoustic_aim_states`` — sealed installed aim identities (cabinet
  pose / acoustic axis / dataset axis / design aim / as-built aim).
* ``cad_coverage_listener_areas`` — sealed listener-domain declarations
  with design/holdout roles.
* ``cad_coverage_predictions`` — sealed predicted coverage sets.
* ``cad_coverage_measurement_sets`` — sealed field-evidence sets.
* ``cad_coverage_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_coverage_aim_authority import (
    CadAcousticAimState,
    CadCoverageListenerArea,
    CadCoverageMeasurementSet,
    CadCoveragePrediction,
    CadCoverageQualification,
)


class CoverageAimConflictError(ValueError):
    """A coverage-aim save violated append-only identity rules."""


class CoverageAimIntegrityError(ValueError):
    """A stored coverage-aim row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CoverageAimIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CoverageAimIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadCoverageAimRepository:
    """Native storage for the #634 coverage/aim authority."""

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
                'cad_acoustic_aim_states',
                'cad_coverage_listener_areas',
                'cad_coverage_predictions',
                'cad_coverage_measurement_sets',
                'cad_coverage_qualifications',
            )

    # ------------------------------------------------------------------
    # Aim states

    def save_aim_state(self, aim: CadAcousticAimState) -> None:
        _assert_sealed(aim, 'aim_sha256', 'aim_id')
        existing = self.get_aim_state(aim.aim_id)
        if existing is not None:
            if existing.aim_sha256 == aim.aim_sha256:
                return
            raise CoverageAimConflictError(
                'acoustic aim states are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_acoustic_aim_states (
                    aim_id, aim_sha256, document_id, speaker_entity_id,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    aim.aim_id,
                    aim.aim_sha256,
                    aim.document_id,
                    aim.speaker_entity_id,
                    aim.declared_at_utc,
                    aim.model_dump_json(),
                ),
            )

    def get_aim_state(self, aim_id: str) -> CadAcousticAimState | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_acoustic_aim_states WHERE aim_id=?',
                (aim_id,),
            ).fetchone()
        if row is None:
            return None
        aim = CadAcousticAimState.model_validate_json(row['payload_json'])
        if (
            aim.aim_id != row['aim_id']
            or aim.aim_sha256 != row['aim_sha256']
            or aim.document_id != row['document_id']
            or aim.speaker_entity_id != row['speaker_entity_id']
            or aim.declared_at_utc != row['declared_at_utc']
        ):
            raise CoverageAimIntegrityError(
                'stored aim row disagrees with its payload'
            )
        return aim

    def list_aim_states(
        self, document_id: str | None = None
    ) -> tuple[CadAcousticAimState, ...]:
        query = 'SELECT payload_json FROM cad_acoustic_aim_states'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadAcousticAimState.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Listener areas

    def save_listener_area(self, area: CadCoverageListenerArea) -> None:
        _assert_sealed(area, 'area_sha256', 'area_id')
        existing = self.get_listener_area(area.area_id)
        if existing is not None:
            if existing.area_sha256 == area.area_sha256:
                return
            raise CoverageAimConflictError(
                'coverage listener areas are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_coverage_listener_areas (
                    area_id, area_sha256, document_id, label,
                    position_count, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    area.area_id,
                    area.area_sha256,
                    area.document_id,
                    area.label,
                    len(area.positions),
                    area.declared_at_utc,
                    area.model_dump_json(),
                ),
            )

    def get_listener_area(
        self, area_id: str
    ) -> CadCoverageListenerArea | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_coverage_listener_areas WHERE area_id=?',
                (area_id,),
            ).fetchone()
        if row is None:
            return None
        area = CadCoverageListenerArea.model_validate_json(
            row['payload_json']
        )
        if (
            area.area_id != row['area_id']
            or area.area_sha256 != row['area_sha256']
            or area.document_id != row['document_id']
            or area.label != row['label']
            or len(area.positions) != row['position_count']
            or area.declared_at_utc != row['declared_at_utc']
        ):
            raise CoverageAimIntegrityError(
                'stored listener-area row disagrees with its payload'
            )
        return area

    def list_listener_areas(
        self, document_id: str | None = None
    ) -> tuple[CadCoverageListenerArea, ...]:
        query = 'SELECT payload_json FROM cad_coverage_listener_areas'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadCoverageListenerArea.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Predictions

    def save_prediction(
        self, prediction: CadCoveragePrediction
    ) -> None:
        _assert_sealed(
            prediction, 'prediction_sha256', 'prediction_id'
        )
        existing = self.get_prediction(prediction.prediction_id)
        if existing is not None:
            if existing.prediction_sha256 == prediction.prediction_sha256:
                return
            raise CoverageAimConflictError(
                'coverage predictions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_coverage_predictions (
                    prediction_id, prediction_sha256, document_id,
                    area_ref_id, quantity, summation_model,
                    path_count, predicted_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.prediction_id,
                    prediction.prediction_sha256,
                    prediction.document_id,
                    prediction.area_ref.ref_id,
                    prediction.quantity,
                    prediction.summation_model,
                    len(prediction.paths),
                    prediction.predicted_at_utc,
                    prediction.model_dump_json(),
                ),
            )

    def get_prediction(
        self, prediction_id: str
    ) -> CadCoveragePrediction | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_coverage_predictions '
                'WHERE prediction_id=?',
                (prediction_id,),
            ).fetchone()
        if row is None:
            return None
        prediction = CadCoveragePrediction.model_validate_json(
            row['payload_json']
        )
        if (
            prediction.prediction_id != row['prediction_id']
            or prediction.prediction_sha256 != row['prediction_sha256']
            or prediction.document_id != row['document_id']
            or prediction.area_ref.ref_id != row['area_ref_id']
            or prediction.quantity != row['quantity']
            or prediction.summation_model != row['summation_model']
            or len(prediction.paths) != row['path_count']
            or prediction.predicted_at_utc != row['predicted_at_utc']
        ):
            raise CoverageAimIntegrityError(
                'stored prediction row disagrees with its payload'
            )
        return prediction

    def list_predictions(
        self, document_id: str | None = None
    ) -> tuple[CadCoveragePrediction, ...]:
        query = 'SELECT payload_json FROM cad_coverage_predictions'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadCoveragePrediction.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Measurement sets

    def save_measurement_set(
        self, measurement_set: CadCoverageMeasurementSet
    ) -> None:
        _assert_sealed(measurement_set, 'set_sha256', 'set_id')
        existing = self.get_measurement_set(measurement_set.set_id)
        if existing is not None:
            if existing.set_sha256 == measurement_set.set_sha256:
                return
            raise CoverageAimConflictError(
                'coverage measurement sets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_coverage_measurement_sets (
                    set_id, set_sha256, document_id, area_ref_id,
                    quantity, observation_count, measured_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement_set.set_id,
                    measurement_set.set_sha256,
                    measurement_set.document_id,
                    measurement_set.area_ref.ref_id,
                    measurement_set.quantity,
                    len(measurement_set.observations),
                    measurement_set.measured_at_utc,
                    measurement_set.model_dump_json(),
                ),
            )

    def get_measurement_set(
        self, set_id: str
    ) -> CadCoverageMeasurementSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_coverage_measurement_sets WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        measurement_set = CadCoverageMeasurementSet.model_validate_json(
            row['payload_json']
        )
        if (
            measurement_set.set_id != row['set_id']
            or measurement_set.set_sha256 != row['set_sha256']
            or measurement_set.document_id != row['document_id']
            or measurement_set.area_ref.ref_id != row['area_ref_id']
            or measurement_set.quantity != row['quantity']
            or len(measurement_set.observations)
            != row['observation_count']
            or measurement_set.measured_at_utc != row['measured_at_utc']
        ):
            raise CoverageAimIntegrityError(
                'stored measurement set row disagrees with its payload'
            )
        return measurement_set

    def list_measurement_sets(
        self, document_id: str | None = None
    ) -> tuple[CadCoverageMeasurementSet, ...]:
        query = 'SELECT payload_json FROM cad_coverage_measurement_sets'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadCoverageMeasurementSet.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadCoverageQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == (
                qualification.qualification_sha256
            ):
                return
            raise CoverageAimConflictError(
                'coverage qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_coverage_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    area_ref_id, coverage_state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.area_ref.ref_id,
                    qualification.coverage_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadCoverageQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_coverage_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadCoverageQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.area_ref.ref_id != row['area_ref_id']
            or qualification.coverage_state != row['coverage_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise CoverageAimIntegrityError(
                'stored qualification row disagrees with its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[CadCoverageQualification, ...]:
        query = 'SELECT payload_json FROM cad_coverage_qualifications'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadCoverageQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
