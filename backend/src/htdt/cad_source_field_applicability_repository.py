"""Append-only persistence for the source field applicability authority
(#655, REV58-AUDIOMODEL).

Two tables:

* ``cad_source_field_profiles`` — sealed measurement-distance/regime
  declarations.
* ``cad_source_field_qualifications`` — sealed applicability verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_source_field_applicability_authority import (
    SourceFieldProfile,
    SourceFieldQualification,
)


class SourceFieldConflictError(ValueError):
    """A source-field save violated append-only identity rules."""


class SourceFieldIntegrityError(ValueError):
    """A stored source-field row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SourceFieldIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SourceFieldIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSourceFieldApplicabilityRepository:
    """Native storage for the #655 field applicability authority."""

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
                'cad_source_field_profiles',
                'cad_source_field_qualifications',
            )

    # ------------------------------------------------------------------
    # Field profiles

    def save_profile(self, profile: SourceFieldProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise SourceFieldConflictError(
                'source field profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_field_profiles (
                    profile_id, profile_sha256, document_id,
                    source_ref_id, mic_distance_m, environment,
                    default_source_model, band_count, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.source_ref.ref_id,
                    profile.measurement_geometry.mic_distance_m,
                    profile.measurement_geometry.environment,
                    profile.default_source_model,
                    len(profile.band_regimes),
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> SourceFieldProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_field_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = SourceFieldProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.source_ref.ref_id != row['source_ref_id']
            or profile.measurement_geometry.mic_distance_m
            != row['mic_distance_m']
            or profile.measurement_geometry.environment
            != row['environment']
            or profile.default_source_model != row['default_source_model']
            or len(profile.band_regimes) != row['band_count']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise SourceFieldIntegrityError(
                'stored source field profile disagrees with its payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[SourceFieldProfile, ...]:
        query = 'SELECT payload_json FROM cad_source_field_profiles'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceFieldProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Field qualifications

    def save_qualification(
        self, qualification: SourceFieldQualification
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
            raise SourceFieldConflictError(
                'source field qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_field_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, requested_distance_m,
                    effective_regime, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.requested_distance_m,
                    qualification.effective_regime,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> SourceFieldQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_field_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = SourceFieldQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.requested_distance_m
            != row['requested_distance_m']
            or qualification.effective_regime != row['effective_regime']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SourceFieldIntegrityError(
                'stored source field qualification disagrees with its '
                'payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[SourceFieldQualification, ...]:
        query = (
            'SELECT payload_json FROM cad_source_field_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceFieldQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
