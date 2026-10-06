"""Append-only persistence for the validation sample-dependence /
benchmark-leakage authority (#698, REV58-IDENT).

Five tables:

* ``cad_validation_statistical_designs`` — sealed statistical designs.
* ``cad_dependence_models`` — sealed declared dependence structures.
* ``cad_dataset_role_assignments`` — sealed corpus epistemic roles.
* ``cad_benchmark_exposures`` — append-only holdout-exposure ledger.
* ``cad_challenge_qualifications`` — sealed validation-claim verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_validation_statistics import (
    CadBenchmarkExposureRecord,
    CadChallengeQualification,
    CadDatasetRoleAssignment,
    CadDependenceModel,
    CadValidationStatisticalDesign,
)


class ValidationStatisticsConflictError(ValueError):
    """A validation-statistics save violated append-only identity rules."""


class ValidationStatisticsIntegrityError(ValueError):
    """A stored validation-statistics row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ValidationStatisticsIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ValidationStatisticsIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadValidationStatisticsRepository:
    """Native storage for the #698 validation-statistics records."""

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
                'cad_validation_statistical_designs',
                'cad_dependence_models',
                'cad_dataset_role_assignments',
                'cad_benchmark_exposures',
                'cad_challenge_qualifications',
            )

    # ------------------------------------------------------------------
    # Statistical designs

    def save_design(
        self, design: CadValidationStatisticalDesign
    ) -> None:
        _assert_sealed(design, 'design_sha256', 'design_id')
        existing = self.get_design(design.design_id)
        if existing is not None:
            if existing.design_sha256 == design.design_sha256:
                return
            raise ValidationStatisticsConflictError(
                'statistical designs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_validation_statistical_designs (
                    design_id, design_sha256, document_id,
                    design_label, generalization_claim,
                    independent_unit, independent_unit_count,
                    raw_observation_count, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    design.design_id,
                    design.design_sha256,
                    design.document_id,
                    design.design_label,
                    design.generalization_claim,
                    design.independent_unit,
                    design.independent_unit_count,
                    design.raw_observation_count,
                    design.declared_at_utc,
                    design.model_dump_json(),
                ),
            )

    def get_design(
        self, design_id: str
    ) -> CadValidationStatisticalDesign | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_validation_statistical_designs '
                'WHERE design_id=?',
                (design_id,),
            ).fetchone()
        if row is None:
            return None
        design = CadValidationStatisticalDesign.model_validate_json(
            row['payload_json']
        )
        if (
            design.design_id != row['design_id']
            or design.design_sha256 != row['design_sha256']
            or design.document_id != row['document_id']
            or design.design_label != row['design_label']
            or design.generalization_claim
            != row['generalization_claim']
            or design.independent_unit != row['independent_unit']
            or design.independent_unit_count
            != row['independent_unit_count']
            or design.raw_observation_count
            != row['raw_observation_count']
            or design.declared_at_utc != row['declared_at_utc']
        ):
            raise ValidationStatisticsIntegrityError(
                'statistical design row disagrees with payload'
            )
        return design

    def list_designs(
        self, document_id: str
    ) -> tuple[CadValidationStatisticalDesign, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_validation_statistical_designs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadValidationStatisticalDesign.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Dependence models

    def save_dependence(self, model: CadDependenceModel) -> None:
        _assert_sealed(model, 'dependence_sha256', 'dependence_id')
        existing = self.get_dependence(model.dependence_id)
        if existing is not None:
            if existing.dependence_sha256 == model.dependence_sha256:
                return
            raise ValidationStatisticsConflictError(
                'dependence models are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dependence_models (
                    dependence_id, dependence_sha256, document_id,
                    design_ref_id, spatial_correlation_model,
                    resampling_unit, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model.dependence_id,
                    model.dependence_sha256,
                    model.document_id,
                    (
                        model.design_ref.ref_id
                        if model.design_ref is not None
                        else None
                    ),
                    model.spatial_correlation_model,
                    model.resampling_unit,
                    model.declared_at_utc,
                    model.model_dump_json(),
                ),
            )

    def get_dependence(
        self, dependence_id: str
    ) -> CadDependenceModel | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dependence_models '
                'WHERE dependence_id=?',
                (dependence_id,),
            ).fetchone()
        if row is None:
            return None
        model = CadDependenceModel.model_validate_json(
            row['payload_json']
        )
        design_ref_id = (
            model.design_ref.ref_id if model.design_ref is not None
            else None
        )
        if (
            model.dependence_id != row['dependence_id']
            or model.dependence_sha256 != row['dependence_sha256']
            or model.document_id != row['document_id']
            or design_ref_id != row['design_ref_id']
            or model.spatial_correlation_model
            != row['spatial_correlation_model']
            or model.resampling_unit != row['resampling_unit']
            or model.declared_at_utc != row['declared_at_utc']
        ):
            raise ValidationStatisticsIntegrityError(
                'dependence model row disagrees with payload'
            )
        return model

    def list_dependence_models(
        self, document_id: str
    ) -> tuple[CadDependenceModel, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dependence_models '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDependenceModel.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Dataset role assignments

    def save_role_assignment(
        self, assignment: CadDatasetRoleAssignment
    ) -> None:
        _assert_sealed(
            assignment, 'assignment_sha256', 'assignment_id'
        )
        existing = self.get_role_assignment(assignment.assignment_id)
        if existing is not None:
            if existing.assignment_sha256 == assignment.assignment_sha256:
                return
            raise ValidationStatisticsConflictError(
                'dataset role assignments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dataset_role_assignments (
                    assignment_id, assignment_sha256, document_id,
                    corpus_ref_id, role, context_label,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment.assignment_id,
                    assignment.assignment_sha256,
                    assignment.document_id,
                    assignment.corpus_ref.ref_id,
                    assignment.role,
                    assignment.context_label,
                    assignment.declared_at_utc,
                    assignment.model_dump_json(),
                ),
            )

    def get_role_assignment(
        self, assignment_id: str
    ) -> CadDatasetRoleAssignment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dataset_role_assignments '
                'WHERE assignment_id=?',
                (assignment_id,),
            ).fetchone()
        if row is None:
            return None
        assignment = CadDatasetRoleAssignment.model_validate_json(
            row['payload_json']
        )
        if (
            assignment.assignment_id != row['assignment_id']
            or assignment.assignment_sha256 != row['assignment_sha256']
            or assignment.document_id != row['document_id']
            or assignment.corpus_ref.ref_id != row['corpus_ref_id']
            or assignment.role != row['role']
            or assignment.context_label != row['context_label']
            or assignment.declared_at_utc != row['declared_at_utc']
        ):
            raise ValidationStatisticsIntegrityError(
                'dataset role row disagrees with payload'
            )
        return assignment

    def list_role_assignments(
        self, document_id: str
    ) -> tuple[CadDatasetRoleAssignment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dataset_role_assignments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDatasetRoleAssignment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Benchmark exposure ledger

    def save_exposure(
        self, exposure: CadBenchmarkExposureRecord
    ) -> None:
        _assert_sealed(exposure, 'exposure_sha256', 'exposure_id')
        existing = self.get_exposure(exposure.exposure_id)
        if existing is not None:
            if existing.exposure_sha256 == exposure.exposure_sha256:
                return
            raise ValidationStatisticsConflictError(
                'benchmark exposures are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_benchmark_exposures (
                    exposure_id, exposure_sha256, document_id,
                    corpus_ref_id, decision_class, solver_version,
                    exposed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exposure.exposure_id,
                    exposure.exposure_sha256,
                    exposure.document_id,
                    exposure.corpus_ref.ref_id,
                    exposure.decision_class,
                    exposure.solver_version,
                    exposure.exposed_at_utc,
                    exposure.model_dump_json(),
                ),
            )

    def get_exposure(
        self, exposure_id: str
    ) -> CadBenchmarkExposureRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_benchmark_exposures '
                'WHERE exposure_id=?',
                (exposure_id,),
            ).fetchone()
        if row is None:
            return None
        exposure = CadBenchmarkExposureRecord.model_validate_json(
            row['payload_json']
        )
        if (
            exposure.exposure_id != row['exposure_id']
            or exposure.exposure_sha256 != row['exposure_sha256']
            or exposure.document_id != row['document_id']
            or exposure.corpus_ref.ref_id != row['corpus_ref_id']
            or exposure.decision_class != row['decision_class']
            or exposure.solver_version != row['solver_version']
            or exposure.exposed_at_utc != row['exposed_at_utc']
        ):
            raise ValidationStatisticsIntegrityError(
                'benchmark exposure row disagrees with payload'
            )
        return exposure

    def list_exposures(
        self, document_id: str
    ) -> tuple[CadBenchmarkExposureRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_benchmark_exposures '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBenchmarkExposureRecord.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Challenge qualifications

    def save_qualification(
        self, qualification: CadChallengeQualification
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
            raise ValidationStatisticsConflictError(
                'challenge qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_challenge_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    design_ref_id, state, independent_unit,
                    independent_unit_count, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.design_ref.ref_id,
                    qualification.state,
                    qualification.independent_unit,
                    qualification.independent_unit_count,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadChallengeQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_challenge_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadChallengeQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.design_ref.ref_id != row['design_ref_id']
            or qualification.state != row['state']
            or qualification.independent_unit
            != row['independent_unit']
            or qualification.independent_unit_count
            != row['independent_unit_count']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ValidationStatisticsIntegrityError(
                'challenge qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadChallengeQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_challenge_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadChallengeQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadValidationStatisticsRepository',
    'ValidationStatisticsConflictError',
    'ValidationStatisticsIntegrityError',
]
