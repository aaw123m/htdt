"""Append-only persistence for :class:`AssumptionDecision` (#620).

Shares the SceneRepository SQLite path. Re-saving the same record is a
no-op; re-using an id with different content raises
:class:`AssumptionDecisionConflictError` — corrections supersede, they
never edit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from .cad_schema import connect_sqlite, require_native_tables
from .cad_assumption_decision import (
    AssumptionDecision,
    AssumptionDecisionIntegrityError,
    AssumptionSubjectRef,
    assumption_lineage_issues,
)
from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
)


class AssumptionDecisionConflictError(ValueError):
    """Persisted assumption decision differs from an attempted re-save."""


class AssumptionDecisionRefError(ValueError):
    """A decision names an authority that canonical resolution rejects."""


class AssumptionDecisionStaleHeadError(ValueError):
    """The superseded decision already has a successor (#869).

    Supersession is a single-head lineage within one subject: the losing
    writer rebuilds the decision as superseding the subject's current head
    instead.
    """


def _instant(value: str) -> datetime:
    """Parse a validated UTC-aware persisted timestamp (#869)."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # Writers persist UTC ISO-8601; a foreign/restored row that lost its
        # marker is the same instant — treating it as host-local would skew.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection,
                'assumption_decisions',
            )

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
        return connect_sqlite(self.path)

    def save_decision(self, decision: AssumptionDecision) -> AssumptionDecision:
        """Append a decision into a same-subject, single-head lineage (#869).

        Supersession is enforced at the repository boundary — the builder's
        check is convenience, this is authority:

        - the predecessor must exist, live in this document and cover the
          *same subject* (kind + ref_id), so a revision can never
          deactivate an unrelated assumption;
        - the predecessor must still be the head — one predecessor has one
          successor until explicit branch semantics exist;
        - ``attested_classification``/scope/expiry *may* legitimately
          change across a revision (e.g. narrowing a project assumption to
          a checkpoint); the change is recorded semantics, not equality —
          the lineage edge and the distinct payload hash keep it
          explainable;
        - the new ``created_at_utc`` must not predate the superseded
          record's — timestamps are provenance, never head selection.
        """

        # BEGIN IMMEDIATE makes the successor-existence check atomic: two
        # concurrent writers cannot both observe the same head (#869).
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
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
                    'SELECT document_id, subject_kind, subject_ref_id, '
                    'created_at_utc '
                    'FROM assumption_decisions WHERE decision_id = ?',
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
                if (
                    superseded['subject_kind'] != decision.subject.kind
                    or superseded['subject_ref_id'] != decision.subject.ref_id
                ):
                    raise ValueError(
                        'superseded decision covers a different subject'
                    )
                if _instant(decision.created_at_utc) < _instant(
                    superseded['created_at_utc']
                ):
                    raise ValueError(
                        'superseding decision predates its predecessor'
                    )
                successor = connection.execute(
                    'SELECT decision_id FROM assumption_decisions '
                    'WHERE supersedes_decision_id = ?',
                    (decision.supersedes_decision_id,),
                ).fetchone()
                if successor is not None:
                    raise AssumptionDecisionStaleHeadError(
                        f'decision {decision.supersedes_decision_id} is '
                        f'already superseded by {successor["decision_id"]}'
                    )
            self._resolve_refs(decision)
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
                            decision.model_dump(mode='json'), ensure_ascii=False,
                            allow_nan=False,
                        ),
                    ),
                )
        return decision

    def _row_to_decision(self, row: sqlite3.Row) -> AssumptionDecision:
        """Authoritative read: row columns, payload and refs must agree (#869)."""

        try:
            decision = AssumptionDecision.model_validate_json(
                row['payload_json']
            )
        except ValidationError as exc:
            raise AssumptionDecisionIntegrityError(
                f'assumption decision payload corrupt: {row["decision_id"]}'
            ) from exc
        if (
            decision.decision_id != row['decision_id']
            or decision.document_id != row['document_id']
            or decision.subject.kind != row['subject_kind']
            or decision.subject.ref_id != row['subject_ref_id']
            or decision.attested_classification != row['attested_classification']
            or decision.supersedes_decision_id != row['supersedes_decision_id']
            or decision.created_at_utc != row['created_at_utc']
            or decision.decision_sha256 != row['decision_sha256']
        ):
            raise AssumptionDecisionIntegrityError(
                'assumption decision row/payload mismatch: '
                f'{row["decision_id"]}'
            )
        self._resolve_refs(decision)
        return decision

    def get_decision(self, decision_id: str) -> AssumptionDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM assumption_decisions WHERE decision_id = ?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_decision(row)

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
            'SELECT * FROM assumption_decisions WHERE '
            + ' AND '.join(clauses)
            + ' ORDER BY created_at_utc ASC, decision_id ASC'
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        decisions = tuple(self._row_to_decision(row) for row in rows)
        # Supersession stays inside one subject (#869), so subject filters
        # never split a lineage — the returned set is the whole chain.
        issues = assumption_lineage_issues(decisions)
        if issues:
            raise AssumptionDecisionIntegrityError(
                'persisted assumption decision lineage is corrupt: '
                + '; '.join(issues)
            )
        return decisions


__all__ = [
    'AssumptionDecisionConflictError',
    'AssumptionDecisionIntegrityError',
    'AssumptionDecisionRefError',
    'AssumptionDecisionStaleHeadError',
    'CadAssumptionDecisionRepository',
]
