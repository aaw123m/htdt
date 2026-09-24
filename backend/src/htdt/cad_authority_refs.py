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
    #: Exact scene baseline the authority is pinned to, when the authority
    #: is scene-bound (predictions, measurements, standards evaluations,
    #: checkpoints, as-built and measured records).
    scene_revision_id: str | None = None
    scene_content_hash: str | None = None
    #: System variant the authority belongs to, when variant-scoped.
    system_variant_id: str | None = None


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
        self._assumption_repository = None
        self._action_item_repository = None
        self._measurement_repository = None
        self._inbox_repository = None
        self._prediction_repository = None
        self._standards_repository = None
        self._search_repository = None
        self._objective_repository = None
        self._roomsim_repository = None
        self._robustness_repository = None
        self._model_validation_repository = None
        self._lifecycle_repository = None
        self._quality_repository = None
        self._measured_repository = None
        self._preset_repository = None

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

    @property
    def _predictions(self):
        if self._prediction_repository is None:
            from .cad_prediction_repository import CadPredictionRepository

            self._prediction_repository = CadPredictionRepository(
                self._scene_repository
            )
        return self._prediction_repository

    @property
    def _standards(self):
        if self._standards_repository is None:
            from .cad_standards_repository import CadStandardsRepository

            self._standards_repository = CadStandardsRepository(
                self._scene_repository
            )
        return self._standards_repository

    @property
    def _search(self):
        if self._search_repository is None:
            from .cad_search_repository import CadSearchRepository

            self._search_repository = CadSearchRepository(
                self._scene_repository
            )
        return self._search_repository

    @property
    def _objectives(self):
        if self._objective_repository is None:
            from .cad_objective_repository import CadObjectiveRepository

            self._objective_repository = CadObjectiveRepository(
                self._scene_repository,
                self._search,
            )
        return self._objective_repository

    @property
    def _roomsim(self):
        if self._roomsim_repository is None:
            from .cad_roomsim_repository import CadRoomSimRepository

            self._roomsim_repository = CadRoomSimRepository(
                self._scene_repository,
                self._search,
            )
        return self._roomsim_repository

    @property
    def _robustness(self):
        if self._robustness_repository is None:
            from .cad_robustness_repository import CadRobustnessRepository

            self._robustness_repository = CadRobustnessRepository(
                scene_repository=self._scene_repository,
                search_repository=self._search,
                objective_repository=self._objectives,
            )
        return self._robustness_repository

    @property
    def _model_validations(self):
        if self._model_validation_repository is None:
            from .cad_model_validation_repository import (
                CadModelValidationRepository,
            )

            self._model_validation_repository = CadModelValidationRepository(
                self._search,
                self._roomsim,
                self._measurements,
                self._objectives,
            )
        return self._model_validation_repository

    @property
    def _as_built_lifecycle(self):
        if self._lifecycle_repository is None:
            from .cad_system_variant_lifecycle import (
                CadSystemVariantLifecycleRepository,
            )

            self._lifecycle_repository = CadSystemVariantLifecycleRepository(
                scene_repository=self._scene_repository,
                variant_repository=self._variants,
            )
        return self._lifecycle_repository

    @property
    def _measured(self):
        if self._measured_repository is None:
            from .cad_measurement_quality_repository import (
                CadMeasurementQualityRepository,
            )
            from .cad_system_variant_measured_lifecycle import (
                CadSystemVariantMeasuredLifecycleRepository,
            )

            self._quality_repository = CadMeasurementQualityRepository(
                self._measurements
            )
            self._measured_repository = (
                CadSystemVariantMeasuredLifecycleRepository(
                    scene_repository=self._scene_repository,
                    lifecycle_repository=self._as_built_lifecycle,
                    measurement_repository=self._measurements,
                    quality_repository=self._quality_repository,
                )
            )
        return self._measured_repository

    @property
    def _presets(self):
        if self._preset_repository is None:
            from .cad_operating_preset_repository import (
                CadOperatingPresetRepository,
            )

            self._preset_repository = CadOperatingPresetRepository(
                self._scene_repository
            )
        return self._preset_repository

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
            'assumption_decision',
            'action_item',
            'measurement',
            'capture_inbox_item',
            'prediction',
            'validation',
            'standards',
            'robustness',
            'as_built',
            'measured_state',
            'named_view',
            'operating_preset',
            'constraint_snapshot',
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
                scene_revision_id=checkpoint.scene_revision_id,
                scene_content_hash=checkpoint.scene_content_hash,
            )
        if kind == 'analysis_study':
            spec = self._studies.get_spec(ref_id)
            if spec is None:
                return None
            return ResolvedAuthority(
                document_id=spec.document_id,
                semantic_sha256=spec.spec_sha256,
            )
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
                scene_revision_id=record.scene_revision_id,
                scene_content_hash=record.scene_content_hash,
            )
        if kind == 'prediction':
            result = self._predictions.get(ref_id)
            if result is None:
                return None
            return ResolvedAuthority(
                document_id=result.document_id,
                semantic_sha256=result.result_sha256,
                scene_revision_id=result.scene_revision_id,
                scene_content_hash=result.scene_content_hash,
            )
        if kind == 'validation':
            record = self._model_validations.inspect(ref_id)
            if record is None:
                return None
            return ResolvedAuthority(
                document_id=record.document_id,
                semantic_sha256=record.validation_sha256,
            )
        if kind == 'standards':
            evaluation = self._standards.get_evaluation(ref_id)
            if evaluation is None:
                return None
            target = evaluation.target
            return ResolvedAuthority(
                document_id=target.document_id,
                semantic_sha256=evaluation.evaluation_sha256,
                scene_revision_id=target.scene_revision_id,
                scene_content_hash=target.scene_content_hash,
                system_variant_id=target.system_variant_id,
            )
        if kind == 'robustness':
            spec = self._robustness.get_spec(ref_id)
            if spec is None:
                return None
            return ResolvedAuthority(
                document_id=spec.document_id,
                semantic_sha256=spec.robustness_spec_sha256,
                scene_revision_id=spec.scene_revision_id,
                scene_content_hash=spec.scene_content_hash,
            )
        if kind == 'as_built':
            if ref_id.startswith('system-variant-measured:'):
                return self._resolve_measured_state(ref_id)
            record = self._as_built_lifecycle.get(ref_id)
            if record is None:
                return None
            variant = self._variants.get_variant(record.variant_id)
            return ResolvedAuthority(
                document_id=(
                    variant.document_id if variant is not None else None
                ),
                semantic_sha256=record.record_sha256,
                scene_revision_id=record.as_built_revision_id,
                scene_content_hash=record.as_built_content_hash,
                system_variant_id=record.variant_id,
            )
        if kind == 'measured_state':
            return self._resolve_measured_state(ref_id)
        if kind == 'operating_preset':
            preset = self._presets.get_preset(ref_id)
            if preset is None:
                return None
            return ResolvedAuthority(
                document_id=preset.document_id,
                semantic_sha256=preset.preset_sha256,
                scene_revision_id=preset.scene_revision_id,
                scene_content_hash=preset.scene_content_hash,
            )
        if kind == 'constraint_snapshot':
            snapshot = self._checkpoints.get_snapshot(ref_id)
            if snapshot is None:
                return None
            return ResolvedAuthority(
                document_id=snapshot.document_id,
                semantic_sha256=snapshot.snapshot_sha256,
            )
        if kind == 'named_view':
            # Named views are editor payloads: keyed by record id, id-only.
            for view in self._scene_repository.named_views(document_id):
                if view.record_id == ref_id:
                    return ResolvedAuthority(document_id=document_id)
            return None
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
        container_ids: list[str] = []
        content_hash: str | None = None
        for comparison_set in self._comparisons.list_sets(document_id):
            alternative = comparison_set.alternative(ref_id)
            if alternative is None:
                continue
            container_ids.append(comparison_set.set_id)
            content_hash = alternative.scene_content_hash
        if not container_ids:
            return None
        return ResolvedAuthority(
            document_id=document_id,
            semantic_sha256=content_hash,
            container_ids=tuple(container_ids),
        )

    def _resolve_measured_state(
        self, ref_id: str
    ) -> ResolvedAuthority | None:
        record = self._measured.get(ref_id)
        if record is None:
            return None
        return ResolvedAuthority(
            document_id=record.document_id,
            semantic_sha256=record.record_sha256,
            scene_revision_id=record.as_built_revision_id,
            scene_content_hash=record.as_built_content_hash,
            system_variant_id=record.variant_id,
        )


__all__ = [
    'AuthorityRefResolver',
    'CanonicalAuthorityRefResolver',
    'ResolvedAuthority',
]
