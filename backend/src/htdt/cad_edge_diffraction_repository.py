"""Append-only persistence for the edge diffraction authority
(#681, REV58-AUDIOMODEL).

Three tables:

* ``cad_diffraction_model_profiles`` — sealed engine domain
  declarations.
* ``cad_diffraction_benchmark_results`` — sealed fixture outcomes.
* ``cad_diffraction_qualifications`` — sealed capability verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_edge_diffraction_authority import (
    DiffractionBenchmarkResult,
    EdgeDiffractionProfile,
    EdgeDiffractionQualification,
)


class EdgeDiffractionConflictError(ValueError):
    """A diffraction save violated append-only identity rules."""


class EdgeDiffractionIntegrityError(ValueError):
    """A stored diffraction row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise EdgeDiffractionIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise EdgeDiffractionIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadEdgeDiffractionRepository:
    """Native storage for the #681 edge diffraction authority."""

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
                'cad_diffraction_model_profiles',
                'cad_diffraction_benchmark_results',
                'cad_diffraction_qualifications',
            )

    # ------------------------------------------------------------------
    # Diffraction profiles

    def save_profile(self, profile: EdgeDiffractionProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise EdgeDiffractionConflictError(
                'edge diffraction profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diffraction_model_profiles (
                    profile_id, profile_sha256, document_id,
                    model_family, implementation, domain, edge_kind,
                    wedge_boundary, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.model_family,
                    profile.implementation,
                    profile.domain,
                    profile.edge_geometry.edge_kind,
                    profile.wedge_material.boundary,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> EdgeDiffractionProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diffraction_model_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = EdgeDiffractionProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.model_family != row['model_family']
            or profile.implementation != row['implementation']
            or profile.domain != row['domain']
            or profile.edge_geometry.edge_kind != row['edge_kind']
            or profile.wedge_material.boundary
            != row['wedge_boundary']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise EdgeDiffractionIntegrityError(
                'stored edge diffraction profile disagrees with its '
                'payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[EdgeDiffractionProfile, ...]:
        query = (
            'SELECT payload_json FROM cad_diffraction_model_profiles'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            EdgeDiffractionProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Benchmark results

    def save_benchmark_result(
        self, result: DiffractionBenchmarkResult
    ) -> None:
        _assert_sealed(result, 'result_sha256', 'result_id')
        existing = self.get_benchmark_result(result.result_id)
        if existing is not None:
            if existing.result_sha256 == result.result_sha256:
                return
            raise EdgeDiffractionConflictError(
                'diffraction benchmark results are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diffraction_benchmark_results (
                    result_id, result_sha256, document_id,
                    profile_ref_id, fixture_id, fixture_kind,
                    reference_class, result, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.result_id,
                    result.result_sha256,
                    result.document_id,
                    result.profile_ref.ref_id,
                    result.fixture_id,
                    result.fixture_kind,
                    result.reference_class,
                    result.result,
                    result.declared_at_utc,
                    result.model_dump_json(),
                ),
            )

    def get_benchmark_result(
        self, result_id: str
    ) -> DiffractionBenchmarkResult | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diffraction_benchmark_results '
                'WHERE result_id=?',
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        result = DiffractionBenchmarkResult.model_validate_json(
            row['payload_json']
        )
        if (
            result.result_id != row['result_id']
            or result.result_sha256 != row['result_sha256']
            or result.document_id != row['document_id']
            or result.profile_ref.ref_id != row['profile_ref_id']
            or result.fixture_id != row['fixture_id']
            or result.fixture_kind != row['fixture_kind']
            or result.reference_class != row['reference_class']
            or result.result != row['result']
            or result.declared_at_utc != row['declared_at_utc']
        ):
            raise EdgeDiffractionIntegrityError(
                'stored diffraction benchmark result disagrees with '
                'its payload'
            )
        return result

    def list_benchmark_results(
        self, document_id: str | None = None
    ) -> tuple[DiffractionBenchmarkResult, ...]:
        query = (
            'SELECT payload_json FROM cad_diffraction_benchmark_results'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            DiffractionBenchmarkResult.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: EdgeDiffractionQualification
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
            raise EdgeDiffractionConflictError(
                'edge diffraction qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diffraction_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, capability, boundary_limited,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.capability,
                    1 if qualification.boundary_limited else 0,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> EdgeDiffractionQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diffraction_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = EdgeDiffractionQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.capability != row['capability']
            or (1 if qualification.boundary_limited else 0)
            != row['boundary_limited']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise EdgeDiffractionIntegrityError(
                'stored edge diffraction qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[EdgeDiffractionQualification, ...]:
        query = (
            'SELECT payload_json FROM cad_diffraction_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            EdgeDiffractionQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
