"""Append-only persistence for the tactile/seat-vibration authority
(#612).

Four tables:

* ``cad_tactile_vibration_paths`` — sealed ``CadTactilePath`` drive
  chains.
* ``cad_tactile_vibration_measurements`` — sealed
  ``CadTactileVibrationMeasurement`` contact-point records; the path
  must persist first.
* ``cad_tactile_profiles`` — sealed ``CadTactileProfile`` target /
  exposure profiles.
* ``cad_tactile_vibration_qualifications`` — sealed
  ``CadTactileVibrationQualification`` per-seat verdicts; the path
  must persist.

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
from .cad_tactile_vibration_authority import (
    CadTactilePath,
    CadTactileProfile,
    CadTactileVibrationMeasurement,
    CadTactileVibrationQualification,
)


class TactileVibrationConflictError(ValueError):
    """A tactile-vibration save violated append-only identity rules."""


class TactileVibrationIntegrityError(ValueError):
    """A stored tactile-vibration row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TactileVibrationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TactileVibrationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadTactileVibrationRepository:
    """Native storage for the #612 tactile-vibration records."""

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
                'cad_tactile_vibration_paths',
                'cad_tactile_vibration_measurements',
                'cad_tactile_profiles',
                'cad_tactile_vibration_qualifications',
            )

    # ------------------------------------------------------------------
    # Tactile paths

    def save_path(self, path: CadTactilePath) -> None:
        _assert_sealed(path, 'path_sha256', 'path_id')
        existing = self.get_path(path.path_id)
        if existing is not None:
            if existing.path_sha256 == path.path_sha256:
                return
            raise TactileVibrationConflictError(
                'tactile paths are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_vibration_paths (
                    path_id, path_sha256, document_id,
                    label, seat_ref, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path.path_id,
                    path.path_sha256,
                    path.document_id,
                    path.label,
                    path.seat_ref,
                    path.declared_at_utc,
                    path.model_dump_json(),
                ),
            )

    def get_path(self, path_id: str) -> CadTactilePath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_tactile_vibration_paths '
                'WHERE path_id=?',
                (path_id,),
            ).fetchone()
        if row is None:
            return None
        path = CadTactilePath.model_validate_json(row['payload_json'])
        if (
            path.path_id != row['path_id']
            or path.path_sha256 != row['path_sha256']
            or path.document_id != row['document_id']
            or path.label != row['label']
            or path.seat_ref != row['seat_ref']
            or path.declared_at_utc != row['declared_at_utc']
        ):
            raise TactileVibrationIntegrityError(
                'tactile path row disagrees with payload'
            )
        return path

    def list_paths(
        self, document_id: str
    ) -> tuple[CadTactilePath, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_tactile_vibration_paths '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTactilePath.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Vibration measurements

    def save_measurement(
        self, measurement: CadTactileVibrationMeasurement
    ) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise TactileVibrationConflictError(
                'vibration measurements are append-only'
            )
        if self.get_path(measurement.path_ref.ref_id) is None:
            raise TactileVibrationConflictError(
                'the bound tactile path must persist before '
                'measurements'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_vibration_measurements (
                    measurement_id, measurement_sha256, document_id,
                    path_ref_id, quantity, axis, contact_point,
                    occupancy_state, sensor_evidence_class,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.path_ref.ref_id,
                    measurement.quantity,
                    measurement.axis,
                    measurement.contact_point,
                    measurement.occupancy_state,
                    measurement.sensor_evidence_class,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> CadTactileVibrationMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_tactile_vibration_measurements '
                'WHERE measurement_id=?',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        measurement = (
            CadTactileVibrationMeasurement.model_validate_json(
                row['payload_json']
            )
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.path_ref.ref_id != row['path_ref_id']
            or measurement.quantity != row['quantity']
            or measurement.axis != row['axis']
            or measurement.contact_point != row['contact_point']
            or measurement.occupancy_state != row['occupancy_state']
            or measurement.sensor_evidence_class
            != row['sensor_evidence_class']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise TactileVibrationIntegrityError(
                'vibration measurement row disagrees with payload'
            )
        return measurement

    def list_measurements(
        self, document_id: str
    ) -> tuple[CadTactileVibrationMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_tactile_vibration_measurements '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTactileVibrationMeasurement.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Tactile profiles

    def save_profile(self, profile: CadTactileProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise TactileVibrationConflictError(
                'tactile profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_profiles (
                    profile_id, profile_sha256, document_id,
                    profile_kind, label, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.profile_kind,
                    profile.label,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadTactileProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_tactile_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadTactileProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.profile_kind != row['profile_kind']
            or profile.label != row['label']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise TactileVibrationIntegrityError(
                'tactile profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadTactileProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_tactile_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTactileProfile.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadTactileVibrationQualification
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
            raise TactileVibrationConflictError(
                'tactile qualifications are append-only'
            )
        if self.get_path(qualification.path_ref.ref_id) is None:
            raise TactileVibrationConflictError(
                'the bound tactile path must persist before '
                'qualifications'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_vibration_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    path_ref_id, verdict, transfer_state,
                    occupancy_state, timing_state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.path_ref.ref_id,
                    qualification.verdict,
                    qualification.transfer_state,
                    qualification.occupancy_state,
                    qualification.timing_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadTactileVibrationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_tactile_vibration_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadTactileVibrationQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.path_ref.ref_id != row['path_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.transfer_state != row['transfer_state']
            or qualification.occupancy_state != row['occupancy_state']
            or qualification.timing_state != row['timing_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise TactileVibrationIntegrityError(
                'tactile qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadTactileVibrationQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_tactile_vibration_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTactileVibrationQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadTactileVibrationRepository',
    'TactileVibrationConflictError',
    'TactileVibrationIntegrityError',
]
