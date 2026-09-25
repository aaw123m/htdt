"""Canonical authority-ref resolution for exact references (#600/#795/#797/#798).

Records like :class:`DesignDecisionRecord` (#654) and
:class:`AssumptionDecision` (#620) carry *exact* typed refs — a ref is only
exact when the canonical owner repository can resolve it: the authority
exists, belongs to the same project/document when project-scoped, and its
semantic hash matches when the owner exposes one.

:class:`CanonicalAuthorityRefResolver` wires the persisted authorities that
live in the project database onto one ``resolve(kind, ref_id, document_id)``
surface so callers (repositories, template resolution, UI previews) share a
single resolution contract instead of re-deriving ownership rules per call
site. Since #902 the resolution logic itself lives in the canonical typed
registry (:mod:`.cad_authority_registry`): this class is the
return-``None``/``knows``-style façade over it that record repositories
consume, while :class:`.cad_authority_resolver.ExactAuthorityResolver`
provides the raise-on-failure save-time contract over the same registry.
There is no third resolver.

Resolution contract:

- ``resolve`` returns :class:`ResolvedAuthority` when the authority exists,
  ``None`` when the kind is known but the ref is absent;
- ``knows(kind)`` reports whether this deployment can resolve the kind at
  all — unknown kinds are *not* proof of existence and callers decide
  whether unresolvable kinds fail closed (decisions) or pass through
  (derived subjects such as ``room_surface``);
- ``semantic_sha256`` is the hash the canonical owner exposes — when it is
  set, an exact ref MUST carry the equal ``ref_sha256``; when ``None`` the
  authority is genuinely id-only;
- ``container_ids`` names the canonical containers a ref belongs to (e.g. a
  comparison alternative's owning set) so membership claims can be checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .cad_authority_registry import (
    CanonicalAuthorityRegistry,
    build_canonical_authority_registry,
)


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_repository import SceneRepository


@dataclass(frozen=True, slots=True)
class ResolvedAuthority:
    """What canonical resolution proved about one ref."""

    #: Project/document the resolved authority belongs to. ``None`` for
    #: genuinely global authorities (e.g. an unassigned Capture Inbox item).
    document_id: str | None
    #: Semantic/content hash the canonical owner exposes, or ``None`` when
    #: the authority's identity is genuinely id-only.
    semantic_sha256: str | None = None
    #: Canonical containers the ref is a member of (e.g. a comparison
    #: alternative's owning ``design_comparison_set`` ids).
    container_ids: tuple[str, ...] = ()


class AuthorityRefResolver(Protocol):
    """Resolves exact typed refs against canonical owner repositories."""

    def knows(self, kind: str) -> bool:
        """Whether this resolver can resolve refs of ``kind`` at all."""

    def resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        """Resolve ``kind``/``ref_id`` for a record owned by ``document_id``.

        ``None`` when the kind is known but no matching authority exists.
        """


class CanonicalAuthorityRefResolver:
    """Resolve ``None``-returning façade over the canonical registry.

    Constructed from the shared ``SceneRepository`` path — the registry
    lazily opens every derived repository against the same SQLite file, so
    lookups stay cheap.
    """

    #: Ref kinds the built-in registry wiring always resolves. Conditional
    #: kinds (``field_evidence``, registered only when its repository is
    #: attached) stay out of this declaration — ``knows()`` is authoritative.
    KNOWN_KINDS: frozenset[str] = frozenset(
        {
            'document',
            'scene_entity',
            'scene_revision',
            'system_variant',
            'design_comparison_set',
            'comparison_alternative',
            'design_checkpoint',
            'analysis_study',
            'intervention_study_spec',
            'intervention_alternative',
            'assumption_decision',
            'action_item',
            'measurement',
            'measurement_dataset',
            'comparison_set',
            'capture_inbox_item',
            'target_curve',
            'standards_profile',
            'video_geometry',
        }
    )

    def __init__(
        self,
        scene_repository: 'SceneRepository',
        *,
        authority_registry: CanonicalAuthorityRegistry | None = None,
    ) -> None:
        if authority_registry is None:
            authority_registry = build_canonical_authority_registry(
                scene_repository
            )
        self._registry = authority_registry

    @property
    def registry(self) -> CanonicalAuthorityRegistry:
        """The canonical registry this façade delegates to."""

        return self._registry

    def knows(self, kind: str) -> bool:
        return self._registry.knows(kind)

    def resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        authority = self._registry.resolve(kind, ref_id, document_id)
        if authority is None:
            return None
        return ResolvedAuthority(
            document_id=authority.document_id,
            semantic_sha256=authority.semantic_sha256,
            container_ids=authority.container_ids,
        )


__all__ = [
    'AuthorityRefResolver',
    'CanonicalAuthorityRefResolver',
    'ResolvedAuthority',
]
