"""Explicit selected-alternative design decisions (#654).

HTDT preserves the *alternatives* (``DesignComparisonSet`` #447), the
*evidence context* (``AnalysisStudy`` #594) and the *state snapshot*
(``ProjectDesignCheckpoint`` #619), but none of them records the bounded
transition "the user reviewed A/B/C and explicitly chose B for this design
stage". A :class:`DesignDecisionRecord` is that record: an immutable
manifest of exact authority references plus the human rationale, accepted
tradeoffs and lifecycle scope of the choice.

Authority boundary (per the issue contract):

- a decision is created only by explicit user action — never derived from
  Pareto ordering, objective values, current selection or import;
- recording a decision performs no state change: applying the selected
  alternative still runs through the canonical SceneRevision /
  SystemVariant / as-built / calibration lifecycle;
- rationale and tradeoffs stay human notes — they never become measured or
  verified evidence, and accepted assumptions stay assumptions (#620);
- a later decision *supersedes* an earlier one; it never rewrites it.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


DECISION_SCHEMA_VERSION = 1
DECISION_AUTHORITY_VERSION = 'design-decision-record-1'

#: What the decision means — a selected direction is not yet an applied or
#: physically installed state.
DecisionScope = Literal[
    'exploratory_preference',
    'design_direction',
    'installation_planning',
    'installation_concession',
    'calibration_settings_choice',
    'equipment_substitution',
    'commissioning_acceptance',
    'custom',
]
DECISION_SCOPES: frozenset[str] = frozenset(
    {
        'exploratory_preference',
        'design_direction',
        'installation_planning',
        'installation_concession',
        'calibration_settings_choice',
        'equipment_substitution',
        'commissioning_acceptance',
        'custom',
    }
)

#: Which canonical lifecycle the choice intends to feed. The decision
#: record references the resulting action; it never performs it.
DecisionLifecycleIntent = Literal[
    'choose_for_design',
    'apply_to_project_design',
    'choose_for_installation',
    'accept_as_built_deviation',
    'accept_commissioning_limitation',
]
DECISION_LIFECYCLE_INTENTS: frozenset[str] = frozenset(
    {
        'choose_for_design',
        'apply_to_project_design',
        'choose_for_installation',
        'accept_as_built_deviation',
        'accept_commissioning_limitation',
    }
)

#: Structured engineering-rationale tags — descriptive metadata for later
#: filtering, never a score or ranking input.
DecisionRationaleTag = Literal[
    'acoustic_performance',
    'robustness',
    'measured_evidence',
    'installation_feasibility',
    'operational_clearance',
    'cost',
    'equipment_availability',
    'visual_room_preference',
    'seat_user_preference',
    'video',
    'complexity',
    'accepted_unknown',
    'other',
]
DECISION_RATIONALE_TAGS: frozenset[str] = frozenset(
    {
        'acoustic_performance',
        'robustness',
        'measured_evidence',
        'installation_feasibility',
        'operational_clearance',
        'cost',
        'equipment_availability',
        'visual_room_preference',
        'seat_user_preference',
        'video',
        'complexity',
        'accepted_unknown',
        'other',
    }
)

#: Kinds of exact authority a decision can reference.
DecisionRefKind = Literal[
    'scene_revision',
    'system_variant',
    'design_comparison_set',
    'comparison_alternative',
    'design_checkpoint',
    'analysis_study',
    'intervention_study_spec',
    'intervention_alternative',
    'assumption_decision',
    'evidence_gap',
    'measurement_plan',
    'calibration_plan',
    'installed_state',
    'commissioning_record',
    'other',
]
DECISION_REF_KINDS: frozenset[str] = frozenset(
    {
        'scene_revision',
        'system_variant',
        'design_comparison_set',
        'comparison_alternative',
        'design_checkpoint',
        'analysis_study',
        'intervention_study_spec',
        'intervention_alternative',
        'assumption_decision',
        'evidence_gap',
        'measurement_plan',
        'calibration_plan',
        'installed_state',
        'commissioning_record',
        'other',
    }
)






class DecisionAuthorityRef(BaseModel):
    """Exact typed reference to one authority a decision is bound to.

    ``ref_sha256`` is required whenever the referenced authority carries a
    semantic hash; id-only authorities may omit it.
    """

    model_config = ConfigDict(frozen=True)

    kind: DecisionRefKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)


class DesignDecisionRecord(BaseModel):
    """Immutable record of one explicit selected-alternative decision.

    A manifest of references plus human rationale — never a copy of result
    arrays, never an automatic winner, and never an implicit state change.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = DECISION_SCHEMA_VERSION
    authority_version: Literal['design-decision-record-1'] = (
        DECISION_AUTHORITY_VERSION
    )
    decision_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    decision_scope: DecisionScope
    custom_scope_label: str | None = Field(default=None, min_length=1)
    lifecycle_intent: DecisionLifecycleIntent
    selected_ref: DecisionAuthorityRef
    considered_refs: tuple[DecisionAuthorityRef, ...] = ()
    comparison_set_ref: DecisionAuthorityRef | None = None
    analysis_study_refs: tuple[DecisionAuthorityRef, ...] = ()
    design_checkpoint_ref: DecisionAuthorityRef | None = None
    rationale_tags: tuple[DecisionRationaleTag, ...] = ()
    rationale_note: str | None = None
    accepted_tradeoffs: tuple[str, ...] = ()
    accepted_assumption_refs: tuple[DecisionAuthorityRef, ...] = ()
    #: Missing/incompatible/stale evidence visible at decision time —
    #: preserved as limitation context, never rewritten later.
    evidence_limitations: tuple[str, ...] = ()
    supersedes_decision_id: str | None = Field(default=None, min_length=1)
    #: The canonical action the user then ran (a new SceneRevision, a
    #: SystemVariant apply, an as-built record, ...). Optional: a decision
    #: can exist before any apply happens.
    applied_action_ref: DecisionAuthorityRef | None = None
    author: str | None = None
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_decision(self) -> 'DesignDecisionRecord':
        if self.decision_scope == 'custom' and self.custom_scope_label is None:
            raise ValueError("custom decision scope requires custom_scope_label")
        if self.decision_scope != 'custom' and self.custom_scope_label is not None:
            raise ValueError('custom_scope_label is only valid for custom scope')
        considered_keys = {
            (item.kind, item.ref_id) for item in self.considered_refs
        }
        if (self.selected_ref.kind, self.selected_ref.ref_id) not in considered_keys:
            raise ValueError('selected_ref must be one of considered_refs')
        if len(considered_keys) != len(self.considered_refs):
            raise ValueError('considered_refs must be unique per kind/ref')
        if self.comparison_set_ref is not None and self.comparison_set_ref.kind not in {
            'design_comparison_set',
            'other',
        }:
            raise ValueError('comparison_set_ref must reference a comparison set')
        if self.design_checkpoint_ref is not None and self.design_checkpoint_ref.kind not in {
            'design_checkpoint',
            'other',
        }:
            raise ValueError('design_checkpoint_ref must reference a design checkpoint')
        for item in self.analysis_study_refs:
            if item.kind not in {'analysis_study', 'other'}:
                raise ValueError('analysis_study_refs must reference analysis studies')
        for item in self.accepted_assumption_refs:
            if item.kind not in {'assumption_decision', 'other'}:
                raise ValueError(
                    'accepted_assumption_refs must reference assumption decisions'
                )
        rationale_set = set(self.rationale_tags)
        if len(rationale_set) != len(self.rationale_tags):
            raise ValueError('rationale_tags must be unique')
        if self.decision_sha256 != _hash(self.semantic_payload()):
            raise ValueError('DesignDecisionRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'decision_id': self.decision_id,
            'document_id': self.document_id,
            'title': self.title,
            'created_at_utc': self.created_at_utc,
            'decision_scope': self.decision_scope,
            'custom_scope_label': self.custom_scope_label,
            'lifecycle_intent': self.lifecycle_intent,
            'selected_ref': self.selected_ref.model_dump(mode='json'),
            'considered_refs': [
                item.model_dump(mode='json') for item in self.considered_refs
            ],
            'comparison_set_ref': (
                None
                if self.comparison_set_ref is None
                else self.comparison_set_ref.model_dump(mode='json')
            ),
            'analysis_study_refs': [
                item.model_dump(mode='json') for item in self.analysis_study_refs
            ],
            'design_checkpoint_ref': (
                None
                if self.design_checkpoint_ref is None
                else self.design_checkpoint_ref.model_dump(mode='json')
            ),
            'rationale_tags': list(self.rationale_tags),
            'rationale_note': self.rationale_note,
            'accepted_tradeoffs': list(self.accepted_tradeoffs),
            'accepted_assumption_refs': [
                item.model_dump(mode='json') for item in self.accepted_assumption_refs
            ],
            'evidence_limitations': list(self.evidence_limitations),
            'supersedes_decision_id': self.supersedes_decision_id,
            'applied_action_ref': (
                None
                if self.applied_action_ref is None
                else self.applied_action_ref.model_dump(mode='json')
            ),
            'author': self.author,
        }


def build_decision_record(
    *,
    document_id: str,
    title: str,
    decision_scope: DecisionScope,
    lifecycle_intent: DecisionLifecycleIntent,
    selected_ref: DecisionAuthorityRef,
    considered_refs: tuple[DecisionAuthorityRef, ...],
    created_at_utc: str,
    comparison_set_ref: DecisionAuthorityRef | None = None,
    analysis_study_refs: tuple[DecisionAuthorityRef, ...] = (),
    design_checkpoint_ref: DecisionAuthorityRef | None = None,
    rationale_tags: tuple[DecisionRationaleTag, ...] = (),
    rationale_note: str | None = None,
    accepted_tradeoffs: tuple[str, ...] = (),
    accepted_assumption_refs: tuple[DecisionAuthorityRef, ...] = (),
    evidence_limitations: tuple[str, ...] = (),
    supersedes: DesignDecisionRecord | None = None,
    applied_action_ref: DecisionAuthorityRef | None = None,
    custom_scope_label: str | None = None,
    author: str | None = None,
    decision_id: str | None = None,
) -> DesignDecisionRecord:
    """Record an explicit user decision; superseding links, never rewrites."""

    if supersedes is not None and supersedes.document_id != document_id:
        raise ValueError('superseded decision belongs to another document')
    payload: dict[str, Any] = {
        'decision_id': decision_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'created_at_utc': created_at_utc,
        'decision_scope': decision_scope,
        'custom_scope_label': custom_scope_label,
        'lifecycle_intent': lifecycle_intent,
        'selected_ref': selected_ref,
        'considered_refs': tuple(considered_refs),
        'comparison_set_ref': comparison_set_ref,
        'analysis_study_refs': tuple(analysis_study_refs),
        'design_checkpoint_ref': design_checkpoint_ref,
        'rationale_tags': tuple(rationale_tags),
        'rationale_note': rationale_note,
        'accepted_tradeoffs': tuple(accepted_tradeoffs),
        'accepted_assumption_refs': tuple(accepted_assumption_refs),
        'evidence_limitations': tuple(evidence_limitations),
        'supersedes_decision_id': (
            None if supersedes is None else supersedes.decision_id
        ),
        'applied_action_ref': applied_action_ref,
        'author': author,
    }
    provisional = DesignDecisionRecord.model_construct(
        **payload, decision_sha256='0' * 64
    )
    return DesignDecisionRecord(
        **payload,
        decision_sha256=_hash(provisional.semantic_payload()),
    )


class DesignDecisionIntegrityError(ValueError):
    """Persisted decision lineage is corrupt (fork, cycle, dangling ref)."""


def decision_lineage_issues(
    decisions: tuple[DesignDecisionRecord, ...],
) -> tuple[str, ...]:
    """Detect topology violations in a decision set (#868).

    Supersession is a single-head lineage: one predecessor has at most one
    successor. Independent roots (records that supersede nothing) are
    allowed — the violations are a predecessor superseded twice, a
    supersession pointing at a missing/foreign/self record, and cycles.
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
        elif target.document_id != item.document_id:
            issues.append(
                f'decision {item.decision_id} supersedes a decision from '
                'another document'
            )
    for predecessor, children in successors.items():
        if len(children) > 1:
            issues.append(
                f'decision {predecessor} has multiple successors '
                f'({len(children)}): forked lineage'
            )
    # Cycle walk: follow supersedes edges; a revisit means the chain loops.
    for item in decisions:
        seen: set[str] = set()
        cursor: DesignDecisionRecord | None = item
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


def current_decisions(
    decisions: tuple[DesignDecisionRecord, ...],
) -> tuple[DesignDecisionRecord, ...]:
    """Project the decision head(s): drop records superseded by a later one.

    Fails closed on corrupt lineage (#868): a forked, cyclic or dangling
    supersession graph cannot define a trustworthy current head, so the
    projection raises :class:`DesignDecisionIntegrityError` instead of
    silently returning multiple heads.

    A superseded decision stays persisted and inspectable — this is only the
    "active selected decision" convenience projection, and overlapping
    scopes are never collapsed by timestamp alone.
    """

    issues = decision_lineage_issues(decisions)
    if issues:
        raise DesignDecisionIntegrityError(
            'design decision lineage is corrupt: ' + '; '.join(issues)
        )
    superseded = {
        item.supersedes_decision_id
        for item in decisions
        if item.supersedes_decision_id is not None
    }
    return tuple(item for item in decisions if item.decision_id not in superseded)


__all__ = [
    'DECISION_AUTHORITY_VERSION',
    'DECISION_LIFECYCLE_INTENTS',
    'DECISION_RATIONALE_TAGS',
    'DECISION_REF_KINDS',
    'DECISION_SCHEMA_VERSION',
    'DECISION_SCOPES',
    'DecisionAuthorityRef',
    'DecisionLifecycleIntent',
    'DecisionRationaleTag',
    'DecisionRefKind',
    'DecisionScope',
    'DesignDecisionIntegrityError',
    'DesignDecisionRecord',
    'build_decision_record',
    'current_decisions',
    'decision_lineage_issues',
]
