"""First-class Room prediction target authority (#983).

A prediction request resolves to exactly one of:

- the current saved ``SceneRevision``;
- a baseline ``SceneRevision`` plus an exact persisted ``SystemVariant``.

The variant is never applied merely to evaluate it: the materialized
proposal document becomes the effective source/receiver surface, while the
request/result identity retains the baseline revision and the exact variant
id/hash so current and proposed predictions stale independently.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument, scene_content_hash
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository


@dataclass(frozen=True, slots=True)
class RoomPredictionTarget:
    """Exact prediction target: current state or baseline + exact variant."""

    baseline_revision: SceneRevision
    document: SceneDocument
    system_variant: SystemVariant | None = None

    @property
    def variant_bound(self) -> bool:
        return self.system_variant is not None

    @property
    def system_variant_id(self) -> str | None:
        return None if self.system_variant is None else self.system_variant.variant_id

    @property
    def system_variant_sha256(self) -> str | None:
        return (
            None
            if self.system_variant is None
            else self.system_variant.variant_sha256
        )

    @property
    def input_revision(self) -> SceneRevision:
        """Effective revision view for option/source/receiver resolution.

        For a proposed target this is an in-memory view over the
        materialized proposal — never persisted — keyed by the exact variant
        hash so provider staleness checks stay explicit instead of silently
        falling back to the current scene.
        """

        if self.system_variant is None:
            return self.baseline_revision
        return SceneRevision(
            revision_id=f'proposal:{self.system_variant.variant_sha256[:24]}',
            document_id=self.document.document_id,
            parent_revision_id=self.baseline_revision.revision_id,
            created_at_utc=self.system_variant.created_at_utc,
            content_hash=scene_content_hash(self.document),
            document=self.document,
            detached=True,
            detached_reason='proposed-system-variant-prediction-target',
        )


def resolve_room_prediction_target(
    *,
    scene_repository: SceneRepository,
    revision_id: str,
    system_variant_id: str | None = None,
    variant_repository: CadSystemVariantRepository | None = None,
) -> RoomPredictionTarget:
    """Resolve an exact prediction target without applying any variant.

    ``revision_id`` is the current/baseline SceneRevision. When
    ``system_variant_id`` is given the variant must be persisted and bound
    to that exact baseline — mismatching baselines fail closed.
    """

    revision = scene_repository.get(revision_id)
    if revision is None:
        raise ValueError(f'SceneRevision {revision_id} is not persisted')
    if system_variant_id is None:
        return RoomPredictionTarget(
            baseline_revision=revision,
            document=revision.document,
        )
    if variant_repository is None:
        raise ValueError(
            'SystemVariant prediction targets require a variant repository'
        )
    variant = variant_repository.get_variant(system_variant_id)
    if variant is None:
        raise ValueError(
            f'proposed SystemVariant {system_variant_id} is not persisted'
        )
    if variant.baseline_revision_id != revision.revision_id:
        raise ValueError('SystemVariant baseline SceneRevision mismatch')
    if variant.document_id != revision.document_id:
        raise ValueError('SystemVariant baseline document mismatch')
    if variant.baseline_content_hash != revision.content_hash:
        raise ValueError(
            'SystemVariant baseline SceneRevision content hash mismatch'
        )
    return RoomPredictionTarget(
        baseline_revision=revision,
        document=materialize_system_variant(revision, variant),
        system_variant=variant,
    )


__all__ = [
    'RoomPredictionTarget',
    'resolve_room_prediction_target',
]
