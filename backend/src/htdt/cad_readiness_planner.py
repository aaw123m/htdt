"""Next Best Action / Readiness Planner (#724).

Deterministic orchestration layer that answers "given the exact current
project state, what should I do next, and what does it unlock?" — as a
persistent, reproducible read model, not an LLM recommendation engine and
not a new source of truth.

Contract properties (per the issue):

- deterministic: identical signals + intent produce the identical ordered
  action set — action ids are content-derived, never random;
- explainable: every action carries a human-readable ``reason``, the exact
  ``deep_link`` route to act on, and the ``unlocks`` capability codes it
  enables; ordering follows the fixed ranking policy documented on
  :func:`plan_next_actions`, with no hidden composite score;
- goal-sensitive: ``intent`` reorders/filters actions without changing
  domain truth — "document existing theater" never forces optimization,
  "commission installed system" elevates as-built/verification evidence;
- conservative: UNKNOWN signals stay unknown (``None`` counts are never
  treated as zero), stale derived results recommend *recompute* (never
  automatic remeasurement), and owned-room validation gaps are explicit.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator


READINESS_PLANNER_SCHEMA_VERSION = 1


def _action_id(document_id: str, action_kind: str, deep_link: str) -> str:
    """Deterministic action id — same state always yields the same id."""
    digest = sha256(
        json.dumps(
            {
                'document_id': document_id,
                'action_kind': action_kind,
                'deep_link': deep_link,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    return f'readiness-action:{digest[:32]}'


ProjectIntent = Literal[
    'document_existing',
    'diagnose_bass',
    'plan_expansion',
    'commission_installed',
    'general',
]

ReadinessUrgency = Literal['blocker', 'recommended', 'optional']
ActionScope = Literal['current', 'historical']
EffortClass = Literal['small', 'medium', 'large']


class ReadinessSignals(BaseModel):
    """Distilled project state the caller supplies from exact authorities.

    ``None``/``0`` semantics matter: ``measurement_count=None`` means the
    count is UNKNOWN (never treated as zero); boolean fields are tri-state
    where ``None`` means "authority absent/unknown".
    """

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    scene_exists: bool = False
    equipment_inventory_exists: bool = False
    source_directivity_bound: bool | None = None
    routing_resolved: bool | None = None
    measurement_plan_exists: bool = False
    #: ``None`` = unknown; callers must not substitute 0 for "not counted".
    measurement_count: int | None = None
    holdout_measurement_count: int | None = None
    stale_derived_results: int = 0
    calibration_plan_exists: bool = False
    applied_settings_verified: bool | None = None
    #: Owned-room validation eligibility as declared by the validation
    #: scope authority — this planner never decides it, only references it.
    owned_room_validation_eligible: bool | None = None
    system_variant_exists: bool = False


class ReadinessAction(BaseModel):
    """One concrete, deep-linkable next action."""

    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    action_kind: str = Field(min_length=1)
    title: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    deep_link: str = Field(min_length=1)
    unlocks: tuple[str, ...] = ()
    urgency: ReadinessUrgency
    scope: ActionScope = 'current'
    effort_class: EffortClass | None = None
    prerequisite_refs: tuple[str, ...] = ()
    blocking_reason_codes: tuple[str, ...] = ()
    #: Which intents elevate this action to goal-critical.
    goal_intents: tuple[ProjectIntent, ...] = ()


class ReadinessPlan(BaseModel):
    """The ordered action set for one document under one intent."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    intent: ProjectIntent
    generated_at_utc: str = Field(min_length=1)
    actions: tuple[ReadinessAction, ...]


# ----------------------------------------------------------------------
# Rules

class _Rule(NamedTuple):
    """One deterministic rule: fires when its signals hold."""

    action_kind: str
    title: str
    reason: str
    deep_link: str
    unlocks: tuple[str, ...]
    urgency: ReadinessUrgency
    effort_class: EffortClass | None
    goal_intents: tuple[ProjectIntent, ...]
    blocking_reason_codes: tuple[str, ...]


def _rule_fires(
    rule: _Rule, signals: ReadinessSignals
) -> ReadinessAction | None:
    return ReadinessAction(
        action_id=_action_id(
            signals.document_id, rule.action_kind, rule.deep_link
        ),
        document_id=signals.document_id,
        action_kind=rule.action_kind,
        title=rule.title,
        reason=rule.reason,
        deep_link=rule.deep_link,
        unlocks=rule.unlocks,
        urgency=rule.urgency,
        effort_class=rule.effort_class,
        prerequisite_refs=(),
        blocking_reason_codes=rule.blocking_reason_codes,
        goal_intents=rule.goal_intents,
    )


def _rule_actions(signals: ReadinessSignals) -> list[ReadinessAction]:
    """Evaluate every rule in fixed declaration order."""
    actions: list[ReadinessAction] = []
    rules = (
        # 1. Hard blockers first — nothing downstream works without these.
        _Rule(
            action_kind='capture_room_geometry',
            title='Capture the room geometry',
            reason='no persisted SceneRevision exists — every downstream '
            'authority depends on the as-built geometry',
            deep_link='cad.geometry',
            unlocks=('scene_model', 'prediction', 'measurement_planning'),
            urgency='blocker',
            effort_class='large',
            goal_intents=(
                'document_existing',
                'diagnose_bass',
                'plan_expansion',
                'commission_installed',
                'general',
            ),
            blocking_reason_codes=('scene_missing',),
        ),
        _Rule(
            action_kind='record_equipment_inventory',
            title='Record the installed equipment inventory',
            reason='no equipment inventory exists — routing, calibration '
            'and validation all bind to installed instances',
            deep_link='cad.equipment',
            unlocks=('routing', 'calibration', 'device_adapter_binding'),
            urgency='blocker',
            effort_class='medium',
            goal_intents=(
                'commission_installed',
                'diagnose_bass',
                'general',
            ),
            blocking_reason_codes=('equipment_inventory_missing',),
        ),
        _Rule(
            action_kind='resolve_avr_routing',
            title='Resolve the current AVR routing',
            reason='the installed routing is unresolved — calibration '
            'verification cannot be checked against it',
            deep_link='cad.routing',
            unlocks=('calibration_verification', 'applied_settings'),
            urgency='blocker',
            effort_class='small',
            goal_intents=('commission_installed', 'diagnose_bass'),
            blocking_reason_codes=('routing_unresolved',),
        ),
        # 2. Recompute before remeasurement — stale derived results are a
        # cheaper fix than re-acquiring evidence.
        _Rule(
            action_kind='recompute_stale_results',
            title='Recompute stale derived results',
            reason='derived authorities are stale — recomputation restores '
            'current views without new measurements',
            deep_link='cad.derived',
            unlocks=('current_derived_views',),
            urgency='recommended',
            effort_class='small',
            goal_intents=(
                'document_existing',
                'diagnose_bass',
                'plan_expansion',
                'commission_installed',
                'general',
            ),
            blocking_reason_codes=(),
        ),
        # 3. Evidence/acquisition enrichment, goal-ordered.
        _Rule(
            action_kind='bind_source_directivity',
            title='Bind exact source/directivity authority for the speakers',
            reason='source directivity is not bound — directional '
            'prediction and aim optimization cannot run on exact data',
            deep_link='cad.sources',
            unlocks=('directional_prediction', 'aim_optimization'),
            urgency='recommended',
            effort_class='medium',
            goal_intents=('diagnose_bass', 'plan_expansion', 'general'),
            blocking_reason_codes=(),
        ),
        _Rule(
            action_kind='plan_measurement_campaign',
            title='Plan the measurement campaign',
            reason='no measurement plan exists — acquisition cannot be '
            'scheduled or validated',
            deep_link='cad.measurement_plan',
            unlocks=('measurement_acquisition', 'validation_scope'),
            urgency='recommended',
            effort_class='medium',
            goal_intents=(
                'diagnose_bass',
                'commission_installed',
                'general',
            ),
            blocking_reason_codes=(),
        ),
        _Rule(
            action_kind='add_holdout_measurements',
            title='Add holdout measurements around the MLP',
            reason='owned-room validation eligibility requires holdout '
            'evidence independent of the tuning set',
            deep_link='cad.measurements',
            unlocks=('owned_room_validation_eligibility',),
            urgency='recommended',
            effort_class='medium',
            goal_intents=('commission_installed',),
            blocking_reason_codes=('holdout_evidence_missing',),
        ),
        _Rule(
            action_kind='verify_applied_settings',
            title='Verify applied settings read-back',
            reason='a calibration plan exists but applied settings have '
            'not been verified — read-back proves the device state',
            deep_link='cad.applied_settings',
            unlocks=('applied_settings_verified',),
            urgency='recommended',
            effort_class='small',
            goal_intents=('commission_installed',),
            blocking_reason_codes=(),
        ),
        _Rule(
            action_kind='create_system_variant',
            title='Create a SystemVariant for the proposed expansion',
            reason='expansion planning requires an exact variant bound to '
            'the baseline scene',
            deep_link='cad.variants',
            unlocks=('variant_evaluation', 'expansion_planning'),
            urgency='optional',
            effort_class='medium',
            goal_intents=('plan_expansion',),
            blocking_reason_codes=(),
        ),
    )

    if not signals.scene_exists:
        actions.append(_rule_fires(rules[0], signals))  # type: ignore[arg-type]
        # Without a scene nothing else is actionable; keep the plan short.
        return [action for action in actions if action is not None]

    if not signals.equipment_inventory_exists:
        actions.append(_rule_fires(rules[1], signals))  # type: ignore[arg-type]
    if signals.routing_resolved is False:
        actions.append(_rule_fires(rules[2], signals))  # type: ignore[arg-type]
    if signals.stale_derived_results > 0:
        actions.append(_rule_fires(rules[3], signals))  # type: ignore[arg-type]
    if signals.source_directivity_bound is False:
        actions.append(_rule_fires(rules[4], signals))  # type: ignore[arg-type]
    if (
        signals.measurement_count is not None
        and signals.measurement_count == 0
        and not signals.measurement_plan_exists
    ):
        actions.append(_rule_fires(rules[5], signals))  # type: ignore[arg-type]
    if (
        signals.owned_room_validation_eligible is False
        and signals.holdout_measurement_count is not None
        and signals.holdout_measurement_count < 3
    ):
        actions.append(_rule_fires(rules[6], signals))  # type: ignore[arg-type]
    if (
        signals.calibration_plan_exists
        and signals.applied_settings_verified is False
    ):
        actions.append(_rule_fires(rules[7], signals))  # type: ignore[arg-type]
    if not signals.system_variant_exists:
        actions.append(_rule_fires(rules[8], signals))  # type: ignore[arg-type]

    return [action for action in actions if action is not None]


def plan_next_actions(
    signals: ReadinessSignals,
    *,
    intent: ProjectIntent = 'general',
    generated_at_utc: str,
) -> ReadinessPlan:
    """Derive the ordered action set — deterministic and explainable.

    Ordering policy (stable, reproducible):

    1. ``blocker`` before ``recommended`` before ``optional``;
    2. within a tier, actions matching the active ``intent`` rank first;
    3. ties keep fixed rule-declaration order (recompute precedes
       acquisition; evidence acquisition precedes irreversible work).
    """

    actions = _rule_actions(signals)
    urgency_rank = {'blocker': 0, 'recommended': 1, 'optional': 2}

    def sort_key(index_action: tuple[int, ReadinessAction]) -> tuple[int, int, int]:
        index, action = index_action
        goal_match = 0 if intent in action.goal_intents else 1
        return (urgency_rank[action.urgency], goal_match, index)

    ordered = tuple(
        action
        for _index, action in sorted(
            enumerate(actions), key=sort_key
        )
    )
    return ReadinessPlan(
        document_id=signals.document_id,
        intent=intent,
        generated_at_utc=generated_at_utc,
        actions=ordered,
    )


__all__ = [
    'ActionScope',
    'EffortClass',
    'ProjectIntent',
    'READINESS_PLANNER_SCHEMA_VERSION',
    'ReadinessAction',
    'ReadinessPlan',
    'ReadinessSignals',
    'ReadinessUrgency',
    'plan_next_actions',
]
