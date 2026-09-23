from __future__ import annotations

from dataclasses import dataclass

from .cad_objective_models import CadObjectiveEvaluation
from .cad_search_models import CadSearchSpec
from .cad_repository import SceneRevision
from .optimization_robustness import (
    PerturbationSample,
    RobustnessEvaluation,
    RobustnessSpec,
)


_AXIS_LABELS = {
    'speaker_x_m': 'スピーカー X位置',
    'speaker_y_m': 'スピーカー Y位置',
    'speaker_z_m': 'スピーカー Z位置',
    'listener_x_m': '座席 X位置',
    'listener_y_m': '座席 Y位置',
    'listener_z_m': '座席 Z位置',
    'aim_yaw_deg': '音響 aim 左右',
    'aim_pitch_deg': '音響 aim 上下',
    'body_yaw_deg': '筐体向き',
}

_OBJECTIVE_LABELS = {
    'response.rms_difference_db': '応答差 RMS',
    'response.peak_excess_db': 'ピーク超過',
    'response.dip_deficit_db': 'ディップ不足',
    'response.shape_rms_db': '応答形状 RMS',
    'pair.rms_difference_db': 'ペア応答差 RMS',
    'pair.shape_rms_db': 'ペア形状 RMS',
    'seat.pairwise_rms_difference_max_db': '座席間差 最大',
    'seat.pairwise_rms_difference_rms_db': '座席間差 RMS',
    'seat.pairwise_shape_max_db': '座席間形状差 最大',
    'seat.pairwise_shape_rms_db': '座席間形状差 RMS',
    'movement.total_m': '総移動量',
    'movement.max_m': '最大移動量',
}


@dataclass(frozen=True, slots=True)
class RobustSensitivityPresentation:
    axis_id: str
    label: str
    input_unit: str
    magnitude_per_unit: float | None
    state: str


@dataclass(frozen=True, slots=True)
class RobustObjectivePresentation:
    objective_id: str
    objective_label: str
    unit: str
    direction: str
    direction_label: str
    nominal_value: float
    sampled_adverse_value: float
    sampled_adverse_label: str
    sensitivity_value: float | None
    mean_value: float | None
    median_value: float | None
    percentile_p95: float | None
    percentile_supported: bool
    percentile_reason: str | None
    violation_probability: float | None
    probability_supported: bool
    probability_reason: str | None
    feasible_fraction: float | None
    axis_sensitivities: tuple[RobustSensitivityPresentation, ...]
    sampled_values: tuple[float, ...]
    sampled_weights: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class RobustnessCompletenessPresentation:
    sample_count: int
    feasible_count: int
    infeasible_count: int
    failed_prediction_count: int
    unsupported_count: int
    expected_sample_count: int | None
    state: str
    state_label: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RobustnessCandidatePresentation:
    candidate_id: str
    spec_id: str
    scene_revision_id: str
    search_spec_id: str
    model_id: str
    model_version: str
    prediction_provider_id: str
    fidelity: str
    objective_evaluation_spec_sha256: str
    sampling_strategy: str
    objectives: tuple[RobustObjectivePresentation, ...]
    completeness: RobustnessCompletenessPresentation
    current: bool
    stale_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RobustnessComparisonEligibility:
    eligible: bool
    reasons: tuple[str, ...]


def _axis_sensitivity_value(item) -> float | None:
    values = [
        abs(float(value))
        for value in (
            item.central_slope_per_unit,
            item.minus_slope_per_unit,
            item.plus_slope_per_unit,
        )
        if value is not None
    ]
    return max(values) if values else None


def _sensitivity_value(evaluation: RobustnessEvaluation) -> float | None:
    values = [
        value
        for item in evaluation.local_sensitivities
        if (value := _axis_sensitivity_value(item)) is not None
    ]
    return max(values) if values else None


def _probability_block_reason(evaluation: RobustnessEvaluation) -> str:
    if evaluation.percentile_semantics == 'not_available_bounded_interval':
        return '確率分布が設定されていないためp95・確率は計算できません'
    if evaluation.percentile_semantics == 'not_available_empirical_unweighted':
        return '実測サンプルに確率重みがないためp95・確率は計算できません'
    if evaluation.percentile_semantics == 'not_available_discrete_unweighted':
        return '離散サンプルに確率重みがないためp95・確率は計算できません'
    return '確率モデルの根拠がないため確率指標は利用できません'


def _objective_view(
    evaluation: RobustnessEvaluation,
    samples: tuple[PerturbationSample, ...],
    axis_labels: dict[str, str],
) -> RobustObjectivePresentation:
    probability_supported = evaluation.probability_semantics is not None
    percentile_supported = (
        evaluation.percentile_semantics == 'explicit_probability_model'
        and evaluation.sampled_envelope is not None
        and evaluation.sampled_envelope.percentile_values is not None
    )
    p95 = None
    median = None
    if percentile_supported:
        assert evaluation.sampled_envelope is not None
        assert evaluation.sampled_envelope.percentile_values is not None
        p95 = evaluation.sampled_envelope.percentile_values.get('p95')
        median = evaluation.sampled_envelope.percentile_values.get('p50')
    blocked_reason = None
    if not probability_supported or not percentile_supported:
        blocked_reason = _probability_block_reason(evaluation)

    axis_sensitivities = tuple(
        RobustSensitivityPresentation(
            axis_id=item.axis_id,
            label=axis_labels.get(item.axis_id, '不確かさ軸'),
            input_unit=item.input_unit,
            magnitude_per_unit=_axis_sensitivity_value(item),
            state=item.state,
        )
        for item in evaluation.local_sensitivities
    )
    sampled_values: list[float] = []
    sampled_weights: list[float | None] = []
    for sample in samples:
        if sample.objective_vector is None:
            continue
        try:
            sampled_values.append(
                float(
                    sample.objective_vector.metric(
                        evaluation.objective_id
                    ).value
                )
            )
            sampled_weights.append(
                None
                if sample.probability_weight is None
                else float(sample.probability_weight)
            )
        except KeyError:
            continue

    return RobustObjectivePresentation(
        objective_id=evaluation.objective_id,
        objective_label=_OBJECTIVE_LABELS.get(
            evaluation.objective_id,
            '評価指標',
        ),
        unit=evaluation.objective_unit,
        direction=evaluation.direction,
        direction_label=(
            '小さいほど有利'
            if evaluation.direction == 'minimize'
            else '大きいほど有利'
        ),
        nominal_value=float(evaluation.nominal_value),
        sampled_adverse_value=float(evaluation.sampled_worst_value),
        sampled_adverse_label='評価サンプル内の不利側最大値',
        sensitivity_value=_sensitivity_value(evaluation),
        mean_value=evaluation.mean_value,
        median_value=median,
        percentile_p95=p95,
        percentile_supported=percentile_supported,
        percentile_reason=None if percentile_supported else blocked_reason,
        violation_probability=evaluation.constraint_violation_probability,
        probability_supported=probability_supported,
        probability_reason=None if probability_supported else blocked_reason,
        feasible_fraction=evaluation.feasible_fraction,
        axis_sensitivities=axis_sensitivities,
        sampled_values=tuple(sampled_values),
        sampled_weights=tuple(sampled_weights),
    )


def _expected_samples(spec: RobustnessSpec) -> int | None:
    if spec.sample_count is not None:
        return int(spec.sample_count)
    if spec.sampling_strategy == 'deterministic_local_stencil':
        return 1 + 2 * len(spec.axes)
    return None


def _completeness(
    spec: RobustnessSpec,
    samples: tuple[PerturbationSample, ...],
) -> RobustnessCompletenessPresentation:
    sample_count = len(samples)
    infeasible = sum(1 for item in samples if not item.feasible)
    unsupported = sum(
        1
        for item in samples
        if item.failure_reason is not None
        and 'unsupported' in item.failure_reason.lower()
    )
    failed = sum(
        1
        for item in samples
        if item.feasible
        and item.failure_reason is not None
        and 'unsupported' not in item.failure_reason.lower()
    )
    feasible = sum(
        1
        for item in samples
        if item.feasible and item.failure_reason is None
    )
    expected = _expected_samples(spec)
    reasons: list[str] = []
    if expected is not None and sample_count < expected:
        reasons.append(
            f'必要サンプル {expected} 件のうち {sample_count} 件まで評価済みです'
        )
    if failed:
        reasons.append(f'予測失敗サンプルが {failed} 件あります')
    if unsupported:
        reasons.append(f'未対応サンプルが {unsupported} 件あります')
    if expected is not None and sample_count < expected:
        state = 'preliminary'
        label = '評価途中'
    elif failed or unsupported:
        state = 'incomplete'
        label = '評価に不足があります'
    else:
        state = 'complete'
        label = '評価完了'
    return RobustnessCompletenessPresentation(
        sample_count=sample_count,
        feasible_count=feasible,
        infeasible_count=infeasible,
        failed_prediction_count=failed,
        unsupported_count=unsupported,
        expected_sample_count=expected,
        state=state,
        state_label=label,
        reasons=tuple(reasons),
    )


def build_robustness_candidate_presentation(
    *,
    spec: RobustnessSpec,
    evaluations: tuple[RobustnessEvaluation, ...],
    samples: tuple[PerturbationSample, ...],
    current_revision: SceneRevision | None,
    current_search_spec: CadSearchSpec | None,
    nominal_objective: CadObjectiveEvaluation | None,
    current_constraint_workspace_hash: str | None = None,
) -> RobustnessCandidatePresentation:
    stale: list[str] = []
    if current_revision is None:
        stale.append('現在の保存済み部屋状態を確認できません')
    elif (
        current_revision.revision_id != spec.scene_revision_id
        or current_revision.content_hash != spec.scene_content_hash
    ):
        stale.append('部屋・配置が変更されたため再評価が必要です')
    if current_search_spec is None:
        stale.append('現在の探索設定を確認できません')
        if current_constraint_workspace_hash is None:
            stale.append('現在の制約条件を確認できません')
    else:
        if (
            current_search_spec.search_spec_id != spec.search_spec_id
            or current_search_spec.search_spec_sha256 != spec.search_spec_sha256
        ):
            stale.append('探索設定が変更されたため再評価が必要です')
        if current_constraint_workspace_hash is None:
            stale.append('現在の制約条件を確認できません')
        elif (
            current_constraint_workspace_hash
            != current_search_spec.constraint_workspace_hash
        ):
            stale.append('制約条件が変更されたため再評価が必要です')
    if nominal_objective is None:
        stale.append('基準評価の保存条件を確認できません')
    elif (
        nominal_objective.evaluation_id
        != spec.nominal_objective_evaluation_id
        or nominal_objective.evaluation_sha256
        != spec.nominal_objective_evaluation_sha256
        or nominal_objective.evaluation_spec_sha256
        != spec.objective_evaluation_spec_sha256
    ):
        stale.append('評価条件が変更されたため再評価が必要です')

    for evaluation in evaluations:
        if (
            evaluation.robustness_spec_id != spec.robustness_spec_id
            or evaluation.robustness_spec_sha256 != spec.robustness_spec_sha256
            or evaluation.candidate_id != spec.candidate_id
        ):
            stale.append('ばらつき評価が別の候補条件へ結び付いています')
            break
    for sample in samples:
        if (
            sample.robustness_spec_id != spec.robustness_spec_id
            or sample.robustness_spec_sha256 != spec.robustness_spec_sha256
            or sample.candidate_id != spec.candidate_id
            or sample.model_id != spec.model_id
            or sample.model_version != spec.model_version
            or sample.prediction_provider_id != spec.prediction_provider_id
            or sample.fidelity != spec.fidelity
            or sample.objective_evaluation_spec_sha256
            != spec.objective_evaluation_spec_sha256
        ):
            stale.append('保存済み評価点の計算条件が一致しません')
            break

    return RobustnessCandidatePresentation(
        candidate_id=spec.candidate_id,
        spec_id=spec.robustness_spec_id,
        scene_revision_id=spec.scene_revision_id,
        search_spec_id=spec.search_spec_id,
        model_id=spec.model_id,
        model_version=spec.model_version,
        prediction_provider_id=spec.prediction_provider_id,
        fidelity=spec.fidelity,
        objective_evaluation_spec_sha256=spec.objective_evaluation_spec_sha256,
        sampling_strategy=spec.sampling_strategy,
        objectives=tuple(
            _objective_view(
                item,
                samples,
                {
                    axis.axis_id: _AXIS_LABELS.get(
                        axis.parameter,
                        '不確かさ軸',
                    )
                    for axis in spec.axes
                },
            )
            for item in evaluations
        ),
        completeness=_completeness(spec, samples),
        current=not stale,
        stale_reasons=tuple(dict.fromkeys(stale)),
    )


def robustness_comparison_eligibility(
    candidates: tuple[RobustnessCandidatePresentation, ...],
) -> RobustnessComparisonEligibility:
    if len(candidates) < 2:
        return RobustnessComparisonEligibility(
            eligible=False,
            reasons=('比較する候補が2件以上必要です',),
        )
    reasons: list[str] = []
    if any(not item.current for item in candidates):
        reasons.append('現在の保存条件と一致しない候補があるため比較できません')
    first = candidates[0]
    exact_fields = (
        ('model_id', '使用モデルが異なります'),
        ('model_version', 'モデル版が異なります'),
        ('prediction_provider_id', '計算方式が異なります'),
        ('fidelity', '計算精度が異なるため最終比較できません'),
        (
            'objective_evaluation_spec_sha256',
            '評価条件が異なるため比較できません',
        ),
        ('search_spec_id', '探索設定が異なるため比較できません'),
    )
    for field, reason in exact_fields:
        if any(getattr(item, field) != getattr(first, field) for item in candidates[1:]):
            reasons.append(reason)
    objective_signature = tuple(
        (item.objective_id, item.unit, item.direction)
        for item in first.objectives
    )
    for item in candidates[1:]:
        if tuple(
            (metric.objective_id, metric.unit, metric.direction)
            for metric in item.objectives
        ) != objective_signature:
            reasons.append('候補間でばらつき評価の指標集合または方向が一致しません')
            break
    if any(item.completeness.state != 'complete' for item in candidates):
        reasons.append('比較に必要な評価が不足しています')
    return RobustnessComparisonEligibility(
        eligible=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )
