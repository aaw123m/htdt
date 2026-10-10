"""Numbered optimization-journey guidance for the optimization workspace.

The six page tabs (setup/candidates/comparison/interventions/robustness/
validation) are flat siblings; the first-time journey actually has a canonical
order — save the room, save a search spec, generate+apply a candidate, record
and measure it, preregister the validation conditions, review the comparison
and validation results — that the tab strip never expresses. This module
evaluates persisted optimization state into that numbered list so the
workspace can render a persistent guide strip
(``OptimizationWorkflowWorkspace`` wires each step to its owning page).

Steps are a recommended order, not hard gates: robustness evaluation and
intervention studies are legitimate detours the user can take at any point,
so the spine never blocks on them. The single true gate is the saved room —
every search spec binds an exact ``SceneRevision`` + content hash, so nothing
downstream exists before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


JourneyStepStatus = Literal['done', 'current', 'pending', 'blocked']


@dataclass(frozen=True, slots=True)
class OptimizationJourneyStep:
    """One numbered journey step evaluated against persisted state.

    ``context_id`` names the optimization page that owns the step (passed to
    ``select_section``); ``workspace`` names an external shell destination for
    steps the optimization workspace cannot perform itself (room save).
    ``detail`` is the state-derived one-line status the guide displays.
    """

    key: str
    number: int
    title: str
    status: JourneyStepStatus
    detail: str
    context_id: str | None
    workspace: str | None = None


#: Canonical journey order — the sequence the guide numbers. ``interventions``
#: and ``robustness`` stay off the spine on purpose: both are optional detours
#: the persisted record can never require, so they would only ever sit there
#: permanently pending and steal the next-action spotlight (the same rule the
#: measurement journey applies to manual instrument checks).
_JOURNEY_ORDER: tuple[str, ...] = (
    'scene',
    'spec',
    'apply',
    'evidence',
    'conditions',
    'review',
)


def evaluate_optimization_journey(
    *,
    scene_saved: bool,
    spec_count: int,
    spec_current_count: int,
    applied: bool,
    plan_count: int,
    plans_measured: int,
    campaign_count: int,
    evaluation_count: int,
    pareto_set_count: int,
    validation_count: int,
) -> tuple[OptimizationJourneyStep, ...]:
    """Evaluate the numbered optimization journey against persisted state.

    ``spec_current_count`` counts saved search specs still bound to the
    current head revision and constraint workspace (a stale spec cannot
    generate or apply candidates). ``applied`` is True once persisted state
    proves a candidate application: either a measurement plan exists (a plan
    can only be built over an applied revision) or the current head descends
    from a spec's source revision. ``plans_measured`` counts measurement plans
    whose measurements are bound; ``evaluation_count`` counts materialized
    objective evidence across specs.
    """
    states: dict[str, JourneyStepStatus] = {}
    details: dict[str, str] = {}
    contexts: dict[str, str | None] = {}
    workspaces: dict[str, str | None] = {}

    states['scene'] = 'done' if scene_saved else 'pending'
    details['scene'] = (
        '部屋は保存済みです'
        if scene_saved
        else '最適化の前に部屋を一度保存してください（「部屋」タブ）'
    )
    contexts['scene'] = None
    workspaces['scene'] = 'room'

    states['spec'] = (
        'done' if spec_current_count > 0 else 'pending'
    )
    if spec_current_count > 0:
        stale = spec_count - spec_current_count
        details['spec'] = (
            f'探索設定 {spec_current_count} 件利用可'
            + (f'（他 {stale} 件は以前の部屋・制約向け）' if stale else '')
        )
    elif spec_count > 0:
        details['spec'] = (
            f'{spec_count} 件は以前の部屋・制約向けです — '
            '「同じ条件で再探索」か新しい探索設定で現在の部屋へ再作成してください'
        )
    else:
        details['spec'] = (
            '可動物体・探索軸・刻みを定めて探索設定を保存してください'
        )
    contexts['spec'] = 'setup'

    states['apply'] = 'done' if applied else 'pending'
    details['apply'] = (
        '候補を適用した部屋が保存されています'
        if applied
        else '「候補」ページで生成し、3Dで確認して「適用」→ 部屋を保存します'
    )
    contexts['apply'] = 'candidates'

    # A recorded candidate stays open until its measurements are bound —
    # the plan only completes through the measurement workspace's runner or
    # imported evidence, never by assumption.
    states['evidence'] = (
        'done' if plans_measured > 0 else 'pending'
    )
    if plans_measured > 0:
        unmeasured = plan_count - plans_measured
        details['evidence'] = (
            f'計測済みの実測候補 {plans_measured} 件'
            + (f' · 未計測 {unmeasured} 件' if unmeasured else '')
        )
    elif plan_count > 0:
        details['evidence'] = (
            f'実測候補 {plan_count} 件 — 測定ワークスペースで計測を進めてください'
        )
    else:
        details['evidence'] = (
            '適用・保存した候補を「現在の保存版を実測候補として記録」で登録し、'
            '計測で実データを結び付けます'
        )
    contexts['evidence'] = 'validation'

    states['conditions'] = 'done' if campaign_count > 0 else 'pending'
    if campaign_count > 0 and evaluation_count > 0:
        details['conditions'] = (
            f'検証条件 {campaign_count} 件 · 比較指標 {evaluation_count} 件'
        )
    elif campaign_count > 0:
        details['conditions'] = (
            f'検証条件 {campaign_count} 件 — '
            '「比較指標の根拠データを生成」で比較指標を作成します'
        )
    else:
        details['conditions'] = (
            '測定結果を見る前に、検証用候補・帯域・閾値を固定して保存します'
        )
    contexts['conditions'] = 'validation'

    states['review'] = (
        'done' if validation_count > 0 or pareto_set_count > 0 else 'pending'
    )
    if validation_count > 0 or pareto_set_count > 0:
        details['review'] = (
            f'保存済み検証 {validation_count} 件 · Pareto {pareto_set_count} 件'
        )
    elif evaluation_count > 0:
        details['review'] = (
            f'比較指標 {evaluation_count} 件 — '
            '「比較」でParetoを更新するか「検証結果を構築・保存」で確定できます'
        )
    else:
        details['review'] = (
            '「比較」のPareto・「ばらつき耐性」の評価・保存済み検証で候補を'
            '確かめます（介入スタディは「介入プランナー」で随時作成できます）'
        )
    contexts['review'] = 'comparison'

    if not scene_saved:
        # Nothing downstream is reachable before the first head revision —
        # every spec binds an exact revision + content hash.
        for key in _JOURNEY_ORDER[1:]:
            states[key] = 'blocked'
            details[key] = '部屋の保存後に利用できます'
        states['scene'] = 'current'
    else:
        for key in _JOURNEY_ORDER:
            if states[key] != 'done':
                states[key] = 'current'
                break

    titles = {
        'scene': '部屋を保存',
        'spec': '探索設定を保存',
        'apply': '候補を生成して適用',
        'evidence': '実測候補を記録・計測',
        'conditions': '検証条件を事前登録',
        'review': '比較と検証を確認',
    }
    return tuple(
        OptimizationJourneyStep(
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
    steps: tuple[OptimizationJourneyStep, ...],
) -> OptimizationJourneyStep | None:
    """The step the guide should spotlight (first non-done, else None)."""
    for step in steps:
        if step.status in ('current', 'pending', 'blocked'):
            return step
    return None


__all__ = [
    'JourneyStepStatus',
    'OptimizationJourneyStep',
    'current_journey_step',
    'evaluate_optimization_journey',
]
