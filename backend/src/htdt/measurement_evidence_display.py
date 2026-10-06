"""Quality-page display helpers for REV56 measurement evidence.

Pure functions — no Qt — so the uncertainty / stability / stimulus-pin /
device-snapshot / standards-registry summary lines shown on the quality
page stay testable without a widget.
"""

from __future__ import annotations

from .cad_bass_management_qualification import BassManagementQualification
from .cad_device_snapshot import (
    ConfigurationRestoreRecord,
    DeviceConfigurationSnapshot,
)
from .cad_external_standards import (
    ExternalStandardDocument,
    StandardsEvaluationPin,
)
from .cad_measurement_state import StateComparabilityVerdict
from .cad_measurement_uncertainty import MeasurementUncertaintyBudget
from .cad_spatial_campaign import CampaignPointBinding
from .cad_response_target import (
    ResponseTargetProfile,
    SpectralBalanceEvaluation,
)
from .cad_rp22_profile import RP22Evaluation
from .cad_stimulus_registry import StimulusMeasurementPin
from .cad_health_drift import DriftAssessment
from .cad_substitution_impact import ChangeImpactAssessment
from .cad_security_authority import SecurityReview
from .cad_control_scenario import ControlScenarioQualification
from .cad_safe_listening import ExposureAssessment


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


# ---------------------------------------------------------------------------
# REV56-SNAPSTD: device snapshot/restore + external standards registry
# (#592/#599) JA labels — absence and conflicts stay honest.
# ---------------------------------------------------------------------------

_STANDARD_LIFECYCLE_LABELS = {
    'draft': '草案',
    'public_review': '公開レビュー中',
    'dis_fdis_prepublication': 'DIS/FDIS（発行前）',
    'industry_review': '業界レビュー中',
    'published_current': '現行',
    'reaffirmed': '再確認済み（現行）',
    'under_revision': '改正作業中',
    'superseded': '旧版（新版に置き換え）',
    'revised': '改訂済み',
    'withdrawn': '撤回済み',
    'replaced_by': '代替規格あり',
    'historical': '歴史的版（保存用）',
    'status_conflict': '発行元の情報が矛盾',
    'unknown': '状態不明',
}

_STANDARD_ADMISSION_LABELS = {
    'discovered': '発見済み',
    'primary_source_confirmed': '一次情報確認済み',
    'rights_reviewed': '権利確認済み',
    'profile_parsed_mapped': 'プロファイル対応付け済み',
    'mapping_reviewed': 'マッピングレビュー済み',
    'validated': '検証済み',
    'production_eligible': '本番適格',
    'limited': '限定使用',
    'retired_for_new_projects': '新規プロジェクトでは退役',
}

_STANDARD_CAPABILITY_LABELS = {
    'production_eligible': '本番適格',
    'limited': '限定使用',
    'draft_research_only': '草案・研究専用',
    'not_registered': '未登録',
    'source_ambiguous': '一次情報が矛盾',
    'mapping_unvalidated': 'マッピング未検証',
    'license_profile_unavailable': 'ライセンスプロファイル利用不可',
    'superseded_historical_only': '旧版（歴史的参照のみ）',
    'retired_for_new_projects': '新規プロジェクトでは退役',
}

_STANDARD_RIGHTS_LABELS = {
    'public_metadata_only': '公開メタデータのみ',
    'public_open_standard': '公開規格',
    'licensed_internal_profile': 'ライセンス内部プロファイル',
    'user_provided_licensed_source': 'ユーザー提供ライセンス版',
    'derived_rules_allowed': '派生ルール許可',
    'reference_only': '参照専用',
    'redistribution_prohibited': '再配布禁止',
    'unknown_rights': '権利不明',
}

_DEVICE_EVIDENCE_CLASS_LABELS = {
    'device_readback': '機器から読み出し',
    'device_export_backup': '機器バックアップ出力',
    'htdt_applied_request': 'HTDT適用要求',
    'user_recorded': 'ユーザー記録',
    'screenshot_documented': 'スクリーンショット記録',
    'inferred_from_measurement': '測定から推定',
    'unknown': '不明',
}

_DEVICE_TRANSITION_LABELS = {
    'factory_default': '出荷状態',
    'initial_configuration': '初期設定',
    'calibration': '校正',
    'manual_tuning': '手動調整',
    'known_good_promotion': '既知良好への昇格',
    'experiment': '実験',
    'firmware_transition': 'ファームウェア更新',
    'restore': '復元',
    'rollback': 'ロールバック',
    'service': '保守',
    'other': 'その他',
}

_RESTORE_VERDICT_LABELS = {
    'restored_exact_observed_state': '観測状態どおり復元',
    'restored_with_differences': '差異ありで復元',
    'restore_unverified': '復元未検証',
    'restore_incompatible': '復元不適合',
    'restore_failed': '復元失敗',
}

_PORTABILITY_CLASS_LABELS = {
    'portable_to_same_model': '同一機種へ移行可',
    'portable_with_firmware_constraint': 'ファームウェア条件付き移行可',
    'device_instance_bound': '機器個体に紐付き',
    'license_bound': 'ライセンス紐付き',
    'measurement_reuse_conditional': '測定再利用は条件付き',
    'not_portable': '移行不可',
    'unknown': '不明',
}


def standards_lifecycle_label(status: str) -> str:
    return _STANDARD_LIFECYCLE_LABELS.get(status, status)


def standards_admission_label(state: str) -> str:
    return _STANDARD_ADMISSION_LABELS.get(state, state)


def standards_capability_label(verdict: str) -> str:
    return _STANDARD_CAPABILITY_LABELS.get(verdict, verdict)


def standards_rights_label(rights: str) -> str:
    return _STANDARD_RIGHTS_LABELS.get(rights, rights)


def device_evidence_class_label(evidence_class: str) -> str:
    return _DEVICE_EVIDENCE_CLASS_LABELS.get(evidence_class, evidence_class)


def device_transition_label(kind: str) -> str:
    return _DEVICE_TRANSITION_LABELS.get(kind, kind)


def restore_verdict_label(verdict: str) -> str:
    return _RESTORE_VERDICT_LABELS.get(verdict, verdict)


def portability_class_label(portability_class: str) -> str:
    return _PORTABILITY_CLASS_LABELS.get(
        portability_class, portability_class
    )


def standards_document_line(document: ExternalStandardDocument) -> str:
    """One JA line for a registered external standard document (#599):
    edition + lifecycle + admission; a superseding edition is named when
    registered."""
    parts = [
        f'{document.document_number}:{document.edition}',
        standards_lifecycle_label(document.lifecycle),
        standards_admission_label(document.admission),
    ]
    if document.replaced_by:
        parts.append(f'後継 {document.replaced_by}')
    return '外部規格: ' + ' — '.join(parts)


def standards_pin_line(pin: StandardsEvaluationPin) -> str:
    """One JA line for an evaluation's standard pin (#599): the exact
    edition + mapping the result was computed against."""
    mapping = pin.mapping_version or 'マッピングなし'
    return (
        f'規格ピン: {pin.standard_id}@{pin.edition}'
        f'（{mapping}）— {pin.result}'
    )


def device_snapshot_line(snapshot: DeviceConfigurationSnapshot) -> str:
    """One JA line for a device configuration snapshot (#592): identity
    pins stay honest — unobserved pins render 不明, never guessed."""
    identity = ' '.join(
        part for part in (snapshot.manufacturer, snapshot.model) if part
    ) or '機種不明'
    firmware = snapshot.firmware_version or 'ファームウェア不明'
    return (
        f'デバイス状態: {identity}（{firmware}）— '
        f'{device_evidence_class_label(snapshot.evidence_class)} '
        f'{len(snapshot.fields)} 項目 · '
        f'{device_transition_label(snapshot.transition_kind)}'
    )


def restore_record_line(record: ConfigurationRestoreRecord) -> str:
    """One JA line for a restore record (#592): the verdict is the
    evidence-derived claim, never the tool's own success message."""
    return (
        f'復元: {restore_verdict_label(record.verdict)}'
        f'（{record.result_status}）'
    )


# ---------------------------------------------------------------------------
# REV56-CAMPPROFILE: spatial campaign bindings + RP32 labels (#581/#585).
# ---------------------------------------------------------------------------

_POINT_ROLE_LABELS = {
    'reference_alignment': '基準点',
    'optimization': '最適化用',
    'spatial_holdout': '空間ホールドアウト',
    'repeatability': '再現性',
    'diagnostic': '診断',
    'boundary_stress': '境界ストレス',
    'standards_required': '規格要求',
}

_EVALUATION_STATE_LABELS = {
    'valid': '妥当',
    'valid_with_warnings': '警告付き妥当',
    'invalid': '不備あり',
}

_RP32_STATE_LABELS = {
    'verified': '検証済み',
    'verified_with_limitations': '制限付き検証済み',
    'failed': '不合格',
    'incomplete': '未完',
    'inconclusive': '不確定',
}

_RP22_STATE_LABELS = {
    'rp22_design_target': 'RP22設計目標',
    'rp22_as_built_predicted': 'RP22竣工予測',
    'rp22_rp32_measured_verified': 'RP22実測検証済み',
    'rp22_rp32_measured_verified_with_limitations': 'RP22実測検証済み（制限付き）',
    'rp22_rp32_measured_failed': 'RP22実測不合格',
    'rp22_verification_incomplete': 'RP22検証未完',
}


def point_role_label(role: str) -> str:
    return _POINT_ROLE_LABELS.get(role, role)


def campaign_evaluation_state_label(state: str) -> str:
    return _EVALUATION_STATE_LABELS.get(state, state)


def rp32_overall_state_label(state: str) -> str:
    return _RP32_STATE_LABELS.get(state, state)


def rp22_state_label(state: str) -> str:
    return _RP22_STATE_LABELS.get(state, state)


def spatial_binding_line(binding: CampaignPointBinding) -> str:
    """One JA line for a measurement's campaign-point binding (#581).

    Deviation is shown honestly — a binding without an observed position
    reads as 位置未記録, never as zero deviation.
    """
    if binding.observed_position is None:
        deviation = '位置未記録'
    elif binding.deviation_m is not None:
        deviation = f'計画から {binding.deviation_m * 1000:.0f} mm'
    else:
        deviation = '位置未記録'
    return (
        f'測定点束縛: {binding.point_id}（計画 {binding.design_id[:30]}…'
        f' / {deviation}）'
    )


# ---------------------------------------------------------------------------
# REV56-TARGETS: RP22 standards profile + response-target authority
# (#579/#588)
# ---------------------------------------------------------------------------

_RP22_PARAMETER_VERDICT_LABELS = {
    'met': '達成',
    'not_met': '未達成',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '適用外',
    'unsupported': 'マッピング未対応',
}

_RP22_CONFORMANCE_LABELS = {
    'met': '適合',
    'not_met': '不適合',
    'indeterminate': '判定不能',
}

_RP22_EVIDENCE_CLASS_LABELS = {
    'design_prediction': '設計予測',
    'as_built': '竣工時',
    'measured_commissioning': '実測コミッショニング',
    'unknown': '証拠区分不明',
}

_RP22_EVALUATION_KIND_LABELS = {
    'design_evaluation': '設計評価',
    'as_built_evaluation': '竣工評価',
    'measured_commissioning_evaluation': '実測コミッショニング評価',
}

_RP22_MAPPING_STATUS_LABELS = {
    'supported': '対応済み',
    'supported_with_limitations': '制限付き対応',
    'measurement_required': '実測証拠が必要',
    'unsupported': '未対応（正直な未マップ）',
}

_RP22_DYNAMICS_BASIS_LABELS = {
    'nominal_spec_only': '定格仕様のみ',
    'modelled_small_signal': '小信号モデル',
    'modelled_with_output_limits': '出力限界込みモデル',
    'lab_measured_output': '実測出力（ラボ）',
    'in_room_measured_capability': '室内実測能力',
    'commissioned_verified': 'コミッショニング検証済み',
}

_RESPONSE_TARGET_KIND_LABELS = {
    'project_defined': 'プロジェクト定義',
    'user_preference': 'ユーザー選好',
    'provider_device_profile': 'プロバイダ/機器プロファイル',
    'external_standard_profile': '外部規格プロファイル',
    'measured_reference_derived': '実測由来ターゲット',
    'system_capability_derived': 'システム能力由来',
    'research_profile': '研究由来プロファイル',
    'unknown': '由来不明',
}

_TARGET_BINDING_STATE_LABELS = {
    'resolved_exact': '版特定済み',
    'source_version_ambiguous': '版特定が曖昧',
    'unregistered': 'レジストリ未登録',
    'license_profile_unavailable': 'ライセンス制約で取得不可',
}

_SEAT_COVERAGE_LABELS = {
    'evaluated': '評価済み',
    'no_coverage': 'カバレッジなし',
    'normalization_failed': '正規化不可',
}

_SEAT_ROLE_LABELS = {
    'control': 'コントロール席',
    'holdout': 'ホールドアウト席',
    'evaluation': '評価席',
}


def rp22_parameter_verdict_label(verdict: str) -> str:
    return _RP22_PARAMETER_VERDICT_LABELS.get(verdict, verdict)


def rp22_conformance_label(verdict: str) -> str:
    return _RP22_CONFORMANCE_LABELS.get(verdict, verdict)


def rp22_evidence_class_label(evidence_class: str) -> str:
    return _RP22_EVIDENCE_CLASS_LABELS.get(evidence_class, evidence_class)


def rp22_evaluation_kind_label(kind: str) -> str:
    return _RP22_EVALUATION_KIND_LABELS.get(kind, kind)


def rp22_mapping_status_label(status: str) -> str:
    return _RP22_MAPPING_STATUS_LABELS.get(status, status)


def rp22_dynamics_basis_label(basis: str) -> str:
    return _RP22_DYNAMICS_BASIS_LABELS.get(basis, basis)


def response_target_kind_label(kind: str) -> str:
    return _RESPONSE_TARGET_KIND_LABELS.get(kind, kind)


def target_binding_state_label(state: str) -> str:
    return _TARGET_BINDING_STATE_LABELS.get(state, state)


def seat_coverage_label(status: str) -> str:
    return _SEAT_COVERAGE_LABELS.get(status, status)


def seat_role_label(role: str) -> str:
    return _SEAT_ROLE_LABELS.get(role, role)


def rp22_evaluation_line(evaluation: RP22Evaluation) -> str:
    """One JA line for an RP22 conformance evaluation (#579): the
    evaluation kind, requested level and overall verdict stay separate —
    a requested level is never claimed as achieved."""
    met = sum(
        1 for item in evaluation.results if item.verdict == 'met'
    )
    return (
        f'RP22評価: {rp22_evaluation_kind_label(evaluation.evaluation_kind)}'
        f' 要求レベル {evaluation.requested_level}'
        f' — {rp22_conformance_label(evaluation.strict_conformance)}'
        f'（達成 {met}/21'
        f'・証拠不足 {len(evaluation.insufficient_parameter_ids)}）'
    )


def response_target_line(profile: ResponseTargetProfile) -> str:
    """One JA line for a response-target profile (#588): kind + declared
    comparison semantics; an external binding shows its state honestly."""
    parts = [
        response_target_kind_label(profile.kind),
        f'帯域 {profile.frequency_validity_hz or "全域"}',
        f'集約 {profile.semantics.aggregation}',
    ]
    if profile.external_binding is not None:
        parts.append(
            target_binding_state_label(profile.external_binding.binding_state)
        )
    return '応答目標: ' + ' — '.join(parts)


def spectral_balance_line(evaluation: SpectralBalanceEvaluation) -> str:
    """One JA line for a spectral-balance evaluation (#588): target
    tracking and seat-to-seat spread are reported as separate numbers,
    never one hidden score."""
    target = (
        f'目標乖離RMS {evaluation.mean_rms_target_deviation_db:.2f} dB'
        if evaluation.mean_rms_target_deviation_db is not None
        else '目標乖離なし'
    )
    spread = (
        f'座席間ばらつき最大 {evaluation.seat_to_seat_spread_max_db:.2f} dB'
        if evaluation.seat_to_seat_spread_max_db is not None
        else '座席間ばらつきなし'
    )
    return (
        f'スペクトルバランス: {target} · {spread}'
        f'（{evaluation.seats_evaluated} 席評価'
        f'・{evaluation.seats_without_coverage} 席カバレッジなし）'
    )


# ---------------------------------------------------------------------------
# REV56-ELEC: electrical compatibility + physical interconnect (#593/#597).
# ---------------------------------------------------------------------------

from .cad_electrical_compatibility import ElectricalPlaybackQualification
from .cad_physical_interconnect import (
    LogicalPhysicalBinding,
    PathStateAssessment,
    PhysicalInterconnect,
    WiringTestResult,
)

_ELEC_VERDICT_LABELS = {
    'qualified': '適合',
    'unqualified': '不適合',
    'indeterminate': '証拠不足（未判定）',
}

_ELEC_FAILURE_LABELS = {
    'load_below_amplifier_rating': '負荷インピーダンスがアンプ定格下限を下回る',
    'current_margin_insufficient': '電流マージン不足',
    'voltage_margin_insufficient': '電圧マージン不足',
    'multichannel_power_limit': 'マルチチャンネル同時出力制限',
    'digital_headroom_limit': 'デジタルヘッドルーム超過',
    'amplifier_clipping': 'アンプ出力上限超過（クリップ）',
    'thermal_derating': '連続熱定格超過',
    'cable_loss_excessive': 'ケーブル損失過大（アンプ出力不達）',
    'loudspeaker_compression_limit': 'スピーカー出力上限超過',
    'protection_engagement': '保護動作条件',
    'insufficient_evidence': '証拠不足',
}

_ELEC_CAPABILITY_CLASS_LABELS = {
    'spec_sheet_estimate': 'スペックシート推定',
    'electrically_qualified_model': '電気モデル適合済み',
    'acoustic_output_measured': '音響出力実測済み',
    'in_room_commissioned': '現地コミッショニング済み',
}

_ELEC_STRESS_LABELS = {
    'short_burst': '短時間バースト',
    'program_like': 'プログラム同等',
    'sustained': '連続持続',
    'thermally_stabilized': '熱定常',
}

_PATH_EVIDENCE_LABELS = {
    'designed_path': '設計上の経路',
    'installed_reported_path': '設置報告済み経路',
    'field_observed_path': '現場目視済み経路',
    'verified_path': '検証済み経路',
    'unverified_declaration': '未検証の宣言',
    'failed_path': '検証不合格経路',
}

_PATH_OBSERVATION_LABELS = {
    'observed_both_ends': '両端目視済み',
    'observed_one_end': '片端のみ目視',
    'documented_not_observed': '書面のみ（未目視）',
    'inferred_by_test': '測定による推定',
    'unknown_route': '経路不明',
}

_BINDING_STATE_LABELS = {
    'verified_binding': '検証済みバインド',
    'observed_binding': '目視済みバインド',
    'reported_binding': '報告済みバインド',
    'designed_binding': '設計上のバインド',
    'unverified_routing': '未検証ルーティング',
    'unverified_declaration': '未検証の宣言',
    'mismatched': '端点不一致',
    'stale': '改訂により陳腐化',
    'failed_path': '物理経路不合格',
}

_WIRING_TEST_LABELS = {
    'continuity_test': '導通試験',
    'insulation_test': '絶縁試験',
    'impedance_sweep': 'インピーダンス掃引',
    'level_continuity_check': 'レベル導通確認',
    'polarity_check': '極性確認',
    'pairing_response_check': 'ペアリング応答確認',
    'bitstream_integrity_check': 'ビットストリーム整合性確認',
    'end_to_end_signal_check': 'エンドツーエンド信号確認',
    'visual_label_check': 'ラベル目視確認',
    'length_measurement': '長さ実測',
    'domain_qualification': 'ドメイン適合評価',
}

_WIRING_RESULT_LABELS = {
    'pass': '合格',
    'fail': '不合格',
    'inconclusive': '不確定',
}


def electrical_verdict_label(verdict: str) -> str:
    return _ELEC_VERDICT_LABELS.get(verdict, verdict)


def electrical_failure_label(code: str) -> str:
    return _ELEC_FAILURE_LABELS.get(code, code)


def capability_class_label(capability_class: str) -> str:
    return _ELEC_CAPABILITY_CLASS_LABELS.get(
        capability_class, capability_class
    )


def stress_profile_label(profile: str) -> str:
    return _ELEC_STRESS_LABELS.get(profile, profile)


def path_evidence_label(state: str) -> str:
    return _PATH_EVIDENCE_LABELS.get(state, state)


def path_observation_label(state: str) -> str:
    return _PATH_OBSERVATION_LABELS.get(state, state)


def binding_state_label(state: str) -> str:
    return _BINDING_STATE_LABELS.get(state, state)


def wiring_test_label(kind: str) -> str:
    return _WIRING_TEST_LABELS.get(kind, kind)


def wiring_result_label(result: str) -> str:
    return _WIRING_RESULT_LABELS.get(result, result)


def electrical_qualification_line(
    qualification: ElectricalPlaybackQualification,
) -> str:
    """One JA line for an electrical qualification (#593): verdict +
    dominant limiter + failure reasons; indeterminate is never rendered
    as a fail or a pass."""
    parts = [electrical_verdict_label(qualification.verdict)]
    if qualification.dominant_limiter is not None:
        parts.append(
            f'主制約 {qualification.dominant_limiter}'
        )
    if qualification.failure_codes:
        parts.append(
            '理由: '
            + '・'.join(
                electrical_failure_label(code)
                for code in qualification.failure_codes
            )
        )
    parts.append(capability_class_label(qualification.capability_class))
    return '電気適合性: ' + ' — '.join(parts)


def physical_interconnect_line(
    path: PhysicalInterconnect,
    assessment: PathStateAssessment | None = None,
) -> str:
    """One JA line for a physical interconnect (#597): declared and
    evaluated states are reported separately so a declaration never
    parades as verification."""
    state = (
        assessment.evaluated_state
        if assessment is not None
        else path.evidence_state
    )
    parts = [
        f'{path.path_id} v{path.version}',
        f'{path_class_label(path.path_class)}',
        path_evidence_label(path.evidence_state),
    ]
    if assessment is not None and state != path.evidence_state:
        parts.append(f'評価: {path_evidence_label(state)}')
    observed = (
        path_observation_label(path.observation_state)
        if path.observation_state is not None
        else '経路状態不明'
    )
    parts.append(observed)
    return '物理経路: ' + ' — '.join(parts)


def path_class_label(path_class: str) -> str:
    return _PATH_CLASS_LABELS.get(path_class, path_class)


_PATH_CLASS_LABELS = {
    'analog_speaker_wire': 'アナログ・スピーカー線',
    'analog_balanced_audio': 'アナログ・バランス音声',
    'analog_unbalanced_audio': 'アナログ・アンバランス音声',
    'digital_audio': 'デジタル音声',
    'network_audio_stream': 'ネットワーク音声ストリーム',
    'hdmi_family': 'HDMI系',
    'avio_streaming': 'AVoIPストリーミング',
    'control_signaling': '制御信号',
    'wireless_logical': '無線（論理）',
    'power_feed': '電源フィード',
}


def logical_binding_line(binding: LogicalPhysicalBinding) -> str:
    """One JA line for a logical→physical binding (#597): the state names
    what evidence stands behind the routing claim."""
    return (
        f'バインド: {binding.logical_path_ref} → '
        f'{binding.physical_path_id} — '
        f'{binding_state_label(binding.binding_state)}'
    )


def wiring_test_line(result: WiringTestResult) -> str:
    """One JA line for a wiring test result (#597): test kind + verdict +
    measured value when present."""
    parts = [
        wiring_test_label(result.test_kind),
        wiring_result_label(result.result),
    ]
    if result.measured_value is not None and result.measured_unit:
        parts.append(
            f'実測 {result.measured_value} {result.measured_unit}'
        )
    return '検証: ' + ' — '.join(parts)

# ---------------------------------------------------------------------------
# REV56-LIFECYCLE (#595 health/drift monitoring, #596 substitution impact)
# ---------------------------------------------------------------------------

_OBSERVATION_KIND_LABELS = {
    'config_hash': '構成ハッシュ',
    'device_online_state': '機器オンライン状態',
    'device_error_warning': '機器エラー/警告',
    'dsp_config_hash': 'DSP構成ハッシュ',
    'firmware_version_observed': 'ファームウェアバージョン',
    'license_expiry': 'ライセンス期限',
    'clock_sync': '時刻同期',
    'environment_temperature': '環境温度',
    'environment_humidity': '環境湿度',
    'network_state': 'ネットワーク状態',
    'av_transport_state': 'AV伝送状態',
    'reference_sweep_result': 'リファレンススイープ結果',
    'service_observation': 'サービス観察',
    'manual_inspection': '目視点検',
    'storage_health': 'ストレージ健全性',
    'room_as_built_change': '竣工状態変化',
}

_MONITORING_CAPABILITY_LABELS = {
    'telemetry_auto': '自動テレメトリ',
    'telemetry_api': 'APIテレメトリ',
    'manual_observation': '手動観察',
    'measurement_capable': '測定可能',
    'event_source': 'イベントソースあり',
    'unobservable': '監視不能',
}

_COLLECTION_MODE_LABELS = {
    'live_telemetry': 'ライブテレメトリ',
    'manual_entry': '手動入力',
    'reference_measurement': 'リファレンス測定',
    'log_import': 'ログ取込',
    'declared_change': '宣言変更',
    'unavailable': '取得不能',
}

_DRIFT_CLASSIFICATION_LABELS = {
    'no_material_change': '実質的変化なし',
    'expected_change': '想定内の変化',
    'configuration_drift': '構成ドリフト',
    'performance_drift': '性能ドリフト',
    'intermittent_fault': '間欠故障',
    'hard_failure': 'ハード故障',
    'evidence_stale': '証拠の陳腐化',
    'insufficient_observability': '観測性不足',
    'not_comparable': '比較不能',
}

_IMPACT_DISPOSITION_LABELS = {
    'stale': '陳腐化',
    'recompute': '再計算',
    'remeasure': '再測定',
    'review_required': 'レビュー要',
    'unaffected': '影響なし',
}

_REVERIFICATION_ACTION_LABELS = {
    'no_action': '対応不要',
    'observe': '観察継続',
    'service_review': 'サービスレビュー',
    'restore_known_good_config': '既知良好構成への復元',
    'run_reference_check': 'リファレンスチェック実行',
    'reverify_domain': 'ドメイン再検証',
    'full_recommission_required': '全面再コミッショニング要',
}

_OPERATIONAL_SEVERITY_LABELS = {
    'none': 'なし',
    'cosmetic': '軽微',
    'functional': '機能影響',
    'safety_critical': '重大',
}

_EVIDENCE_CERTAINTY_LABELS = {
    'confirmed': '確認済み',
    'suspected': '疑いあり',
    'insufficient_evidence': '証拠不足',
}

_TREND_STATE_LABELS = {
    'within_band': '帯域内',
    'drift_detected': 'ドリフト検出',
    'step_detected': 'ステップ変化検出',
    'insufficient_samples': 'サンプル不足',
    'no_threshold': '閾値未設定',
}

_RESTORE_CONFIRMATION_LABELS = {
    'restored_confirmed': '復元を確認',
    'restored_partial': '復元は部分的',
    'not_restored': '復元されていません',
    'insufficient_checks': '確認チェック不足',
}

_DIMENSION_LABELS = {
    'physical_installation': '物理設置',
    'electroacoustic': '電気音響',
    'signal_dsp': '信号/DSP',
    'video_projection': '映像/プロジェクション',
    'interoperability': '相互運用性',
    'infrastructure': 'インフラ',
    'lifecycle_support': 'ライフサイクル/保守',
}

_DIMENSION_VERDICT_LABELS = {
    'equivalent_by_same_exact_evidence': '同一証拠により同等',
    'meets_or_exceeds_requirement': '要件を満たす/上回る',
    'different_but_acceptable': '差異あり・許容',
    'inferior': '低下',
    'incompatible': '不適合',
    'insufficient_evidence': '証拠不足',
    'not_applicable': '該当なし',
}

_TECHNICAL_VERDICT_LABELS = {
    'technically_acceptable': '技術的に許容',
    'technically_limited': '条件付きで技術的に許容',
    'technically_incompatible': '技術的に不適合',
    'indeterminate': '判定不能',
}

_APPROVAL_STATE_LABELS = {
    'proposed': '提案済み',
    'evidence_review': '証拠レビュー中',
    'engineering_approved': '技術承認済み',
    'engineering_approved_with_limitations': '制限付き技術承認',
    'rejected': '却下',
    'commercial_override_accepted': '商業オーバーライド受理',
    'installed_unverified': '設置済み・未検証',
    'as_built_verified': '竣工照合済み',
}

_ASBUILT_VERDICT_LABELS = {
    'matches_approved': '承認品と一致',
    'differs_from_approved': '承認品と不一致',
    'identity_unverified': '識別未検証',
    'no_approved_baseline': '承認ベースラインなし',
}

_EQUIVALENCE_CLASS_LABELS = {
    'same_exact_evidence': '同一型番・同一証拠',
    'verified_equivalent': '検証済み同等',
    'declared_equivalent': 'スペック同等宣言',
    'insufficient': '証拠不足',
}

_SCHEDULE_PHASE_LABELS = {
    'design': '設計',
    'approved_substitution': '承認済み代替',
    'procured': '調達済み',
    'installed_as_built': '設置済み（竣工）',
    'service_replacement': 'サービス交換',
}


def observation_kind_label(kind: str) -> str:
    return _OBSERVATION_KIND_LABELS.get(kind, kind)


def monitoring_capability_label(capability: str) -> str:
    return _MONITORING_CAPABILITY_LABELS.get(capability, capability)


def collection_mode_label(mode: str) -> str:
    return _COLLECTION_MODE_LABELS.get(mode, mode)


def drift_classification_label(classification: str) -> str:
    return _DRIFT_CLASSIFICATION_LABELS.get(classification, classification)


def impact_disposition_label(disposition: str) -> str:
    return _IMPACT_DISPOSITION_LABELS.get(disposition, disposition)


def reverification_action_label(action: str) -> str:
    return _REVERIFICATION_ACTION_LABELS.get(action, action)


def operational_severity_label(severity: str) -> str:
    return _OPERATIONAL_SEVERITY_LABELS.get(severity, severity)


def evidence_certainty_label(certainty: str) -> str:
    return _EVIDENCE_CERTAINTY_LABELS.get(certainty, certainty)


def trend_state_label(state: str) -> str:
    return _TREND_STATE_LABELS.get(state, state)


def restore_confirmation_label(verdict: str) -> str:
    return _RESTORE_CONFIRMATION_LABELS.get(verdict, verdict)


def substitution_dimension_label(dimension: str) -> str:
    return _DIMENSION_LABELS.get(dimension, dimension)


def dimension_verdict_label(verdict: str) -> str:
    return _DIMENSION_VERDICT_LABELS.get(verdict, verdict)


def technical_verdict_label(verdict: str) -> str:
    return _TECHNICAL_VERDICT_LABELS.get(verdict, verdict)


def approval_state_label(state: str) -> str:
    return _APPROVAL_STATE_LABELS.get(state, state)


def asbuilt_verdict_label(verdict: str) -> str:
    return _ASBUILT_VERDICT_LABELS.get(verdict, verdict)


def equivalence_class_label(evidence_class: str) -> str:
    return _EQUIVALENCE_CLASS_LABELS.get(evidence_class, evidence_class)


def schedule_phase_label(phase: str) -> str:
    return _SCHEDULE_PHASE_LABELS.get(phase, phase)


def drift_assessment_line(assessment: DriftAssessment) -> str:
    """One JA line for a drift assessment (#595): severity and certainty
    stay separate — no opaque health score."""
    parts = [
        operational_severity_label(assessment.operational_severity),
        evidence_certainty_label(assessment.evidence_certainty),
    ]
    worst = [
        drift_classification_label(c.classification)
        for c in assessment.components
        if c.classification
        not in {'no_material_change', 'expected_change'}
    ]
    if worst:
        parts.append(' / '.join(dict.fromkeys(worst)))
    else:
        parts.append('各ドメインで実質的変化なし')
    return 'ドリフト評価: ' + ' — '.join(parts)


def change_impact_line(assessment: ChangeImpactAssessment) -> str:
    """One JA line for a substitution impact assessment (#596): the
    derived technical verdict plus the count of re-verification targets."""
    parts = [technical_verdict_label(assessment.technical_verdict)]
    active = [
        e for e in assessment.affected_entries
        if e.disposition != 'unaffected'
    ]
    if active:
        parts.append(f'影響権威 {len(active)} 件')
    else:
        parts.append('影響権威なし')
    return '変更影響: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# REV56-OPS: #598 security, #601 control qualification, #602 exposure
# ---------------------------------------------------------------------------

_SECURITY_STATE_LABELS = {
    'not_applicable': '対象外',
    'unknown': '未評価',
    'reviewed': 'レビュー済み',
    'reviewed_with_limitations': 'レビュー済み(限定あり)',
    'risk_accepted': 'リスク受容済み',
    'mitigation_required': '緩和対応が必要',
    'high_risk_exposure': '高リスク曝露',
}

_SECURITY_CHECK_LABELS = {
    'inventory': '資産棚卸し',
    'credentials': '資格情報',
    'surfaces': '管理サーフェス',
    'firmware': 'ファームウェア/パッチ',
    'remote_access': 'リモートアクセス',
    'backup_sensitivity': 'バックアップ機微性',
    'access_review': 'アクセスレビュー',
    'decommission': '廃止手続き',
}

_SCENARIO_STATE_LABELS = {
    'draft': '宣言のみ(未評価)',
    'not_executed': '未実行 — 動作保証なし',
    'stale': '旧リビジョンの証拠のみ',
    'unverified': '検証不足',
    'qualified': '修飾済み',
    'qualified_with_deviations': '修飾済み(偏差あり)',
    'failed': '不合格',
}

_SCENARIO_CHECK_LABELS = {
    'declaration': 'シナリオ宣言',
    'feedback_expectation': 'フィードバック期待値',
    'timeouts': 'タイムアウト',
    'execution_coverage': '実行カバレッジ',
    'failure_notification': '失敗時通知',
    'revision_freshness': 'リビジョン新規性',
}

_EXPOSURE_STATE_LABELS = {
    'not_applicable': '対象外',
    'unknown': '不明(限界未宣言)',
    'within_limit': '限界内',
    'gate_required': 'ゲート判定が必要',
    'approved': 'ゲート承認済み',
    'approved_with_controls': '条件付きゲート承認',
    'blocked': 'ゲートにより阻止',
}

_CHECK_RESULT_LABELS = {
    'verified': '検証済み',
    'limited': '限定あり',
    'failed': '不合格',
    'not_applicable': '対象外',
}


def security_state_label(state: str) -> str:
    return _SECURITY_STATE_LABELS.get(state, state)


def security_check_label(check: str) -> str:
    return _SECURITY_CHECK_LABELS.get(check, check)


def scenario_state_label(state: str) -> str:
    return _SCENARIO_STATE_LABELS.get(state, state)


def scenario_check_label(check: str) -> str:
    return _SCENARIO_CHECK_LABELS.get(check, check)


def exposure_state_label(state: str) -> str:
    return _EXPOSURE_STATE_LABELS.get(state, state)


def check_result_label(result: str) -> str:
    return _CHECK_RESULT_LABELS.get(result, result)


def security_review_line(review: SecurityReview) -> str:
    """One JA line for a security review verdict (#598): state plus the
    failed/limited dimensions — never an opaque "secure" claim."""
    parts = [security_state_label(review.state)]
    failed = [
        security_check_label(c) for c, r in review.checks
        if r == 'failed'
    ]
    limited = [
        security_check_label(c) for c, r in review.checks
        if r == 'limited'
    ]
    if failed:
        parts.append('不合格: ' + ' / '.join(failed))
    if limited:
        parts.append('限定: ' + ' / '.join(limited))
    if not failed and not limited and review.state in (
        'reviewed', 'risk_accepted',
    ):
        parts.append('全次元検証済み')
    return 'セキュリティレビュー: ' + ' — '.join(parts)


def scenario_qualification_line(
    qualification: ControlScenarioQualification,
) -> str:
    """One JA line for a control-scenario qualification (#601): the
    fail-closed state — an unexecuted scenario reads 未実行, never
    動作保証."""
    parts = [scenario_state_label(qualification.state)]
    failed = [
        scenario_check_label(c) for c, r in qualification.checks
        if r == 'failed'
    ]
    if failed:
        parts.append('不合格: ' + ' / '.join(failed))
    if qualification.deviations:
        parts.append(f'偏差 {len(qualification.deviations)} 件')
    return '制御シナリオ修飾: ' + ' — '.join(parts)


def exposure_assessment_line(assessment: ExposureAssessment) -> str:
    """One JA line for an exposure assessment (#602): the verdict and
    the projected dose — '鳴らせる' と '聴いてよい' の分離を保つ。"""
    parts = [exposure_state_label(assessment.state)]
    if assessment.projected_dose_pct is not None:
        parts.append(f'予測線量 {assessment.projected_dose_pct:.0f}%')
    if assessment.allowable_duration_s is not None:
        parts.append(f'許容時間 {assessment.allowable_duration_s:.0f} 秒')
    failed = [
        c for c, r in assessment.checks if r == 'failed'
    ]
    if failed:
        parts.append('不合格: ' + ' / '.join(failed))
    return '曝露評価: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# REV57-METRO: #609 タイムベース/クロック権威, #610 証拠バンドル, #611 校正ライフサイクル
#

_TIMEBASE_CAPABILITY_LABELS = {
    'magnitude_vs_time': '振幅-時間プロファイル',
    'absolute_delay': '絶対遅延',
    'relative_delay_between_channels': 'チャンネル間相対遅延',
    'phase_response': '位相応答',
    'group_delay': '群遅延',
    'impulse_response_alignment': 'インパルス応答アライメント',
    'frequency_response_magnitude': '周波数応答(振幅)',
    'vector_averaging_complex_transfer': 'ベクトル平均/複素伝達関数',
}

_CAPABILITY_STATE_LABELS = {
    'valid': '有効',
    'limited': '限定',
    'invalid': '不可',
    'unknown': '不明',
}

_BUNDLE_VALIDATION_STATE_LABELS = {
    'complete_valid': '完全・検証済',
    'complete_but_external_dependencies': '完全・外部依存あり',
    'incomplete': '不完全',
    'integrity_failure': '整合性不整合',
    'profile_mismatch': 'プロファイル不一致',
    'unresolved_reference': '未解決参照',
}

_INSTRUMENT_FITNESS_LABELS = {
    'fit_for_purpose': '用途適合',
    'fit_with_limitations': '限定付き適合',
    'calibration_overdue_by_policy': '校正期限超過(ポリシー)',
    'calibration_review_required': '校正レビュー要',
    'check_required': 'チェック要',
    'out_of_tolerance': '公差外',
    'unknown': '不明',
}

_UNTAMPERED_LABEL = '未検証'


def timebase_capability_label(capability: str) -> str:
    return _TIMEBASE_CAPABILITY_LABELS.get(capability, capability)


def capability_state_label(state: str) -> str:
    return _CAPABILITY_STATE_LABELS.get(state, state)


def bundle_validation_state_label(state: str) -> str:
    return _BUNDLE_VALIDATION_STATE_LABELS.get(state, state)


def instrument_fitness_state_label(state: str) -> str:
    return _INSTRUMENT_FITNESS_LABELS.get(state, state)


def timebase_capability_line(assessment) -> str:
    """One JA line for a timebase capability assessment (#609): the
    per-capability states — 公称レート一致だけで位相は有効と読まない。"""
    parts = []
    for capability, state in assessment.capabilities:
        label = timebase_capability_label(capability)
        parts.append(f'{label}: {capability_state_label(state)}')
    if assessment.notes:
        parts.append(assessment.notes)
    return 'タイムベース能力: ' + ' — '.join(parts)


_BUNDLE_CHECK_LABELS = {
    'status_finalized': '確定状態',
    'manifest_root': 'マニフェストルート',
    'payload_presence': 'ペイロード存在',
    'digest_match': 'ダイジェスト一致',
    'external_dependencies': '外部依存',
    'required_roles': '必須ロール',
    'derivation_integrity': '派生整合性',
    'reproducibility_vs_content': '再現性と内容',
}

_BUNDLE_CHECK_RESULT_LABELS = {
    'verified': '検証済',
    'limited': '限定',
    'failed': '不合格',
    'not_applicable': '対象外',
}


def bundle_check_label(check: str) -> str:
    return _BUNDLE_CHECK_LABELS.get(check, check)


def bundle_check_result_label(result: str) -> str:
    return _BUNDLE_CHECK_RESULT_LABELS.get(result, result)


def bundle_validation_line(verdict) -> str:
    """One JA line for a bundle validation verdict (#610): fail-closed
    state — 未解決参照・整合性不整合を完全と読み違えない。"""
    parts = [bundle_validation_state_label(verdict.state)]
    checks_failed = [
        bundle_check_label(check)
        for check, result in verdict.checks
        if result == 'failed'
    ]
    if checks_failed:
        parts.append('不整合: ' + ' / '.join(checks_failed))
    if verdict.missing_roles:
        parts.append(f'欠落ロール {len(verdict.missing_roles)} 件')
    if verdict.unresolved_references:
        parts.append(f'未解決参照 {len(verdict.unresolved_references)} 件')
    if verdict.external_dependencies:
        parts.append(f'外部依存 {len(verdict.external_dependencies)} 件')
    return '証拠バンドル検証: ' + ' — '.join(parts)


def instrument_fitness_line(assessment) -> str:
    """One JA line for an instrument fitness assessment (#611): the
    as-of state — 期限超過は機器の物理的故障とは読み分ける。"""
    parts = [
        f'{instrument_fitness_state_label(assessment.state)}'
        f' ({assessment.at_utc} 時点)'
    ]
    if assessment.limitations:
        parts.append('制限: ' + ' / '.join(assessment.limitations))
    if assessment.reasons:
        parts.append(assessment.reasons[0])
    return '機器適性: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# REV57-PHYS (#613/#614/#615): geometry survey, installed-source boundary,
# porous absorber authorities.

from .cad_geometry_survey import (  # noqa: E402
    AsBuiltGeometryQualification,
    ELEMENT_STATE_LABELS as _GEO_ELEMENT_STATE_LABELS,
    EVIDENCE_CLASS_LABELS as _GEO_EVIDENCE_CLASS_LABELS,
    REASON_LABELS as _GEO_REASON_LABELS,
    TASK_CLASS_LABELS as _GEO_TASK_CLASS_LABELS,
    TASK_VERDICT_LABELS as _GEO_TASK_VERDICT_LABELS,
    CAPABILITY_CLASS_LABELS as _GEO_CAPABILITY_LABELS,
)
from .cad_installed_source_boundary import (  # noqa: E402
    CAPABILITY_LABELS as _SRC_CAPABILITY_LABELS,
    VERIFICATION_STATE_LABELS as _SRC_VERIFICATION_LABELS,
    MOUNTING_KIND_LABELS as _SRC_MOUNTING_LABELS,
    REASON_LABELS as _SRC_REASON_LABELS,
    QUALIFICATION_STATE_LABELS as _SRC_STATE_LABELS,
    InstalledSourceQualification,
)
from .cad_porous_absorber import (  # noqa: E402
    ELIGIBILITY_LABELS as _PAM_ELIGIBILITY_LABELS,
    ELIGIBILITY_REASON_LABELS as _PAM_REASON_LABELS,
    FIT_VERDICT_LABELS as _PAM_FIT_LABELS,
    MODEL_FAMILY_LABELS as _PAM_FAMILY_LABELS,
    PorousBoundaryPrediction,
    PorousFitComparison,
)


def geometry_element_state_label(state: str) -> str:
    return _GEO_ELEMENT_STATE_LABELS.get(state, state)


def geometry_task_verdict_label(verdict: str) -> str:
    return _GEO_TASK_VERDICT_LABELS.get(verdict, verdict)


def geometry_evidence_class_label(evidence_class: str) -> str:
    return _GEO_EVIDENCE_CLASS_LABELS.get(evidence_class, evidence_class)


def geometry_task_class_label(task_class: str) -> str:
    return _GEO_TASK_CLASS_LABELS.get(task_class, task_class)


def survey_capability_label(capability: str) -> str:
    return _GEO_CAPABILITY_LABELS.get(capability, capability)


def geometry_qualification_reason_label(reason: str) -> str:
    return _GEO_REASON_LABELS.get(reason, reason)


def geometry_qualification_line(
    qualification: AsBuiltGeometryQualification,
) -> str:
    """One JA line for a geometry qualification (#613): the strictest
    element state, then which tasks passed/failed — CAD 精度宣言が
    as-built 証跡を超えることはない。"""
    states = [e.state for e in qualification.element_states]
    worst = states[0] if states else 'insufficient_evidence'
    for state in (
        'stale_after_change', 'insufficient_evidence', 'design_only',
        'control_check_failed', 'registration_limited',
        'observed_unqualified', 'field_checked',
    ):
        if state in states:
            worst = state
            break
    parts = [geometry_element_state_label(worst)]
    parts.append(f'{len(qualification.element_states)} 要素')
    fitted = [
        geometry_task_class_label(v.task_class)
        for v in qualification.task_verdicts
        if v.verdict == 'fit_for_declared_task'
    ]
    blocked = [
        geometry_task_class_label(v.task_class)
        for v in qualification.task_verdicts
        if v.verdict != 'fit_for_declared_task'
    ]
    if fitted:
        parts.append('適合: ' + ' / '.join(fitted))
    if blocked:
        parts.append('不足: ' + ' / '.join(blocked))
    return '幾何適格評価: ' + ' — '.join(parts)


def installed_source_state_label(state: str) -> str:
    return _SRC_STATE_LABELS.get(state, state)


def installed_mounting_label(kind: str) -> str:
    return _SRC_MOUNTING_LABELS.get(kind, kind)


def boundary_capability_label(capability: str) -> str:
    return _SRC_CAPABILITY_LABELS.get(capability, capability)


def boundary_verification_label(state: str) -> str:
    return _SRC_VERIFICATION_LABELS.get(state, state)


def installed_source_reason_label(reason: str) -> str:
    return _SRC_REASON_LABELS.get(reason, reason)


def installed_source_line(
    qualification: InstalledSourceQualification,
) -> str:
    """One JA line for an installed-source qualification (#614): state,
    achieved capability and verification — free-field データは境界設置を
    黙認しない。"""
    parts = [installed_source_state_label(qualification.state)]
    parts.append(boundary_capability_label(qualification.achieved_capability))
    if qualification.verification != 'unverified':
        parts.append(boundary_verification_label(qualification.verification))
    notes = [
        installed_source_reason_label(r) for r in qualification.reasons
        if r in (
            'FINITE_BAFFLE_NOT_HALF_SPACE', 'REAR_CAVITY_UNCHARACTERIZED',
            'DSP_DOUBLE_COMPENSATION_RISK',
            'MEASUREMENT_INCOMPATIBLE_WITH_MOUNTING',
        )
    ]
    if notes:
        parts.append(' / '.join(notes))
    return '設置スピーカー境界評価: ' + ' — '.join(parts)


def porous_eligibility_label(eligibility: str) -> str:
    return _PAM_ELIGIBILITY_LABELS.get(eligibility, eligibility)


def porous_eligibility_reason_label(reason: str) -> str:
    return _PAM_REASON_LABELS.get(reason, reason)


def porous_fit_verdict_label(verdict: str) -> str:
    return _PAM_FIT_LABELS.get(verdict, verdict)


def porous_model_family_label(family: str) -> str:
    return _PAM_FAMILY_LABELS.get(family, family)


def porous_prediction_line(prediction: PorousBoundaryPrediction) -> str:
    """One JA line for a porous prediction (#615): model family,
    eligibility and excluded bands — 妥当域外は絶対に射影しない。"""
    parts = [porous_eligibility_label(prediction.eligibility)]
    emitted = len(prediction.bands)
    parts.append(f'{emitted} 帯域算出')
    if prediction.excluded_bands_hz:
        low = min(prediction.excluded_bands_hz)
        high = max(prediction.excluded_bands_hz)
        parts.append(
            f'除外帯域 {low:g}–{high:g} Hz'
            f' ({len(prediction.excluded_bands_hz)} 点)'
        )
    if prediction.limitations:
        parts.append(
            ' / '.join(
                porous_eligibility_reason_label(r)
                for r in prediction.limitations
            )
        )
    return '多孔材予測: ' + ' — '.join(parts)


def porous_fit_line(comparison: PorousFitComparison) -> str:
    """One JA line for a fit comparison (#615): verdict plus band
    statistics — パラメトリック予測は実測とは決して同じ種類の証跡に
    ならない。"""
    parts = [porous_fit_verdict_label(comparison.verdict)]
    if comparison.fit_band_hz:
        parts.append(f'フィット帯域 {len(comparison.fit_band_hz)}')
    if comparison.holdout_band_hz:
        parts.append(f'ホールドアウト {len(comparison.holdout_band_hz)}')
    residuals = [
        r.residual for r in comparison.residuals if r.residual is not None
    ]
    if residuals:
        parts.append(f'最大α残差 {max(residuals):.3f}')
    return '多孔材フィット評価: ' + ' — '.join(parts)
# REV57-PROJ: #619 空間均一性, #622 幾何/マスキング, #624 ハッシュボックス,
# #627 光放射安全
#

_SPATIAL_COVERAGE_LABELS = {
    'full_spatial_coverage': '全域カバー',
    'partial_spatial_coverage': '一部カバー',
    'center_only': '中心点のみ',
    'empty': '観測なし',
}

_SPATIAL_QUANTITY_STATE_LABELS = {
    'within_profile': 'プロファイル内',
    'outside_profile': 'プロファイル外',
    'criterion_unbound': '判定基準未設定',
    'insufficient_coverage': 'カバレッジ不足',
    'insufficient_evidence': '証拠不足',
    'not_evaluable': '評価不可',
}

_GEOMETRY_VERDICT_LABELS = {
    'verified': '検証済',
    'verified_with_limitations': '限定付き検証',
    'verified_with_digital_correction': 'デジタル補正あり検証',
    'failed': '不合格',
    'insufficient_evidence': '証拠不足',
}

_PHYSICAL_ALIGNMENT_LABELS = {
    'physically_aligned': '物理アライメント済',
    'physically_misaligned': '物理未アライメント',
    'unknown': '不明',
}

_DIGITAL_CORRECTION_LABELS = {
    'none': 'なし',
    'active': '有効',
    'unknown': '不明',
}

_ENCLOSURE_VERDICT_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '限定付き適格',
    'failed': '不合格',
    'insufficient_evidence': '証拠不足',
}

_THERMAL_STATE_LABELS = {
    'within_documented_environment': '記載環境内',
    'thermally_measured_stable': '熱安定確認済',
    'thermally_limited': '熱的制限あり',
    'ventilation_requirement_unknown': '換気要件不明',
    'manufacturer_constraint_violated': 'メーカー制約違反',
    'over_temperature_event': '過温イベント',
    'insufficient_evidence': '証拠不足',
    'not_evaluated': '未評価',
}

_ENCLOSURE_ACOUSTIC_LABELS = {
    'net_reduction_documented': '正味減音確認',
    'net_reduction_limited': '限定的減音',
    'fan_escalation_negates': 'ファン増速で相殺',
    'not_comparable': '比較不能',
    'insufficient_evidence': '証拠不足',
    'not_evaluated': '未評価',
}

_ENCLOSURE_OPTICAL_LABELS = {
    'no_optical_port': '光学ポートなし',
    'port_within_limits': 'ポート許容内',
    'port_degrades_image': 'ポートが画像劣化',
    'port_uncharacterized': 'ポート未特性化',
    'not_evaluated': '未評価',
}

_SERVICEABILITY_LABELS = {
    'service_access_documented': 'サービスアクセス記録済',
    'service_access_limited': 'サービスアクセス限定',
    'service_access_blocked': 'サービスアクセス遮断',
    'unknown': '不明',
}

_SAFETY_VERDICT_LABELS = {
    'installation_within_documented_constraints': '記載制約内で設置',
    'qualified_with_limitations': '限定付き適格',
    'safety_zone_conflict': '安全ゾーン抵触',
    'lens_accessory_applicability_unknown': 'レンズアクセサリ適用不明',
    'service_state_not_user_safe': 'サービス状態(ユーザ安全外)',
    'local_review_required': '現地審査要',
    'insufficient_evidence': '証拠不足',
    'stale_after_change': '変更後の陳腐化',
}


def spatial_coverage_label(state: str) -> str:
    return _SPATIAL_COVERAGE_LABELS.get(state, state)


def spatial_quantity_state_label(state: str) -> str:
    return _SPATIAL_QUANTITY_STATE_LABELS.get(state, state)


def geometry_verdict_label(verdict: str) -> str:
    return _GEOMETRY_VERDICT_LABELS.get(verdict, verdict)


def physical_alignment_label(state: str) -> str:
    return _PHYSICAL_ALIGNMENT_LABELS.get(state, state)


def digital_correction_state_label(state: str) -> str:
    return _DIGITAL_CORRECTION_LABELS.get(state, state)


def enclosure_verdict_label(verdict: str) -> str:
    return _ENCLOSURE_VERDICT_LABELS.get(verdict, verdict)


def thermal_state_label(state: str) -> str:
    return _THERMAL_STATE_LABELS.get(state, state)


def enclosure_acoustic_state_label(state: str) -> str:
    return _ENCLOSURE_ACOUSTIC_LABELS.get(state, state)


def enclosure_optical_state_label(state: str) -> str:
    return _ENCLOSURE_OPTICAL_LABELS.get(state, state)


def serviceability_state_label(state: str) -> str:
    return _SERVICEABILITY_LABELS.get(state, state)


def safety_verdict_label(verdict: str) -> str:
    return _SAFETY_VERDICT_LABELS.get(verdict, verdict)


def spatial_uniformity_line(evaluation) -> str:
    """One JA line for a spatial uniformity evaluation (#619): coverage
    plus each quantity's state — 中心点だけで均一性とは読まない。"""
    parts = [spatial_coverage_label(evaluation.coverage_state)]
    for quantity_verdict in evaluation.quantity_verdicts:
        parts.append(
            f'{quantity_verdict.quantity}: '
            f'{spatial_quantity_state_label(quantity_verdict.state)}'
        )
    if evaluation.unbound_observations:
        parts.append(f'未束縛観測 {evaluation.unbound_observations} 点')
    return '空間均一性評価: ' + ' — '.join(parts)


def geometry_evaluation_line(evaluation) -> str:
    """One JA line for a presentation-geometry verdict (#622): verdict,
    physical alignment and digital correction stay separate — デジタル
    warp で傾いたプロジェクタを「検証済」とは読まない。"""
    parts = [
        geometry_verdict_label(evaluation.verdict),
        f'物理: {physical_alignment_label(evaluation.physical_alignment)}',
        'デジタル補正: '
        + digital_correction_state_label(evaluation.digital_correction_state),
    ]
    failed = [
        quantity
        for quantity, state in evaluation.quantity_states
        if state == 'FAIL'
    ]
    if failed:
        parts.append('不合格: ' + ' / '.join(failed))
    if evaluation.correction_costs:
        parts.append('補正コスト: ' + ' / '.join(evaluation.correction_costs))
    return '幾何/マスキング評価: ' + ' — '.join(parts)


def enclosure_qualification_line(qualification) -> str:
    """One JA line for an enclosure qualification (#624): thermal,
    acoustic, optical and serviceability axes — 減音だけで「適格」とは
    読まない。"""
    parts = [
        enclosure_verdict_label(qualification.verdict),
        f'熱: {thermal_state_label(qualification.thermal_state)}',
        f'音: {enclosure_acoustic_state_label(qualification.acoustic_state)}',
        f'光学: {enclosure_optical_state_label(qualification.optical_state)}',
        'サービス: '
        + serviceability_state_label(qualification.serviceability_state),
    ]
    return 'ハッシュボックス適格性: ' + ' — '.join(parts)


def optical_safety_line(evaluation) -> str:
    """One JA line for an optical-radiation safety verdict (#627): the
    fail-closed state — IP 電源断だけでは安全とは読まない。"""
    parts = [safety_verdict_label(evaluation.verdict)]
    conflicts = [
        result.get('position_id', '?')
        for result in evaluation.zone_results
        if isinstance(result, dict) and result.get('result') == 'conflict'
    ]
    if conflicts:
        parts.append('抵触位置: ' + ' / '.join(dict.fromkeys(conflicts)))
    if evaluation.reasons:
        parts.append(evaluation.reasons[0])
    return '光放射安全: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# REV57-DISP (#625/#626/#633): direct-view display, observer metamerism,
# viewing environment authorities.

from .cad_direct_view_display import (  # noqa: E402
    CLAIM_KIND_LABELS as _DV_CLAIM_KIND_LABELS,
    CLAIM_VERDICT_LABELS as _DV_CLAIM_VERDICT_LABELS,
    DirectViewQualification,
    LOCAL_DIMMING_LABELS as _DV_LOCAL_DIMMING_LABELS,
    PANEL_TECHNOLOGY_LABELS as _DV_PANEL_LABELS,
    QUALIFICATION_STATE_LABELS as _DV_STATE_LABELS,
    QUANTITY_KIND_LABELS as _DV_QUANTITY_LABELS,
    REASON_LABELS as _DV_REASON_LABELS,
    TEMPORAL_DIMMING_LABELS as _DV_TEMPORAL_LABELS,
)
from .cad_observer_metamerism import (  # noqa: E402
    MISMATCH_CLASS_LABELS as _OM_MISMATCH_LABELS,
    ObserverMetamerismQualification,
    PROFILE_KIND_LABELS as _OM_PROFILE_LABELS,
    REASON_LABELS as _OM_REASON_LABELS,
    SPD_EVIDENCE_LABELS as _OM_SPD_LABELS,
    VERDICT_LABELS as _OM_VERDICT_LABELS,
)
from .cad_viewing_environment import (  # noqa: E402
    CHANGE_AXIS_LABELS as _VE_AXIS_LABELS,
    EnvironmentComparability,
    PROFILE_KIND_LABELS as _VE_PROFILE_LABELS,
    QUALIFICATION_STATE_LABELS as _VE_STATE_LABELS,
    REASON_LABELS as _VE_REASON_LABELS,
    REQUIREMENT_LABELS as _VE_REQUIREMENT_LABELS,
    REQUIREMENT_STATE_LABELS as _VE_REQ_STATE_LABELS,
    ViewingEnvironmentQualification,
)


def dv_panel_technology_label(technology: str) -> str:
    return _DV_PANEL_LABELS.get(technology, technology)


def dv_quantity_label(quantity: str) -> str:
    return _DV_QUANTITY_LABELS.get(quantity, quantity)


def dv_claim_kind_label(kind: str) -> str:
    return _DV_CLAIM_KIND_LABELS.get(kind, kind)


def dv_qualification_state_label(state: str) -> str:
    return _DV_STATE_LABELS.get(state, state)


def dv_reason_label(reason: str) -> str:
    return _DV_REASON_LABELS.get(reason, reason)


def dv_temporal_dimming_label(state: str) -> str:
    return _DV_TEMPORAL_LABELS.get(state, state)


def direct_view_qualification_line(
    qualification: DirectViewQualification,
) -> str:
    """One JA line for a direct-view qualification (#625): the rolled-up
    state, then per-claim failures — `HDR calibrated` バッジが未検証の
    次元を隠すことはない。"""
    parts = [dv_qualification_state_label(qualification.state)]
    weak = [
        dv_claim_kind_label(v.kind)
        for v in qualification.claim_verdicts
        if v.verdict in (
            'insufficient_evidence', 'unsupported', 'conflicting_evidence'
        )
    ]
    if weak:
        parts.append('未検証: ' + ' / '.join(weak))
    limited = [
        dv_claim_kind_label(v.kind)
        for v in qualification.claim_verdicts
        if v.verdict == 'supported_with_limitations'
    ]
    if limited:
        parts.append('制限付き: ' + ' / '.join(limited))
    return '直視ディスプレイ適格: ' + ' — '.join(parts)


def om_profile_kind_label(kind: str) -> str:
    return _OM_PROFILE_LABELS.get(kind, kind)


def om_verdict_label(verdict: str) -> str:
    return _OM_VERDICT_LABELS.get(verdict, verdict)


def om_reason_label(reason: str) -> str:
    return _OM_REASON_LABELS.get(reason, reason)


def om_spd_evidence_label(evidence_class: str) -> str:
    return _OM_SPD_LABELS.get(evidence_class, evidence_class)


def om_mismatch_class_label(mismatch_class: str) -> str:
    return _OM_MISMATCH_LABELS.get(mismatch_class, mismatch_class)


def observer_metamerism_line(
    qualification: ObserverMetamerismQualification,
) -> str:
    """One JA line for an observer-metamerism qualification (#626):
    verdict plus the class separation — 計器一致は全観察者の一致を
    意味しない。"""
    parts = [om_verdict_label(qualification.verdict)]
    notes = [
        om_reason_label(r)
        for r in qualification.reasons
        if r in (
            'INSTRUMENT_MISMATCH_SUSPECTED',
            'METER_CORRECTION_NOT_OBSERVER_PROOF',
            'SINGLE_OBSERVER_NOT_UNIVERSAL',
            'TRISTIMULUS_ONLY_EVIDENCE',
            'PROJECTION_OUT_OF_PROFILE_SCOPE',
        )
    ]
    if notes:
        parts.append(' / '.join(notes))
    return '観察者メタメリズム評価: ' + ' — '.join(parts)


def ve_profile_kind_label(kind: str) -> str:
    return _VE_PROFILE_LABELS.get(kind, kind)


def ve_state_label(state: str) -> str:
    return _VE_STATE_LABELS.get(state, state)


def ve_reason_label(reason: str) -> str:
    return _VE_REASON_LABELS.get(reason, reason)


def ve_change_axis_label(axis: str) -> str:
    return _VE_AXIS_LABELS.get(axis, axis)


def viewing_environment_line(
    qualification: ViewingEnvironmentQualification,
) -> str:
    """One JA line for a viewing-environment qualification (#633):
    profile name + state + unmet requirements — `display calibrated`
    は `reference viewing condition` を意味しない。"""
    parts = [
        ve_profile_kind_label(qualification.profile_kind),
        ve_state_label(qualification.state),
    ]
    unmet = [
        _VE_REQUIREMENT_LABELS.get(v.requirement, v.requirement)
        for v in qualification.requirement_verdicts
        if v.state == 'unmet'
    ]
    unknown = [
        _VE_REQUIREMENT_LABELS.get(v.requirement, v.requirement)
        for v in qualification.requirement_verdicts
        if v.state == 'unknown'
    ]
    if unmet:
        parts.append('未充足: ' + ' / '.join(unmet))
    if unknown:
        parts.append('未測定: ' + ' / '.join(unknown))
    if qualification.stale_after_change:
        parts.append(
            '陳腐化: '
            + ' / '.join(
                ve_change_axis_label(a)
                for a in qualification.stale_after_change
            )
        )
    return '視聴環境評価: ' + ' — '.join(parts)


def environment_comparability_line(
    comparability: EnvironmentComparability,
) -> str:
    """One JA line for a before/after comparability result (#633 §14):
    changed axes reported alongside — 照明変更と校正変更を分離する。"""
    from .cad_viewing_environment import COMPARABILITY_LABELS

    parts = [
        COMPARABILITY_LABELS.get(
            comparability.verdict, comparability.verdict
        )
    ]
    if comparability.changed_axes:
        parts.append(
            '変更軸: '
            + ' / '.join(
                ve_change_axis_label(a)
                for a in comparability.changed_axes
            )
        )
    return '視聴環境比較: ' + ' — '.join(parts)


# REV57-AUD (#621/#634/#628/#632): channel-identity/polarity,
# coverage/aim, instance variation, media-playback capability
# authorities.

_CHANNEL_IDENTITY_VERDICT_LABELS = {
    'verified': '検証済',
    'verified_compensated': '補償付き検証済',
    'verified_with_limitations': '限定付き検証済',
    'identity_mismatch': '同一性不一致',
    'polarity_fault': '極性異常',
    'not_driven_by_renderer': 'レンダラー未駆動',
    'stale': '陳腐化',
    'insufficient_evidence': '証拠不足',
}

_CHANNEL_RECONCILIATION_LABELS = {
    'all_match': '全一致',
    'device_map_mismatch': 'デバイスマップ不一致',
    'physical_path_mismatch': '物理経路不一致',
    'acoustic_endpoint_mismatch': '音響端点不一致',
    'multiple_unexpected_endpoints': '複数の予期せぬ端点',
    'insufficient_evidence': '証拠不足',
}

_COVERAGE_STATE_LABELS = {
    'predicted_only': '予測のみ',
    'field_measured': '実測済',
    'predicted_and_measured_agree_within_envelope':
        '予測と実測が包絡内一致',
    'qualified_with_limitations': '限定付き適格',
    'source_directivity_limited': '音源指向性限定',
    'occlusion_limited': '遮蔽限定',
    'spatial_sampling_insufficient': '空間サンプリング不足',
    'profile_source_ambiguous': 'プロファイル出所不明',
    'indeterminate': '判定不能',
}

_MATCHED_SET_VERDICT_LABELS = {
    'matched_within_declared_tolerance': '宣言公差内で整合',
    'matched_within_project_tolerance': 'プロジェクト公差内で整合',
    'outlier_detected': '外れ値検出',
    'suspected_defect': '不良疑い',
    'environment_dependent': '環境依存',
    'measurement_inconclusive': '測定不確定',
    'no_population_tolerance_available': '集団公差なし',
    'insufficient_evidence': '証拠不足',
}

_PLAYBACK_VERDICT_LABELS = {
    'qualified_exact_profile': '正確なプロファイルで適格',
    'qualified_with_fallback': 'フォールバック付き適格',
    'qualified_with_limitations': '限定付き適格',
    'player_unsupported': 'プレイヤー非対応',
    'output_profile_mismatch': '出力プロファイル不一致',
    'transport_dependency_failed': '伝送依存性失敗',
    'intermittent': '断続的',
    'insufficient_evidence': '証拠不足',
}


def channel_identity_verdict_label(verdict: str) -> str:
    return _CHANNEL_IDENTITY_VERDICT_LABELS.get(verdict, verdict)


def channel_reconciliation_label(state: str) -> str:
    return _CHANNEL_RECONCILIATION_LABELS.get(state, state)


def coverage_state_label(state: str) -> str:
    return _COVERAGE_STATE_LABELS.get(state, state)


def matched_set_verdict_label(verdict: str) -> str:
    return _MATCHED_SET_VERDICT_LABELS.get(verdict, verdict)


def playback_verdict_label(verdict: str) -> str:
    return _PLAYBACK_VERDICT_LABELS.get(verdict, verdict)


def channel_identity_evaluation_line(evaluation) -> str:
    """One JA line for a channel-identity evaluation (#621): verdict,
    reconciliation and polarity summary — マッピング宣言だけで
    「再生済」とは読まない。"""
    parts = [
        f'{evaluation.logical_channel}: '
        + channel_identity_verdict_label(evaluation.verdict),
        channel_reconciliation_label(evaluation.reconciliation),
    ]
    if evaluation.unexpected_speaker_entity_ids:
        parts.append(
            '予期せぬ端点: '
            + ' / '.join(evaluation.unexpected_speaker_entity_ids)
        )
    if evaluation.missing_speaker_entity_ids:
        parts.append(
            '未到達: ' + ' / '.join(evaluation.missing_speaker_entity_ids)
        )
    if evaluation.reasons:
        parts.append(evaluation.reasons[0])
    return 'チャネル同一性評価: ' + ' — '.join(parts)


def coverage_qualification_line(qualification) -> str:
    """One JA line for a coverage qualification (#634): fail-closed
    coverage state plus limiting positions — 単一 MLP トレースだけで
    全域カバレッジとは読まない。"""
    parts = [coverage_state_label(qualification.coverage_state)]
    limiting = [
        f'{position_id}: {state}'
        for position_id, state in qualification.position_states
        if state != qualification.coverage_state
    ]
    if limiting:
        parts.append('限定位置: ' + ' / '.join(limiting[:4]))
    if qualification.limiting_position_ids:
        parts.append(
            '限定: ' + ' / '.join(qualification.limiting_position_ids[:4])
        )
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return 'カバレッジ修飾: ' + ' — '.join(parts)


def matched_set_qualification_line(qualification) -> str:
    """One JA line for a matched-set qualification (#628): verdict plus
    the limiting metric — 型番一致だけで個体差なしとは読まない。"""
    parts = [matched_set_verdict_label(qualification.verdict)]
    outside = [
        f'{verdict.metric}: '
        + ' / '.join(verdict.limiting_instance_ids or ('?',))
        for verdict in qualification.metric_verdicts
        if verdict.state == 'outside'
    ]
    if outside:
        parts.append('公差外: ' + ' ; '.join(outside[:3]))
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return 'マッチドセット修飾: ' + ' — '.join(parts)


def playback_qualification_line(qualification) -> str:
    """One JA line for a playback qualification (#632): verdict plus the
    observed output — メタデータ宣言だけで「Atmos 再生済」とは読まない。"""
    parts = [playback_verdict_label(qualification.verdict)]
    if qualification.fallback_state:
        parts.append(f'観測出力: {qualification.fallback_state}')
    if qualification.failure_attribution not in (
        None,
        'not_applicable',
    ):
        parts.append(f'失敗属性: {qualification.failure_attribution}')
    if qualification.stale:
        parts.append('スタック更新後の陳腐化')
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '再生能力修飾: ' + ' — '.join(parts)


# REV57-INST (#616/#618/#631/#612): HVAC co-design, playback reference
# calibration, as-built treatment, tactile/seat-vibration authorities.

_HVAC_VERDICT_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'ineligible_airflow': '風量不足で不適格',
    'failed': '不合格',
    'insufficient_evidence': '証拠不足',
}
_HVAC_AIRFLOW_LABELS = {
    'eligible': '風量充足',
    'under_ventilated': '換気不足',
    'airflow_requirement_unbound': '要求風量未宣言',
    'insufficient_evidence': '風量証拠不足',
    'not_evaluated': '未評価',
}
_HVAC_ACOUSTIC_LABELS = {
    'contributions_documented': '寄与量実測済み',
    'contributions_limited': '寄与量一部実測',
    'lab_evidence_not_installed_truth': 'ラボ証拠のみ（設置実態不明）',
    'tonal_content_unresolved': 'トーン成分未解決',
    'insufficient_evidence': '音響証拠不足',
    'not_evaluated': '未評価',
}
_HVAC_FLANKING_LABELS = {
    'no_flanking_declared': 'フランキングなし',
    'flanking_path_open': 'フランキング経路あり',
    'flanking_path_mitigated': 'フランキング対策済み',
    'flanking_unverified': 'フランキング未検証',
    'not_evaluated': '未評価',
}
_HVAC_TONAL_LABELS = {
    'no_tonal_flags': 'トーン成分なし',
    'tonal_flags_present': 'トーン成分あり',
    'not_evaluated': 'トーン未評価',
}


def hvac_qualification_line(qualification) -> str:
    """One JA line for an HVAC co-design qualification (#616): quiet-
    but-underventilated stays visible — 遮音だけでは適格にならない。"""
    parts = [
        _HVAC_VERDICT_LABELS.get(qualification.verdict, qualification.verdict),
        '風量: ' + _HVAC_AIRFLOW_LABELS.get(
            qualification.airflow_eligibility, qualification.airflow_eligibility
        ),
        '音響: ' + _HVAC_ACOUSTIC_LABELS.get(
            qualification.acoustic_state, qualification.acoustic_state
        ),
    ]
    if qualification.flanking_state not in (
        'no_flanking_declared', 'not_evaluated'
    ):
        parts.append(
            'フランキング: '
            + _HVAC_FLANKING_LABELS.get(
                qualification.flanking_state, qualification.flanking_state
            )
        )
    if qualification.tonal_state == 'tonal_flags_present':
        parts.append(_HVAC_TONAL_LABELS['tonal_flags_present'])
    return 'HVAC共同設計評価: ' + ' — '.join(parts)


_REF_CAL_VERDICT_LABELS = {
    'reference_calibrated': 'リファレンス校正済み',
    'calibrated_with_limitations': '制限付き校正済み',
    'device_profile_only': 'デバイスプロファイルのみ',
    'insufficient_evidence': '証拠不足',
    'failed': '不合格',
}
_REF_CAL_STIMULUS_LABELS = {
    'exact_stimulus_bound': '標準刺激に紐付け済み',
    'unbound': '刺激未紐付け',
    'device_internal_documented': 'デバイス内部信号（文書あり）',
    'device_internal_undocumented': 'デバイス内部信号（文書なし）',
}
_REF_CAL_MEASUREMENT_LABELS = {
    'semantics_complete': '計測セマンティクス完備',
    'semantics_incomplete': '計測セマンティクス一部欠落',
    'no_measurements': '計測なし',
}
_REF_CAL_LFE_LABELS = {
    'in_band_gain_verified': 'LFE帯域内ゲイン検証済み',
    'in_band_gain_declared': 'LFE帯域内ゲイン宣言のみ',
    'meter_delta_only_not_proof': 'メータ差分のみ（帯域内証明なし）',
    'redirected_bass_isolated': 'リダイレクト低音は分離済み',
    'not_applicable': '対象外',
}
_REF_CAL_ALIGNMENT_LABELS = {
    'aligned': 'ターゲット内',
    'misaligned': 'ターゲット外',
    'unverifiable': '検証不能',
    'not_evaluated': '未評価',
}


def reference_calibration_line(qualification) -> str:
    """One JA line for a playback reference-calibration qualification
    (#618): LFEの+10dBは帯域内ゲインであってメータ補正ではない —
    calibration は最大能力でもリスニングレベルでもない。"""
    parts = [
        _REF_CAL_VERDICT_LABELS.get(qualification.verdict, qualification.verdict),
        '刺激: ' + _REF_CAL_STIMULUS_LABELS.get(
            qualification.stimulus_state, qualification.stimulus_state
        ),
        '計測: ' + _REF_CAL_MEASUREMENT_LABELS.get(
            qualification.measurement_state, qualification.measurement_state
        ),
    ]
    if qualification.lfe_state != 'not_applicable':
        parts.append(
            'LFE: '
            + _REF_CAL_LFE_LABELS.get(
                qualification.lfe_state, qualification.lfe_state
            )
        )
    if qualification.alignment_state != 'not_evaluated':
        parts.append(
            'アライメント: '
            + _REF_CAL_ALIGNMENT_LABELS.get(
                qualification.alignment_state, qualification.alignment_state
            )
        )
    return '再生参照校正評価: ' + ' — '.join(parts)


_ASBUILT_VERDICT_LABELS = {
    'qualified_as_built': 'as-built適格',
    'qualified_with_limitations': '制限付き適格',
    'prediction_stale': '予測陳腐化',
    'incompatible': '不一致',
    'insufficient_evidence': '証拠不足',
}
_ASBUILT_VALIDITY_LABELS = {
    'remains_eligible': '予測有効',
    'limited': '予測制限付き',
    'stale': '予測陳腐',
    'unknown': '予測有効性不明',
}
_ASBUILT_BEFORE_AFTER_LABELS = {
    'consistent_with_expected': '期待効果と一致',
    'inconsistent_with_expected': '期待効果と不一致',
    'inconclusive': '判定不能',
    'not_performed': '未実施',
}
_ASBUILT_PARAMETER_LABELS = {
    'thickness': '厚さ',
    'air_gap': '空気層',
    'area': '面積',
    'facing': '表面材',
    'orientation': '向き',
    'placement': '位置',
    'backing': '背後構造',
    'product_identity': '製品同一性',
}


def treatment_asbuilt_line(qualification) -> str:
    """One JA line for an as-built treatment qualification (#631):
    per-parameter verdicts surfaced — パネル存在 ≠ 設計通りの境界。"""
    import json as _json

    parts = [
        _ASBUILT_VERDICT_LABELS.get(qualification.verdict, qualification.verdict),
        _ASBUILT_VALIDITY_LABELS.get(
            qualification.prediction_validity,
            qualification.prediction_validity,
        ),
    ]
    try:
        verdicts = _json.loads(qualification.parameter_verdicts_json or '{}')
    except ValueError:
        verdicts = {}
    dev = [
        _ASBUILT_PARAMETER_LABELS.get(p, p)
        for p, v in verdicts.items() if v == 'deviation'
    ]
    unk = [
        _ASBUILT_PARAMETER_LABELS.get(p, p)
        for p, v in verdicts.items() if v == 'unknown'
    ]
    if dev:
        parts.append('乖離: ' + ' / '.join(dev))
    if unk:
        parts.append('未確認: ' + ' / '.join(unk))
    if qualification.before_after_result != 'not_performed':
        parts.append(
            '前後測定: '
            + _ASBUILT_BEFORE_AFTER_LABELS.get(
                qualification.before_after_result,
                qualification.before_after_result,
            )
        )
    return 'as-builtトリートメント評価: ' + ' — '.join(parts)


_TAC_VERDICT_LABELS = {
    'qualified': '適格',
    'qualified_with_limitations': '制限付き適格',
    'research_only': '研究専用',
    'insufficient_evidence': '証拠不足',
    'failed': '不合格',
}
_TAC_TRANSFER_LABELS = {
    'measured': '伝達実測済み',
    'partially_measured': '一部実測',
    'inferred_only': '推定のみ',
    'unmeasured': '未計測',
}
_TAC_OCCUPANCY_LABELS = {
    'empty_seat': '無人シート',
    'occupied_generic': '着座（一般）',
    'occupied_measured': '着座（実測）',
    'unknown': '着座状態不明',
}
_TAC_TIMING_LABELS = {
    'physically_measured': '物理計測済み',
    'dsp_setting_only': 'DSP設定値のみ',
    'unmeasured': '未計測',
}
_TAC_SIDE_EFFECT_LABELS = {
    'evaluated_clean': '副作用なし',
    'evaluated_flagged': '副作用あり（#589へ）',
    'not_evaluated': '副作用未評価',
}
_TAC_COUPLING_LABELS = {
    'evaluated_acceptable': '建物結合許容',
    'evaluated_excessive': '建物結合過剰',
    'not_evaluated': '建物結合未評価',
}


def tactile_vibration_line(qualification) -> str:
    """One JA line for a tactile/seat-vibration qualification (#612):
    触感 ≠ SPL — 駆動→シート伝達はワット数から推定しない。"""
    parts = [
        _TAC_VERDICT_LABELS.get(qualification.verdict, qualification.verdict),
        '伝達: ' + _TAC_TRANSFER_LABELS.get(
            qualification.transfer_state, qualification.transfer_state
        ),
        '着座: ' + _TAC_OCCUPANCY_LABELS.get(
            qualification.occupancy_state, qualification.occupancy_state
        ),
    ]
    if qualification.timing_state != 'unmeasured':
        parts.append(
            '同期: '
            + _TAC_TIMING_LABELS.get(
                qualification.timing_state, qualification.timing_state
            )
        )
    if qualification.acoustic_side_effect_state != 'not_evaluated':
        parts.append(
            _TAC_SIDE_EFFECT_LABELS.get(
                qualification.acoustic_side_effect_state,
                qualification.acoustic_side_effect_state,
            )
        )
    if qualification.building_coupling_state != 'not_evaluated':
        parts.append(
            _TAC_COUPLING_LABELS.get(
                qualification.building_coupling_state,
                qualification.building_coupling_state,
            )
        )
    return '触覚振動評価: ' + ' — '.join(parts)


# REV57-MOUNT (#620): AV mounting / structural-support evidence.

_MOUNT_SUPPORT_STATE_LABELS = {
    'design_support_evidence_complete': '設計支持証拠完備',
    'approved_with_limitations': '限定付き承認',
    'installation_inspection_required': '設置検査が必要',
    'structural_approval_required': '構造承認が必要',
    'manufacturer_mounting_incompatible': 'メーカー取付要件非適合',
    'support_capacity_insufficient': '支持容量不足（宣言値）',
    'support_unknown': '支持構造不明',
    'as_built_mismatch': '竣工状態不一致',
    'stale_after_change': '変更後に陳腐化',
}

_SUSPENSION_LAYER_LABELS = {
    'capable': '対応可能',
    'incompatible': '非適合',
    'unknown': '不明',
    'not_applicable': '対象外',
}


def mounting_support_state_label(state: str) -> str:
    return _MOUNT_SUPPORT_STATE_LABELS.get(state, state)


def suspension_layer_label(state: str) -> str:
    return _SUSPENSION_LAYER_LABELS.get(state, state)


def mounting_qualification_line(qualification) -> str:
    """One JA line for a mounting-support qualification (#620): verdict
    plus the suspension layers and any declared demand/rating —
    CAD 上の配置だけで「取付可能」とは読まない。"""
    parts = [
        mounting_support_state_label(qualification.support_state)
    ]
    layers = qualification.suspension_layers
    if layers is not None and (
        layers.enclosure_capability != 'not_applicable'
        or layers.building_support_point != 'not_applicable'
        or layers.field_rigging_assembly != 'not_applicable'
    ):
        parts.append(
            '吊下げ層: 筐体='
            + suspension_layer_label(layers.enclosure_capability)
            + ' / 支持点='
            + suspension_layer_label(layers.building_support_point)
            + ' / 現場組立='
            + suspension_layer_label(layers.field_rigging_assembly)
        )
    if qualification.demand_vs_rating_kg is not None:
        parts.append(
            '宣言定格−宣言荷重: {0:+.1f} kg'.format(
                qualification.demand_vs_rating_kg,
            )
        )
    if qualification.stale_flags:
        parts.append(
            '陳腐化: ' + ' / '.join(qualification.stale_flags[:4])
        )
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '取付支持修飾: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# REV58-MEASCHAIN (#695/#697/#668): measurement-chain linearity/overload,
# swept-sine deconvolution, room-acoustic excitation source.

_CHAIN_QUALIFICATION_STATE_LABELS = {
    'chain_qualified_within_declared_range': '宣言範囲内で適格',
    'chain_qualified_with_limitations': '制限付き適格',
    'unqualified_insufficient_evidence': '証拠不足で不適格',
    'nonlinear_measurement_ineligible': '非線形測定不適',
    'overload_suspected': '過負荷の疑い',
    'overload_observed': '過負荷を観測',
    'chain_state_unknown': 'チェーン状態不明',
}

_DISTORTION_ATTRIBUTION_LABELS = {
    'dut_attributable': 'DUT 起因と帰属可能',
    'measurement_chain_contaminated': '測定チェーン混入',
    'attribution_indeterminate': '帰属不能',
}

_MEASCHAIN_CAPABILITY_LABELS = {
    'absolute_spl_valid': '絶対 SPL',
    'linear_magnitude_valid': '線形振幅',
    'phase_valid': '位相',
    'high_level_spl_valid': '高 SPL',
    'dut_thd_valid': 'DUT THD',
    'dut_compression_valid': 'DUT 圧縮',
    'peak_transient_valid': 'ピーク過渡',
    'chain_overload_not_excluded': 'チェーン過負荷排除不能',
}


def measchain_qualification_state_label(state: str) -> str:
    return _CHAIN_QUALIFICATION_STATE_LABELS.get(state, state)


def measchain_distortion_attribution_label(value: str) -> str:
    return _DISTORTION_ATTRIBUTION_LABELS.get(value, value)


def measchain_capability_label(capability: str) -> str:
    return _MEASCHAIN_CAPABILITY_LABELS.get(capability, capability)


def measchain_qualification_line(qualification) -> str:
    """One JA line for a measurement-chain qualification (#695): the
    verdict plus the distortion attribution — 過負荷インジケータ未発火は
    線形性の証拠とは読まない。"""
    parts = [
        measchain_qualification_state_label(qualification.state),
        '歪帰属: '
        + measchain_distortion_attribution_label(
            qualification.distortion_attribution
        ),
    ]
    degraded = [
        measchain_capability_label(capability)
        for capability, state in qualification.capabilities
        if state in ('invalid', 'limited')
    ]
    if degraded:
        parts.append('限定/不可: ' + ' / '.join(degraded))
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '測定チェーン適格: ' + ' — '.join(parts)


_IR_CONTAMINATION_LABELS = {
    'linear_ir_clean_within_declared_window': '宣言窓内で線形 IR クリーン',
    'nonlinear_components_separated': '非線形成分分離済',
    'partial_overlap': '部分重畳',
    'causal_nonlinear_contamination_risk': '因果部混入リスク',
    'inseparable': '分離不能',
    'insufficient_evidence': '証拠不足',
}

_IR_METRIC_LABELS = {
    'fr_valid': '周波数応答',
    'direct_arrival_valid': '直接波音',
    'early_reflection_valid': '初期反射',
    'decay_metric_valid': '減衰指標',
    'clarity_valid': '明瞭度',
    'absolute_phase_valid': '絶対位相',
}

_IR_GATE_LABELS = {
    'synchronized': '同期済',
    'unsynchronized_declared': '非同期宣言',
    'unassessed': '未評価',
    'unknown': '不明',
    'qualified': '適格',
    'overload_suspected': '過負荷の疑い',
    'overload_observed': '過負荷観測',
}


def linear_ir_contamination_label(state: str) -> str:
    return _IR_CONTAMINATION_LABELS.get(state, state)


def linear_ir_capability_line(capability) -> str:
    """One JA line for a linear-IR capability verdict (#697):
    contamination state plus clock/chain gates — 「高調波は常に t<0
    に安全」とは読まない。"""
    parts = [
        linear_ir_contamination_label(capability.contamination_state),
        'クロック: ' + _IR_GATE_LABELS.get(
            capability.clock_gate, capability.clock_gate
        ),
        'チェーン: ' + _IR_GATE_LABELS.get(
            capability.chain_gate, capability.chain_gate
        ),
    ]
    degraded = [
        _IR_METRIC_LABELS.get(metric, metric)
        for metric, state in capability.capabilities
        if state in ('invalid', 'limited')
    ]
    if degraded:
        parts.append('限定/不可: ' + ' / '.join(degraded))
    return '線形 IR 能力: ' + ' — '.join(parts)


_SRC_ELIGIBILITY_LABELS = {
    'eligible': '適格',
    'eligible_with_source_limitation': 'ソース制限付き適格',
    'directivity_out_of_profile': '指向性プロファイル外',
    'insufficient_source_level': 'ソースレベル不足',
    'wrong_source_class': 'ソースクラス不一致',
    'source_state_unknown': 'ソース状態不明',
}

_SRC_PURPOSE_LABELS = {
    'standardized_room_characterization': '標準室特性測定',
    'installed_system_diagnostics': '設置系診断',
    'spatial_impression_measurement': '空間印象測定',
    'strength_g_measurement': '強度 G 測定',
    'simulation_validation_comparison': 'シミュレーション検証',
}

_SRC_SIM_LABELS = {
    'comparable': '比較可能',
    'comparable_within_validated_band': '検証帯域内で比較可能',
    'wrong_source_model': 'ソースモデル不一致',
    'insufficient_evidence': '証拠不足',
}

_SRC_LEVEL_GATE_LABELS = {
    'sufficient': '十分',
    'insufficient': '不足',
    'unknown': '不明',
}


def source_qualification_line(qualification) -> str:
    """One JA line for a measurement-source qualification (#668): the
    per-purpose eligibility plus Strength-G gate — 設置チャンネル IR を
    標準室応答と読み違えない。"""
    parts = [
        _SRC_PURPOSE_LABELS.get(purpose, purpose)
        + ': '
        + _SRC_ELIGIBILITY_LABELS.get(state, state)
        for purpose, state in qualification.eligibilities
    ]
    parts.append(
        '強度G: '
        + {
            'eligible': '適格',
            'ineligible': '不適格',
            'unknown': '不明',
        }.get(qualification.strength_g_gate, qualification.strength_g_gate)
    )
    parts.append(
        'レベル: '
        + _SRC_LEVEL_GATE_LABELS.get(
            qualification.level_gate, qualification.level_gate
        )
    )
    if qualification.sim_comparison is not None:
        parts.append(
            'シミュレーション比較: '
            + _SRC_SIM_LABELS.get(
                qualification.sim_comparison,
                qualification.sim_comparison,
            )
        )
    return '測定ソース適格: ' + ' — '.join(parts)


# REV58-DSPDECAY: DSP filter realization (#679), decay-curve
# noise/truncation processing (#676), acoustic-impedance physical
# realizability (#705).

_DSP_REALIZATION_STATE_LABELS = {
    'realized_within_declared_model': '宣言モデル内で実現',
    'realized_with_declared_approximation': '宣言近似込みで実現',
    'nominal_state_match_only': '名目状態一致のみ',
    'realization_model_limited': '実現モデル限定',
    'incompatible': 'デプロイ不能',
    'reoptimization_required': '再最適化要',
    'unqualified': '未適格',
}

_DSP_READBACK_LABELS = {
    'device_readback_match': 'デバイス読み戻し一致',
    'device_readback_mismatch': 'デバイス読み戻し不一致',
    'readback_unavailable': '読み戻し未取得',
    'unassessed': '未評価',
}

_DSP_TRANSFER_LABELS = {
    'transfer_realization_verified': '伝達関数実現検証済',
    'nominal_state_match_only': '名目状態一致のみ',
    'transfer_mismatch_detected': '伝達関数不一致検出',
    'transfer_unverified': '伝達関数未検証',
}


def dsp_realization_line(qualification) -> str:
    """One JA line for a DSP realization qualification (#679): the
    verdict plus readback status and transfer verification kept as
    separate layers — 読み戻し一致は伝達関数の実現証明ではない。"""
    parts = [
        _DSP_REALIZATION_STATE_LABELS.get(
            qualification.state, qualification.state
        ),
        '読み戻し: '
        + _DSP_READBACK_LABELS.get(
            qualification.readback_status, qualification.readback_status
        ),
        '伝達関数: '
        + _DSP_TRANSFER_LABELS.get(
            qualification.transfer_verification,
            qualification.transfer_verification,
        ),
    ]
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return 'DSP 実現適格: ' + ' — '.join(parts)


_DECAY_ELIGIBILITY_LABELS = {
    'eligible': '適格',
    'eligible_with_limitations': '制限付き適格',
    'insufficient_decay_range': '減衰レンジ不足',
    'noise_floor_too_high': '雑音床過高',
    'capture_truncated': 'キャプチャ切断',
    'non_stationary_noise': '非定常ノイズ',
    'multi_slope_model_mismatch': 'マルチスロープ不一致',
    'modal_method_required': 'モーダル法必須',
    'indeterminate': '判定不能',
}

_DECAY_METRIC_LABELS = {
    'edt': 'EDT',
    't10': 'T10',
    't20': 'T20',
    't30': 'T30',
    'custom': 'カスタム',
}


def decay_fit_line(record) -> str:
    """One JA line for a decay fit record (#676): metric, value,
    eligibility and usable range — 仕上がった一つの数値の背後に品質
    警告を隠さない。"""
    metric_label = (
        record.custom_metric_label
        if record.metric == 'custom' and record.custom_metric_label
        else _DECAY_METRIC_LABELS.get(record.metric, record.metric)
    )
    parts = [
        metric_label,
        (
            f'{record.value_s:.3f} s'
            if record.value_s is not None
            else '値なし'
        ),
        _DECAY_ELIGIBILITY_LABELS.get(
            record.eligibility, record.eligibility
        ),
    ]
    if record.dynamic_range_db is not None:
        parts.append(f'使用可能レンジ {record.dynamic_range_db:.0f} dB')
    if record.noise_margin_db is not None:
        parts.append(f'雑音マージン {record.noise_margin_db:.0f} dB')
    if record.reasons:
        parts.append(record.reasons[0])
    return '減衰フィット: ' + ' — '.join(parts)


_BOUNDARY_STATE_LABELS = {
    'passive_causal_validated': '受動・因果検証済',
    'passive_with_limitations': '制限付き受動',
    'passivity_unresolved_with_uncertainty': '不確かさ内受動未決',
    'causality_unresolved_finite_band': '有限帯域で因果未決',
    'stable_numerical_realization': '安定数値実現',
    'nonpassive_input': '非受動入力',
    'unstable_fit': '不安定フィット',
    'time_domain_realization_mismatch': 'TD実現不一致',
    'active_boundary_explicit': '明示アクティブ境界',
    'insufficient_evidence': '証拠不足',
}

_BOUNDARY_PASSIVITY_LABELS = {
    'passive_boundary': '受動境界',
    'active_boundary_explicit': '明示アクティブ',
    'nonpassive_unexpected': '非受動',
    'unknown': '不明',
}

_BOUNDARY_CAUSALITY_LABELS = {
    'causal_by_physical_parametric_model': '物理モデルにより因果',
    'causal_by_stable_rational_realization': '安定有理実現により因果',
    'causality_supported_with_limitations': '制限付き因果支持',
    'causality_unresolved_finite_band': '有限帯域で未決',
    'causality_violation_detected': '因果違反検出',
    'unknown': '不明',
}

_BOUNDARY_STABILITY_LABELS = {
    'stable_realization': '安定実現',
    'marginally_stable_review': '限界安定・要レビュー',
    'unstable_realization': '不安定実現',
    'stability_unknown': '安定性不明',
}


def boundary_realizability_line(assessment) -> str:
    """One JA line for a boundary realizability assessment (#705): the
    verdict plus the passivity/causality/stability components — 極の
    不安定・非受動・非因果を一つのバッジに潰さない。"""
    parts = [
        _BOUNDARY_STATE_LABELS.get(assessment.state, assessment.state),
        '受動: '
        + _BOUNDARY_PASSIVITY_LABELS.get(
            assessment.passivity_class, assessment.passivity_class
        ),
        '因果: '
        + _BOUNDARY_CAUSALITY_LABELS.get(
            assessment.causality_state, assessment.causality_state
        ),
        '安定: '
        + _BOUNDARY_STABILITY_LABELS.get(
            assessment.stability_state, assessment.stability_state
        ),
    ]
    if assessment.solver_band_within_evidence is False:
        parts.append('求解帯域が証拠外')
    if assessment.reasons:
        parts.append(assessment.reasons[0])
    return '境界実現性: ' + ' — '.join(parts)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# REV58-NUMERIC: ソルバー数値忠実度 (#683/#685/#687)

_FIDELITY_STATE_LABELS = {
    'qualified_for_declared_domain': '宣言領域で適格',
    'qualified_with_limitations': '限定付き適格',
    'insufficient_evidence': '証拠不足',
    'not_qualified': '不適格',
}

_ERROR_CLASS_LABELS = {
    'qualified': '適格',
    'limited': '限定',
    'unresolved': '未評価',
    'not_applicable': '対象外',
}

_DETERMINISTIC_STATE_LABELS = {
    'qualified': '適格',
    'limited': '限定',
    'unqualified': '不適格',
    'not_applicable': '対象外',
}

_HANDOFF_STATE_LABELS = {
    'overlap_qualified': 'オーバーラップ適格',
    'qualified_with_limitations': '限定付き適格',
    'gap_in_capability': '能力ギャップ',
    'double_count_risk': '二重計上リスク',
    'transition_unqualified': '引継不適格',
    'insufficient_evidence': '証拠不足',
}


def fidelity_state_label(state: str) -> str:
    return _FIDELITY_STATE_LABELS.get(state, state)


def handoff_state_label(state: str) -> str:
    return _HANDOFF_STATE_LABELS.get(state, state)


def wave_fidelity_line(qualification) -> str:
    """One JA line for a wave-fidelity qualification (#683): overall
    verdict plus the unresolved/limited error classes — 設定宣言のない
    「正確さ」は読まない。"""
    parts = [fidelity_state_label(qualification.fidelity_state)]
    flagged = [
        '{0}={1}'.format(
            entry.error_class,
            _ERROR_CLASS_LABELS.get(entry.state, entry.state),
        )
        for entry in qualification.error_class_states
        if entry.state in ('limited', 'unresolved')
    ]
    if flagged:
        parts.append('誤差クラス: ' + ' / '.join(flagged[:4]))
    qualified_bands = sum(
        1
        for band in qualification.band_qualifications
        if band.state == 'qualified'
    )
    if qualified_bands:
        parts.append('適格帯域×{0}'.format(qualified_bands))
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '波動忠実度適格: ' + ' — '.join(parts)


def geometric_fidelity_line(qualification) -> str:
    """One JA line for a geometric-fidelity qualification (#685):
    overall verdict plus deterministic/stochastic axis states and
    receiver/energy accounting — レイ数だけで収束とは読まない。"""
    parts = [fidelity_state_label(qualification.fidelity_state)]
    parts.append(
        '確定的軸='
        + _DETERMINISTIC_STATE_LABELS.get(
            qualification.deterministic_state,
            qualification.deterministic_state,
        )
        + ' / 確率的軸='
        + _DETERMINISTIC_STATE_LABELS.get(
            qualification.stochastic_state,
            qualification.stochastic_state,
        )
    )
    if qualification.receiver_domain_state == 'radius_unevaluated':
        parts.append('受信半径未掃引')
    if qualification.energy_accounting_state == 'declared_limitation':
        parts.append('エネルギ計上は限定宣言')
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '幾何忠実度適格: ' + ' — '.join(parts)


def hybrid_handoff_line(qualification) -> str:
    """One JA line for a hybrid transition qualification (#687):
    handoff verdict plus gap band or double-count findings — 帯域を
    比較してから読む。"""
    parts = [handoff_state_label(qualification.handoff_state)]
    if qualification.gap_band_hz is not None:
        low, high = qualification.gap_band_hz
        parts.append('ギャップ帯域: {0:.0f}–{1:.0f} Hz'.format(low, high))
    if qualification.double_count_findings:
        parts.append(
            '二重計上: '
            + ' / '.join(qualification.double_count_findings[:3])
        )
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return 'ハイブリッド引継適格: ' + ' — '.join(parts)


# REV58-AUDIOMODEL: 音響モデル権威 (#654/#655/#656/#690/#684/#681)

_ORIGIN_VERDICT_LABELS = {
    'origin_qualified': '原点適格',
    'origin_limited': '原点限定',
    'origin_unverified': '原点未検証',
    'insufficient_evidence': '証拠不足',
}

_FIELD_VERDICT_LABELS = {
    'directly_applicable': '直接適用可',
    'applicable_with_approximation': '近似で適用可',
    'transition_field_limited': '遷移場で限定',
    'distance_too_close_for_selected_far_field_model': (
        '遠距離場モデルに対し距離不足'
    ),
    'nearfield_only': '近接場のみ',
    'farfield_only': '遠距離場のみ',
    'requires_explicit_multi_radiator_model': '多放射器モデル必須',
    'insufficient_evidence': '証拠不足',
}

_DIRECTION_VERDICT_LABELS = {
    'measured_direction': '実測方向',
    'interpolated_eligible': '補間適格',
    'interpolated_limited': '補間限定',
    'extrapolated': '外挿',
    'outside_coverage': 'カバレッジ外',
    'sh_order_unsupported': 'SH次数超過',
    'insufficient_evidence': '証拠不足',
}

_COMBINATION_VERDICT_LABELS = {
    'combination_qualified': '合成適格',
    'qualified_with_limitations': '限定付き適格',
    'qualified_with_scenario_bounds': 'シナリオ境界付き適格',
    'incompatible_combination': '不整合な合成',
    'insufficient_evidence': '証拠不足',
}

_SCATTERING_VERDICT_LABELS = {
    'qualified_for_declared_domain': '宣言領域で適格',
    'qualified_with_limitations': '限定付き適格',
    'directional_redirection_unsupported': '方向再配分非対応',
    'insufficient_evidence': '証拠不足',
    'not_qualified': '不適格',
}

_DIFFRACTION_CAPABILITY_LABELS = {
    'physical_reference_capability': '物理基準能力',
    'physical_approximation_capability': '物理近似能力',
    'perceptual_approximation_capability': '知覚近似能力',
    'unqualified': '不適格',
    'insufficient_evidence': '証拠不足',
}


def source_origin_line(qualification) -> str:
    """One JA line for a source-origin qualification (#654): verdict
    plus the effective origin kind — CAD ポーズ既定の原点は読まない。"""
    parts = [_ORIGIN_VERDICT_LABELS.get(
        qualification.verdict, qualification.verdict)]
    parts.append('有効原点: {0}'.format(qualification.effective_origin_kind))
    if qualification.limitations:
        parts.append(qualification.limitations[0])
    elif qualification.reasons:
        parts.append(qualification.reasons[0])
    return '音源原点適格: ' + ' — '.join(parts)


def source_field_line(qualification) -> str:
    """One JA line for a source-field qualification (#655): verdict
    plus the effective field regime and requested distance — 測定距離
    宣言のない遠場再利用は読まない。"""
    parts = [_FIELD_VERDICT_LABELS.get(
        qualification.verdict, qualification.verdict)]
    parts.append('場種別: {0}'.format(qualification.effective_regime))
    if qualification.requested_distance_m is not None:
        parts.append(
            '要求距離: {0:.2f} m'.format(qualification.requested_distance_m)
        )
    if qualification.limitations:
        parts.append(qualification.limitations[0])
    elif qualification.reasons:
        parts.append(qualification.reasons[0])
    return '音場適用適格: ' + ' — '.join(parts)


def directivity_direction_line(qualification) -> str:
    """One JA line for a direction query qualification (#656): verdict
    plus queried angles — 補間密度は実測情報密度として読まない。"""
    parts = [_DIRECTION_VERDICT_LABELS.get(
        qualification.verdict, qualification.verdict)]
    parts.append(
        '方向: az={0:.1f}° el={1:.1f}°'.format(
            qualification.azimuth_deg, qualification.elevation_deg)
    )
    if qualification.limitations:
        parts.append(qualification.limitations[0])
    elif qualification.reasons:
        parts.append(qualification.reasons[0])
    return '方向問合せ適格: ' + ' — '.join(parts)


def source_combination_line(qualification) -> str:
    """One JA line for a source-combination qualification (#690):
    verdict plus requested/effective modes — 相関証拠なしの独立源仮定は
    読まない。"""
    parts = [_COMBINATION_VERDICT_LABELS.get(
        qualification.verdict, qualification.verdict)]
    parts.append('要求モード: {0}'.format(qualification.requested_mode))
    if qualification.effective_mode is not None and (
        qualification.effective_mode != qualification.requested_mode
    ):
        parts.append('有効モード: {0}'.format(qualification.effective_mode))
    if qualification.reasons:
        parts.append(qualification.reasons[0])
    return '音源合成適格: ' + ' — '.join(parts)


def scattering_model_line(qualification) -> str:
    """One JA line for a scattering-model qualification (#684): verdict
    plus the effective solver model — 係数を黙って Lambert には読まない。"""
    parts = [_SCATTERING_VERDICT_LABELS.get(
        qualification.verdict, qualification.verdict)]
    if qualification.effective_model is not None:
        parts.append('モデル: {0}'.format(qualification.effective_model))
    if qualification.limitations:
        parts.append(qualification.limitations[0])
    elif qualification.reasons:
        parts.append(qualification.reasons[0])
    return '散乱モデル適格: ' + ' — '.join(parts)


def edge_diffraction_line(qualification) -> str:
    """One JA line for an edge-diffraction qualification (#681):
    capability plus covered fixture kinds — 未検証モデルの正確さは
    読まない。"""
    parts = [_DIFFRACTION_CAPABILITY_LABELS.get(
        qualification.capability, qualification.capability)]
    if qualification.covered_fixture_kinds:
        parts.append(
            'fixtures: '
            + ' / '.join(qualification.covered_fixture_kinds[:4])
        )
    if qualification.boundary_limited:
        parts.append('境界限定')
    if qualification.limitations:
        parts.append(qualification.limitations[0])
    elif qualification.reasons:
        parts.append(qualification.reasons[0])
    return '回折適格: ' + ' — '.join(parts)
