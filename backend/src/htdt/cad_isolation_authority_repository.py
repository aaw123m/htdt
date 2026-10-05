"""Append-only persistence for the inter-room isolation authority (#576).

Five tables:

* ``cad_isolation_elements`` — sealed ``IsolationConstructionElement``
  records keyed by ``element_id``.
* ``cad_interroom_scenarios`` — sealed ``InterRoomIsolationScenario``
  records keyed by ``scenario_id``.
* ``cad_interroom_field_measurements`` — sealed banded field evidence;
  a measurement may only be persisted against a stored scenario whose
  sha matches — field evidence can never float free of its scenario.
* ``cad_isolation_calibrations`` — declared predict<->measure
  calibration records.
* ``cad_isolation_qualifications`` — derived verdicts; a qualification
  may only be persisted against a stored scenario.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_isolation_authority import (
    InterRoomFieldMeasurement,
    InterRoomIsolationScenario,
    IsolationCalibrationRecord,
    IsolationConstructionElement,
    SoundIsolationQualification,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class IsolationAuthorityConflictError(ValueError):
    """An isolation-authority save violated append-only identity rules."""


class IsolationAuthorityIntegrityError(ValueError):
    """A stored isolation-authority row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise IsolationAuthorityIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadIsolationAuthorityRepository:
    """Native storage for inter-room isolation authorities."""

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
                'cad_isolation_elements',
                'cad_interroom_scenarios',
                'cad_interroom_field_measurements',
                'cad_isolation_calibrations',
                'cad_isolation_qualifications',
            )

    # ------------------------------------------------------------------
    # Elements

    def save_element(self, element: IsolationConstructionElement) -> None:
        _assert_sealed(element, 'element_sha256', 'element_id')
        existing = self.get_element(element.element_id)
        if existing is not None:
            if existing.element_sha256 == element.element_sha256:
                return
            raise IsolationAuthorityConflictError(
                'isolation elements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_isolation_elements (
                    element_id, element_sha256, document_id,
                    label, construction_class, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    element.element_id,
                    element.element_sha256,
                    element.document_id,
                    element.label,
                    element.construction_class,
                    element.model_dump_json(),
                ),
            )

    def get_element(
        self, element_id: str
    ) -> IsolationConstructionElement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT element_id, element_sha256, document_id,
                       label, construction_class, payload_json
                FROM cad_isolation_elements
                WHERE element_id=?
                """,
                (element_id,),
            ).fetchone()
        if row is None:
            return None
        return self._element_from_row(row)

    def list_elements(
        self, document_id: str
    ) -> tuple[IsolationConstructionElement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT element_id, element_sha256, document_id,
                       label, construction_class, payload_json
                FROM cad_isolation_elements
                WHERE document_id=?
                ORDER BY element_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._element_from_row(row) for row in rows)

    def _element_from_row(
        self, row: sqlite3.Row
    ) -> IsolationConstructionElement:
        element = IsolationConstructionElement.model_validate_json(
            row['payload_json']
        )
        if (
            element.element_id != row['element_id']
            or element.element_sha256 != row['element_sha256']
            or element.document_id != row['document_id']
            or element.label != row['label']
            or element.construction_class != row['construction_class']
        ):
            raise IsolationAuthorityIntegrityError(
                'isolation element row disagrees with its payload'
            )
        return element

    # ------------------------------------------------------------------
    # Scenarios

    def save_scenario(self, scenario: InterRoomIsolationScenario) -> None:
        _assert_sealed(scenario, 'scenario_sha256', 'scenario_id')
        existing = self.get_scenario(scenario.scenario_id)
        if existing is not None:
            if existing.scenario_sha256 == scenario.scenario_sha256:
                return
            raise IsolationAuthorityConflictError(
                'inter-room scenarios are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_interroom_scenarios (
                    scenario_id, scenario_sha256, document_id, label,
                    source_region_id, receiving_region_id,
                    construction_state, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.label,
                    scenario.source_region_id,
                    scenario.receiving_region_id,
                    scenario.construction_state,
                    scenario.created_at_utc,
                    scenario.model_dump_json(),
                ),
            )

    def get_scenario(
        self, scenario_id: str
    ) -> InterRoomIsolationScenario | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT scenario_id, scenario_sha256, document_id, label,
                       source_region_id, receiving_region_id,
                       construction_state, created_at_utc, payload_json
                FROM cad_interroom_scenarios
                WHERE scenario_id=?
                """,
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        return self._scenario_from_row(row)

    def list_scenarios(
        self, document_id: str
    ) -> tuple[InterRoomIsolationScenario, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT scenario_id, scenario_sha256, document_id, label,
                       source_region_id, receiving_region_id,
                       construction_state, created_at_utc, payload_json
                FROM cad_interroom_scenarios
                WHERE document_id=?
                ORDER BY created_at_utc, scenario_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._scenario_from_row(row) for row in rows)

    def _scenario_from_row(
        self, row: sqlite3.Row
    ) -> InterRoomIsolationScenario:
        scenario = InterRoomIsolationScenario.model_validate_json(
            row['payload_json']
        )
        if (
            scenario.scenario_id != row['scenario_id']
            or scenario.scenario_sha256 != row['scenario_sha256']
            or scenario.document_id != row['document_id']
            or scenario.label != row['label']
            or scenario.source_region_id != row['source_region_id']
            or scenario.receiving_region_id != row['receiving_region_id']
            or scenario.construction_state != row['construction_state']
            or scenario.created_at_utc != row['created_at_utc']
        ):
            raise IsolationAuthorityIntegrityError(
                'inter-room scenario row disagrees with its payload'
            )
        return scenario

    # ------------------------------------------------------------------
    # Field measurements

    def save_field_measurement(
        self, measurement: InterRoomFieldMeasurement
    ) -> None:
        _assert_sealed(measurement, 'measurement_sha256', 'measurement_id')
        existing = self.get_field_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise IsolationAuthorityConflictError(
                'field measurements are append-only'
            )
        scenario = self.get_scenario(measurement.scenario_id)
        if scenario is None:
            raise IsolationAuthorityIntegrityError(
                'a field measurement must reference a persisted scenario'
            )
        if scenario.scenario_sha256 != measurement.scenario_sha256:
            raise IsolationAuthorityIntegrityError(
                'field measurement scenario hash does not match the '
                'stored scenario'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_interroom_field_measurements (
                    measurement_id, measurement_sha256, document_id,
                    scenario_id, scenario_sha256, method_profile,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.scenario_id,
                    measurement.scenario_sha256,
                    measurement.method_profile,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_field_measurement(
        self, measurement_id: str
    ) -> InterRoomFieldMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       scenario_id, scenario_sha256, method_profile,
                       measured_at_utc, payload_json
                FROM cad_interroom_field_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._field_measurement_from_row(row)

    def field_measurements_for_scenario(
        self, scenario_id: str
    ) -> tuple[InterRoomFieldMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       scenario_id, scenario_sha256, method_profile,
                       measured_at_utc, payload_json
                FROM cad_interroom_field_measurements
                WHERE scenario_id=?
                ORDER BY measured_at_utc, measurement_id
                """,
                (scenario_id,),
            ).fetchall()
        return tuple(
            self._field_measurement_from_row(row) for row in rows
        )

    def _field_measurement_from_row(
        self, row: sqlite3.Row
    ) -> InterRoomFieldMeasurement:
        measurement = InterRoomFieldMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256 != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.scenario_id != row['scenario_id']
            or measurement.scenario_sha256 != row['scenario_sha256']
            or measurement.method_profile != row['method_profile']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise IsolationAuthorityIntegrityError(
                'field measurement row disagrees with its payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Calibrations

    def save_calibration(self, record: IsolationCalibrationRecord) -> None:
        _assert_sealed(record, 'calibration_sha256', 'calibration_id')
        existing = self.get_calibration(record.calibration_id)
        if existing is not None:
            if existing.calibration_sha256 == record.calibration_sha256:
                return
            raise IsolationAuthorityConflictError(
                'calibration records are append-only'
            )
        scenario = self.get_scenario(record.scenario_id)
        if scenario is None:
            raise IsolationAuthorityIntegrityError(
                'a calibration record must reference a persisted scenario'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_isolation_calibrations (
                    calibration_id, calibration_sha256, document_id,
                    scenario_id, model_ref, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.calibration_id,
                    record.calibration_sha256,
                    record.document_id,
                    record.scenario_id,
                    record.model_ref,
                    record.model_dump_json(),
                ),
            )

    def get_calibration(
        self, calibration_id: str
    ) -> IsolationCalibrationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT calibration_id, calibration_sha256, document_id,
                       scenario_id, model_ref, payload_json
                FROM cad_isolation_calibrations
                WHERE calibration_id=?
                """,
                (calibration_id,),
            ).fetchone()
        if row is None:
            return None
        record = IsolationCalibrationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.calibration_id != row['calibration_id']
            or record.calibration_sha256 != row['calibration_sha256']
            or record.document_id != row['document_id']
            or record.scenario_id != row['scenario_id']
            or record.model_ref != row['model_ref']
        ):
            raise IsolationAuthorityIntegrityError(
                'calibration row disagrees with its payload'
            )
        return record

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: SoundIsolationQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == qualification.qualification_sha256:
                return
            raise IsolationAuthorityConflictError(
                'isolation qualifications are append-only'
            )
        scenario = self.get_scenario(qualification.scenario_id)
        if scenario is None:
            raise IsolationAuthorityIntegrityError(
                'a qualification must reference a persisted scenario'
            )
        if scenario.scenario_sha256 != qualification.scenario_sha256:
            raise IsolationAuthorityIntegrityError(
                'qualification scenario hash does not match the stored '
                'scenario'
            )
        for measurement_id in qualification.measurement_ids:
            if self.get_field_measurement(measurement_id) is None:
                raise IsolationAuthorityIntegrityError(
                    'a qualification must reference persisted field '
                    'measurements'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_isolation_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    scenario_id, scenario_sha256, lifecycle_state,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.scenario_id,
                    qualification.scenario_sha256,
                    qualification.lifecycle_state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> SoundIsolationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       scenario_id, scenario_sha256, lifecycle_state,
                       evaluated_at_utc, payload_json
                FROM cad_isolation_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = SoundIsolationQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.scenario_id != row['scenario_id']
            or qualification.scenario_sha256 != row['scenario_sha256']
            or qualification.lifecycle_state != row['lifecycle_state']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise IsolationAuthorityIntegrityError(
                'isolation qualification row disagrees with its payload'
            )
        return qualification


__all__ = [
    'CadIsolationAuthorityRepository',
    'IsolationAuthorityConflictError',
    'IsolationAuthorityIntegrityError',
]
