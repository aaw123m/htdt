"""First-run wizard progress derivation (#886).

Pure function over injected facts — the wizard never duplicates authority
models; it reads canonical state (scene, calibration runs, channel
verification plans, sweep runs, commissioning runs) through the facts
bundle the shell assembles, so expert edits automatically re-derive
wizard position on next refresh.

A stage is ``blocked`` only when the prerequisite is structurally absent
(no project, no audio backend) with an explicit reason — never silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


class WizardStage(StrEnum):
    PROJECT_ROOM = 'project_room'
    SYSTEM_DEFINITION = 'system_definition'
    MEASUREMENT_READINESS = 'measurement_readiness'
    BASELINE_COMMISSIONING = 'baseline_commissioning'
    OPTIMIZATION_DEPLOYMENT = 'optimization_deployment'
    COMPLETION = 'completion'


class WizardStageStatus(StrEnum):
    COMPLETE = 'complete'
    CURRENT = 'current'
    PENDING = 'pending'
    BLOCKED = 'blocked'


_STAGE_ORDER: tuple[WizardStage, ...] = tuple(WizardStage)

_STAGE_TITLE_JA: dict[WizardStage, str] = {
    WizardStage.PROJECT_ROOM: 'プロジェクトと部屋',
    WizardStage.SYSTEM_DEFINITION: '機材と配置',
    WizardStage.MEASUREMENT_READINESS: '測定の準備',
    WizardStage.BASELINE_COMMISSIONING: 'ベースライン測定',
    WizardStage.OPTIMIZATION_DEPLOYMENT: '最適化と適用',
    WizardStage.COMPLETION: '完了とレポート',
}


@dataclass(frozen=True, slots=True)
class FirstRunWizardFacts:
    """Canonical-state snapshot the shell assembles for derivation."""

    project_exists: bool = False
    room_saved: bool = False
    speakers_present: bool = False
    speaker_roles_ok: bool = False
    equipment_unresolved_count: int = 0
    audio_backend_available: bool = False
    audio_backend_reason: str | None = None
    calibration_complete: bool = False
    channel_verified: bool = False
    baseline_measured: bool = False
    has_candidates: bool = False
    deploy_applied: bool = False
    deploy_readback_verified: bool = False
    verify_measured: bool = False
    # #891: the packaged Reference Theater fixture verifies against its
    # manifest — the wizard may offer "Open Reference Theater".
    reference_theater_available: bool = False


@dataclass(frozen=True, slots=True)
class WizardStageView:
    stage: WizardStage
    status: WizardStageStatus
    title_ja: str
    detail_ja: str
    reason_ja: str | None
    action_label_ja: str | None
    target: WorkspaceDeepLink | None
    # #891: offer the packaged Reference Theater fixture alongside the
    # stage action (intent wiring only — the shell owns materialization).
    offer_reference_theater: bool = False


_ROOM_GEOMETRY = WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry')
_ROOM_PLACEMENT = WorkspaceDeepLink(WorkspaceId.ROOM, 'placement')
_MEASUREMENT_CALIBRATION = WorkspaceDeepLink(
    WorkspaceId.MEASUREMENT, 'calibration_wizard'
)
_MEASUREMENT_ACQUISITION = WorkspaceDeepLink(
    WorkspaceId.MEASUREMENT, 'acquisition'
)
_MEASUREMENT_CAMPAIGN = WorkspaceDeepLink(
    WorkspaceId.MEASUREMENT, 'campaign'
)
_OPTIMIZATION_INTERVENTIONS = WorkspaceDeepLink(
    WorkspaceId.OPTIMIZATION, 'interventions'
)
_PRESENTATION = WorkspaceDeepLink(WorkspaceId.PRESENTATION, 'default')


def _stage_view(
    stage: WizardStage,
    status: WizardStageStatus,
    detail: str,
    *,
    reason: str | None = None,
    action_label: str | None = None,
    target: WorkspaceDeepLink | None = None,
    offer_reference_theater: bool = False,
) -> WizardStageView:
    return WizardStageView(
        stage=stage,
        status=status,
        title_ja=_STAGE_TITLE_JA[stage],
        detail_ja=detail,
        reason_ja=reason,
        action_label_ja=action_label,
        target=target,
        offer_reference_theater=offer_reference_theater,
    )


def derive_wizard_progress(
    facts: FirstRunWizardFacts,
) -> tuple[WizardStageView, ...]:
    """Derive per-stage status from canonical facts.

    The first non-complete stage becomes ``current``; stages after it are
    ``pending`` unless a stage's own prerequisite is structurally absent,
    which makes it ``blocked`` with an explicit operator-facing reason.
    """
    views: list[WizardStageView] = []
    current_claimed = False

    def _status(done: bool, blocked_reason: str | None = None):
        nonlocal current_claimed
        if done:
            return WizardStageStatus.COMPLETE
        if blocked_reason is not None:
            # A blocked stage is the resume target: downstream stages
            # stay pending so exactly one stage holds attention.
            current_claimed = True
            return WizardStageStatus.BLOCKED
        if not current_claimed:
            current_claimed = True
            return WizardStageStatus.CURRENT
        return WizardStageStatus.PENDING

    # Stage 1 — project + room
    project_room_done = facts.project_exists and facts.room_saved
    views.append(
        _stage_view(
            WizardStage.PROJECT_ROOM,
            _status(project_room_done),
            'プロジェクトを作成し、部屋を保存します。',
            reason=(
                None
                if project_room_done
                else '部屋がまだ保存されていません'
                if facts.project_exists
                else 'まだプロジェクトがありません'
            ),
            action_label=(
                None if project_room_done else 'プロジェクト/部屋を作成'
            ),
            target=None if project_room_done else _ROOM_GEOMETRY,
            # The Reference Theater fixture opens as a real project —
            # offering it completes this stage by itself.
            offer_reference_theater=(
                not project_room_done
                and facts.reference_theater_available),
        )
    )

    # Stage 2 — system definition
    system_done = (
        facts.speakers_present
        and facts.speaker_roles_ok
        and facts.equipment_unresolved_count == 0
    )
    if system_done:
        reason = None
    elif not facts.project_exists:
        reason = '先にプロジェクトを作成してください'
    elif not facts.speakers_present:
        reason = 'スピーカー・機材が未登録です'
    elif not facts.speaker_roles_ok:
        reason = 'スピーカーのチャンネル役割が未確定です'
    else:
        reason = f'未解決の機材が {facts.equipment_unresolved_count} 件あります'
    views.append(
        _stage_view(
            WizardStage.SYSTEM_DEFINITION,
            _status(system_done),
            'スピーカー配置・座席・機材バインドを確定します。',
            reason=reason,
            action_label=None if system_done else '機材・配置を設定',
            target=None if system_done else _ROOM_PLACEMENT,
        )
    )

    # Stage 3 — measurement readiness
    readiness_done = (
        facts.audio_backend_available
        and facts.calibration_complete
        and facts.channel_verified
    )
    missing: list[str] = []
    if not facts.audio_backend_available:
        missing.append(
            f'オーディオバックエンド利用不可'
            + (f' ({facts.audio_backend_reason})'
               if facts.audio_backend_reason else '')
        )
    if not facts.calibration_complete:
        missing.append('測定チェーンの校正が未完了')
    if not facts.channel_verified:
        missing.append('チャンネル検証が未完了')
    views.append(
        _stage_view(
            WizardStage.MEASUREMENT_READINESS,
            _status(
                readiness_done,
                blocked_reason=(
                    f'オーディオバックエンド利用不可'
                    f' ({facts.audio_backend_reason or "理由不明"})'
                    if not facts.audio_backend_available
                    else None
                ),
            ),
            'オーディオ I/O・マイク・校正・チャンネル検証を揃えます。',
            reason='・'.join(missing) if missing else None,
            action_label=(
                None if readiness_done else '測定の準備を進める'
            ),
            target=None if readiness_done else _MEASUREMENT_CALIBRATION,
        )
    )

    # Stage 4 — baseline commissioning
    views.append(
        _stage_view(
            WizardStage.BASELINE_COMMISSIONING,
            _status(facts.baseline_measured),
            '規定キャンペーンでベースラインを測定します。',
            reason=(
                None if facts.baseline_measured
                else 'ベースライン測定が未完了です'
            ),
            action_label=(
                None if facts.baseline_measured else 'ベースライン測定へ'
            ),
            target=(
                None if facts.baseline_measured else _MEASUREMENT_ACQUISITION
            ),
        )
    )

    # Stage 5 — optimization / deployment
    deploy_done = facts.deploy_applied and (
        facts.deploy_readback_verified or not facts.has_candidates
    )
    views.append(
        _stage_view(
            WizardStage.OPTIMIZATION_DEPLOYMENT,
            _status(deploy_done),
            '最適化候補を比較し、承認のうえ適用・検証します。',
            reason=(
                None
                if deploy_done
                else '適用はまだ行われていません'
                if facts.has_candidates
                else '最適化候補がまだありません'
            ),
            action_label=(
                None if deploy_done else '最適化・適用を確認'
            ),
            target=None if deploy_done else _OPTIMIZATION_INTERVENTIONS,
        )
    )

    # Stage 6 — completion
    views.append(
        _stage_view(
            WizardStage.COMPLETION,
            _status(facts.verify_measured),
            '再測定で確認し、レポートを出力します。',
            reason=(
                None if facts.verify_measured
                else '再測定による確認がまだです'
            ),
            action_label=(
                None if facts.verify_measured else 'レポート/出力へ'
            ),
            target=None if facts.verify_measured else _PRESENTATION,
        )
    )

    return tuple(views)


def first_incomplete_stage(
    views: tuple[WizardStageView, ...],
) -> WizardStage | None:
    """The stage the wizard resumes at — current beats blocked, else the
    first blocked, else None when everything is complete."""
    for view in views:
        if view.status is WizardStageStatus.CURRENT:
            return view.stage
    for view in views:
        if view.status is WizardStageStatus.BLOCKED:
            return view.stage
    return None


__all__ = [
    'FirstRunWizardFacts',
    'WizardStage',
    'WizardStageStatus',
    'WizardStageView',
    'derive_wizard_progress',
    'first_incomplete_stage',
]
