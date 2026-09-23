"""Measurement campaign runner authority (#529).

Executes an exact channel/source × target/seat × repeat plan as a guided
checklist inside Measurements. The runner never acquires data itself —
REW remains the acquisition engine — it tracks per-cell state, commits
imported measurements to exact planned cells, preserves retake lineage,
and survives restarts through append-only persistence. A blocked or
retake-required cell is never silently skipped, and a committed REW trace
is never equated with completed planned evidence.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerCellStatus = Literal[
    'not_started',
    'staged',
    'assignment_incomplete',
    'quality_pending',
    'retake_required',
    'completed',
    'skipped',
]
RunnerPurpose = Literal['measurement', 'calibration', 'holdout', 'diagnostic']
RUNNER_SCHEMA_VERSION = 'measurement-runner-1'


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(payload: Any) -> str:
    return sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


class RunnerCellSpec(BaseModel):
    """One planned acquisition cell: exact source × target × repeat."""

    model_config = ConfigDict(frozen=True)

    cell_index: int = Field(ge=0)
    channel_role: str = Field(min_length=1)
    source_speaker_ids: tuple[str, ...] = Field(min_length=1)
    target_entity_id: str = Field(min_length=1)
    repeat_index: int = Field(ge=0)
    purpose: RunnerPurpose = 'measurement'
    allow_skip: bool = False
    notes: str = ''


class MeasurementRunnerPlan(BaseModel):
    """Immutable runner plan bound to one scene revision.

    The plan enumerates every required acquisition up front; committing a
    measurement later never mutates the plan.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    cells: tuple[RunnerCellSpec, ...] = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_plan(self) -> 'MeasurementRunnerPlan':
        indices = tuple(c.cell_index for c in self.cells)
        if indices != tuple(range(len(self.cells))):
            raise ValueError('runner cells must be indexed 0..N-1 in order')
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('runner plan hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': RUNNER_SCHEMA_VERSION,
            'plan_id': self.plan_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'cells': [c.model_dump(mode='json') for c in self.cells],
        }

    def cell(self, cell_index: int) -> RunnerCellSpec:
        return self.cells[cell_index]


class RunnerCellEvent(BaseModel):
    """Append-only transition of one planned cell."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    cell_index: int = Field(ge=0)
    status: RunnerCellStatus
    measurement_id: str | None = None
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    supersedes_measurement_id: str | None = None
    reason: str = ''
    created_at: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_event(self) -> 'RunnerCellEvent':
        if self.status == 'completed' and self.measurement_id is None:
            raise ValueError('completed cells must bind a measurement')
        if self.status == 'retake_required' and self.measurement_id is None:
            raise ValueError('retake markers must name the failed measurement')
        return self


class MeasurementRunnerRun(BaseModel):
    """One execution of a runner plan; resumable across restarts."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    started_at: str = Field(min_length=1)


class RunnerCellState(BaseModel):
    """Current resolved state of one cell (latest event)."""

    model_config = ConfigDict(frozen=True)

    cell_index: int = Field(ge=0)
    status: RunnerCellStatus
    measurement_id: str | None = None
    dataset_id: str | None = None
    dataset_sha256: str | None = None
    supersedes_measurement_id: str | None = None
    reason: str = ''
    updated_at: str = ''


class GuidedStep(BaseModel):
    """Descriptor for the current acquisition step shown to the operator."""

    model_config = ConfigDict(frozen=True)

    cell_index: int = Field(ge=0)
    channel_role: str
    source_speaker_ids: tuple[str, ...]
    target_entity_id: str
    repeat_index: int
    purpose: RunnerPurpose
    notes: str = ''
    guidance_entity_ids: tuple[str, ...] = ()


class RunnerProgress(BaseModel):
    """Counts by state — never a misleading generic percentage."""

    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    not_started: int = Field(ge=0)
    staged: int = Field(ge=0)
    assignment_incomplete: int = Field(ge=0)
    quality_pending: int = Field(ge=0)
    retake_required: int = Field(ge=0)
    skipped: int = Field(ge=0)


def build_runner_plan(
    *,
    scene_revision_id: str,
    scene_content_hash: str,
    sources: tuple[tuple[str, tuple[str, ...]], ...],
    target_entity_ids: tuple[str, ...],
    repeat_count: int = 1,
    purposes: tuple[RunnerPurpose, ...] = ('measurement',),
    allow_skip: bool = False,
    plan_id: str | None = None,
) -> MeasurementRunnerPlan:
    """Enumerate the deterministic source × target × purpose × repeat matrix.

    Cell order: purpose, then source, then target, then repeat — matching
    the checklist rows the operator walks through.
    """
    cells: list[RunnerCellSpec] = []
    for purpose in purposes:
        for channel_role, speaker_ids in sources:
            for target_entity_id in target_entity_ids:
                for repeat_index in range(repeat_count):
                    cells.append(
                        RunnerCellSpec(
                            cell_index=len(cells),
                            channel_role=channel_role,
                            source_speaker_ids=tuple(speaker_ids),
                            target_entity_id=target_entity_id,
                            repeat_index=repeat_index,
                            purpose=purpose,
                            allow_skip=allow_skip,
                        )
                    )
    payload: dict[str, Any] = {
        'plan_id': plan_id or str(uuid4()),
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'cells': cells,
    }
    provisional = MeasurementRunnerPlan.model_construct(
        **payload, plan_sha256='0' * 64
    )
    return MeasurementRunnerPlan(
        **payload, plan_sha256=_hash(provisional.identity_payload())
    )


def resolve_cell_states(
    plan: MeasurementRunnerPlan,
    events: tuple[RunnerCellEvent, ...],
) -> dict[int, RunnerCellState]:
    """Latest-event-wins resolution; cells without events are not_started."""
    latest: dict[int, RunnerCellEvent] = {}
    for event in events:
        if event.cell_index not in latest or event.created_at >= latest[event.cell_index].created_at:
            latest[event.cell_index] = event
    states: dict[int, RunnerCellState] = {}
    for cell in plan.cells:
        event = latest.get(cell.cell_index)
        if event is None:
            states[cell.cell_index] = RunnerCellState(
                cell_index=cell.cell_index,
                status='not_started',
                updated_at='',
            )
        else:
            states[cell.cell_index] = RunnerCellState(
                cell_index=cell.cell_index,
                status=event.status,
                measurement_id=event.measurement_id,
                dataset_id=event.dataset_id,
                dataset_sha256=event.dataset_sha256,
                supersedes_measurement_id=event.supersedes_measurement_id,
                reason=event.reason,
                updated_at=event.created_at,
            )
    return states


def next_incomplete_cell(
    plan: MeasurementRunnerPlan,
    states: dict[int, RunnerCellState],
) -> int | None:
    """First cell not yet completed/skipped — blocked and retake cells are
    returned rather than silently skipped; resolving them is the operator's
    explicit choice."""
    for cell in plan.cells:
        if states[cell.cell_index].status not in ('completed', 'skipped'):
            return cell.cell_index
    return None


def guided_step(
    plan: MeasurementRunnerPlan,
    cell_index: int,
    *,
    guidance_entity_ids: tuple[str, ...] = (),
) -> GuidedStep:
    cell = plan.cell(cell_index)
    return GuidedStep(
        cell_index=cell.cell_index,
        channel_role=cell.channel_role,
        source_speaker_ids=cell.source_speaker_ids,
        target_entity_id=cell.target_entity_id,
        repeat_index=cell.repeat_index,
        purpose=cell.purpose,
        notes=cell.notes,
        guidance_entity_ids=tuple(dict.fromkeys((*cell.source_speaker_ids, cell.target_entity_id, *guidance_entity_ids))),
    )


def runner_progress(
    plan: MeasurementRunnerPlan,
    states: dict[int, RunnerCellState],
) -> RunnerProgress:
    counts: dict[str, int] = {s: 0 for s in (
        'not_started', 'staged', 'assignment_incomplete', 'quality_pending',
        'retake_required', 'completed', 'skipped',
    )}
    for cell in plan.cells:
        counts[states[cell.cell_index].status] += 1
    return RunnerProgress(total=len(plan.cells), **counts)
