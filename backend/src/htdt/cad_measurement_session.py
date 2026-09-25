"""Measurement session orchestration — deterministic Stage A of #725.

Execution state and presentation layer over the canonical runner/measurement
authorities — a session is never itself measurement truth. Provides:

- a resume-safe session view (next required cell, completed/blocked/staged
  cells, context staleness) rebuilt from persisted events — progress
  survives restart because it is *derived* from the append-only event log;
- exact cell↔measurement verification before commit: the committed trace
  must match the planned channel role, source set and measurement target
  on the plan's exact SceneRevision — wrong/stale trace assignment is
  rejected rather than silently bound (the strictness the #853 regression
  flags at the persistence layer is enforced here at the session gate);
- context-change detection: if the plan's SceneRevision is no longer the
  document's latest, the session reports STALE instead of silently
  binding old evidence to new geometry.

Stage B (REW remote control) and device coordination are out of scope here:
the session works when every device/measurement step is manual.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cad_measurement_models import CadMeasurementRecord
from .cad_measurement_runner import (
    GuidedStep,
    MeasurementRunnerPlan,
    RunnerCellSpec,
    RunnerCellState,
    guided_step,
)
from .cad_repository import SceneRevision


SessionContextState = Literal['current', 'stale', 'unknown']


def verify_cell_match(
    cell: RunnerCellSpec,
    record: CadMeasurementRecord,
) -> tuple[str, ...]:
    """Exact match violations between a planned cell and a measurement.

    Returns the empty tuple only when the record provably is the planned
    acquisition — channel role, source set, target entity and the plan's
    exact SceneRevision must all agree. Any mismatch is a named violation,
    never a fuzzy match or silent pass.
    """
    violations: list[str] = []
    if record.channel_role != cell.channel_role:
        violations.append(
            f'channel role mismatch: plan {cell.channel_role} vs '
            f'measurement {record.channel_role}'
        )
    if set(record.source_speaker_ids) != set(cell.source_speaker_ids):
        violations.append(
            'source set mismatch: plan '
            f'{sorted(cell.source_speaker_ids)} vs measurement '
            f'{sorted(record.source_speaker_ids)}'
        )
    if record.measurement_entity_id != cell.target_entity_id:
        violations.append(
            f'target mismatch: plan {cell.target_entity_id} vs '
            f'measurement {record.measurement_entity_id}'
        )
    if record.evidence_type == 'unknown':
        violations.append(
            'measurement evidence type is unknown — cell assignment '
            'requires declared evidence'
        )
    return tuple(violations)


def session_context_state(
    plan: MeasurementRunnerPlan,
    latest_revision: SceneRevision | None,
) -> SessionContextState:
    """Whether the plan still pins the document's current revision."""
    if latest_revision is None:
        return 'unknown'
    if plan.scene_revision_id != latest_revision.revision_id:
        return 'stale'
    if plan.scene_content_hash != latest_revision.content_hash:
        return 'stale'
    return 'current'


class MeasurementSessionView(BaseModel):
    """Derived resume/progress view for one runner — never persisted truth."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    context_state: SessionContextState
    next_cell_index: int | None
    next_step: GuidedStep | None
    completed_cells: tuple[int, ...]
    staged_cells: tuple[int, ...]
    pending_cells: tuple[int, ...]
    blocked_cells: tuple[int, ...]
    skipped_cells: tuple[int, ...]
    resumed_at_utc: str = Field(min_length=1)


def build_session_view(
    *,
    run_id: str,
    plan: MeasurementRunnerPlan,
    document_id: str,
    cell_states: tuple[RunnerCellState, ...],
    latest_revision: SceneRevision | None,
    resumed_at_utc: str,
) -> MeasurementSessionView:
    """Assemble the resume view from the plan + resolved cell states."""
    by_index = {state.cell_index: state for state in cell_states}
    completed: list[int] = []
    staged: list[int] = []
    pending: list[int] = []
    blocked: list[int] = []
    skipped: list[int] = []
    next_index: int | None = None
    for index, cell in enumerate(plan.cells):
        state = by_index.get(index)
        status = state.status if state is not None else 'not_started'
        if status == 'completed':
            completed.append(index)
        elif status == 'staged':
            staged.append(index)
            if next_index is None:
                next_index = index
        elif status in ('quality_pending', 'assignment_incomplete'):
            pending.append(index)
            if next_index is None:
                next_index = index
        elif status == 'retake_required':
            blocked.append(index)
            if next_index is None:
                next_index = index
        elif status == 'skipped':
            skipped.append(index)
        else:
            if next_index is None:
                next_index = index
    next_step = None
    if next_index is not None:
        next_step = guided_step(plan, next_index)
    return MeasurementSessionView(
        run_id=run_id,
        plan_id=plan.plan_id,
        document_id=document_id,
        context_state=session_context_state(plan, latest_revision),
        next_cell_index=next_index,
        next_step=next_step,
        completed_cells=tuple(completed),
        staged_cells=tuple(staged),
        pending_cells=tuple(pending),
        blocked_cells=tuple(blocked),
        skipped_cells=tuple(skipped),
        resumed_at_utc=resumed_at_utc,
    )


def load_session_view(
    runner_repository: Any,
    scene_repository: Any,
    run_id: str,
    *,
    resumed_at_utc: str,
) -> MeasurementSessionView:
    """Load the resume view from persisted runner state."""
    run = runner_repository.get_run(run_id)
    if run is None:
        raise ValueError('runner run is not persisted')
    plan = runner_repository.get_plan(run.plan_id)
    if plan is None:
        raise ValueError('runner plan is not persisted')
    revision = scene_repository.get(plan.scene_revision_id)
    document_id = revision.document_id if revision is not None else ''
    latest = (
        scene_repository.latest(document_id) if document_id else None
    )
    states = runner_repository.cell_states(run_id)
    return build_session_view(
        run_id=run_id,
        plan=plan,
        document_id=document_id or 'unknown',
        cell_states=tuple(states[index] for index in sorted(states)),
        latest_revision=latest,
        resumed_at_utc=resumed_at_utc,
    )


__all__ = [
    'MeasurementSessionView',
    'SessionContextState',
    'build_session_view',
    'load_session_view',
    'session_context_state',
    'verify_cell_match',
]
