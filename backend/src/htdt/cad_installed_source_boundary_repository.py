"""Append-only persistence for the installed-source boundary authority (#614).

Five tables:

* ``cad_src_meas_conditions`` — sealed ``SourceMeasurementCondition``
  records keyed by ``condition_id``.
* ``cad_src_mounting_conditions`` — sealed ``InstalledMountingCondition``
  records keyed by ``mounting_id``.
* ``cad_src_boundary_corrections`` — sealed ``SourceBoundaryCorrection``
  declarations keyed by ``correction_id``.
* ``cad_src_measurements`` — sealed ``InstalledSourceMeasurement``
  records; a measurement may only persist against a stored mounting
  whose sha matches.
* ``cad_src_boundary_qualifications`` — sealed
  ``InstalledSourceQualification`` verdicts; mounting + condition +
  correction refs must all persist with matching shas.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_installed_source_boundary import (
    InstalledMountingCondition,
    InstalledSourceMeasurement,
    InstalledSourceQualification,
    SourceBoundaryCorrection,
    SourceMeasurementCondition,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class InstalledSourceConflictError(ValueError):
    """An installed-source save violated append-only identity rules."""


class InstalledSourceIntegrityError(ValueError):
    """A stored installed-source row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise InstalledSourceIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadInstalledSourceBoundaryRepository:
    """Native storage for installed-source boundary authorities."""

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
                'cad_src_meas_conditions',
                'cad_src_mounting_conditions',
                'cad_src_boundary_corrections',
                'cad_src_measurements',
                'cad_src_boundary_qualifications',
            )

    # ------------------------------------------------------------------
    # Measurement conditions

    def save_condition(self, condition: SourceMeasurementCondition) -> None:
        _assert_sealed(condition, 'condition_sha256', 'condition_id')
        existing = self.get_condition(condition.condition_id)
        if existing is not None:
            if existing.condition_sha256 == condition.condition_sha256:
                return
            raise InstalledSourceConflictError(
                'measurement conditions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_src_meas_conditions (
                    condition_id, condition_sha256, document_id,
                    source_dataset_id, environment, evidence_class,
                    includes_installed_boundary, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    condition.condition_id,
                    condition.condition_sha256,
                    condition.document_id,
                    condition.source_dataset_id,
                    condition.environment,
                    condition.evidence_class,
                    int(condition.includes_installed_boundary),
                    condition.model_dump_json(),
                ),
            )

    def get_condition(
        self, condition_id: str
    ) -> SourceMeasurementCondition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT condition_id, condition_sha256, document_id,
                       source_dataset_id, environment, evidence_class,
                       includes_installed_boundary, payload_json
                FROM cad_src_meas_conditions
                WHERE condition_id=?
                """,
                (condition_id,),
            ).fetchone()
        if row is None:
            return None
        return self._condition_from_row(row)

    def conditions_for_dataset(
        self, source_dataset_id: str
    ) -> tuple[SourceMeasurementCondition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT condition_id, condition_sha256, document_id,
                       source_dataset_id, environment, evidence_class,
                       includes_installed_boundary, payload_json
                FROM cad_src_meas_conditions
                WHERE source_dataset_id=?
                ORDER BY condition_id
                """,
                (source_dataset_id,),
            ).fetchall()
        return tuple(self._condition_from_row(row) for row in rows)

    def _condition_from_row(
        self, row: sqlite3.Row
    ) -> SourceMeasurementCondition:
        condition = SourceMeasurementCondition.model_validate_json(
            row['payload_json']
        )
        if (
            condition.condition_id != row['condition_id']
            or condition.condition_sha256 != row['condition_sha256']
            or condition.document_id != row['document_id']
            or condition.source_dataset_id != row['source_dataset_id']
            or condition.environment != row['environment']
            or condition.evidence_class != row['evidence_class']
            or bool(condition.includes_installed_boundary)
            != bool(row['includes_installed_boundary'])
        ):
            raise InstalledSourceIntegrityError(
                'measurement condition row disagrees with its payload'
            )
        return condition

    # ------------------------------------------------------------------
    # Mounting conditions

    def save_mounting(
        self, mounting: InstalledMountingCondition
    ) -> None:
        _assert_sealed(mounting, 'mounting_sha256', 'mounting_id')
        existing = self.get_mounting(mounting.mounting_id)
        if existing is not None:
            if existing.mounting_sha256 == mounting.mounting_sha256:
                return
            raise InstalledSourceConflictError(
                'mounting conditions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_src_mounting_conditions (
                    mounting_id, mounting_sha256, document_id, source_ref,
                    kind, rear_cavity, declared_by, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mounting.mounting_id,
                    mounting.mounting_sha256,
                    mounting.document_id,
                    mounting.source_ref,
                    mounting.kind,
                    mounting.rear_cavity,
                    mounting.declared_by,
                    mounting.model_dump_json(),
                ),
            )

    def get_mounting(
        self, mounting_id: str
    ) -> InstalledMountingCondition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT mounting_id, mounting_sha256, document_id, source_ref,
                       kind, rear_cavity, declared_by, payload_json
                FROM cad_src_mounting_conditions
                WHERE mounting_id=?
                """,
                (mounting_id,),
            ).fetchone()
        if row is None:
            return None
        return self._mounting_from_row(row)

    def list_mountings(
        self, document_id: str
    ) -> tuple[InstalledMountingCondition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT mounting_id, mounting_sha256, document_id, source_ref,
                       kind, rear_cavity, declared_by, payload_json
                FROM cad_src_mounting_conditions
                WHERE document_id=?
                ORDER BY mounting_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._mounting_from_row(row) for row in rows)

    def _mounting_from_row(
        self, row: sqlite3.Row
    ) -> InstalledMountingCondition:
        mounting = InstalledMountingCondition.model_validate_json(
            row['payload_json']
        )
        if (
            mounting.mounting_id != row['mounting_id']
            or mounting.mounting_sha256 != row['mounting_sha256']
            or mounting.document_id != row['document_id']
            or mounting.source_ref != row['source_ref']
            or mounting.kind != row['kind']
            or mounting.rear_cavity != row['rear_cavity']
            or mounting.declared_by != row['declared_by']
        ):
            raise InstalledSourceIntegrityError(
                'mounting condition row disagrees with its payload'
            )
        return mounting

    # ------------------------------------------------------------------
    # Boundary corrections

    def save_correction(self, correction: SourceBoundaryCorrection) -> None:
        _assert_sealed(correction, 'correction_sha256', 'correction_id')
        existing = self.get_correction(correction.correction_id)
        if existing is not None:
            if existing.correction_sha256 == correction.correction_sha256:
                return
            raise InstalledSourceConflictError(
                'boundary corrections are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_src_boundary_corrections (
                    correction_id, correction_sha256, document_id, label,
                    kind, model_identity, model_version, domain,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correction.correction_id,
                    correction.correction_sha256,
                    correction.document_id,
                    correction.label,
                    correction.kind,
                    correction.model_identity,
                    correction.model_version,
                    correction.domain,
                    correction.model_dump_json(),
                ),
            )

    def get_correction(
        self, correction_id: str
    ) -> SourceBoundaryCorrection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT correction_id, correction_sha256, document_id, label,
                       kind, model_identity, model_version, domain,
                       payload_json
                FROM cad_src_boundary_corrections
                WHERE correction_id=?
                """,
                (correction_id,),
            ).fetchone()
        if row is None:
            return None
        correction = SourceBoundaryCorrection.model_validate_json(
            row['payload_json']
        )
        if (
            correction.correction_id != row['correction_id']
            or correction.correction_sha256 != row['correction_sha256']
            or correction.document_id != row['document_id']
            or correction.label != row['label']
            or correction.kind != row['kind']
            or correction.model_identity != row['model_identity']
            or correction.model_version != row['model_version']
            or correction.domain != row['domain']
        ):
            raise InstalledSourceIntegrityError(
                'boundary correction row disagrees with its payload'
            )
        return correction

    # ------------------------------------------------------------------
    # Installed measurements

    def save_measurement(
        self, measurement: InstalledSourceMeasurement
    ) -> None:
        _assert_sealed(measurement, 'measurement_sha256', 'measurement_id')
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise InstalledSourceConflictError(
                'installed measurements are append-only'
            )
        mounting = self.get_mounting(measurement.mounting_id)
        if mounting is None:
            raise InstalledSourceIntegrityError(
                'an installed measurement must reference a persisted mounting'
            )
        if mounting.mounting_sha256 != measurement.mounting_sha256:
            raise InstalledSourceIntegrityError(
                'installed measurement mounting hash does not match the '
                'stored mounting'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_src_measurements (
                    measurement_id, measurement_sha256, document_id,
                    mounting_id, mounting_sha256, measured_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.mounting_id,
                    measurement.mounting_sha256,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> InstalledSourceMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       mounting_id, mounting_sha256, measured_at_utc,
                       payload_json
                FROM cad_src_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def measurements_for_mounting(
        self, mounting_id: str
    ) -> tuple[InstalledSourceMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       mounting_id, mounting_sha256, measured_at_utc,
                       payload_json
                FROM cad_src_measurements
                WHERE mounting_id=?
                ORDER BY measured_at_utc, measurement_id
                """,
                (mounting_id,),
            ).fetchall()
        return tuple(self._measurement_from_row(row) for row in rows)

    def _measurement_from_row(
        self, row: sqlite3.Row
    ) -> InstalledSourceMeasurement:
        measurement = InstalledSourceMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256 != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.mounting_id != row['mounting_id']
            or measurement.mounting_sha256 != row['mounting_sha256']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise InstalledSourceIntegrityError(
                'installed measurement row disagrees with its payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: InstalledSourceQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == qualification.qualification_sha256:
                return
            raise InstalledSourceConflictError(
                'installed source qualifications are append-only'
            )
        mounting = self.get_mounting(qualification.mounting_id)
        if mounting is None:
            raise InstalledSourceIntegrityError(
                'a qualification must reference a persisted mounting'
            )
        if mounting.mounting_sha256 != qualification.mounting_sha256:
            raise InstalledSourceIntegrityError(
                'qualification mounting hash does not match the stored '
                'mounting'
            )
        if qualification.condition_id is not None:
            condition = self.get_condition(qualification.condition_id)
            if condition is None:
                raise InstalledSourceIntegrityError(
                    'a qualification must reference a persisted condition'
                )
            if condition.condition_sha256 != qualification.condition_sha256:
                raise InstalledSourceIntegrityError(
                    'qualification condition hash does not match the '
                    'stored condition'
                )
        for correction_id in qualification.correction_ids:
            if self.get_correction(correction_id) is None:
                raise InstalledSourceIntegrityError(
                    'a qualification must reference persisted corrections'
                )
        for measurement_id in qualification.installed_measurement_ids:
            if self.get_measurement(measurement_id) is None:
                raise InstalledSourceIntegrityError(
                    'a qualification must reference persisted measurements'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_src_boundary_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    source_dataset_id, mounting_id, mounting_sha256,
                    state, achieved_capability, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.source_dataset_id,
                    qualification.mounting_id,
                    qualification.mounting_sha256,
                    qualification.state,
                    qualification.achieved_capability,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> InstalledSourceQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       source_dataset_id, mounting_id, mounting_sha256,
                       state, achieved_capability, evaluated_at_utc,
                       payload_json
                FROM cad_src_boundary_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = InstalledSourceQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.source_dataset_id != row['source_dataset_id']
            or qualification.mounting_id != row['mounting_id']
            or qualification.mounting_sha256 != row['mounting_sha256']
            or qualification.state != row['state']
            or qualification.achieved_capability
            != row['achieved_capability']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise InstalledSourceIntegrityError(
                'installed source qualification row disagrees with payload'
            )
        return qualification

    def qualifications_for_source(
        self, source_dataset_id: str
    ) -> tuple[InstalledSourceQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       source_dataset_id, mounting_id, mounting_sha256,
                       state, achieved_capability, evaluated_at_utc,
                       payload_json
                FROM cad_src_boundary_qualifications
                WHERE source_dataset_id=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (source_dataset_id,),
            ).fetchall()
        result = []
        for row in rows:
            qualification = InstalledSourceQualification.model_validate_json(
                row['payload_json']
            )
            if (
                qualification.qualification_id != row['qualification_id']
                or qualification.source_dataset_id
                != row['source_dataset_id']
            ):
                raise InstalledSourceIntegrityError(
                    'installed source qualification row disagrees with '
                    'payload'
                )
            result.append(qualification)
        return tuple(result)
