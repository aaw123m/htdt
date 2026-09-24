"""Append-only persistence for design comparison sets (#447)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
    ResolvedAuthority,
)
from .cad_design_comparison import (
    ComparisonAlternative,
    DesignComparisonSet,
)
from .cad_repository import SceneRepository


class DesignComparisonConflictError(ValueError):
    """A comparison-set save violated append-only identity rules."""


#: Canonical resolver kind for every declared ComparisonEvidenceKind —
#: ``other`` is deliberately absent: it is the explicit escape hatch for
#: evidence without a canonical authority and skips resolution entirely.
_EVIDENCE_RESOLVER_KINDS: dict[str, str] = {
    'prediction': 'prediction',
    'measurement': 'measurement',
    'validation': 'validation',
    'standards': 'standards',
    'robustness': 'robustness',
    'design_checkpoint': 'design_checkpoint',
}


class CadDesignComparisonRepository:
    """Native storage for immutable DesignComparisonSet rows.

    Each save is a new immutable row; set evolution is expressed through
    ``supersedes_set_id`` chains so an opened historical set keeps its exact
    alternatives forever — including after Undo history is cleared.

    Optional refs (``system_variant_id``, ``design_checkpoint_id``,
    ``as_built_ref_id``, ``view_ref`` and every ``evidence_refs`` entry) are
    validated through a typed :class:`AuthorityRefResolver` at save: each
    must resolve to a canonical authority in the same document, a ref that
    points at a hash-bearing authority must carry the equal pinned hash,
    and an id-only authority cannot claim exactness with a hash it does not
    expose.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: AuthorityRefResolver | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.ref_resolver: AuthorityRefResolver = (
            ref_resolver
            if ref_resolver is not None
            else CanonicalAuthorityRefResolver(scene_repository)
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_design_comparison_sets (
                    set_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    supersedes_set_id TEXT,
                    set_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def save_set(self, comparison_set: DesignComparisonSet) -> None:
        if self.get_set(comparison_set.set_id) is not None:
            raise DesignComparisonConflictError(
                'DesignComparisonSet ids are append-only'
            )
        if comparison_set.supersedes_set_id is not None:
            previous = self.get_set(comparison_set.supersedes_set_id)
            if previous is None:
                raise ValueError('superseded comparison set is not persisted')
            if previous.document_id != comparison_set.document_id:
                raise ValueError('superseded comparison set belongs to another document')
            if previous.revision + 1 != comparison_set.revision:
                raise ValueError('comparison set revision must continue the supersede chain')
        for alternative in comparison_set.alternatives:
            revision = self.scene_repository.get(alternative.scene_revision_id)
            if revision is None:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision that is not persisted'
                )
            if revision.content_hash != alternative.scene_content_hash:
                raise ValueError(
                    f'alternative {alternative.label} SceneRevision content hash mismatch'
                )
            if revision.document_id != comparison_set.document_id:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision of another document'
                )
            self._validate_alternative_refs(
                alternative, comparison_set.document_id
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_design_comparison_sets (
                    set_id, document_id, revision, supersedes_set_id,
                    set_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison_set.set_id,
                    comparison_set.document_id,
                    comparison_set.revision,
                    comparison_set.supersedes_set_id,
                    comparison_set.set_sha256,
                    comparison_set.created_at_utc,
                    comparison_set.model_dump_json(),
                ),
            )

    def _resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        if not self.ref_resolver.knows(kind):
            raise ValueError(
                f'no typed resolver is available for authority kind {kind}'
            )
        return self.ref_resolver.resolve(kind, ref_id, document_id)

    def _validate_alternative_refs(
        self,
        alternative: ComparisonAlternative,
        document_id: str,
    ) -> None:
        label = f'alternative {alternative.label}'
        if alternative.system_variant_id is not None:
            self._assert_resolves(
                'system_variant',
                alternative.system_variant_id,
                alternative.system_variant_sha256,
                document_id,
                label,
            )
        if alternative.design_checkpoint_id is not None:
            self._assert_resolves(
                'design_checkpoint',
                alternative.design_checkpoint_id,
                alternative.design_checkpoint_sha256,
                document_id,
                label,
            )
        if alternative.as_built_ref_id is not None:
            self._assert_resolves(
                'as_built',
                alternative.as_built_ref_id,
                alternative.as_built_ref_sha256,
                document_id,
                label,
            )
        if alternative.view_ref is not None:
            resolved = self._resolve(
                'named_view', alternative.view_ref, document_id
            )
            if resolved is None:
                raise ValueError(
                    f'{label} pins a named view that is not persisted: '
                    f'{alternative.view_ref}'
                )
            if (
                resolved.document_id is not None
                and resolved.document_id != document_id
            ):
                raise ValueError(
                    f'{label} pins a named view of another document'
                )
        for ref in alternative.evidence_refs:
            resolver_kind = _EVIDENCE_RESOLVER_KINDS.get(ref.kind)
            if resolver_kind is None:
                # 'other' is the explicit escape hatch: no canonical
                # authority exists for it by design.
                continue
            self._assert_resolves(
                resolver_kind,
                ref.ref_id,
                ref.ref_sha256,
                document_id,
                f'{label} evidence {ref.kind}:{ref.ref_id}',
            )

    def _assert_resolves(
        self,
        resolver_kind: str,
        ref_id: str,
        ref_sha256: str | None,
        document_id: str,
        label: str,
    ) -> ResolvedAuthority:
        resolved = self._resolve(resolver_kind, ref_id, document_id)
        if resolved is None:
            raise ValueError(
                f'{label} references a {resolver_kind} that does not '
                f'resolve: {ref_id}'
            )
        if (
            resolved.document_id is not None
            and resolved.document_id != document_id
        ):
            raise ValueError(
                f'{label} references a {resolver_kind} of another document'
            )
        if resolved.semantic_sha256 is not None:
            if ref_sha256 is None:
                raise ValueError(
                    f'{label} must pin the {resolver_kind} semantic hash '
                    'to claim an exact reference'
                )
            if ref_sha256 != resolved.semantic_sha256:
                raise ValueError(
                    f'{label} {resolver_kind} hash does not match the '
                    'canonical authority'
                )
        elif ref_sha256 is not None:
            raise ValueError(
                f'{label} supplies a hash the id-only {resolver_kind} '
                'authority does not expose'
            )
        return resolved

    def get_set(self, set_id: str) -> DesignComparisonSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_design_comparison_sets WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        return DesignComparisonSet.model_validate_json(row['payload_json'])

    def list_sets(
        self,
        document_id: str,
    ) -> tuple[DesignComparisonSet, ...]:
        """All saved sets, oldest first — including superseded revisions."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_design_comparison_sets
                WHERE document_id=?
                ORDER BY created_at_utc, set_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            DesignComparisonSet.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_sets(
        self,
        document_id: str,
    ) -> tuple[DesignComparisonSet, ...]:
        """Newest revision of each supersede chain, oldest chain first."""
        all_sets = self.list_sets(document_id)
        superseded = {item.supersedes_set_id for item in all_sets}
        return tuple(item for item in all_sets if item.set_id not in superseded)

    # ------------------------------------------------------------------
    # Persisted-record verification (#757 semantic audit)

    def verify_persisted_set(self, set_id: str) -> DesignComparisonSet:
        """Re-run the save-time invariants on a persisted comparison set.

        Raises ``ValueError`` when the row fails any invariant — the
        supersede chain must resolve and continue, and every alternative's
        scene pins and exact refs must re-validate.
        """
        comparison_set = self.get_set(set_id)
        if comparison_set is None:
            raise ValueError(f'comparison set {set_id} no longer resolves')
        if comparison_set.supersedes_set_id is not None:
            previous = self.get_set(comparison_set.supersedes_set_id)
            if previous is None:
                raise ValueError('superseded comparison set is not persisted')
            if previous.document_id != comparison_set.document_id:
                raise ValueError(
                    'superseded comparison set belongs to another document'
                )
            if previous.revision + 1 != comparison_set.revision:
                raise ValueError(
                    'comparison set revision must continue the supersede chain'
                )
        for alternative in comparison_set.alternatives:
            revision = self.scene_repository.get(alternative.scene_revision_id)
            if revision is None:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision '
                    'that is not persisted'
                )
            if revision.content_hash != alternative.scene_content_hash:
                raise ValueError(
                    f'alternative {alternative.label} SceneRevision content '
                    'hash mismatch'
                )
            if revision.document_id != comparison_set.document_id:
                raise ValueError(
                    f'alternative {alternative.label} pins a SceneRevision '
                    'of another document'
                )
            self._validate_alternative_refs(
                alternative, comparison_set.document_id
            )
        return comparison_set
