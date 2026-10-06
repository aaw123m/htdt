"""Append-only persistence for the room-acoustic excitation-source
authority (#668, REV58-MEASCHAIN).

Three tables:

* ``cad_excitation_source_profiles`` — sealed excitation-source
  profiles.
* ``cad_source_orientation_captures`` — sealed immutable
  per-orientation captures.
* ``cad_measurement_source_qualifications`` — sealed fail-closed
  source-eligibility verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_excitation_source import (
    CadExcitationSourceProfile,
    CadMeasurementSourceQualification,
    CadSourceOrientationCapture,
)


class ExcitationAuthorityConflictError(ValueError):
    """An excitation-authority save violated append-only rules."""


class ExcitationAuthorityIntegrityError(ValueError):
    """A stored excitation row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ExcitationAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ExcitationAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadExcitationSourceRepository:
    """Native storage for the #668 excitation-source authority records."""

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
                'cad_excitation_source_profiles',
                'cad_source_orientation_captures',
                'cad_measurement_source_qualifications',
            )

    # ------------------------------------------------------------------
    # Source profiles

    def save_profile(self, profile: CadExcitationSourceProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise ExcitationAuthorityConflictError(
                'excitation source profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_excitation_source_profiles (
                    profile_id, profile_sha256, document_id,
                    source_label, source_type, measurand_class,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.source_label,
                    profile.source_type,
                    profile.measurand_class,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadExcitationSourceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_excitation_source_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadExcitationSourceProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.source_label != row['source_label']
            or profile.source_type != row['source_type']
            or profile.measurand_class != row['measurand_class']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise ExcitationAuthorityIntegrityError(
                'excitation profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadExcitationSourceProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_excitation_source_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadExcitationSourceProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Orientation captures

    def save_capture(
        self, capture: CadSourceOrientationCapture
    ) -> None:
        _assert_sealed(capture, 'capture_sha256', 'capture_id')
        existing = self.get_capture(capture.capture_id)
        if existing is not None:
            if existing.capture_sha256 == capture.capture_sha256:
                return
            raise ExcitationAuthorityConflictError(
                'source orientation captures are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_orientation_captures (
                    capture_id, capture_sha256, document_id,
                    source_ref_id, aggregation_role,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture.capture_id,
                    capture.capture_sha256,
                    capture.document_id,
                    capture.source_ref.ref_id,
                    capture.aggregation_role,
                    capture.declared_at_utc,
                    capture.model_dump_json(),
                ),
            )

    def get_capture(
        self, capture_id: str
    ) -> CadSourceOrientationCapture | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_orientation_captures '
                'WHERE capture_id=?',
                (capture_id,),
            ).fetchone()
        if row is None:
            return None
        capture = CadSourceOrientationCapture.model_validate_json(
            row['payload_json']
        )
        if (
            capture.capture_id != row['capture_id']
            or capture.capture_sha256 != row['capture_sha256']
            or capture.document_id != row['document_id']
            or capture.source_ref.ref_id != row['source_ref_id']
            or capture.aggregation_role != row['aggregation_role']
            or capture.declared_at_utc != row['declared_at_utc']
        ):
            raise ExcitationAuthorityIntegrityError(
                'orientation capture row disagrees with payload'
            )
        return capture

    def list_captures(
        self, document_id: str
    ) -> tuple[CadSourceOrientationCapture, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_source_orientation_captures '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSourceOrientationCapture.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Source qualifications

    def save_qualification(
        self, qualification: CadMeasurementSourceQualification
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
            raise ExcitationAuthorityConflictError(
                'source qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_source_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    source_ref_id, strength_g_gate, level_gate,
                    sim_comparison, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.source_ref.ref_id,
                    qualification.strength_g_gate,
                    qualification.level_gate,
                    qualification.sim_comparison,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadMeasurementSourceQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_measurement_source_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadMeasurementSourceQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.source_ref.ref_id != row['source_ref_id']
            or qualification.strength_g_gate != row['strength_g_gate']
            or qualification.level_gate != row['level_gate']
            or qualification.sim_comparison != row['sim_comparison']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ExcitationAuthorityIntegrityError(
                'source qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadMeasurementSourceQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_measurement_source_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMeasurementSourceQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadExcitationSourceRepository',
    'ExcitationAuthorityConflictError',
    'ExcitationAuthorityIntegrityError',
]
