from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .cad_dependency_impact import WatchedArtifact, build_dependency_impact_report
from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    gate_measurement_claim,
    phase_response_capability,
)
from .cad_model_validation import CadModelValidationRecord
from .cad_prediction_models import CadPredictionResult
from .cad_repository import SceneRevision
from .cad_scene import duplicated_speaker_roles, is_unassigned_speaker_role
from .cad_search_models import CadSearchSpec
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


OverviewSeverity = Literal['blocker', 'warning']
OverviewNavigationTarget = WorkspaceDeepLink


class OverviewArea:
    """Lifecycle areas used to group Overview notices (#443)."""

    ROOM = 'room'
    EQUIPMENT = 'equipment'
    MEASUREMENT = 'measurement'
    PREDICTION = 'prediction'
    OPTIMIZATION = 'optimization'
    VARIANT = 'variant'


OverviewAreaId = str

#: Display order and Japanese labels for lifecycle grouping.
OVERVIEW_AREA_LABELS: dict[str, str] = {
    OverviewArea.ROOM: '部屋',
    OverviewArea.EQUIPMENT: '機材・ソース',
    OverviewArea.MEASUREMENT: '測定',
    OverviewArea.PREDICTION: '予測',
    OverviewArea.OPTIMIZATION: '最適化・検証',
    OverviewArea.VARIANT: 'SystemVariant',
}
OVERVIEW_AREA_ORDER: tuple[str, ...] = (
    OverviewArea.ROOM,
    OverviewArea.EQUIPMENT,
    OverviewArea.MEASUREMENT,
    OverviewArea.PREDICTION,
    OverviewArea.VARIANT,
    OverviewArea.OPTIMIZATION,
)

_NOTICE_AREA: dict[str, str] = {
    'room.missing': OverviewArea.ROOM,
    'room.geometry_incomplete': OverviewArea.ROOM,
    'speaker.missing': OverviewArea.ROOM,
    'speaker.role_missing': OverviewArea.ROOM,
    'speaker.role_duplicate': OverviewArea.ROOM,
    'measurement.missing': OverviewArea.MEASUREMENT,
    'measurement.common_timing_unverified': OverviewArea.MEASUREMENT,
    'measurement.phase_timing_unavailable': OverviewArea.MEASUREMENT,
    'equipment.binding_missing': OverviewArea.EQUIPMENT,
    'prediction.unsupported_geometry': OverviewArea.PREDICTION,
    'prediction.stale': OverviewArea.PREDICTION,
    'prediction.missing': OverviewArea.PREDICTION,
    'validation.recommendation_blocked': OverviewArea.OPTIMIZATION,
}


def notice_area(code: str) -> str:
    """Map a notice code to its lifecycle area (impact.* -> optimization)."""
    if code in _NOTICE_AREA:
        return _NOTICE_AREA[code]
    if code.startswith('impact.'):
        return OverviewArea.OPTIMIZATION
    return OverviewArea.OPTIMIZATION


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

    @property
    def area(self) -> str:
        return notice_area(self.code)

    @property
    def state_label(self) -> str:
        """Non-color state signal for cards (color alone never gates UX)."""
        return '要対応' if self.severity == 'blocker' else '警告'


#: Coarse lifecycle bucket for an active SystemVariant, combining the
#: physical lifecycle and the measurement campaign stage.
OverviewVariantStage = Literal[
    'current',
    'proposed',
    'applied',
    'as_built',
    'campaign_preregistered',
    'measured_unvalidated',
]


@dataclass(frozen=True, slots=True)
class OverviewVariantState:
    """Per-variant lifecycle presentation on the Overview (#443)."""

    variant_id: str
    name: str
    stage: OverviewVariantStage
    lifecycle_label: str
    stage_label: str
    detail: str
    action: OverviewAction | None = None


_VARIANT_STAGE_LABELS: dict[str, str] = {
    'current': '現在のシステム',
    'proposed': '提案のみ',
    'applied': '適用済み（設置記録なし）',
    'as_built': '設置済み',
    'campaign_preregistered': '測定キャンペーン登録済み',
    'measured_unvalidated': '実測済み・未検証',
}


@dataclass(frozen=True, slots=True)
class OverviewActivityItem:
    """One row of the Overview 'Recent activity' strip (#615)."""

    event_id: str
    kind: str
    title: str
    occurred_at_utc: str
    deep_link: str | None


@dataclass(frozen=True, slots=True)
class OverviewReadinessViewModel:
    """Read-only presentation model for the workflow-first Overview."""

    summary: str
    blockers: tuple[OverviewNotice, ...]
    warnings: tuple[OverviewNotice, ...]
    next_action: OverviewAction | None
    optimization_ready: bool
    variant_states: tuple[OverviewVariantState, ...] = ()
    recent_activity: tuple[OverviewActivityItem, ...] = ()


class SceneReadSource(Protocol):
    def latest(self, document_id: str) -> SceneRevision | None: ...


class SceneRevisionReadSource(Protocol):
    """Fetches a committed revision by id (used for head-vs-parent impact)."""

    def get(self, revision_id: str) -> SceneRevision | None: ...


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


class ActivityReadSource(Protocol):
    def recent(
        self, document_id: str, *, limit: int = 8
    ) -> tuple[Any, ...]: ...


class ValidationReadSource(Protocol):
    def inspect_for_search_spec(
        self,
        search_spec_id: str,
    ) -> tuple[CadModelValidationRecord, ...]: ...


class VariantLifecycleReadSource(Protocol):
    """SystemVariant lifecycle + measurement-campaign stage (#443).

    Satisfied by ``SystemExpansionWorkflowService`` — the same authority the
    Optimization workspace reads — so Overview never recomputes the lifecycle.
    """

    def variants(self) -> tuple: ...

    def lifecycle(self, variant_id: str): ...

    def measurement(self, variant_id: str): ...


class EquipmentBindingReadSource(Protocol):
    """Latest equipment/source binding for a scene entity (#443)."""

    def get_binding_for_entity(self, document_id: str, entity_id: str): ...


ROOM_GEOMETRY = OverviewNavigationTarget(WorkspaceId.ROOM, 'geometry')
ROOM_PLACEMENT = OverviewNavigationTarget(WorkspaceId.ROOM, 'placement')
ROOM_OBJECTS = OverviewNavigationTarget(WorkspaceId.ROOM, 'objects')
ROOM_ACOUSTICS = OverviewNavigationTarget(WorkspaceId.ROOM, 'acoustics')
MEASUREMENT_IMPORT = OverviewNavigationTarget(WorkspaceId.MEASUREMENT, 'import')
MEASUREMENT_QUALITY = OverviewNavigationTarget(WorkspaceId.MEASUREMENT, 'quality')
MEASUREMENT_CAMPAIGN = OverviewNavigationTarget(WorkspaceId.MEASUREMENT, 'campaign')
OPTIMIZATION_SETUP = OverviewNavigationTarget(WorkspaceId.OPTIMIZATION, 'setup')
OPTIMIZATION_COMPARISON = OverviewNavigationTarget(WorkspaceId.OPTIMIZATION, 'comparison')
OPTIMIZATION_VALIDATION = OverviewNavigationTarget(WorkspaceId.OPTIMIZATION, 'validation')


def _action(
    action_id: str,
    label: str,
    target: OverviewNavigationTarget,
) -> OverviewAction:
    return OverviewAction(action_id=action_id, label=label, target=target)


def _variant_stage(lifecycle, measurement) -> OverviewVariantStage:
    """Combine the physical lifecycle with the campaign stage (#443).

    ``applied`` means an application exists without an as-built record;
    ``measured_unvalidated`` means campaign measurement completed while
    O60/R180 validation is still pending.
    """
    state = getattr(lifecycle, 'state', 'proposed')
    if state == 'current':
        return 'current'
    if state == 'proposed':
        # #914: an applied proposal (SystemVariantApplication exists, no
        # AsBuilt) is not merely "proposed" — surface the application.
        if getattr(lifecycle, 'application_exists', False):
            return 'applied'
        return 'proposed'
    if state == 'as_built':
        mstate = getattr(measurement, 'state', 'unplanned')
        if mstate in ('campaign_preregistered', 'evidence_incomplete'):
            return 'campaign_preregistered'
        return 'as_built'
    return 'measured_unvalidated'


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
        activity_source: ActivityReadSource | None = None,
        impact_source: SceneRevisionReadSource | None = None,
        variant_source: VariantLifecycleReadSource | None = None,
        equipment_source: EquipmentBindingReadSource | None = None,
    ) -> None:
        self._scene_source = scene_source
        self._measurement_source = measurement_source
        self._prediction_source = prediction_source
        self._search_source = search_source
        self._validation_source = validation_source
        self._quality_source = quality_source
        self._activity_source = activity_source
        self._impact_source = impact_source
        self._variant_source = variant_source
        self._equipment_source = equipment_source

    def read(
        self,
        document_id: str,
        *,
        constraint_workspace_hash: str | None = None,
    ) -> OverviewReadinessViewModel:
        revision = self._scene_source.current_head(document_id)
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
            # Reserved placeholder tokens (UNASSIGNED-<n>, legacy SPK) are not
            # channel identities: they fail the role prerequisite just like an
            # empty role, so "not yet assigned" can never pass as a real role.
            missing_roles = tuple(
                speaker
                for speaker in speakers
                if is_unassigned_speaker_role(speaker.speaker_role)
            )
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
            duplicated = duplicated_speaker_roles(speakers)
            if duplicated:
                target = next(
                    (
                        speaker
                        for speaker in speakers
                        if (speaker.speaker_role or '').strip() == duplicated[0]
                    ),
                    speakers[0],
                )
                action = _action(
                    'room.resolve_speaker_role',
                    '役割を整理',
                    OverviewNavigationTarget(
                        'room',
                        'placement',
                        entity_id=target.entity_id,
                    ),
                )
                blockers.append(
                    OverviewNotice(
                        code='speaker.role_duplicate',
                        severity='blocker',
                        message='同じ役割が複数のスピーカーに割り当てられています。',
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

        warnings.extend(
            self._equipment_notices(document_id, speakers)
        )
        warnings.extend(
            self._impact_notices(
                revision,
                completed_predictions=completed_predictions,
                measurements=measurements,
            )
        )

        variant_states = self._variant_states()

        setup_blocked = any(
            notice.code
            in {
                'room.geometry_incomplete',
                'speaker.missing',
                'speaker.role_missing',
                'speaker.role_duplicate',
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
            variant_states=variant_states,
            recent_activity=self._recent_activity(document_id),
        )

    def _recent_activity(self, document_id: str) -> tuple[OverviewActivityItem, ...]:
        if self._activity_source is None:
            return ()
        return tuple(
            OverviewActivityItem(
                event_id=event.event_id,
                kind=event.kind,
                title=event.title,
                occurred_at_utc=event.occurred_at_utc,
                deep_link=event.deep_link,
            )
            for event in self._activity_source.recent(document_id, limit=8)
        )

    def _equipment_notices(
        self,
        document_id: str,
        speakers: tuple,
    ) -> tuple[OverviewNotice, ...]:
        """Relevant-only equipment/source readiness (#443).

        Shown only while real speakers exist and at least one lacks a
        persisted equipment binding — equipment/source capability gates
        prediction quality, so an empty or bound-complete rig stays silent.
        """
        if self._equipment_source is None or not speakers:
            return ()
        unbound = tuple(
            speaker
            for speaker in speakers
            if self._equipment_source.get_binding_for_entity(
                document_id, speaker.entity_id
            )
            is None
        )
        if not unbound:
            return ()
        return (
            OverviewNotice(
                code='equipment.binding_missing',
                severity='warning',
                message=(
                    f'{len(unbound)}台のスピーカーに機材・ソースモデルが'
                    '設定されていません。予測の精度が制限されます。'
                ),
                action=_action(
                    'equipment.bind_speakers',
                    '機材を設定',
                    ROOM_OBJECTS,
                ),
            ),
        )

    def _variant_states(self) -> tuple[OverviewVariantState, ...]:
        """Per-variant lifecycle + measurement stage (#443, read-only)."""
        if self._variant_source is None:
            return ()
        states: list[OverviewVariantState] = []
        for variant in self._variant_source.variants():
            lifecycle = self._variant_source.lifecycle(variant.variant_id)
            measurement = self._variant_source.measurement(variant.variant_id)
            stage = _variant_stage(lifecycle, measurement)
            if stage == 'current':
                action = None
            elif stage == 'proposed':
                action = _action(
                    'variant.review_comparison',
                    '比較を確認',
                    OPTIMIZATION_COMPARISON,
                )
            elif stage == 'applied':
                # An applied proposal awaits AsBuilt confirmation, which
                # lives on the optimization validation surface (#914).
                action = _action(
                    'variant.record_as_built',
                    '実設置を記録',
                    OPTIMIZATION_VALIDATION,
                )
            elif stage == 'campaign_preregistered':
                action = _action(
                    'variant.open_campaign',
                    'キャンペーンを確認',
                    MEASUREMENT_CAMPAIGN,
                )
            else:
                action = _action(
                    'variant.open_validation',
                    '検証へ進む',
                    OPTIMIZATION_VALIDATION,
                )
            states.append(
                OverviewVariantState(
                    variant_id=variant.variant_id,
                    name=getattr(variant, 'name', variant.variant_id),
                    stage=stage,
                    lifecycle_label=getattr(lifecycle, 'label', stage),
                    stage_label=_VARIANT_STAGE_LABELS[stage],
                    detail=(
                        f'{getattr(lifecycle, "validation_label", "")} · '
                        f'{getattr(measurement, "state_label", "")}'
                    ).strip(' ·'),
                    action=action,
                )
            )
        return tuple(states)

    _IMPACT_KIND_JA: dict[str, str] = {
        'prediction': '予測',
        'coverage_evaluation': '指向カバレッジ評価',
        'direct_level_evaluation': 'ダイレクトレベル評価',
        'seat_priority_profile': 'リスニング集団',
        'cost_evaluation': 'コスト評価',
        'measurement_plan': '測定計画',
        'measured_dataset': '測定データ',
        'calibration_plan': '校正計画',
        'installation_report': '設置レポート',
        'optimization_run': '最適化検索',
        'design_comparison': '設計比較',
        'treatment_plan': '音響処理計画',
        'commissioning_plan': '導入調整計画',
        'video_geometry': '映像ジオメトリ',
    }
    _IMPACT_ACTION_JA: dict[str, str] = {
        'recompute': '再計算が必要です',
        're_evaluate': '再評価が必要です',
        're_import': '再読み込みが必要です',
        'remeasure': '再測定が必要です',
        're_commission': '再調整・再校正が必要です',
    }

    def _impact_notices(
        self,
        revision: SceneRevision,
        *,
        completed_predictions: tuple[CadPredictionResult, ...],
        measurements: tuple[CadMeasurementRecord, ...],
    ) -> tuple[OverviewNotice, ...]:
        """Summarize the head-vs-parent change into stale/uncertain notices (#561).

        Read-only: exact revision/hash bindings are reused as-is, and the
        report never deletes or rewrites artifacts. Items already bound to the
        head revision, and items whose watched axes did not change, stay quiet.
        """

        if self._impact_source is None or revision.parent_revision_id is None:
            return ()
        parent = self._impact_source.get(revision.parent_revision_id)
        if parent is None:
            return ()

        artifacts: list[WatchedArtifact] = []
        for result in completed_predictions:
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='prediction',
                    artifact_id=result.result_id,
                    bound_revision_id=result.scene_revision_id,
                    bound_content_hash=result.scene_content_hash,
                    watched_axes=frozenset(
                        {
                            'geometry',
                            'source_equipment',
                            'material_boundary',
                            'operating_state',
                            'solver_provider',
                        }
                    ),
                )
            )
        for spec in self._search_source.list_specs(revision.document_id):
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='optimization_run',
                    artifact_id=spec.search_spec_id,
                    bound_revision_id=spec.scene_revision_id,
                    bound_content_hash=spec.scene_content_hash,
                    watched_axes=frozenset(
                        {
                            'geometry',
                            'source_equipment',
                            'material_boundary',
                            'target_design',
                        }
                    ),
                )
            )
        for measurement in measurements:
            artifacts.append(
                WatchedArtifact(
                    artifact_kind='measured_dataset',
                    artifact_id=measurement.measurement_id,
                    bound_revision_id=measurement.scene_revision_id,
                    bound_content_hash=measurement.scene_content_hash,
                    watched_axes=frozenset({'measurement_context'}),
                )
            )
        if not artifacts:
            return ()

        report = build_dependency_impact_report(
            from_revision=parent,
            to_revision=revision,
            artifacts=artifacts,
        )
        notices: list[OverviewNotice] = []
        actionable = (
            item
            for item in report.impacts
            if item.state == 'uncertain'
            or (item.state == 'stale' and item.action != 'none')
        )
        for item in list(actionable)[:4]:
            kind_ja = self._IMPACT_KIND_JA.get(item.artifact_kind, '成果物')
            action_ja = self._IMPACT_ACTION_JA.get(item.action, '再評価が必要です')
            uncertainty = (
                '（依存関係を確定できないため保守的に表示）'
                if item.state == 'uncertain'
                else ''
            )
            notices.append(
                OverviewNotice(
                    code=f'impact.{item.state}',
                    severity='warning',
                    message=(
                        f'{kind_ja}「{item.artifact_id}」は'
                        f'{action_ja}。{uncertainty}'
                    ),
                )
            )
        return tuple(notices)

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
            'speaker.role_duplicate',
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
            'speaker.role_duplicate': '重複するスピーカー役割を解決してください。',
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
