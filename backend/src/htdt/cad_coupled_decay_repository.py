"""Append-only persistence for the coupled-decay authority (#671,
REV58-VALIDMETH).

Three tables:

* ``cad_multi_slope_fits`` — sealed multi-component decay fits.
* ``cad_single_slope_assessments`` — sealed per-position adequacy gate
  verdicts.
* ``cad_coupled_decay_qualifications`` — sealed inter-region decay
  qualifications.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_coupled_decay import (
    InterRegionEnergyDecayQualification,
    MultiSlopeDecayFit,
    SingleSlopeAdequacyAssessment,
)


class CoupledDecayConflictError(ValueError):
    """A coupled-decay save violated append-only identity rules."""


class CoupledDecayIntegrityError(ValueError):
    """A stored coupled-decay row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CoupledDecayIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CoupledDecayIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadCoupledDecayRepository:
    """Native storage for the #671 coupled-decay authority records."""

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
                'cad_multi_slope_fits',
                'cad_single_slope_assessments',
                'cad_coupled_decay_qualifications',
            )

    def save_fit(self, fit: MultiSlopeDecayFit) -> None:
        _assert_sealed(fit, 'fit_sha256', 'fit_id')
        existing = self.get_fit(fit.fit_id)
        if existing is not None:
            if existing.fit_sha256 == fit.fit_sha256:
                return
            raise CoupledDecayConflictError(
                'multi-slope fits are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_multi_slope_fits (
                    fit_id, fit_sha256, document_id,
                    raw_evidence_ref_id, model_class, component_count,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fit.fit_id,
                    fit.fit_sha256,
                    fit.document_id,
                    fit.raw_evidence_ref.ref_id,
                    fit.model_class,
                    len(fit.components),
                    fit.declared_at_utc,
                    fit.model_dump_json(),
                ),
            )

    def get_fit(self, fit_id: str) -> MultiSlopeDecayFit | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_multi_slope_fits WHERE fit_id=?',
                (fit_id,),
            ).fetchone()
        if row is None:
            return None
        fit = MultiSlopeDecayFit.model_validate_json(row['payload_json'])
        if (
            fit.fit_id != row['fit_id']
            or fit.fit_sha256 != row['fit_sha256']
            or fit.document_id != row['document_id']
            or fit.raw_evidence_ref.ref_id != row['raw_evidence_ref_id']
            or fit.model_class != row['model_class']
            or len(fit.components) != row['component_count']
            or fit.declared_at_utc != row['declared_at_utc']
        ):
            raise CoupledDecayIntegrityError(
                'multi-slope fit row disagrees with payload'
            )
        return fit

    def list_fits(
        self, document_id: str
    ) -> tuple[MultiSlopeDecayFit, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_multi_slope_fits '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            MultiSlopeDecayFit.model_validate_json(r['payload_json'])
            for r in rows
        )

    def save_assessment(
        self, assessment: SingleSlopeAdequacyAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise CoupledDecayConflictError(
                'single-slope assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_single_slope_assessments (
                    assessment_id, assessment_sha256, document_id,
                    raw_evidence_ref_id, adequacy_state,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.raw_evidence_ref.ref_id,
                    assessment.adequacy_state,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> SingleSlopeAdequacyAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_single_slope_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = SingleSlopeAdequacyAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.raw_evidence_ref.ref_id
            != row['raw_evidence_ref_id']
            or assessment.adequacy_state != row['adequacy_state']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise CoupledDecayIntegrityError(
                'single-slope assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[SingleSlopeAdequacyAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_single_slope_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            SingleSlopeAdequacyAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    def save_qualification(
        self, qualification: InterRegionEnergyDecayQualification
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
            raise CoupledDecayConflictError(
                'coupled-decay qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_coupled_decay_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    behavior_state, state, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.behavior_state,
                    qualification.state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> InterRegionEnergyDecayQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_coupled_decay_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            InterRegionEnergyDecayQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.behavior_state != row['behavior_state']
            or qualification.state != row['state']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise CoupledDecayIntegrityError(
                'coupled-decay qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[InterRegionEnergyDecayQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_coupled_decay_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            InterRegionEnergyDecayQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadCoupledDecayRepository',
    'CoupledDecayConflictError',
    'CoupledDecayIntegrityError',
]
