"""Sealed re-measurement queue authority (#968).

A re-measurement queue derives *only* from replay-validated
:class:`CadMeasurementQualityReport` rows — the sealed quality authority.
Each persisted report either recommends ``RETAKE`` (declared thresholds
failed), ``NOT_NEEDED``, or stays ``UNKNOWN`` (evidence missing).

Classification rules (mirrors the issue's honesty contract):

* ``RETAKE`` → queued for a physical re-capture, with the failed check
  names and authority reasons carried verbatim — the queue never
  paraphrases or fabricates criteria.
* ``UNKNOWN`` → the missing evidence decides: evidence completable from
  stored authorities (file re-read / metadata declaration, e.g. an IR
  import, a calibration file, an acquisition context, sibling repeats)
  routes to a *soft re-evaluation* candidate, never a re-measurement.
  Evidence that only another capture can supply (a repeat measurement
  with no eligible siblings) stays queued.
* Everything else is skipped with a stated reason — superseded
  measurements, disposition-excluded evidence, targets no longer in the
  current scene, unreadable/missing reports. Unknown quality is never
  silently passed and never queued without a reason.

The queue is sealed and *deterministic*: the same evaluation set, scene
head and classification rules reproduce the same ``queue_sha256`` /
``queue_id``, so re-generation is idempotent. ``evaluation_set_sha256``
pins the exact (measurement, report) set the queue was built from; when
the underlying evaluations drift the queue lapses honestly — execution
stays blocked until a fresh queue is generated against the current set.

Dismissals and campaign conversions are append-only sealed events bound
to the queue instance; a dismissed item re-derives only when the
evaluation set itself changes (a new queue), keeping history honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_scene import Direction3, Position3
from ...canonical_json import canonical_sha256 as _hash
from ...cad_delegated_provider import _require_iso8601, _seal
from .cad_measurement_quality import (
    CadMicrophoneCapture,
    CadMeasurementQualityReport,
    CadPlaybackCapture,
    measurement_retake_guidance,
)

REMEASURE_QUEUE_AUTHORITY_VERSION = 'remeasure-queue-1'

RemeasureQueueStatus = Literal['current', 'lapsed_evaluations', 'lapsed_scene']
"""Freshness verdict of a persisted queue against current authorities."""

RemeasureItemState = Literal['pending', 'dismissed', 'converted']
"""Resolved per-item state from the append-only queue event stream."""

RemeasureCandidateKind = Literal['remeasure', 'soft_reevaluation', 'skipped']

RemeasureSkipReason = Literal[
    'superseded',
    'not_normally_eligible',
    'target_not_in_current_scene',
    'no_current_report',
    'report_unreadable',
    'undetermined',
]

#: Missing-evidence codes (from ``measurement_retake_guidance``) that a
#: stored authority can complete without another acoustic capture:
#: dataset/IR file re-reads, observation/acquisition-context/calibration
#: declarations and repeat bindings all produce a new report epoch —
#: none of them requires the microphone to move.
SOFT_REEVALUABLE_EVIDENCE: frozenset[str] = frozenset(
    {
        'clipping_metadata',
        'snr_evidence',
        'usable_band_evidence',
        'timing_reference_evidence',
        'polarity_evidence',
        'impulse_response',
        'ir_window_evidence',
        'calibration_provenance',
        'acquisition_context',
    }
)

#: Missing evidence only a new physical capture supplies. Repeats can
#: additionally be satisfied by already-committed same-binding siblings —
#: the service resolves that at build time, so the code alone is not a
#: verdict.
CAPTURE_REQUIRED_EVIDENCE: frozenset[str] = frozenset(
    {
        'repeat_measurements',
    }
)


class RemeasureRequiredInputs(BaseModel):
    """The exact re-capture inputs resolved from the sealed context.

    Entity/channel/source identity comes from the *effective* binding
    (latest append-only correction overlaid on the immutable record) —
    the same identity the runner validates on commit. Equipment fields
    mirror the acquisition-context authority the report bound; ``None``
    means the evaluation epoch declared nothing and the operator must
    supply it at run time — never a fabricated value.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_entity_id: str = Field(min_length=1)
    measurement_position: Position3
    measurement_direction: Direction3 | None = None
    channel_role: str = Field(min_length=1)
    source_speaker_ids: tuple[str, ...]
    radiation_scope: str = Field(min_length=1)
    routing_evidence: str = Field(min_length=1)
    acquisition_context_id: str | None = Field(default=None, min_length=1)
    microphone: CadMicrophoneCapture | None = None
    playback: CadPlaybackCapture | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    calibration_file_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )


class RemeasureQueueItem(BaseModel):
    """One measurement queued for re-capture, with verbatim criteria."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    failed_checks: tuple[str, ...]
    unknown_checks: tuple[str, ...]
    not_evaluated_checks: tuple[str, ...]
    # ``check: reason`` strings verbatim from the quality authority.
    retake_reasons: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recoverable_evidence: tuple[str, ...]
    remeasure: tuple[str, ...]
    inputs: RemeasureRequiredInputs


class RemeasureSoftCandidate(BaseModel):
    """Poor-quality measurement recoverable without a new capture.

    The operator resolves the named missing evidence by re-reading files
    or completing stored metadata, then re-derives the quality epoch —
    a safe soft re-evaluation, never an unnecessary physical retake.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    unknown_checks: tuple[str, ...]
    not_evaluated_checks: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    recoverable_evidence: tuple[str, ...]
    detail: str = Field(min_length=1)
    inputs: RemeasureRequiredInputs


class RemeasureSkippedCandidate(BaseModel):
    """A candidate measurement the queue cannot honestly action."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    measurement_id: str = Field(min_length=1)
    report_id: str | None = None
    reason_code: RemeasureSkipReason
    detail: str = ''


class RemeasureQueue(BaseModel):
    """Sealed deterministic re-measurement queue (``rqueue-`` prefix).

    ``evaluation_set_sha256`` pins the full (measurement → latest report)
    set the queue was classified from — including measurements that
    passed — so any report epoch change lapses the queue. There is no
    timestamp inside the payload: the queue is a pure function of the
    evaluation set, and the persisted row records creation time itself.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    queue_id: str = Field(min_length=1)
    queue_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluation_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    authority_version: str = REMEASURE_QUEUE_AUTHORITY_VERSION
    items: tuple[RemeasureQueueItem, ...]
    soft_candidates: tuple[RemeasureSoftCandidate, ...]
    skipped: tuple[RemeasureSkippedCandidate, ...]
    passed_count: int = Field(ge=0)

    @model_validator(mode='after')
    def _valid(self) -> 'RemeasureQueue':
        ids = [item.measurement_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError('re-measurement queue items must be unique')
        overlap = set(ids) & {
            c.measurement_id for c in self.soft_candidates
        }
        if overlap:
            raise ValueError(
                'a measurement cannot be queued and soft-reevaluable'
            )
        if self.queue_sha256 != _hash(self.identity_payload()):
            raise ValueError('re-measurement queue hash mismatch')
        if self.queue_id != f'rqueue-{self.queue_sha256[:24]}':
            raise ValueError('re-measurement queue id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'queue_id', 'queue_sha256'}
        )

    def item(self, measurement_id: str) -> RemeasureQueueItem | None:
        return next(
            (i for i in self.items if i.measurement_id == measurement_id),
            None,
        )

    def queue_ref_payload(self) -> dict[str, str]:
        return {'queue_id': self.queue_id, 'queue_sha256': self.queue_sha256}


class RemeasureQueueEvent(BaseModel):
    """Append-only sealed state transition of one queued item.

    ``dismissed`` records the operator's reason verbatim; ``converted``
    pins the runner plan the item became part of. Both are terminal for
    this queue instance — a regenerated queue re-derives membership from
    the new evaluation set instead of mutating history.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    event_id: str = Field(min_length=1)
    event_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    queue_id: str = Field(min_length=1)
    queue_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_id: str = Field(min_length=1)
    kind: Literal['dismissed', 'converted']
    reason: str = ''
    runner_plan_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'RemeasureQueueEvent':
        _require_iso8601(self.created_at_utc, 'queue event created_at_utc')
        if self.kind == 'dismissed' and not self.reason.strip():
            raise ValueError('dismissed items require a recorded reason')
        if self.kind == 'converted' and self.runner_plan_id is None:
            raise ValueError('converted items must pin the runner plan')
        if self.event_sha256 != _hash(self.identity_payload()):
            raise ValueError('re-measurement queue event hash mismatch')
        if self.event_id != f'rqev-{self.event_sha256[:24]}':
            raise ValueError('re-measurement queue event id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'event_id', 'event_sha256'}
        )


@dataclass(frozen=True, slots=True)
class RemeasureEvaluation:
    """One evaluated measurement handed to the queue builder.

    The service fills this from the effective-evidence resolver and the
    replay-validated report overlay; the builder stays pure.
    """

    measurement_id: str
    eligible: bool
    ineligibility_reasons: tuple[str, ...]
    is_selected_head: bool
    target_in_current_scene: bool
    inputs: RemeasureRequiredInputs | None
    report: CadMeasurementQualityReport | None
    report_error: str | None
    has_same_binding_sibling: bool


def evaluation_set_sha256(
    evaluations: tuple[tuple[str, str, str, str], ...],
) -> str:
    """Pin the full (measurement_id, report_id, report_sha256, error) set."""
    return _hash(
        {
            'kind': 'remeasure-evaluation-set',
            'evaluations': [
                {
                    'measurement_id': measurement_id,
                    'report_id': report_id,
                    'report_sha256': report_sha256,
                    'report_error': report_error,
                }
                for (
                    measurement_id,
                    report_id,
                    report_sha256,
                    report_error,
                ) in evaluations
            ],
        }
    )


def build_remeasure_queue(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    evaluations: tuple[RemeasureEvaluation, ...],
) -> RemeasureQueue:
    """Classify every evaluated measurement and seal the queue.

    Deterministic: identical evaluations on the identical scene head
    reproduce the identical sealed queue — re-generation is a no-op
    append, never a new authority row.
    """

    items: list[RemeasureQueueItem] = []
    soft: list[RemeasureSoftCandidate] = []
    skipped: list[RemeasureSkippedCandidate] = []
    eval_rows: list[tuple[str, str, str, str]] = []
    passed = 0

    for evaluation in sorted(evaluations, key=lambda e: e.measurement_id):
        report = evaluation.report
        eval_rows.append(
            (
                evaluation.measurement_id,
                '' if report is None else report.report_id,
                '' if report is None else report.report_sha256,
                # An unreadable report pins the failure text — an error
                # row and a report-less row are different eval sets.
                evaluation.report_error or '',
            )
        )
        if evaluation.report_error is not None:
            skipped.append(
                RemeasureSkippedCandidate(
                    measurement_id=evaluation.measurement_id,
                    reason_code='report_unreadable',
                    detail=evaluation.report_error,
                )
            )
            continue
        if report is None:
            skipped.append(
                RemeasureSkippedCandidate(
                    measurement_id=evaluation.measurement_id,
                    reason_code='no_current_report',
                )
            )
            continue
        report_id = report.report_id
        if report.retake_recommendation == 'NOT_NEEDED':
            passed += 1
            continue
        if not evaluation.is_selected_head:
            skipped.append(
                RemeasureSkippedCandidate(
                    measurement_id=evaluation.measurement_id,
                    report_id=report_id,
                    reason_code='superseded',
                )
            )
            continue
        if not evaluation.eligible:
            skipped.append(
                RemeasureSkippedCandidate(
                    measurement_id=evaluation.measurement_id,
                    report_id=report_id,
                    reason_code='not_normally_eligible',
                    detail=', '.join(evaluation.ineligibility_reasons),
                )
            )
            continue
        if evaluation.inputs is None or not evaluation.target_in_current_scene:
            skipped.append(
                RemeasureSkippedCandidate(
                    measurement_id=evaluation.measurement_id,
                    report_id=report_id,
                    reason_code='target_not_in_current_scene',
                )
            )
            continue
        inputs = evaluation.inputs
        guidance = measurement_retake_guidance(report)
        recoverable = tuple(
            code
            for code in guidance.missing_evidence
            if code in SOFT_REEVALUABLE_EVIDENCE
        )
        if report.retake_recommendation == 'RETAKE':
            items.append(
                RemeasureQueueItem(
                    measurement_id=evaluation.measurement_id,
                    report_id=report_id,
                    report_sha256=report.report_sha256,
                    failed_checks=guidance.failed_checks,
                    unknown_checks=guidance.unknown_checks,
                    not_evaluated_checks=guidance.not_evaluated_checks,
                    retake_reasons=guidance.reasons,
                    missing_evidence=guidance.missing_evidence,
                    recoverable_evidence=recoverable,
                    remeasure=guidance.remeasure,
                    inputs=inputs,
                )
            )
            continue
        if report.retake_recommendation == 'UNKNOWN':
            capture_required = tuple(
                code
                for code in guidance.missing_evidence
                if code in CAPTURE_REQUIRED_EVIDENCE
            )
            if capture_required and not evaluation.has_same_binding_sibling:
                items.append(
                    RemeasureQueueItem(
                        measurement_id=evaluation.measurement_id,
                        report_id=report_id,
                        report_sha256=report.report_sha256,
                        failed_checks=guidance.failed_checks,
                        unknown_checks=guidance.unknown_checks,
                        not_evaluated_checks=guidance.not_evaluated_checks,
                        retake_reasons=guidance.reasons,
                        missing_evidence=guidance.missing_evidence,
                        recoverable_evidence=recoverable,
                        remeasure=guidance.remeasure,
                        inputs=inputs,
                    )
                )
                continue
            if not guidance.missing_evidence:
                skipped.append(
                    RemeasureSkippedCandidate(
                        measurement_id=evaluation.measurement_id,
                        report_id=report_id,
                        reason_code='undetermined',
                        detail='UNKNOWN verdict without named missing evidence',
                    )
                )
                continue
            soft.append(
                RemeasureSoftCandidate(
                    measurement_id=evaluation.measurement_id,
                    report_id=report_id,
                    report_sha256=report.report_sha256,
                    unknown_checks=guidance.unknown_checks,
                    not_evaluated_checks=guidance.not_evaluated_checks,
                    missing_evidence=guidance.missing_evidence,
                    recoverable_evidence=recoverable,
                    detail=(
                        'repeat bindings already exist as committed '
                        'same-binding measurements'
                        if capture_required
                        else 'missing evidence is completable from stored authorities'
                    ),
                    inputs=inputs,
                )
            )
            continue
        # Defensive: an unrecognized recommendation never silently passes
        # or queues — it is recorded as undetermined.
        skipped.append(
            RemeasureSkippedCandidate(
                measurement_id=evaluation.measurement_id,
                report_id=report_id,
                reason_code='undetermined',
                detail=f'unrecognized retake recommendation: '
                f'{report.retake_recommendation!r}',
            )
        )

    return _seal(
        RemeasureQueue,
        {
            'document_id': document_id,
            'scene_revision_id': scene_revision_id,
            'scene_content_hash': scene_content_hash,
            'evaluation_set_sha256': evaluation_set_sha256(
                tuple(eval_rows)
            ),
            'authority_version': REMEASURE_QUEUE_AUTHORITY_VERSION,
            'items': [i.model_dump(mode='python') for i in items],
            'soft_candidates': [s.model_dump(mode='python') for s in soft],
            'skipped': [s.model_dump(mode='python') for s in skipped],
            'passed_count': passed,
        },
        'queue_id',
        'queue_sha256',
        'rqueue',
    )


def build_queue_event(
    *,
    queue: RemeasureQueue,
    measurement_id: str,
    kind: Literal['dismissed', 'converted'],
    created_at_utc: str,
    reason: str = '',
    runner_plan_id: str | None = None,
) -> RemeasureQueueEvent:
    """Seal one terminal state transition of a queued item."""

    return _seal(
        RemeasureQueueEvent,
        {
            'queue_id': queue.queue_id,
            'queue_sha256': queue.queue_sha256,
            'measurement_id': measurement_id,
            'kind': kind,
            'reason': reason,
            'runner_plan_id': runner_plan_id,
            'created_at_utc': created_at_utc,
        },
        'event_id',
        'event_sha256',
        'rqev',
    )


def resolve_item_states(
    queue: RemeasureQueue,
    events: tuple[RemeasureQueueEvent, ...],
) -> dict[str, RemeasureItemState]:
    """Latest event per queued item decides its state (append-only)."""

    states: dict[str, RemeasureItemState] = {
        item.measurement_id: 'pending' for item in queue.items
    }
    for event in events:
        if event.measurement_id in states:
            states[event.measurement_id] = event.kind
    return states


__all__ = [
    'CAPTURE_REQUIRED_EVIDENCE',
    'REMEASURE_QUEUE_AUTHORITY_VERSION',
    'RemeasureEvaluation',
    'RemeasureItemState',
    'RemeasureQueue',
    'RemeasureQueueEvent',
    'RemeasureQueueItem',
    'RemeasureQueueStatus',
    'RemeasureRequiredInputs',
    'RemeasureSkippedCandidate',
    'RemeasureSoftCandidate',
    'SOFT_REEVALUABLE_EVIDENCE',
    'build_queue_event',
    'build_remeasure_queue',
    'evaluation_set_sha256',
    'resolve_item_states',
]
