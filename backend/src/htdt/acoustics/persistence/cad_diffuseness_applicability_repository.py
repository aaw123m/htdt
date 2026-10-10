"""Append-only persistence for the diffuseness applicability authority
(#673, REV58-VALIDMETH).

Two tables:

* ``cad_diffuseness_assessments`` — sealed sound-field diffuseness
  eligibility assessments.
* ``cad_statistical_applicability_declarations`` — sealed downstream
  statistical-model applicability decisions.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ...cad_repository import SceneRepository
from ...cad_schema import connect_sqlite, require_native_tables
from ...canonical_json import canonical_sha256
from ..domain.cad_diffuseness_applicability import (
    SoundFieldDiffusenessAssessment,
    StatisticalModelApplicabilityDeclaration,
)


class DiffusenessApplicabilityConflictError(ValueError):
    """A diffuseness-applicability save violated append-only identity."""


class DiffusenessApplicabilityIntegrityError(ValueError):
    """A stored diffuseness-applicability row disagreed with its
    payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DiffusenessApplicabilityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DiffusenessApplicabilityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDiffusenessApplicabilityRepository:
    """Native storage for the #673 diffuseness-applicability records."""

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
                'cad_diffuseness_assessments',
                'cad_statistical_applicability_declarations',
            )

    def save_assessment(
        self, assessment: SoundFieldDiffusenessAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise DiffusenessApplicabilityConflictError(
                'diffuseness assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_diffuseness_assessments (
                    assessment_id, assessment_sha256, document_id,
                    eligibility_state, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.eligibility_state,
                    assessment.declared_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> SoundFieldDiffusenessAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_diffuseness_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = SoundFieldDiffusenessAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.eligibility_state != row['eligibility_state']
            or assessment.declared_at_utc != row['declared_at_utc']
        ):
            raise DiffusenessApplicabilityIntegrityError(
                'diffuseness assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[SoundFieldDiffusenessAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_diffuseness_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            SoundFieldDiffusenessAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    def save_declaration(
        self, declaration: StatisticalModelApplicabilityDeclaration
    ) -> None:
        _assert_sealed(
            declaration, 'declaration_sha256', 'declaration_id'
        )
        existing = self.get_declaration(declaration.declaration_id)
        if existing is not None:
            if (
                existing.declaration_sha256
                == declaration.declaration_sha256
            ):
                return
            raise DiffusenessApplicabilityConflictError(
                'applicability declarations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_statistical_applicability_declarations (
                    declaration_id, declaration_sha256, document_id,
                    assessment_ref_id, state, basis, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    declaration.declaration_id,
                    declaration.declaration_sha256,
                    declaration.document_id,
                    (
                        declaration.assessment_ref.ref_id
                        if declaration.assessment_ref is not None
                        else None
                    ),
                    declaration.state,
                    declaration.basis,
                    declaration.evaluated_at_utc,
                    declaration.model_dump_json(),
                ),
            )

    def get_declaration(
        self, declaration_id: str
    ) -> StatisticalModelApplicabilityDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM '
                'cad_statistical_applicability_declarations '
                'WHERE declaration_id=?',
                (declaration_id,),
            ).fetchone()
        if row is None:
            return None
        declaration = (
            StatisticalModelApplicabilityDeclaration.model_validate_json(
                row['payload_json']
            )
        )
        stored_assessment_ref = (
            declaration.assessment_ref.ref_id
            if declaration.assessment_ref is not None
            else None
        )
        if (
            declaration.declaration_id != row['declaration_id']
            or declaration.declaration_sha256
            != row['declaration_sha256']
            or declaration.document_id != row['document_id']
            or stored_assessment_ref != row['assessment_ref_id']
            or declaration.state != row['state']
            or declaration.basis != row['basis']
            or declaration.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DiffusenessApplicabilityIntegrityError(
                'applicability declaration row disagrees with payload'
            )
        return declaration

    def list_declarations(
        self, document_id: str
    ) -> tuple[StatisticalModelApplicabilityDeclaration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_statistical_applicability_declarations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            StatisticalModelApplicabilityDeclaration.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadDiffusenessApplicabilityRepository',
    'DiffusenessApplicabilityConflictError',
    'DiffusenessApplicabilityIntegrityError',
]
