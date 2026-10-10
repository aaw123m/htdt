"""Append-only persistence for the wave-solver numerical-fidelity
authority (#683, REV58-NUMERIC).

Three tables:

* ``cad_wave_fidelity_profiles`` — sealed solver-formulation +
  discretization + per-error-class declarations.
* ``cad_wave_convergence_records`` — sealed refinement / fixture /
  cross-solver convergence studies.
* ``cad_wave_fidelity_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256
from ..domain.cad_wave_fidelity_authority import (
    NumericalConvergenceRecord,
    WaveFidelityQualification,
    WaveNumericalFidelityProfile,
)


class WaveFidelityConflictError(ValueError):
    """A wave-fidelity save violated append-only identity rules."""


class WaveFidelityIntegrityError(ValueError):
    """A stored wave-fidelity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise WaveFidelityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise WaveFidelityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadWaveFidelityRepository:
    """Native storage for the #683 wave-fidelity authority."""

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
                'cad_wave_fidelity_profiles',
                'cad_wave_convergence_records',
                'cad_wave_fidelity_qualifications',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(
        self, profile: WaveNumericalFidelityProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise WaveFidelityConflictError(
                'wave fidelity profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_wave_fidelity_profiles (
                    profile_id, profile_sha256, document_id,
                    solver_family, solver_result_ref_id,
                    mesh_identity, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.formulation.solver_family,
                    (
                        profile.solver_result_ref.ref_id
                        if profile.solver_result_ref is not None
                        else None
                    ),
                    profile.discretization.mesh_identity,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> WaveNumericalFidelityProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_wave_fidelity_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = WaveNumericalFidelityProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.formulation.solver_family != row['solver_family']
            or (
                profile.solver_result_ref.ref_id
                if profile.solver_result_ref is not None
                else None
            ) != row['solver_result_ref_id']
            or profile.discretization.mesh_identity
            != row['mesh_identity']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise WaveFidelityIntegrityError(
                'stored wave fidelity profile disagrees with its payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[WaveNumericalFidelityProfile, ...]:
        query = 'SELECT payload_json FROM cad_wave_fidelity_profiles'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            WaveNumericalFidelityProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Convergence records

    def save_convergence(
        self, record: NumericalConvergenceRecord
    ) -> None:
        _assert_sealed(record, 'convergence_sha256', 'convergence_id')
        existing = self.get_convergence(record.convergence_id)
        if existing is not None:
            if existing.convergence_sha256 == record.convergence_sha256:
                return
            raise WaveFidelityConflictError(
                'wave convergence records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_wave_convergence_records (
                    convergence_id, convergence_sha256, document_id,
                    profile_ref_id, study_kind, level_count,
                    fixture_count, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.convergence_id,
                    record.convergence_sha256,
                    record.document_id,
                    record.profile_ref.ref_id,
                    record.study_kind,
                    len(record.refinement_levels),
                    len(record.fixture_results),
                    record.declared_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_convergence(
        self, convergence_id: str
    ) -> NumericalConvergenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_wave_convergence_records '
                'WHERE convergence_id=?',
                (convergence_id,),
            ).fetchone()
        if row is None:
            return None
        record = NumericalConvergenceRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.convergence_id != row['convergence_id']
            or record.convergence_sha256 != row['convergence_sha256']
            or record.document_id != row['document_id']
            or record.profile_ref.ref_id != row['profile_ref_id']
            or record.study_kind != row['study_kind']
            or len(record.refinement_levels) != row['level_count']
            or len(record.fixture_results) != row['fixture_count']
            or record.declared_at_utc != row['declared_at_utc']
        ):
            raise WaveFidelityIntegrityError(
                'stored wave convergence record disagrees with its payload'
            )
        return record

    def list_convergences(
        self, document_id: str | None = None
    ) -> tuple[NumericalConvergenceRecord, ...]:
        query = 'SELECT payload_json FROM cad_wave_convergence_records'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            NumericalConvergenceRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: WaveFidelityQualification
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
            raise WaveFidelityConflictError(
                'wave fidelity qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_wave_fidelity_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, fidelity_state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.fidelity_state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> WaveFidelityQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_wave_fidelity_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = WaveFidelityQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.fidelity_state != row['fidelity_state']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise WaveFidelityIntegrityError(
                'stored wave fidelity qualification disagrees with its '
                'payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[WaveFidelityQualification, ...]:
        query = (
            'SELECT payload_json FROM cad_wave_fidelity_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            WaveFidelityQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
