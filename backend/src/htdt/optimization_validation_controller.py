from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QObject, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QListWidget,
    QListWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analysis_markers import render_analysis_marker_cloud
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedCandidateSetPage,
    CadExtendedSearchAxis,
    aim_horizontal_yaw_deg,
    apply_extended_candidate,
    build_extended_model_capability,
    build_extended_search_spec,
    extended_candidate_preview_document,
    generate_extended_candidates,
)
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_objectives import build_pareto_set
from .cad_objective_repository import CadObjectiveRepository
from .cad_scene import F1_DOCUMENT_ID
from .cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    generate_cad_candidates,
    search_spec_current_working,
)
from .cad_search_models import CadCandidate, CadCandidateSetPage, CadSearchAxis, CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .measurement_workspace import _MeasurementScrollArea
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_model_validation_service import (
    CadModelValidationService,
    CadValidationApplicabilitySpec,
)
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_measurement_loop import build_measurement_plan, complete_measurement_plan
from .cad_validation_campaign import (
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
)
from .cad_applicability import applicability_authority_summary
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_campaign_service import CadValidationCampaignService
from .native_editor import ROLE
from .user_facing_error import operation_error_message


_SPLIT_LABELS = {"calibration": "調整用", "holdout": "検証用"}
_GATE_LABELS = {
    "pass": "合格",
    "fail": "不合格",
    "insufficient": "不足",
    "eligible": "推薦可",
    "disabled": "無効",
}
_SCOPE_LABELS = {
    "synthetic_fixture": "合成データ",
    "development_synthetic": "合成データ",
    "owned_room": "実室データ",
    "owned_room_campaign": "実室データ",
    "production_owned_room": "実室データ",
}
_APPLICABILITY_LABELS = {"geometry": "形状", "band": "帯域", "routing": "経路"}
_OBJECTIVE_DISPLAY = {"response.shape_rms_db": "応答形状 RMS"}


def _split_label(value: str) -> str:
    return _SPLIT_LABELS.get(value, value)


def _gate_label(value: str) -> str:
    return _GATE_LABELS.get(value, value)


def _scope_label(value: str) -> str:
    return _SCOPE_LABELS.get(value, "根拠データ")


def _objective_label(value: str) -> str:
    return _OBJECTIVE_DISPLAY.get(value, value.rsplit(".", 1)[-1].replace("_", " "))


def _reason_label(value: str) -> str:
    exact = {
        "completed prediction attempt is missing": "完了した予測結果がありません",
        "multiple completed prediction attempts are ambiguous": "完了した予測結果が複数あり特定できません",
        "owned-room measured evidence is missing": "実室の実測データがありません",
        "completed Measurement Plan is missing": "完了した実測計画がありません",
        "multiple completed Measurement Plans are ambiguous": "完了した実測計画が複数あり特定できません",
        "predicted objective evaluation is missing": "予測の比較指標がありません",
        "predicted objective evaluations are ambiguous": "予測の比較指標が複数あり特定できません",
        "measured objective evaluation is missing": "実測の比較指標がありません",
        "measured objective evaluations are ambiguous": "実測の比較指標が複数あり特定できません",
        "measured evidence is missing": "実測データがありません",
    }
    if value in exact:
        return exact[value]
    if value.startswith("repeatability requires ") and value.endswith(" measurements"):
        count = value.removeprefix("repeatability requires ").removesuffix(" measurements")
        return f"再現性確認には実測が{count}回必要です"
    if value.startswith("candidate separation references missing candidate "):
        return "候補差の検証対象が見つかりません"
    if ": evidence_type is not measured" in value:
        return "実測として扱えない測定データがあります"
    if ": captured_at is missing or not timezone-aware" in value:
        return "測定時刻が不足している、またはタイムゾーンを確認できません"
    if ": captured before campaign preregistration" in value:
        return "検証条件の事前登録より前に取得された実測です"
    if ": validation_scope is not owned_room" in value:
        return "実室検証として登録されていない実測です"
    if ": validation campaign binding mismatch" in value:
        return "実測と検証条件の関連付けが一致しません"
    if value.startswith("unknown measurement "):
        return "測定データが見つかりません"
    return value


class ValidationControllerMixin:
    def assign_selected_candidate_to_campaign(self, split: str) -> None:
        candidate_id = self.search_selected_candidate_id
        if candidate_id is None:
            self.statusBar().showMessage('検証条件へ追加する候補を選択してください')
            return
        if split not in {'calibration', 'holdout'}:
            raise ValueError('検証条件の区分は調整用または検証用である必要があります')
        self.campaign_assignments[candidate_id] = split
        self._refresh_campaign_assignment_tree()
        self.statusBar().showMessage(
            f'候補を検証条件へ追加しました · {_split_label(split)}'
        )

    def remove_selected_campaign_assignment(self) -> None:
        tree = self.campaign_assignment_tree
        if tree is None:
            return
        item = tree.currentItem()
        candidate_id = None if item is None else item.data(0, ROLE)
        if not isinstance(candidate_id, str):
            return
        self.campaign_assignments.pop(candidate_id, None)
        self._refresh_campaign_assignment_tree()

    def _refresh_campaign_assignment_tree(self) -> None:
        tree = self.campaign_assignment_tree
        if tree is None:
            return
        tree.clear()
        for index, (candidate_id, split) in enumerate(
            self.campaign_assignments.items(), start=1
        ):
            item = QTreeWidgetItem([f"候補 {index}", _split_label(split)])
            item.setData(0, ROLE, candidate_id)
            tree.addTopLevelItem(item)

    def save_validation_campaign(self) -> None:
        spec = self._selected_search_spec()
        page = self.search_candidate_page
        if spec is None or page is None:
            self.statusBar().showMessage(
                '探索設定を選択して候補を生成してから検証条件を保存してください'
            )
            return
        if (
            self.working is None
            or not search_spec_current_working(
                spec,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.statusBar().showMessage(
                '部屋または制約が変更された探索設定から検証条件を保存できません'
            )
            return
        if page.search_spec_id != spec.search_spec_id:
            self.statusBar().showMessage(
                '表示中の候補と探索設定が一致しません · 候補を再生成してください'
            )
            return

        assignments = tuple(self.campaign_assignments.items())
        calibration = [candidate_id for candidate_id, split in assignments if split == 'calibration']
        holdout = [candidate_id for candidate_id, split in assignments if split == 'holdout']
        if len(calibration) < 1 or len(holdout) < 2:
            self.statusBar().showMessage(
                '検証条件には調整用1件以上と検証用2件以上の候補が必要です'
            )
            return

        model_version = (
            ''
            if self.campaign_model_version_field is None
            else self.campaign_model_version_field.text().strip()
        )
        low_hz = 20.0 if self.campaign_low_field is None else self.campaign_low_field.value()
        high_hz = 160.0 if self.campaign_high_field is None else self.campaign_high_field.value()
        max_residual = (
            0.0 if self.campaign_residual_field is None
            else self.campaign_residual_field.value()
        )
        max_sensitivity = (
            0.0 if self.campaign_sensitivity_field is None
            else self.campaign_sensitivity_field.value()
        )
        max_sensitivity_error = (
            0.0 if self.campaign_sensitivity_error_field is None
            else self.campaign_sensitivity_error_field.value()
        )
        separation_multiple = (
            0.0 if self.campaign_separation_field is None
            else self.campaign_separation_field.value()
        )
        if not model_version:
            self.statusBar().showMessage('検証に使うモデル版を入力してください')
            return
        if high_hz <= low_hz:
            self.statusBar().showMessage('検証帯域を確認してください')
            return
        if min(max_residual, max_sensitivity, max_sensitivity_error, separation_multiple) <= 0:
            self.statusBar().showMessage('検証閾値をすべて設定してください')
            return

        holdout_a, holdout_b = holdout[:2]
        try:
            campaign = build_validation_campaign(
                document_id=spec.document_id,
                search_spec_id=spec.search_spec_id,
                search_spec_sha256=spec.search_spec_sha256,
                candidate_set_sha256=page.candidate_set_sha256,
                model_id='rew-roomsim',
                model_version=model_version,
                requested_band_hz=(float(low_hz), float(high_hz)),
                max_holdout_rms_db=float(max_residual),
                candidates=tuple(
                    CadValidationCampaignCandidate(
                        candidate_id=candidate_id,
                        split=split,
                    )
                    for candidate_id, split in assignments
                ),
                objective_ids=('response.shape_rms_db',),
                target_response=CadValidationTargetResponse(
                    frequency_hz=(float(low_hz), float(high_hz)),
                    level_db=(0.0, 0.0),
                ),
                reference_band_hz=(float(low_hz), float(high_hz)),
                sensitivity=(
                    CadValidationCampaignSensitivity(
                        objective_id='response.shape_rms_db',
                        candidate_a_id=holdout_a,
                        candidate_b_id=holdout_b,
                        max_observed_sensitivity_per_m=float(max_sensitivity),
                        max_model_error_per_m=float(max_sensitivity_error),
                    ),
                ),
                repeatability=(
                    CadValidationCampaignRepeatability(
                        candidate_id=holdout_a,
                        min_measurements=2,
                    ),
                ),
                separation=(
                    CadValidationCampaignSeparation(
                        candidate_a_id=holdout_a,
                        candidate_b_id=holdout_b,
                        repeatability_candidate_id=holdout_a,
                        min_repeatability_multiple=float(separation_multiple),
                    ),
                ),
                required_applicability_codes=('geometry', 'band', 'routing'),
            )
            self.campaign_repository.save(campaign)
        except Exception as exc:
            self.statusBar().showMessage(f'検証条件を保存できません · {operation_error_message(exc)}')
            return

        self.campaign_assignments.clear()
        self._refresh_campaign_assignment_tree()
        self.refresh_validation_campaigns(select_campaign_id=campaign.campaign_id)
        self.statusBar().showMessage('検証条件を測定前に保存しました')

    def refresh_validation_campaigns(
        self,
        *,
        select_campaign_id: str | None = None,
    ) -> None:
        tree = self.campaign_tree
        if tree is None:
            return
        tree.clear()
        spec_id = self.search_selected_spec_id
        if spec_id is None:
            if self.campaign_detail_label is not None:
                self.campaign_detail_label.setText('検証条件が未選択です')
            return

        selected_item: QTreeWidgetItem | None = None
        try:
            campaigns = self.campaign_repository.list_for_search_spec(spec_id)
        except Exception as exc:
            self.statusBar().showMessage(f'検証条件を読み込めません · {operation_error_message(exc)}')
            return
        for display_index, campaign in enumerate(campaigns, start=1):
            try:
                readiness = self.campaign_service.readiness(campaign.campaign_id)
                missing_count = sum(
                    len(candidate.missing_reasons)
                    for candidate in readiness.candidates
                ) + len(readiness.missing_reasons)
                readiness_text = (
                    '準備完了'
                    if readiness.evidence_ready
                    else f'不足 {missing_count}件'
                )
            except Exception as exc:
                readiness_text = f'確認できません: {operation_error_message(exc)}'
            item = QTreeWidgetItem([
                f'検証条件 {display_index}',
                f'{campaign.model_id}/{campaign.model_version}',
                str(len(campaign.candidates)),
                readiness_text,
            ])
            item.setData(0, ROLE, campaign.campaign_id)
            tree.addTopLevelItem(item)
            if campaign.campaign_id == select_campaign_id:
                selected_item = item
        if selected_item is None and tree.topLevelItemCount() > 0:
            selected_item = tree.topLevelItem(tree.topLevelItemCount() - 1)
        if selected_item is not None:
            tree.setCurrentItem(selected_item)
        else:
            self._campaign_selected()

    def _selected_campaign(self):
        tree = self.campaign_tree
        if tree is None:
            return None
        item = tree.currentItem()
        campaign_id = None if item is None else item.data(0, ROLE)
        if not isinstance(campaign_id, str):
            return None
        return self.campaign_repository.get(campaign_id)

    def _campaign_selected(self) -> None:
        campaign = self._selected_campaign()
        label = self.campaign_detail_label
        if label is None:
            return
        if campaign is None:
            label.setText('検証条件が未選択です')
            return
        try:
            readiness = self.campaign_service.readiness(campaign.campaign_id)
            registration = self.campaign_repository.get_registration(
                campaign.campaign_id
            )
        except Exception as exc:
            label.setText(f'検証条件の準備状況を読み込めません · {operation_error_message(exc)}')
            return
        objective_text = ", ".join(
            _objective_label(value) for value in campaign.objective_ids
        )
        registered_text = (
            registration.registered_at_utc
            if registration is not None
            else '登録時刻なし'
        )
        lines = [
            f'モデル {campaign.model_id} / {campaign.model_version}',
            f'帯域 {campaign.requested_band_hz[0]:g}–{campaign.requested_band_hz[1]:g} Hz · '
            f'指標 {objective_text}',
            f'測定前に登録済み · {registered_text}',
        ]
        for candidate_index, candidate in enumerate(readiness.candidates, start=1):
            state = '準備完了' if not candidate.missing_reasons else '不足'
            lines.append(
                f'候補 {candidate_index} · {_split_label(candidate.split)} · {state} · '
                f'実測 {len(candidate.measurement_ids)}件'
            )
            lines.extend(
                f'  - 不足: {_reason_label(reason)}'
                for reason in candidate.missing_reasons
            )
        lines.extend(f'停止理由: {_reason_label(reason)}' for reason in readiness.missing_reasons)
        lines.append(
            '根拠データは準備完了です'
            if readiness.evidence_ready
            else '根拠データが不足しています · 本番の次候補計算は無効です'
        )
        label.setText('\n'.join(lines))

    def materialize_selected_campaign_objectives(self) -> None:
        campaign = self._selected_campaign()
        if campaign is None:
            self.statusBar().showMessage('検証条件を選択してください')
            return
        try:
            evaluation_ids = self.campaign_service.materialize_objective_evidence(
                campaign.campaign_id
            )
        except Exception as exc:
            self.statusBar().showMessage(f'比較指標の根拠データを生成できません · {operation_error_message(exc)}')
            self._campaign_selected()
            return
        self.refresh_validation_campaigns(select_campaign_id=campaign.campaign_id)
        self.statusBar().showMessage(
            f'比較指標の根拠データを確認・保存しました · {len(evaluation_ids)}件'
        )



    def build_and_save_selected_campaign_validation(self) -> None:
        campaign = self._selected_campaign()
        if campaign is None:
            self.statusBar().showMessage('検証条件を選択してください')
            return
        try:
            readiness = self.campaign_service.readiness(campaign.campaign_id)
        except Exception as exc:
            self.statusBar().showMessage(f'検証条件の準備状況を読み込めません · {operation_error_message(exc)}')
            return
        if not readiness.evidence_ready:
            self.statusBar().showMessage(
                '検証に必要な根拠データが不足しています · 表示された不足理由を解消してください'
            )
            self._campaign_selected()
            return

        requests: list[CadValidationApplicabilitySpec] = []
        for code in campaign.required_applicability_codes:
            state_widget = self.campaign_applicability_state.get(code)
            detail_widget = self.campaign_applicability_detail.get(code)
            if state_widget is None or detail_widget is None:
                self.statusBar().showMessage(
                    f'適用条件の入力欄が未対応です · {code}'
                )
                return
            state = str(state_widget.currentData())
            detail = detail_widget.text().strip()
            if state == 'auto':
                requests.append(CadValidationApplicabilitySpec(
                    code=code,
                    mode='auto',
                    detail=detail or None,
                ))
            elif state == 'manual':
                if not detail:
                    self.statusBar().showMessage(
                        f'{code}の手動証跡には登録済みattestation IDを入力してください'
                    )
                    return
                requests.append(CadValidationApplicabilitySpec(
                    code=code,
                    mode='manual',
                    attestation_id=detail,
                ))

        try:
            record = self.campaign_service.build_validation_record(
                campaign.campaign_id,
                tuple(requests),
            )
            self.validation_repository.save(record)
        except Exception as exc:
            self.statusBar().showMessage(f'検証結果を保存できません · {operation_error_message(exc)}')
            return

        self.refresh_model_validations()
        self.refresh_validation_campaigns(select_campaign_id=campaign.campaign_id)
        self.statusBar().showMessage(
            f'検証結果を保存しました · 推薦可否 {_gate_label(record.recommendation_gate)}'
        )

    def read_selected_rew_for_campaign_async(self) -> None:
        campaign = self._selected_campaign()
        plan = self._selected_measurement_plan()
        if campaign is None:
            self.statusBar().showMessage('検証条件を選択してください')
            return
        if plan is None:
            self.statusBar().showMessage('検証候補の実測計画を選択してください')
            return
        if plan.status != 'planned':
            self.statusBar().showMessage('REW読込には測定待ちの実測計画が必要です')
            return
        if (
            plan.search_spec_id != campaign.search_spec_id
            or plan.search_spec_sha256 != campaign.search_spec_sha256
            or plan.candidate_set_sha256 != campaign.candidate_set_sha256
        ):
            self.statusBar().showMessage('実測計画と検証条件の探索設定が一致しません')
            return
        if not any(
            assignment.candidate_id == plan.candidate_id
            for assignment in campaign.candidates
        ):
            self.statusBar().showMessage('実測計画の候補が選択中の検証条件に含まれていません')
            return
        if (
            self.working is None
            or self.working.is_dirty
            or self.working.source_revision_id != plan.applied_scene_revision_id
        ):
            self.statusBar().showMessage(
                '実測計画を作成した保存状態へ部屋を戻し、未保存の編集を解消してください'
            )
            return

        try:
            self._start_selected_rew_read(
                validation_scope='owned_room',
                validation_campaign_id=campaign.campaign_id,
                evidence_type_override='measured',
            )
        except Exception as exc:
            self.statusBar().showMessage(f'検証用REW読込を開始できません · {operation_error_message(exc)}')
            return


    @staticmethod
    def _validation_gate_summary(checks) -> str:
        if not checks:
            return '—'
        passed = sum(getattr(check, 'gate', None) == 'pass' for check in checks)
        failed = sum(getattr(check, 'gate', None) == 'fail' for check in checks)
        insufficient = sum(getattr(check, 'gate', None) == 'insufficient' for check in checks)
        parts = [f'合格 {passed}']
        if failed:
            parts.append(f'不合格 {failed}')
        if insufficient:
            parts.append(f'不足 {insufficient}')
        return ' / '.join(parts)

    def refresh_model_validations(self) -> None:
        tree = self.validation_tree
        if tree is None:
            return
        tree.clear()
        if self.validation_detail_label is not None:
            self.validation_detail_label.setText('検証結果が未選択です')
        spec_id = self.search_selected_spec_id
        if spec_id is None:
            return
        try:
            records = self.validation_repository.inspect_for_search_spec(spec_id)
        except Exception as exc:
            self.statusBar().showMessage(f'検証結果を読み込めません · {operation_error_message(exc)}')
            return
        for display_index, record in enumerate(records, start=1):
            repeatability = '—'
            if record.repeatability_checks:
                floors = ', '.join(
                    f'{check.rms_floor_db:.3g} dB'
                    for check in record.repeatability_checks
                )
                repeatability = f'{len(record.repeatability_checks)}組 · {floors}'
            item = QTreeWidgetItem([
                f'検証 {display_index}',
                _scope_label(record.evidence_scope),
                _gate_label(record.residual_gate),
                self._validation_gate_summary(record.trend_checks),
                self._validation_gate_summary(record.sensitivity_checks),
                repeatability,
                _gate_label(record.recommendation_gate),
            ])
            item.setData(0, ROLE, record.validation_id)
            tree.addTopLevelItem(item)

    def _validation_selected(self) -> None:
        tree = self.validation_tree
        label = self.validation_detail_label
        if tree is None or label is None:
            return
        item = tree.currentItem()
        validation_id = None if item is None else item.data(0, ROLE)
        if not isinstance(validation_id, str):
            label.setText('検証結果が未選択です')
            return
        record = self.validation_repository.inspect(validation_id)
        if record is None:
            label.setText('検証結果が見つかりません')
            return

        calibration_count = sum(pair.split == 'calibration' for pair in record.pairs)
        holdout_count = sum(pair.split == 'holdout' for pair in record.pairs)
        lines = [
            f'モデル {record.model_id} / {record.model_version}',
            f'調整用 {calibration_count} · 検証用 {holdout_count} · '
            f'帯域 {record.requested_band_hz[0]:g}–{record.requested_band_hz[1]:g} Hz',
            f'残差 {_gate_label(record.residual_gate)} · 検証用 RMS '
            f'{record.holdout_rms_db if record.holdout_rms_db is not None else "—"} dB',
        ]
        for check in record.trend_checks:
            agreement = '—' if check.agreement_ratio is None else f'{check.agreement_ratio:.3f}'
            lines.append(
                f'傾向 {_objective_label(check.objective_id)}: {_gate_label(check.gate)} · '
                f'一致率 {agreement} · 比較可能 {check.comparable_pairs}組'
            )
        for check in record.sensitivity_checks:
            lines.append(
                f'感度 {_objective_label(check.objective_id)}: {_gate_label(check.gate)} · '
                f'観測 {check.observed_sensitivity_per_m:.3g} {check.unit}/m · '
                f'モデル誤差 {check.model_error_per_m:.3g} {check.unit}/m'
            )
        for check in record.repeatability_checks:
            lines.append(
                f'再現性: {check.rms_floor_db:.3g} dB · {len(check.measurement_ids)}回'
            )
        for check in record.separation_checks:
            ratio = '∞' if check.separation_ratio is None and check.response_difference_rms_db > 0 else (
                '—' if check.separation_ratio is None else f'{check.separation_ratio:.3g}×'
            )
            lines.append(
                f'候補差: {_gate_label(check.gate)} · {ratio} 再現性'
            )
        for check in record.applicability_checks:
            note = f' · {check.detail}' if check.detail else ''
            lines.append(
                f'{_APPLICABILITY_LABELS.get(check.code, check.code)}: '
                f'{"合格" if check.passed else "不合格"} · '
                f'{applicability_authority_summary(check)}{note}'
            )
        lines.append(f'推薦可否: {_gate_label(record.recommendation_gate)}')
        if record.gate_reasons:
            lines.extend(f'停止理由: {reason}' for reason in record.gate_reasons)
        label.setText('\n'.join(lines))

    def _selected_validation_record(self):
        tree = self.validation_tree
        if tree is None:
            return None
        item = tree.currentItem()
        validation_id = None if item is None else item.data(0, ROLE)
        if not isinstance(validation_id, str):
            return None
        return self.validation_repository.get(validation_id)

