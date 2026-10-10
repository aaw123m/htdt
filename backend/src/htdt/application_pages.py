"""Application-scope destinations for the workflow shell (UX160 IA v2).

These pages are read-mostly surfaces over existing persisted authorities —
the project library lists real documents, the inbox lists real capture items,
activity lists real revisions, the library wraps EquipmentLibraryService, and
support reports the real diagnostics/version data. None of them fabricate
project state.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal

from PySide6.QtCore import QDate, QObject, Qt, Signal
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .build_info import version_string
from .cad_repository import SceneRepository
from .capture_inbox import capture_inbox_item_project_id
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .navigation_target import (
    NavigationIntent,
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from .reference_library_browser import (
    STATUS_ALL,
    STATUS_ATTENTION,
    STATUS_LATEST,
    STATUS_UNQUALIFIED,
    category_label,
    collect_library_rows,
    compare_rows,
    filter_rows,
    row_status_label,
)
from .automatic_backup import AutomaticBackupScheduler
from .project_library_repository import ProjectLibraryRepository
from .project_lifecycle import (
    DeletionBlocker,
    ProjectDeletionBlockedError,
    ProjectDeletionPlan,
    ProjectDeletionStaleError,
    ProjectLibrary as _LifecycleProjectLibrary,
    ProjectTombstone,
)
from .diagnostics_support import diagnostics_dir
from .support_diagnostics import (
    HealthCategory,
    HealthCheckResult,
    HealthReport,
    HealthStatus,
)
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)
from .user_facing_error import operation_error_message
from .operation_error_dialog import warn_user
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_shell import TargetFocusResult
from .activity_center import (
    RetryPolicy,
    operation_progress_text,
    operation_state_label,
)


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    """One canonical project row for the Projects listing (#919).

    ``project_id`` is the stable semantic identity used to open a project;
    ``display_name`` is presentation only.
    """

    project_id: str
    document_id: str
    display_name: str
    head_revision_id: str | None
    created_at_utc: str
    revision_count: int
    archived: bool = False
    last_opened_at_utc: str | None = None


class ProjectLibraryService:
    """Lists canonical projects — read-only over the library authority.

    ``ProjectLibraryRepository`` auto-registers every document that already
    carries scene content, so canonical ``htdt_project_documents`` rows cover
    legacy and unregistered documents alike (#919).
    """

    def __init__(
        self,
        repository: SceneRepository,
        project_library: ProjectLibraryRepository | None = None,
    ) -> None:
        self.path = Path(repository.path)
        self._repository = repository
        self._project_library = project_library or ProjectLibraryRepository(
            repository
        )
        self._lifecycle: _LifecycleProjectLibrary | None = None

    def _lifecycle_library(self) -> _LifecycleProjectLibrary:
        """The lifecycle authority over the same store (archive/delete)."""
        if self._lifecycle is None:
            self._lifecycle = _LifecycleProjectLibrary(self.path)
        return self._lifecycle

    def list_projects(self) -> tuple[ProjectEntry, ...]:
        """All registered projects, archived included (they stay restorable)."""
        heads = self._document_heads()
        return tuple(
            ProjectEntry(
                project_id=entry.project_id,
                document_id=entry.document_id,
                display_name=entry.display_name,
                head_revision_id=(
                    None
                    if entry.document_id not in heads
                    else heads[entry.document_id][0]
                ),
                created_at_utc=entry.created_at_utc,
                revision_count=(
                    0
                    if entry.document_id not in heads
                    else heads[entry.document_id][1]
                ),
                archived=entry.archived,
                last_opened_at_utc=entry.last_opened_at_utc,
            )
            for entry in self._project_library.list_projects(
                include_archived=True
            )
        )

    def set_archived(self, project_id: str, archived: bool) -> None:
        """Archive/restore via the lifecycle authority (#753)."""
        if archived:
            self._lifecycle_library().archive_project(project_id)
        else:
            self._lifecycle_library().unarchive_project(project_id)

    def plan_project_deletion(self, project_id: str) -> ProjectDeletionPlan:
        """Read-only deletion preview; nothing is removed yet."""
        return self._lifecycle_library().plan_project_deletion(project_id)

    def delete_project(
        self, project_id: str, *, expected_plan: ProjectDeletionPlan
    ) -> ProjectTombstone:
        """Atomic delete pinned to the plan the user approved."""
        return self._lifecycle_library().delete_project(
            project_id, expected_plan=expected_plan
        )

    def _document_heads(self) -> dict[str, tuple[str, int]]:
        if not self.path.is_file():
            return {}
        try:
            with closing(self._repository._read()) as connection, connection:
                rows = connection.execute(
                    """
                    SELECT h.document_id AS document_id,
                           h.head_revision_id AS head_revision_id,
                           (SELECT COUNT(*) FROM scene_revisions s
                            WHERE s.document_id = h.document_id) AS revisions
                    FROM scene_document_heads h
                    """
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(document_id): (str(head_revision_id), int(revisions))
            for document_id, head_revision_id, revisions in rows
        }


def _page_layout(page: QWidget, title: str, hint: str | None = None) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(10)
    heading = QLabel(title)
    set_typography_role(heading, TypographyRole.WORKSPACE_TITLE)
    layout.addWidget(heading)
    if hint:
        label = QLabel(hint)
        set_typography_role(label, TypographyRole.SECONDARY)
        label.setWordWrap(True)
        layout.addWidget(label)
    return layout


#: JP labels for the authority tables a deletion plan can enumerate.
#: Unknown names render verbatim — the plan never invents a friendlier
#: name for a table it did not predict.
_LIFECYCLE_TABLE_LABELS = {
    "scene_revisions": "シーンリビジョン",
    "scene_bookmarks": "ブックマーク",
    "scene_review_marks": "レビュー",
    "cad_measurements": "測定",
    "cad_frequency_responses": "周波数応答",
    "cad_impulse_responses": "インパルス応答",
    "cad_calibration_plans": "校正プラン",
    "cad_correction_qualifications": "補正修飾レコード",
    "cad_comparison_records": "比較履歴",
    "cad_measurement_uncertainty_budgets": "測定不確かさ予算",
    "cad_measurement_significance_assessments": "残差有意性評価",
    "cad_measurement_state_policies": "測定状態ポリシー",
    "cad_measurement_state_snapshots": "測定状態スナップショット",
    "cad_measurement_state_verdicts": "測定状態判定",
    "cad_measurement_transforms": "測定変換レコード",
    "cad_decision_rule_specs": "決定ルール仕様",
    "cad_decision_verdicts": "証拠判定レコード",
    "cad_uncertain_input_sets": "不確かさ入力セット",
    "cad_robust_design_assessments": "堅牢設計評価",
    "cad_stimulus_assets": "刺激アセット登録",
    "cad_stimulus_pins": "測定刺激ピン",
    "cad_stimulus_eligibility": "刺激適格性判定",
    "cad_bass_splice_evidence": "バス合成証拠",
    "cad_bass_qualifications": "バス管理適格性",
    "cad_device_backup_artifacts": "デバイスバックアップアーティファクト",
    "cad_device_config_snapshots": "デバイス設定スナップショット",
    "cad_device_firmware_transitions": "ファームウェア更新記録",
    "cad_device_known_good_baselines": "既知良好ベースライン",
    "cad_device_replacement_assessments": "代替機器ポータビリティ評価",
    "cad_device_restore_records": "デバイス設定復元記録",
    "cad_external_standard_documents": "外部規格登録ドキュメント",
    "cad_standard_evaluation_pins": "規格評価ピン",
    "cad_standard_lifecycle_observations": "規格ライフサイクル観測",
    "cad_standard_profile_mappings": "規格プロファイルマッピング",
    "cad_standard_revision_diffs": "規格改版差分",
    "cad_spatial_campaign_designs": "空間測定キャンペーン設計",
    "cad_spatial_campaign_evaluations": "空間キャンペーン設計評価",
    "cad_spatial_campaign_bindings": "測定点キャプチャ束縛",
    "cad_rp32_profiles": "RP32コミッショニングプロファイル",
    "cad_rp32_reconciliations": "RP32設計・実測照合",
    "cad_rp32_readiness": "RP32測定準備評価",
    "cad_rp32_verification_plans": "RP32検証計画",
    "cad_rp32_verification_records": "RP32検証レコード",
    "cad_rp32_reports": "RP32コミッショニングレポート",
    "cad_room_noise_metric_profiles": "室内ノイズ指標プロファイル",
    "cad_background_noise_measurements": "バックグラウンドノイズ測定",
    "cad_noise_criterion_evaluations": "ノイズ評価基準評価",
    "cad_speech_intelligibility_profiles": "音声明瞭度プロファイル",
    "cad_sti_measurements": "STI測定",
    "cad_sti_predictions": "STI予測",
    "cad_dialogue_intelligibility_assessments": "対話明瞭度評価",
    "cad_content_loudness_profiles": "コンテンツラウドネスプロファイル",
    "cad_programme_loudness_measurements": "番組ラウドネス測定",
    "cad_normalization_observations": "正規化観測",
    "cad_playback_gain_states": "再生ゲイン状態",
    "cad_loudness_matching_records": "ラウドネス整合レコード",
    "cad_rp22_profiles": "RP22標準プロファイル",
    "cad_rp22_evaluations": "RP22適合性評価",
    "cad_response_targets": "応答目標プロファイル",
    "cad_spectral_balance_evaluations": "スペクトルバランス評価",
    "cad_electrical_qualifications": "アンプ・スピーカー電気適合性評価",
    "cad_physical_interconnects": "物理配線経路（as-built）",
    "cad_wiring_verifications": "配線検証レコード",
    "cad_logical_physical_bindings": "論理経路・物理配線バインディング",
    "cad_av_latency_profiles": "A/V同期プロファイル",
    "cad_av_latency_paths": "A/V遅延パス",
    "cad_av_latency_path_measurements": "A/V同期測定",
    "cad_av_latency_qualifications": "A/V同期適合評価",
    "cad_hdmi_signal_profiles": "HDMI要求信号プロファイル",
    "cad_hdmi_edid_artifacts": "EDIDアーティファクト",
    "cad_hdmi_hdcp_observations": "HDCP状態観測",
    "cad_hdmi_link_observations": "HDMIリンク状態観測",
    "cad_hdmi_verification_records": "HDMI検証レコード",
    "cad_hdmi_qualifications": "HDMI適合評価",
    "cad_rp28_profiles": "RP28プロファイル",
    "cad_network_av_paths": "ネットワークAV経路",
    "cad_network_media_flows": "ネットワークメディアフロー",
    "cad_network_transport_observations": "ネットワーク伝送観測",
    "cad_network_timing_observations": "ネットワークタイミング観測",
    "cad_network_av_qualifications": "ネットワークAV適合評価",
    "cad_monitoring_declarations": "監視能力・収集許可宣言",
    "cad_lifecycle_observations": "ライフサイクル観測記録",
    "cad_change_events": "システム変更イベント",
    "cad_trend_assessments": "傾向評価",
    "cad_symptom_episodes": "症状エピソード",
    "cad_drift_assessments": "ドリフト評価",
    "cad_reverification_triggers": "再検証トリガー",
    "cad_restore_confirmations": "復元確認レコード",
    "cad_substitution_proposals": "機器代替提案",
    "cad_change_impact_assessments": "変更影響評価",
    "cad_substitution_decisions": "代替承認判定",
    "cad_asbuilt_reconciliations": "竣工機器照合",
    "cad_equipment_schedule_records": "機器スケジュール履歴",
    "cad_isolation_elements": "遮音構成要素",
    "cad_interroom_scenarios": "室間遮音シナリオ",
    "cad_interroom_field_measurements": "室間遮音実測",
    "cad_isolation_calibrations": "遮音校正レコード",
    "cad_isolation_qualifications": "遮音適合評価",
    "cad_mechanical_noise_tests": "機械ノイズ試験",
    "cad_rattle_events": "ラトルイベント",
    "cad_remediation_actions": "ラトル是正処置",
    "cad_mechanical_noise_qualifications": "機械ノイズ適合評価",
    "cad_seat_acoustic_models": "座席音響モデル",
    "cad_occupancy_scenarios": "占有シナリオ",
    "cad_clearance_evaluations": "直達音クリアランス評価",
    "cad_seating_commissioning_results": "座席コミッショニング結果",
    "cad_security_assets": "セキュリティ資産宣言",
    "cad_security_credentials": "資格情報レコード",
    "cad_security_surfaces": "管理サーフェス宣言",
    "cad_security_observations": "セキュリティ観測記録",
    "cad_security_risks": "セキュリティリスク記録",
    "cad_remote_service_authorizations": "リモートサービス認可",
    "cad_security_test_evidence": "セキュリティ試験証拠",
    "cad_access_reviews": "アクセスレビュー記録",
    "cad_security_reviews": "セキュリティレビュー評価",
    "cad_control_surfaces": "制御サーフェス宣言",
    "cad_control_scenarios": "制御シナリオ宣言",
    "cad_control_scenario_runs": "制御シナリオ実行記録",
    "cad_control_qualifications": "制御シナリオ適格評価",
    "cad_exposure_limits": "曝露限界プロファイル",
    "cad_spl_capabilities": "SPL能力宣言",
    "cad_test_exposure_plans": "試験曝露プラン",
    "cad_exposure_gates": "曝露ゲート判定",
    "cad_exposure_assessments": "曝露評価",
    "cad_ifc_import_artifacts": "IFCインポート成果物",
    "cad_ifc_entity_mappings": "IFCエンティティマッピング",
    "cad_ifc_revision_deltas": "IFCリビジョン差分",
    "cad_ifc_diff_applies": "IFC差分適用レコード",
    "cad_ifc_intake_profiles": "IFC取込プロファイル",
    "cad_ifc_intake_evaluations": "IFC取込評価",
    "cad_ifc_exports": "IFCエクスポート",
    "cad_performance_fact_profiles": "性能ファクトプロファイル",
    "cad_performance_fact_products": "製品識別情報",
    "cad_performance_facts": "性能ファクト",
    "cad_performance_fact_imports": "性能ファクト取込",
    "cad_performance_fact_evaluations": "製品適合評価",
    "cad_performance_fact_rebinds": "プロファイル再バインド",
    # REV56-INFRA: #587 ラック/電源/熱適格性
    "cad_rack_enclosures": "ラックエンクロージャ",
    "cad_rack_devices": "ラック収納機器",
    "cad_branch_circuits": "分岐回路",
    "cad_power_protection_devices": "電源保護機器",
    "cad_poe_budgets": "PoE電力バジェット",
    "cad_infrastructure_scenarios": "インフラ動作シナリオ",
    "cad_rack_thermal_measurements": "ラック熱測定",
    "cad_infrastructure_qualifications": "インフラ適格評価",
    # REV56-INFRA: #603 イマーシブレンダーパス
    "cad_immersive_contents": "イマーシブコンテンツ",
    "cad_renderer_capabilities": "レンダラ能力プロファイル",
    "cad_speaker_layouts": "スピーカーレイアウト宣言",
    "cad_render_sessions": "レンダリングセッション",
    "cad_render_output_observations": "出力観測",
    "cad_render_path_qualifications": "レンダーパス適格評価",
    # REV56-INFRA: #606 ハム/バズ/接地EMC診断
    "cad_electrical_noise_observations": "電気ノイズ観測",
    "cad_audio_interconnects": "音声インターコネクト証跡",
    "cad_noise_isolation_tests": "ノイズ分離試験",
    "cad_humbuzz_diagnostics": "ハム/バズ診断",
    "cad_noise_mitigations": "ノイズ是正措置",
    "cad_humbuzz_verdicts": "ハム/バズ評価",
    # REV57-METRO: #609 タイムベース/クロック権威
    "cad_timebase_clock_domains": "クロックドメイン宣言",
    "cad_measurement_timebases": "測定タイムベース権威",
    "cad_timebase_capability_assessments": "タイムベース能力評価",
    # REV57-METRO: #610 証拠バンドル/整合性マニフェスト
    "cad_evidence_bundles": "証拠バンドル",
    "cad_evidence_artifacts": "証拠アーティファクト",
    "cad_evidence_derivation_edges": "派生プロビナンスエッジ",
    "cad_evidence_attestations": "バンドルアテステーション",
    "cad_evidence_bundle_validations": "バンドル検証判定",
    # REV57-METRO: #611 校正ライフサイクル
    "cad_instrument_instances": "測定器インスタンス",
    "cad_calibration_events": "校正イベント",
    "cad_calibration_interval_policies": "校正間隔ポリシー",
    "cad_instrument_verification_checks": "検証チェック記録",
    "cad_instrument_service_events": "機器サービスイベント",
    "cad_instrument_fitness_assessments": "機器適性評価",
    "cad_out_of_tolerance_reviews": "公差外影響レビュー",
    # REV57-PHYS: #613 幾何測量 / #614 設置スピーカー境界 / #615 多孔質吸収体
    "cad_geo_survey_instruments": "測量機器",
    "cad_geo_survey_campaigns": "測量キャンペーン",
    "cad_geo_element_evidence": "幾何要素証跡",
    "cad_geo_control_measurements": "測量コントロール計測",
    "cad_geo_reconciliations": "as-built 整合",
    "cad_geo_task_requirements": "幾何タスク要件",
    "cad_geo_qualifications": "幾何適格評価",
    "cad_src_meas_conditions": "スピーカー測定条件",
    "cad_src_mounting_conditions": "設置条件",
    "cad_src_boundary_corrections": "境界補正",
    "cad_src_measurements": "設置済み計測",
    "cad_src_boundary_qualifications": "境界適格評価",
    "cad_pam_parameter_evidence": "多孔材パラメータ証跡",
    "cad_pam_material_models": "多孔材モデル",
    "cad_pam_buildups": "多孔材構成",
    "cad_pam_predictions": "多孔材予測",
    "cad_pam_fit_comparisons": "多孔材フィット比較",
    # REV57-PROJ: #619 空間投影画質適格性
    "cad_spatial_measurement_plans": "空間測定プラン",
    "cad_spatial_measurement_sets": "空間測定セット",
    "cad_spatial_derived_maps": "空間補間マップ",
    "cad_spatial_uniformity_evaluations": "空間均一性評価",
    # REV57-PROJ: #622 投影幾何/マスキング
    "cad_presentation_geometry_bindings": "プレゼンテーション幾何バインディング",
    "cad_image_geometry_measurements": "画像幾何測定",
    "cad_lens_memory_recalls": "レンズメモリ呼出記録",
    "cad_geometry_evaluations": "幾何評価",
    # REV57-PROJ: #624 ハッシュボックス/エンクロージャ共同設計
    "cad_projector_install_constraints": "プロジェクタ設置制約",
    "cad_projector_enclosure_plans": "エンクロージャ計画",
    "cad_enclosure_operating_observations": "エンクロージャ動作観測",
    "cad_enclosure_acoustic_observations": "エンクロージャ音響観測",
    "cad_enclosure_qualifications": "エンクロージャ適格性評価",
    # REV57-PROJ: #627 光放射安全
    "cad_projector_safety_identities": "プロジェクタ安全識別",
    "cad_manufacturer_safety_constraints": "メーカー安全制約",
    "cad_projector_placements": "プロジェクタ配置宣言",
    "cad_optical_safety_evaluations": "光放射安全評価",
    # REV57-DISP: #625 直視ディスプレイ / #626 観察者メタメリズム / #633 視聴環境
    "cad_dv_display_states": "ディスプレイ状態",
    "cad_dv_stimulus_contexts": "ディスプレイ刺激コンテキスト",
    "cad_dv_photometric_measurements": "測光測定",
    "cad_dv_temporal_observations": "時間調光観測",
    "cad_dv_spatial_measurements": "パネル均一性測定",
    "cad_dv_angle_measurements": "視角測定",
    "cad_dv_qualifications": "直視ディスプレイ適格評価",
    "cad_om_spectral_states": "分光状態",
    "cad_om_observer_profiles": "観察者モデルプロファイル",
    "cad_om_evaluations": "メタメリズム評価",
    "cad_om_perceptual_matches": "知覚マッチ記録",
    "cad_om_qualifications": "メタメリズム適格評価",
    "cad_ve_observations": "視聴環境観測",
    "cad_ve_geometry_observations": "視聴幾何観測",
    "cad_ve_lighting_scenes": "照明シーン",
    "cad_ve_qualifications": "視聴環境適格評価",
    # REV57-AUD: #621/#634/#628/#632.
    "cad_channel_identity_chains": "チャネル同一性チェーン",
    "cad_acoustic_endpoint_observations": "音響端点観測",
    "cad_channel_identity_tests": "チャネル同一性テスト",
    "cad_polarity_verification_records": "極性検証記録",
    "cad_channel_identity_evaluations": "チャネル同一性評価",
    "cad_acoustic_aim_states": "音響エイム状態",
    "cad_coverage_listener_areas": "カバレッジ聴取エリア",
    "cad_coverage_predictions": "カバレッジ予測",
    "cad_coverage_measurement_sets": "カバレッジ実測セット",
    "cad_coverage_qualifications": "カバレッジ修飾",
    "cad_instance_acoustic_evidence": "個体音響証跡",
    "cad_model_instance_deltas": "型番↔個体デルタ",
    "cad_matched_set_declarations": "マッチドセット宣言",
    "cad_matched_set_qualifications": "マッチドセット修飾",
    "cad_playback_stack_identities": "再生スタック同一性",
    "cad_media_profile_requirements": "メディアプロファイル要件",
    "cad_playback_capability_records": "再生能力記録",
    "cad_playback_operation_runs": "再生オペレーション実行",
    "cad_playback_qualifications": "再生能力修飾",
    # REV57-INST: #616 HVAC共同設計 / #618 再生参照校正 / #631 as-builtトリートメント / #612 触覚・シート振動
    "cad_hvac_ventilation_scenarios": "換気シナリオ",
    "cad_hvac_path_declarations": "HVACパス宣言",
    "cad_hvac_component_evidence": "HVACコンポーネント証拠",
    "cad_hvac_field_observations": "HVAC現場観測",
    "cad_hvac_qualifications": "HVAC適格評価",
    "cad_ref_cal_profiles": "参照校正プロファイル",
    "cad_ref_cal_stimuli": "校正刺激",
    "cad_ref_cal_observations": "チャネル校正観測",
    "cad_ref_cal_qualifications": "参照校正適格評価",
    "cad_treatment_install_specs": "トリートメント設置仕様",
    "cad_treatment_asbuilt_observations": "as-built観測",
    "cad_treatment_inspections": "トリートメント検査記録",
    "cad_treatment_qualifications": "as-built適格評価",
    "cad_tactile_vibration_paths": "触覚パス",
    "cad_tactile_vibration_measurements": "振動測定",
    "cad_tactile_profiles": "触覚プロファイル",
    "cad_tactile_vibration_qualifications": "触覚振動適格評価",
    # REV57-MOUNT: #620 AV取付/構造支持証拠
    "cad_mount_assemblies": "取付アセンブリ",
    "cad_mount_load_evidence": "取付荷重証拠",
    "cad_mount_support_elements": "構造支持要素",
    "cad_mount_manufacturer_requirements": "メーカー取付要件",
    "cad_mount_structural_approvals": "構造承認記録",
    "cad_mount_inspection_records": "取付検査記録",
    "cad_mount_qualifications": "取付支持修飾",
    # REV58-MEASCHAIN: #695 測定チェーン線形性/過負荷
    "cad_measchain_linearity_profiles": "測定チェーン線形性プロファイル",
    "cad_measchain_overload_observations": "取得過負荷観測",
    "cad_measchain_qualifications": "測定チェーン適格評価",
    # REV58-MEASCHAIN: #697 swept-sine畳込分離
    "cad_sweep_deconvolution_specs": "畳込分離仕様",
    "cad_harmonic_impulse_components": "高調波インパルス成分",
    "cad_recovered_impulse_responses": "復元インパルス応答",
    "cad_linear_ir_capabilities": "線形IR能力評価",
    # REV58-MEASCHAIN: #668 室音響励起源
    "cad_excitation_source_profiles": "励起源プロファイル",
    "cad_source_orientation_captures": "音源指向キャプチャ",
    "cad_measurement_source_qualifications": "測定ソース適格評価",
    # REV58-DSPDECAY: #679 DSPフィルタ実現
    "cad_dsp_realization_profiles": "DSP実現プロファイル",
    "cad_dsp_stage_records": "DSP段階レコード",
    "cad_dsp_parameter_mappings": "DSPパラメータ写像",
    "cad_dsp_realization_qualifications": "DSP実現適格評価",
    # REV58-DSPDECAY: #676 減衰曲線ノイズ/切断処理
    "cad_decay_processing_profiles": "減衰処理プロファイル",
    "cad_decay_noise_estimates": "雑音床推定",
    "cad_decay_truncation_decisions": "RIR切断判定",
    "cad_decay_edc_artifacts": "EDCアーティファクト",
    "cad_decay_fit_records": "減衰フィット記録",
    # REV58-DSPDECAY: #705 音響インピーダンス物理実現性
    "cad_boundary_evidence_records": "境界証拠レコード",
    "cad_boundary_rational_fits": "境界有理フィット",
    "cad_td_impedance_realizations": "時間領域インピーダンス実現",
    "cad_boundary_realizability_assessments": "境界実現性評価",
    # REV58-NUMERIC: #683/#685/#687 ソルバー数値忠実度
    "cad_wave_fidelity_profiles": "波動忠実度プロファイル",
    "cad_wave_convergence_records": "波動収束記録",
    "cad_wave_fidelity_qualifications": "波動忠実度適格評価",
    "cad_geometric_fidelity_profiles": "幾何忠実度プロファイル",
    "cad_ray_sampling_convergences": "レイサンプリング収束記録",
    "cad_path_enumeration_qualifications": "経路列挙適格評価",
    "cad_geometric_fidelity_qualifications": "幾何忠実度適格評価",
    "cad_hybrid_composition_profiles": "ハイブリッド合成プロファイル",
    "cad_hybrid_transition_qualifications": "ハイブリッド引継適格評価",
    # REV58-AUDIOMODEL: #654/#655/#656/#690/#684/#681 音響モデル権威
    "cad_source_origin_profiles": "音源基準原点プロファイル",
    "cad_source_origin_qualifications": "音源原点適格評価",
    "cad_source_field_profiles": "音源音場適用プロファイル",
    "cad_source_field_qualifications": "音場適用適格評価",
    "cad_directivity_sampling_profiles": "指向性サンプリングプロファイル",
    "cad_directivity_interpolation_records": "指向性補間レコード",
    "cad_directivity_direction_qualifications": "方向問合せ適格評価",
    "cad_source_coherence_profiles": "音源相関プロファイル",
    "cad_source_combination_qualifications": "音源合成適格評価",
    "cad_scattering_model_profiles": "散乱モデルプロファイル",
    "cad_scattering_model_qualifications": "散乱モデル適格評価",
    "cad_diffraction_model_profiles": "回折モデルプロファイル",
    "cad_diffraction_benchmark_results": "回折ベンチ結果",
    "cad_diffraction_qualifications": "回折適格評価",
    # REV58-IDENT: #691 型付き対数量/dB基準
    "cad_log_quantities": "型付き対数量",
    "cad_log_calibration_bridges": "対数量校正ブリッジ",
    "cad_log_operations": "対数量演算評価",
    # REV58-IDENT: #689 校正パラメータ同定性
    "cad_calib_parameter_records": "校正パラメータ記録",
    "cad_ident_sensitivity_evidence": "同定性感度証拠",
    "cad_ident_correlation_evidence": "同定性相関証拠",
    "cad_ident_equivalent_sets": "同定等価解集合",
    "cad_identifiability_assessments": "同定性評価",
    # REV58-IDENT: #698 検証サンプル依存/ベンチリーク
    "cad_validation_statistical_designs": "検証統計設計",
    "cad_dependence_models": "検証依存構造モデル",
    "cad_dataset_role_assignments": "データセット役割割当",
    "cad_benchmark_exposures": "ベンチマーク露出台帳",
    "cad_challenge_qualifications": "検証主張適格評価",
    # REV58-VALIDMETH: #675 最適化アルゴリズム適格
    "cad_optimization_problems": "最適化問題同一性",
    "cad_optimizer_run_profiles": "最適化実行プロファイル",
    "cad_optimizer_qualifications": "最適化実行適格",
    "cad_pareto_assessments": "Pareto近似評価",
    # REV58-VALIDMETH: #674 固有モード検証
    "cad_mode_pairings": "モード対応付け",
    "cad_eigenmode_verdicts": "固有モード検証判定",
    # REV58-VALIDMETH: #673 拡散場適用性
    "cad_diffuseness_assessments": "拡散場評価",
    "cad_statistical_applicability_declarations": "統計モデル適用性宣言",
    # REV58-VALIDMETH: #671 結合室マルチスロープ減衰
    "cad_multi_slope_fits": "マルチスロープフィット",
    "cad_single_slope_assessments": "単一スロープ適性評価",
    "cad_coupled_decay_qualifications": "結合室減衰適格",
    # REV58-VALIDMETH: #677 初期反射対応
    "cad_reflection_pairings": "反射対応付け",
    "cad_reflection_correspondence_sets": "反射対応セット",
    "cad_reflection_correspondence_verdicts": "反射対応判定",
    # REV58-VALIDMETH: #706 時周波モーダル減衰
    "cad_modal_decay_observations": "時周波減衰観測",
    "cad_modal_decay_qualifications": "時周波減衰適格",
    # REV58-DISPLAYMEAS: #682 パターンジェネレータ忠実度
    "cad_pg_generator_instances": "パターンジェネレータ実機",
    "cad_pg_requested_patches": "要求映像パッチ",
    "cad_pg_delivered_observations": "送出刺激観測",
    "cad_pg_fidelity_qualifications": "ジェネレータ忠実度適格評価",
    # REV58-DISPLAYMEAS: #680 プローブマッチング/分光ミスマッチ
    "cad_mm_match_profiles": "計測器マッチングプロファイル",
    "cad_mm_match_observations": "プローブマッチ観測",
    "cad_mm_verifications": "プローブマッチ検証",
    "cad_mm_applicability": "補正適用可否評価",
    # REV58-DISPLAYMEAS: #686 加法性/RGB分離/立体特性
    "cad_da_additivity_observations": "加法性観測",
    "cad_da_separation_assessments": "RGB分離評価",
    "cad_da_volumetric_characterisations": "立体特性測定",
    "cad_da_holdout_verifications": "ホールドアウト検証",
    "cad_da_model_eligibility": "校正モデル適格評価",
    "cad_da_characterisation_plans": "特性測定計画",
    # REV58-DISPLAYMEAS: #647 時間応答ディスプレイ忠実度
    "cad_td_states": "時間応答状態",
    "cad_td_step_responses": "ステップ応答測定",
    "cad_td_motion_measurements": "動画アーティファクト測定",
    "cad_td_flicker_measurements": "フリッカー測定",
    "cad_td_retention_observations": "残像観測",
    "cad_td_qualifications": "時間応答適格評価",
    # REV58-DISPLAYMEAS: #666 LUTクローズドループ校正
    "cad_lut_artifacts": "LUTアーティファクト",
    "cad_lut_generation_records": "LUT生成レコード",
    "cad_lut_preflight_verifications": "LUT転送前検証",
    "cad_lut_deployments": "LUTデプロイ記録",
    "cad_lut_post_verifications": "LUT転送後検証",
    "cad_lut_qualifications": "LUTループ適格評価",
    # REV58-MEASELEC: #699 オーディオI/F ループバック校正
    "cad_interface_loopback_observations": "I/Fループバック観測",
    "cad_interface_transfer_calibrations": "I/F伝達校正",
    "cad_interface_correction_qualifications": "I/F補正適格評価",
    # REV58-MEASELEC: #651 ゲイン構造/ノイズ床
    "cad_signal_level_references": "基準レベル参照",
    "cad_noise_floor_observations": "ノイズ床観測",
    "cad_clipping_margins": "クリッピング余裕",
    "cad_gain_structure_qualifications": "ゲイン構造適格評価",
    # REV58-MEASELEC: #649 再生ダイナミクス/リミッタ
    "cad_playback_dynamics_states": "再生ダイナミクス状態",
    "cad_level_sweep_observations": "レベル掃引観測",
    "cad_playback_dynamics_qualifications": "再生ダイナミクス適格評価",
    # REV58-MEASELEC: #665 アクティブクロスオーバー
    "cad_multiway_speaker_definitions": "マルチウェイスピーカー定義",
    "cad_active_crossover_plans": "アクティブXO計画",
    "cad_driver_alignment_measurements": "ドライバアライメント測定",
    "cad_active_crossover_qualifications": "アクティブXO適格評価",
    # REV58-MEASELEC: #693 測定法再現性
    "cad_method_procedures": "測定手順定義",
    "cad_reproducibility_campaigns": "再現性キャンペーン",
    "cad_method_precision_models": "測定法精度モデル",
    "cad_reproducibility_qualifications": "再現性適格評価",
    # REV59-APPLY: #723 デバイス適用トランザクション
    "cad_apply_capability_profiles": "デバイス適用能力プロファイル",
    "cad_apply_plans": "デバイス適用計画",
    "cad_apply_write_records": "適用書込み記録",
    "cad_apply_verifications": "適用後検証記録",
    "cad_apply_rollback_plans": "ロールバック計画",
    "cad_apply_rollback_executions": "ロールバック実行記録",
    "cad_apply_transactions": "デバイス適用トランザクション",
    "cad_fractional_octave_profiles": "分数オクターブ帯域定義",
    "cad_band_integrations": "帯域統合レコード",
    "cad_echo_density_profiles": "エコー密度推定プロファイル",
    "cad_mixing_time_estimates": "ミキシングタイム推定",
    "cad_late_field_assessments": "後期音場遷移評価",
    "cad_interpolation_profiles": "音場補間プロファイル",
    "cad_field_surface_records": "音場面レコード",
    "cad_solver_budget_profiles": "ソルバー計算予算プロファイル",
    "cad_compute_observations": "計算資源観測レコード",
    "cad_accuracy_cost_envelopes": "精度-コストエンベロープ",
    "cad_prerun_estimates": "実行前計算コスト推定レコード",
    "cad_jitter_profiles": "ジッタ測定プロファイル",
    "cad_jitter_observations": "ジッタ観測レコード",
    "cad_jitter_transfer_measurements": "ジッタ伝達測定",
    "cad_converter_jitter_susceptibility": "コンバータジッタ感受性",
    "cad_dither_profiles": "ディザ/ノイズシェイププロファイル",
    "cad_digital_path_transforms": "デジタルパス変換レコード",
    "cad_playback_src_profiles": "再生 SRC プロファイル",
    "cad_src_qualifications": "SRC 適格レコード",
    "cad_clock_domain_crossings": "クロックドメイン横断レコード",
    "cad_interchannel_leakage_measurements": "チャネル間漏洩測定",
    "cad_channel_separation_qualifications": "チャネル分離適格レコード",
    "cad_projector_light_profiles": "プロジェクター光源プロファイル",
    "cad_temporal_contrast_measures": "時間領域コントラスト測定",
    "cad_dynamic_contrast_qualifications": "動的コントラスト適格レコード",
    "cad_light_measurement_capabilities": "輝度計測定能力レコード",
    "cad_low_luminance_observations": "低輝度観測レコード",
    "cad_display_boundary_profiles": "表示面音響境界プロファイル",
    "cad_front_stage_variants": "フロントステージ配置バリアント",
    "cad_codec_chain_profiles": "コーデックチェーンプロファイル",
    "cad_quality_method_profiles": "品質評価方式プロファイル",
    "cad_codec_fidelity_observations": "コーデック忠実度観測レコード",
    "cad_power_sequencing_profiles": "電源シーケンスプロファイル",
    "cad_power_sequence_events": "電源シーケンスイベント",
    "cad_ups_transition_records": "UPS遷移記録",
    "cad_power_quality_measurements": "電源品質測定レコード",
    "cad_power_quality_qualifications": "電源品質適格レコード",
    "cad_emc_product_profiles": "EMC製品プロファイル",
    "cad_emc_symptom_records": "EMC症状記録",
    "cad_product_safety_profiles": "製品安全認証プロファイル",
    "cad_occupied_iaq_observations": "占有時IAQ観測レコード",
    "cad_occupied_iaq_qualifications": "占有時IAQ適格レコード",
    "cad_voc_emission_profiles": "VOC排出プロファイル",
    "cad_measurement_fixture_profiles": "測定治具プロファイル",
    "cad_fixture_scattering_observations": "治具散乱観測レコード",
    "cad_spectral_estimator_profiles": "スペクトル推定プロファイル",
    "cad_spectral_resolution_claims": "スペクトル分解能クレーム",
    "cad_external_evidence_sources": "外部証拠ソース",
    "cad_evidence_supersession_records": "証拠継承判定レコード",
    # REV59-DEPS: #729 権威依存/陳腐化グラフ
    "cad_dependency_edge_declarations": "権威依存エッジ宣言",
    "cad_dependency_change_events": "意味変更イベント",
    "cad_dependency_rule_profiles": "失効ルールプロファイル",
    "cad_staleness_assessments": "陳腐化評価",
    "cad_revalidation_plans": "再検証計画",
    # REV59-DEPS: #725 証拠アテステーション/時刻権威
    "cad_signed_manifests": "署名マニフェスト",
    "cad_manifest_attestations": "マニフェストアテステーション",
    "cad_attestation_verifications": "アテステーション検証",
    # REV59-DEPS: #718 プロジェクトアーカイブ/移行権威
    "cad_archive_snapshots": "アーカイブスナップショット",
    "cad_archive_verifications": "アーカイブ再読出し検証",
    "cad_migration_records": "移行レコード",
    "cad_migration_verifications": "移行検証",
    "cad_sound_strength_observations": "サウンドストレングスG観測レコード",
    "cad_sound_strength_qualifications": "サウンドストレングスG適格レコード",
    "cad_resonant_absorber_profiles": "共振吸音体プロファイル",
    "cad_resonant_performance_records": "共振吸音性能レコード",
    "cad_service_envelope_profiles": "保守エンベローププロファイル",
    "cad_service_access_observations": "保守アクセス観測レコード",
    "cad_numerical_repro_profiles": "数値再現性プロファイル",
    "cad_stochastic_realizations": "確率実現レコード",
    "cad_numerical_comparisons": "数値比較レコード",
    "cad_imaging_measurement_chains": "撮像計測チェーン",
    "cad_camera_calibrations": "カメラ校正プロファイル",
    "cad_camera_derived_observations": "カメラ導出観測レコード",
    "cad_wireless_av_links": "無線AVリンク",
    "cad_wireless_transport_observations": "無線伝送観測レコード",
    "cad_wireless_sync_evidence": "無線同期証拠レコード",
    "cad_ht_video_design_profiles": "HT映像設計プロファイル",
    "cad_ceb23_evaluations": "CEB23評価レコード",
    "cad_drawing_symbol_profiles": "図面シンボルプロファイル",
    "cad_device_symbol_mappings": "機器シンボル割当レコード",
    "cad_drawing_export_records": "図面エクスポートレコード",
    "cad_timed_text_profiles": "字幕プロファイル",
    "cad_caption_render_observations": "字幕表示観測レコード",
    "cad_finite_absorber_geometries": "有限吸音体幾何",
    "cad_finite_treatment_boundary_models": "有限吸音体境界モデル",
    "cad_precedence_profiles": "優先効果プロファイル",
    "cad_echo_risk_observations": "エコーリスク観測",
    "cad_reaction_to_fire_evidence": "防火試験証拠",
    "cad_finish_assembly_evidence": "仕上げ組立体安全証拠",    "cad_listening_experiment_plans": "聴取実験計画",
    "cad_listener_qualifications": "聴取者適格証拠",
    "cad_subjective_inference_records": "主観推定レコード",
    "cad_assistive_listening_paths": "補聴経路",
    "cad_als_qualifications": "補聴経路適格証拠",
    "cad_receiver_compatibility_evidence": "受信機互換証拠",
    "cad_dynamic_binaural_sessions": "動的バイノーラルセッション",
    "cad_pose_tracking_evidence": "姿勢追跡証拠",
    "cad_binaural_qualifications": "バイノーラル適格レコード",
    "cad_receiver_reference_points": "受信基準点",
    "cad_microphone_capsule_poses": "マイクカプセル位置",
    "cad_measurement_fixtures": "測定治具",
    "cad_fixture_scattering_evidence": "治具散乱証拠",
    "cad_discrete_reflection_events": "離散反射イベント",
    "cad_echo_diagnostics": "エコー診断",
    "cad_drr_method_profiles": "DRR手法プロファイル",
    "cad_drr_measurements": "DRR測定値",
    "cad_adaptive_identification_profiles": "適応同定プロファイル",
    "cad_arbitrary_stimulus_measurements": "任意刺激測定",
    "cad_adaptive_transfer_estimates": "適応伝達推定",
    "cad_adaptive_residual_evidence": "適応残差証拠",
    "cad_live_tf_sessions": "ライブTFセッション",
    "cad_dual_channel_tf_observations": "二ch伝達観測",
    "cad_coherence_observations": "コヒーレンス観測",
    "cad_reference_delay_tracks": "基準遅延追跡",
    "cad_microphone_array_geometries": "マイクアレイ形状",
    "cad_spatial_sampling_capabilities": "空間サンプリング能力",
    "cad_beamforming_transforms": "ビームフォーミング変換",
    "cad_impedance_measurement_profiles": "インピーダンス測定系",
    "cad_impedance_calibration_states": "インピーダンス校正状態",
    "cad_measured_load_evidence": "測定負荷証拠",
    "cad_thiele_small_derivations": "T-S導出",
    "cad_power_sequence_plans": "電源シーケンス計画",
    "cad_power_sequence_evidence": "電源シーケンス証拠",
    "cad_power_quality_observations": "電源品質観測",
    "cad_indoor_air_observations": "室内空気質観測",
    "cad_material_emission_evidence": "材料放出席証拠",
    "cad_product_safety_evidence": "製品安全証拠",
    "cad_emc_compliance_evidence": "EMC適合証拠",
    "cad_displayed_gradation_observations": "表示グラデーション観測",
    "cad_colour_volume_measurements": "色立体積測定",
    "cad_spatial_resolution_evidence": "空間解像証拠",
    "cad_low_luminance_capabilities": "低輝度計測能力",
    "cad_dynamic_contrast_measurements": "ダイナミックコントラスト測定",
    "cad_display_wall_boundaries": "表示壁境界",
    "cad_wall_acoustic_impacts": "壁音響影響",
    "cad_panning_continuity_evidence": "パンニング連続性証拠",
    "cad_subwoofer_localization_profiles": "サブウーファー定位プロファイル",
    "cad_groupdelay_audibility": "群遅延可聴性",
    "cad_headphone_coupling_evidence": "ヘッドホン結合証拠",
    "cad_structureborne_paths": "固体伝搬パス",
    "cad_spatial_remapping_evidence": "空間リマップ証拠",
    "cad_codec_fidelity_evidence": "コーデック忠実度証拠",
    "cad_fft_spectral_estimator_profiles": "スペクトル推定プロファイル",
    "cad_clock_domain_observations": "クロックドメイン観測",
    "cad_external_fact_claims": "外部事実クレーム",
    "cad_fact_conflict_resolutions": "事実矛盾解決",
    "cad_bom_estimates": "BOM・見積",
    "cad_cadence_delivery_evidence": "コーデンス配信証拠",
    "cad_reference_room_profiles": "参照室プロファイル",
    "cad_verification_requirements": "検証要件",
    "cad_verification_closures": "検証クローズ",

    # REV59-UNITS: #728 型付き物理量
    "cad_typed_quantities": "型付き物理量",
    "cad_quantity_operations": "物理量演算評価",
    # REV59-UNITS: #730 工学仮定台帳
    "cad_engineering_assumptions": "工学仮定",
    "cad_assumption_resolutions": "仮定解消記録",
    "cad_permissible_use_assessments": "許容用途評価",
    # REV59-UNITS: #720 知覚関連性/可聴性
    "cad_perceptual_model_profiles": "知覚モデルプロファイル",
    "cad_audibility_assessments": "可聴性評価",
    # REV59-UNITS: #719 残差診断仮説
    "cad_diagnostic_cases": "診断ケース",
    "cad_diagnostic_hypotheses": "診断仮説",
    "cad_diagnostic_tests": "診断試験",
    "cad_diagnostic_verdicts": "診断判定",

    # REV59-LOUDSPK authorities
    "cad_service_access_observations": "保守アクセス観測レコード",
    "cad_large_signal_models": "大信号トランスデューサモデル",
    "cad_excursion_capabilities": "振幅能力証拠",
    "cad_vent_flow_capabilities": "ポート・通気流動能力",
    "cad_mechanical_output_limits": "機械的出力限界評価",
    "cad_source_normalizations": "ソース規格化宣言",
    "cad_reference_drive_conditions": "基準ドライブ条件",
    "cad_absolute_output_anchors": "絶対出力アンカー",
    "cad_sustained_output_tests": "持続出力試験",
    "cad_thermal_compression_observations": "熱圧縮観測",
    "cad_recovery_profiles": "回復プロファイル",
    "cad_microphone_directional_profiles": "測定マイク指向性プロファイル",
    "cad_receiver_orientation_states": "受信器姿勢状態",
    "cad_microphone_incidence_applicability": "マイク入射角適用範囲",
    "cad_same_channel_arrays": "同チャネルスピーカーアレイ",
    "cad_array_reproduction_modes": "アレイ再生モード",
    "cad_array_qualifications": "アレイ音響検定",
    "cad_loudspeaker_front_layers": "ラウドスピーカ前面層",
    "cad_grille_transfer_evidence": "グリル透過証拠",
    "cad_front_layer_applicability": "前面層適用範囲",
    "cad_collaboration_actors": "コラボレーション参加者",
    "cad_revision_authorship": "リビジョン作成者記録",
    "cad_information_states": "情報状態記録",
    "cad_approval_records": "承認記録",
    "cad_review_decisions": "レビュー決定",
    "cad_sibling_divergences": "分岐評価",
    "cad_conflict_resolutions": "競合解決",
    "cad_branch_proposals": "ブランチ提案",
    "cad_proposal_promotions": "提案昇格",
    "cad_client_acceptances": "クライアント受け入れ",
    "cad_collaboration_events": "コラボレーション監査イベント",
    "cad_material_condition_states": "材質状態記録",
    "cad_material_durability_evidence": "材質耐久性証拠",
    "cad_material_evidence_applicability": "材質証拠適用性",
    "cad_material_reinspections": "材質再点検評価",
    "cad_ulf_acoustic_profiles": "超低域音響プロファイル",
    "cad_infrasonic_measurement_capabilities": "超低音測定チェーン能力",
    "cad_ulf_acoustic_observations": "超低域音響観測",
    "cad_ulf_system_qualifications": "超低域システム検定",
    "cad_external_noise_ingress_scenarios": "外部騒音侵入シナリオ",
    "cad_facade_transmission_models": "外装透過モデル",
    "cad_external_noise_ingress_measurements": "外部騒音侵入測定",
    "cad_indoor_noise_ingress_qualifications": "室内騒音侵入検定",
    "cad_fire_safety_evidence_profiles": "防火証拠プロファイル",
    "cad_material_reaction_to_fire_evidence": "材料燃焼反応証拠",
    "cad_installed_material_safety_requirements": "設置材料安全要求",
    "cad_fire_safety_approval_refs": "防火承認参照",
    "cad_accessible_media_profiles": "アクセシブルメディアプロファイル",
    "cad_caption_presentation_observations": "字幕提示観測",
    "cad_audio_description_playback_observations": "音声解説再生観測",
    "cad_accessible_playback_qualifications": "アクセシブル再生検定",
    "cad_deployment_capability_declarations": "デプロイ機能宣言",
    "cad_calibration_deployments": "校正デプロイ",
    "cad_deployment_effectiveness_reports": "デプロイ効果レポート",
    "cad_deployment_rollbacks": "デプロイロールバック",
    "cad_camilladsp_deployment_sessions": "CamillaDSPデプロイセッション",
    "cad_camilladsp_runtime_observations": "CamillaDSPランタイム観測",
    "cad_camilladsp_rollback_evidence": "CamillaDSPロールバック証跡",
    "cad_deployment_pipeline_records": "デプロイパイプラインレコード",
    "cad_deployment_operator_authorizations": "デプロイ操作者認可",
    "cad_assisted_instruction_manifests": "支援付き手順マニフェスト",
    "cad_assisted_deployment_attestations": "支援付きデプロイ証明",
    "cad_apo_install_records": "APOインストール記録",
    "cad_ux_acceptance_bundle_records": "UX受入証跡バンドル",
    "cad_adapter_sdk_descriptors": "アダプタSDK契約記述子",
    "cad_adapter_conformance_results": "アダプタ適合性評価結果",
    "cad_discovery_runs": "機器探索ラン",
    "cad_discovered_devices": "発見機器レコード",
    "cad_capability_probe_records": "機能プローブレコード",
    "cad_trusted_device_bindings": "信頼済み機器バインド",
    "cad_device_identity_drift_reports": "機器識別子ドリフトレポート",
    "cad_device_rebinding_decisions": "機器再バインド決定",
    "cad_realtime_measurement_sessions": "ライブ測定セッション",
    "cad_live_spectrum_observations": "ライブスペクトル観測",
    "cad_spl_time_histories": "SPL時系列履歴",
    "cad_captured_live_traces": "ライブ捕捉トレース",
    "cad_live_event_annotations": "ライブイベント注釈",
    "cad_validation_uncertainty_protocols": "不確かさ検証プロトコル",
    "cad_observable_uncertainty_evaluations": "観測量不確かさ評価",
    "cad_uncertainty_validation_verdicts": "不確かさ検証判定",
    "cad_material_input_authorities": "材料入力権限",
    "cad_source_directivity_authorities": "音源指向性入力権限",
    "cad_geometry_input_authorities": "幾何入力権限",
    "cad_pose_input_authorities": "配置入力権限",
    "cad_solver_input_envelopes": "ソルバー入力エンベロープ",
    "cad_claim_bound_records": "クレーム拘束レコード",
    "cad_protected_paths": "保護対象経路",
    "cad_transient_protection_plans": "過渡保護計画",
    "cad_spd_evidence": "SPDエビデンス",
    "cad_transient_protection_observations": "過渡保護状態観測",
    "cad_transient_protection_events": "過渡保護イベント",
    "cad_transient_protection_assessments": "過渡保護評価",
    "cad_device_power_mode_observations": "電源モード観測",
    "cad_networked_standby_evidence": "ネットワークスタンバイエビデンス",
    "cad_operational_energy_scenarios": "運用エネルギーシナリオ",
    "cad_energy_use_derivations": "エネルギー使用量導出",
    "cad_campaign_preregistrations": "キャンペーン事前登録",
    "cad_campaign_measurements": "キャンペーン測定",
    "cad_campaign_verdicts": "キャンペーン評価",
    "cad_production_readiness_decisions": "本番適格判定",
    "cad_recommendation_surface_decisions": "推奨サーフェス判定",
    "cad_delegated_provider_manifests": "委託プロバイダマニフェスト",
    "cad_provider_acquisitions": "プロバイダ取得レコード",
    "cad_file_deployments": "ファイルデプロイ証跡",
    "cad_commissioning_orch_runs": "コミッショニングラン",
    "cad_commissioning_orch_transitions": "コミッショニング段階遷移",
    "cad_commissioning_orch_authorizations": "コミッショニング操作承認",
    "cad_commissioning_orch_rollbacks": "コミッショニングロールバック",
    "cad_commissioning_orch_before_after": "コミッショニング前後比較",
    "cad_commissioning_orch_verdicts": "コミッショニング受け入れ判定",
    "cad_sweep_stimulus_definitions": "掃引刺激定義",
    "cad_sweep_acquisition_runs": "掃引測定実行レコード",
    "cad_sweep_acquisition_stage_events": "掃引測定ステージイベント",
    "cad_channel_verification_plans": "チャンネル検証計画",
    "cad_channel_excitation_results": "チャンネル励起測定結果",
    "cad_channel_operator_attestations": "チャンネル確認証言",
    "cad_channel_verification_verdicts": "チャンネル検証判定",
    "cad_campaign_execution_plans": "測定キャンペーン実行計画",
    "cad_campaign_execution_events": "測定キャンペーン実行イベント",
    "cad_campaign_execution_runs": "測定キャンペーン実行レコード",
    "cad_diagnostic_sessions": "診断セッション",
    "cad_diagnostic_session_transitions": "診断セッション遷移",
    "cad_diagnostic_session_hypotheses": "診断仮説エントリ",
    "cad_diagnostic_test_plans": "診断試験計画",
    "cad_diagnostic_observations": "診断観測レコード",
    "cad_diagnostic_evidence_updates": "診断証拠更新",
    "cad_diagnostic_resolutions": "診断解決レコード",
    "cad_diagnostic_authorizations": "診断操作承認",
    "cad_headless_run_records": "ヘッドレス実行レコード",
    "cad_interop_fixture_runs": "相互運用フィクスチャ判定レコード",
    "cad_interop_corpus_runs": "相互運用コーパス実行レコード",
    "cad_credential_references": "資格情報参照",
    "cad_credential_lifecycle_events": "資格情報ライフサイクルイベント",
    "cad_update_packages": "アップデートパッケージ",
    "cad_update_sessions": "アップデートセッション",
    "cad_update_transitions": "アップデート遷移",
    "cad_update_preflight_reports": "アップデート事前検証レポート",
    "cad_update_restore_points": "アップデート復元ポイント",
    "cad_update_health_reports": "アップデート健全性レポート",
    "cad_update_authorizations": "アップデート操作承認",
    "cad_update_outcomes": "アップデート結果",
    "cad_reference_theater_runs": "リファレンスシアター検証",
    "cad_decision_briefs": "決定ブリーフ",
    # REV72: #964 変更差分による証拠失効権威
    "cad_change_diff_records": "変更差分レコード",
    "cad_revalidation_queues": "再検証キュー",
    "cad_revalidation_queue_runs": "再検証キュー実行レコード",
    "cad_gate_operator_plans": "ゲートオペレータ計画",
    "cad_gate_acceptance_runs": "ゲート受入実行レコード",
    # REV73: #1011 ケーブル経路ジオメトリ権威
    "cad_cable_run_geometries": "ケーブル経路ジオメトリ",
    "cad_remeasure_queues": "再測定キュー",
    "cad_remeasure_queue_events": "再測定キューイベント",
}


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024.0
    return f"{value:,} B"


#: Operator-facing rendering of each lifecycle blocker kind — the
#: blocker's ``detail`` is diagnostic English; the dialog shows the kind's
#: localized form with ``count`` supplying the numbers.
_DELETION_BLOCKER_LINES = {
    'project_not_archived': (
        'プロジェクトがまだアクティブです — 先にアーカイブしてください'
    ),
    'active_descendants': (
        'このプロジェクトからクローンされたプロジェクトが {count} 件'
        'あります — 先にそれらを削除またはアーカイブしてください'
    ),
    'pending_capture_missions': (
        'このプロジェクトを対象とするキャプチャミッションが {count} 件'
        '残っています — 先に中止または退役させてください'
    ),
    'pending_inbox_items': (
        'このプロジェクトのキャプチャ受信ボックスに未処理の項目が {count} 件'
        'あります'
    ),
    'unknown_project': 'プロジェクトが見つかりません',
}


def _deletion_blocker_line(blocker: DeletionBlocker) -> str:
    template = _DELETION_BLOCKER_LINES.get(blocker.kind)
    if template is None:
        # A future kind must not render silently wrong copy — keep the
        # authored detail rather than inventing a reason.
        return blocker.detail
    return template.format(count=blocker.count)


def _deletion_plan_lines(plan: ProjectDeletionPlan) -> list[str]:
    """Render the planner's own numbers verbatim — consequence preview."""
    lines = [
        f"対象: {plan.display_name}",
        f"削除対象: 合計 {plan.total_rows} 行"
        f"（約 {_format_bytes(plan.estimated_bytes)}）",
    ]
    for count in plan.authorities:
        lines.append(
            f"・{_LIFECYCLE_TABLE_LABELS.get(count.table, count.table)}: "
            f"{count.row_count} 件（約 {_format_bytes(count.estimated_bytes)}）"
        )
    lines.append(
        f"共有アセット（保持）: {plan.assets.shared_asset_count} 件 / "
        f"{_format_bytes(plan.assets.shared_asset_bytes)}"
    )
    lines.append(
        f"プロジェクト専用アセット（削除後GC対象）: "
        f"{plan.assets.local_asset_count} 件 / "
        f"{_format_bytes(plan.assets.local_asset_bytes)}"
    )
    if plan.pending_mission_count:
        lines.append(
            f"未処理のキャプチャミッション: {plan.pending_mission_count} 件"
        )
    if plan.pending_inbox_item_count:
        lines.append(
            f"未処理の受信ボックス項目: {plan.pending_inbox_item_count} 件"
        )
    return lines


#: #986 shared project-list sort/filter vocabulary — used identically by
#: the Projects page table and the menu switch picker so an operator sees
#: the same ordering and identity semantics in both places.
PROJECT_SORT_RECENT = 'recent'
PROJECT_SORT_CREATED = 'created'
PROJECT_SORT_NAME = 'name'
PROJECT_FILTER_ACTIVE = 'active'
PROJECT_FILTER_ARCHIVED = 'archived'
PROJECT_FILTER_ALL = 'all'


def filter_project_entries(
    entries: tuple,
    *,
    text: str = '',
    sort: str = PROJECT_SORT_RECENT,
    state: str = PROJECT_FILTER_ALL,
) -> tuple:
    """Search/state-filter/sort project entries without touching identity.

    Duck-typed over the fields both entry models share (``display_name``,
    ``archived``, ``created_at_utc``, ``last_opened_at_utc``); the returned
    objects are the same instances — ``project_id`` authority is never
    resolved by display text anywhere downstream.
    """

    needle = text.strip().casefold()
    items = []
    for entry in entries:
        if state == PROJECT_FILTER_ACTIVE and entry.archived:
            continue
        if state == PROJECT_FILTER_ARCHIVED and not entry.archived:
            continue
        if needle and needle not in (entry.display_name or '').casefold():
            continue
        items.append(entry)
    if sort == PROJECT_SORT_NAME:
        items.sort(
            key=lambda e: ((e.display_name or '').casefold(), e.project_id)
        )
    elif sort == PROJECT_SORT_CREATED:
        items.sort(
            key=lambda e: (e.created_at_utc or '', e.project_id),
            reverse=True,
        )
    else:
        # 最近使った順 — opened projects first (never-opened last), then
        # created date as the deterministic tiebreak.
        items.sort(
            key=lambda e: (
                e.last_opened_at_utc or '',
                e.created_at_utc or '',
                e.project_id,
            ),
            reverse=True,
        )
    return tuple(items)


class ProjectLibraryPage(QWidget):
    """Project library: open/switch, archive/restore, delete documents.

    Deletion follows the lifecycle authority's confirm contract: a
    read-only ``plan_project_deletion`` preview listing every consequence
    (per-authority counts, shared-vs-local assets, hard blockers), an
    explicit confirm, then archive-if-needed + ``delete_project`` pinned
    to the plan the user approved. The currently-open project refuses
    lifecycle changes — the shell is standing on it.
    """

    project_open_requested = Signal(str)
    commission_requested = Signal()

    def __init__(
        self,
        service: ProjectLibraryService,
        current_document_id: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._current_document_id = current_document_id
        self._entries: dict[str, ProjectEntry] = {}
        layout = _page_layout(
            self,
            "プロジェクト",
            "保存済みのプロジェクトです。開くとそのプロジェクトに切り替わります。"
            "アーカイブ済みのプロジェクトは開けず、削除は確認のうえ実行されます。",
        )

        controls = QHBoxLayout()
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("projectLibrarySearch")
        self.search_edit.setPlaceholderText("プロジェクト名で検索…")
        self.search_edit.setAccessibleName("プロジェクト名で検索")
        self.search_edit.setToolTip("表示名の部分一致で一覧を絞り込みます")
        self.search_edit.textChanged.connect(self.refresh)
        controls.addWidget(self.search_edit, 1)
        self.sort_combo = QComboBox(self)
        self.sort_combo.setObjectName("projectLibrarySort")
        self.sort_combo.setAccessibleName("プロジェクト一覧の並べ替え")
        for _label, _key in (
            ("最近使った順", PROJECT_SORT_RECENT),
            ("作成日時", PROJECT_SORT_CREATED),
            ("名前", PROJECT_SORT_NAME),
        ):
            self.sort_combo.addItem(_label, _key)
        self.sort_combo.setToolTip("一覧の並べ替え方法を選びます")
        self.sort_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        controls.addWidget(self.sort_combo)
        self.state_combo = QComboBox(self)
        self.state_combo.setObjectName("projectLibraryState")
        self.state_combo.setAccessibleName("プロジェクトの状態絞り込み")
        for _label, _key in (
            ("全件", PROJECT_FILTER_ALL),
            ("作業中", PROJECT_FILTER_ACTIVE),
            ("アーカイブ済み", PROJECT_FILTER_ARCHIVED),
        ):
            self.state_combo.addItem(_label, _key)
        self.state_combo.setToolTip("作業中 / アーカイブ済みで絞り込みます")
        self.state_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        controls.addWidget(self.state_combo)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 7)
        self.table.setAccessibleName("プロジェクト一覧")
        self.table.setToolTip(
            "保存済みプロジェクトの一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
        )
        # Columns 0-4 keep the pre-#986 layout (state stays at index 4);
        # the new columns append at the tail.
        self.table.setHorizontalHeaderLabels(
            ("プロジェクト", "作成日時", "リビジョン数", "現在", "状態", "最終アクセス", "ID")
        )
        for _col, _tip in enumerate((
            "プロジェクトの表示名",
            "プロジェクトを作成した日時",
            "保存されている版（リビジョン）の数",
            "現在開いているプロジェクトには ● が付きます",
            "アクティブ / アーカイブ済み の状態",
            "最後に開いた日時（未オープンは空欄）",
            "プロジェクトIDの先頭（同名案件の区別用）",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in range(1, self.table.columnCount()):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(
            "まだプロジェクトはありません。"
            "「新規プロジェクト…」から作成できます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.open_button = QPushButton("開く")
        # Tooltip stays live while disabled so the gating is discoverable.
        self.open_button.setToolTip("一覧からプロジェクトを選択すると開けます")
        self.open_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.open_button.clicked.connect(self._open_selected)
        actions.addWidget(self.open_button)
        self.archive_button = QPushButton("アーカイブ")
        self.archive_button.setToolTip(
            "アクティブなプロジェクトをアーカイブします（データは保持されます）"
        )
        self.archive_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.archive_button.clicked.connect(
            lambda: self._set_archived_selected(True)
        )
        actions.addWidget(self.archive_button)
        self.restore_button = QPushButton("アーカイブ解除")
        self.restore_button.setToolTip(
            "アーカイブ済みのプロジェクトをアクティブに戻します"
        )
        self.restore_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.restore_button.clicked.connect(
            lambda: self._set_archived_selected(False)
        )
        actions.addWidget(self.restore_button)
        self.delete_button = QPushButton("削除…")
        self.delete_button.setToolTip(
            "削除内容の確認後、プロジェクトを完全に削除します"
        )
        self.delete_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.new_button = QPushButton("新規プロジェクト…")
        self.new_button.setToolTip("新しいプロジェクトの作成を開始します（作成ウィザードが開きます）")
        self.new_button.setWhatsThis("新しいプロジェクトの作成を開始します（作成ウィザードが開きます）")
        self.new_button.clicked.connect(lambda: self.commission_requested.emit())
        layout.addWidget(self.new_button)

        self.selection_status = QLabel("", self)
        self.selection_status.setObjectName("projectSelectionStatus")
        self.selection_status.setWordWrap(True)
        set_typography_role(self.selection_status, TypographyRole.SECONDARY)
        layout.addWidget(self.selection_status)
        self.refresh()

    def refresh(self) -> None:
        selected = self._selected_project_id()
        all_entries = self.service.list_projects()
        self._entries = {entry.project_id: entry for entry in all_entries}
        entries = filter_project_entries(
            all_entries,
            text=self.search_edit.text(),
            sort=self.sort_combo.currentData(),
            state=self.state_combo.currentData(),
        )
        current = self._current_document_id()
        self.table.setRowCount(0)
        self.empty_label.setVisible(not all_entries)
        restore_row: int | None = None
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                entry.display_name,
                entry.created_at_utc,
                str(entry.revision_count),
                "●" if entry.document_id == current else "",
                "アーカイブ済み" if entry.archived else "",
                entry.last_opened_at_utc or "",
                entry.project_id[:8],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.project_id)
                self.table.setItem(row, column, item)
            if selected is not None and entry.project_id == selected:
                restore_row = row
        if restore_row is not None:
            self.table.selectRow(restore_row)
        self._sync_buttons()

    def _selected_entry(self) -> ProjectEntry | None:
        project_id = self._selected_project_id()
        if project_id is None:
            return None
        return self._entries.get(project_id)

    def _sync_buttons(self) -> None:
        entry = self._selected_entry()
        current = self._current_document_id()
        is_current = entry is not None and entry.document_id == current
        self.open_button.setEnabled(
            entry is not None and not entry.archived
        )
        self.archive_button.setEnabled(
            entry is not None and not entry.archived and not is_current
        )
        self.restore_button.setEnabled(
            entry is not None and entry.archived
        )
        self.delete_button.setEnabled(entry is not None and not is_current)
        # #986: name the exact reason the selected row cannot be opened —
        # never silently redirect the operator to a different project.
        if entry is None:
            self.selection_status.setText("")
        elif is_current:
            self.selection_status.setText(
                "このプロジェクトは現在開いています。"
            )
        elif entry.archived:
            self.selection_status.setText(
                "アーカイブ済みのため開けません（アーカイブ解除は可能です）。"
            )
        elif entry.head_revision_id is None:
            self.selection_status.setText(
                "開けるリビジョンがありません。"
            )
        else:
            self.selection_status.setText("")
        if is_current:
            tip = "現在開いているプロジェクトは変更できません"
            self.archive_button.setToolTip(tip)
            self.delete_button.setToolTip(tip)
        else:
            self.archive_button.setToolTip(
                "このプロジェクトはすでにアーカイブ済みです"
                if entry is not None and entry.archived
                else "アクティブなプロジェクトをアーカイブします"
                "（データは保持されます）"
            )
            self.delete_button.setToolTip(
                "削除内容の確認後、プロジェクトを完全に削除します"
            )

    def _selected_project_id(self) -> str | None:
        items = self.table.selectedItems()
        for item in items:
            value = item.data(Qt.ItemDataRole.UserRole)
            if value:
                return str(value)
        return None

    def _open_selected(self) -> None:
        # Open routes through the stable canonical project_id (#919).
        project_id = self._selected_project_id()
        if project_id:
            self.project_open_requested.emit(project_id)

    def _set_archived_selected(self, archived: bool) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        try:
            self.service.set_archived(entry.project_id, archived)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: archive toggle — expected store failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(
                self,
                "アーカイブ" if archived else "アーカイブ解除",
                exc,
            )
            return
        self.refresh()

    def _delete_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None or entry.document_id == self._current_document_id():
            return
        try:
            plan = self.service.plan_project_deletion(entry.project_id)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: deletion plan probe — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "削除内容を確認できませんでした", exc)
            return
        non_archive_blockers = [
            blocker
            for blocker in plan.hard_blockers
            if blocker.kind != "project_not_archived"
        ]
        if non_archive_blockers:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("プロジェクトを削除できません")
            box.setText(f"{plan.display_name} は削除をブロックされています。")
            box.setInformativeText(
                "\n".join(
                    f"・{_deletion_blocker_line(blocker)}"
                    for blocker in non_archive_blockers
                )
            )
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            return

        # The persisted policy decides whether a pre-destructive safety
        # generation is offered — `load_policy` already falls back to
        # defaults on unreadable state, so this can never raise.
        safety_scheduler = AutomaticBackupScheduler(self.service.path.parent)
        safety_backup_offered = safety_scheduler.policy.enabled

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("プロジェクトを削除")
        box.setText(
            f"「{plan.display_name}」を完全に削除します。"
            "この操作は取り消せません（削除記録はトゥームストーンとして残ります）。"
        )
        detail_lines = _deletion_plan_lines(plan)
        if not entry.archived:
            detail_lines.append(
                "このプロジェクトはまだアクティブです。削除の前に"
                "自動でアーカイブします。"
            )
        if safety_backup_offered:
            detail_lines.append(
                "削除の前に現在のデータの安全バックアップを作成します。"
            )
        detail_lines.append("アセットファイル自体は削除されません。")
        box.setInformativeText("\n".join(detail_lines))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        safety_backup_created = False
        try:
            if not entry.archived:
                self.service.set_archived(entry.project_id, True)
            # Re-plan so the approved fingerprint matches the world the
            # delete actually validates (archiving lifts the blocker).
            plan = self.service.plan_project_deletion(entry.project_id)
            if not plan.executable:
                raise ProjectDeletionBlockedError(plan)
            if safety_backup_offered:
                # The delete is atomic, but a validated pre-destructive
                # generation is the only way back to the project's
                # content — the designed-for boundary of the 'pre_destructive'
                # trigger. Best-effort: a failed safety net must not strand
                # the deletion itself, so warn and continue.
                try:
                    safety_backup_created = (
                        safety_scheduler.run_due('pre_destructive')
                        is not None
                    )
                except EXPECTED_OPERATION_ERRORS as backup_exc:  # error-boundary: best-effort safety backup — expected failures warn and the delete continues; unexpected errors propagate
                    warn_user(
                        self,
                        "削除前の安全バックアップを作成できませんでした",
                        backup_exc,
                        effect="削除はこのまま続行します。",
                    )
            tombstone = self.service.delete_project(
                entry.project_id, expected_plan=plan
            )
        except ProjectDeletionStaleError:
            self.refresh()
            QMessageBox.information(
                self,
                "削除できませんでした",
                "プレビュー後にプロジェクトが変更されました。"
                "もう一度削除内容を確認してください。",
            )
            return
        except ProjectDeletionBlockedError as exc:
            self.refresh()
            QMessageBox.warning(
                self,
                "削除をブロックしました",
                "\n".join(
                    f"・{_deletion_blocker_line(blocker)}"
                    for blocker in exc.plan.hard_blockers
                ),
            )
            return
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: project delete — expected store faults surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "削除できませんでした", exc)
            self.refresh()
            return
        self.refresh()
        success_detail = (
            f"{tombstone.display_name}: {tombstone.removed_rows} 行を削除しました。"
        )
        if safety_backup_created:
            success_detail += "削除前の安全バックアップを作成しました。"
        QMessageBox.information(
            self,
            "プロジェクトを削除しました",
            success_detail,
        )


_INBOX_LINEAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1

_INBOX_GATE_LABELS = {
    "bundle_validation": "バンドル検証",
    "dependency_state": "依存関係",
    "alignment_state": "整列状態",
    "evidence_conflict_state": "証拠競合",
}
_INBOX_GATE_DETAIL_FIELDS = {
    "bundle_validation": "validation_detail",
    "dependency_state": "dependency_detail",
    "alignment_state": "alignment_detail",
    "evidence_conflict_state": "evidence_conflict_detail",
}
_INBOX_DISPOSITION_LABELS = {
    "pending": "保留中",
    "deferred": "延期",
    "partially_promoted": "一部昇格",
    "promoted": "昇格済",
    "rejected": "却下",
    "superseded": "置換済",
}
_INBOX_PROMOTABILITY_LABELS = {
    "promotable": "昇格可能",
    "partially_promotable": "一部昇格可能",
    "blocked": "昇格不可",
    "complete": "昇格完了",
}
_INBOX_OUTCOME_LABELS = {
    "promoted": "昇格成功",
    "blocked": "ブロック",
}
_INBOX_CLASSIFICATION_LABELS = {
    "validation_rejected": "検証で却下",
    "exact_duplicate": "完全一致の重複",
    "identity_digest_conflict": "同一性ダイジェストの競合",
    "revision_variant": "リビジョンバリアント",
    "fills_missing_predecessor": "欠落した先行リビジョンの補完",
    "extends_known_head": "既知の先端の延長",
    "continues_branch": "ブランチの継続",
    "parallel_branch_head": "並行ブランチの先端",
    "new_series": "新しい系列",
}
_INBOX_GATE_STATE_LABELS = {
    "validated": "検証済",
    "rejected": "却下",
    "not_evaluated": "未評価",
    "satisfied": "充足",
    "unresolved": "未解決",
    "not_required": "不要",
    "pending": "保留中",
    "resolved": "解決済",
    "blocked": "ブロック",
    "none": "なし",
    "open": "未解決",
}
_INBOX_AUTHORITY_KIND_LABELS = {
    "raw_visual_evidence": "生の視覚証拠",
    "semantic_geometry": "意味ジオメトリ",
    "annotations": "注釈",
    "measurements": "測定",
    "as_built_observations": "竣工観測",
    "connected_space": "接続空間",
    "reference_targets": "参照ターゲット",
    "supplemental_authority": "補足権威",
}
_INBOX_UNASSIGNED_SCOPE = "capture-inbox-unassigned"


def _inbox_scope_label(scope: str) -> str:
    return "（未割り当て）" if scope == _INBOX_UNASSIGNED_SCOPE else scope


def _classification_label(classification: str) -> str:
    return _INBOX_CLASSIFICATION_LABELS.get(classification, classification)


# -- mass-arrival triage (#988) -------------------------------------------
#
# Review queues are derived from each row's own facets — never a second
# inspect() per item — so a 10/100/1000-item inbox refilters instantly.
# ``promotable`` is the *candidate* queue: scope assigned and none of the
# gates ``CaptureInboxRepository._check_promotable`` vets is failing. The
# authoritative promotability verdict still comes from inspection at
# selection/detail time.
_INBOX_QUEUE_LABELS = {
    "pending": "保留中",
    "promotable": "昇格可能",
    "blocked": "ブロック",
    "deferred": "延期",
    "rejected": "却下",
    "processed": "処理済み",
}
# Queues still awaiting an operator decision — the 要レビュー count, the
# default filter, and the rows 次の未処理を表示 walks.
_INBOX_ACTIONABLE_QUEUES = frozenset({"pending", "promotable", "blocked"})
# Display order when sorting by queue: ready-to-promote first, then items
# needing input, then faulted, then parked/finished.
_INBOX_QUEUE_ORDER = (
    "promotable",
    "pending",
    "blocked",
    "deferred",
    "rejected",
    "processed",
)
_INBOX_SORT_OPTIONS = (
    ("arrival_asc", "到着が早い順"),
    ("arrival_desc", "到着が新しい順"),
    ("queue", "状態優先順"),
    ("scope", "スコープ"),
    ("series", "シリーズ"),
    ("classification", "分類"),
)
_INBOX_GROUP_OPTIONS = (
    ("none", "なし"),
    ("queue", "状態"),
    ("scope", "スコープ"),
    ("series", "シリーズ"),
    ("classification", "分類"),
)
# Secondary actions fold once the row's required width exceeds what the
# page can give it (high-DPI logical widths) — measured live, never a
# fixed threshold, so the trigger stays reachable at every window size.


def _inbox_queue_state(item) -> str:
    """Review queue for one staged delivery (#988).

    The blocked queue mirrors ``_check_promotable``'s veto facets exactly
    (validation, identity conflict, dependencies, evidence conflict,
    alignment) so the label names the gate that would actually fail —
    never a guess. ``promotable`` items are promotion-review candidates;
    the promote path still re-verifies at execution time.
    """

    disposition = getattr(item, "disposition", "pending")
    if disposition == "deferred":
        return "deferred"
    if disposition == "rejected":
        return "rejected"
    if disposition in ("promoted", "superseded"):
        return "processed"
    if (
        getattr(item, "bundle_validation", "validated") != "validated"
        or getattr(item, "primary_classification", "")
        == "identity_digest_conflict"
        or getattr(item, "dependency_state", "not_evaluated") == "unresolved"
        or getattr(item, "evidence_conflict_state", "none") == "open"
        or getattr(item, "alignment_state", "not_required") == "blocked"
    ):
        return "blocked"
    if not capture_inbox_item_project_id(item):
        return "pending"
    return "promotable"


def _inbox_search_text(item) -> str:
    """Lowercased haystack the inbox search box matches against."""

    parts = [
        _inbox_scope_label(getattr(item, "scope", "")),
        getattr(item, "scope", ""),
        getattr(item, "capture_series_id", ""),
        getattr(item, "capture_revision_id", ""),
        _classification_label(getattr(item, "primary_classification", "")),
        getattr(item, "primary_classification", ""),
        *(
            _INBOX_CLASSIFICATION_LABELS.get(flag, flag)
            for flag in getattr(item, "classification_flags", ())
        ),
        _INBOX_DISPOSITION_LABELS.get(
            getattr(item, "disposition", ""), getattr(item, "disposition", "")
        ),
        getattr(item, "arrival_source", ""),
        getattr(item, "source_detail", ""),
        getattr(item, "inbox_item_id", ""),
        getattr(item, "lineage_digest", ""),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _inbox_classification_rank(item) -> int:
    try:
        return list(_INBOX_CLASSIFICATION_LABELS).index(
            getattr(item, "primary_classification", "")
        )
    except ValueError:
        return len(_INBOX_CLASSIFICATION_LABELS)


def _inbox_sort_key(item, sort_key: str):
    queue_rank = _INBOX_QUEUE_ORDER.index(_inbox_queue_state(item))
    arrived = getattr(item, "first_arrived_at_utc", "")
    digest = getattr(item, "lineage_digest", "")
    if sort_key == "queue":
        return (queue_rank, arrived, digest)
    if sort_key == "scope":
        return (
            _inbox_scope_label(getattr(item, "scope", "")).lower(),
            arrived,
            digest,
        )
    if sort_key == "series":
        return (getattr(item, "capture_series_id", ""), arrived, digest)
    if sort_key == "classification":
        return (_inbox_classification_rank(item), arrived, digest)
    return (arrived, digest)


def _inbox_group_sort(item, group_key: str):
    """Sortable group key — groups order by the module's own
    vocabularies (queue rank, classification priority), never by raw
    string. 未割り当て pins first: it is the queue's landing zone."""
    if group_key == "queue":
        return (_INBOX_QUEUE_ORDER.index(_inbox_queue_state(item)),)
    if group_key == "scope":
        scope = getattr(item, "scope", "")
        return (
            0 if scope == _INBOX_UNASSIGNED_SCOPE else 1,
            _inbox_scope_label(scope).lower(),
        )
    if group_key == "series":
        return (getattr(item, "capture_series_id", ""),)
    if group_key == "classification":
        return (_inbox_classification_rank(item),)
    return ()


def _inbox_group_title(item, group_key: str) -> str:
    if group_key == "queue":
        return _INBOX_QUEUE_LABELS[_inbox_queue_state(item)]
    if group_key == "scope":
        return f"スコープ: {_inbox_scope_label(getattr(item, 'scope', ''))}"
    if group_key == "series":
        return f"シリーズ: {getattr(item, 'capture_series_id', '')}"
    if group_key == "classification":
        return (
            "分類: "
            + _classification_label(
                getattr(item, "primary_classification", "")
            )
        )
    return ""


def _inbox_state_cell(item) -> str:
    """状態 column: queue vocabulary, with the stored disposition kept
    in parentheses when it carries extra information (一部昇格 etc.)."""
    queue_label = _INBOX_QUEUE_LABELS[_inbox_queue_state(item)]
    disposition = _INBOX_DISPOSITION_LABELS.get(
        getattr(item, "disposition", ""), getattr(item, "disposition", "")
    )
    if queue_label == disposition:
        return queue_label
    return f"{queue_label}（{disposition}）"


class CaptureInboxPage(QWidget):
    """Capture Inbox: staged deliveries awaiting review (#770).

    The listing stays compact; selecting a row opens the exact item and
    project context (identity, scope, gate facets, promotability) in a
    detail pane so review, defer/reject, and scope assignment never operate
    on a bare list row.
    """

    def __init__(
        self,
        list_items: Callable[[], tuple],
        on_navigate: Callable[[WorkspaceDeepLink], bool],
        *,
        inspect_item: Callable[[str], object] | None = None,
        defer_item: Callable[[str, str], object] | None = None,
        reject_item: Callable[[str, str], object] | None = None,
        resume_item: Callable[[str], object] | None = None,
        promote_item: Callable[[str, str], object] | None = None,
        list_projects: Callable[[], tuple] | None = None,
        assign_scope: Callable[[str, str], object] | None = None,
        list_contributions: Callable[[], tuple] | None = None,
        reconcile_contribution: Callable[[object], tuple] | None = None,
        resolve_return_evidence: Callable[[object], object] | None = None,
        rebase_context: Callable[[object], object] | None = None,
        rebase_record: Callable | None = None,
        apply_record: Callable | None = None,
        discard_record: Callable | None = None,
        list_missions: Callable[[], tuple] | None = None,
        list_mission_pairings: Callable[[], tuple] | None = None,
        issue_mission: Callable | None = None,
        export_mission: Callable | None = None,
        list_watch_failures: Callable[[], tuple] | None = None,
        verify_watch_failure: Callable[[str], object] | None = None,
        retry_watch_failure: Callable[[str], object] | None = None,
        import_watch_failure: Callable[[str], object] | None = None,
        diagnose_watch_failure: Callable[[str], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_items = list_items
        self._on_navigate = on_navigate
        self._inspect_item = inspect_item
        self._defer_item = defer_item
        self._reject_item = reject_item
        self._resume_item = resume_item
        self._promote_item = promote_item
        self._list_projects = list_projects
        self._assign_scope = assign_scope
        self._list_contributions = list_contributions
        self._reconcile_contribution = reconcile_contribution
        self._resolve_return_evidence = resolve_return_evidence
        self._rebase_context = rebase_context
        self._rebase_record = rebase_record
        self._apply_record = apply_record
        self._discard_record = discard_record
        self._list_missions = list_missions
        self._list_mission_pairings = list_mission_pairings
        self._issue_mission = issue_mission
        self._export_mission = export_mission
        self._list_watch_failures = list_watch_failures
        self._verify_watch_failure = verify_watch_failure
        self._retry_watch_failure = retry_watch_failure
        self._import_watch_failure = import_watch_failure
        self._diagnose_watch_failure = diagnose_watch_failure
        self._watch_failures: tuple = ()
        self._missions: tuple = ()
        self._active_rebase_context = None
        self._selected_contribution = None
        self._contributions: tuple = ()
        self._last_inspection = None
        # Mass-arrival triage state (#988): ``_items`` is the last
        # ``list_items`` snapshot; ``_displayed_items`` mirrors the
        # delivery table row-for-row (``None`` marks group header rows).
        self._items: tuple = ()
        self._displayed_items: list = []
        self._collapsed_actions: bool | None = None
        self._secondary_widgets: list[QWidget] = []
        layout = _page_layout(
            self,
            "取り込み",
            "取得済みのキャプチャ配送です。項目を選ぶと内容と判断材料を確認できます。",
        )
        delivery_panel = QWidget()
        delivery_layout = QVBoxLayout(delivery_panel)
        delivery_layout.setContentsMargins(0, 0, 0, 0)
        delivery_layout.addLayout(self._build_inbox_filter_row())
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setAccessibleName("取り込み一覧")
        self.table.setToolTip(
            "受け取ったキャプチャ配送の一覧です。行を選ぶと詳細と操作が下に表示されます。"
        )
        self.table.setHorizontalHeaderLabels(
            ("スコープ", "シリーズ", "分類", "状態", "到着数")
        )
        for _col, _tip in enumerate((
            "届いたデータの対象スコープ（プロジェクトまたは受信機）",
            "同じ測定系列に属するグループ名",
            "内容の種類（周波数応答・写真・メモなど）",
            "取り込みの処理状態（保留・延期・却下など）",
            "その系列で届いた項目の数",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.itemSelectionChanged.connect(self._sync_detail)
        splitter.addWidget(self.table)
        delivery_layout.addWidget(splitter, 1)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        self.detail = QLabel("一覧から項目を選択すると詳細を表示します。")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        detail_layout.addWidget(self.detail, 1)
        actions = QHBoxLayout()
        self._actions_layout = actions
        # Primary triage ops stay first and never collapse (#988).
        self.next_unprocessed_button = QPushButton("次の未処理を表示")
        self.next_unprocessed_button.setAccessibleName("次の未処理を表示")
        self.next_unprocessed_button.setToolTip(
            "保留中・昇格可能・ブロックのうち、次の項目を選択します"
            "（延期・却下・処理済みとグループ見出しは飛ばします）"
        )
        self.next_unprocessed_button.setWhatsThis(
            "保留中・昇格可能・ブロックのうち、次の項目を選択します"
            "（延期・却下・処理済みとグループ見出しは飛ばします）"
        )
        self.next_unprocessed_button.clicked.connect(
            self._show_next_unprocessed
        )
        actions.addWidget(self.next_unprocessed_button)
        self.detail_button = QPushButton("詳細を確認")
        self.detail_button.setAccessibleName("詳細を確認")
        self.detail_button.setToolTip(
            "選択項目の詳細を表示します。未選択なら次の未処理項目を選びます。"
        )
        self.detail_button.setWhatsThis(
            "選択項目の詳細を表示します。未選択なら次の未処理項目を選びます。"
        )
        self.detail_button.clicked.connect(self._confirm_detail)
        actions.addWidget(self.detail_button)
        actions.addSpacing(8)
        # Secondary actions fold into the overflow menu when the row
        # cannot fit (high-DPI / narrow windows) so mis-taps cannot
        # happen — the collapsed menu runs the exact same slots.
        self.actions_menu = QMenu(self)
        self._menu_actions = {}

        def _menu_action(key: str, label: str, slot) -> None:
            action = self.actions_menu.addAction(label)
            action.triggered.connect(slot)
            self._menu_actions[key] = action

        self.defer_button = QPushButton("延期…")
        self.defer_button.setToolTip("選択項目の判断をあとに回します（一覧から一時的に外れます）")
        self.defer_button.setWhatsThis("選択項目の判断をあとに回します（一覧から一時的に外れます）")
        self.defer_button.clicked.connect(lambda: self._dispose("defer"))
        actions.addWidget(self.defer_button)
        self._secondary_widgets.append(self.defer_button)
        _menu_action("defer", "延期…", lambda: self._dispose("defer"))
        self.reject_button = QPushButton("却下…")
        self.reject_button.setToolTip("選択項目を取り込まずに破棄します（理由を確認してから実行されます）")
        self.reject_button.setWhatsThis("選択項目を取り込まずに破棄します（理由を確認してから実行されます）")
        self.reject_button.clicked.connect(lambda: self._dispose("reject"))
        actions.addWidget(self.reject_button)
        self._secondary_widgets.append(self.reject_button)
        _menu_action("reject", "却下…", lambda: self._dispose("reject"))
        self.resume_button = QPushButton("再開")
        self.resume_button.setToolTip("延期・却下した項目を再度「保留」に戻して検討対象にします")
        self.resume_button.setWhatsThis("延期・却下した項目を再度「保留」に戻して検討対象にします")
        self.resume_button.clicked.connect(lambda: self._dispose("resume"))
        actions.addWidget(self.resume_button)
        self._secondary_widgets.append(self.resume_button)
        _menu_action("resume", "再開", lambda: self._dispose("resume"))
        self.promote_button = QPushButton("昇格…")
        self.promote_button.setToolTip("取り込み可能な権威レコード（注釈エンティティ）をプロジェクトのシーンに反映します")
        self.promote_button.setWhatsThis("取り込み可能な権威レコード（注釈エンティティ）をプロジェクトのシーンに反映します")
        self.promote_button.clicked.connect(self._promote)
        actions.addWidget(self.promote_button)
        self._secondary_widgets.append(self.promote_button)
        _menu_action("promote", "昇格…", self._promote)
        self.scope_combo = QComboBox()
        self.scope_combo.setToolTip("選択項目を取り込む先のプロジェクトを選びます")
        self.scope_combo.setWhatsThis("選択項目を取り込む先のプロジェクトを選びます")
        self.scope_combo.setAccessibleName("割り当て先プロジェクト")
        actions.addWidget(QLabel("プロジェクト:"))
        actions.addWidget(self.scope_combo, 1)
        self.scope_button = QPushButton("割り当て")
        self.scope_button.setToolTip("選択項目を左で選んだプロジェクトに取り込み（関連付け）ます")
        self.scope_button.setWhatsThis("選択項目を左で選んだプロジェクトに取り込み（関連付け）ます")
        self.scope_button.clicked.connect(self._apply_scope)
        actions.addWidget(self.scope_button)
        self._secondary_widgets.append(self.scope_button)
        _menu_action("assign", "プロジェクト割当", self._apply_scope)
        self.actions_menu.addSeparator()
        link = QPushButton("測定ワークスペースを開く")
        link.setToolTip("測定ワークスペースの「読み込み」ページへ移動します")
        link.setWhatsThis("測定ワークスペースの「読み込み」ページへ移動します")
        link.setAccessibleName("測定ワークスペースを開く")
        link.clicked.connect(self._open_measurement_import)
        actions.addWidget(link)
        self._secondary_widgets.append(link)
        _menu_action(
            "open_measurement",
            "測定ワークスペースを開く",
            self._open_measurement_import,
        )
        self.field_return_link = None
        if self._list_contributions is not None:
            self.field_return_link = QPushButton("フィールドリターンを開く")
            self.field_return_link.setToolTip(
                "受け取ったフィールドリターンの一覧タブへ移動します"
            )
            self.field_return_link.setWhatsThis(
                "受け取ったフィールドリターンの一覧タブへ移動します"
            )
            self.field_return_link.setAccessibleName(
                "フィールドリターンを開く"
            )
            self.field_return_link.clicked.connect(
                self._open_field_return_tab
            )
            actions.addWidget(self.field_return_link)
            self._secondary_widgets.append(self.field_return_link)
            _menu_action(
                "open_field_return",
                "フィールドリターンを開く",
                self._open_field_return_tab,
            )
        self.actions_overflow = QToolButton()
        self.actions_overflow.setText("操作 ▾")
        self.actions_overflow.setToolTip(
            "延期・却下・再開・昇格・割り当て・画面遷移の一覧です"
        )
        self.actions_overflow.setAccessibleName("その他の操作")
        self.actions_overflow.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.actions_overflow.setMenu(self.actions_menu)
        self.actions_overflow.setVisible(False)
        actions.addWidget(self.actions_overflow)
        self.actions_menu.aboutToShow.connect(
            lambda: self._sync_actions(self._last_inspection)
        )
        detail_layout.addLayout(actions)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        self._tabs = QTabWidget()
        self._tabs.addTab(delivery_panel, "キャプチャ配送")
        if self._list_watch_failures is not None:
            self._tabs.addTab(self._build_watch_failures_tab(), "失敗キュー")
        self._contributions_tab = None
        if self._list_contributions is not None:
            self._contributions_tab = self._build_contributions_tab()
            self._tabs.addTab(self._contributions_tab, "フィールドリターン")
        if self._list_missions is not None:
            self._tabs.addTab(self._build_missions_tab(), "ミッション")
        layout.addWidget(self._tabs, 1)
        # Seed the fold state now: hidden secondary widgets don't count
        # toward minimumSizeHint, so the page's minimum width stays small
        # instead of pinning the whole shell above a 1366px screen.
        # The first real resizeEvent recomputes and unfolds when it fits.
        self._sync_action_layout()
        self.refresh()

    # -- mass-arrival triage (#988) --------------------------------------
    #
    # The filter row re-presents the last ``list_items`` snapshot —
    # search/queue/sort/group never re-read the store and never carry
    # approval authority; assign/promote/reject keep their exact-identity
    # (lineage_digest) handlers and explicit confirmations unchanged.

    def _build_inbox_filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.inbox_search_edit = QLineEdit()
        self.inbox_search_edit.setPlaceholderText(
            "検索（スコープ・シリーズ・分類・由来）"
        )
        self.inbox_search_edit.setClearButtonEnabled(True)
        self.inbox_search_edit.setToolTip(
            "表示する取り込み項目を絞り込みます。"
            "検索や並び順は見た目だけを変え、承認の根拠にはなりません。"
        )
        self.inbox_search_edit.setAccessibleName("取り込み検索")
        row.addWidget(self.inbox_search_edit, 1)
        row.addWidget(QLabel("状態:"))
        self.inbox_state_combo = QComboBox()
        self.inbox_state_combo.setToolTip(
            "処理状態のキューで絞り込みます。"
            "要レビューは保留中・昇格可能・ブロックの未処理分です。"
        )
        self.inbox_state_combo.setAccessibleName("取り込み状態フィルタ")
        self._state_base_labels = {}
        for key, label in (
            ("review", "要レビュー"),
            ("all", "すべて"),
            *(
                (queue, _INBOX_QUEUE_LABELS[queue])
                for queue in _INBOX_QUEUE_ORDER
            ),
        ):
            self._state_base_labels[key] = label
            self.inbox_state_combo.addItem(label, key)
        row.addWidget(self.inbox_state_combo)
        row.addWidget(QLabel("並び:"))
        self.inbox_sort_combo = QComboBox()
        self.inbox_sort_combo.setToolTip(
            "一覧の並び順です（承認の根拠にはなりません）。"
        )
        self.inbox_sort_combo.setAccessibleName("取り込み並び替え")
        for key, label in _INBOX_SORT_OPTIONS:
            self.inbox_sort_combo.addItem(label, key)
        row.addWidget(self.inbox_sort_combo)
        row.addWidget(QLabel("グループ:"))
        self.inbox_group_combo = QComboBox()
        self.inbox_group_combo.setToolTip(
            "一覧を状態・スコープ・シリーズ・分類で区切って表示します。"
        )
        self.inbox_group_combo.setAccessibleName("取り込みグループ化")
        for key, label in _INBOX_GROUP_OPTIONS:
            self.inbox_group_combo.addItem(label, key)
        row.addWidget(self.inbox_group_combo)
        self.inbox_count_label = QLabel()
        self.inbox_count_label.setAccessibleName("要レビュー件数")
        self.inbox_count_label.setToolTip(
            "保留中・昇格可能・ブロックの合計が要レビュー件数です。"
        )
        set_typography_role(
            self.inbox_count_label, TypographyRole.SECONDARY
        )
        row.addWidget(self.inbox_count_label)
        row.addStretch(1)
        # Connect last — the first addItem in a fresh combo emits
        # currentIndexChanged while later controls are still being built.
        self.inbox_search_edit.textChanged.connect(self._refilter)
        self.inbox_state_combo.currentIndexChanged.connect(self._refilter)
        self.inbox_sort_combo.currentIndexChanged.connect(self._refilter)
        self.inbox_group_combo.currentIndexChanged.connect(self._refilter)
        return row

    def _refilter(self, *_args: object) -> None:
        """Re-present the cached snapshot — never re-reads the store."""
        self._rebuild_delivery_rows()

    def _rebuild_delivery_rows(self) -> None:
        selected = self._selected_row_data()
        state_filter = self.inbox_state_combo.currentData() or "review"
        sort_key = self.inbox_sort_combo.currentData() or "arrival_asc"
        group_key = self.inbox_group_combo.currentData() or "none"
        query = self.inbox_search_edit.text().strip().lower()
        counts = {queue: 0 for queue in _INBOX_QUEUE_LABELS}
        visible = []
        for item in self._items:
            queue = _inbox_queue_state(item)
            counts[queue] = counts.get(queue, 0) + 1
            if state_filter == "review":
                if queue not in _INBOX_ACTIONABLE_QUEUES:
                    continue
            elif state_filter != "all" and queue != state_filter:
                continue
            if query and query not in _inbox_search_text(item):
                continue
            visible.append(item)
        visible.sort(
            key=lambda item: _inbox_sort_key(item, sort_key),
            reverse=sort_key == "arrival_desc",
        )
        if group_key != "none":
            # Stable re-sort by group keeps the chosen order inside each
            # group — group headers only ever precede their own members.
            visible.sort(
                key=lambda item: _inbox_group_sort(item, group_key)
            )
        self._populate_delivery_table(visible, group_key)
        self._sync_inbox_counts(counts, len(visible))
        actionable_rows = any(
            entry is not None
            and _inbox_queue_state(entry) in _INBOX_ACTIONABLE_QUEUES
            for entry in self._displayed_items
        )
        self.next_unprocessed_button.setEnabled(actionable_rows)
        self.detail_button.setEnabled(
            any(entry is not None for entry in self._displayed_items)
        )
        if selected is not None and self._select_delivery_row(selected[0]):
            return
        self._sync_detail()

    def _populate_delivery_table(
        self, items: list, group_key: str
    ) -> None:
        self.table.setRowCount(0)
        self._displayed_items = []
        group_counts: dict = {}
        if group_key != "none":
            for item in items:
                key = _inbox_group_sort(item, group_key)
                group_counts[key] = group_counts.get(key, 0) + 1
        previous_group = None
        for item in items:
            if group_key != "none":
                key = _inbox_group_sort(item, group_key)
                if key != previous_group:
                    previous_group = key
                    self._insert_group_row(
                        item, group_key, group_counts[key]
                    )
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._displayed_items.append(item)
            for column, value in enumerate(
                (
                    _inbox_scope_label(item.scope),
                    item.capture_series_id,
                    _classification_label(item.primary_classification),
                    _inbox_state_cell(item),
                    str(item.arrival_count),
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item.inbox_item_id)
                    cell.setData(_INBOX_LINEAGE_ROLE, item.lineage_digest)
                self.table.setItem(row, column, cell)

    def _insert_group_row(
        self, item, group_key: str, count: int
    ) -> None:
        """Non-selectable group header — spans the row, carries no item
        identity so selection/next-unprocessed can never land on it."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._displayed_items.append(None)
        cell = QTableWidgetItem(
            f"{_inbox_group_title(item, group_key)}（{count} 件）"
        )
        cell.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = cell.font()
        font.setBold(True)
        cell.setFont(font)
        self.table.setItem(row, 0, cell)
        self.table.setSpan(row, 0, 1, self.table.columnCount())

    def _sync_inbox_counts(self, counts: dict, shown: int) -> None:
        actionable = sum(
            counts.get(queue, 0) for queue in _INBOX_ACTIONABLE_QUEUES
        )
        self.inbox_count_label.setText(
            f"要レビュー {actionable} 件 / 表示 {shown} / "
            f"全 {len(self._items)} 件"
        )
        # Live per-queue counts on the combo — mass arrivals stay legible
        # without flipping the filter to count each state.
        for index in range(self.inbox_state_combo.count()):
            key = self.inbox_state_combo.itemData(index)
            base = self._state_base_labels.get(key, "")
            if key == "review":
                total = actionable
            elif key == "all":
                total = len(self._items)
            else:
                total = counts.get(key, 0)
            self.inbox_state_combo.setItemText(index, f"{base}（{total}）")

    def _select_delivery_row(self, inbox_item_id: str) -> bool:
        """Select the row whose col-0 cell carries this exact item id."""
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) == inbox_item_id
            ):
                self.table.selectRow(row)
                self.table.scrollToItem(cell)
                return True
        return False

    def reveal_all_items(self) -> None:
        """Drop queue filter + search so deep links reach hidden rows.

        The default 要レビュー queue hides deferred/rejected/processed
        items — a focus target must still be able to land on them.
        """
        self.inbox_search_edit.clear()
        all_index = self.inbox_state_combo.findData("all")
        if all_index >= 0:
            self.inbox_state_combo.setCurrentIndex(all_index)
        self._refilter()

    def _show_next_unprocessed(self) -> None:
        """Advance selection to the next actionable row (wraps)."""
        selected = self._selected_row_data()
        start = -1
        if selected is not None:
            for row, entry in enumerate(self._displayed_items):
                if (
                    entry is not None
                    and entry.inbox_item_id == selected[0]
                ):
                    start = row
                    break
        rows = len(self._displayed_items)
        for offset in range(1, rows + 1):
            index = (start + offset) % rows
            entry = self._displayed_items[index]
            if entry is None:
                continue
            if _inbox_queue_state(entry) in _INBOX_ACTIONABLE_QUEUES:
                self.table.selectRow(index)
                cell = self.table.item(index, 0)
                if cell is not None:
                    self.table.scrollToItem(cell)
                return
        self.detail.setText("未処理の項目はありません。")

    def _confirm_detail(self) -> None:
        """詳細を確認 — focus the detail pane, selecting the next
        unprocessed row first when nothing is selected."""
        if self._selected_digest() is None:
            self._show_next_unprocessed()
            if self._selected_digest() is None:
                return
        else:
            self._sync_detail()
        self.detail.setFocus()

    def _applicable_ops(self, inspection) -> tuple:
        """Action labels currently valid for the inspected item — the
        display mirror of ``_sync_actions``' enable rules."""
        disposition = inspection.item.disposition
        ops = []
        if self._defer_item is not None and disposition == "pending":
            ops.append("延期")
        if self._reject_item is not None and disposition in (
            "pending",
            "deferred",
        ):
            ops.append("却下")
        if self._resume_item is not None and disposition in (
            "deferred",
            "rejected",
        ):
            ops.append("再開")
        if self._promote_item is not None and self._promotable(inspection):
            ops.append("昇格")
        if self._assign_scope is not None and disposition in (
            "pending",
            "deferred",
        ):
            ops.append("プロジェクト割当")
        return tuple(ops)

    def _blocked_reason(self, inspection) -> str:
        """Why the item cannot promote right now, or what it is missing."""
        item = inspection.item
        reasons = []
        if item.bundle_validation != "validated":
            reasons.append(
                "バンドル検証未通過"
                + (
                    f"（{item.validation_detail}）"
                    if item.validation_detail
                    else ""
                )
            )
        if item.primary_classification == "identity_digest_conflict":
            reasons.append("同一性ダイジェストの競合が未解決")
        if item.dependency_state == "unresolved":
            reasons.append(
                "依存関係が未解決"
                + (
                    f"（{item.dependency_detail}）"
                    if item.dependency_detail
                    else ""
                )
            )
        if item.alignment_state == "blocked":
            reasons.append(
                "整列がブロック"
                + (
                    f"（{item.alignment_detail}）"
                    if item.alignment_detail
                    else ""
                )
            )
        if item.evidence_conflict_state == "open":
            reasons.append(
                "証拠競合が未解決"
                + (
                    f"（{item.evidence_conflict_detail}）"
                    if item.evidence_conflict_detail
                    else ""
                )
            )
        if not reasons and not capture_inbox_item_project_id(item):
            reasons.append(
                "プロジェクト未割当 — 割り当てると昇格を検討できます"
            )
        if (
            inspection.promotability == "blocked"
            and inspection.blocked_authority_kinds
        ):
            kinds = "・".join(
                _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                for kind in inspection.blocked_authority_kinds
            )
            reasons.append(f"昇格できない権威: {kinds}")
        return " / ".join(reasons)

    def _next_action_hint(self, inspection) -> str:
        queue = _inbox_queue_state(inspection.item)
        promotability = inspection.promotability
        if queue in ("promotable", "pending"):
            if promotability == "blocked":
                return (
                    "ブロック理由を解消してから再評価します"
                    "（延期もできます）。"
                )
            if promotability == "complete":
                return "昇格は完了しています。"
            if queue == "promotable":
                return "「昇格…」で注釈・測定をプロジェクトへ反映できます。"
            return (
                "「プロジェクト割当」で割り当てると昇格を検討できます"
                "（延期・却下も可）。"
            )
        if queue == "blocked":
            return (
                "ブロック理由を解消してから再評価します（延期もできます）。"
            )
        if queue == "deferred":
            return "「再開」で検討対象に戻します。"
        if queue == "rejected":
            return "却下済みです（「再開」で保留に戻せます）。"
        return "処理済みです — 追加の操作は不要です。"

    def _open_measurement_import(self) -> None:
        self._on_navigate(
            WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "import")
        )

    def _open_field_return_tab(self) -> None:
        """Field-return deep link — the surface lives in this page's
        フィールドリターン tab, so the honest route is switching to it."""
        if self._contributions_tab is not None:
            self._tabs.setCurrentWidget(self._contributions_tab)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_action_layout()

    def _actions_required_width(self) -> int:
        """Width the action row needs to show every control unclipped.

        Computed from child size hints so it stays correct whether the
        row is currently folded or not (hidden widgets keep their hints);
        the stretch-factor scope combo contributes only its minimum —
        folding guards the fixed controls, not its slack.
        """
        total = 0
        visible = 0
        for index in range(self._actions_layout.count()):
            item = self._actions_layout.itemAt(index)
            widget = item.widget()
            if widget is self.actions_overflow:
                continue  # replaces the secondary set, never coexists
            if widget is self.scope_combo:
                total += widget.minimumSizeHint().width()
                visible += 1
            elif widget is not None:
                total += widget.sizeHint().width()
                visible += 1
            else:
                total += item.sizeHint().width()
        if visible > 1:
            total += self._actions_layout.spacing() * (visible - 1)
        return total

    def _sync_action_layout(self) -> None:
        """Fold secondary actions into 操作 ▾ when the row cannot fit.

        Trigger = required row width vs the width the page can give it —
        a fixed pixel threshold can sit below the page's own minimum and
        never fire. High-DPI screens shrink logical width, which is
        exactly the crowded case. The primary triage ops
        (次の未処理/詳細) never collapse.
        """
        chrome = 0
        layout = self.layout()
        if layout is not None:
            margins = layout.contentsMargins()
            chrome = margins.left() + margins.right()
        available = self.width() - chrome
        required = self._actions_required_width()
        if self._collapsed_actions is True:
            # Small hysteresis so a borderline resize doesn't flap open.
            collapse = required > available - 24
        else:
            collapse = required > available
        if self._collapsed_actions == collapse:
            return
        self._collapsed_actions = collapse
        for widget in self._secondary_widgets:
            widget.setVisible(not collapse)
        self.actions_overflow.setVisible(collapse)

    def _selected_row_data(self) -> tuple[str, str] | None:
        items = self.table.selectedItems()
        for item in items:
            if item.column() != 0:
                continue
            item_id = item.data(Qt.ItemDataRole.UserRole)
            digest = item.data(_INBOX_LINEAGE_ROLE)
            if item_id and digest:
                return str(item_id), str(digest)
        return None

    def _selected_digest(self) -> str | None:
        data = self._selected_row_data()
        return data[1] if data else None

    def _sync_detail(self) -> None:
        digest = self._selected_digest()
        if digest is None:
            if self.table.rowCount() == 0:
                self.detail.setText(
                    "条件に一致する項目はありません。"
                    "検索や状態フィルタを見直してください。"
                    if self._items
                    else "取り込み待ちの配送はありません。"
                    "配送が到着するとここに表示されます。"
                )
            else:
                self.detail.setText("一覧から項目を選択すると詳細を表示します。")
            self._sync_actions(None)
            return
        inspection = (
            self._inspect_item(digest) if self._inspect_item is not None else None
        )
        if inspection is None:
            self.detail.setText(f"項目を確認できません: {digest[:12]}…")
            self._sync_actions(None)
            return
        self._populate_detail(inspection)
        self._sync_actions(inspection)

    def _populate_detail(self, inspection) -> None:
        item = inspection.item
        flags = (
            "・".join(
                _INBOX_CLASSIFICATION_LABELS.get(flag, flag)
                for flag in item.classification_flags
            )
            if item.classification_flags
            else _classification_label(item.primary_classification)
        )
        scope = _inbox_scope_label(item.scope)
        lines = [
            f"スコープ: {scope}",
            f"項目: {item.inbox_item_id.split(':', 1)[-1][:16]}…"
            f" / 系列 {item.capture_series_id} / リビジョン {item.capture_revision_id}",
            f"分類: {_classification_label(item.primary_classification)}（{flags}）",
            f"到着: {item.arrival_source} ×{item.arrival_count}（{item.first_arrived_at_utc}）",
            "ゲート: "
            + " / ".join(
                f"{_INBOX_GATE_LABELS[key]}="
                f"{_INBOX_GATE_STATE_LABELS.get(getattr(item, key), getattr(item, key))}"
                + (
                    f"（{getattr(item, _INBOX_GATE_DETAIL_FIELDS[key], '')}）"
                    if getattr(item, _INBOX_GATE_DETAIL_FIELDS[key], '')
                    else ""
                )
                for key in _INBOX_GATE_LABELS
            ),
            f"昇格可能性: {_INBOX_PROMOTABILITY_LABELS.get(inspection.promotability, inspection.promotability)}"
            + (
                "（昇格対象: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.available_authority_kinds
                    )
                    or "なし"
                )
                + "）"
            ),
            (
                "昇格済: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.promoted_authority_kinds
                    )
                    or "なし"
                )
                + " / 不可: "
                + (
                    "・".join(
                        _INBOX_AUTHORITY_KIND_LABELS.get(kind, kind)
                        for kind in inspection.blocked_authority_kinds
                    )
                    or "なし"
                )
            ),
            f"内容: 証拠{inspection.source_evidence_count} / "
            f"間取り{inspection.roomplan_record_count} / "
            f"メッシュ{inspection.raw_mesh_count} / "
            f"権威{inspection.authority_record_count}",
            f"状態: {_INBOX_DISPOSITION_LABELS.get(item.disposition, item.disposition)}"
            + (f" — {item.disposition_reason}" if item.disposition_reason else ""),
        ]
        promotions = getattr(inspection, "promotions", ()) or ()
        if promotions:
            latest = max(
                promotions, key=lambda record: record.promoted_at_utc
            )
            lines.append(
                "直近の昇格: "
                f"{_INBOX_AUTHORITY_KIND_LABELS.get(latest.authority_kind, latest.authority_kind)} — "
                f"{_INBOX_OUTCOME_LABELS.get(latest.outcome, latest.outcome)}"
                + (f"（{latest.detail}）" if latest.detail else "")
            )
        if item.operator_notes:
            lines.append(f"メモ: {item.operator_notes}")
        # #988 detail triage: what can be done now, what is missing or
        # blocking, and the suggested next step — the operator never
        # re-derives it from raw facets during a mass-triage pass.
        ops = self._applicable_ops(inspection)
        if ops:
            lines.append("適用可能な操作: " + "・".join(ops))
        blocked = self._blocked_reason(inspection)
        if blocked:
            lines.append(f"不足・ブロック理由: {blocked}")
        lines.append(f"次のアクション: {self._next_action_hint(inspection)}")
        self.detail.setText("\n".join(lines))
        self._populate_scope_combo(item.scope)

    def _populate_scope_combo(self, current_scope: str) -> None:
        self.scope_combo.clear()
        self.scope_combo.addItem("（未割り当て）", "capture-inbox-unassigned")
        if self._list_projects is None:
            return
        current_index = 0
        for entry in self._list_projects():
            label = getattr(entry, "display_name", "") or getattr(
                entry, "project_id", ""
            )
            document_id = getattr(entry, "document_id", "")
            if not document_id:
                continue
            self.scope_combo.addItem(label, document_id)
            if document_id == current_scope:
                current_index = self.scope_combo.count() - 1
        self.scope_combo.setCurrentIndex(current_index)

    def _sync_actions(self, inspection) -> None:
        disposition = (
            getattr(inspection.item, "disposition", None)
            if inspection is not None
            else None
        )
        self._last_inspection = inspection
        self.defer_button.setEnabled(
            self._defer_item is not None and disposition == "pending"
        )
        self.reject_button.setEnabled(
            self._reject_item is not None
            and disposition in ("pending", "deferred")
        )
        self.resume_button.setEnabled(
            self._resume_item is not None
            and disposition in ("deferred", "rejected")
        )
        self.promote_button.setEnabled(
            self._promote_item is not None
            and self._promotable(inspection)
        )
        self.scope_button.setEnabled(
            self._assign_scope is not None
            and disposition in ("pending", "deferred")
        )
        # The collapsed overflow menu mirrors the buttons' enable state —
        # a folded action must be exactly as reachable as the button was.
        for key, button in (
            ("defer", self.defer_button),
            ("reject", self.reject_button),
            ("resume", self.resume_button),
            ("promote", self.promote_button),
            ("assign", self.scope_button),
        ):
            action = self._menu_actions.get(key)
            if action is not None:
                action.setEnabled(button.isEnabled())

    @staticmethod
    def _promotable(inspection) -> bool:
        """True when the item can execute a promotion right now."""

        if inspection is None:
            return False
        item = inspection.item
        if item.disposition not in ("pending", "partially_promoted"):
            return False
        if not capture_inbox_item_project_id(item):
            return False
        return bool(
            set(inspection.available_authority_kinds)
            & {"annotations", "measurements"}
        )

    def _promote(self) -> None:
        digest = self._selected_digest()
        if digest is None or self._promote_item is None:
            return
        reason, ok = QInputDialog.getText(
            self, "昇格", "昇格の理由を入力してください。"
        )
        if not ok or not reason.strip():
            return
        try:
            self._promote_item(digest, reason.strip())
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: promotion — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "昇格できませんでした", exc)
            return
        self._refresh_keep_selection()

    def _dispose(self, action: str) -> None:
        digest = self._selected_digest()
        if digest is None:
            return
        handler = {
            "defer": self._defer_item,
            "reject": self._reject_item,
            "resume": self._resume_item,
        }[action]
        if handler is None:
            return
        reason = ""
        if action in ("defer", "reject"):
            title = {"defer": "延期", "reject": "却下"}[action]
            reason, ok = QInputDialog.getText(
                self, title, f"{title}の理由を入力してください。"
            )
            if not ok or not reason.strip():
                return
            reason = reason.strip()
        try:
            handler(digest, reason) if reason else handler(digest)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: contribution disposition — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "取り込みできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _apply_scope(self) -> None:
        digest = self._selected_digest()
        scope = self.scope_combo.currentData()
        if digest is None or not scope or self._assign_scope is None:
            return
        try:
            self._assign_scope(digest, str(scope))
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: scope assignment — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "プロジェクト領域を割り当てできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _refresh_keep_selection(self) -> None:
        selected = self._selected_row_data()
        self.refresh()
        if selected is None:
            return
        if self._select_delivery_row(selected[0]):
            return
        # The acted item left the visible queue (e.g. deferred under the
        # 要レビュー filter) — advance to the next unprocessed row so a
        # mass-triage pass never loses its place or lands on a wrong item.
        self._show_next_unprocessed()

    def refresh(self) -> None:
        self._items = tuple(self._list_items())
        self._rebuild_delivery_rows()
        self._refresh_contributions()
        self._refresh_missions()
        self._refresh_watch_failures()

    # -- watch-folder failure queue (#1022) ------------------------------
    #
    # Drops that exhausted the runner's route cap never reach the inbox
    # above, so this bounded persistent queue is their recovery surface:
    # per route the sanitized error kind, attempt count and first/last
    # seen, plus the explicit reprocess actions — never a fake success.

    _WATCH_FAILURE_KIND_LABELS = {
        "routing_error": "取り込み処理エラー",
        "import_failed": "取り込み失敗",
        "invalid_or_unsupported": "未対応・不正",
        "user_action_required": "要対応",
    }
    _WATCH_FAILURE_CLASS_LABELS = {
        "retryable": "再試行可能",
        "unsupported": "未対応",
        "user_action_required": "要対応",
    }
    _WATCH_FAILURE_VERDICT_LABELS = {
        "ok": "再試行できます",
        "not_retryable": "再試行では解決しません",
        "deleted": "ファイルが見つかりません",
        "replaced": "記録時と内容が変わっています",
        "epoch_mismatch": "監視フォルダー設定が記録時と異なります",
    }

    def _build_watch_failures_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        intro = QLabel(
            "監視フォルダーで自動再試行の上限に達し、取り込めなかった"
            "ファイルです。ここには受信ボックス行の無い失敗だけが残ります"
            " — 行を選ぶと原因と再処理の操作が表示されます。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)
        self.watch_failure_table = QTableWidget(0, 5)
        self.watch_failure_table.setAccessibleName("失敗キュー一覧")
        self.watch_failure_table.setToolTip(
            "監視フォルダーで取り込めなかったファイルの一覧です。"
            "行を選ぶと詳細と再処理の操作が下に表示されます。"
        )
        self.watch_failure_table.setHorizontalHeaderLabels(
            ("ファイル", "エラー", "分類", "試行", "最終確認（UTC）")
        )
        for _col, _tip in enumerate((
            "失敗したファイル名",
            "取り込みに失敗した種類（サニタイズ済み）",
            "再試行可能か、対応が必要か",
            "自動＋手動の取り込み試行回数",
            "最後に失敗を確認した日時（UTC）",
        )):
            self.watch_failure_table.horizontalHeaderItem(
                _col
            ).setToolTip(_tip)
        self.watch_failure_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.watch_failure_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.watch_failure_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.watch_failure_table.itemSelectionChanged.connect(
            self._sync_watch_failure_detail
        )
        layout.addWidget(self.watch_failure_table, 1)
        self.watch_failure_detail = QLabel(
            "一覧から項目を選択すると詳細を表示します。"
        )
        self.watch_failure_detail.setWordWrap(True)
        self.watch_failure_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(
            self.watch_failure_detail, TypographyRole.SECONDARY
        )
        layout.addWidget(self.watch_failure_detail)
        actions = QHBoxLayout()
        self.watch_retry_button = QPushButton("安全に再試行…")
        self.watch_retry_button.setAccessibleName("安全に再試行")
        self.watch_retry_button.setToolTip(
            "記録されたファイルと一致すること（場所・内容・監視設定）を"
            "確認してから、もう一度取り込みを試みます"
        )
        self.watch_retry_button.setWhatsThis(
            "記録されたファイルと一致すること（場所・内容・監視設定）を"
            "確認してから、もう一度取り込みを試みます"
        )
        self.watch_retry_button.setEnabled(False)
        self.watch_retry_button.clicked.connect(self._retry_watch_failure_row)
        actions.addWidget(self.watch_retry_button)
        self.watch_import_button = QPushButton("このファイルを選んで取込…")
        self.watch_import_button.setAccessibleName("このファイルを選んで取込")
        self.watch_import_button.setToolTip(
            "記録時と変わっていても構わず、その場所にある現在のファイルを"
            "明示的に取り込みます（受信ボックスでレビューされます）"
        )
        self.watch_import_button.setWhatsThis(
            "記録時と変わっていても構わず、その場所にある現在のファイルを"
            "明示的に取り込みます（受信ボックスでレビューされます）"
        )
        self.watch_import_button.setEnabled(False)
        self.watch_import_button.clicked.connect(
            self._import_watch_failure_row
        )
        actions.addWidget(self.watch_import_button)
        self.watch_diag_button = QPushButton("サポート診断")
        self.watch_diag_button.setAccessibleName("サポート診断")
        self.watch_diag_button.setToolTip(
            "この失敗についてサポートへ共有可能な診断メモ"
            "（パスを含まない）を診断フォルダーに書き出します"
        )
        self.watch_diag_button.setWhatsThis(
            "この失敗についてサポートへ共有可能な診断メモ"
            "（パスを含まない）を診断フォルダーに書き出します"
        )
        self.watch_diag_button.setEnabled(False)
        self.watch_diag_button.clicked.connect(
            self._diagnose_watch_failure_row
        )
        actions.addWidget(self.watch_diag_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return panel

    def _selected_watch_failure(self):
        items = self.watch_failure_table.selectedItems()
        index = None
        for item in items:
            if item.column() == 0:
                index = item.row()
                break
        if index is None or index >= len(self._watch_failures):
            return None
        return self._watch_failures[index]

    def _refresh_watch_failures(self) -> None:
        if self._list_watch_failures is None:
            return
        self._watch_failures = self._list_watch_failures()
        self.watch_failure_table.setRowCount(0)
        for entry in self._watch_failures:
            row = self.watch_failure_table.rowCount()
            self.watch_failure_table.insertRow(row)
            for column, value in enumerate(
                (
                    entry.basename,
                    self._WATCH_FAILURE_KIND_LABELS.get(
                        entry.error_kind, entry.error_kind
                    ),
                    self._WATCH_FAILURE_CLASS_LABELS.get(
                        str(entry.failure_class), str(entry.failure_class)
                    ),
                    str(entry.attempts),
                    entry.last_seen_utc,
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, entry.path)
                self.watch_failure_table.setItem(row, column, cell)
        self._sync_watch_failure_detail()

    def _sync_watch_failure_detail(self) -> None:
        entry = self._selected_watch_failure()
        retryable = False
        importable = False
        if entry is None:
            self.watch_retry_button.setEnabled(False)
            self.watch_import_button.setEnabled(False)
            self.watch_diag_button.setEnabled(False)
            self.watch_failure_detail.setText(
                "取り込みに失敗して自動再試行を終えたファイルは"
                "ありません。"
                if self.watch_failure_table.rowCount() == 0
                else "一覧から項目を選択すると詳細を表示します。"
            )
            return
        self.watch_diag_button.setEnabled(
            self._diagnose_watch_failure is not None
        )
        verdict_reason = ""
        if self._verify_watch_failure is not None:
            try:
                verdict, verdict_reason = self._verify_watch_failure(
                    entry.path
                )
            except EXPECTED_OPERATION_ERRORS:
                verdict, verdict_reason = None, ""
            retryable = getattr(verdict, "value", verdict) == "ok"
            importable = getattr(verdict, "value", verdict) != "deleted"
            verdict_label = self._WATCH_FAILURE_VERDICT_LABELS.get(
                getattr(verdict, "value", verdict), ""
            )
        else:
            verdict_label = ""
        self.watch_retry_button.setEnabled(
            retryable and self._retry_watch_failure is not None
        )
        self.watch_import_button.setEnabled(
            importable and self._import_watch_failure is not None
        )
        lines = [
            f"ファイル: {entry.basename}",
            "エラー: "
            + self._WATCH_FAILURE_KIND_LABELS.get(
                entry.error_kind, entry.error_kind
            )
            + (f" — {entry.detail}" if entry.detail else ""),
            "分類: "
            + self._WATCH_FAILURE_CLASS_LABELS.get(
                str(entry.failure_class), str(entry.failure_class)
            ),
            f"試行回数: {entry.attempts} 回",
            f"初回検出（UTC）: {entry.first_seen_utc}",
            f"最終確認（UTC）: {entry.last_seen_utc}",
            f"取り込み元: 監視フォルダー {entry.watch_root}",
        ]
        if verdict_label:
            lines.append(f"現在の状態: {verdict_label}")
        if verdict_reason and not retryable:
            lines.append(f"再試行できない理由: {verdict_reason}")
        lines.append(
            "診断ID: "
            f"[diag: {entry.diagnostic_id}]（サポート共有時に使用）"
        )
        self.watch_failure_detail.setText("\n".join(lines))

    def _retry_watch_failure_row(self) -> None:
        """安全に再試行 — show source + why-not-imported before retrying."""

        entry = self._selected_watch_failure()
        if entry is None or self._retry_watch_failure is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("安全に再試行")
        box.setText(
            "次のファイルの取り込みをもう一度試みます。\n\n"
            f"ファイル: {entry.basename}\n"
            f"取り込み元: 監視フォルダー {entry.watch_root}\n"
            "記録時の理由: "
            + self._WATCH_FAILURE_KIND_LABELS.get(
                entry.error_kind, entry.error_kind
            )
            + (f" — {entry.detail}" if entry.detail else "")
            + f"\n試行回数: {entry.attempts} 回\n\n"
            "ファイルの内容と監視設定が記録時と一致していることを"
            "確認してから実行します。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            self._retry_watch_failure(entry.path)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: watch-failure retry — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "安全な再試行に失敗しました", exc)
            return
        self.refresh()

    def _import_watch_failure_row(self) -> None:
        """このファイルを選んで取込 — explicit import of the current file."""

        entry = self._selected_watch_failure()
        if entry is None or self._import_watch_failure is None:
            return
        extra = ""
        if self._verify_watch_failure is not None:
            try:
                verdict, _reason = self._verify_watch_failure(entry.path)
            except EXPECTED_OPERATION_ERRORS:
                verdict = None
            if getattr(verdict, "value", verdict) == "replaced":
                extra = (
                    "\n\n記録時と内容が変わっています — "
                    "現在のファイルの内容が取り込まれます。"
                )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("このファイルを選んで取込")
        box.setText(
            "監視フォルダーにあるこのファイルを明示的に取り込みます。"
            "内容は取り込み時に検証され、受信ボックスでレビューされます"
            "（証拠への昇格は行いません）。\n\n"
            f"ファイル: {entry.basename}" + extra
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        try:
            self._import_watch_failure(entry.path)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: explicit watch-failure import — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "取り込みに失敗しました", exc)
            return
        self.refresh()

    def _diagnose_watch_failure_row(self) -> None:
        entry = self._selected_watch_failure()
        if entry is None or self._diagnose_watch_failure is None:
            return
        try:
            self._diagnose_watch_failure(entry.path)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: support diagnostics — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "サポート診断に失敗しました", exc)
            return
        self.refresh()

    # -- field-return contributions -------------------------------------

    _CONTRIB_VALIDATION_LABELS = {
        "validated": "検証済み",
        "unsupported": "未対応バージョン",
        "malformed": "不正",
    }
    _CONTRIB_ROUTING_LABELS = {
        "exact_project_match": "プロジェクト一致",
        "known_project_lineage": "系譜一致",
        "unknown_project_reference": "不明なプロジェクト参照",
        "legacy_project_ref": "従来参照",
        "channel_project_match": "ペアリング割当",
        "unrouted": "未振分",
    }

    def _build_contributions_tab(self) -> QWidget:
        """Received field-return contributions staged by the receiver."""

        panel = QWidget()
        layout = QVBoxLayout(panel)
        intro = QLabel(
            "ペアリング済みデバイスから届いたフィールドリターン（現地作業の完了報告）"
            "の一覧です。項目を選ぶと詳細を確認できます。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)
        self.contribution_table = QTableWidget(0, 4)
        self.contribution_table.setAccessibleName("フィールドリターン一覧")
        self.contribution_table.setToolTip(
            "受け取ったフィールドリターン貢献の一覧です。"
            "行を選ぶと下に詳細が表示されます。"
        )
        self.contribution_table.setHorizontalHeaderLabels(
            ("貢献", "検証", "ルーティング", "受信")
        )
        for _col, _tip in enumerate((
            "貢献の識別子（先頭のみ表示）",
            "アーティファクトの検証状態",
            "保存先プロジェクトへの振分状態",
            "受信日時（UTC）",
        )):
            self.contribution_table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.contribution_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.contribution_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.contribution_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.contribution_table.itemSelectionChanged.connect(
            self._sync_contribution_detail
        )
        layout.addWidget(self.contribution_table, 1)
        self.contribution_detail = QLabel(
            "一覧から項目を選択すると詳細を表示します。"
        )
        self.contribution_detail.setWordWrap(True)
        self.contribution_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(
            self.contribution_detail, TypographyRole.SECONDARY
        )
        layout.addWidget(self.contribution_detail)
        self.rebase_button = QPushButton("再基準決定…")
        self.rebase_button.setAccessibleName("再基準決定")
        self.rebase_button.setToolTip(
            "要調整のタスクについて、返却証跡を現在の対象へ"
            "対応付ける判断を記録します。"
        )
        self.rebase_button.setEnabled(False)
        self.rebase_button.clicked.connect(self._record_rebase_decision)
        actions = QHBoxLayout()
        actions.addWidget(self.rebase_button)
        self.apply_button = QPushButton("適用…")
        self.apply_button.setAccessibleName("返却証跡の適用")
        self.apply_button.setToolTip(
            "照合済みの返却証跡を、解決した対象エンティティへ"
            "結びつけます。"
        )
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply_returned_tasks)
        actions.addWidget(self.apply_button)
        self.discard_button = QPushButton("破棄…")
        self.discard_button.setAccessibleName("返却の破棄")
        self.discard_button.setToolTip(
            "この返却を一覧から取り除きます。"
            "適用済み証跡の原本は破棄できません。"
        )
        self.discard_button.setEnabled(False)
        self.discard_button.clicked.connect(self._discard_contribution)
        actions.addWidget(self.discard_button)
        layout.addLayout(actions)
        if self._rebase_record is None:
            self.rebase_button.setVisible(False)
        if self._apply_record is None:
            self.apply_button.setVisible(False)
        if self._discard_record is None:
            self.discard_button.setVisible(False)
        return panel

    def _refresh_contributions(self) -> None:
        if self._list_contributions is None:
            return
        self._contributions = self._list_contributions()
        self.contribution_table.setRowCount(0)
        for contribution in self._contributions:
            row = self.contribution_table.rowCount()
            self.contribution_table.insertRow(row)
            for column, value in enumerate(
                (
                    f"{(contribution.contribution_id or '—')[:12]}…",
                    self._CONTRIB_VALIDATION_LABELS.get(
                        contribution.validation_state,
                        contribution.validation_state,
                    ),
                    self._CONTRIB_ROUTING_LABELS.get(
                        contribution.routing, contribution.routing
                    ),
                    contribution.recorded_at_utc or "",
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole, contribution.contribution_id
                    )
                self.contribution_table.setItem(row, column, cell)
        self._sync_contribution_detail()

    def _sync_contribution_detail(self) -> None:
        items = self.contribution_table.selectedItems()
        index = None
        for item in items:
            if item.column() == 0:
                index = item.row()
                break
        if index is None or index >= len(self._contributions):
            self._active_rebase_context = None
            self.rebase_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.discard_button.setEnabled(False)
            if self.contribution_table.rowCount() == 0:
                self.contribution_detail.setText(
                    "フィールドリターンはまだ届いていません。"
                )
            else:
                self.contribution_detail.setText(
                    "一覧から項目を選択すると詳細を表示します。"
                )
            return
        contribution = self._contributions[index]
        self._selected_contribution = contribution
        if self._rebase_context is not None:
            try:
                self._active_rebase_context = self._rebase_context(
                    contribution
                )
            except EXPECTED_OPERATION_ERRORS as exc:
                # The decision surface degrades to 'no context', never
                # silently — and an authority/integrity failure must not
                # masquerade as 'nothing to decide'.
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(
                    exc, operation='再基準コンテキストの解決'
                )
                self._active_rebase_context = None
            self.rebase_button.setEnabled(
                bool(
                    getattr(
                        self._active_rebase_context, 'undecided', ()
                    )
                )
            )
            self.apply_button.setEnabled(
                bool(
                    getattr(
                        self._active_rebase_context, 'applyable', ()
                    )
                )
            )
        # An applied contribution is the retained original its bound
        # record_refs resolve through — only offer discard when nothing
        # in the application ledger cites this contribution.
        self.discard_button.setEnabled(
            not any(
                application.contribution_id
                == contribution.contribution_id
                for application in getattr(
                    self._active_rebase_context, 'applications', ()
                ) or ()
            )
        )
        lines = [
            f"貢献: {contribution.contribution_id}",
            f"検証: "
            + self._CONTRIB_VALIDATION_LABELS.get(
                contribution.validation_state, contribution.validation_state
            ),
            "ルーティング: "
            + self._CONTRIB_ROUTING_LABELS.get(
                contribution.routing, contribution.routing
            )
            + (
                f"（{contribution.matched_project_id}）"
                if contribution.matched_project_id
                else ""
            ),
        ]
        if contribution.mission_id:
            lines.append(f"ミッション: {contribution.mission_id}")
        if contribution.plan_sha256:
            lines.append(f"計画: {contribution.plan_sha256[:16]}…")
        if self._reconcile_contribution is not None:
            lines.extend(self._reconcile_contribution(contribution))
        retention = (
            'バイト保持'
            if getattr(contribution, 'artifact_retained', False)
            else 'バイト未保持'
        )
        lines.append(
            f"アーティファクト: {contribution.artifact_sha256[:16]}…"
            f"（{retention}）"
        )
        if (
            self._resolve_return_evidence is not None
            and getattr(contribution, 'artifact_retained', False)
        ):
            try:
                resolved_tasks = self._resolve_return_evidence(
                    contribution
                )
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise
                lines.append(
                    "証跡: 解読失敗（"
                    f"{operation_error_message(exc)}）"
                )
            else:
                if resolved_tasks is not None:
                    counts = {
                        'resolved': 0,
                        'evidence_asset': 0,
                        'external': 0,
                        'unresolved': 0,
                    }
                    for task in resolved_tasks:
                        for ref in getattr(task, 'refs', ()):
                            state = getattr(ref, 'state', '')
                            if state in counts:
                                counts[state] += 1
                    total = sum(counts.values())
                    parts = []
                    if counts['resolved']:
                        parts.append(f"解決 {counts['resolved']}")
                    if counts['evidence_asset']:
                        parts.append(
                            f"証拠ファイル {counts['evidence_asset']}"
                        )
                    if counts['external']:
                        parts.append(f"外部参照 {counts['external']}")
                    if counts['unresolved']:
                        parts.append(f"未解決 {counts['unresolved']}")
                    lines.append(
                        f"証跡: {total}件参照（{' / '.join(parts)}）"
                        if total
                        else "証跡: 参照なし"
                    )
        if contribution.detail:
            lines.append(f"詳細: {contribution.detail}")
        self.contribution_detail.setText("\n".join(lines))

    def _record_rebase_decision(self) -> None:
        context = self._active_rebase_context
        undecided = getattr(context, 'undecided', None)
        if not undecided or self._rebase_record is None:
            return
        # Labels must be unique — two undecided tasks can share the
        # 8-char prefix and reason, and matching by text would silently
        # record the decision against whichever sorts first.
        raw_task_labels = [
            f'{result.task_id[:8]}… — {result.reason}'
            for result in undecided
        ]
        task_labels = [
            (
                label
                if raw_task_labels.count(label) == 1
                else f'{label} [{result.task_id}]'
            )
            for label, result in zip(
                raw_task_labels, undecided, strict=True
            )
        ]
        choice, ok = QInputDialog.getItem(
            self, "再基準決定", "対象タスク:", task_labels, 0, False
        )
        if not ok:
            return
        task_id = undecided[task_labels.index(choice)].task_id
        entities = context.revision.document.entities
        if not entities:
            warn_user(
                self,
                "再基準決定を記録できませんでした",
                ValueError("対応付け先のエンティティが現在版にありません"),
            )
            return
        entity_labels = [
            f'{entity.entity_id} — {entity.name}（{entity.kind}）'
            for entity in entities
        ]
        choice, ok = QInputDialog.getItem(
            self,
            "再基準決定",
            "対応付ける現在の対象:",
            entity_labels,
            0,
            False,
        )
        if not ok:
            return
        current_target_id = entities[entity_labels.index(choice)].entity_id
        reason, ok = QInputDialog.getText(
            self, "再基準決定", "対応付けの理由を入力してください。"
        )
        if not ok or not reason.strip():
            return
        decided_by, ok = QInputDialog.getText(
            self, "再基準決定", "決定者名を入力してください。"
        )
        if not ok or not decided_by.strip():
            return
        try:
            self._rebase_record(
                self._selected_contribution,
                task_id,
                current_target_id,
                reason.strip(),
                decided_by.strip(),
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: rebase decision record — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "再基準決定を記録できませんでした", exc)
            return
        self._sync_contribution_detail()

    def _apply_returned_tasks(self) -> None:
        context = self._active_rebase_context
        if not getattr(context, 'applyable', ()) or (
            self._apply_record is None
        ):
            return
        applied_by, ok = QInputDialog.getText(
            self, "適用", "適用者名を入力してください。"
        )
        if not ok or not applied_by.strip():
            return
        try:
            outcome = self._apply_record(
                self._selected_contribution, applied_by.strip()
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: returned-task apply — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "適用できませんでした", exc)
            return
        applied_count = len(getattr(outcome, 'applied', ()))
        skipped_count = len(getattr(outcome, 'skipped_task_ids', ()))
        message = f"{applied_count} 件を適用しました" + (
            f"（未決定のため {skipped_count} 件を見送り）"
            if skipped_count
            else ""
        )
        unresolved = tuple(
            getattr(outcome, 'unresolved_refs', ()) or ()
        )
        if unresolved:
            preview = '、'.join(unresolved[:3])
            message += (
                f"\n証跡参照の未解決: {len(unresolved)} 件"
                f"（{preview}…）"
            )
        if getattr(outcome, 'refs_unverifiable', False):
            message += (
                "\nアーティファクト未保持のため"
                "証跡参照を検証できませんでした"
            )
        QMessageBox.information(self, "適用完了", message)
        self._sync_contribution_detail()

    def _discard_contribution(self) -> None:
        contribution = self._selected_contribution
        if contribution is None or self._discard_record is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("返却の破棄")
        box.setText("この返却を一覧から取り除きます。")
        box.setInformativeText(
            "ステージング済みの記録だけが削除されます。"
            "アーティファクト原本と、別の返却が共有する内容は残ります。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self._discard_record(contribution)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: contribution discard — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "破棄できませんでした", exc)
            return
        self._refresh_contributions()

    # -- mission ledger ---------------------------------------------------

    _MISSION_STATUS_LABELS = {
        'pending': '発行済み',
        'received': '受信済み',
        'failed': '失敗',
        'superseded': '置換済み',
        'completed': '完了',
    }
    _MISSION_PURPOSE_LABELS = {
        'initial_capture': '初期キャプチャ',
        'design_verification': '設計検証',
        'equipment_identity': '機器識別',
        'measurement_campaign': '測定キャンペーン',
        'recapture': '再キャプチャ',
        'commissioning': 'コミッショニング',
        'custom': 'カスタム',
    }

    def _build_missions_tab(self) -> QWidget:
        """Issued mission packages awaiting/confirming delivery."""

        panel = QWidget()
        layout = QVBoxLayout(panel)
        intro = QLabel(
            "ペアリング済みデバイスへ発行したミッションパッケージの一覧です。"
            "「発行…」でこのプロジェクトから新しいミッションを送り出せます。"
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.SECONDARY)
        layout.addWidget(intro)

        self.mission_table = QTableWidget(0, 5)
        self.mission_table.setAccessibleName("ミッション一覧")
        self.mission_table.setToolTip(
            "発行済みのキャプチャミッションの一覧です。行を選ぶと詳細が下に表示されます。"
        )
        self.mission_table.setHorizontalHeaderLabels(
            ("ミッション", "目的", "部屋", "状態", "更新")
        )
        for _col, _tip in enumerate((
            "ミッションの識別子（先頭12文字）",
            "ミッションの目的分類",
            "作業対象の部屋名",
            "デバイスへの配送・完了状態",
            "状態が最後に更新された日時",
        )):
            self.mission_table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.mission_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.mission_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.mission_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.mission_table.itemSelectionChanged.connect(
            self._sync_mission_detail
        )
        layout.addWidget(self.mission_table, 1)

        self.mission_detail = QLabel(
            "一覧からミッションを選択すると詳細を表示します。"
        )
        self.mission_detail.setWordWrap(True)
        self.mission_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(
            self.mission_detail, TypographyRole.SECONDARY
        )
        layout.addWidget(self.mission_detail)

        actions = QHBoxLayout()
        self.issue_button = QPushButton("発行…")
        self.issue_button.setToolTip(
            "プロジェクトの現行シーンからタスク計画を生成し、ペアリング済みデバイスのプルレーンへ登録します"
        )
        self.issue_button.setWhatsThis(self.issue_button.toolTip())
        self.issue_button.clicked.connect(self._issue_mission_dialog)
        self.issue_button.setEnabled(
            self._issue_mission is not None
            and self._list_projects is not None
        )
        actions.addWidget(self.issue_button)
        self.export_mission_button = QPushButton("エクスポート…")
        self.export_mission_button.setToolTip(
            "選択したミッションパッケージのワイヤバイトをファイルへ書き出します（ペアリング外デバイスへの手渡し用）"
        )
        self.export_mission_button.setWhatsThis(
            self.export_mission_button.toolTip()
        )
        self.export_mission_button.clicked.connect(
            self._export_mission_dialog
        )
        self.export_mission_button.setEnabled(False)
        actions.addWidget(self.export_mission_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return panel

    def _refresh_missions(self) -> None:
        if self._list_missions is None:
            return
        self._missions = self._list_missions()
        self.mission_table.setRowCount(0)
        for package in self._missions:
            row = self.mission_table.rowCount()
            self.mission_table.insertRow(row)
            for column, value in enumerate(
                (
                    f"{(package.mission_id or package.package_id)[:12]}…",
                    self._MISSION_PURPOSE_LABELS.get(
                        package.purpose, package.purpose or '—'
                    ),
                    package.room_label or '—',
                    self._MISSION_STATUS_LABELS.get(
                        package.status, package.status
                    ),
                    getattr(package, 'updated_at_utc', None)
                    or package.issued_at_utc
                    or '',
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole, package.package_id
                    )
                self.mission_table.setItem(row, column, cell)
        self._sync_mission_detail()

    def _selected_mission(self):
        items = self.mission_table.selectedItems()
        index = None
        for item in items:
            if item.column() == 0:
                index = item.row()
                break
        if index is None or index >= len(self._missions):
            return None
        return self._missions[index]

    def _sync_mission_detail(self) -> None:
        package = self._selected_mission()
        self.export_mission_button.setEnabled(
            package is not None and self._export_mission is not None
        )
        if package is None:
            self.mission_detail.setText(
                "一覧からミッションを選択すると詳細を表示します。"
            )
            return
        lines = [
            f"ミッション: {package.mission_id or '—'}",
            f"パッケージ: {package.package_id}",
            f"目的: {self._MISSION_PURPOSE_LABELS.get(package.purpose, package.purpose or '—')}",
            f"部屋: {package.room_label or '—'}",
            f"対象: {package.project_ref or '—'}",
            (
                f"状態: {self._MISSION_STATUS_LABELS.get(package.status, package.status)}"
                + (
                    f" — {package.status_detail}"
                    if package.status_detail
                    else ""
                )
            ),
            f"パッケージ SHA-256: {package.package_sha256[:16]}…",
            f"サイズ: {package.byte_size:,} バイト",
            f"ペアリング: {package.pairing_id or '（スコープなし）'}",
        ]
        if package.supersedes_package_id:
            lines.append(f"置換元: {package.supersedes_package_id[:12]}…")
        if package.required_schema_version:
            lines.append(
                f"必要スキーマ: {package.required_schema_version}"
            )
        if package.issued_at_utc:
            lines.append(f"発行: {package.issued_at_utc}")
        updated_at = getattr(package, 'updated_at_utc', None)
        if updated_at:
            lines.append(f"更新: {updated_at}")
        self.mission_detail.setText("\n".join(lines))

    def _issue_mission_dialog(self) -> None:
        if (
            self._issue_mission is None
            or self._list_projects is None
        ):
            return
        entries = tuple(
            entry
            for entry in self._list_projects()
            if getattr(entry, 'document_id', None)
            and not getattr(entry, 'archived', False)
        )
        if not entries:
            QMessageBox.information(
                self,
                "ミッション発行",
                "発行先となるプロジェクトがありません。",
            )
            return
        # Labels must be unique — two projects may share a display
        # name, and matching by text would silently issue the mission
        # against whichever sorts first.
        labels = [
            (
                entry.display_name
                if sum(
                    other.display_name == entry.display_name
                    for other in entries
                )
                == 1
                else f'{entry.display_name} [{entry.project_id}]'
            )
            for entry in entries
        ]
        name, ok = QInputDialog.getItem(
            self,
            "ミッション発行",
            "発行対象のプロジェクトを選んでください。",
            labels,
            0,
            False,
        )
        if not ok:
            return
        entry = entries[labels.index(name)]
        purposes = list(self._MISSION_PURPOSE_LABELS)
        purpose_label, ok = QInputDialog.getItem(
            self,
            "ミッション発行",
            "ミッションの目的を選んでください。",
            [
                self._MISSION_PURPOSE_LABELS[purpose]
                for purpose in purposes
            ],
            0,
            False,
        )
        if not ok:
            return
        purpose = purposes[
            [
                self._MISSION_PURPOSE_LABELS[p] for p in purposes
            ].index(purpose_label)
        ]
        room_name, ok = QInputDialog.getText(
            self,
            "ミッション発行",
            "作業対象の部屋名を入力してください。",
            text=entry.display_name,
        )
        if not ok or not room_name.strip():
            return
        pairing_id = None
        if self._list_mission_pairings is not None:
            pairings = tuple(
                pairing
                for pairing in self._list_mission_pairings()
                if getattr(pairing, 'state', 'active')
                in ('offered', 'active')
            )
            if pairings:
                device_labels = [
                    getattr(p, 'capture_instance_id', None)
                    or getattr(p, 'pairing_id', None)
                    or '（不明なデバイス）'
                    for p in pairings
                ]
                # Labels must be unique — two pairings can show the
                # same device id, and options.index() would silently
                # scope the mission to whichever sorts first.
                device_labels = [
                    (
                        label
                        if device_labels.count(label) == 1
                        else f'{label} [{pairing.pairing_id}]'
                    )
                    for label, pairing in zip(
                        device_labels, pairings, strict=True
                    )
                ]
                options = [
                    "（スコープなし — 全ペアリングが取得可能）"
                ] + device_labels
                choice, ok = QInputDialog.getItem(
                    self,
                    "ミッション発行",
                    "配送先を絞る場合はペアリングを選んでください。",
                    options,
                    0,
                    False,
                )
                if not ok:
                    return
                if choice != options[0]:
                    pairing_id = pairings[options.index(choice) - 1].pairing_id
        try:
            package = self._issue_mission(
                entry, purpose, room_name.strip(), pairing_id
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: mission issue — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "ミッションを発行できませんでした", exc)
            return
        QMessageBox.information(
            self,
            "ミッション発行",
            "ミッションを発行しました"
            + (
                f"（ミッションID: {package.mission_id or package.package_id}）"
            ),
        )
        self._refresh_missions()

    def _export_mission_dialog(self) -> None:
        package = self._selected_mission()
        if package is None or self._export_mission is None:
            return
        destination, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "ミッションのエクスポート",
            f"mission-{package.package_id[:8]}.json",
            "ミッションパッケージ (*.json)",
        )
        if not destination:
            return
        try:
            written = self._export_mission(
                package.package_id, destination
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: mission export — expected failures surface verbatim; unexpected errors propagate to diagnostics
            warn_user(self, "エクスポートできませんでした", exc)
            return
        QMessageBox.information(
            self,
            "エクスポート完了",
            f"ミッションパッケージを書き出しました。\n{written}",
        )


_OPERATION_STATE_LABELS = {
    "queued": "待機中",
    "preflighting": "準備中",
    "running": "実行中",
    "cancellation_requested": "キャンセル要求中",
    "cancelled": "キャンセル済み",
    "completed": "完了",
    "failed": "失敗",
    "completed_for_historical_input": "完了（旧入力）",
    "result_stale": "結果が古い",
}


#: Timeline kind-filter groups (#1023) — JA labels over ActivityEventKind.
#: ``None`` selects every kind; each group maps to the closed vocabulary in
#: ``cad_project_activity.ActivityEventKind``.
_EVENT_KIND_GROUPS: tuple[tuple[str, frozenset | None], ...] = (
    ("すべての種類", None),
    ("プロジェクト・保存", frozenset({
        "project_created",
        "scene_revision_saved",
        "scene_revision_labeled",
    })),
    ("バリアント", frozenset({
        "system_variant_proposed",
        "system_variant_applied",
        "system_variant_as_built",
        "system_variant_measured",
    })),
    ("キャプチャ", frozenset({
        "capture_staged",
        "capture_promoted",
        "capture_superseded",
        "capture_deferred",
        "capture_rejected",
    })),
    ("計測", frozenset({"measurement_imported"})),
    ("校正", frozenset({
        "calibration_plan_created",
        "calibration_exported",
        "calibration_applied",
        "calibration_remeasured",
        "calibration_validated",
    })),
    ("チェックポイント", frozenset({
        "design_checkpoint_created",
        "design_checkpoint_restored",
    })),
    ("プリセット", frozenset({
        "operating_preset_created",
        "operating_preset_applied",
    })),
    ("健康点検", frozenset({
        "health_baseline_created",
        "health_check_completed",
    })),
    ("AV同期", frozenset({"av_sync_recorded"})),
    ("メモ", frozenset({"project_note"})),
    ("その他", frozenset({"other_authority"})),
)

#: Timeline date-range filter options: (label, days-back-or-None).
#: ``0`` means today (UTC); ``None`` disables the range filter;
#: :data:`_CUSTOM_RANGE` reveals the arbitrary start/end date inputs
#: (#1017).
_EVENT_RANGE_OPTIONS: tuple[tuple[str, int | str | None], ...] = (
    ("すべての期間", None),
    ("今日", 0),
    ("過去7日間", 7),
    ("過去30日間", 30),
    ("期間を指定", "custom"),
)

#: Sentinel selecting the operator-entered start/end bounds (#1017).
_CUSTOM_RANGE = "custom"

#: Unique object marking "no cached projection" — distinct from any real
#: document_id including ``None`` (the global scope).
_CACHE_MISS = object()

_EVENT_PAGE_SIZE = 50
_REVISION_PAGE_SIZE = 50

#: Per-kind JA labels for the timeline inspector (#1017) — finer than the
#: filter groups: the inspector names the exact kind, the filter groups
#: them.
_EVENT_KIND_LABELS: dict[str, str] = {
    'project_created': 'プロジェクト作成',
    'project_note': 'メモ',
    'scene_revision_saved': '部屋リビジョン保存',
    'scene_revision_labeled': 'リビジョンラベル',
    'system_variant_proposed': 'バリアント提案',
    'system_variant_applied': 'バリアント適用',
    'system_variant_as_built': 'バリアント設置済み記録',
    'system_variant_measured': 'バリアント計測',
    'capture_staged': 'キャプチャ受信',
    'capture_promoted': 'キャプチャ昇格',
    'capture_superseded': 'キャプチャ置き換え',
    'capture_deferred': 'キャプチャ保留',
    'capture_rejected': 'キャプチャ却下',
    'measurement_imported': '測定インポート',
    'calibration_plan_created': '校正プラン作成',
    'calibration_exported': '校正設定出力',
    'calibration_applied': '校正適用記録',
    'calibration_remeasured': '校正再測定',
    'calibration_validated': '校正検証',
    'design_checkpoint_created': 'チェックポイント作成',
    'design_checkpoint_restored': 'チェックポイント復元',
    'operating_preset_created': 'プリセット作成',
    'operating_preset_applied': 'プリセット適用記録',
    'health_baseline_created': '健全性ベースライン',
    'health_check_completed': '健全性チェック',
    'av_sync_recorded': 'AV同期記録',
    'other_authority': 'その他の権威記録',
}

#: Evidence typing of a timeline event (#1017): ``'evidence'`` is a row
#: projected from surviving canonical authority; ``'assumption'`` is an
#: operator-attested record (recorded as applied/as-built by a person,
#: not measured); ``'historical'`` is a non-current row — detached or
#: inherited; ``'failed'`` is a terminal rejection; ``'note'`` is a user
#: メモ, which is documentation and is never presented as canonical
#: evidence.
ActivityEvidenceClass = Literal[
    'evidence', 'assumption', 'historical', 'failed', 'note'
]

_EVENT_EVIDENCE_CLASS: dict[str, ActivityEvidenceClass] = {
    'project_created': 'evidence',
    'project_note': 'note',
    'scene_revision_saved': 'evidence',
    'scene_revision_labeled': 'evidence',
    'system_variant_proposed': 'evidence',
    'system_variant_applied': 'evidence',
    # Operator-confirmed placement — a person attested the variant is
    # installed; no device or measurement verified it.
    'system_variant_as_built': 'assumption',
    'system_variant_measured': 'evidence',
    'capture_staged': 'evidence',
    'capture_promoted': 'evidence',
    'capture_superseded': 'historical',
    'capture_deferred': 'evidence',
    'capture_rejected': 'failed',
    'measurement_imported': 'evidence',
    'calibration_plan_created': 'evidence',
    'calibration_exported': 'evidence',
    # 'user_applied' lifecycle state — operator assertion, not a verified
    # device state.
    'calibration_applied': 'assumption',
    'calibration_remeasured': 'evidence',
    'calibration_validated': 'evidence',
    'design_checkpoint_created': 'evidence',
    'design_checkpoint_restored': 'evidence',
    'operating_preset_created': 'evidence',
    # 「実機へ適用と記録」 — recorded by operator assertion.
    'operating_preset_applied': 'assumption',
    'health_baseline_created': 'evidence',
    'health_check_completed': 'evidence',
    'av_sync_recorded': 'evidence',
    'other_authority': 'evidence',
}

_EVIDENCE_CLASS_LABELS: dict[ActivityEvidenceClass, str] = {
    'evidence': '証跡',
    'assumption': '申告記録',
    'historical': '履歴（非現行）',
    'failed': '失敗・却下',
    'note': 'メモ',
}

#: One-line honest gloss under the class label — メモ is explicitly
#: marked as documentation, never canonical evidence (#1017).
_EVIDENCE_CLASS_NOTES: dict[ActivityEvidenceClass, str] = {
    'evidence': '正準権威から投影された記録です。',
    'assumption': '操作者が記録した申告です。実測値としては扱いません。',
    'historical': '現在の状態を表さない過去・継承の記録です。',
    'failed': '失敗・却下として記録された出来事です。',
    'note': 'メモはドキュメントであり、正準の証拠としては扱いません。',
}


def event_evidence_class(event: object) -> ActivityEvidenceClass:
    """Evidence typing of one timeline event (#1017) — read-only.

    ``inherited`` rows and detached scene revisions are ``'historical'``
    regardless of kind: they describe a state that is not the current
    project. Everything else follows the closed kind map; an unknown
    kind falls back to ``'evidence'`` only when the row is a normal
    projected authority event.
    """

    if getattr(event, 'inherited', False):
        return 'historical'
    kind = getattr(event, 'kind', 'other_authority')
    # ``_scene_events`` marks detached (non-head) revisions via their
    # detail text — that is the only existing detached marker on the
    # projected event.
    if kind == 'scene_revision_saved' and (
        getattr(event, 'detail', None) == '非ヘッド履歴'
    ):
        return 'historical'
    return _EVENT_EVIDENCE_CLASS.get(kind, 'evidence')


def _event_timestamp(value: str) -> str:
    """Normalize an ISO-8601 UTC timestamp for lexicographic comparison.

    ``datetime.fromisoformat`` round-trips the stored value; unparseable
    stamps pass through so a filter never silently drops a row it cannot
    interpret.
    """
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return value


class ActivityPage(QWidget):
    """Activity: app operations, the projected project timeline, revisions.

    Sections (all read-mostly) state their scope in their headings and
    follow the page-level 「このプロジェクト / 全プロジェクト」 selector:

    * ``operations`` — live/recent :class:`ApplicationOperation` rows split
      into this-project rows (``project_ref`` match) and a separate
      app-global/other-project section,
    * ``timeline`` — the canonical ``CadProjectActivityService`` projection
      (kind/date-range/search filters, newest first). The latest 50 rows
      are the initial view and さらに読み込む walks deeper history via a
      ``(occurred_at_utc, event_id)`` cursor; selecting a row fills a
      read-only detail inspector typed by evidence class (#1017), and a
      row's nav URI deep link opens on double-click,
    * ``revisions`` — the persisted scene-revision ledger, scoped in SQL
      by ``document_id`` with keyset (cursor) paging so no fixed cap can
      hide the current project's past (#1023, #1017).

    ``list_revisions`` / ``count_revisions`` / ``list_events`` all take
    the effective document id — ``None`` requests the explicit global
    merge. ``list_revisions`` is the cursor reader:
    ``(document_id, limit, after) -> (rows, next_cursor)`` where
    ``after`` is the opaque cursor returned by the previous call (``None``
    for the newest page) and ``next_cursor`` is ``None`` once history is
    exhausted (#1017).
    """

    def __init__(
        self,
        list_revisions: Callable[
            [str | None, int, str | None], tuple[tuple, str | None]
        ],
        *,
        count_revisions: Callable[[str | None], int] | None = None,
        list_operations: Callable[[], tuple] | None = None,
        list_events: Callable[[str | None], tuple] | None = None,
        open_link: Callable[[str], bool] | None = None,
        cancel_operation: Callable[[str], bool] | None = None,
        retry_operation: Callable[[str], bool] | None = None,
        document_id: str | None = None,
        project_refs: Iterable[str] = (),
        document_label: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_revisions = list_revisions
        self._count_revisions = count_revisions
        self._list_operations = list_operations
        self._list_events = list_events
        self._open_link = open_link
        self._cancel_operation = cancel_operation
        self._retry_operation = retry_operation
        # Live snapshot lookup for row activation/action wiring (#974) —
        # refreshed alongside the operations tables.
        self._operations_by_id: dict[str, object] = {}
        self._document_id = document_id
        self._document_label = document_label
        # An operation's ``project_ref`` is a free-form ref: match against
        # every identifier this project answers to (document id, canonical
        # project id, display name) so scoped ops land in the project
        # section and never leak into the global one.
        self._project_refs = {
            str(ref)
            for ref in (document_id, *project_refs)
            if ref
        }
        # Load-more/cursor state (#1017): ``_events_shown`` is the visible
        # window length over the filtered list and ``_events_cursor`` the
        # (occurred_at_utc, event_id) key of its last row — the continue
        # point even if new events arrive above it. ``_revisions_after``
        # is the opaque SQL keyset cursor; ``_revisions_shown`` the window.
        self._events_shown = _EVENT_PAGE_SIZE
        self._events_cursor: tuple[str, str] | None = None
        self._revisions_shown = _REVISION_PAGE_SIZE
        self._revisions_after: str | None = None
        # Per-refresh projection cache (#1017): filter/search/paging re-use
        # one rebuild; only an external refresh() re-derives the timeline.
        self._events_cache: tuple | None = None
        self._events_cache_doc: str | None | object = _CACHE_MISS
        self._displayed_events: list = []
        # The page hosts three stacked sections (operations, timeline,
        # revisions) whose natural height exceeds a 768px screen. Build
        # the body inside a scroll area so the mount's minimumSizeHint
        # stays small instead of inflating the whole shell (QStackedWidget
        # takes the max over children).
        body = QWidget(self)
        layout = _page_layout(
            body,
            "アクティビティ",
            "実行中の操作・プロジェクトの記録（最新順）です。"
            "タイムラインの行をダブルクリックすると、"
            "その出来事が起きた画面へ移動します。",
        )

        # -- scope selector -------------------------------------------------
        scope_row = QHBoxLayout()
        scope_label = QLabel("表示範囲:")
        scope_row.addWidget(scope_label)
        self.scope_combo = QComboBox()
        self.scope_combo.setToolTip(
            "このページに表示する履歴の範囲です。各セクションの見出しにも範囲が表示されます。"
        )
        self.scope_combo.addItem("このプロジェクト", "project")
        self.scope_combo.addItem("全プロジェクト", "global")
        if document_id is None:
            # No bound project — the only honest listing is the global one.
            self.scope_combo.setCurrentIndex(1)
            self.scope_combo.setEnabled(False)
        self.scope_combo.currentIndexChanged.connect(
            self._on_scope_changed
        )
        scope_row.addWidget(self.scope_combo)
        scope_row.addStretch(1)
        layout.addLayout(scope_row)

        # -- operations -----------------------------------------------------
        if self._list_operations is not None:
            self.operations_heading = QLabel()
            set_typography_role(
                self.operations_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(self.operations_heading)
            self.operations_table = self._operations_table()
            layout.addWidget(self.operations_table, 1)
            self.other_operations_heading = QLabel()
            set_typography_role(
                self.other_operations_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(self.other_operations_heading)
            self.other_operations_table = self._operations_table(
                "このプロジェクト以外・アプリ全体の操作の一覧です。"
            )
            layout.addWidget(self.other_operations_table, 1)
        else:
            self.operations_heading = None
            self.operations_table = None
            self.other_operations_heading = None
            self.other_operations_table = None

        # -- timeline ---------------------------------------------------------
        if self._list_events is not None:
            self.timeline_heading = QLabel()
            set_typography_role(
                self.timeline_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(self.timeline_heading)
            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("種類:"))
            self.kind_combo = QComboBox()
            self.kind_combo.setToolTip("表示する出来事の種類です。")
            for label, kinds in _EVENT_KIND_GROUPS:
                self.kind_combo.addItem(label, kinds)
            self.kind_combo.currentIndexChanged.connect(
                self._on_event_filter_changed
            )
            filter_row.addWidget(self.kind_combo)
            filter_row.addWidget(QLabel("期間:"))
            self.range_combo = QComboBox()
            self.range_combo.setToolTip("表示する出来事の期間です。")
            for label, days in _EVENT_RANGE_OPTIONS:
                self.range_combo.addItem(label, days)
            self.range_combo.currentIndexChanged.connect(
                self._on_event_filter_changed
            )
            filter_row.addWidget(self.range_combo)
            # Arbitrary start/end bounds (#1017): inclusive UTC calendar
            # days. Only enabled while 期間を指定 is selected.
            self.range_start_edit = QDateEdit()
            self.range_start_edit.setCalendarPopup(True)
            self.range_start_edit.setDisplayFormat("yyyy/MM/dd")
            self.range_start_edit.setDate(
                QDate.currentDate().addDays(-30)
            )
            self.range_start_edit.setToolTip(
                "期間の開始日です（この日を含みます）。"
            )
            self.range_start_edit.setAccessibleName("期間の開始日")
            self.range_start_edit.dateChanged.connect(
                self._on_event_filter_changed
            )
            filter_row.addWidget(self.range_start_edit)
            filter_row.addWidget(QLabel("〜"))
            self.range_end_edit = QDateEdit()
            self.range_end_edit.setCalendarPopup(True)
            self.range_end_edit.setDisplayFormat("yyyy/MM/dd")
            self.range_end_edit.setDate(QDate.currentDate())
            self.range_end_edit.setToolTip(
                "期間の終了日です（この日を含みます）。"
            )
            self.range_end_edit.setAccessibleName("期間の終了日")
            self.range_end_edit.dateChanged.connect(
                self._on_event_filter_changed
            )
            filter_row.addWidget(self.range_end_edit)
            self._sync_range_edit_visibility()
            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText("タイムラインを検索")
            self.search_edit.setAccessibleName("タイムラインを検索")
            self.search_edit.setClearButtonEnabled(True)
            self.search_edit.setToolTip(
                "内容・詳細・プロジェクト名で絞り込みます。"
            )
            self.search_edit.textChanged.connect(
                self._on_event_filter_changed
            )
            filter_row.addWidget(self.search_edit, 1)
            layout.addLayout(filter_row)
            self.events_table = QTableWidget(0, 4)
            self.events_table.setToolTip(
                "プロジェクトで起きた出来事の記録です。行をダブルクリックすると該当画面へ移動できます。"
            )
            self.events_table.setHorizontalHeaderLabels(
                ("時刻", "プロジェクト", "内容", "詳細")
            )
            for _col, _tip in enumerate((
                "記録された時刻（新しい順）",
                "出来事が起きたプロジェクト",
                "プロジェクトで起きた出来事の概要",
                "対象の詳細（ダブルクリックで該当画面へ移動できます）",
            )):
                self.events_table.horizontalHeaderItem(_col).setToolTip(_tip)
            self.events_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Stretch
            )
            self.events_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            self.events_table.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.events_table.setSelectionBehavior(
                QTableWidget.SelectionBehavior.SelectRows
            )
            self.events_table.itemActivated.connect(self._activate_event)
            self.events_table.itemDoubleClicked.connect(self._activate_event)
            self.events_table.itemSelectionChanged.connect(
                self._on_event_selection_changed
            )
            self.events_table.setAccessibleName("タイムライン一覧")
            layout.addWidget(self.events_table, 1)
            self.events_pager = self._more_pager(self._events_load_more)
            layout.addLayout(self.events_pager[0])
            self.event_inspector = self._build_event_inspector()
            layout.addWidget(self.event_inspector)
        else:
            self.timeline_heading = None
            self.events_table = None
            self.kind_combo = None
            self.range_combo = None
            self.range_start_edit = None
            self.range_end_edit = None
            self.search_edit = None
            self.events_pager = None
            self.event_inspector = None

        # -- revisions --------------------------------------------------------
        self.revisions_heading = QLabel()
        set_typography_role(self.revisions_heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(self.revisions_heading)
        self.table = QTableWidget(0, 3)
        self.table.setToolTip(
            "保存された版（リビジョン）の履歴一覧です。"
            "行をダブルクリックするとその版の履歴画面へ移動します。"
        )
        self.table.setHorizontalHeaderLabels(("時刻", "プロジェクト", "リビジョン"))
        for _col, _tip in enumerate((
            "版（リビジョン）が保存された時刻",
            "対象のプロジェクト名",
            "保存された版の識別子（ダブルクリックで履歴・差分比較画面へ移動できます）",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.itemActivated.connect(self._activate_revision)
        self.table.itemDoubleClicked.connect(self._activate_revision)
        self.table.setAccessibleName("リビジョン履歴一覧")
        layout.addWidget(self.table, 1)
        self.revisions_pager = self._more_pager(
            self._revisions_load_more
        )
        layout.addLayout(self.revisions_pager[0])
        self.empty_label = QLabel(
            "まだ記録はありません。保存や昇格を行うとここに表示されます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        scroll = QScrollArea(self)
        scroll.setObjectName("activityScroll")
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setAccessibleName("アクティビティ一覧")
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self._sync_scope_headings()
        self.refresh()

    # -- scope ---------------------------------------------------------------

    def _scope(self) -> str:
        return str(self.scope_combo.currentData() or "project")

    def _scope_document_id(self) -> str | None:
        """Effective listing scope: the document id, or ``None`` = global."""

        if self._scope() == "project":
            return self._document_id
        return None

    def _project_name(self) -> str | None:
        if self._document_id is None:
            return None
        if self._document_label is not None:
            return self._document_label(self._document_id)
        return self._document_id

    def _document_name(self, document_id: str) -> str:
        if self._document_label is not None:
            return self._document_label(document_id)
        return document_id

    def _scope_label(self) -> str:
        if self._scope() == "project":
            name = self._project_name()
            return (
                f"このプロジェクト（{name}）" if name else "このプロジェクト"
            )
        return "全プロジェクト"

    def _sync_scope_headings(self) -> None:
        scope = self._scope_label()
        if self.operations_heading is not None:
            self.operations_heading.setText(f"操作（{scope}）")
        if self.other_operations_heading is not None:
            self.other_operations_heading.setText(
                "その他の操作（アプリ全体・他のプロジェクト）"
            )
        if self.timeline_heading is not None:
            self.timeline_heading.setText(
                f"プロジェクトタイムライン（{scope}）"
            )
        self.revisions_heading.setText(f"リビジョン履歴（{scope}）")
        if self.events_table is not None:
            # The project column only carries information in the global view.
            self.events_table.setColumnHidden(1, self._scope() == "project")

    def _on_scope_changed(self, _index: int) -> None:
        self._events_shown = _EVENT_PAGE_SIZE
        self._events_cursor = None
        self._revisions_shown = _REVISION_PAGE_SIZE
        self._revisions_after = None
        self._events_cache = None
        self._events_cache_doc = _CACHE_MISS
        self._sync_scope_headings()
        self.refresh()

    def _preserve_selection(self, refresh: Callable[[], None]) -> None:
        """Re-render through ``refresh`` without losing the selected row.

        Selection is re-bound by (event_id / revision_id / document_id),
        never by row index (#1023).
        """

        keys = self._selected_keys()
        refresh()
        self._restore_selection(keys)

    def _on_event_filter_changed(self, *_args: object) -> None:
        self._sync_range_edit_visibility()
        self._events_shown = _EVENT_PAGE_SIZE
        self._events_cursor = None
        self._preserve_selection(self._refresh_events)

    def _sync_range_edit_visibility(self) -> None:
        """The arbitrary bounds only edit while 期間を指定 is selected."""

        if self.range_combo is None:
            return
        custom = self.range_combo.currentData() == _CUSTOM_RANGE
        for edit in (self.range_start_edit, self.range_end_edit):
            if edit is not None:
                edit.setEnabled(custom)

    # -- load-more paging (cursor / さらに読み込む, #1017) -----------------------

    def _more_pager(
        self, on_more: Callable[[], None]
    ) -> tuple[QHBoxLayout, QPushButton, QLabel]:
        """(row, load-more button, count label) — append-mode paging.

        The latest page is the initial view; さらに読み込む extends the
        window into deeper history instead of replacing it.
        """

        row = QHBoxLayout()
        more_button = QPushButton("さらに読み込む")
        more_button.setToolTip(
            "古い記録を50件ずつ追加で表示します。"
        )
        more_button.clicked.connect(on_more)
        count_label = QLabel()
        set_typography_role(count_label, TypographyRole.SECONDARY)
        row.addWidget(more_button)
        row.addStretch(1)
        row.addWidget(count_label)
        return row, more_button, count_label

    def _update_more_pager(
        self,
        pager: tuple[QHBoxLayout, QPushButton, QLabel],
        *,
        total: int,
        shown: int,
    ) -> None:
        _row, more_button, count_label = pager
        first = 1 if shown else 0
        count_label.setText(f"全{total}件 · {first}–{shown}件を表示")
        more_button.setEnabled(shown < total)

    def _events_load_more(self) -> None:
        """Extend the timeline window by one page from the key cursor.

        ``_events_cursor`` (occurred_at_utc, event_id of the last shown
        row) is the continue point: events arriving at the top while the
        operator browses shift the window's start, never skip or
        duplicate a row. A vanished cursor row falls back to the current
        window length (#1017).
        """

        events = self._filtered_events()
        if self._events_cursor is not None:
            position = next(
                (
                    index
                    for index, event in enumerate(events)
                    if (
                        event.occurred_at_utc,
                        event.event_id,
                    )
                    == self._events_cursor
                ),
                None,
            )
            start = position + 1 if position is not None else self._events_shown
        else:
            start = self._events_shown
        self._events_shown = start + _EVENT_PAGE_SIZE
        self._preserve_selection(self._refresh_events)

    def _revisions_load_more(self) -> None:
        """Append one SQL keyset page after ``_revisions_after`` (#1017)."""

        if self._revisions_after is None:
            return
        document_id = self._scope_document_id()
        rows, self._revisions_after = self._list_revisions(
            document_id, _REVISION_PAGE_SIZE, self._revisions_after
        )
        self._revisions_shown += len(rows)
        self._append_revision_rows(rows)
        total = (
            self._count_revisions(document_id)
            if self._count_revisions is not None
            else self.table.rowCount()
            + (1 if self._revisions_after is not None else 0)
        )
        self._update_more_pager(
            self.revisions_pager,
            total=total,
            shown=self.table.rowCount(),
        )

    def _append_revision_rows(self, rows: Iterable[tuple]) -> None:
        for created_at, row_document_id, revision_id in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    created_at,
                    self._document_name(row_document_id),
                    revision_id,
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole,
                        self._revision_link(row_document_id, revision_id),
                    )
                elif column == 1:
                    cell.setData(
                        Qt.ItemDataRole.UserRole, row_document_id
                    )
                elif column == 2:
                    cell.setData(Qt.ItemDataRole.UserRole, revision_id)
                self.table.setItem(row, column, cell)

    # -- operations ---------------------------------------------------------------

    def _operations_table(self, tooltip: str | None = None) -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setToolTip(
            tooltip
            or "実行中・実行済みの操作（バックアップ・復元など）の一覧です。"
            "ダブルクリックでその処理を始めた画面へ戻れます。"
        )
        table.setHorizontalHeaderLabels(
            ("状態", "操作", "進捗", "更新時刻", "対応")
        )
        for _col, _tip in enumerate((
            "操作の進行状態（実行中・完了・失敗など）",
            "行われた操作の種類（バックアップ・復元・インポートなど）",
            "実際に報告された進捗（割合・段階・件数のみ）",
            "状態が最後に更新された時刻",
            "中止・再試行・発生元画面への移動",
        )):
            table.horizontalHeaderItem(_col).setToolTip(_tip)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        table.itemDoubleClicked.connect(self._activate_operation)
        return table

    def _operation_matches_project(self, operation: object) -> bool:
        ref = getattr(operation, "project_ref", None)
        if not ref:
            # Unscoped/global operations never pretend to be project rows.
            return False
        return str(ref) in self._project_refs

    def _fill_operations_table(
        self, table: QTableWidget, operations: Iterable[object]
    ) -> None:
        for operation in operations:
            row = table.rowCount()
            table.insertRow(row)
            state = getattr(operation.state, "value", operation.state)
            detail = (
                operation.error_summary
                or operation.result_summary
                or operation.operation_kind
            )
            for column, value in enumerate(
                (
                    operation_state_label(state),
                    f"{operation.title} — {detail}",
                    operation_progress_text(
                        getattr(operation, "progress", None)
                    ),
                    operation.updated_at,
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(
                        Qt.ItemDataRole.UserRole, operation.operation_id
                    )
                table.setItem(row, column, cell)
            self._operations_by_id[operation.operation_id] = operation
            actions = self._operation_actions(operation)
            if actions is not None:
                # An item under the cell widget keeps row-hit-testing and
                # keyboard activation working on the covered column.
                table.setItem(row, 4, QTableWidgetItem(""))
                table.setCellWidget(row, 4, actions)

    def _activate_operation(self, item: QTableWidgetItem) -> None:
        """Double-click: return to the workspace that launched the op (#974)."""

        table = item.tableWidget()
        if table is None or self._open_link is None:
            return
        cell = table.item(item.row(), 0)
        if cell is None:
            return
        operation = self._operations_by_id.get(
            cell.data(Qt.ItemDataRole.UserRole)
        )
        deep_link = getattr(operation, "deep_link", None)
        if deep_link is not None:
            self._open_link(deep_link.as_uri())

    def _operation_actions(self, operation: object) -> QWidget | None:
        """Per-row affordances: cooperative cancel, policy-respecting retry,
        and the originating deep link (#974)."""

        buttons: list[QPushButton] = []
        operation_id = operation.operation_id
        if (
            getattr(operation, "can_cancel_now", False)
            and self._cancel_operation is not None
        ):
            cancel = QPushButton("中止")
            cancel.setAccessibleName(f"{operation.title} を中止")
            cancel.setToolTip(
                "実行中の処理へ協調キャンセルを要求します。"
                "キャンセルされるまで結果は確定しません。"
            )
            cancel.clicked.connect(
                lambda _checked=False, op_id=operation_id:
                    self._cancel_operation(op_id)
            )
            buttons.append(cancel)
        if not getattr(operation, "is_active", True):
            policy = getattr(operation, "retry_policy", None)
            policy_value = getattr(policy, "value", policy)
            if (
                policy_value == RetryPolicy.SAFE_NEW_ATTEMPT.value
                and self._retry_operation is not None
            ):
                retry = QPushButton("再試行")
                retry.setAccessibleName(f"{operation.title} を再試行")
                retry.setToolTip(
                    "同じ入力に対する新しい試行として安全に再実行します。"
                )
                retry.clicked.connect(
                    lambda _checked=False, op_id=operation_id:
                        self._retry_operation(op_id)
                )
                buttons.append(retry)
            elif (
                policy_value == RetryPolicy.UNSAFE.value
                and self._retry_operation is not None
            ):
                retry = QPushButton("再試行（要確認）")
                retry.setAccessibleName(
                    f"{operation.title} を確認のうえ再試行"
                )
                retry.setToolTip(
                    "適用・上書きを伴う再実行です。確認のうえで実行します。"
                )
                retry.clicked.connect(
                    lambda _checked=False, op_id=operation_id:
                        self._confirm_retry(op_id)
                )
                buttons.append(retry)
        deep_link = getattr(operation, "deep_link", None)
        if deep_link is not None and self._open_link is not None:
            open_button = QPushButton("開く")
            open_button.setAccessibleName(
                f"{operation.title} の発生元画面を開く"
            )
            open_button.setToolTip("この処理を始めた画面へ移動します。")
            uri = deep_link.as_uri()
            open_button.clicked.connect(
                lambda _checked=False, link=uri: self._open_link(link)
            )
            buttons.append(open_button)
        if not buttons:
            return None
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(4)
        for button in buttons:
            row.addWidget(button)
        return box

    def _confirm_retry(self, operation_id: str) -> None:
        """UNSAFE retry policy: explicit re-authorization before re-run (#974)."""

        operation = self._operations_by_id.get(operation_id)
        title = getattr(operation, "title", operation_id)
        answer = QMessageBox.question(
            self,
            "再実行の確認",
            f"「{title}」を再実行します。適用・上書きを伴う可能性があります。"
            "続行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if (
            answer == QMessageBox.StandardButton.Yes
            and self._retry_operation is not None
        ):
            self._retry_operation(operation_id)

    def _refresh_operations(self) -> None:
        if self.operations_table is None or self._list_operations is None:
            return
        self._operations_by_id.clear()
        operations = tuple(self._list_operations())
        project_ops = [
            op for op in operations if self._operation_matches_project(op)
        ]
        other_ops = [
            op for op in operations if not self._operation_matches_project(op)
        ]
        self.operations_table.setRowCount(0)
        self._fill_operations_table(self.operations_table, project_ops)
        self.other_operations_table.setRowCount(0)
        self._fill_operations_table(self.other_operations_table, other_ops)

    # -- timeline -------------------------------------------------------------------

    def _scoped_events(self) -> tuple:
        """The merged projection for the current scope — cached per scope.

        Rebuilding the canonical projection walks every authority store;
        filter/search/paging share one rebuild and ``refresh()`` is the
        only point that re-derives it (#1017).
        """

        if self._list_events is None:
            return ()
        document_id = self._scope_document_id()
        if (
            self._events_cache is None
            or self._events_cache_doc != document_id
        ):
            self._events_cache = tuple(self._list_events(document_id))
            self._events_cache_doc = document_id
        return self._events_cache

    def _custom_range_bounds(self) -> tuple[str, str] | None:
        """Inclusive ISO bounds for 期間を指定 — (start, end-of-day) UTC.

        ``None`` while a preset is selected. A start after the end is
        applied literally: no event can satisfy it, so the filter returns
        an empty list rather than silently reordering the operator's
        bounds (#1017).
        """

        if (
            self.range_combo is None
            or self.range_combo.currentData() != _CUSTOM_RANGE
        ):
            return None
        start = self.range_start_edit.date()
        end = self.range_end_edit.date()
        start_iso = datetime(
            start.year(), start.month(), start.day(), tzinfo=timezone.utc
        ).isoformat()
        end_iso = datetime(
            end.year(),
            end.month(),
            end.day(),
            23,
            59,
            59,
            999999,
            tzinfo=timezone.utc,
        ).isoformat()
        return start_iso, end_iso

    def _filtered_events(self) -> tuple:
        if self._list_events is None:
            return ()
        events = list(self._scoped_events())
        if self.kind_combo is not None:
            kinds = self.kind_combo.currentData()
            if kinds is not None:
                events = [
                    event for event in events if event.kind in kinds
                ]
        if self.range_combo is not None:
            option = self.range_combo.currentData()
            if option == _CUSTOM_RANGE:
                bounds = self._custom_range_bounds()
                if bounds is not None:
                    start_iso, end_iso = bounds
                    events = [
                        event
                        for event in events
                        if start_iso
                        <= _event_timestamp(event.occurred_at_utc)
                        <= end_iso
                    ]
            elif option is not None:
                now = datetime.now(timezone.utc)
                if option == 0:
                    cutoff = now.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                else:
                    cutoff = now - timedelta(days=option)
                cutoff_iso = cutoff.isoformat()
                events = [
                    event
                    for event in events
                    if _event_timestamp(event.occurred_at_utc) >= cutoff_iso
                ]
        if self.search_edit is not None:
            needle = self.search_edit.text().strip().lower()
            if needle:
                events = [
                    event
                    for event in events
                    if needle in event.title.lower()
                    or needle in (event.detail or "").lower()
                    or needle in event.event_id.lower()
                    or needle
                    in self._document_name(event.document_id).lower()
                ]
        return tuple(events)

    def _refresh_events(self) -> None:
        if self.events_table is None:
            return
        events = self._filtered_events()
        total = len(events)
        shown = min(self._events_shown, total)
        page_events = events[:shown]
        self.events_table.setRowCount(0)
        self._displayed_events = list(page_events)
        for event in page_events:
            row = self.events_table.rowCount()
            self.events_table.insertRow(row)
            for column, value in enumerate(
                (
                    event.occurred_at_utc,
                    self._document_name(event.document_id),
                    event.title,
                    event.detail or "",
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    if event.deep_link is not None:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, event.deep_link
                        )
                    cell.setData(
                        Qt.ItemDataRole.UserRole + 1, event.event_id
                    )
                    cell.setData(
                        Qt.ItemDataRole.UserRole + 2, event.document_id
                    )
                self.events_table.setItem(row, column, cell)
        self._events_cursor = (
            (
                page_events[-1].occurred_at_utc,
                page_events[-1].event_id,
            )
            if page_events
            else None
        )
        self._update_more_pager(
            self.events_pager, total=total, shown=len(page_events)
        )
        self._update_event_inspector()

    def _activate_event(self, item: QTableWidgetItem) -> None:
        anchor = self.events_table.item(item.row(), 0)
        link = anchor.data(Qt.ItemDataRole.UserRole) if anchor is not None else None
        if not link or self._open_link is None:
            return
        uri = str(link)
        # A row whose document differs from the bound project must open in
        # ITS project: stamp the event's document id onto the target so
        # activation routes through the guarded project switch instead of
        # resolving against whichever project happens to be current (#1023).
        document_id = anchor.data(Qt.ItemDataRole.UserRole + 2)
        if document_id and str(document_id) != self._document_id:
            try:
                target = navigation_target_from_uri(uri)
            except ValueError:
                target = None
            if target is not None and target.project_id is None:
                uri = replace(
                    target, project_id=str(document_id)
                ).as_uri()
        self._open_link(uri)

    # -- revisions -------------------------------------------------------------------

    def _revision_link(self, document_id: str, revision_id: str) -> str:
        """Deep link to the revision's history/diff surface (#1023).

        ``project_id`` carries the row's own document so activating a row
        from another project routes through the guarded project switch —
        a foreign revision is never activated silently in the current one.
        """

        return NavigationTarget(
            kind=NavigationTargetKind.SCENE_REVISION,
            object_ids=(revision_id,),
            project_id=document_id,
            preferred_destination=WorkspaceId.ROOM,
            preferred_section="history",
        ).as_uri()

    def _activate_revision(self, item: QTableWidgetItem) -> None:
        anchor = self.table.item(item.row(), 0)
        link = anchor.data(Qt.ItemDataRole.UserRole) if anchor is not None else None
        if link and self._open_link is not None:
            self._open_link(str(link))

    def _refresh_revisions(self) -> None:
        document_id = self._scope_document_id()
        # One bounded keyset query re-reads the whole visible window; the
        # returned cursor is where さらに読み込む continues (#1017).
        rows, self._revisions_after = self._list_revisions(
            document_id, self._revisions_shown, None
        )
        total = (
            self._count_revisions(document_id)
            if self._count_revisions is not None
            else len(rows) + (1 if self._revisions_after is not None else 0)
        )
        self.table.setRowCount(0)
        self.empty_label.setVisible(not rows)
        self._append_revision_rows(rows)
        self._update_more_pager(
            self.revisions_pager,
            total=total,
            shown=self.table.rowCount(),
        )

    # -- detail inspector (#1017) ------------------------------------------------------

    def _build_event_inspector(self) -> QFrame:
        """Read-only detail panel for the selected timeline row.

        Shows the event's evidence class (証跡/申告記録/履歴/失敗・却下/
        メモ), its source authority ids and any correlated refs. メモ
        rows are labeled as documentation — never canonical evidence.
        Every value is selectable text so keyboard/screen-reader users
        reach the same content.
        """

        frame = QFrame()
        frame.setAccessibleName("選択した出来事の詳細")
        frame.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 4, 8, 8)
        heading = QLabel("出来事の詳細")
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        box.addWidget(heading)

        def value_label(name: str) -> QLabel:
            label = QLabel()
            label.setAccessibleName(name)
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            box.addWidget(label)
            return label

        self._inspector_summary = value_label("出来事の時刻・プロジェクト・種類")
        self._inspector_class = value_label("出来事の区分")
        self._inspector_title = value_label("出来事の内容")
        self._inspector_detail = value_label("出来事の詳細")
        self._inspector_sources = value_label("出来事のソース権威")
        self._inspector_correlation = value_label("出来事の相関")
        self._inspector_id = value_label("出来事ID")
        self._inspector_note = value_label("区分の説明")
        set_typography_role(self._inspector_note, TypographyRole.SECONDARY)
        self._clear_event_inspector()
        return frame

    def _clear_event_inspector(self) -> None:
        placeholder = "行を選ぶと詳細を表示します。"
        self._inspector_summary.setText(placeholder)
        for label in (
            self._inspector_class,
            self._inspector_title,
            self._inspector_detail,
            self._inspector_sources,
            self._inspector_correlation,
            self._inspector_id,
            self._inspector_note,
        ):
            label.setText("")

    def _on_event_selection_changed(self) -> None:
        self._update_event_inspector()

    def _selected_row(self, table: QTableWidget) -> int:
        """Row of the table's actual selection, or ``-1``.

        ``currentRow`` outlives a deselect (Ctrl+click/clearSelection
        leaves the current cell behind), so anything that treats the
        selection as operator intent — the inspector, selection
        rebinding — reads the selection model instead.
        """

        selection = table.selectionModel()
        rows = selection.selectedRows() if selection is not None else []
        return rows[0].row() if rows else -1

    def _selected_event(self):
        """The event object for the selected timeline row, or ``None``.

        Selection — not the current cell — drives the inspector, so a
        cleared selection empties it even though ``currentRow`` stays
        put after ``clearSelection``.
        """

        if self.events_table is None:
            return None
        row = self._selected_row(self.events_table)
        if 0 <= row < len(self._displayed_events):
            return self._displayed_events[row]
        return None

    def _update_event_inspector(self) -> None:
        if self.event_inspector is None:
            return
        event = self._selected_event()
        if event is None:
            self._clear_event_inspector()
            return
        kind_label = _EVENT_KIND_LABELS.get(event.kind, event.kind)
        evidence_class = event_evidence_class(event)
        self._inspector_summary.setText(
            f"時刻: {event.occurred_at_utc}　プロジェクト: "
            f"{self._document_name(event.document_id)}　種類: {kind_label}"
        )
        self._inspector_class.setText(
            f"区分: {_EVIDENCE_CLASS_LABELS[evidence_class]}"
        )
        self._inspector_title.setText(f"内容: {event.title}")
        self._inspector_detail.setText(
            f"詳細: {event.detail or '—'}"
        )
        refs = tuple(getattr(event, 'source_refs', ()))
        if refs:
            self._inspector_sources.setText(
                "ソース権威:\n"
                + "\n".join(
                    f"  {ref.kind}: {ref.ref_id}"
                    + (
                        f"（sha256 {ref.ref_sha256[:12]}…）"
                        if ref.ref_sha256
                        else ""
                    )
                    for ref in refs[:1]
                )
            )
            self._inspector_correlation.setText(
                "相関:\n"
                + "\n".join(
                    f"  {ref.kind}: {ref.ref_id}"
                    + (
                        f"（sha256 {ref.ref_sha256[:12]}…）"
                        if ref.ref_sha256
                        else ""
                    )
                    for ref in refs[1:]
                )
                if len(refs) > 1
                else "相関: なし"
            )
        else:
            self._inspector_sources.setText("ソース権威: なし")
            self._inspector_correlation.setText("相関: なし")
        inherited = "（継承元の記録）" if event.inherited else ""
        self._inspector_id.setText(
            f"event_id: {event.event_id}{inherited}"
        )
        self._inspector_note.setText(
            _EVIDENCE_CLASS_NOTES[evidence_class]
        )

    # -- selection stability ---------------------------------------------------------

    def _selected_keys(self) -> dict[str, tuple]:
        """Identity of the selected row per table — (kind ids + document).

        Selection survives filter/scope/refresh by matching the same
        authority ids, never the row index (#1023).
        """

        keys: dict[str, tuple] = {}
        if self.events_table is not None:
            row = self._selected_row(self.events_table)
            cell = self.events_table.item(row, 0) if row >= 0 else None
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole + 1):
                keys["events"] = (
                    cell.data(Qt.ItemDataRole.UserRole + 1),
                    cell.data(Qt.ItemDataRole.UserRole + 2),
                )
        row = self._selected_row(self.table)
        if row >= 0:
            document = self.table.item(row, 1)
            revision = self.table.item(row, 2)
            if document is not None and revision is not None:
                keys["revisions"] = (
                    revision.data(Qt.ItemDataRole.UserRole),
                    document.data(Qt.ItemDataRole.UserRole),
                )
        for name, table in (
            ("operations", self.operations_table),
            ("other_operations", self.other_operations_table),
        ):
            if table is None:
                continue
            row = self._selected_row(table)
            cell = table.item(row, 0) if row >= 0 else None
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole):
                keys[name] = (cell.data(Qt.ItemDataRole.UserRole),)
        return keys

    def _restore_selection(self, keys: dict[str, tuple]) -> None:
        key = keys.get("events")
        if key is not None and self.events_table is not None:
            event_id, document_id = key
            for row in range(self.events_table.rowCount()):
                cell = self.events_table.item(row, 0)
                if (
                    cell is not None
                    and cell.data(Qt.ItemDataRole.UserRole + 1) == event_id
                    and cell.data(Qt.ItemDataRole.UserRole + 2) == document_id
                ):
                    self.events_table.selectRow(row)
                    break
        key = keys.get("revisions")
        if key is not None:
            revision_id, document_id = key
            for row in range(self.table.rowCount()):
                revision = self.table.item(row, 2)
                document = self.table.item(row, 1)
                if (
                    revision is not None
                    and document is not None
                    and revision.data(Qt.ItemDataRole.UserRole) == revision_id
                    and document.data(Qt.ItemDataRole.UserRole) == document_id
                ):
                    self.table.selectRow(row)
                    break
        for name, table in (
            ("operations", self.operations_table),
            ("other_operations", self.other_operations_table),
        ):
            key = keys.get(name)
            if key is None or table is None:
                continue
            for row in range(table.rowCount()):
                cell = table.item(row, 0)
                if (
                    cell is not None
                    and cell.data(Qt.ItemDataRole.UserRole) == key[0]
                ):
                    table.selectRow(row)
                    break

    # -- refresh -----------------------------------------------------------------------

    def refresh(self) -> None:
        # The projected timeline is re-derived here only; intra-refresh
        # work (filters, search, paging, selection) reuses the cache.
        self._events_cache = None
        self._events_cache_doc = _CACHE_MISS

        def _refresh_all() -> None:
            self._refresh_operations()
            self._refresh_events()
            self._refresh_revisions()

        self._preserve_selection(_refresh_all)


#: Scope of a revision-listing query (#1023). ``'project'`` filters in SQL by
#: ``document_id``; ``'global'`` is the ONLY explicit opt-in to an
#: all-projects listing — a missing document id fails closed instead of
#: silently widening to every project in the shared app store.
RevisionListScope = Literal['project', 'global']


def _revision_scope_clause(
    scope: RevisionListScope, document_id: str | None
) -> tuple[str, tuple]:
    if scope == 'project':
        if not document_id:
            raise ValueError(
                'project scope requires a document_id — pass '
                "scope='global' explicitly for an all-projects listing"
            )
        return ' AND document_id = ?', (document_id,)
    if scope == 'global':
        return '', ()
    raise ValueError(f'unknown revision list scope: {scope!r}')


def list_recent_revisions(
    repository: SceneRepository,
    limit: int = 50,
    *,
    scope: RevisionListScope = 'project',
    document_id: str | None = None,
    offset: int = 0,
) -> tuple:
    """(created_at_utc, document_id, revision_id) rows — read-only.

    The ``document_id`` filter is applied in SQL: rows from other projects
    are never fetched and then hidden in the UI (#1023). ``offset`` +
    ``count_recent_revisions`` page through history so a fixed row cap can
    no longer bury the current project's past.
    """

    clause, params = _revision_scope_clause(scope, document_id)
    path = Path(repository.path)
    if not path.is_file():
        return ()
    try:
        with closing(repository._read()) as connection, connection:
            rows = connection.execute(
                """
                SELECT created_at_utc, document_id, revision_id
                FROM scene_revisions
                WHERE detached = 0"""
                + clause
                + """
                ORDER BY seq DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple((str(a), str(b), str(c)) for a, b, c in rows)


def count_recent_revisions(
    repository: SceneRepository,
    *,
    scope: RevisionListScope = 'project',
    document_id: str | None = None,
) -> int:
    """Total non-detached revisions in scope — drives the paging label."""

    clause, params = _revision_scope_clause(scope, document_id)
    path = Path(repository.path)
    if not path.is_file():
        return 0
    try:
        with closing(repository._read()) as connection, connection:
            row = connection.execute(
                'SELECT COUNT(*) FROM scene_revisions WHERE detached = 0'
                + clause,
                params,
            ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _decode_revision_cursor(after: str | None) -> int | None:
    """Opaque keyset cursor → seq bound; ``None`` starts at the newest.

    A malformed cursor fails closed with ``ValueError`` — a corrupted
    bookmark must never silently restart or widen the listing (#1017).
    """

    if after is None:
        return None
    if isinstance(after, str) and after.startswith('seq:'):
        try:
            return int(after[4:])
        except ValueError:
            pass
    raise ValueError(f'unknown revision page cursor: {after!r}')


def list_revisions_page(
    repository: SceneRepository,
    limit: int = 50,
    *,
    scope: RevisionListScope = 'project',
    document_id: str | None = None,
    after: str | None = None,
) -> tuple[tuple, str | None]:
    """Keyset (cursor) page over the scoped revision ledger (#1017).

    Returns ``(rows, next_cursor)`` — rows are ``(created_at_utc,
    document_id, revision_id)`` ordered newest-first, ``next_cursor`` the
    opaque ``seq:<n>`` token to pass as ``after`` for the next page, or
    ``None`` once history is exhausted. Unlike ``OFFSET`` paging the
    cursor key (``seq``) is stable under concurrent inserts: rows
    committed while the operator browses never shift the window and can
    never skip or repeat a row.
    """

    clause, params = _revision_scope_clause(scope, document_id)
    before_seq = _decode_revision_cursor(after)
    cursor_clause = ''
    cursor_params: tuple = ()
    if before_seq is not None:
        cursor_clause = ' AND seq < ?'
        cursor_params = (before_seq,)
    path = Path(repository.path)
    if not path.is_file():
        return (), None
    try:
        with closing(repository._read()) as connection, connection:
            rows = connection.execute(
                """
                SELECT created_at_utc, document_id, revision_id, seq
                FROM scene_revisions
                WHERE detached = 0"""
                + clause
                + cursor_clause
                + """
                ORDER BY seq DESC LIMIT ?
                """,
                (*params, *cursor_params, limit + 1),
            ).fetchall()
    except sqlite3.Error:
        return (), None
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = f'seq:{rows[-1][3]}' if has_more and rows else None
    return (
        tuple((str(a), str(b), str(c)) for a, b, c, _seq in rows),
        next_cursor,
    )


def list_known_document_ids(repository: SceneRepository) -> tuple:
    """Every document that owns committed revisions — read-only.

    Used by the global activity scope to merge each document's canonical
    ``CadProjectActivityService.events`` projection (#1023).
    """

    path = Path(repository.path)
    if not path.is_file():
        return ()
    try:
        with closing(repository._read()) as connection, connection:
            rows = connection.execute(
                """
                SELECT DISTINCT document_id FROM scene_revisions
                WHERE detached = 0 ORDER BY document_id ASC
                """
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(str(row[0]) for row in rows)


_LIBRARY_FAMILY_TITLES = {
    "equipment": "機材・スピーカー定義",
    "treatment": "吸音・処理材",
    "target_curve": "目標カーブ",
    "standard_profile": "基準プロファイル",
    "instrument": "測定機器",
    "material": "音響材料",
    "operating_profile": "動作プロファイル",
}

_LIBRARY_SCOPE_LABELS = {
    "builtin": "同梱",
    "user_library": "ユーザーライブラリ",
    "project_local": "プロジェクト",
    "imported_dependency": "依存として取り込み",
    "historical": "履歴",
}


class ReferenceLibraryPage(QWidget):
    """Reference library: cross-search, compare, provenance (#990).

    ``library_index`` (the #630 hub read model) drives the 横断検索・比較
    tab: one deferred-rendered results table over every authority family,
    a detail pane with exact authority identity/version/SHA/provenance/
    rights state, and a two-row compare that never merges entries. The
    区分別一覧 tab keeps the original equipment table and per-family
    sections. ``detail_resolver`` is a zero-arg factory returning the
    per-refresh record resolver, ``usage_resolver`` a zero-arg factory
    returning ``semantic_key → usage sites``, and ``open_target`` a
    navigation sink — all read-only projections; the page mutates nothing.
    """

    manage_requested = Signal()

    _RESULTS_PAGE_SIZE = 50

    def __init__(
        self,
        list_definitions: Callable[[], tuple],
        library_index=None,
        *,
        detail_resolver=None,
        usage_resolver=None,
        open_target=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_definitions = list_definitions
        self._library_index = library_index
        self._detail_resolver = detail_resolver
        self._usage_resolver = usage_resolver
        self._open_target = open_target
        self._all_rows: tuple = ()
        self._filtered_rows: list = []
        self._rows_by_key: dict[str, object] = {}
        self._shown_count = 0
        layout = _page_layout(
            self,
            "ライブラリ",
            "機材・材料・規格プロファイルの参照ライブラリです（プロジェクト共通）。",
        )
        if library_index is None:
            self._build_listing_section(layout)
        else:
            self._tabs = QTabWidget(self)
            self._tabs.setAccessibleName("ライブラリの表示切替")
            browser = QWidget(self)
            browser_layout = QVBoxLayout(browser)
            browser_layout.setContentsMargins(0, 8, 0, 0)
            browser_layout.setSpacing(8)
            self._build_browser_section(browser_layout)
            listing = QWidget(self)
            listing_layout = QVBoxLayout(listing)
            listing_layout.setContentsMargins(0, 8, 0, 0)
            listing_layout.setSpacing(8)
            self._build_listing_section(listing_layout)
            listing_layout.addStretch(1)
            self._tabs.addTab(browser, "横断検索・比較")
            self._tabs.addTab(listing, "区分別一覧")
            layout.addWidget(self._tabs, 1)
        self.refresh()

    # --- 区分別一覧 (legacy per-family sections) ------------------------

    def _build_listing_section(self, layout: QVBoxLayout) -> None:
        self.table = QTableWidget(0, 3)
        self.table.setToolTip(
            "登録済みの機材・ソース定義の一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
        )
        self.table.setAccessibleName("機材定義の一覧")
        self.table.setHorizontalHeaderLabels(("メーカー", "モデル", "バージョン"))
        for _col, _tip in enumerate((
            "機材の製造メーカー名",
            "機材のモデル・型番名",
            "登録されている定義のバージョン",
        )):
            self.table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        self.empty_label = QLabel(
            "まだ定義はありません。"
            "「機材ライブラリを管理…」で機材や素材を登録できます。",
            self,
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        manage = QPushButton("機材ライブラリを管理…")
        manage.setToolTip("機材・素材・ソース定義の登録・編集を行う管理画面を開きます")
        manage.setWhatsThis("機材・素材・ソース定義の登録・編集を行う管理画面を開きます")
        manage.setAccessibleName("機材ライブラリを管理")
        manage.clicked.connect(lambda: self.manage_requested.emit())
        layout.addWidget(manage)

        self._family_frames: dict[str, tuple[QLabel, QTableWidget]] = {}
        if self._library_index is not None:
            for family in self._library_index.families():
                header = QLabel(
                    _LIBRARY_FAMILY_TITLES.get(str(family), str(family)),
                    self,
                )
                set_typography_role(header, TypographyRole.SECTION_TITLE)
                table = QTableWidget(0, 4, self)
                table.setToolTip(
                    "この区分で登録されている項目の一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
                )
                table.setAccessibleName(
                    f"{_LIBRARY_FAMILY_TITLES.get(str(family), str(family))}の一覧"
                )
                table.setHorizontalHeaderLabels(
                    ("名前", "区分", "スコープ", "バージョン")
                )
                for _col, _tip in enumerate((
                    "登録されている項目の名前",
                    "項目の種類・区分",
                    "この項目が有効な範囲（プロジェクト共通など）",
                    "登録されている定義のバージョン",
                )):
                    table.horizontalHeaderItem(_col).setToolTip(_tip)
                table.horizontalHeader().setSectionResizeMode(
                    0, QHeaderView.ResizeMode.Stretch
                )
                table.setEditTriggers(
                    QTableWidget.EditTrigger.NoEditTriggers
                )
                table.setSelectionBehavior(
                    QTableWidget.SelectionBehavior.SelectRows
                )
                header.hide()
                table.hide()
                layout.addWidget(header)
                layout.addWidget(table)
                self._family_frames[str(family)] = (header, table)

    # --- 横断検索・比較 (issue #990) -------------------------------------

    def _filter_combo(self, name: str, tooltip: str) -> QComboBox:
        combo = QComboBox(self)
        combo.setAccessibleName(name)
        combo.setToolTip(tooltip)
        combo.setWhatsThis(tooltip)
        combo.currentIndexChanged.connect(self._on_filters_changed)
        return combo

    def _build_browser_section(self, layout: QVBoxLayout) -> None:
        filters = QHBoxLayout()
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(
            "機材名・メーカー・型番・規格・材料・出典・IDを検索"
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("ライブラリ横断検索")
        self.search_edit.setToolTip(
            "名前・メーカー・型番・役割・authority ID・規格・材料カテゴリ・"
            "出典・バージョンを横断検索します"
        )
        self.search_edit.textChanged.connect(self._on_filters_changed)
        filters.addWidget(self.search_edit, 1)
        self.family_combo = self._filter_combo(
            "種別フィルター", "機材・材料・規格などの種別で絞り込みます"
        )
        self.category_combo = self._filter_combo(
            "区分フィルター", "カテゴリ・区分で絞り込みます"
        )
        self.source_combo = self._filter_combo(
            "出典フィルター", "データの出典・発行元で絞り込みます"
        )
        self.status_combo = self._filter_combo(
            "状態フィルター",
            "最新のみ・要注意（旧版や根拠不足）・アーカイブ・未適格で絞り込みます",
        )
        for combo in (
            self.family_combo,
            self.category_combo,
            self.source_combo,
            self.status_combo,
        ):
            filters.addWidget(combo)
        layout.addLayout(filters)
        hint = QLabel(
            "Ctrl/Shiftキーで2件を選ぶと差分比較が表示されます。",
            self,
        )
        set_typography_role(hint, TypographyRole.SECONDARY)
        layout.addWidget(hint)

        self.results_table = QTableWidget(0, 6, self)
        self.results_table.setAccessibleName("ライブラリ検索結果")
        self.results_table.setToolTip(
            "検索・絞り込みの結果です。行を選ぶと下に正確な識別情報・出典・"
            "権利状態が表示されます。"
        )
        self.results_table.setHorizontalHeaderLabels(
            ("名前", "種別", "区分", "スコープ", "バージョン", "状態")
        )
        for _col, _tip in enumerate((
            "項目の表示名（同名の別項目は個別の行のままです）",
            "項目の種別（機材・材料・規格プロファイルなど）",
            "項目の区分（カテゴリ・登録種別）",
            "この項目が有効な範囲",
            "登録されている定義のバージョン",
            "最新・旧版・根拠不足・アーカイブなどの状態",
        )):
            self.results_table.horizontalHeaderItem(_col).setToolTip(_tip)
        self.results_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.results_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.results_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.results_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.results_table.itemSelectionChanged.connect(
            self._on_results_selection_changed
        )
        layout.addWidget(self.results_table, 1)

        pager_row = QHBoxLayout()
        self._more_button = QPushButton("さらに読み込む", self)
        self._more_button.setAccessibleName("検索結果をさらに読み込む")
        self._more_button.setToolTip(
            "結果を50件ずつ追加で表示します。"
        )
        self._more_button.clicked.connect(self._results_load_more)
        self._count_label = QLabel(self)
        set_typography_role(self._count_label, TypographyRole.SECONDARY)
        self._count_label.setAccessibleName("検索結果の件数")
        pager_row.addWidget(self._more_button)
        pager_row.addStretch(1)
        pager_row.addWidget(self._count_label)
        layout.addLayout(pager_row)

        self._detail_frame = QFrame(self)
        detail_layout = QVBoxLayout(self._detail_frame)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        self._detail_title = QLabel("", self._detail_frame)
        set_typography_role(self._detail_title, TypographyRole.SECTION_TITLE)
        self._detail_title.setAccessibleName("選択した項目の詳細")
        detail_layout.addWidget(self._detail_title)
        self._detail_table = QTableWidget(0, 2, self._detail_frame)
        self._detail_table.setAccessibleName("項目の詳細情報")
        self._detail_table.setToolTip(
            "authority kind・ID・バージョン・SHA・出典・根拠区分・権利状態"
            "です。"
        )
        self._detail_table.setHorizontalHeaderLabels(("項目", "値"))
        self._detail_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._detail_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._detail_table.setMaximumHeight(180)
        detail_layout.addWidget(self._detail_table)

        self._usage_header = QLabel(
            "利用箇所（現在のプロジェクト）", self._detail_frame
        )
        set_typography_role(self._usage_header, TypographyRole.SECTION_TITLE)
        detail_layout.addWidget(self._usage_header)
        self._usage_empty = QLabel(
            "現在のプロジェクトでは使われていません。", self._detail_frame
        )
        set_typography_role(self._usage_empty, TypographyRole.SECONDARY)
        self._usage_empty.setWordWrap(True)
        detail_layout.addWidget(self._usage_empty)
        self._usage_table = QTableWidget(0, 3, self._detail_frame)
        self._usage_table.setAccessibleName("この項目の利用箇所")
        self._usage_table.setToolTip(
            "現在のプロジェクト内でこの項目を参照している箇所です。"
            "「開く」でその場所へ移動します（参照のみ・変更しません）。"
        )
        self._usage_table.setHorizontalHeaderLabels(
            ("利用箇所", "参照の解決", "操作")
        )
        self._usage_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._usage_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._usage_table.setMaximumHeight(120)
        detail_layout.addWidget(self._usage_table)

        self._compare_frame = QFrame(self._detail_frame)
        compare_layout = QVBoxLayout(self._compare_frame)
        compare_layout.setContentsMargins(0, 8, 0, 0)
        compare_title = QLabel("選択した2件の差分", self._compare_frame)
        set_typography_role(compare_title, TypographyRole.SECTION_TITLE)
        compare_layout.addWidget(compare_title)
        self._compare_table = QTableWidget(0, 3, self._compare_frame)
        self._compare_table.setAccessibleName("2件の差分比較")
        self._compare_table.setToolTip(
            "2件の項目の識別情報・出典・権利状態の差分です。"
            "同名・別バージョンの項目は自動で統合されません。"
        )
        self._compare_table.setHorizontalHeaderLabels(
            ("項目", "A", "B")
        )
        self._compare_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._compare_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._compare_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._compare_table.setMaximumHeight(160)
        compare_layout.addWidget(self._compare_table)
        self._dependents_label = QLabel("", self._compare_frame)
        self._dependents_label.setWordWrap(True)
        set_typography_role(
            self._dependents_label, TypographyRole.SECONDARY
        )
        compare_layout.addWidget(self._dependents_label)
        self._dependents_frame = QFrame(self._compare_frame)
        self._dependents_layout = QVBoxLayout(self._dependents_frame)
        self._dependents_layout.setContentsMargins(0, 0, 0, 0)
        self._dependents_layout.setSpacing(2)
        compare_layout.addWidget(self._dependents_frame)
        detail_layout.addWidget(self._compare_frame)
        self._compare_frame.setVisible(False)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._detail_frame)
        scroll.setMaximumHeight(340)
        scroll.setAccessibleName("項目の詳細・比較パネル")
        self._detail_scroll = scroll
        layout.addWidget(scroll)
        self._detail_scroll.setVisible(False)

    def _on_filters_changed(self) -> None:
        self._apply_filters()

    def _rebuild_filter_combos(self) -> None:
        families = sorted({row.family for row in self._all_rows})
        categories = sorted(
            {row.category for row in self._all_rows if row.category}
        )
        sources = sorted(
            {
                source
                for row in self._all_rows
                for source in row.sources
            }
        )
        specs = (
            (
                self.family_combo,
                "すべての種別",
                [
                    (
                        _LIBRARY_FAMILY_TITLES.get(family, family),
                        family,
                    )
                    for family in families
                ],
            ),
            (
                self.category_combo,
                "すべての区分",
                [
                    (category_label(category), category)
                    for category in categories
                ],
            ),
            (
                self.source_combo,
                "すべての出典",
                [(source, source) for source in sources],
            ),
            (
                self.status_combo,
                "すべて（アーカイブを除く）",
                [
                    ("最新のみ", STATUS_LATEST),
                    ("要注意（旧版・根拠不足・記録なし）", STATUS_ATTENTION),
                    ("アーカイブ・未適格", STATUS_UNQUALIFIED),
                ],
            ),
        )
        for combo, first_label, items in specs:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first_label, None)
            for label, value in items:
                combo.addItem(str(label), value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _apply_filters(self) -> None:
        self._filtered_rows = filter_rows(
            self._all_rows,
            query=self.search_edit.text(),
            family=self.family_combo.currentData(),
            category=self.category_combo.currentData(),
            source=self.source_combo.currentData(),
            status=self.status_combo.currentData() or STATUS_ALL,
        )
        self._shown_count = 0
        self.results_table.setRowCount(0)
        self._results_load_more()

    def _results_load_more(self) -> None:
        total = len(self._filtered_rows)
        start = self._shown_count
        end = min(start + self._RESULTS_PAGE_SIZE, total)
        for row in self._filtered_rows[start:end]:
            table_row = self.results_table.rowCount()
            self.results_table.insertRow(table_row)
            values = (
                row.entry.display_name,
                _LIBRARY_FAMILY_TITLES.get(row.family, row.family),
                category_label(row.category),
                _LIBRARY_SCOPE_LABELS.get(
                    str(row.entry.scope), str(row.entry.scope)
                ),
                row.entry.version,
                row_status_label(row),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, row.semantic_key)
                self.results_table.setItem(table_row, column, cell)
        self._shown_count = end
        first = 1 if end else 0
        self._count_label.setText(f"全{total}件 · {first}–{end}件を表示")
        self._more_button.setEnabled(end < total)

    def _selected_result_rows(self) -> list:
        selected = sorted(
            {
                index.row()
                for index in self.results_table.selectionModel().selectedRows()
            }
        )
        keys = []
        for row in selected:
            item = self.results_table.item(row, 0)
            if item is not None:
                keys.append(item.data(Qt.ItemDataRole.UserRole))
        return [
            self._rows_by_key[key]
            for key in keys
            if key in self._rows_by_key
        ]

    def _on_results_selection_changed(self) -> None:
        rows = self._selected_result_rows()
        if not rows:
            self._detail_scroll.setVisible(False)
            return
        self._detail_scroll.setVisible(True)
        self._show_detail(rows[-1])
        if len(rows) >= 2:
            self._show_compare(rows[-2], rows[-1])
        else:
            self._compare_frame.setVisible(False)

    def _show_detail(self, row) -> None:
        entry = row.entry
        self._detail_title.setText(
            f"{entry.display_name} — {entry.identity} v{entry.version}"
        )
        self._detail_table.setRowCount(0)
        for label, value in row.detail_fields:
            table_row = self._detail_table.rowCount()
            self._detail_table.insertRow(table_row)
            self._detail_table.setItem(table_row, 0, QTableWidgetItem(label))
            self._detail_table.setItem(
                table_row, 1, QTableWidgetItem(str(value))
            )
        self._usage_table.setRowCount(0)
        sites = row.usage_sites
        self._usage_empty.setVisible(not sites)
        self._usage_table.setVisible(bool(sites))
        for site in sites:
            table_row = self._usage_table.rowCount()
            self._usage_table.insertRow(table_row)
            self._usage_table.setItem(
                table_row, 0, QTableWidgetItem(site.label)
            )
            self._usage_table.setItem(
                table_row, 1, QTableWidgetItem(site.resolution)
            )
            button = QPushButton("開く", self._usage_table)
            button.setAccessibleName(f"{site.label}を開く")
            button.setToolTip(
                "この項目を利用している箇所へ移動します（参照のみ）。"
            )
            button.clicked.connect(
                lambda _checked=False, s=site: self._open_usage_site(s)
            )
            self._usage_table.setCellWidget(table_row, 2, button)

    def _show_compare(self, a, b) -> None:
        comparison = compare_rows(a, b)
        self._compare_frame.setVisible(True)
        if comparison is None:
            self._compare_table.setRowCount(0)
            self._dependents_label.setText(
                "異なる種別の項目は比較できません。"
            )
            self._clear_dependents()
            return
        self._compare_table.setRowCount(0)
        for label, value_a, value_b in comparison.fields:
            table_row = self._compare_table.rowCount()
            self._compare_table.insertRow(table_row)
            self._compare_table.setItem(
                table_row, 0, QTableWidgetItem(label)
            )
            cell_a = QTableWidgetItem(value_a)
            cell_b = QTableWidgetItem(value_b)
            if value_a != value_b:
                brush = self._diff_brush()
                cell_a.setBackground(brush)
                cell_b.setBackground(brush)
            self._compare_table.setItem(table_row, 1, cell_a)
            self._compare_table.setItem(table_row, 2, cell_b)
        dependents = comparison.dependents_a + comparison.dependents_b
        if dependents:
            self._dependents_label.setText(
                "この参照は正確なID・バージョン・SHAに紐づいています。"
                "入れ替えると現在の参照は古い版を指したままになります。"
                f"依存している成果物: {len(dependents)}件"
            )
        else:
            self._dependents_label.setText(
                "現在のプロジェクトでこの2件を参照する成果物はありません。"
            )
        self._show_dependents(dependents)

    @staticmethod
    def _diff_brush() -> QBrush:
        palette = QApplication.palette()
        return QBrush(
            palette.color(QPalette.ColorRole.AlternateBase)
        )

    def _clear_dependents(self) -> None:
        while self._dependents_layout.count():
            item = self._dependents_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _show_dependents(self, sites) -> None:
        self._clear_dependents()
        for site in sites:
            button = QPushButton(f"開く: {site.label}", self._dependents_frame)
            button.setAccessibleName(f"依存先 {site.label}を開く")
            button.setToolTip(
                "この項目を参照している成果物へ移動します（参照のみ）。"
            )
            button.clicked.connect(
                lambda _checked=False, s=site: self._open_usage_site(s)
            )
            self._dependents_layout.addWidget(button)

    def _open_usage_site(self, site) -> None:
        if self._open_target is None:
            return
        kind = (
            NavigationTargetKind.SCENE_ENTITY
            if site.kind == 'scene_entity'
            else NavigationTargetKind.INSTALLED_EQUIPMENT_INSTANCE
        )
        self._open_target(
            NavigationTarget(
                kind=kind,
                object_ids=(site.target_id,),
                preferred_destination=WorkspaceId.ROOM,
                intent=NavigationIntent.INSPECT,
                referrer='reference_library',
            )
        )

    def _refresh_browser(self) -> None:
        if self._library_index is None:
            return
        try:
            usages = (
                self._usage_resolver() if self._usage_resolver else {}
            )
            resolver = (
                self._detail_resolver()
                if self._detail_resolver is not None
                else None
            )
            self._all_rows = collect_library_rows(
                self._library_index,
                detail_resolver=resolver,
                usage_sites=usages,
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: library read — expected failures report and show an honest empty page; sealed-store failures propagate
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='ライブラリ一覧の読み取り')
            self._all_rows = ()
        self._rows_by_key = {
            row.semantic_key: row for row in self._all_rows
        }
        self._rebuild_filter_combos()
        self._apply_filters()

    def _reveal_row(self, row) -> bool:
        """Make ``row`` visible+selected, resetting filters when needed."""

        def _visible() -> int | None:
            for table_row in range(self.results_table.rowCount()):
                item = self.results_table.item(table_row, 0)
                if (
                    item is not None
                    and item.data(Qt.ItemDataRole.UserRole)
                    == row.semantic_key
                ):
                    return table_row
            return None

        target = _visible()
        if target is None:
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
            for combo in (
                self.family_combo,
                self.category_combo,
                self.source_combo,
            ):
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
            self.status_combo.blockSignals(True)
            status_value = (
                STATUS_UNQUALIFIED
                if (row.archived or row.record_missing)
                else STATUS_ALL
            )
            status_index = self.status_combo.findData(status_value)
            self.status_combo.setCurrentIndex(
                status_index if status_index >= 0 else 0
            )
            self.status_combo.blockSignals(False)
            self._apply_filters()
            target = _visible()
        if target is None:
            return False
        self.results_table.selectRow(target)
        self._tabs.setCurrentIndex(0)
        return True

    # --- lifecycle -------------------------------------------------------

    def refresh(self) -> None:
        self.table.setRowCount(0)
        for definition in self._list_definitions():
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    getattr(definition, "manufacturer", "") or "",
                    getattr(definition, "model", "")
                    or getattr(definition, "user_label", "")
                    or getattr(definition, "definition_id", ""),
                    str(getattr(definition, "version", "")),
                )
            ):
                item = QTableWidgetItem(str(value))
                if column == 1:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        getattr(definition, "definition_id", ""),
                    )
                self.table.setItem(row, column, item)
        self.empty_label.setVisible(self.table.rowCount() == 0)
        self._refresh_family_sections()
        self._refresh_browser()

    def _refresh_family_sections(self) -> None:
        if self._library_index is None:
            return
        for family, (header, table) in self._family_frames.items():
            try:
                entries = self._library_index.entries(family=family)
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: per-family library read — expected failures report and hide that section honestly; sealed-store failures propagate
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='ライブラリ族の読み取り')
                entries = ()
            header.setVisible(bool(entries))
            table.setVisible(bool(entries))
            table.setRowCount(0)
            for entry in entries:
                row = table.rowCount()
                table.insertRow(row)
                for column, value in enumerate(
                    (
                        entry.display_name,
                        entry.capability_summary
                        or entry.source_summary
                        or "",
                        _LIBRARY_SCOPE_LABELS.get(
                            str(entry.scope), str(entry.scope)
                        ),
                        entry.version,
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, entry.semantic_key
                        )
                    table.setItem(row, column, cell)

    def focus_definition(self, definition_id: str) -> TargetFocusResult:
        focused = False
        for row in range(self.table.rowCount()):
            model = self.table.item(row, 1)
            if (
                model is not None
                and model.data(Qt.ItemDataRole.UserRole) == definition_id
            ):
                self.table.selectRow(row)
                focused = True
                break
        if self._library_index is not None:
            target_row = next(
                (
                    row
                    for row in self._all_rows
                    if row.entry.identity == definition_id
                    and row.family == 'equipment'
                ),
                None,
            )
            if target_row is not None:
                focused = self._reveal_row(target_row) or focused
        if focused:
            return TargetFocusResult(focused=True)
        return TargetFocusResult(
            focused=False,
            message="ライブラリ内に該当の定義が見つかりません",
        )


#: JA labels for the health-check categories — order is the canonical
#: display order: store health first, semantic integrity second, optional
#: integrations last (their failures are honest but local, never global).
_HEALTH_CATEGORY_LABELS: dict[HealthCategory, str] = {
    HealthCategory.APP_STORAGE: "アプリ・プロジェクトの保存データ",
    HealthCategory.SEMANTIC_INTEGRITY: "データの意味整合性",
    HealthCategory.INTEGRATIONS: "外部連携（任意）",
}

_HEALTH_STATUS_LABELS: dict[HealthStatus, str] = {
    HealthStatus.PASS: "正常",
    HealthStatus.ATTENTION: "注意",
    HealthStatus.FAIL: "失敗",
    HealthStatus.UNKNOWN: "不明",
    HealthStatus.NOT_APPLICABLE: "対象外",
}

_HEALTH_STATUS_SEMANTIC: dict[HealthStatus, SemanticState | None] = {
    HealthStatus.PASS: SemanticState.SUCCESS,
    HealthStatus.ATTENTION: SemanticState.WARNING,
    HealthStatus.FAIL: SemanticState.ERROR,
    HealthStatus.UNKNOWN: SemanticState.STALE,
    HealthStatus.NOT_APPLICABLE: None,
}

#: JA display names per check_id. An id absent here renders verbatim —
#: the surface never invents a friendlier name for a check it did not
#: predict (same honesty rule as the lifecycle table labels above).
_HEALTH_CHECK_LABELS: dict[str, str] = {
    "storage.database_openable": "プロジェクトDBのオープン",
    "storage.sqlite_quick_check": "SQLite構造チェック",
    "storage.schema_compatibility": "スキーマ互換性",
    "storage.data_dir_lock": "データフォルダーのロック",
    "storage.disk_space": "ディスク空き容量",
    "storage.assets_root": "管理アセットの保存先",
    "integrity.semantic": "権威グラフの意味監査",
    "integrations.rew_api": "REW API連携",
    "integrations.capture_receiver": "キャプチャ受信",
    "integrations.vtk": "3D表示スタック（VTK）",
}

#: Per-check next-step guidance (原因別の対処へ誘導). Every entry pairs
#: actionable JA wording with an *existing* surface key — the page only
#: ever points at surfaces it can actually open (data management with the
#: backup/restore preview, the read-only authority inspector, settings
#: tabs, the activity log, the diagnostics package export). Checks are
#: read-only: nothing here auto-repairs, and repair-adjacent wording
#: always routes through the restore preview rather than a blind write.
_HEALTH_GUIDANCE: dict[str, tuple[str, str]] = {
    "storage.database_openable": (
        "プロジェクトDBを開けません。データ管理の「バックアップから復元」"
        "（復元前プレビュー付き）で直近のバックアップを検査・復元して"
        "ください。自動修復は行いません。",
        "data_management",
    ),
    "storage.sqlite_quick_check": (
        "SQLiteの構造チェックで問題が検出されました。データ管理で直近の"
        "バックアップを検査し、必要なら隔離復元（#992）を行ってください。",
        "data_management",
    ),
    "storage.schema_compatibility": (
        "このDBは別バージョンのHTDTで書かれています。データ管理のバック"
        "アップ復元で作成時のデータに戻すか、そのデータを作成したビルド"
        "で開いてください。",
        "data_management",
    ),
    "storage.data_dir_lock": (
        "データフォルダーが別プロセスにロックされています。アクティビティ"
        "で実行中の操作を確認し、他のHTDTインスタンスを終了してから再診断"
        "してください。",
        "activity",
    ),
    "storage.disk_space": (
        "空き容量が不足しています。データ管理で保持データの整理を行うか、"
        "ドライブの空き容量を確保してから再診断してください。",
        "data_management",
    ),
    "storage.assets_root": (
        "管理アセットの保存先にアクセスできません。データフォルダーの"
        "権限を確認してください。",
        "data_management",
    ),
    "integrity.semantic": (
        "データの意味整合性に問題が見つかりました。権威グラフで破損箇所を"
        "確認し、データ管理のバックアップ復元（復元前プレビュー）を検討"
        "してください。自動修復は行いません。",
        "authority",
    ),
    "integrations.rew_api": (
        "REW連携は任意です。測定取り込みを使う場合はREWを起動し、環境"
        "設定の接続先（ホスト・ポート）を確認してください。",
        "preferences",
    ),
    "integrations.capture_receiver": (
        "キャプチャ受信を使う場合は、設定の「キャプチャ」タブで受信状態と"
        "起動エラーを確認してください。",
        "capture_settings",
    ),
    "integrations.vtk": (
        "3D表示機能が利用できません。再インストールまたはGPUドライバーの"
        "更新を検討してください。再現する場合は診断パッケージをサポートへ"
        "共有してください。",
        "export",
    ),
}

#: Category-level fallback guidance for check ids the map does not know
#: (e.g. a dynamically named probe) — never claim a specific cause the
#: check did not report.
_HEALTH_GUIDANCE_BY_CATEGORY: dict[HealthCategory, tuple[str, str]] = {
    HealthCategory.APP_STORAGE: (
        "保存データの問題です。データ管理のバックアップ復元（復元前"
        "プレビュー）を検討してください。",
        "data_management",
    ),
    HealthCategory.SEMANTIC_INTEGRITY: (
        "データの意味整合性の問題です。権威グラフで詳細を確認して"
        "ください。",
        "authority",
    ),
    HealthCategory.INTEGRATIONS: (
        "外部連携の問題です（アプリやプロジェクトDBの障害ではありません）。"
        "設定で接続先を確認してください。",
        "preferences",
    ),
}

_HEALTH_GUIDANCE_STATUS_FALLBACK: dict[HealthStatus, str] = {
    HealthStatus.UNKNOWN: "状態を判定できませんでした。しばらくしてから再診断してください。",
}

#: Button labels for the surface keys used by ``_HEALTH_GUIDANCE``.
_HEALTH_ACTION_LABELS: dict[str, str] = {
    "data_management": "データ管理を開く",
    "preferences": "環境設定を開く",
    "capture_settings": "キャプチャ設定を開く",
    "authority": "権威グラフを開く",
    "activity": "アクティビティを開く",
    "export": "診断パッケージをエクスポート",
}


def health_check_guidance(
    result: HealthCheckResult,
) -> tuple[str, str | None] | None:
    """(JA guidance, surface action key) for a non-passing check.

    ``None`` for PASS/NOT_APPLICABLE — a healthy or inapplicable check
    needs no next step. The action key names an existing Support surface
    the caller can open; it is never a repair promise.
    """

    if result.status in (HealthStatus.PASS, HealthStatus.NOT_APPLICABLE):
        return None
    guidance = _HEALTH_GUIDANCE.get(result.check_id)
    if guidance is not None:
        return guidance
    fallback = _HEALTH_GUIDANCE_BY_CATEGORY.get(result.category)
    if fallback is not None:
        return fallback
    unknown = _HEALTH_GUIDANCE_STATUS_FALLBACK.get(result.status)
    if unknown is not None:
        return (unknown, None)
    return None


class SupportPage(QWidget):
    """Support: diagnostics locations, version, and package export (#604).

    #1018 adds the read-only health-check lane: ``health_runner`` (a
    ``SupportHealthRunner``) executes ``run_health_checks`` off the UI
    thread and this page itemizes its per-category findings with
    reason-specific next-step guidance. The stored report is re-rendered
    on ``refresh`` rather than silently re-run — a re-shown report always
    keeps its own execution timestamp.
    """

    def __init__(
        self,
        data_dir: Path,
        status_provider: Callable[[], tuple[str, ...]] | None = None,
        export_diagnostics: Callable[[QWidget], str | None] | None = None,
        open_authority_graph: Callable[[QWidget], None] | None = None,
        open_solver_diagnostics: Callable[[QWidget], None] | None = None,
        open_applicability_envelope: Callable[[QWidget], None] | None = None,
        open_credential_vault: Callable[[QWidget], None] | None = None,
        health_runner: object | None = None,
        open_data_management: Callable[[QWidget], None] | None = None,
        open_preferences: Callable[[QWidget], None] | None = None,
        open_capture_settings: Callable[[QWidget], None] | None = None,
        open_activity: Callable[[QWidget], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._data_dir = data_dir
        self._status_provider = status_provider
        self._export_diagnostics = export_diagnostics
        self._open_authority_graph = open_authority_graph
        self._open_solver_diagnostics = open_solver_diagnostics
        self._open_applicability_envelope = open_applicability_envelope
        self._open_credential_vault = open_credential_vault
        self._health_runner = health_runner
        self._health_actions: dict[str, Callable[[QWidget], None]] = {}
        if open_data_management is not None:
            self._health_actions["data_management"] = open_data_management
        if open_preferences is not None:
            self._health_actions["preferences"] = open_preferences
        if open_capture_settings is not None:
            self._health_actions["capture_settings"] = open_capture_settings
        if open_activity is not None:
            self._health_actions["activity"] = open_activity
        if open_authority_graph is not None:
            self._health_actions["authority"] = open_authority_graph
        if export_diagnostics is not None:
            self._health_actions["export"] = lambda _w: self._run_export()
        self._health_report: HealthReport | None = None
        self._health_report_data_dir: Path | None = None
        layout = _page_layout(
            self,
            "サポート",
            "問題が発生したときの確認情報です。",
        )
        for label_text in (
            f"バージョン: {version_string()}",
            f"データフォルダー: {data_dir}",
            f"診断ログ: {diagnostics_dir(data_dir)}",
        ):
            label = QLabel(label_text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            layout.addWidget(label)
        self._status_layout = layout
        self._status_labels: list[QLabel] = []
        note = QLabel(
            "起動に失敗した場合は診断ログをサポートに共有してください。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        if self._open_authority_graph is not None:
            self.authority_button = QPushButton("権威グラフを開く", self)
            self.authority_button.setToolTip("データの由来（どの定義・設定から生成されたか）を辿れるグラフ画面を開きます")
            self.authority_button.setWhatsThis("データの由来（どの定義・設定から生成されたか）を辿れるグラフ画面を開きます")
            self.authority_button.setObjectName("supportOpenAuthorityGraph")
            self.authority_button.clicked.connect(
                lambda: self._open_authority_graph(self)
            )
            layout.addWidget(self.authority_button)
        else:
            self.authority_button = None
        if self._open_solver_diagnostics is not None:
            self.solver_button = QPushButton("ソルバー出力の診断", self)
            self.solver_button.setToolTip("音響ソルバーが出力した計算結果の内部診断情報を確認します")
            self.solver_button.setWhatsThis("音響ソルバーが出力した計算結果の内部診断情報を確認します")
            self.solver_button.setObjectName("supportOpenSolverDiagnostics")
            self.solver_button.clicked.connect(
                lambda: self._open_solver_diagnostics(self)
            )
            layout.addWidget(self.solver_button)
        else:
            self.solver_button = None
        if self._open_applicability_envelope is not None:
            self.envelope_button = QPushButton("適用範囲エンベロープ", self)
            self.envelope_button.setToolTip("このソルバーパスに対して記録された証拠を7つの次元（能力・検証・外部検証・入力適格性・実室証拠・不確かさ・用途）で表示します")
            self.envelope_button.setWhatsThis("このソルバーパスに対して記録された証拠を7つの次元で表示します — 能力の宣言は検証ではありません")
            self.envelope_button.setObjectName("supportOpenApplicabilityEnvelope")
            self.envelope_button.clicked.connect(
                lambda: self._open_applicability_envelope(self)
            )
            layout.addWidget(self.envelope_button)
        else:
            self.envelope_button = None
        if self._open_credential_vault is not None:
            self.credential_vault_button = QPushButton(
                "資格情報ヴォールト", self)
            self.credential_vault_button.setToolTip(
                "プロジェクトの資格情報（参照と監査ログ）を管理します"
                " — 秘密の値は表示されません")
            self.credential_vault_button.setWhatsThis(
                "このプロジェクトに紐付く資格情報の登録・ローテーション・"
                "失効・削除と監査ログを開きます。表示されるのは参照IDと"
                "メタデータのみで、秘密の値は表示されません。")
            self.credential_vault_button.setObjectName(
                "supportOpenCredentialVault")
            self.credential_vault_button.clicked.connect(
                lambda: self._open_credential_vault(self)
            )
            layout.addWidget(self.credential_vault_button)
        else:
            self.credential_vault_button = None
        if self._export_diagnostics is not None:
            self.export_button = QPushButton("診断パッケージをエクスポート", self)
            self.export_button.setToolTip("サポート共有用の診断情報（ログ・設定の概要など）を1つのファイルにまとめて書き出します")
            self.export_button.setWhatsThis("サポート共有用の診断情報（ログ・設定の概要など）を1つのファイルにまとめて書き出します")
            self.export_button.setObjectName("supportExportDiagnostics")
            self.export_button.clicked.connect(self._run_export)
            layout.addWidget(self.export_button)
            self.export_status = QLabel(self)
            self.export_status.setObjectName("supportExportStatus")
            self.export_status.setWordWrap(True)
            layout.addWidget(self.export_status)
        else:
            self.export_button = None
            self.export_status = None
        self._build_health_section(layout)
        layout.addStretch(1)
        self.refresh()

    # -- read-only health check (#1018) ------------------------------------

    def _build_health_section(self, layout: QVBoxLayout) -> None:
        """Build the 状態診断 lane; all widgets are None without a runner."""
        if self._health_runner is None:
            self.health_button = None
            self.health_cancel_button = None
            self.health_status = None
            self.health_results = None
            return
        heading = QLabel("アプリとプロジェクトの状態診断", self)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)
        note = QLabel(
            "保存データ・意味整合性・外部連携を読み取り専用で検査します。"
            "プロジェクトの内容は変更されません。外部連携の不具合は"
            "アプリ全体の障害とは別に扱います。"
        )
        note.setWordWrap(True)
        set_typography_role(note, TypographyRole.SECONDARY)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.health_button = QPushButton(
            "アプリとプロジェクトの状態を診断", self
        )
        self.health_button.setToolTip(
            "プロジェクトの内容を変更せずに各項目の健全性を検査します"
        )
        self.health_button.setWhatsThis(
            "読み取り専用の状態診断を実行します。検査中も操作を続けられます。"
        )
        self.health_button.setObjectName("supportRunHealthCheck")
        self.health_button.setAccessibleName("アプリとプロジェクトの状態を診断")
        self.health_button.clicked.connect(self._run_health_check)
        buttons.addWidget(self.health_button)
        self.health_cancel_button = QPushButton("中止", self)
        self.health_cancel_button.setObjectName("supportHealthCheckCancel")
        self.health_cancel_button.setAccessibleName("状態診断を中止")
        self.health_cancel_button.setToolTip("実行中の診断を中止します")
        self.health_cancel_button.setWhatsThis("実行中の診断を中止します")
        self.health_cancel_button.setVisible(False)
        self.health_cancel_button.clicked.connect(self._cancel_health_check)
        buttons.addWidget(self.health_cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.health_status = QLabel(self)
        self.health_status.setObjectName("supportHealthStatus")
        self.health_status.setWordWrap(True)
        self.health_status.setAccessibleName("状態診断の状態")
        layout.addWidget(self.health_status)
        self.health_results = QScrollArea(self)
        self.health_results.setObjectName("supportHealthResults")
        self.health_results.setWidgetResizable(True)
        self.health_results.setFrameShape(QFrame.Shape.StyledPanel)
        self.health_results.setMinimumHeight(160)
        self.health_results.setAccessibleName("状態診断の結果一覧")
        self._health_results_body = QWidget(self.health_results)
        self._health_results_layout = QVBoxLayout(self._health_results_body)
        self._health_results_layout.setContentsMargins(8, 8, 8, 8)
        self._health_results_layout.setSpacing(6)
        self.health_results.setWidget(self._health_results_body)
        self.health_results.setVisible(False)
        layout.addWidget(self.health_results, 1)
        self.health_status.setText("まだ診断は実行されていません。")
        runner = self._health_runner
        if isinstance(runner, QObject):
            # Page owns the runner's lifetime; closeEvent -> shutdown()
            # is the explicit drain, reparenting is the last-resort
            # cleanup if the page is deleted without a close.
            runner.setParent(self)
        runner.check_started.connect(self._on_health_started)
        runner.report_ready.connect(self._on_health_report)
        runner.run_failed.connect(self._on_health_failed)
        runner.run_cancelled.connect(self._on_health_cancelled)
        runner.run_finished.connect(self._on_health_finished)

    def _run_health_check(self) -> None:
        if self._health_runner is None:
            return
        self.health_button.setEnabled(False)
        if not self._health_runner.start():
            self.health_button.setEnabled(True)

    def _cancel_health_check(self) -> None:
        if self._health_runner is not None:
            self._health_runner.request_cancel()

    def _on_health_started(self) -> None:
        if self.health_status is None:
            return
        self.health_status.setText("状態を診断しています…")
        self.health_button.setEnabled(False)
        self.health_cancel_button.setVisible(True)
        self.health_cancel_button.setEnabled(True)

    def _on_health_finished(self) -> None:
        if self.health_status is None:
            return
        self.health_button.setEnabled(True)
        self.health_cancel_button.setVisible(False)
        self.health_cancel_button.setEnabled(False)

    def _on_health_cancelled(self) -> None:
        if self.health_status is not None:
            self.health_status.setText("診断を中止しました。")

    def _on_health_failed(self, error: object) -> None:
        if self.health_status is not None:
            self.health_status.setText(
                f"診断を完了できませんでした: {operation_error_message(error)}"
            )

    def _on_health_report(self, report: object) -> None:
        if not isinstance(report, HealthReport):
            return
        self._health_report = report
        self._health_report_data_dir = getattr(
            self._health_runner, 'data_dir', self._data_dir
        )
        self._render_health_report()

    def _render_health_report(self) -> None:
        """Itemize the stored report per category with honest statuses."""
        report = self._health_report
        if report is None or self.health_results is None:
            return
        results_layout = self._health_results_layout
        while results_layout.count():
            item = results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        overall = report.overall
        overall_label = (
            f"総合判定: {_HEALTH_STATUS_LABELS.get(overall, str(overall))}"
        )
        summary_text = (
            f"{overall_label} · 最終実行: {report.checked_at} · "
            f"対象: {self._health_report_data_dir or self._data_dir}"
        )
        fingerprint = getattr(self._health_runner, 'last_fingerprint', None)
        if fingerprint:
            summary_text += f" · データ状態: {str(fingerprint)[:12]}"
        self.health_status.setText(summary_text)
        set_semantic_state(
            self.health_status, _HEALTH_STATUS_SEMANTIC.get(overall)
        )
        for category in (
            HealthCategory.APP_STORAGE,
            HealthCategory.SEMANTIC_INTEGRITY,
            HealthCategory.INTEGRATIONS,
        ):
            results = report.by_category(category)
            if not results:
                continue
            worst = self._category_status(results)
            header = QLabel(
                f"{_HEALTH_CATEGORY_LABELS[category]}"
                f" — {_HEALTH_STATUS_LABELS[worst]}",
                self._health_results_body,
            )
            set_typography_role(header, TypographyRole.SECTION_TITLE)
            set_semantic_state(
                header, _HEALTH_STATUS_SEMANTIC.get(worst)
            )
            header.setWordWrap(True)
            results_layout.addWidget(header)
            if category is HealthCategory.INTEGRATIONS:
                honesty = QLabel(
                    "外部連携の不具合はアプリやプロジェクトDBの障害では"
                    "ありません（任意機能の状態です）。",
                    self._health_results_body,
                )
                honesty.setWordWrap(True)
                set_typography_role(honesty, TypographyRole.SECONDARY)
                results_layout.addWidget(honesty)
            for result in results:
                results_layout.addWidget(self._health_result_row(result))
        results_layout.addStretch(1)
        self.health_results.setVisible(True)

    @staticmethod
    def _category_status(results: tuple[HealthCheckResult, ...]) -> HealthStatus:
        """Category rollup: FAIL > ATTENTION > UNKNOWN > PASS > N/A."""
        statuses = {r.status for r in results}
        for status in (
            HealthStatus.FAIL,
            HealthStatus.ATTENTION,
            HealthStatus.UNKNOWN,
            HealthStatus.PASS,
        ):
            if status in statuses:
                return status
        return HealthStatus.NOT_APPLICABLE

    def _health_result_row(self, result: HealthCheckResult) -> QWidget:
        row = QWidget(self._health_results_body)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(12, 0, 0, 4)
        row_layout.setSpacing(2)
        status_text = _HEALTH_STATUS_LABELS.get(result.status, result.status)
        name = _HEALTH_CHECK_LABELS.get(result.check_id, result.check_id)
        title = QLabel(f"{name} — {status_text}", row)
        title.setWordWrap(True)
        title.setAccessibleName(f"{name}: {status_text}")
        set_semantic_state(
            title, _HEALTH_STATUS_SEMANTIC.get(result.status)
        )
        row_layout.addWidget(title)
        summary = QLabel(result.summary, row)
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row_layout.addWidget(summary)
        if result.detail:
            detail = QLabel(result.detail, row)
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            set_typography_role(detail, TypographyRole.SECONDARY)
            row_layout.addWidget(detail)
        guidance = health_check_guidance(result)
        if guidance is not None:
            guidance_text, action_key = guidance
            action_row = QWidget(row)
            action_layout = QHBoxLayout(action_row)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(8)
            guidance_label = QLabel(guidance_text, action_row)
            guidance_label.setWordWrap(True)
            set_typography_role(guidance_label, TypographyRole.SECONDARY)
            action_layout.addWidget(guidance_label, 1)
            action = (
                self._health_actions.get(action_key)
                if action_key is not None
                else None
            )
            if action is not None:
                action_button = QPushButton(
                    _HEALTH_ACTION_LABELS[action_key], action_row
                )
                action_button.setObjectName(
                    f"supportHealthAction-{action_key}"
                )
                action_button.setAccessibleName(
                    _HEALTH_ACTION_LABELS[action_key]
                )
                action_button.clicked.connect(
                    lambda _checked=False, fn=action: fn(self)
                )
                action_layout.addWidget(
                    action_button, 0, Qt.AlignmentFlag.AlignTop
                )
            row_layout.addWidget(action_row)
        return row

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._health_runner is not None:
            self._health_runner.shutdown()
        super().closeEvent(event)

    def _run_export(self) -> None:
        try:
            path = self._export_diagnostics(self)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: diagnostics export — expected failures surface verbatim on the status label; unexpected errors propagate to diagnostics
            self.export_status.setText(
                "診断パッケージを作成できませんでした: "
                f"{operation_error_message(exc)}"
            )
            return
        if path is not None:
            self.export_status.setText(f"保存しました: {path}")

    def refresh(self) -> None:
        """Re-render live status lines (e.g. effective receiver state)."""
        # Re-use semantics: an already-produced report is re-rendered
        # verbatim (with its own timestamp) — refresh never re-runs the
        # check behind the user's back.
        if self._health_report is not None and self.health_results is not None:
            self._render_health_report()
        for label in self._status_labels:
            self._status_layout.removeWidget(label)
            label.deleteLater()
        self._status_labels.clear()
        if self._status_provider is None:
            return
        for text in self._status_provider():
            label = QLabel(text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            self._status_layout.insertWidget(
                self._status_layout.count() - 2, label
            )
            self._status_labels.append(label)


def projects_focus(
    page: ProjectLibraryPage, target: NavigationTarget
) -> TargetFocusResult:
    project_id = target.primary_id
    if project_id is None:
        return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == project_id:
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="プロジェクト一覧に該当の項目がありません",
    )


def inbox_focus(page: CaptureInboxPage, target: NavigationTarget) -> TargetFocusResult:
    item_id = target.primary_id
    if item_id is None:
        return TargetFocusResult(focused=True)
    if page._select_delivery_row(item_id):
        return TargetFocusResult(focused=True)
    # The triage filter/search may hide the row — deferred, rejected and
    # processed items are off the default 要レビュー queue (#988). Reveal
    # everything once, then retry the identity-pinned lookup.
    page.reveal_all_items()
    if page._select_delivery_row(item_id):
        return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="取り込み一覧に該当の項目がありません",
    )


def _event_row_matches(cell: QTableWidgetItem, target: NavigationTarget) -> bool:
    """A timeline row matches when its stored nav URI targets the same id."""

    link = cell.data(Qt.ItemDataRole.UserRole)
    if not isinstance(link, str):
        return False
    try:
        linked = navigation_target_from_uri(link)
    except ValueError:
        return False
    if linked.kind is not target.kind:
        return False
    return bool(set(linked.object_ids) & set(target.object_ids))


def activity_focus(page: ActivityPage, target: NavigationTarget) -> TargetFocusResult:
    if target.primary_id is None:
        return TargetFocusResult(focused=True)
    if page.events_table is not None:
        # Deep history (#1017): a linked event older than the initial
        # window loads more pages until it surfaces, so deep links reach
        # the oldest row instead of reporting it missing. The row-count
        # guard stops the walk if a cursor ever stalls without growth.
        while True:
            for row in range(page.events_table.rowCount()):
                cell = page.events_table.item(row, 0)
                if cell is not None and _event_row_matches(cell, target):
                    page.events_table.selectRow(row)
                    page.events_table.scrollToItem(cell)
                    return TargetFocusResult(focused=True)
            _more, more_button, _label = page.events_pager
            before = page.events_table.rowCount()
            if not more_button.isEnabled():
                break
            page._events_load_more()
            if page.events_table.rowCount() == before:
                break
    for table in (page.operations_table, page.other_operations_table):
        if table is None:
            continue
        for row in range(table.rowCount()):
            cell = table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
            ):
                table.selectRow(row)
                table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
    while True:
        for row in range(page.table.rowCount()):
            cell = page.table.item(row, 2)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
            ):
                page.table.selectRow(row)
                page.table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
        _more, more_button, _label = page.revisions_pager
        before = page.table.rowCount()
        if not more_button.isEnabled():
            break
        page._revisions_load_more()
        if page.table.rowCount() == before:
            break
    return TargetFocusResult(
        focused=False,
        message="アクティビティ一覧に該当の記録がありません",
    )


__all__ = [
    "ActivityEvidenceClass",
    "ActivityPage",
    "CaptureInboxPage",
    "ProjectEntry",
    "ProjectLibraryPage",
    "ProjectLibraryService",
    "ReferenceLibraryPage",
    "RevisionListScope",
    "SupportPage",
    "activity_focus",
    "count_recent_revisions",
    "event_evidence_class",
    "inbox_focus",
    "list_known_document_ids",
    "list_revisions_page",
    "projects_focus",
    "list_recent_revisions",
]
