"""Append-only persistence for the optimizer qualification authority
(#675, REV58-VALIDMETH).

Four tables:

* ``cad_optimization_problems`` — sealed optimization problem
  identities.
* ``cad_optimizer_run_profiles`` — sealed benchmark/qualification
  profiles.
* ``cad_optimizer_qualifications`` — sealed per-run qualification
  verdicts.
* ``cad_pareto_assessments`` — sealed Pareto-approximation
  assessments.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256
from ..domain.cad_optimizer_qualification import (
    OptimizationProblemIdentity,
    OptimizationRunQualification,
    OptimizerRunProfile,
    ParetoApproximationAssessment,
)


class OptimizerQualificationConflictError(ValueError):
    """An optimizer-qualification save violated append-only identity."""


class OptimizerQualificationIntegrityError(ValueError):
    """A stored optimizer-qualification row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise OptimizerQualificationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise OptimizerQualificationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadOptimizerQualificationRepository:
    """Native storage for the #675 optimizer-qualification records."""

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
                'cad_optimization_problems',
                'cad_optimizer_run_profiles',
                'cad_optimizer_qualifications',
                'cad_pareto_assessments',
            )

    # ------------------------------------------------------------------
    # Problems

    def save_problem(self, problem: OptimizationProblemIdentity) -> None:
        _assert_sealed(problem, 'problem_sha256', 'problem_id')
        existing = self.get_problem(problem.problem_id)
        if existing is not None:
            if existing.problem_sha256 == problem.problem_sha256:
                return
            raise OptimizerQualificationConflictError(
                'optimization problems are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_optimization_problems (
                    problem_id, problem_sha256, document_id,
                    problem_label, objective_count, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    problem.problem_id,
                    problem.problem_sha256,
                    problem.document_id,
                    problem.problem_label,
                    len(problem.objectives),
                    problem.declared_at_utc,
                    problem.model_dump_json(),
                ),
            )

    def get_problem(
        self, problem_id: str
    ) -> OptimizationProblemIdentity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_optimization_problems '
                'WHERE problem_id=?',
                (problem_id,),
            ).fetchone()
        if row is None:
            return None
        problem = OptimizationProblemIdentity.model_validate_json(
            row['payload_json']
        )
        if (
            problem.problem_id != row['problem_id']
            or problem.problem_sha256 != row['problem_sha256']
            or problem.document_id != row['document_id']
            or problem.problem_label != row['problem_label']
            or len(problem.objectives) != row['objective_count']
            or problem.declared_at_utc != row['declared_at_utc']
        ):
            raise OptimizerQualificationIntegrityError(
                'optimization problem row disagrees with payload'
            )
        return problem

    def list_problems(
        self, document_id: str
    ) -> tuple[OptimizationProblemIdentity, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_optimization_problems '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            OptimizationProblemIdentity.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Run profiles

    def save_profile(self, profile: OptimizerRunProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise OptimizerQualificationConflictError(
                'optimizer run profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_optimizer_run_profiles (
                    profile_id, profile_sha256, document_id,
                    problem_ref_id, algorithm_family, run_count,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.problem_ref.ref_id,
                    profile.algorithm.family,
                    len(profile.runs),
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> OptimizerRunProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_optimizer_run_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = OptimizerRunProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.problem_ref.ref_id != row['problem_ref_id']
            or profile.algorithm.family != row['algorithm_family']
            or len(profile.runs) != row['run_count']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise OptimizerQualificationIntegrityError(
                'optimizer run profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[OptimizerRunProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_optimizer_run_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            OptimizerRunProfile.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: OptimizationRunQualification
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
            raise OptimizerQualificationConflictError(
                'optimizer qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_optimizer_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, state, optimality_claim,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.state,
                    qualification.optimality_claim,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> OptimizationRunQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_optimizer_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = OptimizationRunQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.state != row['state']
            or qualification.optimality_claim != row['optimality_claim']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise OptimizerQualificationIntegrityError(
                'optimizer qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[OptimizationRunQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_optimizer_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            OptimizationRunQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Pareto assessments

    def save_pareto_assessment(
        self, assessment: ParetoApproximationAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_pareto_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise OptimizerQualificationConflictError(
                'pareto assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_pareto_assessments (
                    assessment_id, assessment_sha256, document_id,
                    profile_ref_id, state, reference_status,
                    nondominated_count, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.profile_ref.ref_id,
                    assessment.state,
                    assessment.reference_status,
                    assessment.nondominated_count,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_pareto_assessment(
        self, assessment_id: str
    ) -> ParetoApproximationAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_pareto_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = ParetoApproximationAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.profile_ref.ref_id != row['profile_ref_id']
            or assessment.state != row['state']
            or assessment.reference_status != row['reference_status']
            or assessment.nondominated_count
            != row['nondominated_count']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise OptimizerQualificationIntegrityError(
                'pareto assessment row disagrees with payload'
            )
        return assessment

    def list_pareto_assessments(
        self, document_id: str
    ) -> tuple[ParetoApproximationAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_pareto_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ParetoApproximationAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadOptimizerQualificationRepository',
    'OptimizerQualificationConflictError',
    'OptimizerQualificationIntegrityError',
]
