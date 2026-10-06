"""Append-only persistence for the audio-interface transfer / loopback
calibration authority (#699, REV58-MEASELEC).

Three tables:

* ``cad_interface_loopback_observations`` — sealed raw loopback-capture
  evidence bound to an exact I/O path.
* ``cad_interface_transfer_calibrations`` — sealed interface-transfer
  calibration artifacts (combined path or de-embedded single side).
* ``cad_interface_correction_qualifications`` — sealed fail-closed
  applicability verdicts for a measurement path and purpose.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_interface_loopback import (
    CadInterfaceCorrectionQualification,
    CadInterfaceTransferCalibration,
    CadLoopbackObservation,
)


class InterfaceLoopbackAuthorityConflictError(ValueError):
    """An interface-loopback save violated append-only identity rules."""


class InterfaceLoopbackAuthorityIntegrityError(ValueError):
    """A stored interface-loopback row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise InterfaceLoopbackAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise InterfaceLoopbackAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadInterfaceLoopbackRepository:
    """Native storage for the #699 interface-calibration records."""

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
                'cad_interface_loopback_observations',
                'cad_interface_transfer_calibrations',
                'cad_interface_correction_qualifications',
            )

    # ------------------------------------------------------------------
    # Loopback observations

    def save_observation(self, observation: CadLoopbackObservation) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise InterfaceLoopbackAuthorityConflictError(
                'loopback observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_interface_loopback_observations (
                    observation_id, observation_sha256, document_id,
                    loopback_path_kind, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.io_path.loopback_path_kind,
                    observation.declared_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadLoopbackObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_interface_loopback_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadLoopbackObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.io_path.loopback_path_kind
            != row['loopback_path_kind']
            or observation.declared_at_utc != row['declared_at_utc']
        ):
            raise InterfaceLoopbackAuthorityIntegrityError(
                'loopback observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[CadLoopbackObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_interface_loopback_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadLoopbackObservation.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Interface-transfer calibrations

    def save_calibration(
        self, calibration: CadInterfaceTransferCalibration
    ) -> None:
        _assert_sealed(
            calibration, 'calibration_sha256', 'calibration_id'
        )
        existing = self.get_calibration(calibration.calibration_id)
        if existing is not None:
            if existing.calibration_sha256 == calibration.calibration_sha256:
                return
            raise InterfaceLoopbackAuthorityConflictError(
                'interface calibrations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_interface_transfer_calibrations (
                    calibration_id, calibration_sha256, document_id,
                    calibration_kind, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    calibration.calibration_id,
                    calibration.calibration_sha256,
                    calibration.document_id,
                    calibration.calibration_kind,
                    calibration.declared_at_utc,
                    calibration.model_dump_json(),
                ),
            )

    def get_calibration(
        self, calibration_id: str
    ) -> CadInterfaceTransferCalibration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_interface_transfer_calibrations '
                'WHERE calibration_id=?',
                (calibration_id,),
            ).fetchone()
        if row is None:
            return None
        calibration = CadInterfaceTransferCalibration.model_validate_json(
            row['payload_json']
        )
        if (
            calibration.calibration_id != row['calibration_id']
            or calibration.calibration_sha256 != row['calibration_sha256']
            or calibration.document_id != row['document_id']
            or calibration.calibration_kind != row['calibration_kind']
            or calibration.declared_at_utc != row['declared_at_utc']
        ):
            raise InterfaceLoopbackAuthorityIntegrityError(
                'interface calibration row disagrees with payload'
            )
        return calibration

    def list_calibrations(
        self, document_id: str
    ) -> tuple[CadInterfaceTransferCalibration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_interface_transfer_calibrations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadInterfaceTransferCalibration.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Correction qualifications

    def save_qualification(
        self, qualification: CadInterfaceCorrectionQualification
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
            raise InterfaceLoopbackAuthorityConflictError(
                'correction qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_interface_correction_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    calibration_ref_id, state, sample_rate_applicability,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    (
                        qualification.calibration_ref.ref_id
                        if qualification.calibration_ref is not None
                        else None
                    ),
                    qualification.state,
                    qualification.sample_rate_applicability,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadInterfaceCorrectionQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_interface_correction_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadInterfaceCorrectionQualification.model_validate_json(
                row['payload_json']
            )
        )
        cal_ref_id = (
            qualification.calibration_ref.ref_id
            if qualification.calibration_ref is not None
            else None
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or cal_ref_id != row['calibration_ref_id']
            or qualification.state != row['state']
            or qualification.sample_rate_applicability
            != row['sample_rate_applicability']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise InterfaceLoopbackAuthorityIntegrityError(
                'correction qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadInterfaceCorrectionQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_interface_correction_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadInterfaceCorrectionQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadInterfaceLoopbackRepository',
    'InterfaceLoopbackAuthorityConflictError',
    'InterfaceLoopbackAuthorityIntegrityError',
]
