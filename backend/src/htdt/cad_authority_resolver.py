"""Shared exact-authority resolution for persistence boundaries (#804).

Repositories that persist records referencing other authorities — a
``SceneRevision``, a ``SystemVariant``, a measurement, a piece of field
evidence — must resolve the referenced authority at save time and prove it
exists, belongs to the same project document, and carries the exact
semantic hash the caller pinned. An unresolved ID is never persisted as an
exact reference; hash-bearing authorities require the caller to pin the
hash so an ID-only ref cannot silently degrade to "probably that one".

Built-in resolution covers the two authorities every project owns —
``scene_revision`` and ``system_variant``. Other authorities register a
kind resolver (``kind_resolvers``) returning a :class:`ResolvedAuthority`;
``field_evidence`` registers automatically when an evidence repository is
attached. A kind without a registered resolver stays unresolved — callers
get an explicit failure, not a pass-through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_field_evidence_repository import CadFieldEvidenceRepository


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


#: Target kinds with a fixed, document-scoped resolution story.
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
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.field_evidence_repository = field_evidence_repository
        self._kind_resolvers = dict(kind_resolvers or {})

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

        resolved = self._lookup(ref.kind, ref.ref_id)
        if resolved is None:
            raise ValueError(
                f'unresolvable authority ref {ref.kind}:{ref.ref_id}'
            )
        if resolved.document_id != document_id:
            raise ValueError(
                f'authority ref {ref.kind}:{ref.ref_id} belongs to a '
                'different document'
            )
        if resolved.semantic_sha256 is not None:
            if ref.ref_sha256 is None:
                raise ValueError(
                    f'hash-bearing authority {ref.kind}:{ref.ref_id} '
                    'requires ref_sha256'
                )
            if ref.ref_sha256 != resolved.semantic_sha256:
                raise ValueError(
                    f'authority ref {ref.kind}:{ref.ref_id} hash mismatch'
                )
        elif ref.ref_sha256 is not None:
            raise ValueError(
                f'authority {ref.kind}:{ref.ref_id} has no semantic hash '
                'to verify'
            )
        return resolved

    def _lookup(self, kind: str, ref_id: str) -> ResolvedAuthority | None:
        if kind == 'scene_revision':
            revision = self.scene_repository.get(ref_id)
            if revision is None:
                return None
            return ResolvedAuthority(
                kind='scene_revision',
                ref_id=ref_id,
                document_id=revision.document_id,
                semantic_sha256=revision.content_hash,
            )
        if kind == 'system_variant':
            repository = self.system_variant_repository
            if repository is None:
                return None
            variant = repository.get_variant(ref_id)
            if variant is None:
                return None
            return ResolvedAuthority(
                kind='system_variant',
                ref_id=ref_id,
                document_id=variant.document_id,
                semantic_sha256=variant.variant_sha256,
            )
        if kind == 'field_evidence':
            repository = self.field_evidence_repository
            if repository is None:
                return None
            record = repository.get_evidence(ref_id)
            if record is None:
                return None
            return ResolvedAuthority(
                kind='field_evidence',
                ref_id=ref_id,
                document_id=record.document_id,
                semantic_sha256=record.evidence_sha256,
            )
        resolver = self._kind_resolvers.get(kind)
        if resolver is None:
            return None
        return resolver(ref_id)

    def resolve_scene_entity(
        self,
        revision_id: str,
        entity_id: str,
        *,
        document_id: str,
    ) -> None:
        """Resolve an entity inside one exact pinned revision.

        The revision must exist, belong to ``document_id`` and contain
        ``entity_id`` — evidence bound to an entity of another revision or
        project never resolves.
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
