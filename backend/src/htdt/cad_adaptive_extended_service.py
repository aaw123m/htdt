from __future__ import annotations

from .cad_adaptive_extended import (
    AdaptiveExecutionScope,
    CadAdaptiveExtendedPlan,
    build_adaptive_extended_plan,
)
from .cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from .cad_extended_search import generate_extended_candidates
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_model_validation_repository import CadModelValidationRepository


class CadAdaptiveExtendedPlannerService:
    """Build deterministic adaptive proposals over exact O10+O80 candidate authority."""

    def __init__(
        self,
        extended_repository: CadExtendedSearchRepository,
        validation_repository: CadModelValidationRepository,
        adaptive_extended_repository: CadAdaptiveExtendedRepository,
    ) -> None:
        self.extended_repository = extended_repository
        self.validation_repository = validation_repository
        self.adaptive_extended_repository = adaptive_extended_repository
        self.search_repository = extended_repository.search_repository
        paths = {
            str(self.search_repository.path),
            str(validation_repository.path),
            str(adaptive_extended_repository.path),
        }
        if len(paths) != 1:
            raise ValueError(
                'adaptive extended planner repositories must share one native CAD database'
            )

    def _all_candidates(self, extended_spec, base_spec):
        candidates = []
        offset = 0
        candidate_set_sha256: str | None = None
        while True:
            page = generate_extended_candidates(
                self.search_repository.scene_repository,
                base_spec,
                extended_spec,
                offset=offset,
                limit=500,
            )
            if candidate_set_sha256 is None:
                candidate_set_sha256 = page.candidate_set_sha256
            elif page.candidate_set_sha256 != candidate_set_sha256:
                raise ValueError(
                    'adaptive extended candidate-set identity changed between pages'
                )
            candidates.extend(page.candidates)
            offset += len(page.candidates)
            if not page.candidates or offset >= page.feasible_candidate_count:
                break
        if candidate_set_sha256 is None or not candidates:
            raise ValueError('adaptive extended SearchSpec has no feasible candidates')
        return tuple(candidates), candidate_set_sha256

    def build_and_save(
        self,
        *,
        extended_search_id: str,
        validation_id: str,
        execution_scope: AdaptiveExecutionScope,
        length_scale_normalized: float = 0.5,
        proposal_limit: int = 20,
    ) -> CadAdaptiveExtendedPlan:
        extended_spec = self.extended_repository.get_spec(extended_search_id)
        if extended_spec is None:
            raise ValueError('adaptive extended SearchSpec does not exist')
        base_spec = self.search_repository.get(
            extended_spec.base_search_spec_id
        )
        if base_spec is None:
            raise ValueError('adaptive extended base SearchSpec does not exist')
        capability = self.extended_repository.get_capability(
            extended_spec.capability_id
        )
        if capability is None:
            raise ValueError('adaptive extended capability does not exist')
        if capability.capability_sha256 != extended_spec.capability_sha256:
            raise ValueError('adaptive extended capability hash mismatch')

        validation = self.validation_repository.get(validation_id)
        if validation is None:
            raise ValueError('adaptive extended O60 ValidationRecord does not exist')

        candidates, candidate_set_sha256 = self._all_candidates(
            extended_spec,
            base_spec,
        )
        observations = self.adaptive_extended_repository.current_observations(
            extended_search_id
        )
        if not observations:
            raise ValueError(
                'adaptive extended planning requires persisted objective observations'
            )

        plan = build_adaptive_extended_plan(
            base_spec=base_spec,
            base_candidate_set_sha256=extended_spec.base_candidate_set_sha256,
            extended_spec=extended_spec,
            extended_candidate_set_sha256=candidate_set_sha256,
            capability_id=capability.capability_id,
            capability_sha256=capability.capability_sha256,
            extended_model_id=capability.model_id,
            extended_model_version=capability.model_version,
            validation=validation,
            candidates=candidates,
            observations=observations,
            execution_scope=execution_scope,
            length_scale_normalized=length_scale_normalized,
            proposal_limit=proposal_limit,
        )
        existing = self.adaptive_extended_repository.find_plan_by_sha(
            extended_search_id,
            plan.adaptive_extended_sha256,
        )
        if existing is not None:
            return existing
        self.adaptive_extended_repository.save_plan(plan)
        return plan
