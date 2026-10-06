"""Append-only persistence for the scattering model authority
(#684, REV58-AUDIOMODEL).

Two tables:

* ``cad_scattering_model_profiles`` — sealed solver reflection-model
  declarations.
* ``cad_scattering_model_qualifications`` — sealed model verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_scattering_model_authority import (
    ScatteringModelQualification,
    SurfaceReflectionModelProfile,
)


class ScatteringModelConflictError(ValueError):
    """A scattering-model save violated append-only identity rules."""


class ScatteringModelIntegrityError(ValueError):
    """A stored scattering-model row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ScatteringModelIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ScatteringModelIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadScatteringModelRepository:
    """Native storage for the #684 scattering model authority."""

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
                'cad_scattering_model_profiles',
                'cad_scattering_model_qualifications',
            )

    # ------------------------------------------------------------------
    # Reflection model profiles

    def save_profile(
        self, profile: SurfaceReflectionModelProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise ScatteringModelConflictError(
                'surface reflection model profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_scattering_model_profiles (
                    profile_id, profile_sha256, document_id,
                    solver_model, implementation,
                    directional_redirection, incidence_domain,
                    early_late_applicability, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.solver_model,
                    profile.implementation,
                    1 if profile.directional_redirection else 0,
                    profile.incidence_domain,
                    profile.early_late_applicability,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> SurfaceReflectionModelProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_scattering_model_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = SurfaceReflectionModelProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.solver_model != row['solver_model']
            or profile.implementation != row['implementation']
            or (1 if profile.directional_redirection else 0)
            != row['directional_redirection']
            or profile.incidence_domain != row['incidence_domain']
            or profile.early_late_applicability
            != row['early_late_applicability']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise ScatteringModelIntegrityError(
                'stored surface reflection model profile disagrees '
                'with its payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[SurfaceReflectionModelProfile, ...]:
        query = (
            'SELECT payload_json FROM cad_scattering_model_profiles'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            SurfaceReflectionModelProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Model qualifications

    def save_qualification(
        self, qualification: ScatteringModelQualification
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
            raise ScatteringModelConflictError(
                'scattering model qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_scattering_model_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, verdict, effective_model,
                    requires_redirection, reflection_order,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.verdict,
                    qualification.effective_model,
                    1 if qualification.requires_redirection else 0,
                    qualification.reflection_order,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ScatteringModelQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_scattering_model_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = ScatteringModelQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.effective_model != row['effective_model']
            or (1 if qualification.requires_redirection else 0)
            != row['requires_redirection']
            or qualification.reflection_order
            != row['reflection_order']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ScatteringModelIntegrityError(
                'stored scattering model qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[ScatteringModelQualification, ...]:
        query = (
            'SELECT payload_json FROM cad_scattering_model_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            ScatteringModelQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
