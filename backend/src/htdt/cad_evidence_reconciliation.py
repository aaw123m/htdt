"""Multi-source evidence reconciliation (#602).

Capture data, manual dimensions, floor plans and imported geometry all
describe the same physical room, but they are *different authorities with
different provenance*. This module reconciles them per subject without
collapsing provenance into a single merged truth: every observation keeps
its source identity, and the reconciliation decision records which sources
agreed, conflicted, or could not be compared at all.

Contract properties:

- an :class:`EvidenceSubject` names *what* is being observed (a dimension,
  position, orientation, placement, identity or metric of a target ref);
- an :class:`EvidenceObservation` is immutable evidence from exactly one
  source; its ``alignment_key`` must name a shared frame (e.g. the scene
  revision + coordinate convention) before it can be compared;
- comparisons are alignment-gated: observations whose ``alignment_key`` is
  unset or different are ``not_comparable`` — never silently merged;
- uncertainty is uncertainty-aware and ``unknown`` stays ``unknown``: an
  observation without a quantified uncertainty cannot prove consistency or
  conflict;
- every :class:`ReconciliationDecision` is a versioned, content-hashed
  record — later re-reconciliation appends a new decision instead of
  rewriting the previous one.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


RECONCILIATION_SCHEMA_VERSION = 1
RECONCILIATION_AUTHORITY_VERSION = 'evidence-reconciliation-1'

#: Where an observation comes from; provenance is never collapsed.
ObservationSourceKind = Literal[
    'capture',
    'manual_dimension',
    'floor_plan',
    'imported_geometry',
    'measurement',
    'derived',
    'other',
]

SubjectKind = Literal[
    'dimension',
    'position',
    'orientation',
    'placement',
    'identity',
    'metric',
    'other',
]

#: Per-pair and subject-level outcomes.
#:
#: - ``consistent`` — every comparable pair agrees within tolerance plus
#:   combined uncertainty;
#: - ``conflict`` — at least one comparable pair disagrees beyond tolerance
#:   plus combined uncertainty;
#: - ``unknown`` — a numeric comparison could not be decided because an
#:   uncertainty was never quantified or an interval straddles the bound;
#: - ``not_comparable`` — alignment (shared frame) is unresolved for a pair;
#: - ``insufficient`` — fewer than two observations exist.
ReconciliationOutcome = Literal[
    'consistent', 'conflict', 'not_comparable', 'unknown', 'insufficient'
]


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


class EvidenceSubject(BaseModel):
    """What is being reconciled across sources."""

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_kind: SubjectKind
    target_ref: str = Field(min_length=1)
    attribute: str = Field(min_length=1)
    description: str | None = None


def build_evidence_subject(
    *,
    document_id: str,
    subject_kind: SubjectKind,
    target_ref: str,
    attribute: str,
    description: str | None = None,
    subject_id: str | None = None,
) -> EvidenceSubject:
    return EvidenceSubject(
        subject_id=subject_id or str(uuid4()),
        document_id=document_id,
        subject_kind=subject_kind,
        target_ref=target_ref,
        attribute=attribute,
        description=description,
    )


class EvidenceObservation(BaseModel):
    """One immutable observation from exactly one source."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source: ObservationSourceKind
    source_ref: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(
        default=None, min_length=8, max_length=64
    )
    value: float | None = None
    value_text: str | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    alignment_key: str | None = Field(default=None, min_length=1)
    captured_at_utc: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_observation(self) -> 'EvidenceObservation':
        if self.value is None and self.value_text is None:
            raise ValueError('observation requires a value or value_text')
        return self


def build_observation(
    *,
    subject_id: str,
    source: ObservationSourceKind,
    source_ref: str | None = None,
    source_sha256: str | None = None,
    value: float | None = None,
    value_text: str | None = None,
    uncertainty: float | None = None,
    alignment_key: str | None = None,
    captured_at_utc: str | None = None,
    note: str | None = None,
    observation_id: str | None = None,
) -> EvidenceObservation:
    return EvidenceObservation(
        observation_id=observation_id or str(uuid4()),
        subject_id=subject_id,
        source=source,
        source_ref=source_ref,
        source_sha256=source_sha256,
        value=value,
        value_text=value_text,
        uncertainty=uncertainty,
        alignment_key=alignment_key,
        captured_at_utc=captured_at_utc,
        note=note,
    )


class ObservationComparison(BaseModel):
    """One pairwise comparison inside a reconciliation decision."""

    model_config = ConfigDict(frozen=True)

    left_observation_id: str
    right_observation_id: str
    outcome: ReconciliationOutcome
    delta: float | None = None
    reason: str = Field(min_length=1)


class ReconciliationDecision(BaseModel):
    """A versioned decision about one subject's observations."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = RECONCILIATION_SCHEMA_VERSION
    authority_version: Literal['evidence-reconciliation-1'] = (
        RECONCILIATION_AUTHORITY_VERSION
    )
    decision_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    outcome: ReconciliationOutcome
    tolerance: float | None = Field(default=None, ge=0)
    comparisons: tuple[ObservationComparison, ...] = ()
    rationale: str = Field(min_length=1)
    decided_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_decision(self) -> 'ReconciliationDecision':
        if self.decision_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ReconciliationDecision hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'decision_id': self.decision_id,
            'subject_id': self.subject_id,
            'document_id': self.document_id,
            'outcome': self.outcome,
            'tolerance': self.tolerance,
            'comparisons': [
                c.model_dump(mode='json') for c in self.comparisons
            ],
            'rationale': self.rationale,
            'decided_by': self.decided_by,
            'decided_at_utc': self.decided_at_utc,
        }


def _compare_pair(
    left: EvidenceObservation,
    right: EvidenceObservation,
    tolerance: float,
) -> ObservationComparison:
    def comparison(
        outcome: ReconciliationOutcome,
        reason: str,
        delta: float | None = None,
    ) -> ObservationComparison:
        return ObservationComparison(
            left_observation_id=left.observation_id,
            right_observation_id=right.observation_id,
            outcome=outcome,
            delta=delta,
            reason=reason,
        )

    if left.alignment_key is None or right.alignment_key is None:
        return comparison(
            'not_comparable',
            'alignment is unresolved for at least one observation',
        )
    if left.alignment_key != right.alignment_key:
        return comparison(
            'not_comparable',
            'observations describe different alignment frames',
        )
    if left.value is None or right.value is None:
        if (
            left.value_text is not None
            and left.value_text == right.value_text
        ):
            return comparison(
                'consistent', 'textual observations agree', delta=0.0
            )
        return comparison('conflict', 'textual observations disagree')
    delta = abs(left.value - right.value)
    if left.uncertainty is None or right.uncertainty is None:
        return comparison(
            'unknown',
            'an observation uncertainty was never quantified',
            delta=delta,
        )
    bound = tolerance + left.uncertainty + right.uncertainty
    if delta <= bound:
        return comparison(
            'consistent',
            'observations agree within tolerance plus uncertainty',
            delta=delta,
        )
    return comparison(
        'conflict',
        'observations disagree beyond tolerance plus uncertainty',
        delta=delta,
    )


def reconcile_subject(
    subject: EvidenceSubject,
    observations: tuple[EvidenceObservation, ...],
    *,
    tolerance: float = 0.0,
    decided_at_utc: str,
    decided_by: str = 'system',
    decision_id: str | None = None,
) -> ReconciliationDecision:
    """Reconcile every pair of observations for one subject.

    The subject outcome is strict: any unresolved alignment makes the
    subject ``not_comparable``; else any conflict makes it ``conflict``;
    else any undecidable pair makes it ``unknown``; only a fully comparable,
    fully consistent set is ``consistent``.
    """

    subject_observations = tuple(
        o for o in observations if o.subject_id == subject.subject_id
    )
    comparisons: list[ObservationComparison] = []
    for index, left in enumerate(subject_observations):
        for right in subject_observations[index + 1 :]:
            comparisons.append(_compare_pair(left, right, tolerance))
    if len(subject_observations) < 2:
        outcome: ReconciliationOutcome = 'insufficient'
        rationale = 'fewer than two observations exist for this subject'
    elif any(c.outcome == 'not_comparable' for c in comparisons):
        outcome = 'not_comparable'
        rationale = 'alignment is unresolved across the observation set'
    elif any(c.outcome == 'conflict' for c in comparisons):
        outcome = 'conflict'
        rationale = 'at least one comparable pair disagrees'
    elif any(c.outcome == 'unknown' for c in comparisons):
        outcome = 'unknown'
        rationale = 'at least one comparison could not be decided'
    else:
        outcome = 'consistent'
        rationale = 'all comparable observations agree within tolerance'
    payload: dict[str, Any] = {
        'decision_id': decision_id or str(uuid4()),
        'subject_id': subject.subject_id,
        'document_id': subject.document_id,
        'outcome': outcome,
        'tolerance': tolerance,
        'comparisons': tuple(comparisons),
        'rationale': rationale,
        'decided_by': decided_by,
        'decided_at_utc': decided_at_utc,
    }
    provisional = ReconciliationDecision.model_construct(
        **payload, decision_sha256='0' * 64
    )
    return ReconciliationDecision(
        **payload,
        decision_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'RECONCILIATION_AUTHORITY_VERSION',
    'RECONCILIATION_SCHEMA_VERSION',
    'EvidenceObservation',
    'EvidenceSubject',
    'ObservationComparison',
    'ObservationSourceKind',
    'ReconciliationDecision',
    'ReconciliationOutcome',
    'SubjectKind',
    'build_evidence_subject',
    'build_observation',
    'reconcile_subject',
]
