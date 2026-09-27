"""Scoped assumption-acceptance decisions (#620).

When a project must proceed on an assumption — "treat the wall substrate as
concrete until capture evidence arrives" — the user records an
:class:`AssumptionDecision`. It is an explicit, immutable authority record:
typed subject, the assumed value or behavior, a bounded scope, rationale,
and the provenance behind the decision.

Contract:

- an assumption decision *annotates* the register — a covered gap is
  reclassified ``user_attested`` but stays visible; the record never turns
  an assumed value into measured evidence;
- scope is explicit (a checkpoint, a study, the project, a commissioning
  run, or a custom scope) and bound to exact references;
- a changed assumption is a new decision that *supersedes* the old one —
  revalidation (#561) observes the supersede edge, never an edit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


ASSUMPTION_DECISION_SCHEMA_VERSION = 1
ASSUMPTION_DECISION_AUTHORITY_VERSION = 'assumption-decision-1'

#: The bounded scope an assumption is accepted for.
AssumptionScope = Literal[
    'design_checkpoint',
    'analysis_study',
    'project',
    'commissioning_run',
    'custom',
]
ASSUMPTION_SCOPES: frozenset[str] = frozenset(
    {
        'design_checkpoint',
        'analysis_study',
        'project',
        'commissioning_run',
        'custom',
    }
)

#: Which register classification the decision attests — an assumption can
#: only cover non-measured states, never claimed evidence.
ATTESTABLE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {'unknown', 'missing_evidence', 'assumed', 'inferred', 'unverified'}
)


class AssumptionSubjectRef(BaseModel):
    """Exact typed pointer the assumption is about."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)






class AssumptionDecision(BaseModel):
    """Immutable record of an explicit, scoped assumption acceptance."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ASSUMPTION_DECISION_SCHEMA_VERSION
    authority_version: Literal['assumption-decision-1'] = (
        ASSUMPTION_DECISION_AUTHORITY_VERSION
    )
    decision_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    subject: AssumptionSubjectRef
    #: Register classification the decision covers — must be attestable.
    attested_classification: str = Field(min_length=1)
    #: The value or behavior the project proceeds on, in human prose.
    assumed_value: str = Field(min_length=1)
    decision_scope: AssumptionScope
    custom_scope_label: str | None = Field(default=None, min_length=1)
    scope_ref: AssumptionSubjectRef | None = None
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[AssumptionSubjectRef, ...] = ()
    expires_at_utc: str | None = None
    supersedes_decision_id: str | None = Field(default=None, min_length=1)
    author: str | None = None
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_decision(self) -> 'AssumptionDecision':
        if self.attested_classification not in ATTESTABLE_CLASSIFICATIONS:
            raise ValueError(
                'attested_classification must be an attestable register '
                'classification'
            )
        if self.decision_scope == 'custom' and self.custom_scope_label is None:
            raise ValueError('custom scope requires custom_scope_label')
        if self.decision_scope != 'custom' and self.custom_scope_label is not None:
            raise ValueError('custom_scope_label is only valid for custom scope')
        _validate_instant(self.created_at_utc, 'created_at_utc')
        if self.expires_at_utc is not None:
            _validate_instant(self.expires_at_utc, 'expires_at_utc')
            if _parse_instant(self.expires_at_utc) < _parse_instant(
                self.created_at_utc
            ):
                raise ValueError('expires_at_utc is before created_at_utc')
        if self.decision_sha256 != _hash(self.semantic_payload()):
            raise ValueError('AssumptionDecision hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'decision_id': self.decision_id,
            'document_id': self.document_id,
            'created_at_utc': self.created_at_utc,
            'subject': self.subject.model_dump(mode='json'),
            'attested_classification': self.attested_classification,
            'assumed_value': self.assumed_value,
            'decision_scope': self.decision_scope,
            'custom_scope_label': self.custom_scope_label,
            'scope_ref': (
                None
                if self.scope_ref is None
                else self.scope_ref.model_dump(mode='json')
            ),
            'rationale': self.rationale,
            'evidence_refs': [
                ref.model_dump(mode='json') for ref in self.evidence_refs
            ],
            'expires_at_utc': self.expires_at_utc,
            'supersedes_decision_id': self.supersedes_decision_id,
            'author': self.author,
        }


def build_assumption_decision(
    *,
    document_id: str,
    subject: AssumptionSubjectRef,
    attested_classification: str,
    assumed_value: str,
    decision_scope: AssumptionScope,
    rationale: str,
    created_at_utc: str,
    custom_scope_label: str | None = None,
    scope_ref: AssumptionSubjectRef | None = None,
    evidence_refs: tuple[AssumptionSubjectRef, ...] = (),
    expires_at_utc: str | None = None,
    supersedes: AssumptionDecision | None = None,
    author: str | None = None,
    decision_id: str | None = None,
) -> AssumptionDecision:
    """Record a scoped assumption acceptance; supersedes links, never edits."""

    if supersedes is not None:
        if supersedes.document_id != document_id:
            raise ValueError('superseded decision belongs to another document')
        if supersedes.subject != subject:
            raise ValueError(
                'superseded decision must cover the same subject'
            )
    payload: dict[str, Any] = {
        'decision_id': decision_id or str(uuid4()),
        'document_id': document_id,
        'created_at_utc': created_at_utc,
        'subject': subject,
        'attested_classification': attested_classification,
        'assumed_value': assumed_value,
        'decision_scope': decision_scope,
        'custom_scope_label': custom_scope_label,
        'scope_ref': scope_ref,
        'rationale': rationale,
        'evidence_refs': tuple(evidence_refs),
        'expires_at_utc': expires_at_utc,
        'supersedes_decision_id': (
            None if supersedes is None else supersedes.decision_id
        ),
        'author': author,
    }
    provisional = AssumptionDecision.model_construct(
        **payload, decision_sha256='0' * 64
    )
    return AssumptionDecision(
        **payload,
        decision_sha256=_hash(provisional.semantic_payload()),
    )


def _validate_instant(value: str, field: str) -> None:
    """Enforce strict, explicitly-UTC ISO 8601 persisted timestamps (#869).

    Lexical quirks (naive strings, partial dates, non-UTC offsets) are
    rejected so they can never rewrite decision semantics silently.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} is not a valid ISO 8601 instant') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f'{field} must carry an explicit UTC offset')


def _parse_instant(value: str) -> datetime:
    """Normalize a persisted UTC timestamp into a comparable instant.

    ``Z`` suffixes become explicit offsets; naive timestamps are treated as
    UTC (the project's persisted-UTC convention) so lexical quirks can
    never extend an expiry silently.
    """

    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AssumptionDecisionIntegrityError(ValueError):
    """Persisted assumption lineage is corrupt (fork, cycle, dangling ref)."""


def assumption_lineage_issues(
    decisions: tuple[AssumptionDecision, ...],
) -> tuple[str, ...]:
    """Detect topology violations in an assumption decision set (#869).

    Supersession is a single-head lineage within one subject: a fork, a
    cycle, a dangling predecessor, or a cross-subject edge makes the
    recorded history untrustworthy and must fail closed.
    """

    issues: list[str] = []
    by_id = {item.decision_id: item for item in decisions}
    successors: dict[str, list[str]] = {}
    for item in decisions:
        predecessor = item.supersedes_decision_id
        if predecessor is None:
            continue
        successors.setdefault(predecessor, []).append(item.decision_id)
        target = by_id.get(predecessor)
        if predecessor == item.decision_id:
            issues.append(f'decision {item.decision_id} supersedes itself')
        elif target is None:
            issues.append(
                f'decision {item.decision_id} supersedes missing '
                f'predecessor {predecessor}'
            )
        elif target.subject != item.subject:
            issues.append(
                f'decision {item.decision_id} supersedes a decision on a '
                'different subject'
            )
    for predecessor_id, children in successors.items():
        if len(children) > 1:
            issues.append(
                f'decision {predecessor_id} has multiple successors '
                f'({len(children)}): forked lineage'
            )
    for item in decisions:
        seen: set[str] = set()
        cursor: AssumptionDecision | None = item
        while cursor is not None and cursor.supersedes_decision_id is not None:
            predecessor_id = cursor.supersedes_decision_id
            if predecessor_id in seen:
                issues.append(
                    f'decision {item.decision_id} reaches a supersession '
                    'cycle'
                )
                break
            seen.add(predecessor_id)
            cursor = by_id.get(predecessor_id)
    return tuple(issues)


def active_assumption_decisions(
    decisions: tuple[AssumptionDecision, ...],
    *,
    as_of_utc: str | None = None,
) -> tuple[AssumptionDecision, ...]:
    """Project the live decisions: drop superseded and expired records.

    Expiry is exclusive and evaluated on parsed instants: when
    ``as_of_utc`` is ``None`` no time filter applies (the caller owns
    current-vs-historical evaluation); a decision expiring exactly at
    ``as_of_utc`` is already inactive.
    """

    issues = assumption_lineage_issues(decisions)
    if issues:
        raise AssumptionDecisionIntegrityError(
            'assumption decision lineage is corrupt: ' + '; '.join(issues)
        )
    superseded = {
        item.supersedes_decision_id
        for item in decisions
        if item.supersedes_decision_id is not None
    }
    as_of = _parse_instant(as_of_utc) if as_of_utc is not None else None
    return tuple(
        item
        for item in decisions
        if item.decision_id not in superseded
        and (
            as_of is None
            or item.expires_at_utc is None
            or _parse_instant(item.expires_at_utc) > as_of
        )
    )


__all__ = [
    'ASSUMPTION_DECISION_AUTHORITY_VERSION',
    'ASSUMPTION_DECISION_SCHEMA_VERSION',
    'ASSUMPTION_SCOPES',
    'ATTESTABLE_CLASSIFICATIONS',
    'AssumptionDecision',
    'AssumptionScope',
    'AssumptionSubjectRef',
    'AssumptionDecisionIntegrityError',
    'active_assumption_decisions',
    'assumption_lineage_issues',
    'build_assumption_decision',
]
