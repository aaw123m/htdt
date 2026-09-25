"""Shared exact-authority resolution for persistence boundaries (#804).

Repositories that persist records referencing other authorities — a
``SceneRevision``, a ``SystemVariant``, a measurement, a piece of field
evidence — must resolve the referenced authority at save time and prove it
exists, belongs to the same project document, and carries the exact
semantic hash the caller pinned. An unresolved ID is never persisted as an
exact reference; hash-bearing authorities require the caller to pin the
hash so an ID-only ref cannot silently degrade to "probably that one".

Since #902 the per-kind resolution logic lives in the canonical typed
registry (:mod:`.cad_authority_registry`): this resolver builds (or is
given) that registry and layers the fail-closed save-time contract on top —
unresolvable kind, foreign document, missing ``ref_sha256`` on a
hash-bearing authority, or a pinned hash that disagrees all raise
``ValueError``. The canonical owner lookup itself is delegated to the
registry so every consumer — here and in
:class:`.cad_authority_refs.CanonicalAuthorityRefResolver` — shares one
resolution story.

``kind_resolvers`` remains the explicit escape hatch for authorities the
deployment knows about that have no canonical store adapter yet; each is
wrapped as a project-scoped hash-bearing adapter on the resolver's private
registry copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .cad_authority_registry import (
    AuthorityKindAdapter,
    CanonicalAuthorityRegistry,
    build_canonical_authority_registry,
)
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_field_evidence_repository import CadFieldEvidenceRepository
    from .cad_measurement_repository import CadMeasurementRepository


class AuthorityRef(BaseModel):
    """A typed reference to one exact canonical authority.

    ``ref_sha256`` pins the authority's semantic/content hash when the
    target kind is hash-bearing; it is the caller's responsibility to pin
    it — resolution rejects hash-bearing refs that omit it.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class ResolvedAuthority:
    """The canonical identity one kind resolver proved for a ref."""

    kind: str
    ref_id: str
    document_id: str
    #: The authority's semantic/content hash; ``None`` when the kind has
    #: no semantic hash (the ref is ID-bearing only).
    semantic_sha256: str | None


#: A kind resolver maps (kind, ref_id) to the canonical identity or None.
KindResolver = Callable[[str], ResolvedAuthority | None]


#: Target kinds the registry wires for every document-scoped project.
BUILTIN_TARGET_KINDS: tuple[str, ...] = (
    'scene_revision',
    'system_variant',
)


class ExactAuthorityResolver:
    """Resolves typed refs against canonical repositories, same-document only."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        field_evidence_repository: 'CadFieldEvidenceRepository | None' = None,
        measurement_repository: 'CadMeasurementRepository | None' = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
        authority_registry: CanonicalAuthorityRegistry | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.field_evidence_repository = field_evidence_repository
        if authority_registry is None:
            authority_registry = build_canonical_authority_registry(
                scene_repository,
                system_variant_repository=system_variant_repository,
                field_evidence_repository=field_evidence_repository,
                measurement_repository=measurement_repository,
            )
        self._registry = authority_registry
        for kind, kind_resolver in (kind_resolvers or {}).items():
            # Callsite escape hatch: a KindResolver is the pre-registry
            # shape, wrapped here as a project-scoped adapter returning the
            # canonical identity type.
            self._registry.register(
                AuthorityKindAdapter(
                    kind=kind,
                    scope='project',
                    hash_bearing=True,
                    owner='callsite',
                    resolve=self._adapt_kind_resolver(kind, kind_resolver),
                )
            )

    @staticmethod
    def _adapt_kind_resolver(
        kind: str, resolver: KindResolver
    ) -> Callable[[str, str], object]:
        def resolve(ref_id: str, _document_id: str):
            resolved = resolver(ref_id)
            if resolved is None:
                return None
            from .cad_authority_registry import CanonicalAuthority

            return CanonicalAuthority(
                kind=kind,
                ref_id=ref_id,
                document_id=resolved.document_id,
                semantic_sha256=resolved.semantic_sha256,
            )

        return resolve

    @property
    def registry(self) -> CanonicalAuthorityRegistry:
        return self._registry

    def resolve(
        self,
        ref: AuthorityRef,
        *,
        document_id: str,
    ) -> ResolvedAuthority:
        """Resolve ``ref`` inside one project document.

        Raises ``ValueError`` when the authority does not exist, belongs to
        another document, omits a required semantic hash, or carries a hash
        that does not match the canonical authority.
        """

        authority = self._registry.resolve(ref.kind, ref.ref_id, document_id)
        if authority is None:
            raise ValueError(
                f'unresolvable authority ref {ref.kind}:{ref.ref_id}'
            )
        if (
            authority.document_id is not None
            and authority.document_id != document_id
        ):
            raise ValueError(
                f'authority ref {ref.kind}:{ref.ref_id} belongs to a '
                'different document'
            )
        if authority.semantic_sha256 is not None:
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'hash-bearing authority {ref.kind}:{ref.ref_id} '
                    'requires ref_sha256'
                )
            if ref.ref_sha256 != authority.semantic_sha256:
                raise ValueError(
                    f'authority ref {ref.kind}:{ref.ref_id} hash mismatch'
                )
        elif ref.ref_sha256 is not None:
            raise ValueError(
                f'authority {ref.kind}:{ref.ref_id} has no semantic hash '
                'to verify'
            )
        return ResolvedAuthority(
            kind=ref.kind,
            ref_id=ref.ref_id,
            # Global authorities (document_id=None) satisfy a project-scoped
            # ref — report the requesting document as the resolution scope.
            document_id=authority.document_id or document_id,
            semantic_sha256=authority.semantic_sha256,
        )

    def resolve_scene_entity(
        self,
        revision_id: str,
        entity_id: str,
        * ,
        document_id: str,
    ) -> None:
        """Resolve an entity inside one exact pinned revision.

        The revision must exist, belong to ``document_id`` and contain
        ``entity_id`` — evidence bound to an entity of another revision or
        project never resolves. This is the explicit historical
        ``scene_entity`` at-revision form: the bare ``scene_entity`` adapter
        resolves at head, this method pins the revision.
        """

        revision = self.scene_repository.get(revision_id)
        if revision is None:
            raise ValueError(
                f'unresolvable scene revision {revision_id}'
            )
        if revision.document_id != document_id:
            raise ValueError(
                'scene revision belongs to a different document'
            )
        try:
            revision.document.entity(entity_id)
        except KeyError:
            raise ValueError(
                f'entity {entity_id} is not part of pinned revision '
                f'{revision_id}'
            ) from None


__all__ = [
    'AuthorityRef',
    'BUILTIN_TARGET_KINDS',
    'ExactAuthorityResolver',
    'KindResolver',
    'ResolvedAuthority',
]
