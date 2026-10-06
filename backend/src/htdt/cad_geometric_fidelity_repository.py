"""Append-only persistence for the geometrical-acoustics
numerical-fidelity authority (#685, REV58-NUMERIC).

Four tables:

* ``cad_geometric_fidelity_profiles`` — sealed GA-solver declarations
  (algorithm family, truncation, visibility, launch, receiver,
  accounting).
* ``cad_ray_sampling_convergences`` — sealed per-observable sampling
  convergence studies.
* ``cad_path_enumeration_qualifications`` — sealed deterministic
  path-enumeration qualifications (the #677 'exact path' gate).
* ``cad_geometric_fidelity_qualifications`` — sealed fail-closed
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_geometric_fidelity_authority import (
    GeometricalNumericalFidelityProfile,
    GeometricFidelityQualification,
    PathEnumerationQualification,
    RaySamplingConvergence,
)


class GeometricFidelityConflictError(ValueError):
    """A geometric-fidelity save violated append-only identity rules."""


class GeometricFidelityIntegrityError(ValueError):
    """A stored geometric-fidelity row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise GeometricFidelityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise GeometricFidelityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadGeometricFidelityRepository:
    """Native storage for the #685 geometric-fidelity authority."""

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
                'cad_geometric_fidelity_profiles',
                'cad_ray_sampling_convergences',
                'cad_path_enumeration_qualifications',
                'cad_geometric_fidelity_qualifications',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(
        self, profile: GeometricalNumericalFidelityProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise GeometricFidelityConflictError(
                'geometric fidelity profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geometric_fidelity_profiles (
                    profile_id, profile_sha256, document_id,
                    algorithm_family, solver_result_ref_id,
                    receiver_model, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.algorithm.family,
                    (
                        profile.solver_result_ref.ref_id
                        if profile.solver_result_ref is not None
                        else None
                    ),
                    profile.receiver.model,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> GeometricalNumericalFidelityProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_geometric_fidelity_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = (
            GeometricalNumericalFidelityProfile.model_validate_json(
                row['payload_json']
            )
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.algorithm.family != row['algorithm_family']
            or (
                profile.solver_result_ref.ref_id
                if profile.solver_result_ref is not None
                else None
            ) != row['solver_result_ref_id']
            or profile.receiver.model != row['receiver_model']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise GeometricFidelityIntegrityError(
                'stored geometric fidelity profile disagrees with its '
                'payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[GeometricalNumericalFidelityProfile, ...]:
        query = (
            'SELECT payload_json FROM cad_geometric_fidelity_profiles'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            GeometricalNumericalFidelityProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Ray-sampling convergence records

    def save_convergence(
        self, record: RaySamplingConvergence
    ) -> None:
        _assert_sealed(record, 'convergence_sha256', 'convergence_id')
        existing = self.get_convergence(record.convergence_id)
        if existing is not None:
            if existing.convergence_sha256 == record.convergence_sha256:
                return
            raise GeometricFidelityConflictError(
                'ray sampling convergences are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ray_sampling_convergences (
                    convergence_id, convergence_sha256, document_id,
                    profile_ref_id, evidence_count, fixture_count,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.convergence_id,
                    record.convergence_sha256,
                    record.document_id,
                    record.profile_ref.ref_id,
                    len(record.evidence),
                    len(record.fixture_results),
                    record.declared_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_convergence(
        self, convergence_id: str
    ) -> RaySamplingConvergence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ray_sampling_convergences '
                'WHERE convergence_id=?',
                (convergence_id,),
            ).fetchone()
        if row is None:
            return None
        record = RaySamplingConvergence.model_validate_json(
            row['payload_json']
        )
        if (
            record.convergence_id != row['convergence_id']
            or record.convergence_sha256 != row['convergence_sha256']
            or record.document_id != row['document_id']
            or record.profile_ref.ref_id != row['profile_ref_id']
            or len(record.evidence) != row['evidence_count']
            or len(record.fixture_results) != row['fixture_count']
            or record.declared_at_utc != row['declared_at_utc']
        ):
            raise GeometricFidelityIntegrityError(
                'stored ray sampling convergence disagrees with its '
                'payload'
            )
        return record

    def list_convergences(
        self, document_id: str | None = None
    ) -> tuple[RaySamplingConvergence, ...]:
        query = (
            'SELECT payload_json FROM cad_ray_sampling_convergences'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            RaySamplingConvergence.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Path-enumeration qualifications

    def save_enumeration(
        self, qualification: PathEnumerationQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_enumeration(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise GeometricFidelityConflictError(
                'path enumeration qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_path_enumeration_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, deterministic_state,
                    named_path_evidence_class, max_qualified_order,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.deterministic_state,
                    qualification.named_path_evidence_class,
                    qualification.max_qualified_order,
                    qualification.declared_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_enumeration(
        self, qualification_id: str
    ) -> PathEnumerationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_path_enumeration_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = PathEnumerationQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.deterministic_state
            != row['deterministic_state']
            or qualification.named_path_evidence_class
            != row['named_path_evidence_class']
            or qualification.max_qualified_order
            != row['max_qualified_order']
            or qualification.declared_at_utc != row['declared_at_utc']
        ):
            raise GeometricFidelityIntegrityError(
                'stored path enumeration qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_enumerations(
        self, document_id: str | None = None
    ) -> tuple[PathEnumerationQualification, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_path_enumeration_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            PathEnumerationQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Overall qualifications

    def save_qualification(
        self, qualification: GeometricFidelityQualification
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
            raise GeometricFidelityConflictError(
                'geometric fidelity qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_geometric_fidelity_qualifications (
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
    ) -> GeometricFidelityQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_geometric_fidelity_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = GeometricFidelityQualification.model_validate_json(
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
            raise GeometricFidelityIntegrityError(
                'stored geometric fidelity qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[GeometricFidelityQualification, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_geometric_fidelity_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            GeometricFidelityQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
