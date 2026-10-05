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
from .cad_response_target import (
    ResponseTargetProfile,
    SpectralBalanceEvaluation,
)
from .cad_rp22_profile import RP22Evaluation
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
# REV56-TARGETS: RP22 standards profile + response-target authority
# (#579/#588)

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
