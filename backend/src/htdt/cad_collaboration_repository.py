"""Append-only persistence for REV60-COLLABENV collaboration authority.

Eleven tables in one repository (issue #721):

* ``cad_collaboration_actors`` / ``cad_revision_authorship`` /
  ``cad_information_states``
* ``cad_approval_records`` / ``cad_review_decisions``
* ``cad_sibling_divergences`` / ``cad_conflict_resolutions``
* ``cad_branch_proposals`` / ``cad_proposal_promotions``
* ``cad_client_acceptances`` / ``cad_collaboration_events``
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_collaboration import (
    ApprovalRecord,
    BranchProposal,
    ClientAcceptance,
    CollaborationActor,
    CollaborationEvent,
    ConflictResolution,
    InformationStateRecord,
    ProposalPromotion,
    ReviewDecision,
    RevisionAuthorship,
    SiblingDivergence,
)


class CollaborationConflictError(ValueError):
    """A collaboration save violated append-only identity rules."""


class CollaborationIntegrityError(ValueError):
    """A stored collaboration row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(
        record.identity_payload()  # type: ignore[attr-defined]
    )
    if getattr(record, sha_field) != sha:
        raise CollaborationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CollaborationIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise CollaborationConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise CollaborationIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise CollaborationIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise CollaborationIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCollaborationRepository:
    """Native storage for the #721 collaboration/approval authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_collaboration_actors',
                'cad_revision_authorship',
                'cad_information_states',
                'cad_approval_records',
                'cad_review_decisions',
                'cad_sibling_divergences',
                'cad_conflict_resolutions',
                'cad_branch_proposals',
                'cad_proposal_promotions',
                'cad_client_acceptances',
                'cad_collaboration_events',
            )
        self.actors = _SealedStore(
            self._connect, 'cad_collaboration_actors',
            CollaborationActor, 'actor_id', 'actor_sha256',
            (
                ('document_id', '__document_id__'),
                ('identity_basis', 'identity_basis'),
            ),
        )
        self.authorship = _SealedStore(
            self._connect, 'cad_revision_authorship',
            RevisionAuthorship, 'authorship_id', 'authorship_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('author_ref_id', 'author_ref'),
                ('parent_revision_id', 'parent_revision_id'),
                ('result_revision_id', 'result_revision_id'),
                ('change_scope', 'change_scope'),
            ),
        )
        self.information_states = _SealedStore(
            self._connect, 'cad_information_states',
            InformationStateRecord, 'state_id', 'state_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('state', 'state'),
                _ref('actor_ref_id', 'actor_ref'),
            ),
        )
        self.approvals = _SealedStore(
            self._connect, 'cad_approval_records',
            ApprovalRecord, 'approval_id', 'approval_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                ('scope_kind', 'scope_kind'),
                _ref('approver_ref_id', 'approver_ref'),
                ('decision', 'decision'),
            ),
        )
        self.review_decisions = _SealedStore(
            self._connect, 'cad_review_decisions',
            ReviewDecision, 'decision_id', 'decision_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                _ref('author_ref_id', 'author_ref'),
                ('kind', 'kind'),
                ('status', 'status'),
            ),
        )
        self.sibling_divergences = _SealedStore(
            self._connect, 'cad_sibling_divergences',
            SiblingDivergence, 'assessment_id', 'assessment_sha256',
            (
                ('document_id', '__document_id__'),
                ('verdict', 'verdict'),
                ('outcome', 'outcome'),
            ),
        )
        self.conflict_resolutions = _SealedStore(
            self._connect, 'cad_conflict_resolutions',
            ConflictResolution, 'resolution_id', 'resolution_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('assessment_ref_id', 'assessment_ref'),
                ('resolution_kind', 'resolution_kind'),
                _ref('resolver_ref_id', 'resolver_ref'),
            ),
        )
        self.branch_proposals = _SealedStore(
            self._connect, 'cad_branch_proposals',
            BranchProposal, 'proposal_id', 'proposal_sha256',
            (
                ('document_id', '__document_id__'),
                ('proposal_kind', 'proposal_kind'),
                _ref('author_ref_id', 'author_ref'),
                ('status', 'status'),
            ),
        )
        self.proposal_promotions = _SealedStore(
            self._connect, 'cad_proposal_promotions',
            ProposalPromotion, 'promotion_id', 'promotion_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('proposal_ref_id', 'proposal_ref'),
                _ref('promoted_by_ref_id', 'promoted_by_ref'),
            ),
        )
        self.client_acceptances = _SealedStore(
            self._connect, 'cad_client_acceptances',
            ClientAcceptance, 'acceptance_id', 'acceptance_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('subject_ref_id', 'subject_ref'),
                _ref('client_ref_id', 'client_ref'),
            ),
        )
        self.events = _SealedStore(
            self._connect, 'cad_collaboration_events',
            CollaborationEvent, 'event_id', 'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('kind', 'kind'),
                _ref('actor_ref_id', 'actor_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # Wrappers used by the audit replay chain and callers.
    def save_actor(self, record: CollaborationActor) -> None:
        self.actors.save(record)

    def get_actor(self, actor_id: str) -> CollaborationActor | None:
        return self.actors.get(actor_id)

    def save_authorship(self, record: RevisionAuthorship) -> None:
        self.authorship.save(record)

    def get_authorship(
        self, authorship_id: str
    ) -> RevisionAuthorship | None:
        return self.authorship.get(authorship_id)

    def save_information_state(
        self, record: InformationStateRecord
    ) -> None:
        self.information_states.save(record)

    def get_information_state(
        self, state_id: str
    ) -> InformationStateRecord | None:
        return self.information_states.get(state_id)

    def save_approval(self, record: ApprovalRecord) -> None:
        self.approvals.save(record)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self.approvals.get(approval_id)

    def save_review_decision(self, record: ReviewDecision) -> None:
        self.review_decisions.save(record)

    def get_review_decision(
        self, decision_id: str
    ) -> ReviewDecision | None:
        return self.review_decisions.get(decision_id)

    def save_sibling_divergence(
        self, record: SiblingDivergence
    ) -> None:
        self.sibling_divergences.save(record)

    def get_sibling_divergence(
        self, assessment_id: str
    ) -> SiblingDivergence | None:
        return self.sibling_divergences.get(assessment_id)

    def save_conflict_resolution(
        self, record: ConflictResolution
    ) -> None:
        self.conflict_resolutions.save(record)

    def get_conflict_resolution(
        self, resolution_id: str
    ) -> ConflictResolution | None:
        return self.conflict_resolutions.get(resolution_id)

    def save_branch_proposal(self, record: BranchProposal) -> None:
        self.branch_proposals.save(record)

    def get_branch_proposal(
        self, proposal_id: str
    ) -> BranchProposal | None:
        return self.branch_proposals.get(proposal_id)

    def save_proposal_promotion(
        self, record: ProposalPromotion
    ) -> None:
        self.proposal_promotions.save(record)

    def get_proposal_promotion(
        self, promotion_id: str
    ) -> ProposalPromotion | None:
        return self.proposal_promotions.get(promotion_id)

    def save_client_acceptance(
        self, record: ClientAcceptance
    ) -> None:
        self.client_acceptances.save(record)

    def get_client_acceptance(
        self, acceptance_id: str
    ) -> ClientAcceptance | None:
        return self.client_acceptances.get(acceptance_id)

    def save_event(self, record: CollaborationEvent) -> None:
        self.events.save(record)

    def get_event(self, event_id: str) -> CollaborationEvent | None:
        return self.events.get(event_id)
