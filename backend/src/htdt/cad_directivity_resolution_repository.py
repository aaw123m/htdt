"""Append-only persistence for the directivity resolution authority
(#656, REV58-AUDIOMODEL).

Three tables:

* ``cad_directivity_sampling_profiles`` — sealed measured-grid +
  interpolation declarations.
* ``cad_directivity_interpolation_records`` — sealed upsampling
  derivations.
* ``cad_directivity_direction_qualifications`` — sealed per-direction
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_directivity_resolution_authority import (
    DirectionQueryQualification,
    DirectivityInterpolationRecord,
    DirectivitySamplingProfile,
)


class DirectivityResolutionConflictError(ValueError):
    """A directivity-resolution save violated append-only rules."""


class DirectivityResolutionIntegrityError(ValueError):
    """A stored directivity-resolution row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DirectivityResolutionIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DirectivityResolutionIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDirectivityResolutionRepository:
    """Native storage for the #656 directivity resolution authority."""

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
                'cad_directivity_sampling_profiles',
                'cad_directivity_interpolation_records',
                'cad_directivity_direction_qualifications',
            )

    # ------------------------------------------------------------------
    # Sampling profiles

    def save_profile(self, profile: DirectivitySamplingProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise DirectivityResolutionConflictError(
                'directivity sampling profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_directivity_sampling_profiles (
                    profile_id, profile_sha256, document_id,
                    dataset_ref_id, coverage_class,
                    measured_direction_count, dataset_kind,
                    has_interpolation, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.dataset_ref.ref_id,
                    profile.sampling.coverage_class,
                    profile.sampling.measured_direction_count,
                    profile.dataset_kind,
                    1 if profile.interpolation is not None else 0,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> DirectivitySamplingProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_directivity_sampling_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = DirectivitySamplingProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.dataset_ref.ref_id != row['dataset_ref_id']
            or profile.sampling.coverage_class != row['coverage_class']
            or profile.sampling.measured_direction_count
            != row['measured_direction_count']
            or profile.dataset_kind != row['dataset_kind']
            or (1 if profile.interpolation is not None else 0)
            != row['has_interpolation']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise DirectivityResolutionIntegrityError(
                'stored directivity sampling profile disagrees with '
                'its payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[DirectivitySamplingProfile, ...]:
        query = (
            'SELECT payload_json FROM cad_directivity_sampling_profiles'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            DirectivitySamplingProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Interpolation records

    def save_interpolation_record(
        self, record: DirectivityInterpolationRecord
    ) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_interpolation_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise DirectivityResolutionConflictError(
                'directivity interpolation records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_directivity_interpolation_records (
                    record_id, record_sha256, document_id,
                    profile_ref_id, method, domain, output_step_deg,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.profile_ref.ref_id,
                    record.method,
                    record.domain,
                    record.output_grid.nominal_step_deg,
                    record.declared_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_interpolation_record(
        self, record_id: str
    ) -> DirectivityInterpolationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_directivity_interpolation_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = DirectivityInterpolationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.profile_ref.ref_id != row['profile_ref_id']
            or record.method != row['method']
            or record.domain != row['domain']
            or record.output_grid.nominal_step_deg
            != row['output_step_deg']
            or record.declared_at_utc != row['declared_at_utc']
        ):
            raise DirectivityResolutionIntegrityError(
                'stored directivity interpolation record disagrees '
                'with its payload'
            )
        return record

    def list_interpolation_records(
        self, document_id: str | None = None
    ) -> tuple[DirectivityInterpolationRecord, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_directivity_interpolation_records'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            DirectivityInterpolationRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Direction qualifications

    def save_qualification(
        self, qualification: DirectionQueryQualification
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
            raise DirectivityResolutionConflictError(
                'directivity direction qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_directivity_direction_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, azimuth_deg, elevation_deg,
                    frequency_hz, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.azimuth_deg,
                    qualification.elevation_deg,
                    qualification.frequency_hz,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> DirectionQueryQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_directivity_direction_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = DirectionQueryQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.azimuth_deg != row['azimuth_deg']
            or qualification.elevation_deg != row['elevation_deg']
            or qualification.frequency_hz != row['frequency_hz']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DirectivityResolutionIntegrityError(
                'stored direction query qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[DirectionQueryQualification, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_directivity_direction_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            DirectionQueryQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
