"""Solver-neutral prediction execution presentation authority (#478).

The R140 executor already owns resource estimates, bounded admission,
cancellation tokens, progress sinks, attempt telemetry and content-addressed
cache reuse. This module projects that authority into a stable,
solver-neutral execution surface that product UI (Room prediction today,
batch/optimization consumers later) can render without re-interpreting
executor internals:

- a preflight view preserving KNOWN/UNKNOWN/UNAVAILABLE estimate states
  (unknown is never shown as zero);
- an admission decision that distinguishes ADMITTED / DEFER / REJECT with
  the exact reason instead of a generic failure;
- a progress/cancellation view driven by the existing progress sink and
  cancellation token authority;
- cache/attempt visibility that distinguishes exact cache reuse, prior
  failed/cancelled attempts and full rerun requirements;
- long-running job ownership held by the controller, not a transient widget.

The layer intentionally exposes no estimated-time-of-completion: there is no
validated runtime evidence for ETA, so the contract cannot express one.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import FrequencyDomain
from .cad_multifidelity_execution import (
    MultiFidelityAuthorityRef,
    MultiFidelityExecutionCacheEntry,
    MultiFidelityExecutionSchedule,
    MultiFidelityExecutionTask,
)
from .cad_r140_executor import (
    BoundedR140Executor,
    ExecutionAttemptRecord,
    ExecutionResourceEstimate,
    ExecutionRunSummary,
    ResourceAdmissionError,
    ResourceQuantity,
)
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


PREDICTION_EXECUTION_SCHEMA_VERSION = 1
PREDICTION_EXECUTION_AUTHORITY_VERSION = 'prediction-execution-ux-1'

# Per-task live bookkeeping stays bounded across a long campaign: only the
# most recent task ids keep progress/cancellation entries; completed tasks
# are replayable from the runtime repository instead.
EXECUTION_PROGRESS_CACHE_LIMIT = 512

ExecutionAdmissionState = Literal['ADMITTED', 'DEFER', 'REJECT']
ExecutionCacheState = Literal['EXACT_CACHE_HIT', 'NO_EXACT_RESULT']
ExecutionPhase = Literal[
    'QUEUED',
    'RUNNING',
    'CANCELLING',
    'SUCCEEDED',
    'FAILED',
    'CANCELLED',
]
ExecutionCurrency = Literal['CURRENT', 'STALE_SCENE', 'STALE_INPUT']
PriorAttemptState = Literal['NONE', 'FAILED', 'CANCELLED', 'SUCCEEDED']






class PredictionExecutionScope(BaseModel):
    """What the product is about to ask the solver/provider stack to do.

    The scope is a display/admission summary, not a new solver contract: the
    exact task/schedule identity remains the R140 task authority.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    operation_label: str = Field(min_length=1)
    provider_label: str = Field(min_length=1)
    requested_observables: tuple[str, ...] = Field(min_length=1)
    requested_frequency_domain: FrequencyDomain | None = None
    fidelity_label: str | None = None
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_scope(self) -> 'PredictionExecutionScope':
        observables = list(self.requested_observables)
        if len(observables) != len(set(observables)):
            raise ValueError('execution scope observables must be unique')
        return self


class ResourceQuantityView(BaseModel):
    """One resource quantity exactly as the estimate declared it.

    ``state`` preserves KNOWN/UNKNOWN/UNAVAILABLE; an unknown estimate is a
    first-class display state and is never rendered as a zero.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    label: str = Field(min_length=1)
    state: Literal['KNOWN', 'UNKNOWN', 'UNAVAILABLE']
    value: int | None = None
    unit: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_state(self) -> 'ResourceQuantityView':
        if self.state == 'KNOWN' and self.value is None:
            raise ValueError('KNOWN resource view requires a value')
        if self.state != 'KNOWN' and self.value is not None:
            raise ValueError('UNKNOWN/UNAVAILABLE resource view cannot carry a value')
        return self

    @classmethod
    def from_quantity(
        cls,
        label: str,
        quantity: ResourceQuantity,
    ) -> 'ResourceQuantityView':
        return cls(
            label=label,
            state=quantity.state,
            value=quantity.value,
            unit=quantity.unit,
        )


class PredictionExecutionPreflight(BaseModel):
    """Immutable preflight card for one exact prediction execution."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PREDICTION_EXECUTION_SCHEMA_VERSION
    authority_version: Literal[
        'prediction-execution-ux-1'
    ] = PREDICTION_EXECUTION_AUTHORITY_VERSION
    preflight_id: str = Field(pattern=r'^prediction-preflight:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    scope: PredictionExecutionScope
    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    task_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    resource_estimate_ref: MultiFidelityAuthorityRef
    resources: tuple[ResourceQuantityView, ...] = Field(min_length=1)
    admission_state: ExecutionAdmissionState
    admission_reasons: tuple[str, ...]
    cache_state: ExecutionCacheState
    prior_attempt_state: PriorAttemptState
    currency: ExecutionCurrency = 'CURRENT'
    requires_explicit_confirmation: bool
    blockers: tuple[str, ...]

    @model_validator(mode='after')
    def validate_preflight(self) -> 'PredictionExecutionPreflight':
        if len(self.admission_reasons) != len(set(self.admission_reasons)):
            raise ValueError('preflight admission reasons must be unique')
        if len(self.blockers) != len(set(self.blockers)):
            raise ValueError('preflight blockers must be unique')
        if self.admission_state == 'ADMITTED' and self.blockers:
            raise ValueError('admitted preflight cannot carry blockers')
        if self.admission_state != 'ADMITTED' and not self.blockers:
            raise ValueError('deferred/rejected preflight requires blockers')
        if self.admission_state == 'ADMITTED' and self.admission_reasons:
            raise ValueError('admitted preflight cannot carry admission reasons')
        if self.admission_state != 'ADMITTED' and not self.admission_reasons:
            raise ValueError('deferred/rejected preflight requires admission reasons')
        if self.currency != 'CURRENT' and self.admission_state == 'ADMITTED':
            raise ValueError('stale preflight cannot be admitted')
        labels = [item.label for item in self.resources]
        if len(labels) != len(set(labels)):
            raise ValueError('preflight resource labels must be unique')
        expected = _digest(self.semantic_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('prediction preflight semantic hash mismatch')
        if self.preflight_id != f'prediction-preflight:{expected}':
            raise ValueError('prediction preflight id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'preflight_id', 'semantic_sha256'},
        )


def _quantity_views(estimate: ExecutionResourceEstimate) -> tuple[ResourceQuantityView, ...]:
    return (
        ResourceQuantityView.from_quantity('logical_cpu_demand', estimate.logical_cpu_demand),
        ResourceQuantityView.from_quantity('physical_core_demand', estimate.physical_core_demand),
        ResourceQuantityView.from_quantity('inner_solver_threads', estimate.inner_solver_threads),
        ResourceQuantityView.from_quantity('peak_memory_bytes', estimate.peak_memory_bytes),
        ResourceQuantityView.from_quantity('scratch_bytes', estimate.scratch_bytes),
        ResourceQuantityView.from_quantity('gpu_slots', estimate.gpu_slots),
        ResourceQuantityView.from_quantity('vram_bytes', estimate.vram_bytes),
    )


def build_prediction_execution_preflight(
    *,
    task: MultiFidelityExecutionTask,
    estimate: ExecutionResourceEstimate,
    scope: PredictionExecutionScope,
    cache_entry: MultiFidelityExecutionCacheEntry | None,
    prior_attempts: tuple[ExecutionAttemptRecord, ...],
    current_scene_content_hash: str | None,
    confirmable_when_deferred: bool = True,
) -> PredictionExecutionPreflight:
    """Derive the preflight card for one exact task/estimate/cache view.

    ``current_scene_content_hash`` is the hash of the SceneRevision the user
    is looking at right now; when it differs from the scope's pinned hash the
    run is a full rerun, never a silent reuse of the old scene's output.
    """
    if task.resource_estimate_ref != estimate.authority_ref():
        raise ValueError('preflight resource estimate does not match task authority')
    if scope.scene_content_hash != current_scene_content_hash:
        currency: ExecutionCurrency = 'STALE_SCENE'
    elif (
        cache_entry is not None
        and cache_entry.execution_input_sha256 != task.execution_input_sha256
    ):
        currency = 'STALE_INPUT'
    else:
        currency = 'CURRENT'

    blockers: list[str] = []
    reasons: list[str] = []
    try:
        estimate.admission_resource_vector()
    except ResourceAdmissionError as exc:
        admission_state: ExecutionAdmissionState = exc.state
        reasons.append(str(exc))
        blockers.append(
            'DEFER: re-estimation required before admission'
            if exc.state == 'DEFER'
            else 'REJECT: executor cannot admit this task'
        )
    else:
        admission_state = 'ADMITTED'

    if currency != 'CURRENT':
        admission_state = 'REJECT'
        reasons = [f'execution input is {currency.lower()} for the current scene']
        blockers = ['a full rerun is required because scene/config changed']
    elif admission_state == 'ADMITTED':
        reasons = []

    if cache_entry is not None:
        if (
            cache_entry.task_id != task.task_id
            or cache_entry.task_semantic_sha256 != task.semantic_sha256
            or cache_entry.execution_input_sha256 != task.execution_input_sha256
        ):
            raise ValueError('cache entry does not match the exact task identity')
        cache_state: ExecutionCacheState = 'EXACT_CACHE_HIT'
    else:
        cache_state = 'NO_EXACT_RESULT'

    if not prior_attempts:
        prior_state: PriorAttemptState = 'NONE'
    else:
        latest = prior_attempts[-1]
        prior_state = {
            'SUCCEEDED': 'SUCCEEDED',
            'FAILED': 'FAILED',
            'CANCELLED': 'CANCELLED',
        }[latest.state]

    requires_confirmation = admission_state != 'ADMITTED' or not confirmable_when_deferred

    payload = {
        'schema_version': PREDICTION_EXECUTION_SCHEMA_VERSION,
        'authority_version': PREDICTION_EXECUTION_AUTHORITY_VERSION,
        'scope': scope.model_dump(mode='json'),
        'task_id': task.task_id,
        'task_semantic_sha256': task.semantic_sha256,
        'resource_estimate_ref': estimate.authority_ref().model_dump(mode='json'),
        'resources': [item.model_dump(mode='json') for item in _quantity_views(estimate)],
        'admission_state': admission_state,
        'admission_reasons': reasons,
        'cache_state': cache_state,
        'prior_attempt_state': prior_state,
        'currency': currency,
        'requires_explicit_confirmation': requires_confirmation,
        'blockers': blockers,
    }
    digest = _digest(payload)
    return PredictionExecutionPreflight(
        scope=scope,
        task_id=task.task_id,
        task_semantic_sha256=task.semantic_sha256,
        resource_estimate_ref=estimate.authority_ref(),
        resources=_quantity_views(estimate),
        admission_state=admission_state,
        admission_reasons=tuple(reasons),
        cache_state=cache_state,
        prior_attempt_state=prior_state,
        currency=currency,
        requires_explicit_confirmation=requires_confirmation,
        blockers=tuple(blockers),
        preflight_id=f'prediction-preflight:{digest}',
        semantic_sha256=digest,
    )


class PredictionExecutionProgressView(BaseModel):
    """Solver-neutral live progress surface for one running/queued task.

    ``progress_fraction`` is ``None`` whenever the underlying solver reports
    stage-level progress without a meaningful fraction; the UI must render a
    stage/message view in that case rather than a fabricated percentage.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    task_id: str = Field(pattern=r'^r140-execution-task:[0-9a-f]{64}$')
    phase: ExecutionPhase
    progress_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    progress_message: str | None = None
    cancellation_requested: bool
    elapsed_seconds: float | None = Field(default=None, ge=0.0)

    @model_validator(mode='after')
    def validate_view(self) -> 'PredictionExecutionProgressView':
        if self.phase in ('QUEUED', 'RUNNING', 'CANCELLING'):
            pass
        elif self.phase in ('FAILED', 'CANCELLED') and self.progress_fraction == 1.0:
            # a terminal non-success may still report 1.0 progress but must
            # never be presented as a completed prediction.
            pass
        if self.phase == 'CANCELLING' and not self.cancellation_requested:
            raise ValueError('CANCELLING phase requires a cancellation request')
        return self


class PredictionAttemptView(BaseModel):
    """One historical attempt as the product may show it."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    attempt_id: str = Field(min_length=1)
    state: Literal['SUCCEEDED', 'FAILED', 'CANCELLED']
    exit_condition: str = Field(min_length=1)
    failure_reason: str | None = None
    retriable: bool
    result_ref: MultiFidelityAuthorityRef | None = None


def attempt_view(attempt: ExecutionAttemptRecord) -> PredictionAttemptView:
    return PredictionAttemptView(
        attempt_id=attempt.attempt_id,
        state=attempt.state,
        exit_condition=attempt.telemetry.exit_condition,
        failure_reason=attempt.telemetry.failure_reason,
        retriable=attempt.state in ('FAILED', 'CANCELLED'),
        result_ref=(
            attempt.execution_result.result_authority_ref
            if attempt.execution_result is not None
            else None
        ),
    )


class PredictionExecutionController:
    """Owns long-running prediction execution independently of any widget.

    The controller registers the progress sink once and keeps per-task
    progress/cancellation state so a UI can navigate away, return, and still
    observe an accurate execution view. It never invents progress.
    """

    def __init__(self, executor: BoundedR140Executor) -> None:
        self.executor = executor
        self._lock = threading.Lock()
        self._progress: OrderedDict[str, tuple[float | None, str | None]] = (
            OrderedDict()
        )
        self._submitted: OrderedDict[str, float] = OrderedDict()
        self._cancelling: OrderedDict[str, None] = OrderedDict()
        # Explicit lifecycle contract: the controller holds a sink lease
        # for as long as it exists. A successor controller's lease stacks
        # on top — it reports immediately rather than starving behind a
        # stale binding — and releasing unwinds to whatever was effective
        # before.
        self._sink_lease = executor.acquire_progress_sink(self._on_progress)

    def close(self) -> None:
        """Release the progress-sink lease; idempotent."""
        self._sink_lease.release()

    def __enter__(self) -> 'PredictionExecutionController':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _cap(registry: OrderedDict[str, Any]) -> None:
        # Per-task bookkeeping must stay bounded across a long campaign:
        # keep only the most recent task ids; older entries are already
        # terminal and their progress view degrades to the attempt record.
        while len(registry) > EXECUTION_PROGRESS_CACHE_LIMIT:
            registry.popitem(last=False)

    def _on_progress(
        self,
        task_id: str,
        fraction: float,
        message: str | None,
    ) -> None:
        with self._lock:
            self._progress[task_id] = (fraction, message)
            self._progress.move_to_end(task_id)
            self._cap(self._progress)

    def mark_submitted(self, task_id: str) -> None:
        with self._lock:
            self._submitted[task_id] = time.monotonic()
            self._submitted.move_to_end(task_id)
            self._cap(self._submitted)
            self._progress.setdefault(task_id, (None, None))
            self._progress.move_to_end(task_id)
            self._cap(self._progress)

    def request_cancellation(self, task_id: str) -> bool:
        """Request cooperative cancellation; returns True when registered."""
        accepted = self.executor.cancel(task_id)
        if accepted:
            with self._lock:
                self._cancelling[task_id] = None
                self._cancelling.move_to_end(task_id)
                self._cap(self._cancelling)
        return accepted

    def progress_view(
        self,
        task_id: str,
        *,
        latest_attempt: ExecutionAttemptRecord | None = None,
    ) -> PredictionExecutionProgressView:
        with self._lock:
            fraction, message = self._progress.get(task_id, (None, None))
            cancelling = task_id in self._cancelling
            submitted_at = self._submitted.get(task_id)
        if latest_attempt is not None:
            phase: ExecutionPhase = {
                'SUCCEEDED': 'SUCCEEDED',
                'FAILED': 'FAILED',
                'CANCELLED': 'CANCELLED',
            }[latest_attempt.state]
        elif cancelling:
            phase = 'CANCELLING'
        elif fraction is not None:
            phase = 'RUNNING'
        else:
            phase = 'QUEUED'
        elapsed = (
            None if submitted_at is None else time.monotonic() - submitted_at
        )
        return PredictionExecutionProgressView(
            task_id=task_id,
            phase=phase,
            progress_fraction=fraction,
            progress_message=message,
            cancellation_requested=cancelling
            or (latest_attempt.state == 'CANCELLED' if latest_attempt else False),
            elapsed_seconds=elapsed,
        )

    def run_schedule(
        self,
        schedule: MultiFidelityExecutionSchedule,
    ) -> ExecutionRunSummary:
        for ref in schedule.task_refs:
            self.mark_submitted(ref.task_id)
        summary = self.executor.run_schedule(schedule)
        with self._lock:
            for attempt in summary.attempts:
                self._cancelling.pop(attempt.task_id, None)
        return summary

    def attempt_history(
        self,
        task_id: str,
    ) -> tuple[PredictionAttemptView, ...]:
        return tuple(
            attempt_view(attempt)
            for attempt in self.executor.runtime_repository.list_attempts(task_id)
        )


@dataclass(frozen=True)
class PredictionExecutionDecision:
    """Outcome of the explicit admission gate.

    ``task``/``estimate`` are returned for the admitted path only so callers
    can never bypass a blocker.
    """

    admitted: bool
    preflight: PredictionExecutionPreflight
    task: MultiFidelityExecutionTask | None = None
    estimate: ExecutionResourceEstimate | None = None


def confirm_execution(
    preflight: PredictionExecutionPreflight,
    *,
    task: MultiFidelityExecutionTask,
    estimate: ExecutionResourceEstimate,
) -> PredictionExecutionDecision:
    """Explicit admission gate executed after the user sees the preflight.

    A deferred or rejected preflight is never silently admitted; the caller
    receives the decision object and must not dispatch a task when
    ``admitted`` is False.
    """
    if preflight.task_id != task.task_id:
        raise ValueError('confirmation task does not match the preflight task')
    if preflight.resource_estimate_ref != estimate.authority_ref():
        raise ValueError('confirmation estimate does not match the preflight estimate')
    if preflight.admission_state != 'ADMITTED':
        return PredictionExecutionDecision(admitted=False, preflight=preflight)
    return PredictionExecutionDecision(
        admitted=True,
        preflight=preflight,
        task=task,
        estimate=estimate,
    )
