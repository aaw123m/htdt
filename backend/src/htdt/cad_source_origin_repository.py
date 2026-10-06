"""Append-only persistence for the acoustic-reference origin authority
(#654, REV58-AUDIOMODEL).

Two tables:

* ``cad_source_origin_profiles`` — sealed origin/phase-center declarations.
* ``cad_source_origin_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_source_origin_authority import (
    SourceOriginQualification,
    SourceReferenceOriginProfile,
)


class SourceOriginConflictError(ValueError):
    """A source-origin save violated append-only identity rules."""


class SourceOriginIntegrityError(ValueError):
    """A stored source-origin row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise SourceOriginIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise SourceOriginIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadSourceOriginRepository:
    """Native storage for the #654 acoustic-reference origin authority."""

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
                'cad_source_origin_profiles',
                'cad_source_origin_qualifications',
            )

    # ------------------------------------------------------------------
    # Origin profiles

    def save_profile(
        self, profile: SourceReferenceOriginProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise SourceOriginConflictError(
                'source origin profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_origin_profiles (
                    profile_id, profile_sha256, document_id,
                    source_ref_id, capability, boundary_state,
                    estimate_count, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.source_ref.ref_id,
                    profile.capability,
                    profile.boundary_state,
                    len(profile.acoustic_center_estimates),
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> SourceReferenceOriginProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_origin_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = SourceReferenceOriginProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.source_ref.ref_id != row['source_ref_id']
            or profile.capability != row['capability']
            or profile.boundary_state != row['boundary_state']
            or len(profile.acoustic_center_estimates)
            != row['estimate_count']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise SourceOriginIntegrityError(
                'stored source origin profile disagrees with its payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[SourceReferenceOriginProfile, ...]:
        query = 'SELECT payload_json FROM cad_source_origin_profiles'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceReferenceOriginProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Origin qualifications

    def save_qualification(
        self, qualification: SourceOriginQualification
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
            raise SourceOriginConflictError(
                'source origin qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_source_origin_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, requested_capability,
                    effective_origin_kind, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.requested_capability,
                    qualification.effective_origin_kind,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> SourceOriginQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_source_origin_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = SourceOriginQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.requested_capability
            != row['requested_capability']
            or qualification.effective_origin_kind
            != row['effective_origin_kind']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise SourceOriginIntegrityError(
                'stored source origin qualification disagrees with its '
                'payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[SourceOriginQualification, ...]:
        query = (
            'SELECT payload_json FROM cad_source_origin_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SourceOriginQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
