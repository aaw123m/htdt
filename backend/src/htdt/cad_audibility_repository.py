"""Append-only persistence for the perceptual relevance / audibility
authority (#720, REV59-UNITS).

Two tables:

* ``cad_perceptual_model_profiles`` — sealed literature-pinned model /
  threshold profiles.
* ``cad_audibility_assessments`` — sealed audibility verdicts on
  physical differences.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_audibility import (
    CadAudibilityAssessment,
    CadPerceptualModelProfile,
)


class AudibilityConflictError(ValueError):
    """An audibility save violated append-only identity rules."""


class AudibilityIntegrityError(ValueError):
    """A stored audibility row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AudibilityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise AudibilityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadAudibilityRepository:
    """Native storage for the #720 audibility authority records."""

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
                'cad_perceptual_model_profiles',
                'cad_audibility_assessments',
            )

    # ------------------------------------------------------------------
    # Model profiles

    def save_profile(
        self, profile: CadPerceptualModelProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise AudibilityConflictError(
                'perceptual model profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_perceptual_model_profiles (
                    profile_id, profile_sha256, document_id,
                    model_kind, scope_class, literature_ref,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.model_kind,
                    profile.applicability.scope_class,
                    profile.literature_ref,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadPerceptualModelProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_perceptual_model_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadPerceptualModelProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.model_kind != row['model_kind']
            or profile.applicability.scope_class != row['scope_class']
            or profile.literature_ref != row['literature_ref']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise AudibilityIntegrityError(
                'perceptual model profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadPerceptualModelProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_perceptual_model_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadPerceptualModelProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Assessments

    def save_assessment(
        self, assessment: CadAudibilityAssessment
    ) -> None:
        _assert_sealed(
            assessment, 'assessment_sha256', 'assessment_id'
        )
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if (
                existing.assessment_sha256
                == assessment.assessment_sha256
            ):
                return
            raise AudibilityConflictError(
                'audibility assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_audibility_assessments (
                    assessment_id, assessment_sha256, document_id,
                    profile_ref_id, difference_ref_id, verdict,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.profile_ref.ref_id,
                    assessment.difference_ref.ref_id,
                    assessment.verdict,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadAudibilityAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_audibility_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = CadAudibilityAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.profile_ref.ref_id != row['profile_ref_id']
            or assessment.difference_ref.ref_id
            != row['difference_ref_id']
            or assessment.verdict != row['verdict']
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise AudibilityIntegrityError(
                'audibility assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[CadAudibilityAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_audibility_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadAudibilityAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'AudibilityConflictError',
    'AudibilityIntegrityError',
    'CadAudibilityRepository',
]
