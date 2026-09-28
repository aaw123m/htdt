from __future__ import annotations

from typing import Callable

from .cad_adaptive_planner import (
    AdaptiveExecutionScope,
    CadAdaptivePlan,
    build_adaptive_plan,
    select_predicted_evaluations,
)
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_objective_repository import CadObjectiveRepository
from .cad_search import iter_cad_candidate_pages
from .cad_search_repository import CadSearchRepository


class CadAdaptivePlannerService:
    """Build and persist deterministic O70 proposals from immutable O10/O30/O60 evidence."""

    def __init__(
        self,
        search_repository: CadSearchRepository,
        objective_repository: CadObjectiveRepository,
        validation_repository: CadModelValidationRepository,
        adaptive_repository: CadAdaptivePlanRepository,
    ) -> None:
        self.search_repository = search_repository
        self.objective_repository = objective_repository
        self.validation_repository = validation_repository
        self.adaptive_repository = adaptive_repository
        paths = {
            str(search_repository.path),
            str(objective_repository.path),
            str(validation_repository.path),
            str(adaptive_repository.path),
        }
        if len(paths) != 1:
            raise ValueError('adaptive planner repositories must share one native CAD database')

    def build_and_save(
        self,
        *,
        validation_id: str,
        execution_scope: AdaptiveExecutionScope,
        length_scale_m: float = 0.5,
        proposal_limit: int = 20,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CadAdaptivePlan:
        validation = self.validation_repository.get(validation_id)
        if validation is None:
            raise ValueError('adaptive O60 ValidationRecord does not exist')
        spec = self.search_repository.get(validation.search_spec_id)
        if spec is None:
            raise ValueError('adaptive SearchSpec does not exist')

        objective_ids = tuple(
            dict.fromkeys(
                sample.objective_id for sample in validation.objective_samples
            )
        )
        evaluations = self.objective_repository.list_evaluations(spec.search_spec_id)
        predicted_evaluations = select_predicted_evaluations(
            evaluations,
            objective_ids,
        )

        required_ids = {
            evaluation.candidate_id for evaluation in predicted_evaluations
        }
        required_ids.update(pair.candidate_id for pair in validation.pairs)
        if not required_ids:
            raise ValueError('adaptive planner has no candidate evidence')

        candidate_map = {}
        candidate_set_sha256 = None
        for page in iter_cad_candidate_pages(
            self.search_repository.scene_repository,
            spec,
            cancelled=is_cancelled,
        ):
            candidate_set_sha256 = page.candidate_set_sha256
            for candidate in page.candidates:
                if candidate.candidate_id in required_ids:
                    candidate_map[candidate.candidate_id] = candidate
            if not required_ids - set(candidate_map):
                break

        missing = required_ids - set(candidate_map)
        if missing:
            raise ValueError(
                f'adaptive evidence candidates are outside SearchSpec: {sorted(missing)}'
            )
        if candidate_set_sha256 is None:
            raise ValueError('adaptive SearchSpec has no feasible candidate set')

        plan = build_adaptive_plan(
            spec=spec,
            candidate_set_sha256=candidate_set_sha256,
            validation=validation,
            candidates=tuple(candidate_map.values()),
            predicted_evaluations=predicted_evaluations,
            execution_scope=execution_scope,
            length_scale_m=length_scale_m,
            proposal_limit=proposal_limit,
            is_cancelled=is_cancelled,
        )
        existing = self.adaptive_repository.find_by_sha(
            plan.search_spec_id,
            plan.adaptive_sha256,
        )
        if existing is not None:
            return existing
        self.adaptive_repository.save(plan)
        return plan
