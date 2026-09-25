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
site.

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
from typing import Protocol


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
    """Resolver over the project database's canonical owner repositories.

    Constructed lazily from the shared ``SceneRepository`` path — every
    derived repository opens the same SQLite file, so lookups stay cheap.
    """

    def __init__(self, scene_repository) -> None:
        self._scene_repository = scene_repository
        self._variant_repository = None
        self._comparison_repository = None
        self._checkpoint_repository = None
        self._study_repository = None
        self._analysis_repository = None
        self._assumption_repository = None
        self._action_item_repository = None
        self._measurement_repository = None
        self._inbox_repository = None

    # -- lazy repository accessors -------------------------------------

    @property
    def _variants(self):
        if self._variant_repository is None:
            from .cad_system_variant_repository import (
                CadSystemVariantRepository,
            )

            self._variant_repository = CadSystemVariantRepository(
                self._scene_repository
            )
        return self._variant_repository

    @property
    def _comparisons(self):
        if self._comparison_repository is None:
            from .cad_design_comparison_repository import (
                CadDesignComparisonRepository,
            )

            self._comparison_repository = CadDesignComparisonRepository(
                self._scene_repository
            )
        return self._comparison_repository

    @property
    def _checkpoints(self):
        if self._checkpoint_repository is None:
            from .cad_design_checkpoint_repository import (
                CadDesignCheckpointRepository,
            )

            self._checkpoint_repository = CadDesignCheckpointRepository(
                self._scene_repository
            )
        return self._checkpoint_repository

    @property
    def _studies(self):
        if self._study_repository is None:
            from .cad_intervention_study_repository import (
                CadInterventionStudyRepository,
            )

            self._study_repository = CadInterventionStudyRepository(
                scene_repository=self._scene_repository
            )
        return self._study_repository

    @property
    def _analysis(self):
        if self._analysis_repository is None:
            from .cad_analysis_study_repository import (
                CadAnalysisStudyRepository,
            )

            self._analysis_repository = CadAnalysisStudyRepository(
                self._scene_repository
            )
        return self._analysis_repository

    @property
    def _assumptions(self):
        if self._assumption_repository is None:
            from .cad_assumption_decision_repository import (
                CadAssumptionDecisionRepository,
            )

            self._assumption_repository = CadAssumptionDecisionRepository(
                self._scene_repository
            )
        return self._assumption_repository

    @property
    def _action_items(self):
        if self._action_item_repository is None:
            from .cad_action_item_repository import CadActionItemRepository

            self._action_item_repository = CadActionItemRepository(
                self._scene_repository
            )
        return self._action_item_repository

    @property
    def _measurements(self):
        if self._measurement_repository is None:
            from .cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self._scene_repository
            )
        return self._measurement_repository

    @property
    def _inbox(self):
        if self._inbox_repository is None:
            from .capture_inbox import CaptureInboxRepository

            self._inbox_repository = CaptureInboxRepository(
                self._scene_repository
            )
        return self._inbox_repository

    # -- protocol -------------------------------------------------------

    #: Ref kinds this resolver can prove against canonical persistence.
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
            'capture_inbox_item',
        }
    )

    def knows(self, kind: str) -> bool:
        return kind in self.KNOWN_KINDS

    def resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        if kind == 'document':
            # A document's head moves — identity is id-only.
            if self._scene_repository.latest(ref_id) is None:
                return None
            return ResolvedAuthority(document_id=ref_id)
        if kind == 'scene_entity':
            revision = self._scene_repository.latest(document_id)
            if revision is None:
                return None
            if not any(
                entity.entity_id == ref_id
                for entity in revision.document.entities
            ):
                return None
            return ResolvedAuthority(document_id=document_id)
        if kind == 'scene_revision':
            revision = self._scene_repository.get(ref_id)
            if revision is None:
                return None
            return ResolvedAuthority(
                document_id=revision.document_id,
                semantic_sha256=revision.content_hash,
            )
        if kind == 'system_variant':
            variant = self._variants.get_variant(ref_id)
            if variant is None:
                return None
            return ResolvedAuthority(
                document_id=variant.document_id,
                semantic_sha256=variant.variant_sha256,
            )
        if kind == 'design_comparison_set':
            comparison_set = self._comparisons.get_set(ref_id)
            if comparison_set is None:
                return None
            return ResolvedAuthority(
                document_id=comparison_set.document_id,
                semantic_sha256=comparison_set.set_sha256,
            )
        if kind == 'comparison_alternative':
            return self._resolve_comparison_alternative(ref_id, document_id)
        if kind == 'design_checkpoint':
            checkpoint = self._checkpoints.get_checkpoint(ref_id)
            if checkpoint is None:
                return None
            return ResolvedAuthority(
                document_id=checkpoint.document_id,
                semantic_sha256=checkpoint.checkpoint_sha256,
            )
        if kind == 'analysis_study':
            # #871: 'analysis_study' names the #594 AnalysisStudy artifact,
            # never a #519 InterventionStudySpec — the two families share no
            # id space, so a colliding id cannot resolve the wrong owner.
            study = self._analysis.get_study(ref_id)
            if study is None:
                return None
            return ResolvedAuthority(
                document_id=study.document_id,
                semantic_sha256=study.study_sha256,
            )
        if kind == 'intervention_study_spec':
            spec = self._studies.get_spec(ref_id)
            if spec is None:
                return None
            return ResolvedAuthority(
                document_id=spec.document_id,
                semantic_sha256=spec.spec_sha256,
            )
        if kind == 'intervention_alternative':
            # Alternative ids are only unique inside their spec — resolve
            # through the document's specs and report the owning spec as the
            # container so membership claims can be checked.
            for spec in self._studies.list_specs(document_id):
                for alternative in self._studies.list_alternatives(spec.spec_id):
                    if alternative.alternative_id != ref_id:
                        continue
                    return ResolvedAuthority(
                        document_id=spec.document_id,
                        semantic_sha256=alternative.alternative_sha256,
                        container_ids=(spec.spec_id,),
                    )
            return None
        if kind == 'assumption_decision':
            decision = self._assumptions.get_decision(ref_id)
            if decision is None:
                return None
            return ResolvedAuthority(
                document_id=decision.document_id,
                semantic_sha256=decision.decision_sha256,
            )
        if kind == 'action_item':
            action = self._action_items.get(ref_id)
            if action is None:
                return None
            return ResolvedAuthority(
                document_id=action.document_id,
                semantic_sha256=action.action_sha256,
            )
        if kind == 'measurement':
            record = self._measurements.get_measurement(ref_id)
            if record is None:
                return None
            from .cad_measurement_quality import measurement_sha256

            return ResolvedAuthority(
                document_id=record.document_id,
                semantic_sha256=measurement_sha256(record),
            )
        if kind == 'capture_inbox_item':
            # Refs name the public inbox_item_id; the store keys rows by
            # lineage_digest, so resolve through a scoped scan.
            item = next(
                (
                    candidate
                    for candidate in self._inbox.list_items()
                    if candidate.inbox_item_id == ref_id
                    or candidate.lineage_digest == ref_id
                ),
                None,
            )
            if item is None:
                return None
            # Inbox scope is the assigned project/document id or the shared
            # unassigned scope — unassigned items stay global and cannot
            # satisfy a project-scoped exact ref.
            if item.scope == 'capture-inbox-unassigned':
                return ResolvedAuthority(document_id=None)
            return ResolvedAuthority(document_id=item.scope)
        return None

    # -- helpers ---------------------------------------------------------

    def _resolve_comparison_alternative(
        self, ref_id: str, document_id: str
    ) -> ResolvedAuthority | None:
        # An alternative_id is set-local: the same id can live in several
        # sets. The semantic identity is the alternative's own
        # ``alternative_sha256`` — when colliding matches disagree the ref
        # is ambiguous and must fail closed rather than pick one hash.
        container_ids: list[str] = []
        hashes: set[str] = set()
        for comparison_set in self._comparisons.list_sets(document_id):
            alternative = comparison_set.alternative(ref_id)
            if alternative is None:
                continue
            container_ids.append(comparison_set.set_id)
            hashes.add(alternative.alternative_sha256)
        if not container_ids or len(hashes) > 1:
            return None
        return ResolvedAuthority(
            document_id=document_id,
            semantic_sha256=hashes.pop(),
            container_ids=tuple(container_ids),
        )


__all__ = [
    'AuthorityRefResolver',
    'CanonicalAuthorityRefResolver',
    'ResolvedAuthority',
]
