"""Intervention Planner orchestration over canonical evaluators (#519).

The planner does not implement a second optimizer: it resolves the exact
baseline authorities through :class:`JointOptimizationContext`, packages a
problem ROI plus an independent guardrail domain into an immutable
``InterventionStudySpec``, and records ``InterventionAlternative`` rows that
reference generated authority ids (SearchSpec / JointOptimizationSpec /
treatment plans). Previewing alternatives never mutates the scene; applying
one goes through the existing explicit lifecycle.
"""

from __future__ import annotations

from typing import Sequence

from .cad_intervention_study import (
    EvidenceState,
    GuardrailDomain,
    InterventionAlternative,
    InterventionFamily,
    InterventionFinding,
    InterventionMetric,
    InterventionSemanticDiff,
    InterventionStudySpec,
    build_intervention_alternative,
    build_intervention_study_spec,
)
from .cad_intervention_study_repository import CadInterventionStudyRepository
from .cad_repository import SceneRepository
from .joint_optimization_context import JointOptimizationContext


DEFAULT_INTERVENTION_ALGORITHM = 'intervention-planner-1'


class InterventionPlanner:
    """Resolve baseline authorities and persist study/alternative records."""

    def __init__(self, context: JointOptimizationContext) -> None:
        self.context = context
        self.repository = CadInterventionStudyRepository(
            scene_repository=context.repository,
        )
        self.document_id = context.document_id

    @property
    def scene_repository(self) -> SceneRepository:
        return self.context.repository

    def create_study(
        self,
        *,
        finding: InterventionFinding,
        allowed_families: Sequence[InterventionFamily],
        fidelity_label: str,
        provider_id: str,
        provider_version: str,
        candidate_budget: int,
        created_at_utc: str,
        guardrail_domain: GuardrailDomain | None = None,
        treatment_capability_authority_id: str | None = None,
        treatment_capability_authority_sha256: str | None = None,
        evidence_state_floor: EvidenceState = 'exploratory',
        algorithm_version: str = DEFAULT_INTERVENTION_ALGORITHM,
    ) -> InterventionStudySpec | None:
        """Package a finding into a persisted spec; None when no baseline."""
        baseline = self.context.resolve_baseline()
        if baseline is None or not baseline.objective_definitions:
            return None
        spec = build_intervention_study_spec(
            scene_revision=baseline.scene_revision,
            base_variant=baseline.base_variant,
            finding=finding,
            allowed_families=tuple(allowed_families),
            metric_definitions=baseline.objective_definitions,
            fidelity_label=fidelity_label,
            provider_id=provider_id,
            provider_version=provider_version,
            algorithm_version=algorithm_version,
            candidate_budget=candidate_budget,
            created_at_utc=created_at_utc,
            guardrail_domain=guardrail_domain,
            treatment_capability_authority_id=(
                treatment_capability_authority_id
            ),
            treatment_capability_authority_sha256=(
                treatment_capability_authority_sha256
            ),
            robustness_policy=(
                'o90_bounded'
                if baseline.robustness_spec is not None
                else 'none'
            ),
            evidence_state_floor=evidence_state_floor,
        )
        return self.repository.save_spec(spec)

    def record_alternative(
        self,
        *,
        spec: InterventionStudySpec,
        family: InterventionFamily,
        diff_summary: str,
        evidence_state: EvidenceState,
        fidelity_label: str,
        metrics: Sequence[InterventionMetric],
        changed_entity_ids: Sequence[str] = (),
        dsp_parameters: Sequence[str] = (),
        treatment_item_ids: Sequence[str] = (),
        topology_changes: Sequence[str] = (),
        generated_authority_ids: Sequence[str] = (),
        regressions: Sequence[str] = (),
        apply_instructions: str | None = None,
    ) -> InterventionAlternative:
        """Record one evaluated counterfactual against a persisted spec."""
        alternative = build_intervention_alternative(
            spec=spec,
            family=family,
            semantic_diff=InterventionSemanticDiff(
                changed_entity_ids=tuple(changed_entity_ids),
                dsp_parameters=tuple(dsp_parameters),
                treatment_item_ids=tuple(treatment_item_ids),
                topology_changes=tuple(topology_changes),
                summary=diff_summary,
            ),
            evidence_state=evidence_state,
            fidelity_label=fidelity_label,
            metrics=metrics,
            generated_authority_ids=generated_authority_ids,
            regressions=regressions,
            apply_instructions=apply_instructions,
        )
        return self.repository.save_alternative(alternative)

    def list_studies(
        self, *, scene_revision_id: str | None = None
    ) -> list[InterventionStudySpec]:
        return self.repository.list_specs(
            self.document_id, scene_revision_id=scene_revision_id
        )

    def list_alternatives(
        self, spec_id: str
    ) -> list[InterventionAlternative]:
        return self.repository.list_alternatives(spec_id)


__all__ = [
    'DEFAULT_INTERVENTION_ALGORITHM',
    'InterventionPlanner',
]
