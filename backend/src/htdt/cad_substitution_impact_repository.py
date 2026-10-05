"""Append-only persistence for the substitution/change-impact authority
(#596).

Five sealed record families:

* ``cad_substitution_proposals`` — exact original/proposed declarations;
* ``cad_change_impact_assessments`` — dimensional matrices + dependency
  dispositions + derived technical verdicts;
* ``cad_substitution_decisions`` — approval/override/as-built-verified
  state transitions (technical and commercial channels kept separate);
* ``cad_asbuilt_reconciliations`` — installed-vs-approved identity
  checks;
* ``cad_equipment_schedule_records`` — versioned schedule phases
  (design → approved → procured → installed → service).

Every save re-verifies the sealed hash; an id re-saved with a different
payload is a conflict, never an update.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_substitution_impact import (
    AsBuiltReconciliation,
    ChangeImpactAssessment,
    EquipmentScheduleRecord,
    EquipmentSubstitutionProposal,
    SubstitutionApprovalDecision,
)
from .canonical_json import canonical_sha256 as _hash


class SubstitutionImpactConflictError(ValueError):
    """A save violated append-only identity rules."""


class SubstitutionImpactIntegrityError(ValueError):
    """A stored or incoming payload disagreed with its sealed hash."""


def _assert_sealed(record, *, sha_field: str) -> None:
    digest = _hash(record.semantic_payload())
    if getattr(record, sha_field) != digest:
        raise SubstitutionImpactIntegrityError(
            f'{type(record).__name__} payload diverges from its sealed '
            'hash — re-derive the record instead of mutating a copy'
        )


class CadSubstitutionImpactRepository:
    """Native storage for #596 substitution lifecycle records."""

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
                'cad_substitution_proposals',
                'cad_change_impact_assessments',
                'cad_substitution_decisions',
                'cad_asbuilt_reconciliations',
                'cad_equipment_schedule_records',
            )

    def _save(
        self,
        *,
        table: str,
        id_column: str,
        record_id: str,
        columns: dict[str, object],
        payload_json: str,
        existing_sha: str | None,
        exists: bool,
        record_sha: str,
    ) -> None:
        if exists:
            if existing_sha == record_sha:
                return
            raise SubstitutionImpactConflictError(
                f'{table} rows are append-only — a change is a new '
                'record, never an update in place'
            )
        cols = ', '.join([id_column, *columns.keys(), 'payload_json'])
        marks = ', '.join(['?'] * (len(columns) + 2))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({cols}) VALUES ({marks})',
                (record_id, *columns.values(), payload_json),
            )

    # ------------------------------------------------------------------
    # Proposals

    def save_proposal(
        self, proposal: EquipmentSubstitutionProposal
    ) -> None:
        _assert_sealed(proposal, sha_field='proposal_sha256')
        existing = self.get_proposal(proposal.proposal_id)
        self._save(
            table='cad_substitution_proposals',
            id_column='proposal_id',
            record_id=proposal.proposal_id,
            columns={
                'document_id': proposal.document_id,
                'original_definition_id': proposal.original.definition_id,
                'proposed_definition_id': proposal.proposed.definition_id,
                'reason_kind': proposal.reason_kind,
                'evidence_class': proposal.evidence_class,
                'proposal_sha256': proposal.proposal_sha256,
                'requested_at_utc': proposal.requested_at_utc,
            },
            payload_json=proposal.model_dump_json(),
            existing_sha=(
                None if existing is None else existing.proposal_sha256
            ),
            exists=existing is not None,
            record_sha=proposal.proposal_sha256,
        )

    def get_proposal(
        self, proposal_id: str
    ) -> EquipmentSubstitutionProposal | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_substitution_proposals '
                'WHERE proposal_id=?',
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return EquipmentSubstitutionProposal.model_validate_json(
            row['payload_json']
        )

    def list_proposals(
        self, document_id: str
    ) -> tuple[EquipmentSubstitutionProposal, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_substitution_proposals '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            EquipmentSubstitutionProposal.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Impact assessments

    def save_assessment(self, assessment: ChangeImpactAssessment) -> None:
        _assert_sealed(assessment, sha_field='assessment_sha256')
        if assessment.proposal_id != self._proposal_id_for_sha(
            assessment.proposal_sha256
        ):
            raise SubstitutionImpactIntegrityError(
                'assessment references a proposal hash that does not '
                'resolve to the recorded proposal id'
            )
        existing = self.get_assessment(assessment.assessment_id)
        self._save(
            table='cad_change_impact_assessments',
            id_column='assessment_id',
            record_id=assessment.assessment_id,
            columns={
                'document_id': assessment.document_id,
                'proposal_id': assessment.proposal_id,
                'proposal_sha256': assessment.proposal_sha256,
                'technical_verdict': assessment.technical_verdict,
                'assessment_sha256': assessment.assessment_sha256,
                'assessed_at_utc': assessment.assessed_at_utc,
            },
            payload_json=assessment.model_dump_json(),
            existing_sha=(
                None if existing is None else existing.assessment_sha256
            ),
            exists=existing is not None,
            record_sha=assessment.assessment_sha256,
        )

    def _proposal_id_for_sha(self, proposal_sha256: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT proposal_id FROM cad_substitution_proposals '
                'WHERE proposal_sha256=?',
                (proposal_sha256,),
            ).fetchone()
        return None if row is None else row['proposal_id']

    def get_assessment(
        self, assessment_id: str
    ) -> ChangeImpactAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_change_impact_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        return ChangeImpactAssessment.model_validate_json(
            row['payload_json']
        )

    def list_assessments(
        self, document_id: str, *, proposal_id: str | None = None
    ) -> tuple[ChangeImpactAssessment, ...]:
        where = 'document_id=?'
        params: tuple[str, ...] = (document_id,)
        if proposal_id is not None:
            where += ' AND proposal_id=?'
            params = (document_id, proposal_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_change_impact_assessments '
                f'WHERE {where} ORDER BY seq ASC',
                params,
            ).fetchall()
        return tuple(
            ChangeImpactAssessment.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Decisions

    def save_decision(self, decision: SubstitutionApprovalDecision) -> None:
        _assert_sealed(decision, sha_field='decision_sha256')
        existing = self.get_decision(decision.decision_id)
        self._save(
            table='cad_substitution_decisions',
            id_column='decision_id',
            record_id=decision.decision_id,
            columns={
                'document_id': decision.document_id,
                'proposal_id': decision.proposal_id,
                'state': decision.state,
                'commercial_state': decision.commercial_state,
                'decision_sha256': decision.decision_sha256,
                'decided_at_utc': decision.decided_at_utc,
            },
            payload_json=decision.model_dump_json(),
            existing_sha=(
                None if existing is None else existing.decision_sha256
            ),
            exists=existing is not None,
            record_sha=decision.decision_sha256,
        )

    def get_decision(
        self, decision_id: str
    ) -> SubstitutionApprovalDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_substitution_decisions '
                'WHERE decision_id=?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return SubstitutionApprovalDecision.model_validate_json(
            row['payload_json']
        )

    def list_decisions(
        self, document_id: str, *, proposal_id: str | None = None
    ) -> tuple[SubstitutionApprovalDecision, ...]:
        where = 'document_id=?'
        params: tuple[str, ...] = (document_id,)
        if proposal_id is not None:
            where += ' AND proposal_id=?'
            params = (document_id, proposal_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_substitution_decisions '
                f'WHERE {where} ORDER BY seq ASC',
                params,
            ).fetchall()
        return tuple(
            SubstitutionApprovalDecision.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # As-built reconciliations

    def save_reconciliation(
        self, reconciliation: AsBuiltReconciliation
    ) -> None:
        _assert_sealed(
            reconciliation, sha_field='reconciliation_sha256'
        )
        existing = self.get_reconciliation(reconciliation.reconciliation_id)
        self._save(
            table='cad_asbuilt_reconciliations',
            id_column='reconciliation_id',
            record_id=reconciliation.reconciliation_id,
            columns={
                'document_id': reconciliation.document_id,
                'proposal_id': reconciliation.proposal_id,
                'verdict': reconciliation.verdict,
                'reconciliation_sha256': reconciliation.reconciliation_sha256,
                'reconciled_at_utc': reconciliation.reconciled_at_utc,
            },
            payload_json=reconciliation.model_dump_json(),
            existing_sha=(
                None
                if existing is None
                else existing.reconciliation_sha256
            ),
            exists=existing is not None,
            record_sha=reconciliation.reconciliation_sha256,
        )

    def get_reconciliation(
        self, reconciliation_id: str
    ) -> AsBuiltReconciliation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_asbuilt_reconciliations '
                'WHERE reconciliation_id=?',
                (reconciliation_id,),
            ).fetchone()
        if row is None:
            return None
        return AsBuiltReconciliation.model_validate_json(row['payload_json'])

    def list_reconciliations(
        self, document_id: str, *, proposal_id: str | None = None
    ) -> tuple[AsBuiltReconciliation, ...]:
        where = 'document_id=?'
        params: tuple[str, ...] = (document_id,)
        if proposal_id is not None:
            where += ' AND proposal_id=?'
            params = (document_id, proposal_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_asbuilt_reconciliations '
                f'WHERE {where} ORDER BY seq ASC',
                params,
            ).fetchall()
        return tuple(
            AsBuiltReconciliation.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Equipment schedule records

    def save_schedule(self, schedule: EquipmentScheduleRecord) -> None:
        _assert_sealed(schedule, sha_field='schedule_sha256')
        existing = self.get_schedule(schedule.schedule_id)
        self._save(
            table='cad_equipment_schedule_records',
            id_column='schedule_id',
            record_id=schedule.schedule_id,
            columns={
                'document_id': schedule.document_id,
                'phase': schedule.phase,
                'supersedes_schedule_id': schedule.supersedes_schedule_id,
                'schedule_sha256': schedule.schedule_sha256,
                'recorded_at_utc': schedule.recorded_at_utc,
            },
            payload_json=schedule.model_dump_json(),
            existing_sha=(
                None if existing is None else existing.schedule_sha256
            ),
            exists=existing is not None,
            record_sha=schedule.schedule_sha256,
        )

    def get_schedule(
        self, schedule_id: str
    ) -> EquipmentScheduleRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_equipment_schedule_records '
                'WHERE schedule_id=?',
                (schedule_id,),
            ).fetchone()
        if row is None:
            return None
        return EquipmentScheduleRecord.model_validate_json(
            row['payload_json']
        )

    def list_schedules(
        self, document_id: str, *, phase: str | None = None
    ) -> tuple[EquipmentScheduleRecord, ...]:
        where = 'document_id=?'
        params: tuple[str, ...] = (document_id,)
        if phase is not None:
            where += ' AND phase=?'
            params = (document_id, phase)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_equipment_schedule_records '
                f'WHERE {where} ORDER BY seq ASC',
                params,
            ).fetchall()
        return tuple(
            EquipmentScheduleRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadSubstitutionImpactRepository',
    'SubstitutionImpactConflictError',
    'SubstitutionImpactIntegrityError',
]
