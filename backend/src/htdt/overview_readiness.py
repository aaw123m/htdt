from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    gate_measurement_claim,
    phase_response_capability,
)
from .cad_model_validation import CadModelValidationRecord
from .cad_prediction_models import CadPredictionResult
from .cad_repository import SceneRevision
from .cad_search_models import CadSearchSpec
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


OverviewSeverity = Literal['blocker', 'warning']
OverviewNavigationTarget = WorkspaceDeepLink


@dataclass(frozen=True, slots=True)
class OverviewAction:
    action_id: str
    label: str
    target: OverviewNavigationTarget


@dataclass(frozen=True, slots=True)
class OverviewNotice:
    code: str
    severity: OverviewSeverity
    message: str
    action: OverviewAction | None = None


@dataclass(frozen=True, slots=True)
class OverviewReadinessViewModel:
    """Read-only presentation model for the workflow-first Overview."""

    summary: str
    blockers: tuple[OverviewNotice, ...]
    warnings: tuple[OverviewNotice, ...]
    next_action: OverviewAction | None
    optimization_ready: bool


class SceneReadSource(Protocol):
    def latest(self, document_id: str) -> SceneRevision | None: ...


class MeasurementReadSource(Protocol):
    def list_measurements(self, document_id: str) -> tuple[CadMeasurementRecord, ...]: ...

    def dataset_for_measurement(
        self,
        measurement_id: str,
    ) -> CadFrequencyResponseDataset | None: ...


class MeasurementQualityReadSource(Protocol):
    """Replay-validated quality authority for persisted measurements.

    ``CadMeasurementQualityRepository.latest_report`` replays the pinned
    algorithm before returning a report, so capability decisions consumed
    here are never trusted on payload alone.
    """

    def latest_report(
        self,
        measurement_id: str,
    ) -> CadMeasurementQualityReport | None: ...


class PredictionReadSource(Protocol):
    def list_results(self, document_id: str) -> tuple[CadPredictionResult, ...]: ...


class SearchReadSource(Protocol):
    def list_specs(self, document_id: str) -> tuple[CadSearchSpec, ...]: ...


class ValidationReadSource(Protocol):
    def inspect_for_search_spec(
        self,
        search_spec_id: str,
    ) -> tuple[CadModelValidationRecord, ...]: ...


ROOM_GEOMETRY = OverviewNavigationTarget(WorkspaceId.ROOM, 'geometry')
ROOM_PLACEMENT = OverviewNavigationTarget(WorkspaceId.ROOM, 'placement')
ROOM_ACOUSTICS = OverviewNavigationTarget(WorkspaceId.ROOM, 'acoustics')
MEASUREMENT_IMPORT = OverviewNavigationTarget(WorkspaceId.MEASUREMENT, 'import')
MEASUREMENT_QUALITY = OverviewNavigationTarget(WorkspaceId.MEASUREMENT, 'quality')
OPTIMIZATION_SETUP = OverviewNavigationTarget(WorkspaceId.OPTIMIZATION, 'setup')
OPTIMIZATION_VALIDATION = OverviewNavigationTarget(WorkspaceId.OPTIMIZATION, 'validation')


def _action(
    action_id: str,
    label: str,
    target: OverviewNavigationTarget,
) -> OverviewAction:
    return OverviewAction(action_id=action_id, label=label, target=target)


def _validation_reason_ja(reasons: tuple[str, ...]) -> str:
    """Collapse authority-owned gate diagnostics into short user-facing Japanese.

    Raw reasons may contain objective/candidate identifiers. Overview deliberately
    maps them to stable categories instead of leaking internal identifiers.
    """

    for reason in reasons:
        if reason == 'automatic recommendation requires owned-room evidence':
            return '実室の検証データが必要です。'
        if reason == 'independent calibration evidence is required':
            return '校正用の実測が不足しています。'
        if reason == 'independent holdout evidence is required':
            return '検証用の実測が不足しています。'
        if reason == 'calibration and holdout candidate sets must be disjoint':
            return '校正用と検証用の測定を分けてください。'
        if reason == 'holdout residual gate is insufficient':
            return '検証用の実測が不足しています。'
        if reason == 'holdout residual gate is fail':
            return '検証誤差の条件を満たしていません。'
        if reason == 'holdout objective trend evidence is required':
            return '目的指標の傾向検証が不足しています。'
        if reason.startswith('objective trend '):
            if reason.endswith(' check is missing') or 'fewer than two holdout candidates' in reason:
                return '目的指標の傾向検証が不足しています。'
            return '目的指標の傾向が検証条件を満たしていません。'
        if reason == 'placement sensitivity evidence is required':
            return '配置変化に対する感度検証が不足しています。'
        if reason.startswith('placement sensitivity '):
            return '配置変化に対する感度検証が成立していません。'
        if reason == 'same-condition repeatability evidence is required':
            return '同一条件の再測定が不足しています。'
        if reason == 'candidate separation vs repeatability evidence is required':
            return '候補差と測定ばらつきの検証が不足しています。'
        if reason.startswith('candidate separation '):
            return '候補差が測定ばらつきを十分に上回っていません。'
        if reason == 'model applicability checks are required':
            return 'モデルの適用条件を確認してください。'
        if reason.startswith('model applicability failed:'):
            return 'モデルの適用条件を満たしていません。'
    return '検証条件を満たしていません。'


class OverviewReadinessService:
    """Aggregate existing authorities into a read-only Overview view model.

    Dependencies are injected as read protocols. This class never constructs a
    repository, writes storage, mutates SceneDocument, or recomputes domain gates.
    """

    def __init__(
        self,
        scene_source: SceneReadSource,
        measurement_source: MeasurementReadSource,
        prediction_source: PredictionReadSource,
        search_source: SearchReadSource,
        validation_source: ValidationReadSource,
        quality_source: MeasurementQualityReadSource | None = None,
    ) -> None:
        self._scene_source = scene_source
        self._measurement_source = measurement_source
        self._prediction_source = prediction_source
        self._search_source = search_source
        self._validation_source = validation_source
        self._quality_source = quality_source

    def read(
        self,
        document_id: str,
        *,
        constraint_workspace_hash: str | None = None,
    ) -> OverviewReadinessViewModel:
        revision = self._scene_source.latest(document_id)
        if revision is None:
            action = _action('room.create', '部屋を作成', ROOM_GEOMETRY)
            blocker = OverviewNotice(
                code='room.missing',
                severity='blocker',
                message='保存された部屋がありません。',
                action=action,
            )
            return OverviewReadinessViewModel(
                summary='まず部屋を作成してください。',
                blockers=(blocker,),
                warnings=(),
                next_action=action,
                optimization_ready=False,
            )

        blockers: list[OverviewNotice] = []
        warnings: list[OverviewNotice] = []
        document = revision.document

        if document.room is None:
            action = _action('room.complete_geometry', '部屋を完成させる', ROOM_GEOMETRY)
            blockers.append(
                OverviewNotice(
                    code='room.geometry_incomplete',
                    severity='blocker',
                    message='部屋形状がまだありません。',
                    action=action,
                )
            )

        speakers = tuple(entity for entity in document.entities if entity.kind == 'speaker')
        if not speakers:
            action = _action('room.add_speaker', 'スピーカーを追加', ROOM_PLACEMENT)
            blockers.append(
                OverviewNotice(
                    code='speaker.missing',
                    severity='blocker',
                    message='スピーカーがありません。',
                    action=action,
                )
            )
        else:
            missing_roles = tuple(speaker for speaker in speakers if not speaker.speaker_role)
            if missing_roles:
                speaker = missing_roles[0]
                action = _action(
                    'room.assign_speaker_role',
                    '役割を設定',
                    OverviewNavigationTarget(
                        'room',
                        'placement',
                        entity_id=speaker.entity_id,
                    ),
                )
                blockers.append(
                    OverviewNotice(
                        code='speaker.role_missing',
                        severity='blocker',
                        message='役割が未設定のスピーカーがあります。',
                        action=action,
                    )
                )

        measurements = self._measurement_source.list_measurements(document_id)
        if not measurements:
            warnings.append(
                OverviewNotice(
                    code='measurement.missing',
                    severity='warning',
                    message='測定データがありません。',
                    action=_action(
                        'measurement.import_rew',
                        'REWを読み込む',
                        MEASUREMENT_IMPORT,
                    ),
                )
            )
        else:
            # Phase availability and common timing are distinct canonical
            # capability claims (#466): valid phase samples never imply a
            # shared timing reference. Only a replay-validated quality report
            # can establish common_timing; without one it fails closed.
            phase_available = False
            timing_established = False
            for measurement in measurements:
                dataset = self._measurement_source.dataset_for_measurement(
                    measurement.measurement_id
                )
                if dataset is None:
                    continue
                report = (
                    self._quality_source.latest_report(measurement.measurement_id)
                    if self._quality_source is not None
                    else None
                )
                if report is not None and report.dataset_id == dataset.dataset_id:
                    phase_decision = gate_measurement_claim(
                        report, 'phase_response'
                    ).decision
                    timing_decision = gate_measurement_claim(
                        report, 'common_timing'
                    ).decision
                else:
                    phase_decision = phase_response_capability(dataset).decision
                    timing_decision = 'UNKNOWN'
                if phase_decision == 'ALLOWED':
                    phase_available = True
                if timing_decision == 'ALLOWED':
                    timing_established = True
            if timing_established:
                pass
            elif phase_available:
                warnings.append(
                    OverviewNotice(
                        code='measurement.common_timing_unverified',
                        severity='warning',
                        message=(
                            '位相データは利用できますが、共通タイミング基準が確認できません。'
                            '測定間のタイミング/位相比較には品質レポートの確認が必要です。'
                        ),
                        action=_action(
                            'measurement.review_quality',
                            '測定品質を確認',
                            MEASUREMENT_QUALITY,
                        ),
                    )
                )
            else:
                warnings.append(
                    OverviewNotice(
                        code='measurement.phase_timing_unavailable',
                        severity='warning',
                        message='タイミング/位相比較に使える測定が確認できません。',
                        action=_action(
                            'measurement.review_quality',
                            '測定品質を確認',
                            MEASUREMENT_QUALITY,
                        ),
                    )
                )

        prediction_results = self._prediction_source.list_results(document_id)
        completed_predictions = tuple(
            result for result in prediction_results if result.status == 'completed'
        )
        current_predictions = tuple(
            result
            for result in completed_predictions
            if result.scene_revision_id == revision.revision_id
            and result.scene_content_hash == revision.content_hash
            and getattr(result, 'geometry_compatibility', None) != 'unsupported'
        )
        prediction_action: OverviewAction | None = None
        unsupported_current_predictions = tuple(
            result
            for result in completed_predictions
            if result.scene_revision_id == revision.revision_id
            and result.scene_content_hash == revision.content_hash
            and getattr(result, 'geometry_compatibility', None) == 'unsupported'
        )
        if not current_predictions and unsupported_current_predictions:
            prediction_action = _action(
                'prediction.review_geometry',
                '部屋形状を確認',
                ROOM_GEOMETRY,
            )
            warnings.append(
                OverviewNotice(
                    code='prediction.unsupported_geometry',
                    severity='warning',
                    message=(
                        '現在の部屋形状は予測モデルに対応していません。'
                        '矩形の部屋に変更すると予測できます。'
                    ),
                    action=prediction_action,
                )
            )
        elif not current_predictions:
            if completed_predictions:
                code = 'prediction.stale'
                message = '条件が変更されています。予測を再計算してください。'
                label = '予測を再計算'
            else:
                code = 'prediction.missing'
                message = '予測をまだ実行していません。'
                label = '予測を実行'
            prediction_action = _action('prediction.run', label, ROOM_ACOUSTICS)
            warnings.append(
                OverviewNotice(
                    code=code,
                    severity='warning',
                    message=message,
                    action=prediction_action,
                )
            )

        validation = self._latest_relevant_validation(
            revision,
            constraint_workspace_hash=constraint_workspace_hash,
        )
        validation_action: OverviewAction | None = None
        if validation is not None and validation.recommendation_gate == 'disabled':
            validation_action = _action(
                'optimization.review_validation',
                '検証を確認',
                OPTIMIZATION_VALIDATION,
            )
            blockers.append(
                OverviewNotice(
                    code='validation.recommendation_blocked',
                    severity='blocker',
                    message='自動推薦はまだ利用できません。' + _validation_reason_ja(validation.gate_reasons),
                    action=validation_action,
                )
            )

        setup_blocked = any(
            notice.code
            in {
                'room.geometry_incomplete',
                'speaker.missing',
                'speaker.role_missing',
            }
            for notice in blockers
        )
        optimization_ready = not setup_blocked and bool(current_predictions)

        next_action = self._next_action(
            blockers,
            prediction_action=prediction_action,
            validation_action=validation_action,
            optimization_ready=optimization_ready,
        )
        summary = self._summary(
            blockers,
            prediction_action=prediction_action,
            validation_action=validation_action,
            optimization_ready=optimization_ready,
        )
        return OverviewReadinessViewModel(
            summary=summary,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            next_action=next_action,
            optimization_ready=optimization_ready,
        )

    def _latest_relevant_validation(
        self,
        revision: SceneRevision,
        *,
        constraint_workspace_hash: str | None,
    ) -> CadModelValidationRecord | None:
        specs = tuple(
            spec
            for spec in self._search_source.list_specs(revision.document_id)
            if spec.scene_revision_id == revision.revision_id
            and spec.scene_content_hash == revision.content_hash
            and (
                constraint_workspace_hash is None
                or spec.constraint_workspace_hash == constraint_workspace_hash
            )
        )
        if not specs:
            return None
        # Advisory display only: stale history must stay browsable, so the
        # readiness overview uses the non-authoritative inspect view. Any
        # production authorization still goes through the repository's
        # re-attesting reads and fails closed on stale evidence.
        records = self._validation_source.inspect_for_search_spec(
            specs[-1].search_spec_id
        )
        return records[-1] if records else None

    @staticmethod
    def _next_action(
        blockers: list[OverviewNotice],
        *,
        prediction_action: OverviewAction | None,
        validation_action: OverviewAction | None,
        optimization_ready: bool,
    ) -> OverviewAction | None:
        setup_codes = {
            'room.geometry_incomplete',
            'speaker.missing',
            'speaker.role_missing',
        }
        for notice in blockers:
            if notice.code in setup_codes and notice.action is not None:
                return notice.action
        if prediction_action is not None:
            return prediction_action
        if validation_action is not None:
            return validation_action
        if optimization_ready:
            return _action('optimization.open_setup', '最適化を始める', OPTIMIZATION_SETUP)
        return None

    @staticmethod
    def _summary(
        blockers: list[OverviewNotice],
        *,
        prediction_action: OverviewAction | None,
        validation_action: OverviewAction | None,
        optimization_ready: bool,
    ) -> str:
        setup_codes = {
            'room.geometry_incomplete': '部屋形状を完成させてください。',
            'speaker.missing': '次にスピーカーを追加してください。',
            'speaker.role_missing': 'スピーカーの役割を設定してください。',
        }
        for notice in blockers:
            summary = setup_codes.get(notice.code)
            if summary is not None:
                return summary
        if prediction_action is not None:
            if prediction_action.action_id == 'prediction.review_geometry':
                return '現在の部屋形状では予測できません。'
            return '次に予測を実行してください。'
        if validation_action is not None:
            return '自動推薦の前に検証状態を確認してください。'
        if optimization_ready:
            return '最適化の準備ができています。'
        return '現在の状態を確認してください。'
