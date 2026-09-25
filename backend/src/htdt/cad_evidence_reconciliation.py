"""Multi-source evidence reconciliation (#602).

Capture data, manual dimensions, floor plans and imported geometry all
describe the same physical room, but they are *different authorities with
different provenance*. This module reconciles them per subject without
collapsing provenance into a single merged truth: every observation keeps
its exact source identity, and the reconciliation decision records which
sources agreed, conflicted, or could not be compared at all.

Contract properties:

- an :class:`EvidenceSubject` names *what* is being observed — a typed,
  resolvable ``target`` authority plus the attribute under comparison;
- an :class:`EvidenceObservation` is immutable evidence from exactly one
  source; non-manual sources (capture, floor plan, imported geometry,
  measurement, derived) must carry the exact ``source_ref`` +
  ``source_sha256`` of the producing authority;
- numeric observations carry an explicit ``unit`` and comparisons convert
  through the canonical unit policy — a millimetre observation and a metre
  observation compare only after conversion, and unlike quantities are
  never numerically compared;
- comparability requires an exact alignment frame: an
  :class:`AlignmentRef` naming a typed frame/datum identity. Missing or
  mismatched frames are ``not_comparable`` — equal strings typed by two
  callers are not proof of a shared frame;
- uncertainty is uncertainty-aware and ``unknown`` stays ``unknown``: an
  observation without a quantified uncertainty cannot prove consistency or
  conflict;
- every :class:`ReconciliationDecision` is a versioned, content-hashed
  record pinning the exact ``observation_ids`` it reconciled — later
  re-reconciliation appends a new decision and never rewrites the previous
  one's input set.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_units import UnitKind, convert_unit


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

#: Sources that must carry exact producing-authority provenance
#: (``source_ref`` + ``source_sha256``). ``manual_dimension``/``other`` may
#: be operator-entered values with no canonical producer.
_HASH_BEARING_SOURCES: frozenset[str] = frozenset(
    {'capture', 'floor_plan', 'imported_geometry', 'measurement', 'derived'}
)

#: Alignment frame authorities two observations may share. Non-manual
#: frames carry an exact ``frame_sha256``; ``manual_frame`` is a declared
#: operator datum compared on id alone.
AlignmentFrameKind = Literal[
    'scene_datum',
    'capture_frame',
    'imported_frame',
    'manual_frame',
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


class AlignmentRef(BaseModel):
    """An exact shared frame/datum two observations must name identically.

    ``frame_sha256`` pins the frame authority's semantic hash where the
    frame kind is hash-bearing (everything except ``manual_frame``); two
    manually declared frames compare only on ``frame_kind`` + ``frame_id``.
    """

    model_config = ConfigDict(frozen=True)

    frame_kind: AlignmentFrameKind
    frame_id: str = Field(min_length=1)
    frame_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_alignment(self) -> 'AlignmentRef':
        if self.frame_kind != 'manual_frame' and self.frame_sha256 is None:
            raise ValueError(
                f'alignment frame kind {self.frame_kind!r} requires an '
                'exact frame_sha256'
            )
        return self

    def comparable_to(self, other: 'AlignmentRef') -> bool:
        """Exact frame identity, not a matching string."""

        if self.frame_kind != other.frame_kind:
            return False
        if self.frame_id != other.frame_id:
            return False
        if self.frame_kind == 'manual_frame':
            return True
        return self.frame_sha256 == other.frame_sha256


def _validate_instant(value: str, field: str) -> None:
    """Enforce strict, explicitly-UTC ISO 8601 persisted timestamps (#873)."""

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} is not a valid ISO 8601 instant') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f'{field} must carry an explicit UTC offset')


class EvidenceSubject(BaseModel):
    """What is being reconciled across sources.

    ``target`` is a typed, resolvable authority ref — an exact
    SceneRevision+entity, SystemVariant, equipment instance or other
    canonical authority — never an opaque string that could collide across
    projects or revisions.

    ``subject_sha256`` is the subject's immutable semantic identity (#873):
    required for newly built subjects, ``None`` only on legacy payloads
    persisted before the field existed — repositories classify those as
    legacy rather than silently trusting them.
    """

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    subject_kind: SubjectKind
    target: AuthorityRef
    attribute: str = Field(min_length=1)
    description: str | None = None
    subject_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_subject(self) -> 'EvidenceSubject':
        if (
            self.subject_sha256 is not None
            and self.subject_sha256 != _hash(self.semantic_payload())
        ):
            raise ValueError('EvidenceSubject hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        """The content the subject hash binds — every semantic field."""

        return {
            'subject_id': self.subject_id,
            'document_id': self.document_id,
            'subject_kind': self.subject_kind,
            'target': self.target.model_dump(mode='json'),
            'attribute': self.attribute,
            'description': self.description,
        }


def evidence_subject_sha256(subject: EvidenceSubject) -> str:
    """Semantic identity of a subject, computed for legacy rows too (#873)."""

    return _hash(subject.semantic_payload())


def build_evidence_subject(
    *,
    document_id: str,
    subject_kind: SubjectKind,
    target: AuthorityRef,
    attribute: str,
    description: str | None = None,
    subject_id: str | None = None,
) -> EvidenceSubject:
    subject = EvidenceSubject(
        subject_id=subject_id or str(uuid4()),
        document_id=document_id,
        subject_kind=subject_kind,
        target=target,
        attribute=attribute,
        description=description,
    )
    return subject.model_copy(
        update={'subject_sha256': _hash(subject.semantic_payload())}
    )


class EvidenceObservation(BaseModel):
    """One immutable observation from exactly one source."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source: ObservationSourceKind
    source_ref: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    value: float | None = None
    value_text: str | None = None
    #: The unit ``value``/``uncertainty`` are expressed in; required for
    #: numeric observations.
    unit: UnitKind | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    #: The exact shared frame this observation is expressed in; ``None``
    #: means no comparability claim exists.
    alignment: AlignmentRef | None = None
    captured_at_utc: str | None = None
    note: str | None = None
    #: Immutable semantic identity (#873): required on newly built
    #: observations; ``None`` only on legacy payloads.
    observation_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_observation(self) -> 'EvidenceObservation':
        if self.value is None and self.value_text is None:
            raise ValueError('observation requires a value or value_text')
        if self.value is not None and self.unit is None:
            raise ValueError(
                'numeric observations require an explicit unit'
            )
        if self.value is None and self.unit is not None:
            raise ValueError('unit requires a numeric value')
        if self.source in _HASH_BEARING_SOURCES:
            if self.source_ref is None or self.source_sha256 is None:
                raise ValueError(
                    f'{self.source} observations require the exact '
                    'source_ref and source_sha256 of the producing '
                    'authority'
                )
        elif self.source_sha256 is not None and self.source_ref is None:
            raise ValueError('source_sha256 requires source_ref')
        if self.captured_at_utc is not None:
            _validate_instant(self.captured_at_utc, 'captured_at_utc')
        if (
            self.observation_sha256 is not None
            and self.observation_sha256 != _hash(self.semantic_payload())
        ):
            raise ValueError('EvidenceObservation hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        """The content the observation hash binds (#873): id, bound
        subject, exact source authority ref/hash, value/unit/uncertainty,
        alignment frame and provenance fields."""

        return {
            'observation_id': self.observation_id,
            'subject_id': self.subject_id,
            'source': self.source,
            'source_ref': self.source_ref,
            'source_sha256': self.source_sha256,
            'value': self.value,
            'value_text': self.value_text,
            'unit': self.unit,
            'uncertainty': self.uncertainty,
            'alignment': (
                None
                if self.alignment is None
                else self.alignment.model_dump(mode='json')
            ),
            'captured_at_utc': self.captured_at_utc,
            'note': self.note,
        }


def evidence_observation_sha256(observation: EvidenceObservation) -> str:
    """Semantic identity of an observation, computed for legacy rows (#873)."""

    return _hash(observation.semantic_payload())


def build_observation(
    *,
    subject_id: str,
    source: ObservationSourceKind,
    source_ref: str | None = None,
    source_sha256: str | None = None,
    value: float | None = None,
    value_text: str | None = None,
    unit: UnitKind | None = None,
    uncertainty: float | None = None,
    alignment: AlignmentRef | None = None,
    captured_at_utc: str | None = None,
    note: str | None = None,
    observation_id: str | None = None,
) -> EvidenceObservation:
    observation = EvidenceObservation(
        observation_id=observation_id or str(uuid4()),
        subject_id=subject_id,
        source=source,
        source_ref=source_ref,
        source_sha256=source_sha256,
        value=value,
        value_text=value_text,
        unit=unit,
        uncertainty=uncertainty,
        alignment=alignment,
        captured_at_utc=captured_at_utc,
        note=note,
    )
    return observation.model_copy(
        update={
            'observation_sha256': _hash(observation.semantic_payload())
        }
    )


class ObservationComparison(BaseModel):
    """One pairwise comparison inside a reconciliation decision."""

    model_config = ConfigDict(frozen=True)

    left_observation_id: str
    right_observation_id: str
    outcome: ReconciliationOutcome
    #: Absolute difference expressed in the left observation's unit.
    delta: float | None = None
    reason: str = Field(min_length=1)


class ReconciliationObservationRef(BaseModel):
    """Exact pinned identity of one reconciled observation (#873).

    A bare ``observation_id`` cannot prove the stored row is still the
    bytes the decision reconciled; the hash pin turns row edits/corruption
    into read failures instead of silently shifting semantics.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ReconciliationDecision(BaseModel):
    """A versioned decision about one subject's observations.

    ``observation_ids`` pins the exact (sorted) observation set the
    decision reconciled — observations appended later never retroactively
    change which evidence this decision claims to cover.

    #873 pins the inputs' semantic identities too: ``subject_sha256`` and
    ``observation_refs`` carry the exact hashes the decision was derived
    from, so corrupted/imported input rows invalidate the read instead of
    changing the decision's meaning. ``None`` on both marks a legacy
    ID-only record persisted before the pins existed.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = RECONCILIATION_SCHEMA_VERSION
    authority_version: Literal['evidence-reconciliation-1'] = (
        RECONCILIATION_AUTHORITY_VERSION
    )
    decision_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    observation_ids: tuple[str, ...]
    #: Exact subject semantic hash the decision reconciled (#873).
    subject_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    #: Exact observation pins, sorted by id; must match observation_ids.
    observation_refs: tuple[ReconciliationObservationRef, ...] | None = None
    outcome: ReconciliationOutcome
    tolerance: float | None = Field(default=None, ge=0)
    tolerance_unit: UnitKind | None = None
    comparisons: tuple[ObservationComparison, ...] = ()
    rationale: str = Field(min_length=1)
    decided_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @property
    def pins_exact_inputs(self) -> bool:
        """Whether the decision carries exact input hashes (#873).

        ``False`` marks a legacy ID-only record — it was replay-verified
        at save but cannot prove today's stored inputs are the exact bytes
        it reconciled.
        """

        return (
            self.subject_sha256 is not None
            and self.observation_refs is not None
        )

    @model_validator(mode='after')
    def valid_decision(self) -> 'ReconciliationDecision':
        ids = list(self.observation_ids)
        if len(ids) != len(set(ids)):
            raise ValueError('decision observation_ids must be unique')
        if ids != sorted(ids):
            raise ValueError('decision observation_ids must be sorted')
        if self.tolerance is not None and self.tolerance_unit is None:
            raise ValueError('numeric tolerance requires tolerance_unit')
        if self.tolerance is None and self.tolerance_unit is not None:
            raise ValueError('tolerance_unit requires a numeric tolerance')
        pinned = set(self.observation_ids)
        for comparison in self.comparisons:
            if (
                comparison.left_observation_id not in pinned
                or comparison.right_observation_id not in pinned
            ):
                raise ValueError(
                    'comparison references an observation outside the '
                    'pinned set'
                )
        # #873: pin fields are all-or-nothing — a decision that pins only
        # some inputs is malformed, not partially exact.
        if (self.subject_sha256 is None) != (self.observation_refs is None):
            raise ValueError(
                'subject_sha256 and observation_refs must pin together'
            )
        if self.observation_refs is not None:
            ref_ids = [r.observation_id for r in self.observation_refs]
            if ref_ids != sorted(ref_ids) or len(ref_ids) != len(set(ref_ids)):
                raise ValueError('observation_refs must be sorted and unique')
            if tuple(ref_ids) != self.observation_ids:
                raise ValueError(
                    'observation_refs must pin every observation_id'
                )
        _validate_instant(self.decided_at_utc, 'decided_at_utc')
        if self.decision_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ReconciliationDecision hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'decision_id': self.decision_id,
            'subject_id': self.subject_id,
            'document_id': self.document_id,
            'observation_ids': list(self.observation_ids),
            'outcome': self.outcome,
            'tolerance': self.tolerance,
            'tolerance_unit': self.tolerance_unit,
            'comparisons': [
                c.model_dump(mode='json') for c in self.comparisons
            ],
            'rationale': self.rationale,
            'decided_by': self.decided_by,
            'decided_at_utc': self.decided_at_utc,
        }
        # Pin fields are part of the semantic payload only when present —
        # a legacy ID-only payload keeps its original hash (#873).
        if self.subject_sha256 is not None:
            payload['subject_sha256'] = self.subject_sha256
        if self.observation_refs is not None:
            payload['observation_refs'] = [
                r.model_dump(mode='json') for r in self.observation_refs
            ]
        return payload


def _compare_pair(
    left: EvidenceObservation,
    right: EvidenceObservation,
    tolerance: float,
    tolerance_unit: str,
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

    if left.alignment is None or right.alignment is None:
        return comparison(
            'not_comparable',
            'alignment is unresolved for at least one observation',
        )
    if not left.alignment.comparable_to(right.alignment):
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
    assert left.unit is not None and right.unit is not None
    try:
        right_value = convert_unit(right.value, right.unit, left.unit)
        bound_unit = convert_unit(tolerance, tolerance_unit, left.unit)
        right_uncertainty = (
            convert_unit(right.uncertainty, right.unit, left.unit)
            if right.uncertainty is not None
            else None
        )
    except ValueError:
        return comparison(
            'not_comparable',
            'observation or tolerance units are not in the same quantity '
            'family',
        )
    delta = abs(left.value - right_value)
    if left.uncertainty is None or right_uncertainty is None:
        return comparison(
            'unknown',
            'an observation uncertainty was never quantified',
            delta=delta,
        )
    bound = bound_unit + left.uncertainty + right_uncertainty
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
    tolerance: float | None = None,
    tolerance_unit: UnitKind | None = None,
    decided_at_utc: str,
    decided_by: str = 'system',
    decision_id: str | None = None,
) -> ReconciliationDecision:
    """Reconcile every pair of observations for one subject.

    Numeric comparison requires an explicit tolerance unit matching the
    observation quantity family — a bare float can never bound a measured
    quantity. The subject outcome is strict: any unresolved alignment makes
    the subject ``not_comparable``; else any conflict makes it ``conflict``;
    else any undecidable pair makes it ``unknown``; only a fully comparable,
    fully consistent set is ``consistent``.
    """

    subject_observations = tuple(
        sorted(
            (
                o
                for o in observations
                if o.subject_id == subject.subject_id
            ),
            key=lambda o: o.observation_id,
        )
    )
    numeric = [
        o for o in subject_observations if o.value is not None
    ]
    if len(numeric) >= 2 and tolerance_unit is None:
        raise ValueError(
            'reconciling numeric observations requires tolerance_unit'
        )
    pair_tolerance = tolerance if tolerance is not None else 0.0
    comparisons: list[ObservationComparison] = []
    for index, left in enumerate(subject_observations):
        for right in subject_observations[index + 1 :]:
            comparisons.append(
                _compare_pair(
                    left,
                    right,
                    pair_tolerance,
                    tolerance_unit or 'dimensionless',
                )
            )
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
        'observation_ids': tuple(
            sorted(o.observation_id for o in subject_observations)
        ),
        # #873: pin the exact input semantic hashes so the persisted
        # decision can later prove which bytes it reconciled.
        'subject_sha256': (
            subject.subject_sha256 or evidence_subject_sha256(subject)
        ),
        'observation_refs': tuple(
            ReconciliationObservationRef(
                observation_id=o.observation_id,
                observation_sha256=(
                    o.observation_sha256 or evidence_observation_sha256(o)
                ),
            )
            for o in subject_observations
        ),
        'outcome': outcome,
        'tolerance': tolerance,
        'tolerance_unit': tolerance_unit,
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
    'AlignmentFrameKind',
    'AlignmentRef',
    'EvidenceObservation',
    'EvidenceSubject',
    'ObservationComparison',
    'ObservationSourceKind',
    'ReconciliationDecision',
    'ReconciliationObservationRef',
    'ReconciliationOutcome',
    'SubjectKind',
    'build_evidence_subject',
    'build_observation',
    'evidence_observation_sha256',
    'evidence_subject_sha256',
    'reconcile_subject',
]
