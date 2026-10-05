"""Quality-page display helpers for REV56 measurement evidence.

Pure functions — no Qt — so the uncertainty / stability / stimulus-pin
summary lines shown on the quality page stay testable without a widget.
"""

from __future__ import annotations

from .cad_bass_management_qualification import BassManagementQualification
from .cad_measurement_state import StateComparabilityVerdict
from .cad_measurement_uncertainty import MeasurementUncertaintyBudget
from .cad_stimulus_registry import StimulusMeasurementPin


_TRACEABILITY_LABELS = {
    'traceable_documented': '校正連鎖あり（文書化）',
    'traceable_limited': '校正連鎖あり（一部制限）',
    'relative_only': '相対比較のみ有効',
    'untraceable': 'トレーサビリティなし',
    'unknown': 'トレーサビリティ不明',
}

_STATE_VERDICT_LABELS = {
    'comparable_stable_state': '測定状態は安定',
    'comparable_with_declared_drift': '宣言ドリフト内で比較可能',
    'state_changed': '測定状態が変化（比較・校正の証拠には使えません）',
    'non_stationary_during_capture': '取得中に非定常（再測定が必要です）',
    'insufficient_state_evidence': '測定状態の証拠不足',
}

_STATE_REASON_LABELS = {
    'environment_delta_exceeds': '環境変動が許容超過',
    'occupancy_changed': '占有状態が変化',
    'opening_state_changed': '開口部の状態が変化',
    'opening_requirement_unmet': '開口部の要件を満たしません',
    'dynamic_dsp_state_changed': '動的DSP状態が変化',
    'limiter_detected': 'リミッター動作を検出',
    'noise_regime_changed': 'ノイズ環境が変化',
    'noise_floor_exceeds': 'ノイズフロア超過',
    'repeat_non_stationarity': '繰り返し測定が非定常',
    'elapsed_interval_exceeds': '測定間隔が許容超過',
    'thermal_state_changed': '機器の熱状態が変化',
    'scene_content_changed': 'シーン内容が変化',
    'geometry_or_position_changed': '配置・位置が変化',
    'unknown_device_state': '機器状態が未記録',
    'unknown_occupancy': '占有状態が未記録',
    'unknown_environment': '環境状態が未記録',
    'snapshot_missing': '状態スナップショットがありません',
}


def traceability_label(traceability_class: str) -> str:
    return _TRACEABILITY_LABELS.get(traceability_class, traceability_class)


def state_verdict_label(state: str) -> str:
    return _STATE_VERDICT_LABELS.get(state, state)


def state_reason_label(code: str) -> str:
    return _STATE_REASON_LABELS.get(code, code)


_STIMULUS_VERDICT_LABELS = {
    'ELIGIBLE': '適格',
    'ELIGIBLE_WITH_LIMITATIONS': '制限付き適格',
    'WRONG_REVISION': '版違い',
    'WRONG_SAMPLE_RATE': 'サンプルレート違い',
    'WRONG_LEVEL_OR_CREST_FACTOR': 'レベル/クレスト因子違い',
    'TRANSFORMED_NOT_BIT_EXACT': '再生経路でビット一致しない',
    'INCOMPATIBLE': '手順と不適合',
    'INSUFFICIENT_EVIDENCE': '証拠不足（適格性を判断できません）',
}

_BASS_STATUS_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'not_qualified': '不適格',
    'insufficient_evidence': '証拠不足',
}

_BASS_SCOPE_LABELS = {
    'unqualified': '未修飾',
    'candidate': '候補',
    'qualified_point': '一点適格',
    'qualified_region': '領域適格',
}

_BASS_FAILURE_LABELS = {
    'main_too_weak_below_crossover': 'クロスオーバー以下でメインが不足',
    'sub_too_weak_above_crossover': 'クロスオーバー以上でサブが不足',
    'phase_cancellation_at_splice': 'スプライス帯域で相殺',
    'polarity_mismatch': '極性不一致',
    'delay_mismatch': '遅延不一致',
    'lfe_routing_error': 'LFEルーティング不整合',
    'redirected_bass_routing_error': '転送バスのルーティング不整合',
    'double_bass': 'バスの二重経路',
    'insufficient_sub_headroom': 'サブのヘッドルーム不足',
    'unknown_device_filter_topology': '機器のフィルタトポロジー不明',
    'device_state_mismatch': '測定時と機器状態が不一致',
    'multi_seat_instability': '座席間で合成が不安定',
}


def stimulus_verdict_label(verdict: str) -> str:
    return _STIMULUS_VERDICT_LABELS.get(verdict, verdict)


def bass_status_label(status: str) -> str:
    return _BASS_STATUS_LABELS.get(status, status)


def bass_scope_label(scope: str) -> str:
    return _BASS_SCOPE_LABELS.get(scope, scope)


def bass_failure_label(reason: str) -> str:
    return _BASS_FAILURE_LABELS.get(reason, reason)


def stimulus_pin_line(pin: StimulusMeasurementPin) -> str:
    """One JA line for a stimulus pin bound to the measurement (#608)."""
    return f'刺激ピン: {pin.stimulus_id}（{pin.stimulus_sha256[:12]}…）'


def bass_qualification_line(
    qualification: BassManagementQualification,
) -> str:
    """One JA line for a bass-management qualification verdict (#574)."""
    status = bass_status_label(qualification.status)
    scope = bass_scope_label(qualification.scope)
    if qualification.failure_reasons:
        reasons = '、'.join(
            bass_failure_label(reason)
            for reason in qualification.failure_reasons
        )
        return f'バス管理: {status}（{scope}）— {reasons}'
    return f'バス管理: {status}（{scope}）'


def uncertainty_summary_line(
    budget: MeasurementUncertaintyBudget,
) -> str:
    """One JA line summarizing a budget for the quality detail panel."""
    outcome = budget.outcome
    parts: list[str] = []
    if outcome.expanded_uncertainty is not None:
        decimals = 2 if outcome.expanded_uncertainty < 1.0 else 1
        parts.append(
            f'拡張不確かさ ±{outcome.expanded_uncertainty:.{decimals}f}'
            f'（k={budget.coverage_factor:g}）'
        )
    elif outcome.combined_standard_uncertainty is not None:
        decimals = 2 if outcome.combined_standard_uncertainty < 1.0 else 1
        parts.append(
            f'合成標準不確かさ ±{outcome.combined_standard_uncertainty:.{decimals}f}'
        )
    elif outcome.combined_bound_half_width is not None:
        parts.append(
            f'合成上限 ±{outcome.combined_bound_half_width:.2f}'
        )
    else:
        parts.append('不確かさ未合成（宣言のみ）')
    parts.append(traceability_label(budget.traceability_class))
    if budget.measurand_unit:
        return f'不確かさ: {parts[0]} {budget.measurand_unit} · {parts[1]}'
    return f'不確かさ: {parts[0]} · {parts[1]}'


def state_verdict_line(verdict: StateComparabilityVerdict) -> str:
    """One JA line for the latest state comparability verdict."""
    label = state_verdict_label(verdict.state)
    if verdict.reason_codes:
        reasons = '、'.join(
            state_reason_label(code) for code in verdict.reason_codes
        )
        return f'状態安定性: {label}（{reasons}）'
    return f'状態安定性: {label}'


def quality_evidence_detail_lines(
    measurement_id: str,
    *,
    repository,
) -> list[str]:
    """Evidence lines for one measurement — budgets bound to it plus the
    latest comparability verdict referencing its snapshots. Absence is
    honest: an unbudgeted measurement shows "未登録"."""
    budgets = repository.uncertainty_budgets_for_measurement(measurement_id)
    lines: list[str] = []
    if budgets:
        for budget in budgets[-2:]:
            lines.append(uncertainty_summary_line(budget))
    else:
        lines.append('不確かさ: 未登録（不確かさ予算が作成されていません）')
    snapshots = repository.state_snapshots_for_measurement(measurement_id)
    if snapshots:
        snapshot_ids = {s.snapshot_id for s in snapshots}
        verdicts = [
            v
            for v in repository.list_state_verdicts(
                snapshots[-1].document_id
            )
            if v.subject_snapshot_id in snapshot_ids
            or v.baseline_snapshot_id in snapshot_ids
            or measurement_id in v.subject_measurement_ids
        ]
        if verdicts:
            lines.append(state_verdict_line(verdicts[-1]))
        else:
            lines.append('状態安定性: スナップショット済み（判定なし）')
    else:
        lines.append(
            '状態安定性: 未記録（測定時の状態証拠がありません）'
        )
    return lines
