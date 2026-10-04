"""Append-only persistence for presentation authority (#534).

Stores immutable ``PresentationSession``, ``PresentationProposal`` and
``SynchronizedReviewBinding`` rows on the shared native cad database.
Every save re-validates that pinned scene/variant/comparison references
resolve to canonical authorities in the same document — a save into a
fresh checkout must fail closed rather than persist dangling refs.

Validation mirrors the design-comparison repository: typed refs resolve
through the canonical resolver, hash-bearing authorities must match the
pinned hash, and viewpoint entity anchors are checked against the
materialized scene the session replays.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_authority_refs import (
    AuthorityRefResolver,
    CanonicalAuthorityRefResolver,
    ResolvedAuthority,
)
from .cad_design_comparison import ComparisonAlternative, DesignComparisonSet
from .cad_presentation_session import (
    PresentationProposal,
    PresentationSession,
    SynchronizedReviewBinding,
    SynchronizedSide,
)
from .cad_repository import SceneRepository
from .cad_scene import SceneDocument
from .cad_schema import connect_sqlite, require_native_tables
from .cad_system_variant import materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository


class PresentationConflictError(ValueError):
    """A presentation save violated append-only identity rules."""


#: Canonical resolver kind for every declared evidence kind — ``other``
#: stays unresolvable by design and skips resolution entirely (same
#: convention as the comparison repository's _EVIDENCE_RESOLVER_KINDS).
_EVIDENCE_RESOLVER_KINDS: dict[str, str] = {
    'prediction': 'prediction',
    'measurement': 'measurement',
    'validation': 'validation',
    'standards': 'standards',
    'robustness': 'robustness',
    'design_checkpoint': 'design_checkpoint',
}


class CadPresentationRepository:
    """Native storage for immutable presentation rows.

    Sessions, proposals and sync bindings are append-only manifests of
    references; none of them owns a live view or an edit path back into
    engineering state.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: AuthorityRefResolver | None = None,
        variant_repository: CadSystemVariantRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.ref_resolver: AuthorityRefResolver = (
            ref_resolver
            if ref_resolver is not None
            else CanonicalAuthorityRefResolver(scene_repository)
        )
        self.variant_repository = (
            variant_repository
            if variant_repository is not None
            else CadSystemVariantRepository(scene_repository)
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_presentation_sessions',
                'cad_presentation_proposals',
                'cad_presentation_sync_bindings',
            )

    # ------------------------------------------------------------------
    # Shared ref validation

    def _resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        if not self.ref_resolver.knows(kind):
            raise ValueError(
                f'no typed resolver is available for authority kind {kind}'
            )
        return self.ref_resolver.resolve(kind, ref_id, document_id)

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

    def _validate_evidence_refs(
        self, session: PresentationSession
    ) -> None:
        label = f'session {session.label}'
        for ref in session.evidence_refs:
            resolver_kind = _EVIDENCE_RESOLVER_KINDS.get(ref.kind)
            if resolver_kind is None:
                continue
            self._assert_resolves(
                resolver_kind,
                ref.ref_id,
                ref.ref_sha256,
                session.document_id,
                f'{label} evidence {ref.kind}:{ref.ref_id}',
            )

    def _validate_variant_pin(
        self,
        document_id: str,
        variant_id: str,
        variant_sha256: str,
        label: str,
    ) -> ResolvedAuthority:
        return self._assert_resolves(
            'system_variant', variant_id, variant_sha256, document_id, label
        )

    def _validate_session_authority(
        self, session: PresentationSession
    ) -> SceneDocument:
        """Resolve the exact scene the session replays; fail closed."""
        label = f'session {session.label}'
        revision = self.scene_repository.get(session.scene_revision_id)
        if revision is None:
            raise ValueError(
                f'{label} pins a SceneRevision that is not persisted'
            )
        if revision.content_hash != session.scene_content_hash:
            raise ValueError(
                f'{label} SceneRevision content hash mismatch'
            )
        if revision.document_id != session.document_id:
            raise ValueError(
                f'{label} pins a SceneRevision of another document'
            )
        document = revision.document
        if session.system_variant_id is not None:
            self._validate_variant_pin(
                session.document_id,
                session.system_variant_id,
                session.system_variant_sha256 or '',
                label,
            )
            variant = self.variant_repository.get_variant(
                session.system_variant_id
            )
            if variant is None:
                raise ValueError(f'{label} SystemVariant does not resolve')
            if (
                variant.baseline_revision_id != revision.revision_id
                or variant.baseline_content_hash != revision.content_hash
            ):
                # The variant only replays over its own baseline; a pin
                # pair that splits them is incoherent, never displayed.
                raise ValueError(
                    f'{label} pins a SystemVariant whose baseline is not '
                    'the pinned SceneRevision'
                )
            document = materialize_system_variant(revision, variant)
        if session.comparison_set_id is not None:
            self._assert_resolves(
                'design_comparison_set',
                session.comparison_set_id,
                session.comparison_set_sha256,
                session.document_id,
                label,
            )
        return document

    def _validate_viewpoints(
        self, session: PresentationSession, document: SceneDocument
    ) -> None:
        label = f'session {session.label}'
        entity_ids = {entity.entity_id for entity in document.entities}
        for viewpoint in session.viewpoints:
            if (
                viewpoint.focus_entity_id is not None
                and viewpoint.focus_entity_id not in entity_ids
            ):
                raise ValueError(
                    f'{label} viewpoint {viewpoint.name} focuses an entity '
                    'outside the pinned scene'
                )
            if viewpoint.hidden_ids is not None:
                unknown = [
                    entity_id
                    for entity_id in viewpoint.hidden_ids
                    if entity_id not in entity_ids
                ]
                if unknown:
                    raise ValueError(
                        f'{label} viewpoint {viewpoint.name} hides entities '
                        'outside the pinned scene: ' + ', '.join(unknown)
                    )
        for annotation in session.annotations:
            if (
                annotation.entity_id is not None
                and annotation.entity_id not in entity_ids
            ):
                raise ValueError(
                    f'{label} annotation references an entity outside the '
                    'pinned scene'
                )

    # ------------------------------------------------------------------
    # Sessions

    def save_session(self, session: PresentationSession) -> None:
        if self.get_session(session.session_id) is not None:
            raise PresentationConflictError(
                'PresentationSession ids are append-only'
            )
        document = self._validate_session_authority(session)
        self._validate_viewpoints(session, document)
        self._validate_evidence_refs(session)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_presentation_sessions (
                    session_id, document_id, scene_revision_id,
                    session_sha256, status_label, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.document_id,
                    session.scene_revision_id,
                    session.session_sha256,
                    session.status_label,
                    session.created_at_utc,
                    session.model_dump_json(),
                ),
            )

    def get_session(self, session_id: str) -> PresentationSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_presentation_sessions '
                'WHERE session_id=?',
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return PresentationSession.model_validate_json(row['payload_json'])

    def find_session_by_sha(
        self, session_sha256: str
    ) -> PresentationSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_presentation_sessions '
                'WHERE session_sha256=?',
                (session_sha256,),
            ).fetchone()
        if row is None:
            return None
        return PresentationSession.model_validate_json(row['payload_json'])

    def list_sessions(
        self, document_id: str
    ) -> tuple[PresentationSession, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_presentation_sessions '
                'WHERE document_id=? ORDER BY created_at_utc, session_id',
                (document_id,),
            ).fetchall()
        return tuple(
            PresentationSession.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Proposals

    def save_proposal(self, proposal: PresentationProposal) -> None:
        if self.get_proposal(proposal.proposal_id) is not None:
            raise PresentationConflictError(
                'PresentationProposal ids are append-only'
            )
        session = self.get_session(proposal.session_id)
        if session is None:
            raise ValueError('proposal pins a session that is not persisted')
        if session.session_sha256 != proposal.session_sha256:
            raise ValueError('proposal pins a different session revision')
        if session.document_id != proposal.document_id:
            raise ValueError('proposal document does not match its session')
        if proposal.kind == 'variant_candidate':
            self._validate_variant_pin(
                proposal.document_id,
                proposal.system_variant_id or '',
                proposal.system_variant_sha256 or '',
                f'proposal {proposal.title}',
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_presentation_proposals (
                    proposal_id, session_id, document_id, kind,
                    proposal_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    proposal.session_id,
                    proposal.document_id,
                    proposal.kind,
                    proposal.proposal_sha256,
                    proposal.created_at_utc,
                    proposal.model_dump_json(),
                ),
            )

    def get_proposal(self, proposal_id: str) -> PresentationProposal | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_presentation_proposals '
                'WHERE proposal_id=?',
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return PresentationProposal.model_validate_json(row['payload_json'])

    def list_proposals(
        self,
        document_id: str | None = None,
        *,
        session_id: str | None = None,
    ) -> tuple[PresentationProposal, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if session_id is not None:
            clauses.append('session_id=?')
            params.append(session_id)
        where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_presentation_proposals '
                f'{where} ORDER BY created_at_utc, proposal_id',
                tuple(params),
            ).fetchall()
        return tuple(
            PresentationProposal.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Synchronized bindings

    def _validate_side(
        self,
        side: SynchronizedSide,
        document_id: str,
        label: str,
    ) -> None:
        revision = self.scene_repository.get(side.scene_revision_id)
        if revision is None:
            raise ValueError(
                f'{label} pins a SceneRevision that is not persisted'
            )
        if revision.content_hash != side.scene_content_hash:
            raise ValueError(f'{label} SceneRevision content hash mismatch')
        if revision.document_id != document_id:
            raise ValueError(
                f'{label} pins a SceneRevision of another document'
            )
        if side.kind == 'presentation_session':
            session = self.find_session_by_sha(side.session_sha256 or '')
            if session is None or session.session_id != side.session_id:
                raise ValueError(
                    f'{label} pins a session that is not persisted'
                )
            if session.document_id != document_id:
                raise ValueError(
                    f'{label} pins a session of another document'
                )
            if session.scene_revision_id != side.scene_revision_id:
                raise ValueError(
                    f'{label} session scene pin does not match the side'
                )
        else:
            resolved = self._resolve(
                'design_comparison_set',
                side.comparison_set_id or '',
                document_id,
            )
            if (
                resolved is None
                or resolved.semantic_sha256 != side.comparison_set_sha256
            ):
                raise ValueError(
                    f'{label} pins a comparison set that is not persisted'
                )
            from .cad_design_comparison_repository import (
                CadDesignComparisonRepository,
            )

            comparison_repo = CadDesignComparisonRepository(
                self.scene_repository, ref_resolver=self.ref_resolver
            )
            comparison_set = comparison_repo.get_set(
                side.comparison_set_id or ''
            )
            if comparison_set is None:
                raise ValueError(f'{label} comparison set does not resolve')
            alternative = comparison_set.alternative(
                side.alternative_id or ''
            )
            if (
                alternative is None
                or alternative.alternative_sha256
                != side.alternative_sha256
            ):
                raise ValueError(
                    f'{label} pins an alternative that is not persisted'
                )
            if alternative.scene_revision_id != side.scene_revision_id:
                raise ValueError(
                    f'{label} alternative scene pin does not match the side'
                )

    def save_binding(self, binding: SynchronizedReviewBinding) -> None:
        if self.get_binding(binding.binding_id) is not None:
            raise PresentationConflictError(
                'SynchronizedReviewBinding ids are append-only'
            )
        self._validate_side(binding.left, binding.document_id, 'left side')
        self._validate_side(binding.right, binding.document_id, 'right side')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_presentation_sync_bindings (
                    binding_id, document_id, binding_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.document_id,
                    binding.binding_sha256,
                    binding.created_at_utc,
                    binding.model_dump_json(),
                ),
            )

    def get_binding(
        self, binding_id: str
    ) -> SynchronizedReviewBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_presentation_sync_bindings '
                'WHERE binding_id=?',
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return SynchronizedReviewBinding.model_validate_json(
            row['payload_json']
        )

    def list_bindings(
        self, document_id: str
    ) -> tuple[SynchronizedReviewBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_presentation_sync_bindings '
                'WHERE document_id=? ORDER BY created_at_utc, binding_id',
                (document_id,),
            ).fetchall()
        return tuple(
            SynchronizedReviewBinding.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Persisted-record verification

    def verify_persisted_session(
        self, session_id: str
    ) -> PresentationSession:
        """Re-run save-time invariants on a persisted session."""
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(
                f'presentation session {session_id} no longer resolves'
            )
        document = self._validate_session_authority(session)
        self._validate_viewpoints(session, document)
        self._validate_evidence_refs(session)
        return session

    def verify_persisted_proposal(
        self, proposal_id: str
    ) -> PresentationProposal:
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError(
                f'presentation proposal {proposal_id} no longer resolves'
            )
        session = self.get_session(proposal.session_id)
        if session is None:
            raise ValueError('proposal pins a session that is not persisted')
        if session.session_sha256 != proposal.session_sha256:
            raise ValueError('proposal pins a different session revision')
        if proposal.kind == 'variant_candidate':
            self._validate_variant_pin(
                proposal.document_id,
                proposal.system_variant_id or '',
                proposal.system_variant_sha256 or '',
                f'proposal {proposal.title}',
            )
        return proposal

    def verify_persisted_binding(
        self, binding_id: str
    ) -> SynchronizedReviewBinding:
        binding = self.get_binding(binding_id)
        if binding is None:
            raise ValueError(
                f'sync binding {binding_id} no longer resolves'
            )
        self._validate_side(binding.left, binding.document_id, 'left side')
        self._validate_side(binding.right, binding.document_id, 'right side')
        return binding

    # ------------------------------------------------------------------
    # Replay helpers for surfaces

    def session_document(
        self, session: PresentationSession
    ) -> SceneDocument:
        """Materialize the exact scene the session replays."""
        revision = self.scene_repository.get(session.scene_revision_id)
        if revision is None:
            raise ValueError(
                f'session {session.label} scene revision does not resolve'
            )
        if revision.content_hash != session.scene_content_hash:
            raise ValueError('session scene content hash mismatch')
        if session.system_variant_id is None:
            return revision.document
        variant = self.variant_repository.get_variant(
            session.system_variant_id
        )
        if variant is None:
            raise ValueError('session SystemVariant does not resolve')
        if variant.baseline_revision_id != revision.revision_id:
            raise ValueError(
                'session variant baseline is not the pinned revision'
            )
        return materialize_system_variant(revision, variant)

    def session_stale(self, session: PresentationSession) -> bool:
        """True when the document head no longer is the pinned revision."""
        head = self.scene_repository.current_head(session.document_id)
        return head is None or head.revision_id != session.scene_revision_id

    def alternative_document(
        self, alternative: ComparisonAlternative
    ) -> SceneDocument:
        """Materialize the scene one comparison alternative replays."""
        revision = self.scene_repository.get(alternative.scene_revision_id)
        if revision is None:
            raise ValueError('alternative scene revision does not resolve')
        if revision.content_hash != alternative.scene_content_hash:
            raise ValueError('alternative scene content hash mismatch')
        if alternative.system_variant_id is None:
            return revision.document
        variant = self.variant_repository.get_variant(
            alternative.system_variant_id
        )
        if variant is None:
            raise ValueError('alternative SystemVariant does not resolve')
        if variant.baseline_revision_id != revision.revision_id:
            raise ValueError(
                'alternative variant baseline is not the pinned revision'
            )
        return materialize_system_variant(revision, variant)

    def side_viewpoints(
        self, side: SynchronizedSide
    ) -> tuple | None:
        """Viewpoints one side replays; None when it does not resolve."""
        if side.kind == 'presentation_session':
            session = self.get_session(side.session_id or '')
            if session is None:
                return None
            return session.ordered_viewpoints()
        # Comparison alternatives pin at most a named view; they have no
        # viewpoint sequence of their own — the step table declares 'fit'.
        return ()


__all__ = [
    'CadPresentationRepository',
    'PresentationConflictError',
]
