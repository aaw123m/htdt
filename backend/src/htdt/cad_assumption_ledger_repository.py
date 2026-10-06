"""Append-only persistence for the engineering-assumption /
permissible-use ledger authority (#730, REV59-UNITS).

Three tables:

* ``cad_engineering_assumptions`` — sealed assumption records
  (content, rationale, permissible uses).
* ``cad_assumption_resolutions`` — sealed resolution lifecycle events.
* ``cad_permissible_use_assessments`` — sealed permissible-use
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_assumption_ledger import (
    CadAssumptionResolution,
    CadEngineeringAssumption,
    CadPermissibleUseAssessment,
)


class AssumptionLedgerConflictError(ValueError):
    """An assumption-ledger save violated append-only identity rules."""


class AssumptionLedgerIntegrityError(ValueError):
    """A stored assumption-ledger row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AssumptionLedgerIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise AssumptionLedgerIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadAssumptionLedgerRepository:
    """Native storage for the #730 assumption-ledger authority."""

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
                'cad_engineering_assumptions',
                'cad_assumption_resolutions',
                'cad_permissible_use_assessments',
            )

    # ------------------------------------------------------------------
    # Assumptions

    def save_assumption(
        self, assumption: CadEngineeringAssumption
    ) -> None:
        _assert_sealed(
            assumption, 'assumption_sha256', 'assumption_id'
        )
        existing = self.get_assumption(assumption.assumption_id)
        if existing is not None:
            if (
                existing.assumption_sha256
                == assumption.assumption_sha256
            ):
                return
            raise AssumptionLedgerConflictError(
                'engineering assumptions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_engineering_assumptions (
                    assumption_id, assumption_sha256, document_id,
                    subject_ref_id, assumption_kind, evidence_state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assumption.assumption_id,
                    assumption.assumption_sha256,
                    assumption.document_id,
                    assumption.subject_ref.ref_id,
                    assumption.assumption_kind,
                    assumption.evidence_state,
                    assumption.declared_at_utc,
                    assumption.model_dump_json(),
                ),
            )

    def get_assumption(
        self, assumption_id: str
    ) -> CadEngineeringAssumption | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_engineering_assumptions '
                'WHERE assumption_id=?',
                (assumption_id,),
            ).fetchone()
        if row is None:
            return None
        assumption = CadEngineeringAssumption.model_validate_json(
            row['payload_json']
        )
        if (
            assumption.assumption_id != row['assumption_id']
            or assumption.assumption_sha256 != row['assumption_sha256']
            or assumption.document_id != row['document_id']
            or assumption.subject_ref.ref_id != row['subject_ref_id']
            or assumption.assumption_kind != row['assumption_kind']
            or assumption.evidence_state != row['evidence_state']
            or assumption.declared_at_utc != row['declared_at_utc']
        ):
            raise AssumptionLedgerIntegrityError(
                'engineering assumption row disagrees with payload'
            )
        return assumption

    def list_assumptions(
        self, document_id: str
    ) -> tuple[CadEngineeringAssumption, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_engineering_assumptions '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEngineeringAssumption.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Resolutions

    def save_resolution(
        self, resolution: CadAssumptionResolution
    ) -> None:
        _assert_sealed(
            resolution, 'resolution_sha256', 'resolution_id'
        )
        existing = self.get_resolution(resolution.resolution_id)
        if existing is not None:
            if (
                existing.resolution_sha256
                == resolution.resolution_sha256
            ):
                return
            raise AssumptionLedgerConflictError(
                'assumption resolutions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_assumption_resolutions (
                    resolution_id, resolution_sha256, document_id,
                    assumption_ref_id, resolution_state,
                    resolved_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution.resolution_id,
                    resolution.resolution_sha256,
                    resolution.document_id,
                    resolution.assumption_ref.ref_id,
                    resolution.resolution_state,
                    resolution.resolved_at_utc,
                    resolution.model_dump_json(),
                ),
            )

    def get_resolution(
        self, resolution_id: str
    ) -> CadAssumptionResolution | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_assumption_resolutions '
                'WHERE resolution_id=?',
                (resolution_id,),
            ).fetchone()
        if row is None:
            return None
        resolution = CadAssumptionResolution.model_validate_json(
            row['payload_json']
        )
        if (
            resolution.resolution_id != row['resolution_id']
            or resolution.resolution_sha256 != row['resolution_sha256']
            or resolution.document_id != row['document_id']
            or resolution.assumption_ref.ref_id
            != row['assumption_ref_id']
            or resolution.resolution_state != row['resolution_state']
            or resolution.resolved_at_utc != row['resolved_at_utc']
        ):
            raise AssumptionLedgerIntegrityError(
                'assumption resolution row disagrees with payload'
            )
        return resolution

    def list_resolutions(
        self, document_id: str
    ) -> tuple[CadAssumptionResolution, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_assumption_resolutions '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadAssumptionResolution.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Permissible-use assessments

    def save_assessment(
        self, assessment: CadPermissibleUseAssessment
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
            raise AssumptionLedgerConflictError(
                'permissible-use assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_permissible_use_assessments (
                    assessment_id, assessment_sha256, document_id,
                    intended_use, verdict, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.intended_use,
                    assessment.verdict,
                    assessment.evaluation_version,
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> CadPermissibleUseAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_permissible_use_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = CadPermissibleUseAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.intended_use != row['intended_use']
            or assessment.verdict != row['verdict']
            or assessment.evaluation_version
            != row['evaluation_version']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise AssumptionLedgerIntegrityError(
                'permissible-use assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[CadPermissibleUseAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_permissible_use_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadPermissibleUseAssessment.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'AssumptionLedgerConflictError',
    'AssumptionLedgerIntegrityError',
    'CadAssumptionLedgerRepository',
]
