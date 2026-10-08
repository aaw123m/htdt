"""Append-only persistence for decision briefs (#937).

One table — ``cad_decision_briefs`` — behind the shared ``_SealedStore``
machinery (save-time seal re-verification, read-time column-vs-payload
checks). On top of the store, every authority the brief binds is resolved
exactly at write time: the baseline, the comparison set it reuses, each
candidate and each gate's pinned verdict must exist in this document and
match the pinned hash — an unverifiable pin is never persisted.

Reads re-resolve the identity refs (baseline / comparison / candidate)
so a brief whose lineage refs no longer resolve fails closed instead of
re-presenting stale conclusions; gate pins are sealed evidence claims
verified by the record's hash and re-evaluated by the caller's freshness
check, not silently re-promoted.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from .cad_authority_resolver import (
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_calibration_deployment_repository import _SealedStore
from .cad_decision_brief import CadDecisionBrief, collect_gate_pins
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_system_variant_repository import CadSystemVariantRepository


class DecisionBriefIntegrityError(ValueError):
    """A persisted decision brief failed authoritative re-verification."""


class CadDecisionBriefRepository:
    """Native storage for CadDecisionBrief records.

    Briefs are immutable: a new evaluation appends a new sealed record,
    never an UPDATE. ``latest_brief`` returns the most recently appended
    record — the UI layer applies :func:`brief_freshness` against the
    live scene head rather than re-showing stale conclusions.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            kind_resolvers=kind_resolvers,
        )
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_decision_briefs')
        self.briefs = _SealedStore(
            self._connect,
            'cad_decision_briefs',
            CadDecisionBrief,
            'brief_id',
            'brief_sha256',
            (
                ('document_id', '__document_id__'),
                ('scene_revision_id', 'scene_revision_id'),
                ('baseline_ref_id', 'baseline_ref.ref_id'),
                ('top_tier', 'top_tier'),
                ('action_count', 'action_count'),
                ('ready_count', 'ready_count'),
                ('created_at_utc', 'created_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- writes ---------------------------------------------------------------

    def save_brief(self, brief: CadDecisionBrief) -> None:
        # Fail-closed at write: every pinned authority — baseline, reused
        # comparison set, candidates and every gate verdict — must resolve
        # exactly inside this document. A pin that cannot resolve is never
        # persisted as evidence.
        for ref in collect_gate_pins(brief):
            self.resolver.resolve(ref, document_id=brief.document_id)
        self.briefs.save(brief)

    # -- reads ----------------------------------------------------------------

    def _verify_identity_refs(self, brief: CadDecisionBrief) -> None:
        """Re-resolve the refs that establish the brief's lineage.

        Baseline, comparison set and candidate refs are canonical identity:
        a stored brief whose lineage no longer resolves is corrupt or
        detached, and fails closed rather than re-showing stale
        conclusions. Gate pins are sealed evidence payload — their
        integrity is guaranteed by ``brief_sha256`` and their currency is
        the caller's freshness check.
        """

        refs = [brief.baseline_ref]
        if brief.comparison_ref is not None:
            refs.append(brief.comparison_ref)
        refs.extend(action.candidate_ref for action in brief.actions)
        for ref in refs:
            # A kind this deployment cannot resolve was proven at write by
            # whatever resolver was wired then; the seal still covers it —
            # skip rather than fail closed on a resolver that was never
            # part of this repository's deployment.
            if not self.resolver.registry.knows(ref.kind):
                continue
            try:
                self.resolver.resolve(ref, document_id=brief.document_id)
            except ValueError as exc:
                raise DecisionBriefIntegrityError(
                    f'decision brief {brief.brief_id} identity ref no '
                    f'longer resolves: {ref.kind}:{ref.ref_id}'
                ) from exc

    def get_brief(self, brief_id: str) -> CadDecisionBrief | None:
        brief = self.briefs.get(brief_id)
        if brief is not None:
            self._verify_identity_refs(brief)
        return brief

    def list_briefs(
        self,
        document_id: str,
        *,
        scene_revision_id: str | None = None,
    ) -> tuple[CadDecisionBrief, ...]:
        clause = ' WHERE document_id = ?'
        params: tuple[str, ...] = (document_id,)
        if scene_revision_id is not None:
            clause += ' AND scene_revision_id = ?'
            params = params + (scene_revision_id,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT brief_id FROM cad_decision_briefs'
                + clause + ' ORDER BY seq ASC',
                params,
            ).fetchall()
        briefs: list[CadDecisionBrief] = []
        for row in rows:
            brief = self.get_brief(row[0])
            if brief is None:
                raise DecisionBriefIntegrityError(  # error-boundary: sealed store read
                    f'decision brief {row[0]} unreadable'
                )
            briefs.append(brief)
        return tuple(briefs)

    def latest_brief(
        self,
        document_id: str,
        *,
        scene_revision_id: str | None = None,
    ) -> CadDecisionBrief | None:
        """The most recently appended brief — never an inferred one."""

        briefs = self.list_briefs(document_id, scene_revision_id=scene_revision_id)
        return briefs[-1] if briefs else None


__all__ = [
    'CadDecisionBriefRepository',
    'DecisionBriefIntegrityError',
]
