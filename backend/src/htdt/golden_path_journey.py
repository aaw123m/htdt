"""Golden-path journey guidance for the Overview workspace (#804 Gate B).

The per-workspace strips (measurement_journey, optimization_journey) number
the steps *inside* one context. The primary product journey crosses
workspaces — 部屋 → 機材 → 測定 → 予測 → 比較 → 適用 → 再測定 → 出力 — and
nothing answered "where am I in the whole flow" until now. This module is a
pure evaluator over state the Overview already aggregates, so the strip is
always derivable and never a gate: every step is navigation, not a check
that blocks the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .workflow_navigation import WorkspaceDeepLink, WorkspaceId


GoldenPathStatus = Literal['done', 'current', 'pending', 'blocked']


@dataclass(frozen=True, slots=True)
class GoldenPathStep:
    """One numbered golden-path step evaluated against project state."""

    key: str
    number: int
    title: str
    status: GoldenPathStatus
    detail: str
    target: WorkspaceDeepLink | None


_ROOM_GEOMETRY = WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry')
_ROOM_PLACEMENT = WorkspaceDeepLink(WorkspaceId.ROOM, 'placement')
_MEASUREMENT_QUALITY = WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'quality')
_MEASUREMENT_CAMPAIGN = WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'campaign')
_ROOM_ACOUSTICS = WorkspaceDeepLink(WorkspaceId.ROOM, 'acoustics')
_OPTIMIZATION_COMPARISON = WorkspaceDeepLink(
    WorkspaceId.OPTIMIZATION, 'comparison'
)
_OPTIMIZATION_INTERVENTIONS = WorkspaceDeepLink(
    WorkspaceId.OPTIMIZATION, 'interventions'
)
_PRESENTATION = WorkspaceDeepLink(WorkspaceId.PRESENTATION, 'default')

_JOURNEY_ORDER: tuple[str, ...] = (
    'room',
    'equipment',
    'measurement',
    'prediction',
    'comparison',
    'apply',
    'verify',
    'export',
)

_TITLES: dict[str, str] = {
    'room': '部屋',
    'equipment': '機材・配置',
    'measurement': '測定',
    'prediction': '予測',
    'comparison': '候補の比較',
    'apply': '変更の適用',
    'verify': '再測定で確認',
    'export': '成果物の出力',
}

_TARGETS: dict[str, WorkspaceDeepLink | None] = {
    'room': _ROOM_GEOMETRY,
    'equipment': _ROOM_PLACEMENT,
    'measurement': _MEASUREMENT_QUALITY,
    'prediction': _ROOM_ACOUSTICS,
    'comparison': _OPTIMIZATION_COMPARISON,
    'apply': _OPTIMIZATION_INTERVENTIONS,
    'verify': _MEASUREMENT_CAMPAIGN,
    'export': _PRESENTATION,
}

#: Variant stages that mean a change was physically applied (a proposal
#: alone is not an applied change).
_APPLIED_STAGES: frozenset[str] = frozenset(
    {'applied', 'as_built', 'campaign_preregistered', 'measured_unvalidated',
     'measured_validated'}
)


def evaluate_golden_path_journey(
    *,
    scene_saved: bool,
    room_complete: bool,
    speakers_present: bool,
    speaker_roles_ok: bool,
    measurement_count: int,
    has_current_prediction: bool,
    has_candidates: bool,
    variant_stages: frozenset[str],
) -> tuple[GoldenPathStep, ...]:
    """Evaluate the golden path against Overview-aggregated state.

    ``variant_stages`` is the set of OverviewVariantStage values currently
    held by any SystemVariant. ``has_candidates`` means an optimization
    spec/comparison exists at all — proposals, not applied changes.
    """
    states: dict[str, GoldenPathStatus] = {}
    details: dict[str, str] = {}

    states['room'] = 'done' if scene_saved and room_complete else 'pending'
    details['room'] = (
        '部屋は保存済みです'
        if scene_saved and room_complete
        else '部屋を作成・保存してください'
    )

    equipment_done = speakers_present and speaker_roles_ok
    states['equipment'] = 'done' if equipment_done else 'pending'
    if not speakers_present:
        details['equipment'] = 'スピーカー・機材を登録してください'
    elif not speaker_roles_ok:
        details['equipment'] = 'スピーカーの役割（ch）を整理してください'
    else:
        details['equipment'] = '機材・配置は登録済みです'

    states['measurement'] = 'done' if measurement_count > 0 else 'pending'
    details['measurement'] = (
        f'測定 {measurement_count} 件'
        if measurement_count > 0
        else 'REW 等の測定結果を取り込んでください'
    )

    states['prediction'] = 'done' if has_current_prediction else 'pending'
    details['prediction'] = (
        '最新の条件で予測済みです'
        if has_current_prediction
        else '現在の部屋・機材で予測を実行してください'
    )

    states['comparison'] = 'done' if has_candidates else 'pending'
    details['comparison'] = (
        '候補の比較ができます'
        if has_candidates
        else '最適化で配置候補を生成・比較してください'
    )

    applied = bool(variant_stages & _APPLIED_STAGES)
    states['apply'] = 'done' if applied else 'pending'
    details['apply'] = (
        '変更は適用済みです'
        if applied
        else '比較した候補を明示的に適用してください'
    )

    verified = 'measured_validated' in variant_stages
    states['verify'] = 'done' if verified else 'pending'
    details['verify'] = (
        '適用後の状態を実測で確認済みです'
        if verified
        else '適用後に再測定してビフォーアフターを確認してください'
    )

    # Export/share is always available once a scene exists — it never
    # blocks and never shows as a failure, it just stays the last stop.
    states['export'] = 'done' if verified else 'pending'
    details['export'] = '成果物・レポートはいつでも出力できます'

    if not scene_saved:
        # No head revision — every downstream authority call fails closed.
        for key in _JOURNEY_ORDER[1:]:
            states[key] = 'blocked'
            details[key] = '部屋の保存後に利用できます'
        states['room'] = 'current'
        details['room'] = 'まず部屋を作成してください'
    else:
        for key in _JOURNEY_ORDER:
            if states[key] != 'done':
                states[key] = 'current'
                break

    return tuple(
        GoldenPathStep(
            key=key,
            number=index + 1,
            title=_TITLES[key],
            status=states[key],
            detail=details[key],
            target=_TARGETS[key],
        )
        for index, key in enumerate(_JOURNEY_ORDER)
    )


__all__ = [
    'GoldenPathStep',
    'GoldenPathStatus',
    'evaluate_golden_path_journey',
]
