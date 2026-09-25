"""Canonical typed authority registry (#902).

Every exact-ref resolution in HTDT answers the same question — "does the
canonical owner repository prove this typed ref?" — but that answer used to
live in two parallel frameworks (:class:`ExactAuthorityResolver` for
save-time pinning and :class:`CanonicalAuthorityRefResolver` for record
validation) plus a scattering of per-callsite ``kind_resolvers``. The
registry replaces that fan-out: each authority kind registers exactly one
:class:`AuthorityKindAdapter` declaring the kind's scope, hash policy and
owner repository, and both resolver frameworks delegate to it — so a new
authority is adopted once, not once per consumer.

Adapter contract:

- ``resolve(ref_id, document_id)`` returns :class:`CanonicalAuthority`
  when the owner proves the ref, ``None`` otherwise;
- ``document_id`` on the result names the owning project — ``None`` marks
  a genuinely global authority (a library-level standards profile, an
  unassigned inbox item) that any document-scoped ref may legitimately
  carry;
- ``semantic_sha256`` is the hash the canonical owner exposes — the ref
  must pin the same digest when the kind is ``hash_bearing``;
- ``container_ids`` names the canonical containers a ref belongs to (e.g.
  a comparison alternative's owning set) so membership claims can be
  checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_repository import SceneRepository


AuthorityScope = Literal['project', 'global', 'contextual']


@dataclass(frozen=True, slots=True)
class CanonicalAuthority:
    """The canonical identity the registry proved for one typed ref."""

    kind: str
    ref_id: str
    #: Owning project document; ``None`` for genuinely global authorities.
    document_id: str | None
    #: The owner's semantic/content hash, or ``None`` when the kind is
    #: genuinely id-only.
    semantic_sha256: str | None = None
    #: Canonical containers the ref is a member of.
    container_ids: tuple[str, ...] = ()
    #: Exact scene baseline the authority is pinned to, when the authority
    #: is scene-bound (predictions, measurements, standards evaluations,
    #: checkpoints, as-built and measured records).
    scene_revision_id: str | None = None
    scene_content_hash: str | None = None
    #: System variant the authority belongs to, when variant-scoped.
    system_variant_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorityKindAdapter:
    """How one authority kind resolves against its canonical owner."""

    kind: str
    #: ``project``: owned by one document; ``global``: shared across
    #: projects; ``contextual``: the record itself decides scope.
    scope: AuthorityScope
    #: Whether the owner exposes a semantic hash refs must pin.
    hash_bearing: bool
    #: Human-readable owner repository (metadata for audits/diagnostics).
    owner: str
    resolve: Callable[[str, str], CanonicalAuthority | None]


class CanonicalAuthorityRegistry:
    """kind → :class:`AuthorityKindAdapter`; the single resolution surface."""

    def __init__(self) -> None:
        self._adapters: dict[str, AuthorityKindAdapter] = {}

    def register(self, adapter: AuthorityKindAdapter) -> None:
        self._adapters[adapter.kind] = adapter

    def knows(self, kind: str) -> bool:
        """Whether the registry can prove refs of ``kind`` at all."""

        return kind in self._adapters

    def adapter(self, kind: str) -> AuthorityKindAdapter | None:
        return self._adapters.get(kind)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def resolve(
        self, kind: str, ref_id: str, document_id: str
    ) -> CanonicalAuthority | None:
        """Resolve ``kind``/``ref_id`` for a record owned by ``document_id``.

        ``None`` when the kind is unregistered or the owner cannot prove
        the ref — unknown kinds are not proof of absence, callers decide
        whether unresolvable kinds fail closed.
        """

        adapter = self._adapters.get(kind)
        if adapter is None:
            return None
        return adapter.resolve(ref_id, document_id)


def build_canonical_authority_registry(
    scene_repository: 'SceneRepository',
    *,
    system_variant_repository=None,
    design_comparison_repository=None,
    design_checkpoint_repository=None,
    intervention_study_repository=None,
    analysis_study_repository=None,
    assumption_decision_repository=None,
    action_item_repository=None,
    measurement_repository=None,
    capture_inbox_repository=None,
    field_evidence_repository=None,
    target_profile_repository=None,
    standards_repository=None,
    video_geometry_repository=None,
    extra_adapters=(),
) -> CanonicalAuthorityRegistry:
    """Wire every persisted authority kind into one registry.

    Owner repositories default to lazily-instantiated canonical stores over
    the shared ``scene_repository`` path; tests and alternate compositions
    inject theirs through the keyword parameters.
    """

    repositories: dict[str, object] = {}

    def repo(key: str, factory):
        candidate = repositories.get(key)
        if candidate is None:
            candidate = factory()
            repositories[key] = candidate
        return candidate

    def variants():
        if system_variant_repository is not None:
            return system_variant_repository
        from .cad_system_variant_repository import CadSystemVariantRepository

        return repo(
            'variants', lambda: CadSystemVariantRepository(scene_repository)
        )

    def comparisons():
        if design_comparison_repository is not None:
            return design_comparison_repository
        from .cad_design_comparison_repository import (
            CadDesignComparisonRepository,
        )

        return repo(
            'comparisons',
            lambda: CadDesignComparisonRepository(scene_repository),
        )

    def checkpoints():
        if design_checkpoint_repository is not None:
            return design_checkpoint_repository
        from .cad_design_checkpoint_repository import (
            CadDesignCheckpointRepository,
        )

        return repo(
            'checkpoints',
            lambda: CadDesignCheckpointRepository(scene_repository),
        )

    def interventions():
        if intervention_study_repository is not None:
            return intervention_study_repository
        from .cad_intervention_study_repository import (
            CadInterventionStudyRepository,
        )

        return repo(
            'interventions',
            lambda: CadInterventionStudyRepository(
                scene_repository=scene_repository
            ),
        )

    def analyses():
        if analysis_study_repository is not None:
            return analysis_study_repository
        from .cad_analysis_study_repository import CadAnalysisStudyRepository

        return repo(
            'analyses',
            lambda: CadAnalysisStudyRepository(scene_repository),
        )

    def assumptions():
        if assumption_decision_repository is not None:
            return assumption_decision_repository
        from .cad_assumption_decision_repository import (
            CadAssumptionDecisionRepository,
        )

        return repo(
            'assumptions',
            lambda: CadAssumptionDecisionRepository(scene_repository),
        )

    def action_items():
        if action_item_repository is not None:
            return action_item_repository
        from .cad_action_item_repository import CadActionItemRepository

        return repo(
            'action_items',
            lambda: CadActionItemRepository(scene_repository),
        )

    def measurements():
        if measurement_repository is not None:
            return measurement_repository
        from .cad_measurement_repository import CadMeasurementRepository

        return repo(
            'measurements',
            lambda: CadMeasurementRepository(scene_repository),
        )

    def inbox():
        if capture_inbox_repository is not None:
            return capture_inbox_repository
        from .capture_inbox import CaptureInboxRepository

        return repo(
            'inbox', lambda: CaptureInboxRepository(scene_repository)
        )

    def target_profiles():
        if target_profile_repository is not None:
            return target_profile_repository
        from .cad_target_profile_repository import (
            CadTargetProfileRepository,
        )

        return repo(
            'target_profiles',
            lambda: CadTargetProfileRepository(scene_repository),
        )

    def standards():
        if standards_repository is not None:
            return standards_repository
        from .cad_standards_repository import CadStandardsRepository

        return repo(
            'standards', lambda: CadStandardsRepository(scene_repository)
        )

    def video_geometry():
        if video_geometry_repository is not None:
            return video_geometry_repository
        from .cad_video_geometry_repository import CadVideoGeometryRepository

        return repo(
            'video_geometry',
            lambda: CadVideoGeometryRepository(scene_repository),
        )

    def predictions():
        from .cad_prediction_repository import CadPredictionRepository

        return repo(
            'predictions',
            lambda: CadPredictionRepository(scene_repository),
        )

    def search():
        from .cad_search_repository import CadSearchRepository

        return repo('search', lambda: CadSearchRepository(scene_repository))

    def objectives():
        from .cad_objective_repository import CadObjectiveRepository

        return repo(
            'objectives',
            lambda: CadObjectiveRepository(scene_repository, search()),
        )

    def roomsim():
        from .cad_roomsim_repository import CadRoomSimRepository

        return repo(
            'roomsim',
            lambda: CadRoomSimRepository(scene_repository, search()),
        )

    def robustness_repo():
        from .cad_robustness_repository import CadRobustnessRepository

        return repo(
            'robustness',
            lambda: CadRobustnessRepository(
                scene_repository=scene_repository,
                search_repository=search(),
                objective_repository=objectives(),
            ),
        )

    def model_validations():
        from .cad_model_validation_repository import (
            CadModelValidationRepository,
        )

        return repo(
            'model_validations',
            lambda: CadModelValidationRepository(
                search(), roomsim(), measurements(), objectives()
            ),
        )

    def as_built_lifecycle():
        from .cad_system_variant_lifecycle import (
            CadSystemVariantLifecycleRepository,
        )

        return repo(
            'as_built_lifecycle',
            lambda: CadSystemVariantLifecycleRepository(
                scene_repository=scene_repository,
                variant_repository=variants(),
            ),
        )

    def measured_states():
        from .cad_system_variant_measured_lifecycle import (
            CadSystemVariantMeasuredLifecycleRepository,
        )
        from .cad_measurement_quality_repository import (
            CadMeasurementQualityRepository,
        )

        return repo(
            'measured_states',
            lambda: CadSystemVariantMeasuredLifecycleRepository(
                scene_repository=scene_repository,
                lifecycle_repository=as_built_lifecycle(),
                measurement_repository=measurements(),
                quality_repository=repo(
                    'measurement_quality',
                    lambda: CadMeasurementQualityRepository(measurements()),
                ),
            ),
        )

    def presets():
        from .cad_operating_preset_repository import (
            CadOperatingPresetRepository,
        )

        return repo(
            'presets',
            lambda: CadOperatingPresetRepository(scene_repository),
        )

    registry = CanonicalAuthorityRegistry()

    def register(
        kind: str,
        scope: AuthorityScope,
        hash_bearing: bool,
        owner: str,
        resolve: Callable[[str, str], CanonicalAuthority | None],
    ) -> None:
        registry.register(
            AuthorityKindAdapter(
                kind=kind,
                scope=scope,
                hash_bearing=hash_bearing,
                owner=owner,
                resolve=resolve,
            )
        )

    def document_resolve(ref_id: str, _document_id: str):
        # A document's head moves — identity is id-only.
        if scene_repository.latest(ref_id) is None:
            return None
        return CanonicalAuthority(
            kind='document', ref_id=ref_id, document_id=ref_id
        )

    register('document', 'project', False, 'SceneRepository', document_resolve)

    def scene_entity_resolve(ref_id: str, document_id: str):
        revision = scene_repository.latest(document_id)
        if revision is None:
            return None
        if not any(
            entity.entity_id == ref_id
            for entity in revision.document.entities
        ):
            return None
        return CanonicalAuthority(
            kind='scene_entity', ref_id=ref_id, document_id=document_id
        )

    register(
        'scene_entity', 'project', False, 'SceneRepository', scene_entity_resolve
    )

    def scene_revision_resolve(ref_id: str, _document_id: str):
        revision = scene_repository.get(ref_id)
        if revision is None:
            return None
        return CanonicalAuthority(
            kind='scene_revision',
            ref_id=ref_id,
            document_id=revision.document_id,
            semantic_sha256=revision.content_hash,
        )

    register(
        'scene_revision',
        'project',
        True,
        'SceneRepository',
        scene_revision_resolve,
    )

    def system_variant_resolve(ref_id: str, _document_id: str):
        variant = variants().get_variant(ref_id)
        if variant is None:
            return None
        return CanonicalAuthority(
            kind='system_variant',
            ref_id=ref_id,
            document_id=variant.document_id,
            semantic_sha256=variant.variant_sha256,
        )

    register(
        'system_variant',
        'project',
        True,
        'CadSystemVariantRepository',
        system_variant_resolve,
    )

    def comparison_set_resolve(ref_id: str, _document_id: str):
        comparison_set = comparisons().get_set(ref_id)
        if comparison_set is None:
            return None
        return CanonicalAuthority(
            kind='design_comparison_set',
            ref_id=ref_id,
            document_id=comparison_set.document_id,
            semantic_sha256=comparison_set.set_sha256,
        )

    register(
        'design_comparison_set',
        'project',
        True,
        'CadDesignComparisonRepository',
        comparison_set_resolve,
    )

    def comparison_alternative_resolve(ref_id: str, document_id: str):
        # An alternative_id is set-local: the same id can live in several
        # sets. The semantic identity is the alternative's own
        # ``alternative_sha256`` — when colliding matches disagree the ref
        # is ambiguous and must fail closed rather than pick one hash.
        container_ids: list[str] = []
        hashes: set[str] = set()
        for comparison_set in comparisons().list_sets(document_id):
            alternative = comparison_set.alternative(ref_id)
            if alternative is None:
                continue
            container_ids.append(comparison_set.set_id)
            hashes.add(alternative.alternative_sha256)
        if not container_ids or len(hashes) > 1:
            return None
        return CanonicalAuthority(
            kind='comparison_alternative',
            ref_id=ref_id,
            document_id=document_id,
            semantic_sha256=hashes.pop(),
            container_ids=tuple(container_ids),
        )

    register(
        'comparison_alternative',
        'project',
        True,
        'CadDesignComparisonRepository',
        comparison_alternative_resolve,
    )

    def checkpoint_resolve(ref_id: str, _document_id: str):
        checkpoint = checkpoints().get_checkpoint(ref_id)
        if checkpoint is None:
            return None
        return CanonicalAuthority(
            kind='design_checkpoint',
            ref_id=ref_id,
            document_id=checkpoint.document_id,
            semantic_sha256=checkpoint.checkpoint_sha256,
        )

    register(
        'design_checkpoint',
        'project',
        True,
        'CadDesignCheckpointRepository',
        checkpoint_resolve,
    )

    def analysis_study_resolve(ref_id: str, _document_id: str):
        # #871: 'analysis_study' names the #594 AnalysisStudy artifact,
        # never a #519 InterventionStudySpec — the two families share no
        # id space, so a colliding id cannot resolve the wrong owner.
        study = analyses().get_study(ref_id)
        if study is None:
            return None
        return CanonicalAuthority(
            kind='analysis_study',
            ref_id=ref_id,
            document_id=study.document_id,
            semantic_sha256=study.study_sha256,
        )

    register(
        'analysis_study',
        'project',
        True,
        'CadAnalysisStudyRepository',
        analysis_study_resolve,
    )

    def intervention_spec_resolve(ref_id: str, _document_id: str):
        spec = interventions().get_spec(ref_id)
        if spec is None:
            return None
        return CanonicalAuthority(
            kind='intervention_study_spec',
            ref_id=ref_id,
            document_id=spec.document_id,
            semantic_sha256=spec.spec_sha256,
        )

    register(
        'intervention_study_spec',
        'project',
        True,
        'CadInterventionStudyRepository',
        intervention_spec_resolve,
    )

    def intervention_alternative_resolve(ref_id: str, document_id: str):
        # Alternative ids are only unique inside their spec — resolve
        # through the document's specs and report the owning spec as the
        # container so membership claims can be checked.
        for spec in interventions().list_specs(document_id):
            for alternative in interventions().list_alternatives(
                spec.spec_id
            ):
                if alternative.alternative_id != ref_id:
                    continue
                return CanonicalAuthority(
                    kind='intervention_alternative',
                    ref_id=ref_id,
                    document_id=spec.document_id,
                    semantic_sha256=alternative.alternative_sha256,
                    container_ids=(spec.spec_id,),
                )
        return None

    register(
        'intervention_alternative',
        'project',
        True,
        'CadInterventionStudyRepository',
        intervention_alternative_resolve,
    )

    def assumption_resolve(ref_id: str, _document_id: str):
        decision = assumptions().get_decision(ref_id)
        if decision is None:
            return None
        return CanonicalAuthority(
            kind='assumption_decision',
            ref_id=ref_id,
            document_id=decision.document_id,
            semantic_sha256=decision.decision_sha256,
        )

    register(
        'assumption_decision',
        'project',
        True,
        'CadAssumptionDecisionRepository',
        assumption_resolve,
    )

    def action_item_resolve(ref_id: str, _document_id: str):
        action = action_items().get(ref_id)
        if action is None:
            return None
        return CanonicalAuthority(
            kind='action_item',
            ref_id=ref_id,
            document_id=action.document_id,
            semantic_sha256=action.action_sha256,
        )

    register(
        'action_item',
        'project',
        True,
        'CadActionItemRepository',
        action_item_resolve,
    )

    def measurement_resolve(ref_id: str, _document_id: str):
        record = measurements().get_measurement(ref_id)
        if record is None:
            return None
        from .cad_measurement_quality import measurement_sha256

        return CanonicalAuthority(
            kind='measurement',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=measurement_sha256(record),
            scene_revision_id=record.scene_revision_id,
            scene_content_hash=record.scene_content_hash,
        )

    register(
        'measurement',
        'project',
        True,
        'CadMeasurementRepository',
        measurement_resolve,
    )

    def measurement_dataset_resolve(ref_id: str, _document_id: str):
        dataset = measurements().get_dataset(ref_id)
        if dataset is None:
            return None
        # A dataset's project scope comes through its owning measurement.
        record = measurements().get_measurement(dataset.measurement_id)
        if record is None:
            return None
        return CanonicalAuthority(
            kind='measurement_dataset',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=dataset.dataset_sha256,
        )

    register(
        'measurement_dataset',
        'project',
        True,
        'CadMeasurementRepository',
        measurement_dataset_resolve,
    )

    def comparison_authority_resolve(ref_id: str, _document_id: str):
        comparison = measurements().get_comparison(ref_id)
        if comparison is None:
            return None
        return CanonicalAuthority(
            kind='comparison_set',
            ref_id=ref_id,
            document_id=comparison.document_id,
            semantic_sha256=comparison.comparison_sha256,
        )

    register(
        'comparison_set',
        'project',
        True,
        'CadMeasurementRepository',
        comparison_authority_resolve,
    )

    def capture_inbox_resolve(ref_id: str, _document_id: str):
        # Refs name the public inbox_item_id; the store keys rows by
        # lineage_digest, so resolve through a scoped scan.
        item = next(
            (
                candidate
                for candidate in inbox().list_items()
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
            return CanonicalAuthority(
                kind='capture_inbox_item', ref_id=ref_id, document_id=None
            )
        return CanonicalAuthority(
            kind='capture_inbox_item', ref_id=ref_id, document_id=item.scope
        )

    register(
        'capture_inbox_item',
        'contextual',
        False,
        'CaptureInboxRepository',
        capture_inbox_resolve,
    )

    if field_evidence_repository is not None:

        def field_evidence_resolve(ref_id: str, _document_id: str):
            record = field_evidence_repository.get_evidence(ref_id)
            if record is None:
                return None
            return CanonicalAuthority(
                kind='field_evidence',
                ref_id=ref_id,
                document_id=record.document_id,
                semantic_sha256=record.evidence_sha256,
            )

        register(
            'field_evidence',
            'project',
            True,
            'CadFieldEvidenceRepository',
            field_evidence_resolve,
        )

    def target_curve_resolve(ref_id: str, document_id: str):
        # ``ref_id`` names the profile; the exact authority is its latest
        # persisted version inside this document.
        profile = next(
            (
                candidate
                for candidate in reversed(
                    target_profiles().list_profiles(document_id)
                )
                if candidate.profile_id == ref_id
            ),
            None,
        )
        if profile is None:
            return None
        return CanonicalAuthority(
            kind='target_curve',
            ref_id=ref_id,
            document_id=document_id,
            semantic_sha256=profile.semantic_sha256,
        )

    register(
        'target_curve',
        'project',
        True,
        'CadTargetProfileRepository',
        target_curve_resolve,
    )

    def standards_profile_resolve(ref_id: str, _document_id: str):
        # Profiles are library-level: no document scope. ``ref_id`` names
        # the profile; the exact authority is its latest version.
        versions = standards().list_profile_versions(ref_id)
        if not versions:
            return None
        latest = versions[-1]
        return CanonicalAuthority(
            kind='standards_profile',
            ref_id=ref_id,
            document_id=None,
            semantic_sha256=latest.profile_semantic_hash,
        )

    register(
        'standards_profile',
        'global',
        True,
        'CadStandardsRepository',
        standards_profile_resolve,
    )

    def video_geometry_resolve(ref_id: str, _document_id: str):
        # Projector specifications are library-level: no document scope.
        # ``ref_id`` names the specification; the exact authority is its
        # latest persisted version.
        spec = next(
            (
                candidate
                for candidate in reversed(
                    video_geometry().list_projector_specifications()
                )
                if candidate.specification_id == ref_id
            ),
            None,
        )
        if spec is None:
            return None
        return CanonicalAuthority(
            kind='video_geometry',
            ref_id=ref_id,
            document_id=None,
            semantic_sha256=spec.specification_sha256,
        )

    register(
        'video_geometry',
        'global',
        True,
        'CadVideoGeometryRepository',
        video_geometry_resolve,
    )

    # --- Evaluation/lifecycle authorities ---------------------------------

    def prediction_resolve(ref_id: str, _document_id: str):
        result = predictions().get(ref_id)
        if result is None:
            return None
        return CanonicalAuthority(
            kind='prediction',
            ref_id=ref_id,
            document_id=result.document_id,
            semantic_sha256=result.result_sha256,
            scene_revision_id=result.scene_revision_id,
            scene_content_hash=result.scene_content_hash,
        )

    register(
        'prediction',
        'project',
        True,
        'CadPredictionRepository',
        prediction_resolve,
    )

    def validation_resolve(ref_id: str, _document_id: str):
        record = model_validations().inspect(ref_id)
        if record is None:
            return None
        return CanonicalAuthority(
            kind='validation',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=record.validation_sha256,
        )

    register(
        'validation',
        'project',
        True,
        'CadModelValidationRepository',
        validation_resolve,
    )

    def standards_evaluation_resolve(ref_id: str, _document_id: str):
        evaluation = standards().get_evaluation(ref_id)
        if evaluation is None:
            return None
        target = evaluation.target
        return CanonicalAuthority(
            kind='standards',
            ref_id=ref_id,
            document_id=target.document_id,
            semantic_sha256=evaluation.evaluation_sha256,
            scene_revision_id=target.scene_revision_id,
            scene_content_hash=target.scene_content_hash,
            system_variant_id=target.system_variant_id,
        )

    register(
        'standards',
        'project',
        True,
        'CadStandardsRepository',
        standards_evaluation_resolve,
    )

    def robustness_resolve(ref_id: str, _document_id: str):
        spec = robustness_repo().get_spec(ref_id)
        if spec is None:
            return None
        return CanonicalAuthority(
            kind='robustness',
            ref_id=ref_id,
            document_id=spec.document_id,
            semantic_sha256=spec.robustness_spec_sha256,
            scene_revision_id=spec.scene_revision_id,
            scene_content_hash=spec.scene_content_hash,
        )

    register(
        'robustness',
        'project',
        True,
        'CadRobustnessRepository',
        robustness_resolve,
    )

    def measured_state_authority(ref_id: str):
        record = measured_states().get(ref_id)
        if record is None:
            return None
        return CanonicalAuthority(
            kind='measured_state',
            ref_id=ref_id,
            document_id=record.document_id,
            semantic_sha256=record.record_sha256,
            scene_revision_id=record.as_built_revision_id,
            scene_content_hash=record.as_built_content_hash,
            system_variant_id=record.variant_id,
        )

    def as_built_resolve(ref_id: str, _document_id: str):
        if ref_id.startswith('system-variant-measured:'):
            return measured_state_authority(ref_id)
        record = as_built_lifecycle().get(ref_id)
        if record is None:
            return None
        variant = variants().get_variant(record.variant_id)
        return CanonicalAuthority(
            kind='as_built',
            ref_id=ref_id,
            document_id=(
                variant.document_id if variant is not None else None
            ),
            semantic_sha256=record.record_sha256,
            scene_revision_id=record.as_built_revision_id,
            scene_content_hash=record.as_built_content_hash,
            system_variant_id=record.variant_id,
        )

    register(
        'as_built',
        'project',
        True,
        'CadSystemVariantLifecycleRepository',
        as_built_resolve,
    )

    register(
        'measured_state',
        'project',
        True,
        'CadSystemVariantMeasuredLifecycleRepository',
        lambda ref_id, _document_id: measured_state_authority(ref_id),
    )

    def operating_preset_resolve(ref_id: str, _document_id: str):
        preset = presets().get_preset(ref_id)
        if preset is None:
            return None
        return CanonicalAuthority(
            kind='operating_preset',
            ref_id=ref_id,
            document_id=preset.document_id,
            semantic_sha256=preset.preset_sha256,
            scene_revision_id=preset.scene_revision_id,
            scene_content_hash=preset.scene_content_hash,
        )

    register(
        'operating_preset',
        'project',
        True,
        'CadOperatingPresetRepository',
        operating_preset_resolve,
    )

    def constraint_snapshot_resolve(ref_id: str, _document_id: str):
        snapshot = checkpoints().get_snapshot(ref_id)
        if snapshot is None:
            return None
        return CanonicalAuthority(
            kind='constraint_snapshot',
            ref_id=ref_id,
            document_id=snapshot.document_id,
            semantic_sha256=snapshot.snapshot_sha256,
        )

    register(
        'constraint_snapshot',
        'project',
        True,
        'CadDesignCheckpointRepository',
        constraint_snapshot_resolve,
    )

    def named_view_resolve(ref_id: str, document_id: str):
        # Named views are editor payloads: keyed by record id, id-only.
        for view in scene_repository.named_views(document_id):
            if view.record_id == ref_id:
                return CanonicalAuthority(
                    kind='named_view',
                    ref_id=ref_id,
                    document_id=document_id,
                )
        return None

    register(
        'named_view',
        'project',
        False,
        'SceneRepository',
        named_view_resolve,
    )

    for adapter in extra_adapters:
        registry.register(adapter)

    return registry


__all__ = [
    'AuthorityKindAdapter',
    'AuthorityScope',
    'CanonicalAuthority',
    'CanonicalAuthorityRegistry',
    'build_canonical_authority_registry',
]
