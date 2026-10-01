"""O90 robustness-spec authoring over existing search/objective authorities.

The Optimize > robustness page previously had no way to create a
``RobustnessSpec``: ``CadRobustnessRepository`` had only test callers, which
made the joint placement+DSP lane a dead end (``create_spec`` requires
``baseline.robustness_spec``). This Qt-free context is the smallest viable
production path the UI drives:

- ``candidate_choices`` lists candidates that already carry an O30 objective
  evaluation for the selected SearchSpec (same authority the read-only
  robustness view lists).
- ``axis_choices`` offers only axes the candidate document can actually
  perturb — derived from ``physical_variables_from_authority`` and gated by
  the same entity-kind rules ``_axis_value`` enforces.
- ``create_spec`` compiles via ``build_robustness_spec`` and persists through
  ``repository.save_spec`` (fail-closed candidate-membership replay).
- ``evaluate_spec`` runs ``evaluate_local_robustness`` with an honest
  perturbation evaluator: ``candidate_movement`` vectors are recomputed on
  the perturbed document; every other objective authority raises an
  ``unsupported`` failure so the sample records 未対応 instead of a
  fabricated prediction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from . import __version__
from .cad_constraint_models import CadConstraintSet
from .cad_joint_optimization import physical_variables_from_authority
from .cad_objective_authority import objective_spec_authority
from .cad_objective_models import CadObjectiveEvaluation
from .cad_repository import SceneRepository, SceneRevision
from .cad_robustness_repository import CadRobustnessRepository
from .cad_search import iter_cad_candidate_pages
from .cad_search_models import CadCandidate, CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .cad_extended_search_repository import CadExtendedSearchRepository
from .joint_optimization_context import _o90_parameter
from .optimization_objectives import ObjectiveVector, movement_objectives
from .optimization_robustness import (
    PerturbationObjectiveResult,
    PerturbationSample,
    RobustnessAxisParameter,
    RobustnessEvaluation,
    RobustnessEvaluationCancelled,
    RobustnessSpec,
    UncertaintyAxis,
    _axis_value,
    _candidate_document,
    _candidate_from_payload,
    build_robustness_spec,
    evaluate_local_robustness,
)


DEFAULT_POSITION_DELTA_M = 0.05
DEFAULT_ANGLE_DELTA_DEG = 2.0

_AXIS_PARAMETER_LABELS: dict[str, str] = {
    'speaker_x_m': 'スピーカー X位置',
    'speaker_y_m': 'スピーカー Y位置',
    'speaker_z_m': 'スピーカー高さ',
    'listener_x_m': 'リスナー X位置',
    'listener_y_m': 'リスナー Y位置',
    'listener_z_m': 'リスナー高さ',
    'aim_yaw_deg': '音響照準 (ヨー)',
    'aim_pitch_deg': '音響照準 (ピッチ)',
    'body_yaw_deg': '筐体ヨー (トーイン)',
}


@dataclass(frozen=True, slots=True)
class RobustnessCandidateChoice:
    """One evaluated candidate the operator can bind a robustness spec to."""

    candidate_id: str
    label: str
    evaluation: CadObjectiveEvaluation


@dataclass(frozen=True, slots=True)
class RobustnessAxisChoice:
    """One perturbable axis offered by the physical search authority."""

    axis_id: str
    entity_id: str
    entity_label: str
    parameter: RobustnessAxisParameter
    unit: str
    nominal_value: float
    default_delta: float
    label: str

    def to_axis(self, delta: float) -> UncertaintyAxis:
        return UncertaintyAxis(
            axis_id=self.axis_id,
            entity_id=self.entity_id,
            parameter=self.parameter,
            unit=self.unit,  # type: ignore[arg-type]
            nominal_value=self.nominal_value,
            minus_delta=float(delta),
            plus_delta=float(delta),
        )


class RobustnessAuthoringContext:
    """Shared state + operations for the robustness authoring panel."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        search_repository: CadSearchRepository,
        extended_search_repository: CadExtendedSearchRepository | None,
        objective_repository,
        robustness_repository: CadRobustnessRepository,
        document_id: str,
    ) -> None:
        self.scene_repository = scene_repository
        self.search_repository = search_repository
        self.extended_search_repository = extended_search_repository
        self.objective_repository = objective_repository
        self.robustness_repository = robustness_repository
        self.document_id = document_id

    # ------------------------------------------------------------------
    # Authority reads
    # ------------------------------------------------------------------

    def candidate_choices(
        self,
        search_spec: CadSearchSpec,
    ) -> tuple[RobustnessCandidateChoice, ...]:
        """Latest evaluated candidates for the selected SearchSpec."""
        evaluations = self.objective_repository.latest_evaluations_by_candidate(
            search_spec.search_spec_id
        )
        choices: list[RobustnessCandidateChoice] = []
        for index, evaluation in enumerate(evaluations, start=1):
            metrics = ', '.join(
                f'{metric.objective_id}={metric.value:.4g} {metric.unit}'
                for metric in evaluation.vector.metrics
                if metric.value is not None
            )
            label = (
                f'候補 {index} · {metrics}'
                if metrics
                else f'候補 {index} · {evaluation.candidate_id[:12]}'
            )
            choices.append(
                RobustnessCandidateChoice(
                    candidate_id=evaluation.candidate_id,
                    label=label,
                    evaluation=evaluation,
                )
            )
        return tuple(choices)

    def axis_choices(
        self,
        search_spec: CadSearchSpec,
        candidate_id: str,
    ) -> tuple[RobustnessAxisChoice, ...]:
        """Perturbable axes for the candidate, bound to the search authority.

        Only axes ``_axis_value`` can evaluate on the candidate document are
        offered — speaker positions require a speaker entity, listener
        positions a seat/measurement point, and aim/body axes a speaker with
        an explicit acoustic aim.
        """
        revision = self.scene_repository.get(search_spec.scene_revision_id)
        if revision is None:
            raise ValueError('探索仕様のソースリビジョンが存在しません')
        candidate, _set_sha = self._resolve_candidate(search_spec, candidate_id)
        document = _candidate_document(revision, candidate)
        extended = self._extended_spec_for(search_spec)
        choices: list[RobustnessAxisChoice] = []
        for variable in physical_variables_from_authority(search_spec, extended):
            try:
                entity = document.entity(variable.entity_id)
            except KeyError:
                continue
            parameter = _o90_parameter(entity.kind, variable.parameter)
            if parameter is None:
                continue
            axis_id = f'{variable.entity_id}:{parameter}'
            unit = 'm' if parameter.endswith('_m') else 'deg'
            probe = UncertaintyAxis(
                axis_id=axis_id,
                entity_id=variable.entity_id,
                parameter=parameter,  # type: ignore[arg-type]
                unit=unit,  # type: ignore[arg-type]
                nominal_value=0.0,
                minus_delta=1.0,
                plus_delta=1.0,
            )
            try:
                nominal = _axis_value(document, probe)
            except (ValueError, KeyError):
                continue
            entity_label = getattr(entity, 'name', None) or variable.entity_id
            parameter_label = _AXIS_PARAMETER_LABELS.get(parameter, parameter)
            choices.append(
                RobustnessAxisChoice(
                    axis_id=axis_id,
                    entity_id=variable.entity_id,
                    entity_label=entity_label,
                    parameter=parameter,  # type: ignore[arg-type]
                    unit=unit,
                    nominal_value=nominal,
                    default_delta=(
                        DEFAULT_POSITION_DELTA_M
                        if unit == 'm'
                        else DEFAULT_ANGLE_DELTA_DEG
                    ),
                    label=(
                        f'{entity_label} · {parameter_label} · '
                        f'基準 {nominal:.4g} {unit}'
                    ),
                )
            )
        return tuple(choices)

    # ------------------------------------------------------------------
    # Spec authoring + evaluation
    # ------------------------------------------------------------------

    def create_spec(
        self,
        *,
        search_spec: CadSearchSpec,
        candidate_id: str,
        axes: Sequence[UncertaintyAxis],
        cancelled: Callable[[], bool] | None = None,
    ) -> RobustnessSpec:
        revision = self.scene_repository.get(search_spec.scene_revision_id)
        if revision is None:
            raise ValueError('探索仕様のソースリビジョンが存在しません')
        candidate, set_sha = self._resolve_candidate(
            search_spec, candidate_id, cancelled=cancelled
        )
        nominal = self._nominal_evaluation(search_spec, candidate_id)
        provenance = self._provenance(nominal)
        spec = build_robustness_spec(
            source_revision=revision,
            search_spec=search_spec,
            candidate=candidate,
            candidate_set_sha256=set_sha,
            nominal_objective=nominal,
            axes=axes,
            **provenance,
            software_version=__version__,
        )
        self.robustness_repository.save_spec(spec)
        return spec

    def evaluate_spec(
        self,
        spec: RobustnessSpec,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[
        tuple[PerturbationSample, ...],
        tuple[RobustnessEvaluation, ...],
    ]:
        """Run the local stencil and persist samples + evaluations.

        Cancel inside ``evaluate_local_robustness`` aborts without persisting
        partial evidence — the spec itself is already saved and can be
        re-evaluated later.
        """
        revision = self.scene_repository.get(spec.scene_revision_id)
        if revision is None:
            raise ValueError('耐性仕様のソースリビジョンが存在しません')
        search_spec = self.search_repository.get(spec.search_spec_id)
        if search_spec is None:
            raise ValueError('耐性仕様の探索仕様が存在しません')
        nominal = self.objective_repository.get_evaluation(
            spec.nominal_objective_evaluation_id
        )
        if nominal is None:
            raise ValueError('耐性仕様のノミナル O30評価が存在しません')
        constraint_set = CadConstraintSet.model_validate(
            json.loads(search_spec.constraint_snapshot_json)
        )
        evaluator = self._perturbation_evaluator(
            revision=revision,
            spec=spec,
            nominal_objective=nominal,
        )
        samples, evaluations = evaluate_local_robustness(
            source_revision=revision,
            search_spec=search_spec,
            spec=spec,
            constraint_set=constraint_set,
            nominal_objective=nominal,
            evaluator=evaluator,
            is_cancelled=is_cancelled,
        )
        if is_cancelled is not None and is_cancelled():
            raise RobustnessEvaluationCancelled(
                'robustness evaluation cancelled before persistence'
            )
        self.robustness_repository.save_samples(samples)
        self.robustness_repository.save_evaluations(evaluations)
        return samples, evaluations

    # ------------------------------------------------------------------

    def _extended_spec_for(self, search_spec: CadSearchSpec):
        if self.extended_search_repository is None:
            return None
        specs = self.extended_search_repository.list_for_base_search(
            search_spec.search_spec_id
        )
        return specs[-1] if specs else None

    def _resolve_candidate(
        self,
        search_spec: CadSearchSpec,
        candidate_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[CadCandidate, str]:
        """Re-derive one feasible candidate by id through the cached enumeration."""
        for page in iter_cad_candidate_pages(
            self.scene_repository,
            search_spec,
            cancelled=cancelled,
        ):
            for candidate in page.candidates:
                if candidate.candidate_id == candidate_id:
                    return candidate, page.candidate_set_sha256
        raise ValueError(
            '選択候補は探索仕様の実行可能候補集合にありません: '
            f'{candidate_id}'
        )

    def _nominal_evaluation(
        self,
        search_spec: CadSearchSpec,
        candidate_id: str,
    ) -> CadObjectiveEvaluation:
        evaluations = self.objective_repository.latest_evaluations_by_candidate(
            search_spec.search_spec_id
        )
        for evaluation in evaluations:
            if evaluation.candidate_id == candidate_id:
                return evaluation
        raise ValueError('選択候補のO30評価が存在しません')

    @staticmethod
    def _provenance(nominal: CadObjectiveEvaluation) -> dict[str, str]:
        """Derive honest model/provider/fidelity labels from the O30 evaluation."""
        evaluation_spec = json.loads(nominal.evaluation_spec_json)
        authority = objective_spec_authority(evaluation_spec)
        predicted = next(
            (
                ref
                for ref in nominal.input_refs
                if ref.evidence_class == 'predicted'
            ),
            nominal.input_refs[0],
        )
        model_id = evaluation_spec.get('model_id')
        model_version = evaluation_spec.get('model_version') or evaluation_spec.get(
            'algorithm_version'
        )
        provider_id = evaluation_spec.get('provider_id')
        fidelity = evaluation_spec.get('fidelity')
        return {
            'nominal_prediction_result_ref': predicted.source_id,
            'model_id': str(model_id) if model_id else f'o30:{authority}',
            'model_version': str(model_version) if model_version else '1',
            'prediction_provider_id': (
                str(provider_id) if provider_id else predicted.source_kind
            ),
            'fidelity': str(fidelity) if fidelity else predicted.evidence_class,
        }

    @staticmethod
    def _perturbation_evaluator(
        *,
        revision: SceneRevision,
        spec: RobustnessSpec,
        nominal_objective: CadObjectiveEvaluation,
    ) -> Callable[[object, str], PerturbationObjectiveResult]:
        """Perturbed-document evaluator honest about what it can recompute.

        ``candidate_movement`` is pure geometry over the perturbed document,
        so those samples get real recomputed values rebound onto the nominal
        metric schema. Every other objective authority raises an
        ``unsupported`` error, which ``evaluate_local_robustness`` records as
        a failed (未対応) sample — never a fabricated prediction.
        """
        evaluation_spec = json.loads(nominal_objective.evaluation_spec_json)
        authority = objective_spec_authority(evaluation_spec)
        prefix = evaluation_spec.get('prefix')
        if not isinstance(prefix, str) or not prefix:
            prefix = 'movement'
        candidate = _candidate_from_payload(
            spec.candidate_kind,
            json.loads(spec.candidate_payload_json),
        )
        baseline_positions = {}
        for entity_id in candidate.positions:
            entity = revision.document.entity(entity_id)
            baseline_positions[entity_id] = {
                'x_m': float(entity.position.x_m),
                'y_m': float(entity.position.y_m),
                'z_m': float(entity.position.z_m),
            }

        def evaluator(document, sample_id: str) -> PerturbationObjectiveResult:
            if authority != 'candidate_movement':
                raise ValueError(
                    f'unsupported perturbation evaluation: {authority}'
                )
            positions = {}
            for entity_id in candidate.positions:
                entity = document.entity(entity_id)
                positions[entity_id] = {
                    'x_m': float(entity.position.x_m),
                    'y_m': float(entity.position.y_m),
                    'z_m': float(entity.position.z_m),
                }
            vector = movement_objectives(
                sample_id,
                baseline_positions,
                positions,
                prefix=prefix,
            )
            metrics = []
            for nominal_metric in nominal_objective.vector.metrics:
                metric = vector.metric(nominal_metric.objective_id)
                metrics.append(
                    nominal_metric.model_copy(update={'value': metric.value})
                )
            return PerturbationObjectiveResult(
                prediction_result_ref=spec.nominal_prediction_result_ref,
                objective_vector=ObjectiveVector(
                    candidate_id=sample_id,
                    metrics=tuple(metrics),
                ),
            )

        return evaluator


__all__ = [
    'DEFAULT_ANGLE_DELTA_DEG',
    'DEFAULT_POSITION_DELTA_M',
    'RobustnessAuthoringContext',
    'RobustnessAxisChoice',
    'RobustnessCandidateChoice',
    'RobustnessEvaluationCancelled',
]
