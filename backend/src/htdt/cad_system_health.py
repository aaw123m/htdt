"""Post-commissioning health baseline and drift verification (#568).

Commissioning proves the theater was correct at one point in time. This
module answers the follow-up question — "is it still behaving like the
commissioned baseline?" — through three immutable authorities:

- :class:`SystemHealthBaseline` pins an exact accepted state: the as-built
  SceneRevision, the operating preset used, the measurement campaign and
  points, routing/calibration/settings identity, instrument context, the
  measured metric pins and the change-detection policy. It is never a moving
  pointer to "latest project state".
- :class:`HealthCheckPlan` is a bounded, repeatable check set derived from
  the baseline — deliberately smaller than full commissioning.
- :class:`HealthCheckRun` retains exact new observations plus acquisition
  context and carries a per-check :class:`HealthCheckAssessment`.

Drift semantics are per check and never collapse to one health score:
``within_baseline`` / ``changed`` / ``indeterminate`` / ``not_comparable`` /
``not_run``. Two curves are never called "drift" when acquisition context
(microphone position, room state, routing, processing, instrument) makes
them non-comparable — they become ``not_comparable``. Insufficient
repeatability evidence produces ``indeterminate``. A detected change never
assigns a physical cause; ``cause_hypothesis`` is kept as an explicit
unverified note for diagnostic follow-up.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


HEALTH_SCHEMA_VERSION = 1
HEALTH_AUTHORITY_VERSION = 'system-health-baseline-1'

HealthCheckDomain = Literal['acoustic', 'settings', 'physical', 'video', 'noise']
HealthCheckState = Literal[
    'within_baseline',
    'changed',
    'indeterminate',
    'not_comparable',
    'not_run',
]
HealthRunTrigger = Literal[
    'manual',
    'equipment_change',
    'room_change',
    'periodic_reminder',
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


class HealthAuthorityRef(BaseModel):
    """Exact reference to one authority pinned by a baseline/plan/run."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)


class ChangeDetectionPolicy(BaseModel):
    """Tolerances and evidence requirements for drift detection.

    ``min_repeatability_evidence='required'`` means a run without a
    repeatability/uncertainty pin cannot claim ``changed`` — it returns
    ``indeterminate`` instead of overstating tiny numerical differences.
    """

    model_config = ConfigDict(frozen=True)

    level_tolerance_db: float | None = Field(default=None, gt=0.0)
    position_tolerance_m: float | None = Field(default=None, gt=0.0)
    frequency_tolerance_hz: float | None = Field(default=None, gt=0.0)
    min_repeatability_evidence: Literal['required', 'preferred'] = 'required'

    @model_validator(mode='after')
    def valid_policy(self) -> 'ChangeDetectionPolicy':
        for value in (
            self.level_tolerance_db,
            self.position_tolerance_m,
            self.frequency_tolerance_hz,
        ):
            if value is not None and not isfinite(float(value)):
                raise ValueError('detection tolerances must be finite')
        return self


class HealthMetricPin(BaseModel):
    """One frozen measured metric the baseline compares future runs against.

    ``expected_repr`` is the canonical-JSON rendering of the pinned metric
    (for example ``{"value_db": 82.1}``), so the pin keeps the exact observed
    value rather than a moving pointer; ``evidence_ref`` names the authority
    the value came from.
    """

    model_config = ConfigDict(frozen=True)

    pin_id: str = Field(min_length=1)
    domain: HealthCheckDomain
    metric_key: str = Field(min_length=1)
    evidence_ref: HealthAuthorityRef
    expected_repr: str = Field(min_length=1)
    tolerance_repr: str | None = Field(default=None, min_length=1)


class SystemHealthBaseline(BaseModel):
    """Immutable freeze of the commissioned state used for drift detection."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = HEALTH_SCHEMA_VERSION
    authority_version: Literal['system-health-baseline-1'] = HEALTH_AUTHORITY_VERSION
    baseline_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    operating_preset_id: str | None = Field(default=None, min_length=1)
    operating_preset_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    source_refs: tuple[HealthAuthorityRef, ...] = ()
    instrument_refs: tuple[HealthAuthorityRef, ...] = ()
    metric_pins: tuple[HealthMetricPin, ...] = ()
    policy: ChangeDetectionPolicy = ChangeDetectionPolicy()
    created_at_utc: str = Field(min_length=1)
    baseline_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_baseline(self) -> 'SystemHealthBaseline':
        preset_pair = (
            self.operating_preset_id is not None,
            self.operating_preset_sha256 is not None,
        )
        if preset_pair[0] != preset_pair[1]:
            raise ValueError('operating preset id/hash must be supplied together')
        pin_ids = [item.pin_id for item in self.metric_pins]
        if len(pin_ids) != len(set(pin_ids)):
            raise ValueError('baseline metric pins must be unique')
        ref_keys = [
            (item.kind, item.ref_id)
            for item in self.source_refs + self.instrument_refs
        ]
        if len(ref_keys) != len(set(ref_keys)):
            raise ValueError('baseline authority refs must be unique per kind/id')
        if self.baseline_sha256 != _hash(self.semantic_payload()):
            raise ValueError('SystemHealthBaseline hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'baseline_id': self.baseline_id,
            'document_id': self.document_id,
            'name': self.name,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'operating_preset_id': self.operating_preset_id,
            'operating_preset_sha256': self.operating_preset_sha256,
            'source_refs': [item.model_dump(mode='json') for item in self.source_refs],
            'instrument_refs': [
                item.model_dump(mode='json') for item in self.instrument_refs
            ],
            'metric_pins': [item.model_dump(mode='json') for item in self.metric_pins],
            'policy': self.policy.model_dump(mode='json'),
            'created_at_utc': self.created_at_utc,
        }


class HealthCheckItem(BaseModel):
    """One bounded repeatable check in a HealthCheckPlan."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    domain: HealthCheckDomain
    description: str = Field(min_length=1)
    related_pin_ids: tuple[str, ...] = ()
    #: Context kinds that must match the baseline exactly for a new
    #: observation to be comparable (e.g. operating preset, routing,
    #: instrument). Missing/mismatched context yields ``not_comparable``.
    required_context: tuple[HealthAuthorityRef, ...] = ()

    @model_validator(mode='after')
    def valid_check(self) -> 'HealthCheckItem':
        if len(self.related_pin_ids) != len(set(self.related_pin_ids)):
            raise ValueError('check related pin ids must be unique')
        ctx_keys = [(item.kind, item.ref_id) for item in self.required_context]
        if len(ctx_keys) != len(set(ctx_keys)):
            raise ValueError('check required-context refs must be unique per kind/id')
        return self


class HealthCheckPlan(BaseModel):
    """Bounded repeatable check set derived from one baseline."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    baseline_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[HealthCheckItem, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_plan(self) -> 'HealthCheckPlan':
        check_ids = [item.check_id for item in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError('health check ids must be unique')
        if self.plan_sha256 != _hash(self.semantic_payload()):
            raise ValueError('HealthCheckPlan hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'plan_id': self.plan_id,
            'document_id': self.document_id,
            'baseline_id': self.baseline_id,
            'baseline_sha256': self.baseline_sha256,
            'checks': [item.model_dump(mode='json') for item in self.checks],
            'created_at_utc': self.created_at_utc,
        }


class HealthObservation(BaseModel):
    """One new observation captured for one check item."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    evidence_ref: HealthAuthorityRef
    #: Acquisition/operating context under which the observation was made —
    #: preset, routing, instrument, position. Compared against the check's
    #: ``required_context``; mismatches make the check not comparable.
    context_refs: tuple[HealthAuthorityRef, ...] = ()
    observed_repr: str | None = None
    #: True when the observation carries repeatability/uncertainty evidence
    #: (repeat capture, uncertainty bound). Baselines whose policy requires
    #: repeatability return ``indeterminate`` when this is absent.
    has_repeatability_evidence: bool = False


class HealthCheckAssessment(BaseModel):
    """Per-check result; the actual observation/delta is always retained."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    state: HealthCheckState
    observed_delta_repr: str | None = None
    reason: str = Field(min_length=1)


class HealthCheckRun(BaseModel):
    """Append-only result of executing one HealthCheckPlan."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    trigger: HealthRunTrigger = 'manual'
    observations: tuple[HealthObservation, ...] = ()
    assessments: tuple[HealthCheckAssessment, ...] = ()
    cause_hypothesis: str | None = None
    created_at_utc: str = Field(min_length=1)
    run_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_run(self) -> 'HealthCheckRun':
        obs_ids = [item.check_id for item in self.observations]
        if len(obs_ids) != len(set(obs_ids)):
            raise ValueError('health run observations must be unique per check')
        if self.run_sha256 != _hash(self.semantic_payload()):
            raise ValueError('HealthCheckRun hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'run_id': self.run_id,
            'document_id': self.document_id,
            'plan_id': self.plan_id,
            'plan_sha256': self.plan_sha256,
            'trigger': self.trigger,
            'observations': [item.model_dump(mode='json') for item in self.observations],
            'assessments': [item.model_dump(mode='json') for item in self.assessments],
            'cause_hypothesis': self.cause_hypothesis,
            'created_at_utc': self.created_at_utc,
        }

    def assessment_for(self, check_id: str) -> HealthCheckAssessment | None:
        for item in self.assessments:
            if item.check_id == check_id:
                return item
        return None


def build_health_baseline(
    *,
    document_id: str,
    name: str,
    scene_revision_id: str,
    scene_content_hash: str,
    operating_preset_id: str | None = None,
    operating_preset_sha256: str | None = None,
    source_refs: Sequence[HealthAuthorityRef] = (),
    instrument_refs: Sequence[HealthAuthorityRef] = (),
    metric_pins: Sequence[HealthMetricPin] = (),
    policy: ChangeDetectionPolicy | None = None,
    created_at_utc: str,
    baseline_id: str | None = None,
) -> SystemHealthBaseline:
    payload: dict[str, Any] = {
        'baseline_id': baseline_id or str(uuid4()),
        'document_id': document_id,
        'name': name,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'operating_preset_id': operating_preset_id,
        'operating_preset_sha256': operating_preset_sha256,
        'source_refs': tuple(source_refs),
        'instrument_refs': tuple(instrument_refs),
        'metric_pins': tuple(metric_pins),
        'policy': policy or ChangeDetectionPolicy(),
        'created_at_utc': created_at_utc,
    }
    provisional = SystemHealthBaseline.model_construct(
        **payload, baseline_sha256='0' * 64
    )
    return SystemHealthBaseline(
        **payload,
        baseline_sha256=_hash(provisional.semantic_payload()),
    )


def build_health_check_plan(
    baseline: SystemHealthBaseline,
    *,
    checks: Sequence[HealthCheckItem],
    created_at_utc: str,
    plan_id: str | None = None,
) -> HealthCheckPlan:
    check_items = tuple(checks)
    pin_ids = {pin.pin_id for pin in baseline.metric_pins}
    for item in check_items:
        unknown = [pin_id for pin_id in item.related_pin_ids if pin_id not in pin_ids]
        if unknown:
            raise ValueError(
                f'health check {item.check_id} references unknown baseline pins: {unknown}'
            )
    payload: dict[str, Any] = {
        'plan_id': plan_id or str(uuid4()),
        'document_id': baseline.document_id,
        'baseline_id': baseline.baseline_id,
        'baseline_sha256': baseline.baseline_sha256,
        'checks': check_items,
        'created_at_utc': created_at_utc,
    }
    provisional = HealthCheckPlan.model_construct(**payload, plan_sha256='0' * 64)
    return HealthCheckPlan(
        **payload,
        plan_sha256=_hash(provisional.semantic_payload()),
    )


def _context_matches(
    required: HealthAuthorityRef, contexts: tuple[HealthAuthorityRef, ...]
) -> bool:
    for context in contexts:
        if context.kind != required.kind:
            continue
        if context.ref_id != required.ref_id:
            continue
        if required.ref_sha256 is None or context.ref_sha256 == required.ref_sha256:
            return True
    return False


MetricUnit = Literal['db', 'hz', 'm', 'unitless']

# Typed metric keys carry their unit; a metric may never consume a
# tolerance declared for another unit (#745).
_METRIC_VALUE_KEYS: tuple[tuple[str, MetricUnit], ...] = (
    ('value_db', 'db'),
    ('level_db', 'db'),
    ('value_hz', 'hz'),
    ('frequency_hz', 'hz'),
    ('value_m', 'm'),
    ('position_m', 'm'),
)

_TOLERANCE_KEYS: tuple[tuple[str, MetricUnit], ...] = (
    ('tolerance_db', 'db'),
    ('tolerance_hz', 'hz'),
    ('tolerance_m', 'm'),
)

_POLICY_TOLERANCE_FIELD: dict[MetricUnit, str] = {
    'db': 'level_tolerance_db',
    'hz': 'frequency_tolerance_hz',
    'm': 'position_tolerance_m',
}


def _finite_number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    ):
        return float(value)
    return None


def _typed_repr_value(repr_text: str | None) -> tuple[MetricUnit, float] | None:
    """Parse one metric repr into ``(unit, value)``.

    The unit comes from the explicit typed key — never from whichever
    numeric happens to parse first (#745). Bare ``value``/scalars are
    ``unitless`` and carry no comparison semantics.
    """

    if repr_text is None:
        return None
    try:
        parsed = json.loads(repr_text)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        for key, unit in _METRIC_VALUE_KEYS:
            candidate = _finite_number(parsed.get(key))
            if candidate is not None:
                return unit, candidate
        candidate = _finite_number(parsed.get('value'))
        if candidate is not None:
            return 'unitless', candidate
        return None
    candidate = _finite_number(parsed)
    return ('unitless', candidate) if candidate is not None else None


def _pin_tolerance(
    pin: HealthMetricPin,
    policy: ChangeDetectionPolicy,
    metric_unit: MetricUnit,
) -> float | None:
    """Tolerance for one pin/metric comparison.

    A pin-specific ``tolerance_repr`` overrides the baseline policy when
    its declared unit matches the metric's unit (a bare ``tolerance`` key
    inherits the pin's own unit); the policy supplies the per-unit default
    otherwise. A tolerance declared for a different unit is incompatible
    and is never applied (#745).
    """

    if pin.tolerance_repr is not None:
        try:
            parsed = json.loads(pin.tolerance_repr)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key, unit in _TOLERANCE_KEYS:
                candidate = _finite_number(parsed.get(key))
                if (
                    candidate is not None
                    and candidate > 0
                    and unit == metric_unit
                ):
                    return candidate
            candidate = _finite_number(parsed.get('tolerance'))
            if candidate is not None and candidate > 0:
                return candidate
    field = _POLICY_TOLERANCE_FIELD.get(metric_unit)
    if field is None:
        return None
    return getattr(policy, field)


def _observation_repr_map(
    observed_repr: str | None,
    related: Sequence[HealthMetricPin],
) -> dict[str, Any] | None:
    """Explicit observation→pin mapping for multi-pin checks (#745).

    When ``observed_repr`` is a JSON object keyed by ``pin_id`` or
    ``metric_key``, each entry is that pin's own observed repr; otherwise
    a scalar repr maps only to a single-pin check — one arbitrary number
    is never compared against unrelated pins.
    """

    if observed_repr is None:
        return None
    try:
        parsed = json.loads(observed_repr)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    keys = {pin.pin_id for pin in related} | {pin.metric_key for pin in related}
    if not keys.intersection(parsed):
        return None
    return parsed


def assess_health_observation(
    baseline: SystemHealthBaseline,
    check: HealthCheckItem,
    observation: HealthObservation | None,
) -> HealthCheckAssessment:
    """Assess one check against the baseline; never invent drift or cause."""

    if observation is None:
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='not_run',
            reason='no observation was captured for this check',
        )
    missing_context = [
        ref.kind
        for ref in check.required_context
        if not _context_matches(ref, observation.context_refs)
    ]
    if missing_context:
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='not_comparable',
            reason=(
                'acquisition context differs from the baseline: '
                + ', '.join(sorted(set(missing_context)))
            ),
        )
    related = [
        pin for pin in baseline.metric_pins if pin.pin_id in check.related_pin_ids
    ]
    if not related:
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='indeterminate',
            reason='check has no pinned baseline metric to compare against',
        )
    observed_map = _observation_repr_map(observation.observed_repr, related)
    deltas: list[dict[str, Any]] = []
    saw_comparable = False
    saw_changed = False
    saw_indeterminate = False
    for pin in related:
        if observed_map is not None:
            raw = observed_map.get(pin.pin_id, observed_map.get(pin.metric_key))
            if raw is None:
                observed_typed = None
            else:
                observed_typed = _typed_repr_value(
                    raw if isinstance(raw, str) else _canonical_json(raw)
                )
        elif len(related) == 1:
            observed_typed = _typed_repr_value(observation.observed_repr)
        else:
            # Multi-pin check with a scalar observation: no explicit
            # pin mapping means this pin has no observation (#745).
            observed_typed = None
        expected_typed = _typed_repr_value(pin.expected_repr)
        if observed_typed is None or expected_typed is None:
            if (
                observed_map is None
                and len(related) == 1
                and observation.observed_repr is not None
                and observation.observed_repr == pin.expected_repr
            ):
                saw_comparable = True
                deltas.append(
                    {'pin_id': pin.pin_id, 'delta': 0.0, 'unit': None}
                )
                continue
            saw_indeterminate = True
            deltas.append(
                {
                    'pin_id': pin.pin_id,
                    'delta': None,
                    'reason': 'no comparable observation for this pin',
                }
            )
            continue
        observed_unit, observed = observed_typed
        expected_unit, expected = expected_typed
        if observed_unit != expected_unit:
            saw_indeterminate = True
            deltas.append(
                {
                    'pin_id': pin.pin_id,
                    'delta': None,
                    'reason': (
                        'metric unit mismatch: observed '
                        f'{observed_unit} vs pinned {expected_unit}'
                    ),
                }
            )
            continue
        delta = observed - expected
        tolerance = _pin_tolerance(pin, baseline.policy, expected_unit)
        if tolerance is None:
            saw_indeterminate = True
            deltas.append(
                {
                    'pin_id': pin.pin_id,
                    'delta': delta,
                    'unit': expected_unit,
                    'reason': 'no compatible tolerance for this metric unit',
                }
            )
            continue
        saw_comparable = True
        deltas.append(
            {'pin_id': pin.pin_id, 'delta': delta, 'unit': expected_unit}
        )
        if abs(delta) > tolerance:
            saw_changed = True
    if not saw_comparable:
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='indeterminate',
            observed_delta_repr=_canonical_json(deltas) if deltas else None,
            reason=(
                'observation is not numerically comparable to the baseline'
                ' pin with supported comparison semantics'
            ),
        )
    delta_repr = _canonical_json(deltas)
    if saw_changed:
        if (
            baseline.policy.min_repeatability_evidence == 'required'
            and not observation.has_repeatability_evidence
        ):
            return HealthCheckAssessment(
                check_id=check.check_id,
                state='indeterminate',
                observed_delta_repr=delta_repr,
                reason='change exceeds tolerance but repeatability evidence is missing',
            )
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='changed',
            observed_delta_repr=delta_repr,
            reason='observed delta exceeds the baseline tolerance policy',
        )
    if saw_indeterminate:
        return HealthCheckAssessment(
            check_id=check.check_id,
            state='indeterminate',
            observed_delta_repr=delta_repr,
            reason=(
                'some pinned metrics could not be compared with supported'
                ' semantics'
            ),
        )
    return HealthCheckAssessment(
        check_id=check.check_id,
        state='within_baseline',
        observed_delta_repr=delta_repr,
        reason='observed value within the baseline tolerance policy',
    )


def run_health_check(
    plan: HealthCheckPlan,
    baseline: SystemHealthBaseline,
    *,
    observations: Sequence[HealthObservation] = (),
    trigger: HealthRunTrigger = 'manual',
    cause_hypothesis: str | None = None,
    created_at_utc: str,
    run_id: str | None = None,
) -> HealthCheckRun:
    """Execute a bounded check run and produce per-check assessments.

    Detections never imply a cause; ``cause_hypothesis`` stays an explicit
    unverified note intended for diagnostic follow-up.
    """

    if plan.baseline_id != baseline.baseline_id:
        raise ValueError('health run plan belongs to another baseline')
    if plan.baseline_sha256 != baseline.baseline_sha256:
        raise ValueError('health run plan is bound to a different baseline state')
    observations_by_check = {item.check_id: item for item in observations}
    assessments = tuple(
        assess_health_observation(baseline, check, observations_by_check.get(check.check_id))
        for check in plan.checks
    )
    payload: dict[str, Any] = {
        'run_id': run_id or str(uuid4()),
        'document_id': plan.document_id,
        'plan_id': plan.plan_id,
        'plan_sha256': plan.plan_sha256,
        'trigger': trigger,
        'observations': tuple(observations),
        'assessments': assessments,
        'cause_hypothesis': cause_hypothesis,
        'created_at_utc': created_at_utc,
    }
    provisional = HealthCheckRun.model_construct(**payload, run_sha256='0' * 64)
    return HealthCheckRun(
        **payload,
        run_sha256=_hash(provisional.semantic_payload()),
    )
