"""Numbered room-building journey guidance for the room workspace.

The five context tabs (geometry/objects/placement/acoustics/history) are
flat siblings; the first-time build actually has a canonical order — save
the room shape, author walls and openings, place speakers and equipment,
place the seats (receivers), assign acoustic surface materials — that the
tab strip never expresses. This module evaluates persisted room state
into that numbered list so the workspace can render a persistent guide
strip (``RoomWorkspace`` wires each step to its owning context).

Steps are a recommended order, not hard gates: wall/opening editing and
every context remain reachable out of order — the strip never disables a
step. The single true gate is a saved room: entities only exist inside a
saved revision and every sidecar record binds the document, so nothing
downstream exists before it. Optional refinements (treatments, listener
poses, prediction runs) ride inside their step's detail line instead of
earning a spine position — a permanently-pending optional step would
steal the next-action spotlight (the same rule the measurement and
optimization journeys apply).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


JourneyStepStatus = Literal['done', 'current', 'pending', 'blocked']


@dataclass(frozen=True, slots=True)
class RoomJourneyStep:
    """One numbered journey step evaluated against persisted state.

    ``context_id`` names the room context that owns the step (passed to
    ``set_context`` / a ``WorkspaceDeepLink`` section); ``workspace`` names
    an external shell destination for steps the room workspace cannot
    perform itself — currently unused, kept for parity with the other
    journey strips. ``detail`` is the state-derived one-line status the
    guide displays.
    """

    key: str
    number: int
    title: str
    status: JourneyStepStatus
    detail: str
    context_id: str | None
    workspace: str | None = None


#: Canonical journey order — the sequence the guide numbers. ``history``
#: stays off the spine on purpose: it is a review/restore surface, not a
#: build stage (the same rule that keeps interventions/robustness off the
#: optimization spine).
_JOURNEY_ORDER: tuple[str, ...] = (
    'shape',
    'openings',
    'speakers',
    'seat',
    'acoustics',
)


def evaluate_room_journey(
    *,
    room_saved: bool,
    vertex_count: int,
    wall_count: int,
    opening_count: int,
    speaker_count: int,
    unassigned_speaker_count: int,
    duplicate_role_count: int,
    equipment_count: int,
    seat_count: int,
    pose_count: int,
    material_count: int,
    treatment_count: int,
    prediction_count: int,
) -> tuple[RoomJourneyStep, ...]:
    """Evaluate the numbered room journey against persisted state.

    Every signal is persisted state (head revision + document sidecars),
    never the working/dirty draft: a step completes only when the saved
    room proves it. ``unassigned_speaker_count`` counts speakers still
    carrying the reserved ``UNASSIGNED-<n>`` placeholder role and
    ``duplicate_role_count`` counts duplicated channel roles — both are
    real room blockers elsewhere in the product (Overview readiness), so
    the speakers step only completes once the roles are honest.
    ``equipment_count`` counts non-speaker, non-seat physical objects
    (screen/display/projector/av equipment).
    """
    states: dict[str, JourneyStepStatus] = {}
    details: dict[str, str] = {}
    contexts: dict[str, str | None] = {}
    workspaces: dict[str, str | None] = {}

    states['shape'] = 'done' if room_saved else 'pending'
    details['shape'] = (
        f'部屋は保存済みです（{vertex_count} 頂点）'
        if room_saved
        else '「部屋を描く」で外形を描き、Ctrl+S で保存してください'
    )
    contexts['shape'] = 'geometry'

    # Walls/openings only persist once the user materializes wall topology
    # (「壁編集を有効化」) — a sealed theater legitimately never does, so the
    # pending detail says the step can be skipped instead of fabricating
    # completion.
    states['openings'] = 'done' if wall_count > 0 else 'pending'
    if wall_count > 0:
        details['openings'] = (
            f'壁 {wall_count} 本 · 開口 {opening_count} 件'
            if opening_count
            else f'壁 {wall_count} 本（開口なし）'
        )
    else:
        details['openings'] = (
            'ドア・窓がある場合は「形状編集」で辺を選び「壁編集を有効化」'
            '→「開口」で追加します（必要なければ次へ）'
        )
    contexts['openings'] = 'geometry'

    speakers_ready = (
        speaker_count > 0
        and unassigned_speaker_count == 0
        and duplicate_role_count == 0
    )
    states['speakers'] = 'done' if speakers_ready else 'pending'
    if speakers_ready:
        details['speakers'] = (
            f'スピーカー {speaker_count} 本'
            + (f' · 機器 {equipment_count} 件' if equipment_count else '')
        )
    elif duplicate_role_count:
        details['speakers'] = (
            '同じ役割が複数のスピーカーに割り当てられています — '
            '「スピーカー・座席」で整理してください'
        )
    elif unassigned_speaker_count:
        details['speakers'] = (
            f'{unassigned_speaker_count} 本の役割が未設定 — '
            '右パネルの「役割」で FL/C/SUB などを設定します'
        )
    else:
        details['speakers'] = (
            '「物体」タブでスピーカーと機器を追加し、役割を設定します'
        )
    contexts['speakers'] = 'objects' if not speaker_count else 'placement'

    states['seat'] = 'done' if seat_count > 0 else 'pending'
    if seat_count > 0:
        details['seat'] = (
            f'座席 {seat_count} 席'
            + (f' · リスナーポーズ {pose_count} 件' if pose_count else '')
        )
    else:
        details['seat'] = (
            '「物体」で座席を追加し、「スピーカー・座席」で位置を調えます'
            '（音響予測の受音点になります）'
        )
    contexts['seat'] = 'placement'

    states['acoustics'] = 'done' if material_count > 0 else 'pending'
    if material_count > 0:
        extra = ''
        if treatment_count:
            extra += f' · 処理 {treatment_count} 件'
        if prediction_count:
            extra += f' · 予測 {prediction_count} 件'
        details['acoustics'] = f'面材質 {material_count} 面{extra}'
    else:
        details['acoustics'] = (
            '「音響」タブで壁・天井・床に材質を割り当てます'
            '（未割当の面は未対応として扱われます）'
        )
    contexts['acoustics'] = 'acoustics'

    if not room_saved:
        # Nothing downstream is reachable before the first saved head —
        # entities live inside revisions and sidecars bind the document.
        for key in _JOURNEY_ORDER[1:]:
            states[key] = 'blocked'
            details[key] = '部屋の保存後に利用できます'
        states['shape'] = 'current'
    else:
        for key in _JOURNEY_ORDER:
            if states[key] != 'done':
                states[key] = 'current'
                break

    titles = {
        'shape': '部屋形状を保存',
        'openings': '壁と開口を設定',
        'speakers': 'スピーカー・機器を配置',
        'seat': '座席（受音点）を配置',
        'acoustics': '壁材・吸音処理を設定',
    }
    return tuple(
        RoomJourneyStep(
            key=key,
            number=index + 1,
            title=titles[key],
            status=states[key],
            detail=details[key],
            context_id=contexts[key],
            workspace=workspaces.get(key),
        )
        for index, key in enumerate(_JOURNEY_ORDER)
    )


def current_journey_step(
    steps: tuple[RoomJourneyStep, ...],
) -> RoomJourneyStep | None:
    """The step the guide should spotlight (first non-done, else None)."""
    for step in steps:
        if step.status in ('current', 'pending', 'blocked'):
            return step
    return None


__all__ = [
    'JourneyStepStatus',
    'RoomJourneyStep',
    'current_journey_step',
    'evaluate_room_journey',
]
