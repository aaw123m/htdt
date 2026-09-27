"""Project design brief authority (#555).

Today the project's design intent is scattered across geometry constraints,
StandardsProfile criteria, optimization SearchSpec, target curves, seat
priorities, topology/headroom/cost criteria, projector/sightline evaluation
and calibration plans. Each of those authorities stays independent and
canonical; what is missing is one immutable record answering

> What is this theater being designed to achieve, for whom, under which
> explicit constraints and priorities?

:class:`ProjectDesignBrief` is that record. It is a versioned, document-bound
envelope that *references* existing exact authorities (it never copies their
values), classifies each goal as required / preferred / informational, and
preserves every criterion's independent semantics and UNKNOWN state — it is
deliberately not a single hidden score.

Contract properties:

- a project persists an immutable versioned brief independent of mutable UI
  state; a project without a brief is a valid NOT_CONFIGURED state, never an
  inferred one;
- every goal is an exact reference (``BriefGoalRef``) to an existing
  canonical authority plus free-text intent; nothing is duplicated;
- changing the brief appends a new brief (``supersedes_brief_id`` chains
  history) — historical optimization/validation results that bound an older
  brief hash are never rewritten;
- freshness evaluation (:func:`evaluate_brief_coverage`) reports per-goal
  CURRENT / STALE / MISSING / UNEVALUABLE so an Overview can show goal
  coverage and blocked reasons without turning them into one score.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


BRIEF_SCHEMA_VERSION = 1
BRIEF_AUTHORITY_VERSION = 'project-design-brief-1'

#: Summary marker when a project has no brief — a valid state, not an error.
BRIEF_NOT_CONFIGURED = 'not_configured'

#: How strongly the user intends a goal. Informational goals are recorded
#: for context but are never silently upgraded into constraints.
BriefRequirement = Literal['required', 'preferred', 'informational']

#: Authority kinds a goal may reference. 'free_text' carries intent that has
#: no structured authority yet; 'other' leaves room for new authorities
#: without weakening the ones already named.
BriefGoalKind = Literal[
    'intended_use',
    'listening_population',
    'seat_priority',
    'target_curve',
    'acoustic_performance_target',
    'spl_headroom',
    'standards_profile',
    'video_geometry',
    'photometric_goal',
    'budget_scenario',
    'installation_effort',
    'room_use_constraint',
    'noise_goal',
    'free_text',
    'other',
]

BriefGoalFreshness = Literal['current', 'stale', 'missing', 'unevaluable']






def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


class BriefGoalRef(BaseModel):
    """One design goal: an exact authority reference plus human intent.

    ``ref_id``/``ref_sha256`` pin the referenced authority's exact semantic
    hash — both are required for every non-``free_text`` goal so an
    unresolved external intent can never masquerade as a bound authority
    (it must be recorded as a dependency state or free-text instead).
    """

    model_config = ConfigDict(frozen=True)

    goal_id: str = Field(min_length=1)
    kind: BriefGoalKind
    requirement: BriefRequirement = 'preferred'
    ref_id: str | None = Field(default=None, min_length=1)
    ref_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    label: str = Field(min_length=1)
    rationale: str | None = None

    @model_validator(mode='after')
    def valid_goal(self) -> 'BriefGoalRef':
        if self.kind == 'free_text':
            if self.ref_id is not None or self.ref_sha256 is not None:
                raise ValueError(
                    'free_text goals carry intent text only, not refs'
                )
            return self
        if self.ref_id is None or self.ref_sha256 is None:
            raise ValueError(
                'non-free_text goals must reference an exact authority: '
                'both ref_id and ref_sha256 are required'
            )
        return self


class ProjectDesignBrief(BaseModel):
    """Immutable versioned design-intent envelope for one document."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = BRIEF_SCHEMA_VERSION
    authority_version: Literal['project-design-brief-1'] = (
        BRIEF_AUTHORITY_VERSION
    )
    brief_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    #: Free-form intended-use / scenario labels (no hardcoded theater types).
    use_labels: tuple[str, ...] = ()
    goal_refs: tuple[BriefGoalRef, ...] = ()
    note: str | None = None
    provenance: tuple[str, ...] = ()
    supersedes_brief_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    brief_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_brief(self) -> 'ProjectDesignBrief':
        goal_ids = [goal.goal_id for goal in self.goal_refs]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError('brief goal ids must be unique')
        ref_keys = [
            (goal.kind, goal.ref_id)
            for goal in self.goal_refs
            if goal.ref_id is not None
        ]
        if len(ref_keys) != len(set(ref_keys)):
            raise ValueError('brief authority refs must be unique per kind/ref')
        _validate_instant(self.created_at_utc)
        if self.brief_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectDesignBrief hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'brief_id': self.brief_id,
            'document_id': self.document_id,
            'title': self.title,
            'use_labels': list(self.use_labels),
            'goal_refs': [goal.model_dump(mode='json') for goal in self.goal_refs],
            'note': self.note,
            'provenance': list(self.provenance),
            'supersedes_brief_id': self.supersedes_brief_id,
            'created_at_utc': self.created_at_utc,
        }

    def goal(self, goal_id: str) -> BriefGoalRef | None:
        for goal in self.goal_refs:
            if goal.goal_id == goal_id:
                return goal
        return None


class BriefGoalStatus(BaseModel):
    """Freshness/evaluability of one goal against current authority state."""

    model_config = ConfigDict(frozen=True)

    goal_id: str
    kind: str
    requirement: BriefRequirement
    state: BriefGoalFreshness
    reason: str


class BriefCoverage(BaseModel):
    """Per-goal coverage report — never collapsed into one score."""

    model_config = ConfigDict(frozen=True)

    brief_id: str
    goals: tuple[BriefGoalStatus, ...]

    @property
    def evaluable_goal_ids(self) -> tuple[str, ...]:
        return tuple(
            goal.goal_id for goal in self.goals if goal.state == 'current'
        )

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(
            f'{goal.goal_id}: {goal.reason}'
            for goal in self.goals
            if goal.state in {'stale', 'missing'}
        )


def _validate_instant(value: str) -> None:
    """Enforce strict, explicitly-UTC ISO 8601 persisted timestamps (#870).

    Naive strings, partial dates and non-UTC offsets are rejected before
    they can rewrite history semantics silently.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('created_at_utc is not a valid ISO 8601 instant') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError('created_at_utc must carry an explicit UTC offset')


class DesignBriefIntegrityError(ValueError):
    """Persisted brief lineage is corrupt (fork, cycle, dangling ref)."""


def brief_lineage_issues(
    briefs: tuple[ProjectDesignBrief, ...],
) -> tuple[str, ...]:
    """Detect topology violations in a document's brief history (#870).

    A project's brief is a single-head supersession chain: a fork, a cycle,
    or a dangling/self/foreign predecessor makes the persisted history
    untrustworthy and must fail closed rather than collapse to a timestamp
    "newest" pick.
    """

    issues: list[str] = []
    by_id = {item.brief_id: item for item in briefs}
    successors: dict[str, list[str]] = {}
    for item in briefs:
        predecessor = item.supersedes_brief_id
        if predecessor is None:
            continue
        successors.setdefault(predecessor, []).append(item.brief_id)
        target = by_id.get(predecessor)
        if predecessor == item.brief_id:
            issues.append(f'brief {item.brief_id} supersedes itself')
        elif target is None:
            issues.append(
                f'brief {item.brief_id} supersedes missing predecessor '
                f'{predecessor}'
            )
        elif target.document_id != item.document_id:
            issues.append(
                f'brief {item.brief_id} supersedes a brief from another '
                'document'
            )
    for predecessor, children in successors.items():
        if len(children) > 1:
            issues.append(
                f'brief {predecessor} has multiple successors '
                f'({len(children)}): forked lineage'
            )
    for item in briefs:
        seen: set[str] = set()
        cursor: ProjectDesignBrief | None = item
        while cursor is not None and cursor.supersedes_brief_id is not None:
            predecessor_id = cursor.supersedes_brief_id
            if predecessor_id in seen:
                issues.append(
                    f'brief {item.brief_id} reaches a supersession cycle'
                )
                break
            seen.add(predecessor_id)
            cursor = by_id.get(predecessor_id)
    return tuple(issues)


def current_brief(
    briefs: tuple[ProjectDesignBrief, ...],
) -> ProjectDesignBrief | None:
    """The unique head of the document's supersession chain, or ``None``.

    ``None`` is the NOT_CONFIGURED state — a project with no brief. More
    than one head is a persisted fork and raises
    :class:`DesignBriefIntegrityError` instead of guessing (#870).
    """

    issues = brief_lineage_issues(briefs)
    if issues:
        raise DesignBriefIntegrityError(
            'design brief lineage is corrupt: ' + '; '.join(issues)
        )
    superseded = {
        item.supersedes_brief_id
        for item in briefs
        if item.supersedes_brief_id is not None
    }
    heads = [item for item in briefs if item.brief_id not in superseded]
    if len(heads) > 1:
        raise DesignBriefIntegrityError(
            'design brief lineage has multiple heads: '
            + ', '.join(sorted(item.brief_id for item in heads))
        )
    return heads[0] if heads else None


def build_design_brief(
    *,
    document_id: str,
    title: str,
    use_labels: tuple[str, ...] = (),
    goal_refs: tuple[BriefGoalRef, ...] = (),
    note: str | None = None,
    provenance: tuple[str, ...] = (),
    supersedes_brief_id: str | None = None,
    created_at_utc: str,
    brief_id: str | None = None,
) -> ProjectDesignBrief:
    payload: dict[str, Any] = {
        'brief_id': brief_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'use_labels': tuple(use_labels),
        'goal_refs': tuple(goal_refs),
        'note': note,
        'provenance': tuple(provenance),
        'supersedes_brief_id': supersedes_brief_id,
        'created_at_utc': created_at_utc,
    }
    provisional = ProjectDesignBrief.model_construct(
        **payload, brief_sha256='0' * 64
    )
    return ProjectDesignBrief(
        **payload,
        brief_sha256=_hash(provisional.semantic_payload()),
    )


def revise_design_brief(
    brief: ProjectDesignBrief,
    *,
    title: str | None = None,
    use_labels: tuple[str, ...] | None = None,
    goal_refs: tuple[BriefGoalRef, ...] | None = None,
    note: str | None = None,
    provenance: tuple[str, ...] | None = None,
    created_at_utc: str,
    brief_id: str | None = None,
) -> ProjectDesignBrief:
    """Append the next brief version superseding ``brief``.

    The old brief is never mutated; downstream runs that pinned its hash stay
    bound to the exact goal set they used.
    """

    return build_design_brief(
        document_id=brief.document_id,
        title=brief.title if title is None else title,
        use_labels=brief.use_labels if use_labels is None else use_labels,
        goal_refs=brief.goal_refs if goal_refs is None else goal_refs,
        note=brief.note if note is None else note,
        provenance=brief.provenance if provenance is None else provenance,
        supersedes_brief_id=brief.brief_id,
        created_at_utc=created_at_utc,
        brief_id=brief_id,
    )


def evaluate_brief_coverage(
    brief: ProjectDesignBrief,
    *,
    resolve_sha256: 'Mapping[tuple[str, str], str | None]',
) -> BriefCoverage:
    """Per-goal freshness against current authority state.

    ``resolve_sha256`` maps ``(kind, ref_id)`` to the referenced authority's
    current semantic hash, or ``None`` when the id no longer resolves. Every
    non-free-text goal carries an exact pin, so resolution checks the pin
    directly; a free-text goal is always UNEVALUABLE intent.
    """

    goals: list[BriefGoalStatus] = []
    for goal in brief.goal_refs:
        if goal.kind == 'free_text' or goal.ref_id is None:
            goals.append(
                BriefGoalStatus(
                    goal_id=goal.goal_id,
                    kind=goal.kind,
                    requirement=goal.requirement,
                    state='unevaluable',
                    reason='goal is human intent without a bound authority',
                )
            )
            continue
        current = resolve_sha256.get((goal.kind, goal.ref_id))
        if current is None:
            goals.append(
                BriefGoalStatus(
                    goal_id=goal.goal_id,
                    kind=goal.kind,
                    requirement=goal.requirement,
                    state='missing',
                    reason='referenced authority no longer resolves',
                )
            )
        elif goal.ref_sha256 is None:
            goals.append(
                BriefGoalStatus(
                    goal_id=goal.goal_id,
                    kind=goal.kind,
                    requirement=goal.requirement,
                    state='unevaluable',
                    reason='goal has no semantic pin to evaluate against',
                )
            )
        elif goal.ref_sha256 != current:
            goals.append(
                BriefGoalStatus(
                    goal_id=goal.goal_id,
                    kind=goal.kind,
                    requirement=goal.requirement,
                    state='stale',
                    reason='referenced authority changed since the brief was saved',
                )
            )
        else:
            goals.append(
                BriefGoalStatus(
                    goal_id=goal.goal_id,
                    kind=goal.kind,
                    requirement=goal.requirement,
                    state='current',
                    reason='reference unchanged',
                )
            )
    return BriefCoverage(brief_id=brief.brief_id, goals=tuple(goals))


__all__ = [
    'BRIEF_AUTHORITY_VERSION',
    'BRIEF_NOT_CONFIGURED',
    'BRIEF_SCHEMA_VERSION',
    'BriefCoverage',
    'DesignBriefIntegrityError',
    'brief_lineage_issues',
    'current_brief',
    'BriefGoalFreshness',
    'BriefGoalKind',
    'BriefGoalRef',
    'BriefGoalStatus',
    'BriefRequirement',
    'ProjectDesignBrief',
    'build_design_brief',
    'evaluate_brief_coverage',
    'revise_design_brief',
]
