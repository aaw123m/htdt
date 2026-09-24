"""Append-only persistence for :class:`DesignDecisionRecord` (#654).

Shares the SceneRepository SQLite path so decision records travel with the
project database. Decisions are immutable: ``save_decision`` rejects id
re-use (a corrected record supersedes — it never edits in place).

Every non-``other`` authority ref on the record is resolved at save time
(#797): the referenced authority must exist in the canonical owner
repository, belong to the same project when project-scoped, and carry the
exact semantic hash the owner exposes — a self-hash proves the manifest is
internally unchanged, never that its provenance is true.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
)
from .cad_design_decision import DecisionAuthorityRef, DesignDecisionRecord


class DesignDecisionConflictError(ValueError):
    """Persisted decision differs from an attempted re-save."""


class DesignDecisionRefError(ValueError):
    """A decision names an authority that canonical resolution rejects."""


class CadDesignDecisionRepository:
    """SQLite store for explicit design decisions, scoped per document.

    ``ref_resolver`` proves every non-``other`` ref against the canonical
    owner repositories (see :class:`CanonicalAuthorityRefResolver`). Without
    a resolver only ``other`` refs — external authorities HTDT does not own —
    can be persisted.
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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS design_decisions (
                decision_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                decision_scope TEXT NOT NULL,
                selected_ref_id TEXT NOT NULL,
                supersedes_decision_id TEXT,
                created_at_utc TEXT NOT NULL,
                decision_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_design_decisions_document
            ON design_decisions(document_id)
            """
        )
        return connection

    def _resolve_refs(self, decision: DesignDecisionRecord) -> None:
        """Re-resolve every exact ref against canonical owners (#797)."""

        refs: list[DecisionAuthorityRef] = [
            decision.selected_ref,
            *decision.considered_refs,
            *decision.analysis_study_refs,
            *decision.accepted_assumption_refs,
        ]
        if decision.comparison_set_ref is not None:
            refs.append(decision.comparison_set_ref)
        if decision.design_checkpoint_ref is not None:
            refs.append(decision.design_checkpoint_ref)
        if decision.applied_action_ref is not None:
            refs.append(decision.applied_action_ref)

        resolved_by_key: dict[tuple[str, str], object] = {}
        for ref in refs:
            if ref.kind == 'other':
                # 'other' is the documented escape hatch for authorities
                # HTDT does not own; it can never stand in for a canonical
                # kind.
                continue
            resolver = self.ref_resolver
            if resolver is None or not resolver.knows(ref.kind):
                raise DesignDecisionRefError(
                    f'no canonical resolver for authority kind {ref.kind}'
                )
            resolved = resolver.resolve(
                ref.kind, ref.ref_id, decision.document_id
            )
            if resolved is None:
                raise DesignDecisionRefError(
                    f'unknown {ref.kind} authority: {ref.ref_id}'
                )
            if (
                resolved.document_id is not None
                and resolved.document_id != decision.document_id
            ):
                raise DesignDecisionRefError(
                    f'{ref.kind} {ref.ref_id} belongs to another project'
                )
            if resolved.semantic_sha256 is not None:
                if ref.ref_sha256 is None:
                    raise DesignDecisionRefError(
                        f'{ref.kind} {ref.ref_id} is hash-bearing: '
                        'ref_sha256 is required'
                    )
                if ref.ref_sha256 != resolved.semantic_sha256:
                    raise DesignDecisionRefError(
                        f'{ref.kind} {ref.ref_id} semantic hash mismatch'
                    )
            resolved_by_key[(ref.kind, ref.ref_id)] = resolved

        if decision.comparison_set_ref is not None:
            for ref in (decision.selected_ref, *decision.considered_refs):
                if ref.kind != 'comparison_alternative':
                    continue
                resolved = resolved_by_key.get(('comparison_alternative', ref.ref_id))
                if (
                    resolved is None
                    or decision.comparison_set_ref.ref_id
                    not in resolved.container_ids
                ):
                    raise DesignDecisionRefError(
                        f'comparison_alternative {ref.ref_id} is not a member '
                        f'of comparison set {decision.comparison_set_ref.ref_id}'
                    )

    def save_decision(self, decision: DesignDecisionRecord) -> DesignDecisionRecord:
        with closing(self._connect()) as connection:
            existing = connection.execute(
                'SELECT decision_sha256 FROM design_decisions WHERE decision_id = ?',
                (decision.decision_id,),
            ).fetchone()
            if existing is not None:
                if existing['decision_sha256'] != decision.decision_sha256:
                    raise DesignDecisionConflictError(
                        f'design decision {decision.decision_id} already persisted '
                        'with different content'
                    )
                return decision
            if decision.supersedes_decision_id is not None:
                superseded = connection.execute(
                    'SELECT document_id FROM design_decisions WHERE decision_id = ?',
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
                    INSERT INTO design_decisions (
                        decision_id, document_id, decision_scope,
                        selected_ref_id, supersedes_decision_id, created_at_utc,
                        decision_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.document_id,
                        decision.decision_scope,
                        decision.selected_ref.ref_id,
                        decision.supersedes_decision_id,
                        decision.created_at_utc,
                        decision.decision_sha256,
                        json.dumps(
                            decision.model_dump(mode='json'), ensure_ascii=False
                        ),
                    ),
                )
        return decision

    def get_decision(self, decision_id: str) -> DesignDecisionRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM design_decisions WHERE decision_id = ?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return DesignDecisionRecord.model_validate_json(row['payload_json'])

    def list_decisions(
        self,
        document_id: str,
        *,
        decision_scope: str | None = None,
        selected_ref_id: str | None = None,
    ) -> tuple[DesignDecisionRecord, ...]:
        clauses = ['document_id = ?']
        params: list[str] = [document_id]
        if decision_scope is not None:
            clauses.append('decision_scope = ?')
            params.append(decision_scope)
        if selected_ref_id is not None:
            clauses.append('selected_ref_id = ?')
            params.append(selected_ref_id)
        query = (
            'SELECT payload_json FROM design_decisions WHERE '
            + ' AND '.join(clauses)
            + ' ORDER BY created_at_utc ASC, decision_id ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            DesignDecisionRecord.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadDesignDecisionRepository',
    'DesignDecisionConflictError',
    'DesignDecisionRefError',
]
