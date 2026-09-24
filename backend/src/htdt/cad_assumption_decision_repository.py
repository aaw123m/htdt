"""Append-only persistence for :class:`AssumptionDecision` (#620).

Shares the SceneRepository SQLite path. Re-saving the same record is a
no-op; re-using an id with different content raises
:class:`AssumptionDecisionConflictError` — corrections supersede, they
never edit.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_assumption_decision import (
    AssumptionDecision,
    AssumptionSubjectRef,
)
from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
)


class AssumptionDecisionConflictError(ValueError):
    """Persisted assumption decision differs from an attempted re-save."""


class AssumptionDecisionRefError(ValueError):
    """A decision names an authority that canonical resolution rejects."""


class CadAssumptionDecisionRepository:
    """SQLite store for scoped assumption decisions, per document.

    ``ref_resolver`` proves the decision's exact refs (#798): subject,
    scope_ref and every evidence_ref whose kind the resolver knows must
    exist in the canonical owner repository, stay inside this project, and
    carry the exact semantic hash when the owner exposes one. Refs whose
    kind has no canonical resolver (derived subjects such as
    ``room_surface``) pass through — they are addresses, not claims.
    """

    def __init__(
        self,
        scene_repository,
        ref_resolver: AuthorityRefResolver | None = None,
    ) -> None:
        self.path = Path(scene_repository.path)
        if ref_resolver is None:
            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver

    def _resolve_refs(self, decision: AssumptionDecision) -> None:
        resolver = self.ref_resolver
        if resolver is None:
            return
        refs: list[AssumptionSubjectRef] = [
            decision.subject,
            *decision.evidence_refs,
        ]
        if decision.scope_ref is not None:
            refs.append(decision.scope_ref)
        for ref in refs:
            if not resolver.knows(ref.kind):
                continue
            resolved = resolver.resolve(
                ref.kind, ref.ref_id, decision.document_id
            )
            if resolved is None:
                raise AssumptionDecisionRefError(
                    f'unknown {ref.kind} authority: {ref.ref_id}'
                )
            if (
                resolved.document_id is not None
                and resolved.document_id != decision.document_id
            ):
                raise AssumptionDecisionRefError(
                    f'{ref.kind} {ref.ref_id} belongs to another project'
                )
            if resolved.semantic_sha256 is not None:
                if ref.ref_sha256 is None:
                    raise AssumptionDecisionRefError(
                        f'{ref.kind} {ref.ref_id} is hash-bearing: '
                        'ref_sha256 is required'
                    )
                if ref.ref_sha256 != resolved.semantic_sha256:
                    raise AssumptionDecisionRefError(
                        f'{ref.kind} {ref.ref_id} semantic hash mismatch'
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assumption_decisions (
                decision_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_ref_id TEXT NOT NULL,
                attested_classification TEXT NOT NULL,
                supersedes_decision_id TEXT,
                created_at_utc TEXT NOT NULL,
                decision_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_assumption_decisions_document
            ON assumption_decisions(document_id)
            """
        )
        return connection

    def save_decision(self, decision: AssumptionDecision) -> AssumptionDecision:
        with closing(self._connect()) as connection:
            existing = connection.execute(
                'SELECT decision_sha256 FROM assumption_decisions WHERE decision_id = ?',
                (decision.decision_id,),
            ).fetchone()
            if existing is not None:
                if existing['decision_sha256'] != decision.decision_sha256:
                    raise AssumptionDecisionConflictError(
                        f'assumption decision {decision.decision_id} already '
                        'persisted with different content'
                    )
                return decision
            if decision.supersedes_decision_id is not None:
                superseded = connection.execute(
                    'SELECT document_id FROM assumption_decisions WHERE decision_id = ?',
                    (decision.supersedes_decision_id,),
                ).fetchone()
                if superseded is None:
                    raise ValueError(
                        'superseded decision is not persisted for this project'
                    )
                if superseded['document_id'] != decision.document_id:
                    raise ValueError(
                        'superseded decision belongs to another document'
                    )
            self._resolve_refs(decision)
            with connection:
                connection.execute(
                    """
                    INSERT INTO assumption_decisions (
                        decision_id, document_id, subject_kind, subject_ref_id,
                        attested_classification, supersedes_decision_id,
                        created_at_utc, decision_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.document_id,
                        decision.subject.kind,
                        decision.subject.ref_id,
                        decision.attested_classification,
                        decision.supersedes_decision_id,
                        decision.created_at_utc,
                        decision.decision_sha256,
                        json.dumps(
                            decision.model_dump(mode='json'), ensure_ascii=False
                        ),
                    ),
                )
        return decision

    def get_decision(self, decision_id: str) -> AssumptionDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM assumption_decisions WHERE decision_id = ?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return AssumptionDecision.model_validate_json(row['payload_json'])

    def list_decisions(
        self,
        document_id: str,
        *,
        subject_kind: str | None = None,
        subject_ref_id: str | None = None,
    ) -> tuple[AssumptionDecision, ...]:
        clauses = ['document_id = ?']
        params: list[str] = [document_id]
        if subject_kind is not None:
            clauses.append('subject_kind = ?')
            params.append(subject_kind)
        if subject_ref_id is not None:
            clauses.append('subject_ref_id = ?')
            params.append(subject_ref_id)
        query = (
            'SELECT payload_json FROM assumption_decisions WHERE '
            + ' AND '.join(clauses)
            + ' ORDER BY created_at_utc ASC, decision_id ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            AssumptionDecision.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'AssumptionDecisionConflictError',
    'AssumptionDecisionRefError',
    'CadAssumptionDecisionRepository',
]
