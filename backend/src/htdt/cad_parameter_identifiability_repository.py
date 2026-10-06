"""Append-only persistence for the calibration-parameter identifiability
authority (#689, REV58-IDENT).

Five tables:

* ``cad_calib_parameter_records`` — sealed calibration parameter
  records (role, provenance, bounds, constraining domain).
* ``cad_ident_sensitivity_evidence`` — sealed sensitivity/Jacobian
  evidence.
* ``cad_ident_correlation_evidence`` — sealed correlation / trade-off
  evidence.
* ``cad_ident_equivalent_sets`` — sealed equivalent-solution sets.
* ``cad_identifiability_assessments`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_parameter_identifiability import (
    CadCalibrationParameter,
    CadEquivalentSolutionSet,
    CadIdentifiabilityAssessment,
    CadParameterCorrelationEvidence,
    CadSensitivityEvidence,
)


class IdentifiabilityConflictError(ValueError):
    """An identifiability save violated append-only identity rules."""


class IdentifiabilityIntegrityError(ValueError):
    """A stored identifiability row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise IdentifiabilityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise IdentifiabilityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadParameterIdentifiabilityRepository:
    """Native storage for the #689 identifiability authority records."""

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
                'cad_calib_parameter_records',
                'cad_ident_sensitivity_evidence',
                'cad_ident_correlation_evidence',
                'cad_ident_equivalent_sets',
                'cad_identifiability_assessments',
            )

    # ------------------------------------------------------------------
    # Calibration parameters

    def save_parameter(self, parameter: CadCalibrationParameter) -> None:
        _assert_sealed(parameter, 'parameter_sha256', 'parameter_id')
        existing = self.get_parameter(parameter.parameter_id)
        if existing is not None:
            if existing.parameter_sha256 == parameter.parameter_sha256:
                return
            raise IdentifiabilityConflictError(
                'calibration parameters are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_calib_parameter_records (
                    parameter_id, parameter_sha256, document_id,
                    parameter_label, role, provenance,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parameter.parameter_id,
                    parameter.parameter_sha256,
                    parameter.document_id,
                    parameter.parameter_label,
                    parameter.role,
                    parameter.provenance,
                    parameter.declared_at_utc,
                    parameter.model_dump_json(),
                ),
            )

    def get_parameter(
        self, parameter_id: str
    ) -> CadCalibrationParameter | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_calib_parameter_records '
                'WHERE parameter_id=?',
                (parameter_id,),
            ).fetchone()
        if row is None:
            return None
        parameter = CadCalibrationParameter.model_validate_json(
            row['payload_json']
        )
        if (
            parameter.parameter_id != row['parameter_id']
            or parameter.parameter_sha256 != row['parameter_sha256']
            or parameter.document_id != row['document_id']
            or parameter.parameter_label != row['parameter_label']
            or parameter.role != row['role']
            or parameter.provenance != row['provenance']
            or parameter.declared_at_utc != row['declared_at_utc']
        ):
            raise IdentifiabilityIntegrityError(
                'calibration parameter row disagrees with payload'
            )
        return parameter

    def list_parameters(
        self, document_id: str
    ) -> tuple[CadCalibrationParameter, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_calib_parameter_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadCalibrationParameter.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Sensitivity evidence

    def save_sensitivity(
        self, evidence: CadSensitivityEvidence
    ) -> None:
        _assert_sealed(evidence, 'sensitivity_sha256', 'sensitivity_id')
        existing = self.get_sensitivity(evidence.sensitivity_id)
        if existing is not None:
            if existing.sensitivity_sha256 == evidence.sensitivity_sha256:
                return
            raise IdentifiabilityConflictError(
                'sensitivity evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ident_sensitivity_evidence (
                    sensitivity_id, sensitivity_sha256, document_id,
                    method, calibration_run_ref_id,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.sensitivity_id,
                    evidence.sensitivity_sha256,
                    evidence.document_id,
                    evidence.method,
                    (
                        evidence.calibration_run_ref.ref_id
                        if evidence.calibration_run_ref is not None
                        else None
                    ),
                    evidence.declared_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_sensitivity(
        self, sensitivity_id: str
    ) -> CadSensitivityEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ident_sensitivity_evidence '
                'WHERE sensitivity_id=?',
                (sensitivity_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = CadSensitivityEvidence.model_validate_json(
            row['payload_json']
        )
        run_ref_id = (
            evidence.calibration_run_ref.ref_id
            if evidence.calibration_run_ref is not None
            else None
        )
        if (
            evidence.sensitivity_id != row['sensitivity_id']
            or evidence.sensitivity_sha256 != row['sensitivity_sha256']
            or evidence.document_id != row['document_id']
            or evidence.method != row['method']
            or run_ref_id != row['calibration_run_ref_id']
            or evidence.declared_at_utc != row['declared_at_utc']
        ):
            raise IdentifiabilityIntegrityError(
                'sensitivity evidence row disagrees with payload'
            )
        return evidence

    def list_sensitivity_evidence(
        self, document_id: str
    ) -> tuple[CadSensitivityEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ident_sensitivity_evidence '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadSensitivityEvidence.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Correlation evidence

    def save_correlation(
        self, evidence: CadParameterCorrelationEvidence
    ) -> None:
        _assert_sealed(evidence, 'correlation_sha256', 'correlation_id')
        existing = self.get_correlation(evidence.correlation_id)
        if existing is not None:
            if existing.correlation_sha256 == evidence.correlation_sha256:
                return
            raise IdentifiabilityConflictError(
                'correlation evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ident_correlation_evidence (
                    correlation_id, correlation_sha256, document_id,
                    method, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.correlation_id,
                    evidence.correlation_sha256,
                    evidence.document_id,
                    evidence.method,
                    evidence.declared_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_correlation(
        self, correlation_id: str
    ) -> CadParameterCorrelationEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ident_correlation_evidence '
                'WHERE correlation_id=?',
                (correlation_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = CadParameterCorrelationEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.correlation_id != row['correlation_id']
            or evidence.correlation_sha256 != row['correlation_sha256']
            or evidence.document_id != row['document_id']
            or evidence.method != row['method']
            or evidence.declared_at_utc != row['declared_at_utc']
        ):
            raise IdentifiabilityIntegrityError(
                'correlation evidence row disagrees with payload'
            )
        return evidence

    def list_correlation_evidence(
        self, document_id: str
    ) -> tuple[CadParameterCorrelationEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ident_correlation_evidence '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadParameterCorrelationEvidence.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Equivalent solution sets

    def save_equivalent_set(
        self, solution_set: CadEquivalentSolutionSet
    ) -> None:
        _assert_sealed(solution_set, 'set_sha256', 'set_id')
        existing = self.get_equivalent_set(solution_set.set_id)
        if existing is not None:
            if existing.set_sha256 == solution_set.set_sha256:
                return
            raise IdentifiabilityConflictError(
                'equivalent solution sets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ident_equivalent_sets (
                    set_id, set_sha256, document_id,
                    member_count, multimodal,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    solution_set.set_id,
                    solution_set.set_sha256,
                    solution_set.document_id,
                    len(solution_set.members),
                    int(solution_set.multimodal),
                    solution_set.declared_at_utc,
                    solution_set.model_dump_json(),
                ),
            )

    def get_equivalent_set(
        self, set_id: str
    ) -> CadEquivalentSolutionSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_ident_equivalent_sets WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        solution_set = CadEquivalentSolutionSet.model_validate_json(
            row['payload_json']
        )
        if (
            solution_set.set_id != row['set_id']
            or solution_set.set_sha256 != row['set_sha256']
            or solution_set.document_id != row['document_id']
            or len(solution_set.members) != row['member_count']
            or int(solution_set.multimodal) != row['multimodal']
            or solution_set.declared_at_utc != row['declared_at_utc']
        ):
            raise IdentifiabilityIntegrityError(
                'equivalent solution set row disagrees with payload'
            )
        return solution_set

    def list_equivalent_sets(
        self, document_id: str
    ) -> tuple[CadEquivalentSolutionSet, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_ident_equivalent_sets '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEquivalentSolutionSet.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Assessments

    def save_assessment(
        self, assessment: CadIdentifiabilityAssessment
    ) -> None:
        _assert_sealed(assessment, 'assessment_sha256', 'assessment_id')
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise IdentifiabilityConflictError(
                'identifiability assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_identifiability_assessments (
                    assessment_id, assessment_sha256, document_id,
                    parameter_ref_id, identifiability_class,
                    parameter_claim, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.parameter_ref.ref_id,
                    assessment.identifiability_class,
                    assessment.parameter_claim,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadIdentifiabilityAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_identifiability_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = CadIdentifiabilityAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.parameter_ref.ref_id != row['parameter_ref_id']
            or assessment.identifiability_class
            != row['identifiability_class']
            or assessment.parameter_claim != row['parameter_claim']
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise IdentifiabilityIntegrityError(
                'identifiability assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[CadIdentifiabilityAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_identifiability_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadIdentifiabilityAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadParameterIdentifiabilityRepository',
    'IdentifiabilityConflictError',
    'IdentifiabilityIntegrityError',
]
