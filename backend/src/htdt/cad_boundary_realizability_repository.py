"""Append-only persistence for the acoustic-impedance
physical-realizability gate (#705, REV58-DSPDECAY).

Four tables:

* ``cad_boundary_evidence_records`` — sealed pinned boundary evidence.
* ``cad_boundary_rational_fits`` — sealed rational/vector-fit artifacts.
* ``cad_td_impedance_realizations`` — sealed solver-facing TD
  realizations.
* ``cad_boundary_realizability_assessments`` — sealed per
  realization+domain verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_boundary_realizability import (
    CadBoundaryEvidenceRecord,
    CadBoundaryRationalFit,
    CadBoundaryRealizabilityAssessment,
    CadTdImpedanceRealization,
)


class BoundaryRealizabilityConflictError(ValueError):
    """A realizability save violated append-only identity rules."""


class BoundaryRealizabilityIntegrityError(ValueError):
    """A stored realizability row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise BoundaryRealizabilityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise BoundaryRealizabilityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadBoundaryRealizabilityRepository:
    """Native storage for the #705 realizability-gate records."""

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
                'cad_boundary_evidence_records',
                'cad_boundary_rational_fits',
                'cad_td_impedance_realizations',
                'cad_boundary_realizability_assessments',
            )

    # ------------------------------------------------------------------
    # Evidence records

    def save_evidence(
        self, record: CadBoundaryEvidenceRecord
    ) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_evidence(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise BoundaryRealizabilityConflictError(
                'boundary evidence records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_boundary_evidence_records (
                    record_id, record_sha256, document_id,
                    evidence_label, boundary_class, passivity_class,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.evidence_label,
                    record.boundary_class,
                    record.passivity_class,
                    record.declared_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_evidence(
        self, record_id: str
    ) -> CadBoundaryEvidenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_boundary_evidence_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = CadBoundaryEvidenceRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.evidence_label != row['evidence_label']
            or record.boundary_class != row['boundary_class']
            or record.passivity_class != row['passivity_class']
            or record.declared_at_utc != row['declared_at_utc']
        ):
            raise BoundaryRealizabilityIntegrityError(
                'boundary evidence row disagrees with payload'
            )
        return record

    def list_evidence(
        self, document_id: str
    ) -> tuple[CadBoundaryEvidenceRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_boundary_evidence_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBoundaryEvidenceRecord.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Rational fits

    def save_rational_fit(self, fit: CadBoundaryRationalFit) -> None:
        _assert_sealed(fit, 'fit_sha256', 'fit_id')
        existing = self.get_rational_fit(fit.fit_id)
        if existing is not None:
            if existing.fit_sha256 == fit.fit_sha256:
                return
            raise BoundaryRealizabilityConflictError(
                'boundary rational fits are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_boundary_rational_fits (
                    fit_id, fit_sha256, document_id,
                    input_evidence_ref_id, fit_variable, pole_count,
                    algorithm, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fit.fit_id,
                    fit.fit_sha256,
                    fit.document_id,
                    fit.input_evidence_ref.ref_id,
                    fit.fit_variable,
                    fit.pole_count,
                    fit.algorithm,
                    fit.declared_at_utc,
                    fit.model_dump_json(),
                ),
            )

    def get_rational_fit(
        self, fit_id: str
    ) -> CadBoundaryRationalFit | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_boundary_rational_fits WHERE fit_id=?',
                (fit_id,),
            ).fetchone()
        if row is None:
            return None
        fit = CadBoundaryRationalFit.model_validate_json(
            row['payload_json']
        )
        if (
            fit.fit_id != row['fit_id']
            or fit.fit_sha256 != row['fit_sha256']
            or fit.document_id != row['document_id']
            or fit.input_evidence_ref.ref_id
            != row['input_evidence_ref_id']
            or fit.fit_variable != row['fit_variable']
            or fit.pole_count != row['pole_count']
            or fit.algorithm != row['algorithm']
            or fit.declared_at_utc != row['declared_at_utc']
        ):
            raise BoundaryRealizabilityIntegrityError(
                'boundary rational fit row disagrees with payload'
            )
        return fit

    def list_rational_fits(
        self, document_id: str
    ) -> tuple[CadBoundaryRationalFit, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_boundary_rational_fits '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBoundaryRationalFit.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # TD realizations

    def save_td_realization(
        self, realization: CadTdImpedanceRealization
    ) -> None:
        _assert_sealed(
            realization, 'realization_sha256', 'realization_id'
        )
        existing = self.get_td_realization(realization.realization_id)
        if existing is not None:
            if existing.realization_sha256 == (
                realization.realization_sha256
            ):
                return
            raise BoundaryRealizabilityConflictError(
                'td impedance realizations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_td_impedance_realizations (
                    realization_id, realization_sha256, document_id,
                    evidence_ref_id, solver_family, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    realization.realization_id,
                    realization.realization_sha256,
                    realization.document_id,
                    realization.evidence_ref.ref_id,
                    realization.solver_family,
                    realization.declared_at_utc,
                    realization.model_dump_json(),
                ),
            )

    def get_td_realization(
        self, realization_id: str
    ) -> CadTdImpedanceRealization | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_td_impedance_realizations '
                'WHERE realization_id=?',
                (realization_id,),
            ).fetchone()
        if row is None:
            return None
        realization = CadTdImpedanceRealization.model_validate_json(
            row['payload_json']
        )
        if (
            realization.realization_id != row['realization_id']
            or realization.realization_sha256
            != row['realization_sha256']
            or realization.document_id != row['document_id']
            or realization.evidence_ref.ref_id
            != row['evidence_ref_id']
            or realization.solver_family != row['solver_family']
            or realization.declared_at_utc != row['declared_at_utc']
        ):
            raise BoundaryRealizabilityIntegrityError(
                'td realization row disagrees with payload'
            )
        return realization

    def list_td_realizations(
        self, document_id: str
    ) -> tuple[CadTdImpedanceRealization, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_td_impedance_realizations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTdImpedanceRealization.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Assessments

    def save_assessment(
        self, assessment: CadBoundaryRealizabilityAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == (
                assessment.assessment_sha256
            ):
                return
            raise BoundaryRealizabilityConflictError(
                'boundary realizability assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_boundary_realizability_assessments (
                    assessment_id, assessment_sha256, document_id,
                    evidence_ref_id, state, passivity_class,
                    causality_state, stability_state,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.evidence_ref.ref_id,
                    assessment.state,
                    assessment.passivity_class,
                    assessment.causality_state,
                    assessment.stability_state,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadBoundaryRealizabilityAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_boundary_realizability_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = (
            CadBoundaryRealizabilityAssessment.model_validate_json(
                row['payload_json']
            )
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256
            != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.evidence_ref.ref_id != row['evidence_ref_id']
            or assessment.state != row['state']
            or assessment.passivity_class != row['passivity_class']
            or assessment.causality_state != row['causality_state']
            or assessment.stability_state != row['stability_state']
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise BoundaryRealizabilityIntegrityError(
                'boundary assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[CadBoundaryRealizabilityAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_boundary_realizability_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadBoundaryRealizabilityAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'BoundaryRealizabilityConflictError',
    'BoundaryRealizabilityIntegrityError',
    'CadBoundaryRealizabilityRepository',
]
