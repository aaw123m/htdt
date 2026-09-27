"""AcousticTreatment product workflow (#985).

The canonical product path for treatment: author an exact definition,
place it against an exact host surface, and create named baseline/A/B
design comparisons that bind evaluated prediction evidence — never a
displayed benefit that was never evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Sequence

from .cad_acoustic_treatment import (
    AcousticTreatmentDefinition,
    AcousticTreatmentPlacement,
    TreatmentCoverage,
    TreatmentDimensions,
    TreatmentEvidenceSubject,
    TreatmentLayer,
    TreatmentProvenance,
    TreatmentType,
    build_acoustic_treatment_definition,
    build_treatment_evidence_authority,
    build_treatment_placement,
    evaluate_treatment_prediction_capability,
)
from .cad_acoustic_treatment_comparison import (
    CadAcousticTreatmentComparisonRepository,
    TreatmentCandidateOutcome,
    TreatmentComparisonOutcome,
    TreatmentDesignComparisonSpec,
    build_treatment_comparison_outcome,
    build_treatment_design_candidate,
    build_treatment_design_comparison,
)
from .cad_acoustic_treatment_repository import (
    CadAcousticTreatmentRepository,
)
from .cad_prediction_models import CadPredictionResult
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_system_variant_repository import CadSystemVariantRepository
from .r120_geometry_compiler import ExactExternalAuthorityRef
from .clock import utc_now_iso as _utc_now


TREATMENT_TYPES: tuple[str, ...] = (
    'porous_absorber',
    'absorber_with_air_gap',
    'membrane_panel_absorber',
    'perforated_slotted_absorber',
    'bass_trap',
    'diffuser_scattering_element',
    'hybrid',
)


@dataclass(frozen=True, slots=True)
class TreatmentPlacementPresentation:
    instance_id: str
    definition_id: str
    lifecycle: str
    position: Position3
    host_surface_id: str | None
    wave_capability: str
    geometric_capability: str


@dataclass(frozen=True, slots=True)
class TreatmentComparisonPresentation:
    comparison_id: str
    name: str
    candidates: tuple[str, ...]


class AcousticTreatmentService:
    """Native product surface for AcousticTreatment authoring/A-B (#985)."""

    def __init__(
        self,
        scene_repository: SceneRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.variant_repository = CadSystemVariantRepository(
            scene_repository
        )
        self.repository = CadAcousticTreatmentRepository(
            scene_repository, self.variant_repository
        )
        self.comparison_repository = CadAcousticTreatmentComparisonRepository(
            scene_repository, variant_repository=self.variant_repository
        )

    # ------------------------------------------------------------------
    # Definition authoring
    # ------------------------------------------------------------------

    def create_definition(
        self,
        *,
        name: str,
        treatment_type: TreatmentType,
        width_m: float,
        height_m: float,
        thickness_m: float,
        air_gap_m: float = 0.0,
        layers: Sequence[TreatmentLayer] = (),
        definition_id: str | None = None,
        version: str = '1',
    ) -> AcousticTreatmentDefinition:
        """Author a definition bound to persisted evidence authority."""
        if treatment_type not in TREATMENT_TYPES:
            raise ValueError(
                f"unsupported treatment type: {treatment_type!r}"
            )
        definition_id = (
            definition_id
            or f'treatment-{treatment_type}-{int(datetime.now().timestamp())}'
        )
        dimensions = TreatmentDimensions(
            width_m=width_m,
            height_m=height_m,
            thickness_m=thickness_m,
        )
        subject = TreatmentEvidenceSubject(
            definition_id=definition_id,
            definition_version=version,
            treatment_type=treatment_type,
            dimensions=dimensions,
            air_gap_m=air_gap_m,
            layers=tuple(layers),
        )
        evidence = build_treatment_evidence_authority(
            source_kind='user_defined',
            source_id=definition_id,
            source_version=version,
            extraction_id='htdt.native-treatment-author',
            extraction_version='1',
            subject=subject,
        )
        self.repository.save_evidence(evidence)
        definition = build_acoustic_treatment_definition(
            definition_id=definition_id,
            version=version,
            name=name,
            treatment_type=treatment_type,
            provenance=evidence.as_provenance(),
            dimensions=dimensions,
            air_gap_m=air_gap_m,
            layers=layers,
        )
        return self.repository.save_definition(definition)

    def list_definitions(
        self,
    ) -> tuple[AcousticTreatmentDefinition, ...]:
        return self.repository.list_definitions()

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def host_surface_options(self) -> tuple[str, ...]:
        """Exact host-surface ids the placement combo can offer."""
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None or revision.document.r120_semantic_geometry is None:
            return ()
        return tuple(
            surface.surface_id
            for surface in revision.document.r120_semantic_geometry.surfaces
        )

    def place_treatment(
        self,
        *,
        definition: AcousticTreatmentDefinition,
        host_surface_id: str | None = None,
        position: Position3,
        instance_id: str | None = None,
        coverage: TreatmentCoverage | None = None,
    ) -> AcousticTreatmentPlacement:
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            raise ValueError("配置先のSceneRevisionがありません。")
        placement = build_treatment_placement(
            definition=definition,
            revision=revision,
            instance_id=(
                instance_id
                or f"placement-{definition.definition_id}-"
                f"{time.time_ns()}"
            ),
            position=position,
            coverage=coverage
            or TreatmentCoverage(
                width_m=definition.dimensions.width_m,
                height_m=definition.dimensions.height_m,
            ),
            host_surface_id=host_surface_id,
        )
        return self.repository.save_placement(placement)

    def list_placements(
        self,
    ) -> tuple[TreatmentPlacementPresentation, ...]:
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            return ()
        presentations: list[TreatmentPlacementPresentation] = []
        # Many placements share one definition version; resolve each unique
        # definition once per listing instead of once per placement.
        definitions: dict[tuple[str, str], object] = {}
        for placement in self.repository.list_placements_for_scene(
            revision.revision_id
        ):
            definition_key = (placement.definition_id, placement.definition_version)
            if definition_key not in definitions:
                definitions[definition_key] = self.repository.get_definition(
                    *definition_key
                )
            definition = definitions[definition_key]
            capability = (
                None
                if definition is None
                else evaluate_treatment_prediction_capability(definition)
            )
            presentations.append(
                TreatmentPlacementPresentation(
                    instance_id=placement.instance_id,
                    definition_id=placement.definition_id,
                    lifecycle=placement.lifecycle,
                    position=placement.position,
                    host_surface_id=placement.host_surface_id,
                    wave_capability=(
                        'UNKNOWN'
                        if capability is None
                        else capability.wave_material_capability
                    ),
                    geometric_capability=(
                        'UNKNOWN'
                        if capability is None
                        else capability.geometric_material_capability
                    ),
                )
            )
        return tuple(presentations)

    def remove_placement(self, instance_id: str) -> None:
        self.repository.delete_placement(instance_id)

    # ------------------------------------------------------------------
    # Named A/B comparison
    # ------------------------------------------------------------------

    def create_comparison(
        self,
        *,
        name: str,
        baseline_label: str = 'baseline',
        candidate_designs: Sequence[tuple[str, Sequence[str]]],
    ) -> TreatmentDesignComparisonSpec:
        """Baseline no-treatment + named alternatives by placement ids.

        Every named design binds its exact placement refs and the persisted
        comparison spec; evaluated outcomes are bound separately via
        ``bind_comparison_outcome`` so a design never displays a benefit
        that was not evaluated (#985).
        """
        revision = self.scene_repository.current_head(self.document_id)
        if revision is None:
            raise ValueError("比較を作成するSceneRevisionがありません。")
        baseline = build_treatment_design_candidate(
            baseline=revision,
            label=baseline_label,
            role='no_treatment',
        )
        candidates = [baseline]
        for label, instance_ids in candidate_designs:
            placements = []
            for instance_id in instance_ids:
                placement = self.repository.latest_placement(instance_id)
                if placement is None:
                    raise ValueError(
                        f"treatment placementが存在しません: {instance_id}"
                    )
                if (
                    placement.scene_revision_id != revision.revision_id
                    or placement.document_id != self.document_id
                ):
                    raise ValueError(
                        'treatment placementは現在のSceneRevisionと'
                        '一致しません。'
                    )
                placements.append(placement)
            candidates.append(
                build_treatment_design_candidate(
                    baseline=revision,
                    label=label,
                    role='treatment',
                    placements=placements,
                )
            )
        spec = build_treatment_design_comparison(
            name=name,
            baseline=revision,
            candidates=candidates,
        )
        self.comparison_repository.save(spec)
        return spec

    def list_comparisons(
        self,
    ) -> tuple[TreatmentComparisonPresentation, ...]:
        return tuple(
            TreatmentComparisonPresentation(
                comparison_id=spec.comparison_id,
                name=spec.name,
                candidates=tuple(
                    candidate.label for candidate in spec.candidates
                ),
            )
            for spec in self.comparison_repository.list_for_document(
                self.document_id
            )
        )

    # ------------------------------------------------------------------
    # Evaluated-outcome binding
    # ------------------------------------------------------------------

    def bind_comparison_outcome(
        self,
        comparison_id: str,
        *,
        candidate_results: dict[
            str,
            Sequence[CadPredictionResult | ExactExternalAuthorityRef | str],
        ],
    ) -> TreatmentComparisonOutcome:
        """Attach evaluated result evidence to named candidates (#985 §4).

        ``candidate_results`` maps candidate label → exact evaluated
        prediction-result authorities (``CadPredictionResult`` or
        ``ExactExternalAuthorityRef``), or ``('unavailable', reason)`` for
        a design that could not be evaluated. Compatibility is checked
        before any numeric comparison; an unevaluated design never
        displays a benefit.
        """
        spec = self.comparison_repository.get(comparison_id)
        if spec is None:
            raise ValueError(
                f"treatment comparisonが存在しません: {comparison_id}"
            )
        labels = {candidate.label for candidate in spec.candidates}
        unknown = set(candidate_results) - labels
        if unknown:
            raise ValueError(
                f"comparisonに存在しないcandidate label: {sorted(unknown)}"
            )
        by_label = {c.label: c for c in spec.candidates}
        outcomes: list[TreatmentCandidateOutcome] = []
        for label, entries in candidate_results.items():
            candidate = by_label[label]
            if (
                len(entries) == 2
                and entries[0] == 'unavailable'
            ):
                outcomes.append(
                    TreatmentCandidateOutcome(
                        candidate_id=candidate.candidate_id,
                        candidate_sha256=candidate.candidate_sha256,
                        availability='unavailable',
                        unavailable_reason=str(entries[1]),
                    )
                )
                continue
            refs: list[ExactExternalAuthorityRef] = []
            for entry in entries:
                if isinstance(entry, CadPredictionResult):
                    if entry.scene_revision_id != (
                        spec.baseline_scene_revision_id
                    ):
                        raise ValueError(
                            f"candidate {label}: evaluated resultはcomparison"
                            'のbaseline SceneRevisionと一致しません。'
                        )
                    refs.append(
                        ExactExternalAuthorityRef(
                            authority_id=entry.prediction_id,
                            authority_version=entry.model_version,
                            semantic_hash_sha256=entry.result_sha256,
                        )
                    )
                else:
                    refs.append(entry)
            outcomes.append(
                TreatmentCandidateOutcome(
                    candidate_id=candidate.candidate_id,
                    candidate_sha256=candidate.candidate_sha256,
                    availability='evaluated',
                    evaluated_result_refs=tuple(refs),
                )
            )
        outcome = build_treatment_comparison_outcome(
            spec=spec,
            outcomes=outcomes,
            evaluated_at_utc=_utc_now(),
        )
        return self.comparison_repository.save_outcome(outcome)

    def latest_outcome(
        self, comparison_id: str
    ) -> TreatmentComparisonOutcome | None:
        return self.comparison_repository.latest_outcome(comparison_id)
