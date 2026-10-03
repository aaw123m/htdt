"""Numbered measurement-journey guidance for the measurement workspace.

The six context tabs are flat siblings; the first-time journey actually has
a canonical order — save the room, prepare the instrument, build the plan,
run the capture, import+assign the results, review quality — that the tab
strip never expresses. This module evaluates persisted measurement state
into that numbered list so the workspace can render a persistent guide
strip (``MeasurementPageWorkspace`` wires each step to its owning page).

Steps are a recommended order, not hard gates: importing external REW data
without a runner plan is a legitimate path, so downstream steps never block
on plan state. The single true gate is the saved room — no measurement
authority exists before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .measurement_instrument_onboarding import InstrumentStep


JourneyStepStatus = Literal['done', 'current', 'pending', 'blocked']


@dataclass(frozen=True, slots=True)
class MeasurementJourneyStep:
    """One numbered journey step evaluated against persisted state.

    ``context_id`` names the measurement page that owns the step (passed to
    ``set_context``); ``workspace`` names an external shell destination for
    steps the measurement workspace cannot perform itself (room save).
    ``detail`` is the state-derived one-line status the guide displays.
    """

    key: str
    number: int
    title: str
    status: JourneyStepStatus
    detail: str
    context_id: str | None
    workspace: str | None = None


#: Canonical journey order — the sequence the guide numbers, independent of
#: the context-bar tab order (which keeps 'calibration' last for IA reasons).
_JOURNEY_ORDER: tuple[str, ...] = (
    'room',
    'instrument',
    'plan',
    'run',
    'evidence',
    'review',
)


def _instrument_detail(
    steps: tuple[InstrumentStep, ...],
) -> tuple[bool, str]:
    """(blocked?, detail) — 'action' items block; 'manual' never does.

    Manual items are physical/session-side confirmations the stored records
    can never prove, so they cannot stall the journey: the step completes
    while its detail keeps the reminders visible.
    """
    if not steps:
        return True, '取得条件がありません — 測定機器の準備状況を確認してください'
    actions = sum(1 for step in steps if step.status == 'action')
    manuals = sum(1 for step in steps if step.status == 'manual')
    if actions:
        return True, f'要対応 {actions} 項目 · 要確認 {manuals} 項目'
    if manuals:
        return False, f'機器の準備は完了（要確認 {manuals} 項目は現物で確認）'
    return False, '機器の準備は完了しています'


def evaluate_measurement_journey(
    *,
    scene_saved: bool,
    instrument_steps: tuple[InstrumentStep, ...],
    plan_count: int,
    cells_completed: int,
    cells_remaining: int,
    measurement_count: int,
    staged_pending: bool,
) -> tuple[MeasurementJourneyStep, ...]:
    """Evaluate the numbered measurement journey against persisted state.

    ``cells_completed``/``cells_remaining`` count runner cells across each
    plan's latest run — older superseded runs do not define progress.
    ``staged_pending`` is True while an import or batch item still awaits
    assignment, steering the evidence step at the page that unblocks it.
    """
    states: dict[str, JourneyStepStatus] = {}
    details: dict[str, str] = {}
    contexts: dict[str, str | None] = {}
    workspaces: dict[str, str | None] = {}

    states['room'] = 'done' if scene_saved else 'pending'
    details['room'] = (
        '部屋は保存済みです'
        if scene_saved
        else '測定の前に部屋を一度保存してください（「部屋」タブ）'
    )
    contexts['room'] = None
    workspaces['room'] = 'room'

    instrument_blocked, instrument_detail = _instrument_detail(
        instrument_steps
    )
    states['instrument'] = 'pending' if instrument_blocked else 'done'
    details['instrument'] = instrument_detail
    contexts['instrument'] = 'calibration'

    states['plan'] = 'done' if plan_count > 0 else 'pending'
    details['plan'] = (
        f'計画 {plan_count} 件'
        if plan_count > 0
        else '音源×測定位置×リピートを選んで計画を作成してください'
    )
    contexts['plan'] = 'campaign'

    # The run step completes only when the plan is fully captured (or the
    # external-import path bypassed cells entirely); a partially-finished
    # campaign stays current so the guide keeps pointing at it.
    run_done = (cells_completed > 0 or measurement_count > 0) and (
        cells_remaining == 0
    )
    states['run'] = 'done' if run_done else 'pending'
    if cells_remaining:
        details['run'] = (
            f'残り {cells_remaining} セル — 「実行を開始 / 再開」で続きます'
        )
    elif run_done and cells_completed:
        details['run'] = f'完了セル {cells_completed} 件'
    elif run_done:
        details['run'] = '計測済みの測定があります'
    else:
        details['run'] = '計画を開いてセルごとに計測を進めます'
    contexts['run'] = 'campaign'

    # Committed evidence plus a still-staged import is unfinished work —
    # the step stays open until everything staged is assigned or dropped.
    states['evidence'] = (
        'done' if measurement_count > 0 and not staged_pending else 'pending'
    )
    if measurement_count > 0 and staged_pending:
        details['evidence'] = (
            f'測定 {measurement_count} 件 · 未確定の取り込みがあります'
        )
    elif measurement_count > 0:
        details['evidence'] = f'測定 {measurement_count} 件'
    elif staged_pending:
        details['evidence'] = '取り込み済み — 「割り当て」で測定位置と意味付けを確定してください'
    else:
        details['evidence'] = 'REW の結果を読み込み、測定点・役割・音源へ割り当てます'
    contexts['evidence'] = 'assignment' if staged_pending else 'import'

    states['review'] = 'done' if measurement_count > 0 else 'pending'
    details['review'] = (
        f'測定 {measurement_count} 件の品質・比較を確認できます'
        if measurement_count > 0
        else '測定が保存されると品質確認と比較が使えます'
    )
    contexts['review'] = 'quality'

    if not scene_saved:
        # Nothing downstream is reachable before the first head revision —
        # every authority call fails closed on a missing scene.
        for key in _JOURNEY_ORDER[1:]:
            states[key] = 'blocked'
            details[key] = '部屋の保存後に利用できます'
        states['room'] = 'current'
    else:
        for key in _JOURNEY_ORDER:
            if states[key] != 'done':
                states[key] = 'current'
                break

    titles = {
        'room': '部屋を保存',
        'instrument': '機器の準備',
        'plan': '計画を作成',
        'run': '計測を実行',
        'evidence': '取り込みと割り当て',
        'review': '品質の確認と比較',
    }
    return tuple(
        MeasurementJourneyStep(
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
    steps: tuple[MeasurementJourneyStep, ...],
) -> MeasurementJourneyStep | None:
    """The step the guide should spotlight (first non-done, else None)."""
    for step in steps:
        if step.status in ('current', 'pending', 'blocked'):
            return step
    return None


__all__ = [
    'JourneyStepStatus',
    'MeasurementJourneyStep',
    'current_journey_step',
    'evaluate_measurement_journey',
]
