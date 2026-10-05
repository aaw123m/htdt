"""Append-only persistence for bass-management qualification (#574).

Two authorities live here:

* ``cad_bass_splice_evidence`` — isolated/summed path response captures
  bound to role/sub/seat, keyed by ``evidence_id`` with the semantic hash
  carried as an indexed column.
* ``cad_bass_qualifications`` — sealed qualification verdicts keyed by
  ``qualification_id`` (deterministic from payload), indexed by
  ``profile_sha256`` so a profile edit can never silently reuse a verdict.

Saves are append-only: identical rows are no-ops, a divergent payload for
the same id is a conflict.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_bass_management_qualification import (
    BassManagementQualification,
    SplicePathEvidence,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


class BassQualificationConflictError(ValueError):
    """A bass-qualification save violated append-only identity rules."""


class BassQualificationIntegrityError(ValueError):
    """A stored row disagreed with its payload."""


class CadBassQualificationRepository:
    """Native storage for splice evidence and bass qualifications."""

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
                'cad_bass_splice_evidence',
                'cad_bass_qualifications',
            )

    # ------------------------------------------------------------------
    # Splice evidence

    def save_evidence(self, evidence: SplicePathEvidence) -> None:
        existing = self.get_evidence(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise BassQualificationConflictError(
                'splice evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_bass_splice_evidence (
                    evidence_id, evidence_sha256, document_id, role_id,
                    sub_group_id, seat_id, seat_role, path, observed_state,
                    stimulus_pin_id, measurement_dataset_sha256,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    evidence.role_id,
                    evidence.sub_group_id,
                    evidence.seat_id,
                    evidence.seat_role,
                    evidence.path,
                    evidence.observed_state,
                    evidence.stimulus_pin_id,
                    evidence.measurement_dataset_sha256,
                    evidence.captured_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_evidence(
        self, evidence_id: str
    ) -> SplicePathEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT evidence_id, evidence_sha256, document_id, role_id,
                       sub_group_id, seat_id, seat_role, path,
                       observed_state, stimulus_pin_id,
                       measurement_dataset_sha256, payload_json
                FROM cad_bass_splice_evidence
                WHERE evidence_id=?
                """,
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return self._evidence_from_row(row)

    def evidence_for_group(
        self,
        document_id: str,
        role_id: str,
        sub_group_id: str,
    ) -> tuple[SplicePathEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, evidence_sha256, document_id, role_id,
                       sub_group_id, seat_id, seat_role, path,
                       observed_state, stimulus_pin_id,
                       measurement_dataset_sha256, payload_json
                FROM cad_bass_splice_evidence
                WHERE document_id=? AND role_id=? AND sub_group_id=?
                ORDER BY captured_at_utc, evidence_id
                """,
                (document_id, role_id, sub_group_id),
            ).fetchall()
        return tuple(self._evidence_from_row(row) for row in rows)

    def list_evidence(
        self, document_id: str
    ) -> tuple[SplicePathEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, evidence_sha256, document_id, role_id,
                       sub_group_id, seat_id, seat_role, path,
                       observed_state, stimulus_pin_id,
                       measurement_dataset_sha256, payload_json
                FROM cad_bass_splice_evidence
                WHERE document_id=?
                ORDER BY captured_at_utc, evidence_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._evidence_from_row(row) for row in rows)

    def _evidence_from_row(self, row: sqlite3.Row) -> SplicePathEvidence:
        evidence = SplicePathEvidence.model_validate_json(row['payload_json'])
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or evidence.role_id != row['role_id']
            or evidence.sub_group_id != row['sub_group_id']
            or evidence.seat_id != row['seat_id']
            or evidence.seat_role != row['seat_role']
            or evidence.path != row['path']
            or evidence.observed_state != row['observed_state']
            or evidence.stimulus_pin_id != row['stimulus_pin_id']
            or evidence.measurement_dataset_sha256
            != row['measurement_dataset_sha256']
        ):
            raise BassQualificationIntegrityError(
                'splice evidence row disagrees with its payload'
            )
        return evidence

    # ------------------------------------------------------------------
    # Qualification verdicts

    def save_qualification(
        self, qualification: BassManagementQualification
    ) -> None:
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise BassQualificationConflictError(
                'bass qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_bass_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_id, profile_sha256, lifecycle_at_evaluation,
                    status, scope, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_id,
                    qualification.profile_sha256,
                    qualification.lifecycle_at_evaluation,
                    qualification.status,
                    qualification.scope,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> BassManagementQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       profile_id, profile_sha256, lifecycle_at_evaluation,
                       status, scope, payload_json
                FROM cad_bass_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return self._qualification_from_row(row)

    def qualifications_for_profile(
        self, profile_sha256: str
    ) -> tuple[BassManagementQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       profile_id, profile_sha256, lifecycle_at_evaluation,
                       status, scope, payload_json
                FROM cad_bass_qualifications
                WHERE profile_sha256=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (profile_sha256,),
            ).fetchall()
        return tuple(self._qualification_from_row(row) for row in rows)

    def list_qualifications(
        self, document_id: str
    ) -> tuple[BassManagementQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256, document_id,
                       profile_id, profile_sha256, lifecycle_at_evaluation,
                       status, scope, payload_json
                FROM cad_bass_qualifications
                WHERE document_id=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._qualification_from_row(row) for row in rows)

    def _qualification_from_row(
        self, row: sqlite3.Row
    ) -> BassManagementQualification:
        qualification = BassManagementQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_id != row['profile_id']
            or qualification.profile_sha256 != row['profile_sha256']
            or qualification.lifecycle_at_evaluation
            != row['lifecycle_at_evaluation']
            or qualification.status != row['status']
            or qualification.scope != row['scope']
        ):
            raise BassQualificationIntegrityError(
                'bass qualification row disagrees with its payload'
            )
        return qualification


__all__ = [
    'BassQualificationConflictError',
    'BassQualificationIntegrityError',
    'CadBassQualificationRepository',
]
