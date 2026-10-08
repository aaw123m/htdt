"""Change-diff driven evidence invalidation (#964, REV72).

Synthesizes the existing authorities into one executable revalidation
queue instead of re-implementing them:

* ``cad_dependency_impact.diff_scene_documents`` resolves the exact
  scene-document delta between two pinned revisions;
* each changed dependency axis maps onto a sealed #729
  ``SemanticChangeEvent`` — one event per mapped semantic change class;
* ``evaluate_staleness`` + ``build_revalidation_plan`` judge sealed
  sha-pinned evidence whenever an edge graph and rule profile are
  declared (fail-closed: no matching rule stays conservative);
* ``build_dependency_impact_report`` judges the registered watched
  artifacts (fail-closed: 'unknown' axes → uncertain → review).

The queue records the diff, the emitted events, the sealed verdicts and
the deterministic work items in append-only tables, and
``run_queue_software`` executes exactly the software-class items in one
operation. Physical items (remeasure → #956 position plan,
re-commission → #946 authorization UI) are routed with prepared
authority refs and always stop for human confirmation — hardware I/O
and production applies never auto-start. Every composition and run pins
the pre/post ``SceneRevision`` content hash; mid-edit drift aborts
instead of overwriting old results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_dependency import (
    DependencyEdgeDeclaration,
    DependencyRuleProfile,
    LineageCompleteness,
    RevalidationPlan,
    SemanticChangeEvent,
    SemanticChangeClass,
    StalenessAssessment,
    build_change_event,
    build_revalidation_plan,
    change_event_binding,
    evaluate_staleness,
    revalidation_binding,
    staleness_binding,
)
from .cad_authority_resolver import AuthorityRef
from .cad_dependency_impact import (
    ArtifactKind,
    DependencyAxis,
    DependencyImpactReport,
    SceneChange,
    WatchedArtifact,
    build_dependency_impact_report,
    diff_scene_documents,
)
from .cad_repository import SceneRevision
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


EVIDENCE_INVALIDATION_VERSION = 'rev72-evidence-invalidation-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


def _require_ref_sha(ref: AuthorityRef | None, label: str) -> None:
    if ref is not None and ref.ref_sha256 is None:
        raise ValueError(f'{label} must pin its sha256')


# ----------------------------------------------------------------------
# Taxonomies
# ----------------------------------------------------------------------

QueueAction = Literal[
    'recompute',
    're_evaluate',
    're_import',
    'remeasure',
    're_commission',
    'review',
]
"""The executable action verbs of #964: exactly the artifact actions of
``cad_dependency_impact`` plus 'review' for the fail-closed uncertain
verdict. 'none' never enters the queue — unaffected artifacts are not
work items."""

QueueExecutionClass = Literal['software', 'physical', 'review']
"""software = safe/read-only compute the verify operation may run in one
step; physical = human-confirmed device/position work (never auto-run);
review = fail-closed human judgment required."""

QueueRoute = Literal[
    'prediction_recompute',
    'evidence_re_evaluate',
    'source_reimport',
    'measurement_position_plan',
    'commissioning_authorization',
    'evidence_review',
]
"""Where the item's next single human (or software) operation lives:
optimization/prediction surfaces, comparison/brief re-derivation, source
re-import, the #956 campaign position plan, the #946 commissioning
authorization surface, or plain evidence review."""

QueueItemState = Literal['stale', 'uncertain']
"""uncertain keeps the fail-closed semantics: unknown impact scope is
always review work, never silently skipped."""

QueueItemStatus = Literal[
    'completed',
    'failed',
    'skipped',
    'unavailable',
    'awaiting_physical',
    'awaiting_review',
]
"""One item's outcome inside a queue run. 'unavailable' marks an honest
gap — no software runner was wired for that route — rather than
pretending the step ran."""

QueueRunVerdict = Literal[
    'already_current',
    'software_complete',
    'awaiting_human',
    'drift_detected',
    'failed',
]


# dependency axis → #729 semantic change class (fail-closed: unknown or
# unrecognized axes map to 'other_declared', never silently dropped)
_AXIS_CHANGE_CLASS: dict[str, SemanticChangeClass] = {
    'geometry': 'geometry_material',
    'material_boundary': 'geometry_material',
    'source_equipment': 'equipment_definition',
    'target_design': 'standard_profile_revision',
    'measurement_context': 'measurement_state',
    'calibration_settings': 'calibration_parameters',
    'solver_provider': 'solver_algorithm',
    'topology_routing': 'device_configuration',
    'operating_state': 'device_configuration',
    'unknown': 'other_declared',
}

# queue action → (execution class, route)
_ACTION_ROUTE: dict[str, tuple[QueueExecutionClass, QueueRoute]] = {
    'recompute': ('software', 'prediction_recompute'),
    're_evaluate': ('software', 'evidence_re_evaluate'),
    're_import': ('software', 'source_reimport'),
    'remeasure': ('physical', 'measurement_position_plan'),
    're_commission': ('physical', 'commissioning_authorization'),
    'review': ('review', 'evidence_review'),
}

# #729 plan action kind → queue action (readback/recommission are
# commissioning-surface work; requalification re-evaluates evidence)
_PLAN_ACTION_MAP: dict[str, QueueAction] = {
    'recompute_prediction': 'recompute',
    'regenerate_derived': 're_evaluate',
    'remeasure_channel': 'remeasure',
    'readback_device': 're_commission',
    'requalify_profile': 're_evaluate',
    'review_evidence': 'review',
    'full_recommission': 're_commission',
}

# deterministic ordering: software first, then review, then physical;
# inside a class the stronger action sorts first.
_CLASS_ORDER: dict[str, int] = {'software': 0, 'review': 1, 'physical': 2}
_ACTION_ORDER: dict[str, int] = {
    're_import': 0,
    're_evaluate': 1,
    'review': 2,
    'recompute': 3,
    'remeasure': 4,
    're_commission': 5,
}


class QueueItemUnavailableError(RuntimeError):
    """The wired software runner has no execution path for this item.

    Raised by the caller-supplied runner; the run records the item
    'unavailable' instead of fabricating a result.
    """


# ----------------------------------------------------------------------
# Sealed models
# ----------------------------------------------------------------------


class ChangeDiffRecord(BaseModel):
    """Sealed record of one exact revision-to-revision change (#964).

    Pins both SceneRevision identities and content hashes, carries the
    resolved scene changes and the semantic change events emitted from
    them, so the queue's evidence basis is itself replayable authority.
    """

    model_config = ConfigDict(frozen=True)

    diff_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    from_revision_id: str = Field(min_length=1)
    from_content_hash: str = Field(pattern=_SHA256_PATTERN)
    to_revision_id: str = Field(min_length=1)
    to_content_hash: str = Field(pattern=_SHA256_PATTERN)
    changes: tuple[SceneChange, ...] = ()
    changed_axes: frozenset[DependencyAxis] = frozenset()
    change_event_refs: tuple[AuthorityRef, ...] = ()
    recorded_at_utc: str = Field(min_length=1)
    invalidation_version: str = Field(
        default=EVIDENCE_INVALIDATION_VERSION, min_length=1
    )
    diff_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'from_revision_id': self.from_revision_id,
            'from_content_hash': self.from_content_hash,
            'to_revision_id': self.to_revision_id,
            'to_content_hash': self.to_content_hash,
            'changes': [
                {
                    **c.model_dump(mode='json'),
                    # frozenset dumps unordered — pin it sorted so the
                    # identity hash survives the JSON round-trip.
                    'axes': sorted(c.axes),
                }
                for c in self.changes
            ],
            'changed_axes': sorted(self.changed_axes),
            'change_event_refs': [
                r.model_dump(mode='json') for r in self.change_event_refs
            ],
            'recorded_at_utc': self.recorded_at_utc,
            'invalidation_version': self.invalidation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'ChangeDiffRecord':
        _require_iso8601(self.recorded_at_utc, 'diff recorded_at_utc')
        for ref in self.change_event_refs:
            _require_ref_sha(ref, 'diff change_event_refs')
        expected = _hash(self.identity_payload())
        if self.diff_sha256 != expected:
            raise ValueError('change diff record sha256 mismatch')
        if self.diff_id != _semantic_id('chdiff', expected):
            raise ValueError('change diff record id mismatch')
        return self


class RevalidationQueueItem(BaseModel):
    """One deterministic work item: subject, verdict, action, route."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_key: str = Field(min_length=1)
    subject_ref: AuthorityRef
    artifact_kind: str = Field(min_length=1)
    display_name: str | None = None
    state: QueueItemState
    action: QueueAction
    execution_class: QueueExecutionClass
    route: QueueRoute
    prepared_refs: tuple[AuthorityRef, ...] = ()
    reason: str = Field(min_length=1)


class RevalidationQueue(BaseModel):
    """Sealed executable revalidation plan for one head revision (#964).

    ``software_sequence`` lists the item keys the single verify operation
    runs, in deterministic order; physical/review items are routed only.
    """

    model_config = ConfigDict(frozen=True)

    queue_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    diff_ref: AuthorityRef
    impact_report_id: str = Field(min_length=1)
    impact_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_refs: tuple[AuthorityRef, ...] = ()
    plan_refs: tuple[AuthorityRef, ...] = ()
    to_revision_id: str = Field(min_length=1)
    to_content_hash: str = Field(pattern=_SHA256_PATTERN)
    items: tuple[RevalidationQueueItem, ...] = ()
    software_sequence: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    invalidation_version: str = Field(
        default=EVIDENCE_INVALIDATION_VERSION, min_length=1
    )
    queue_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'diff_ref': self.diff_ref.model_dump(mode='json'),
            'impact_report_id': self.impact_report_id,
            'impact_report_sha256': self.impact_report_sha256,
            'assessment_refs': [
                r.model_dump(mode='json') for r in self.assessment_refs
            ],
            'plan_refs': [
                r.model_dump(mode='json') for r in self.plan_refs
            ],
            'to_revision_id': self.to_revision_id,
            'to_content_hash': self.to_content_hash,
            'items': [i.model_dump(mode='json') for i in self.items],
            'software_sequence': list(self.software_sequence),
            'created_at_utc': self.created_at_utc,
            'invalidation_version': self.invalidation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'RevalidationQueue':
        _require_iso8601(self.created_at_utc, 'queue created_at_utc')
        _require_ref_sha(self.diff_ref, 'queue diff_ref')
        for ref in (*self.assessment_refs, *self.plan_refs):
            _require_ref_sha(ref, 'queue verdict refs')
        sequence = set(self.software_sequence)
        for item in self.items:
            if (item.execution_class == 'software') != (
                item.item_key in sequence
            ):
                raise ValueError(
                    'software_sequence must list exactly the software items'
                )
        expected = _hash(self.identity_payload())
        if self.queue_sha256 != expected:
            raise ValueError('revalidation queue sha256 mismatch')
        if self.queue_id != _semantic_id('revqueue', expected):
            raise ValueError('revalidation queue id mismatch')
        return self


class QueueItemOutcome(BaseModel):
    """One item's recorded outcome inside a queue run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    item_key: str = Field(min_length=1)
    status: QueueItemStatus
    result_ref: AuthorityRef | None = None
    note: str | None = None


class RevalidationQueueRun(BaseModel):
    """Sealed record of one verify-impacts run (#964).

    Pins the pre/post head revision identity + content hash; a drifted
    post head means the run's software outcomes must never be treated as
    applying to the new scene — the record stays honest evidence.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    queue_ref: AuthorityRef
    pre_head_revision_id: str | None = None
    pre_head_content_hash: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    post_head_revision_id: str | None = None
    post_head_content_hash: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    outcomes: tuple[QueueItemOutcome, ...] = ()
    verdict: QueueRunVerdict
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)
    invalidation_version: str = Field(
        default=EVIDENCE_INVALIDATION_VERSION, min_length=1
    )
    run_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'queue_ref': self.queue_ref.model_dump(mode='json'),
            'pre_head_revision_id': self.pre_head_revision_id,
            'pre_head_content_hash': self.pre_head_content_hash,
            'post_head_revision_id': self.post_head_revision_id,
            'post_head_content_hash': self.post_head_content_hash,
            'outcomes': [o.model_dump(mode='json') for o in self.outcomes],
            'verdict': self.verdict,
            'started_at_utc': self.started_at_utc,
            'finished_at_utc': self.finished_at_utc,
            'invalidation_version': self.invalidation_version,
        }

    @model_validator(mode='after')
    def _check(self) -> 'RevalidationQueueRun':
        _require_iso8601(self.started_at_utc, 'run started_at_utc')
        _require_iso8601(self.finished_at_utc, 'run finished_at_utc')
        _require_ref_sha(self.queue_ref, 'run queue_ref')
        expected = _hash(self.identity_payload())
        if self.run_sha256 != expected:
            raise ValueError('revalidation queue run sha256 mismatch')
        if self.run_id != _semantic_id('revrun', expected):
            raise ValueError('revalidation queue run id mismatch')
        return self


# ----------------------------------------------------------------------
# AuthorityRef bindings
# ----------------------------------------------------------------------


def scene_revision_binding(revision: SceneRevision) -> AuthorityRef:
    return AuthorityRef(
        kind='scene_revision',
        ref_id=revision.revision_id,
        ref_sha256=revision.content_hash,
    )


def change_diff_binding(record: ChangeDiffRecord) -> AuthorityRef:
    return AuthorityRef(
        kind='change_diff_record',
        ref_id=record.diff_id,
        ref_sha256=record.diff_sha256,
    )


def revalidation_queue_binding(queue: RevalidationQueue) -> AuthorityRef:
    return AuthorityRef(
        kind='revalidation_queue',
        ref_id=queue.queue_id,
        ref_sha256=queue.queue_sha256,
    )


def queue_run_binding(run: RevalidationQueueRun) -> AuthorityRef:
    return AuthorityRef(
        kind='revalidation_queue_run',
        ref_id=run.run_id,
        ref_sha256=run.run_sha256,
    )


# ----------------------------------------------------------------------
# Composition
# ----------------------------------------------------------------------


def resolve_change_events(
    *,
    document_id: str,
    from_revision: SceneRevision,
    to_revision: SceneRevision,
    changes: Sequence[SceneChange],
    occurred_at_utc: str,
) -> tuple[SemanticChangeEvent, ...]:
    """Seal one #729 change event per mapped semantic class.

    The scene diff's axes resolve onto the declared change-class
    vocabulary; each event's ``changed_fields`` enumerate the exact
    ``kind:entity`` slots that changed, and unknown axes land on
    'other_declared' rather than being dropped. One event per class is
    the established granularity (REV59-DEPS).
    """
    changed_ref = scene_revision_binding(from_revision)
    successor_ref = scene_revision_binding(to_revision)
    fields_by_class: dict[str, set[str]] = {}
    for change in changes:
        for axis in change.axes:
            cls = _AXIS_CHANGE_CLASS.get(axis, 'other_declared')
            fields_by_class.setdefault(cls, set()).add(
                f'{change.kind}:{change.entity_id or "scene"}'
            )
    return tuple(
        build_change_event(
            document_id=document_id,
            changed_ref=changed_ref,
            successor_ref=successor_ref,
            change_class=cls,  # type: ignore[arg-type]
            changed_fields=sorted(fields),
            occurred_at_utc=occurred_at_utc,
        )
        for cls, fields in sorted(fields_by_class.items())
    )


def _item_key(action: QueueAction, ref: AuthorityRef) -> str:
    return f'{action}:{ref.kind}:{ref.ref_id}'


def _queue_item(
    *,
    subject_ref: AuthorityRef,
    artifact_kind: str,
    display_name: str | None,
    state: QueueItemState,
    action: QueueAction,
    reason: str,
    prepared_refs: Mapping[tuple[str, str], Sequence[AuthorityRef]],
) -> RevalidationQueueItem:
    if state == 'uncertain':
        # fail-closed: unknown impact scope always routes to review.
        action = 'review'
    execution_class, route = _ACTION_ROUTE[action]
    prepared = prepared_refs.get(
        (subject_ref.kind, subject_ref.ref_id)
    ) or prepared_refs.get((subject_ref.kind, '*'), ())
    return RevalidationQueueItem(
        item_key=_item_key(action, subject_ref),
        subject_ref=subject_ref,
        artifact_kind=artifact_kind,
        display_name=display_name,
        state=state,
        action=action,
        execution_class=execution_class,
        route=route,
        prepared_refs=tuple(prepared),
        reason=reason,
    )


def _merge_items(
    *,
    impact_report: DependencyImpactReport,
    plans: Sequence[RevalidationPlan],
    assessments: Sequence[StalenessAssessment],
    prepared_refs: Mapping[tuple[str, str], Sequence[AuthorityRef]],
) -> tuple[RevalidationQueueItem, ...]:
    """Merge impact-report verdicts and sealed plan actions into items.

    Dedupes on the subject ref; the stronger action wins and any
    uncertain verdict forces review (fail-closed).
    """
    merged: dict[tuple[str, str], RevalidationQueueItem] = {}
    uncertain_subjects: set[tuple[str, str]] = set()
    for assessment in assessments:
        for entry in assessment.entries:
            if entry.state in (
                'stale_review_required',
                'unknown_dependency',
                'incompatible',
            ):
                uncertain_subjects.add(
                    (entry.subject_ref.kind, entry.subject_ref.ref_id)
                )

    def _consider(item: RevalidationQueueItem) -> None:
        key = (item.subject_ref.kind, item.subject_ref.ref_id)
        existing = merged.get(key)
        if key in uncertain_subjects and item.state == 'stale':
            item = item.model_copy(update={
                'state': 'uncertain',
                'action': 'review',
                'execution_class': 'review',
                'route': 'evidence_review',
                'item_key': _item_key('review', item.subject_ref),
            })
        if existing is None:
            merged[key] = item
            return
        # dedupe: uncertain beats stale; otherwise the stronger action wins
        if item.state == 'uncertain' and existing.state == 'stale':
            merged[key] = item
        elif item.state == existing.state and _ACTION_ORDER[
            item.action
        ] > _ACTION_ORDER[existing.action]:
            merged[key] = item

    for impact in impact_report.impacts:
        if impact.state == 'unaffected' or (
            impact.state == 'stale' and impact.action == 'none'
        ):
            continue
        state: QueueItemState = (
            'uncertain' if impact.state == 'uncertain' else 'stale'
        )
        action: QueueAction = (
            'review'
            if impact.state == 'uncertain'
            else impact.action  # type: ignore[assignment]
        )
        _consider(
            _queue_item(
                subject_ref=AuthorityRef(
                    kind=impact.artifact_kind,
                    ref_id=impact.artifact_id,
                    ref_sha256=None,
                ),
                artifact_kind=impact.artifact_kind,
                display_name=impact.display_name,
                state=state,
                action=action,
                reason=impact.reason,
                prepared_refs=prepared_refs,
            )
        )

    for plan in plans:
        for plan_action in plan.actions:
            action = _PLAN_ACTION_MAP.get(plan_action.kind, 'review')
            for subject in plan_action.subject_refs:
                state = (
                    'uncertain'
                    if (subject.kind, subject.ref_id) in uncertain_subjects
                    else 'stale'
                )
                _consider(
                    _queue_item(
                        subject_ref=subject,
                        artifact_kind=subject.kind,
                        display_name=None,
                        state=state,
                        action=action,
                        reason=plan_action.reason,
                        prepared_refs=prepared_refs,
                    )
                )

    # any unknown-dependency subject not carried by a plan still gets a
    # review item — unknown resolution never skips silently.
    covered = set(merged)
    for assessment in assessments:
        for entry in assessment.entries:
            if entry.state != 'unknown_dependency':
                continue
            key = (entry.subject_ref.kind, entry.subject_ref.ref_id)
            if key in covered:
                continue
            _consider(
                _queue_item(
                    subject_ref=entry.subject_ref,
                    artifact_kind=entry.subject_ref.kind,
                    display_name=None,
                    state='uncertain',
                    action='review',
                    reason=entry.reason,
                    prepared_refs=prepared_refs,
                )
            )

    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (
                _CLASS_ORDER[item.execution_class],
                -_ACTION_ORDER[item.action],
                item.subject_ref.kind,
                item.subject_ref.ref_id,
            ),
        )
    )


def build_change_diff_record(
    *,
    document_id: str,
    from_revision: SceneRevision,
    to_revision: SceneRevision,
    changes: Sequence[SceneChange],
    change_event_refs: Sequence[AuthorityRef],
    recorded_at_utc: str,
) -> ChangeDiffRecord:
    return _seal(
        ChangeDiffRecord,
        {
            'document_id': document_id,
            'from_revision_id': from_revision.revision_id,
            'from_content_hash': from_revision.content_hash,
            'to_revision_id': to_revision.revision_id,
            'to_content_hash': to_revision.content_hash,
            'changes': [
                {**c.model_dump(mode='json'), 'axes': sorted(c.axes)}
                for c in changes
            ],
            'changed_axes': sorted(
                {axis for c in changes for axis in c.axes}
            ),
            'change_event_refs': [
                r.model_dump(mode='json') for r in change_event_refs
            ],
            'recorded_at_utc': recorded_at_utc,
            'invalidation_version': EVIDENCE_INVALIDATION_VERSION,
        },
        'diff_id',
        'diff_sha256',
        'chdiff',
    )


def build_revalidation_queue(
    *,
    document_id: str,
    diff_record: ChangeDiffRecord,
    impact_report: DependencyImpactReport,
    assessment_refs: Sequence[AuthorityRef],
    plan_refs: Sequence[AuthorityRef],
    items: Sequence[RevalidationQueueItem],
    created_at_utc: str,
) -> RevalidationQueue:
    software_sequence = tuple(
        item.item_key
        for item in items
        if item.execution_class == 'software'
    )
    return _seal(
        RevalidationQueue,
        {
            'document_id': document_id,
            'diff_ref': change_diff_binding(diff_record).model_dump(
                mode='json'
            ),
            'impact_report_id': impact_report.report_id,
            'impact_report_sha256': impact_report.report_sha256,
            'assessment_refs': [
                r.model_dump(mode='json') for r in assessment_refs
            ],
            'plan_refs': [r.model_dump(mode='json') for r in plan_refs],
            'to_revision_id': diff_record.to_revision_id,
            'to_content_hash': diff_record.to_content_hash,
            'items': [i.model_dump(mode='json') for i in items],
            'software_sequence': list(software_sequence),
            'created_at_utc': created_at_utc,
            'invalidation_version': EVIDENCE_INVALIDATION_VERSION,
        },
        'queue_id',
        'queue_sha256',
        'revqueue',
    )


@dataclass(frozen=True)
class RevalidationQueueBundle:
    """Everything one queue composition produced and persisted."""

    diff_record: ChangeDiffRecord
    change_events: tuple[SemanticChangeEvent, ...]
    assessments: tuple[StalenessAssessment, ...]
    plans: tuple[RevalidationPlan, ...]
    impact_report: DependencyImpactReport
    queue: RevalidationQueue


def compose_revalidation_queue(
    *,
    document_id: str,
    from_revision: SceneRevision,
    to_revision: SceneRevision,
    watched_artifacts: Sequence[WatchedArtifact] = (),
    extra_changes: Sequence[SceneChange] = (),
    edges: Sequence[DependencyEdgeDeclaration] = (),
    rule_profile: DependencyRuleProfile | None = None,
    lineage: dict[str, LineageCompleteness] | None = None,
    unknown_subjects: Sequence[AuthorityRef] = (),
    extra_events: Sequence[SemanticChangeEvent] = (),
    prepared_refs: Mapping[tuple[str, str], Sequence[AuthorityRef]] = {},
    dependency_repository: Any = None,
    revalidation_repository: Any = None,
    changed_at_utc: str | None = None,
) -> RevalidationQueueBundle:
    """Compose and persist the executable revalidation queue (#964).

    The scene diff becomes sealed change events; the declared edge graph
    + rule profile evaluate into sealed staleness assessments and
    revalidation plans when supplied; the watched-artifact impact report
    supplies the per-artifact verdicts. Items merge deterministically —
    any uncertain verdict forces review (fail-closed) — and
    ``changed_at_utc`` defaults to the successor revision's creation
    time so recomposing the same pair is bit-identical (idempotent,
    no duplicate records on restart).
    """
    if from_revision.document_id != to_revision.document_id:
        raise ValueError('revalidation requires revisions of one document')
    if from_revision.document_id != document_id:
        raise ValueError('revisions must belong to document_id')
    if from_revision.revision_id == to_revision.revision_id:
        raise ValueError('revalidation requires two distinct revisions')

    occurred_at_utc = changed_at_utc or to_revision.created_at_utc
    _require_iso8601(occurred_at_utc, 'changed_at_utc')

    changes = tuple(
        diff_scene_documents(from_revision.document, to_revision.document)
    ) + tuple(extra_changes)

    events = resolve_change_events(
        document_id=document_id,
        from_revision=from_revision,
        to_revision=to_revision,
        changes=changes,
        occurred_at_utc=occurred_at_utc,
    ) + tuple(extra_events)

    assessments: list[StalenessAssessment] = []
    plans: list[RevalidationPlan] = []
    if rule_profile is not None:
        for event in events:
            assessment = evaluate_staleness(
                document_id,
                edges,
                event,
                rule_profile,
                lineage=lineage,
                unknown_subjects=unknown_subjects,
                evaluated_at_utc=occurred_at_utc,
            )
            plan = build_revalidation_plan(
                document_id, assessment, planned_at_utc=occurred_at_utc
            )
            assessments.append(assessment)
            plans.append(plan)

    impact_report = build_dependency_impact_report(
        from_revision=from_revision,
        to_revision=to_revision,
        artifacts=watched_artifacts,
        extra_changes=extra_changes,
    )

    items = _merge_items(
        impact_report=impact_report,
        plans=plans,
        assessments=assessments,
        prepared_refs=prepared_refs,
    )

    event_refs = tuple(change_event_binding(e) for e in events)
    diff_record = build_change_diff_record(
        document_id=document_id,
        from_revision=from_revision,
        to_revision=to_revision,
        changes=changes,
        change_event_refs=event_refs,
        recorded_at_utc=occurred_at_utc,
    )
    queue = build_revalidation_queue(
        document_id=document_id,
        diff_record=diff_record,
        impact_report=impact_report,
        assessment_refs=tuple(staleness_binding(a) for a in assessments),
        plan_refs=tuple(revalidation_binding(p) for p in plans),
        items=items,
        created_at_utc=occurred_at_utc,
    )

    if dependency_repository is not None:
        for event in events:
            dependency_repository.save_change_event(event)
        for assessment in assessments:
            dependency_repository.save_assessment(assessment)
        for plan in plans:
            dependency_repository.save_plan(plan)
    if revalidation_repository is not None:
        revalidation_repository.save_diff_record(diff_record)
        revalidation_repository.save_queue(queue)

    return RevalidationQueueBundle(
        diff_record=diff_record,
        change_events=events,
        assessments=tuple(assessments),
        plans=tuple(plans),
        impact_report=impact_report,
        queue=queue,
    )


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------

SoftwareRunner = Callable[[RevalidationQueueItem], AuthorityRef | None]
"""Executes one software-class item and returns the ref of the sealed
result it produced (or None when the step completes without a record).
Raise ``QueueItemUnavailableError`` when no execution path is wired —
the run records 'unavailable' rather than fabricating a result."""


def build_queue_run(
    *,
    document_id: str,
    queue: RevalidationQueue,
    pre_head: SceneRevision | None,
    post_head: SceneRevision | None,
    outcomes: Sequence[QueueItemOutcome],
    verdict: QueueRunVerdict,
    started_at_utc: str,
    finished_at_utc: str,
) -> RevalidationQueueRun:
    return _seal(
        RevalidationQueueRun,
        {
            'document_id': document_id,
            'queue_ref': revalidation_queue_binding(queue).model_dump(
                mode='json'
            ),
            'pre_head_revision_id': (
                pre_head.revision_id if pre_head is not None else None
            ),
            'pre_head_content_hash': (
                pre_head.content_hash if pre_head is not None else None
            ),
            'post_head_revision_id': (
                post_head.revision_id if post_head is not None else None
            ),
            'post_head_content_hash': (
                post_head.content_hash if post_head is not None else None
            ),
            'outcomes': [o.model_dump(mode='json') for o in outcomes],
            'verdict': verdict,
            'started_at_utc': started_at_utc,
            'finished_at_utc': finished_at_utc,
            'invalidation_version': EVIDENCE_INVALIDATION_VERSION,
        },
        'run_id',
        'run_sha256',
        'revrun',
    )


def _head_matches(head: SceneRevision, queue: RevalidationQueue) -> bool:
    return (
        head.revision_id == queue.to_revision_id
        and head.content_hash == queue.to_content_hash
    )


def run_queue_software(
    queue: RevalidationQueue,
    *,
    pre_head_revision: SceneRevision | None,
    post_head_revision: SceneRevision | None = None,
    software_runner: SoftwareRunner | None = None,
    revalidation_repository: Any = None,
    started_at_utc: str | None = None,
    finished_at_utc: str | None = None,
) -> RevalidationQueueRun:
    """Run the queue's software items in one deterministic operation.

    Fail-closed fencing: the pre-run head must match the queue's pinned
    ``to_revision`` (id + content hash); the post-run head is compared
    again and any drift flips the verdict to 'drift_detected' — old
    results are never overwritten by a run that saw a moving target.
    Physical items always record 'awaiting_physical' (confirmation
    required before any device work); review items 'awaiting_review'.
    """
    started = started_at_utc or _utc_now()
    _require_iso8601(started, 'started_at_utc')
    outcomes: list[QueueItemOutcome] = []

    if pre_head_revision is None or not _head_matches(
        pre_head_revision, queue
    ):
        for item in queue.items:
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='skipped',
                    note='head revision does not match the queue pin',
                )
            )
        finished = finished_at_utc or _utc_now()
        run = build_queue_run(
            document_id=queue.document_id,
            queue=queue,
            pre_head=pre_head_revision,
            post_head=pre_head_revision,
            outcomes=outcomes,
            verdict='drift_detected',
            started_at_utc=started,
            finished_at_utc=finished,
        )
        if revalidation_repository is not None:
            revalidation_repository.save_run(run)
        return run

    sequence = set(queue.software_sequence)
    software_failed = False
    for item in queue.items:
        if item.execution_class == 'physical':
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='awaiting_physical',
                    note='human confirmation required before physical work',
                )
            )
            continue
        if item.execution_class == 'review':
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='awaiting_review',
                    note='fail-closed: human review required',
                )
            )
            continue
        if software_failed:
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='skipped',
                    note='skipped after a software item failed',
                )
            )
            continue
        if software_runner is None:
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='unavailable',
                    note='no software runner wired for this route',
                )
            )
            continue
        try:
            result_ref = software_runner(item)
        except QueueItemUnavailableError as exc:
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='unavailable',
                    note=str(exc) or 'runner declined this item',
                )
            )
        except Exception as exc:
            software_failed = True
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='failed',
                    note=f'{type(exc).__name__}: {exc}',
                )
            )
        else:
            outcomes.append(
                QueueItemOutcome(
                    item_key=item.item_key,
                    status='completed',
                    result_ref=result_ref,
                )
            )

    post_head = post_head_revision or pre_head_revision
    finished = finished_at_utc or _utc_now()
    if not _head_matches(post_head, queue):
        verdict: QueueRunVerdict = 'drift_detected'
    elif software_failed:
        verdict = 'failed'
    elif not queue.items:
        verdict = 'already_current'
    elif all(o.status == 'completed' for o in outcomes):
        verdict = 'software_complete'
    else:
        verdict = 'awaiting_human'
    run = build_queue_run(
        document_id=queue.document_id,
        queue=queue,
        pre_head=pre_head_revision,
        post_head=post_head,
        outcomes=outcomes,
        verdict=verdict,
        started_at_utc=started,
        finished_at_utc=finished,
    )
    if revalidation_repository is not None:
        revalidation_repository.save_run(run)
    return run


__all__ = [
    'ChangeDiffRecord',
    'EVIDENCE_INVALIDATION_VERSION',
    'QueueAction',
    'QueueExecutionClass',
    'QueueItemOutcome',
    'QueueItemState',
    'QueueItemStatus',
    'QueueItemUnavailableError',
    'QueueRoute',
    'QueueRunVerdict',
    'RevalidationQueue',
    'RevalidationQueueBundle',
    'RevalidationQueueItem',
    'RevalidationQueueRun',
    'SoftwareRunner',
    'build_change_diff_record',
    'build_queue_run',
    'build_revalidation_queue',
    'change_diff_binding',
    'compose_revalidation_queue',
    'queue_run_binding',
    'resolve_change_events',
    'revalidation_queue_binding',
    'run_queue_software',
    'scene_revision_binding',
]
