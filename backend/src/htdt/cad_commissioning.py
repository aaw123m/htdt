"""Installation / commissioning verification (#520).

Proposed geometry, as-built evidence, settings and measurements are compared
against *explicit, versioned tolerances* — never "looks about right". Every
check evaluates to one of ``pass`` / ``fail`` / ``unknown`` /
``not_applicable`` and those states are never collapsed: an ``unknown`` is a
fact about missing or unquantified evidence, not a soft pass.

Contract properties:

- tolerances live in an append-only, content-hashed
  :class:`ToleranceProfile` — a plan pins the profile's hash so later
  tolerance edits never rewrite what a run was judged against;
- :class:`CommissioningPlan` pins the exact ``SceneRevision`` and optional
  ``SystemVariant`` being verified plus the checks to run;
- the decision rule is uncertainty-aware: a numeric observation only passes
  when its entire uncertainty interval satisfies the tolerance, only fails
  when the entire interval violates it, and reports ``unknown`` when the
  interval straddles the boundary *or the uncertainty was never quantified*;
- an :class:`AcceptedDeviation` records a human decision on a non-passing
  check — it changes the run's *effective outcome* to ``accepted_deviation``
  and is deliberately never equal to ``pass``;
- plans and runs are append-only; observations are immutable evidence.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


COMMISSIONING_SCHEMA_VERSION = 1
COMMISSIONING_AUTHORITY_VERSION = 'commissioning-verification-1'

CommissioningStatus = Literal['pass', 'fail', 'unknown', 'not_applicable']

#: What a check compares tolerance against.
CheckSubjectKind = Literal[
    'dimension',
    'position',
    'orientation',
    'setting',
    'capability',
    'measured_response',
    'other',
]

#: How a numeric/text tolerance is applied.
#:
#: - ``abs_error`` — ``|value - target|`` must stay within ``limit``;
#: - ``min`` / ``max`` — ``value`` must stay at/above (below) ``limit``;
#: - ``range`` — ``value`` must stay inside ``[limit, limit_high]``;
#: - ``equals`` — ``value_text`` must equal ``limit_text`` exactly
#:   (settings/capability checks; uncertainty does not apply).
ToleranceOperator = Literal['abs_error', 'min', 'max', 'range', 'equals']


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


class ToleranceSpec(BaseModel):
    """One explicit tolerance bound inside a versioned profile."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    operator: ToleranceOperator
    limit: float | None = None
    limit_high: float | None = None
    limit_text: str | None = None
    target: float | None = None
    unit: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_spec(self) -> 'ToleranceSpec':
        if self.operator == 'equals':
            if self.limit_text is None:
                raise ValueError("equals tolerances require limit_text")
        elif self.operator == 'range':
            if self.limit is None or self.limit_high is None:
                raise ValueError('range tolerances require limit/limit_high')
            if self.limit > self.limit_high:
                raise ValueError('range tolerance limit exceeds limit_high')
        elif self.operator == 'abs_error':
            if self.limit is None or self.limit < 0:
                raise ValueError('abs_error tolerances require limit >= 0')
            if self.target is None:
                raise ValueError('abs_error tolerances require target')
        else:
            if self.limit is None:
                raise ValueError(f'{self.operator} tolerances require limit')
        return self


class ToleranceProfile(BaseModel):
    """A versioned, content-hashed set of verification tolerances."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = COMMISSIONING_SCHEMA_VERSION
    authority_version: Literal['commissioning-verification-1'] = (
        COMMISSIONING_AUTHORITY_VERSION
    )
    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    specs: tuple[ToleranceSpec, ...] = ()
    note: str | None = None
    created_at_utc: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'ToleranceProfile':
        keys = [spec.key for spec in self.specs]
        if len(keys) != len(set(keys)):
            raise ValueError('tolerance spec keys must be unique')
        if self.profile_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ToleranceProfile hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'document_id': self.document_id,
            'name': self.name,
            'version': self.version,
            'specs': [spec.model_dump(mode='json') for spec in self.specs],
            'note': self.note,
            'created_at_utc': self.created_at_utc,
        }

    def spec(self, key: str) -> ToleranceSpec | None:
        for item in self.specs:
            if item.key == key:
                return item
        return None


def build_tolerance_profile(
    *,
    document_id: str,
    name: str,
    version: str,
    created_at_utc: str,
    specs: tuple[ToleranceSpec, ...] = (),
    note: str | None = None,
    profile_id: str | None = None,
) -> ToleranceProfile:
    payload: dict[str, Any] = {
        'profile_id': profile_id or str(uuid4()),
        'document_id': document_id,
        'name': name,
        'version': version,
        'specs': tuple(specs),
        'note': note,
        'created_at_utc': created_at_utc,
    }
    provisional = ToleranceProfile.model_construct(
        **payload, profile_sha256='0' * 64
    )
    return ToleranceProfile(
        **payload,
        profile_sha256=_hash(provisional.semantic_payload()),
    )


class CommissioningCheck(BaseModel):
    """One verification line item inside a plan."""

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1)
    subject_kind: CheckSubjectKind
    subject_ref: str = Field(min_length=1)
    tolerance_key: str = Field(min_length=1)
    required: bool = True
    description: str | None = None


class CommissioningPlan(BaseModel):
    """The exact authority set a verification run is judged against."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = COMMISSIONING_SCHEMA_VERSION
    authority_version: Literal['commissioning-verification-1'] = (
        COMMISSIONING_AUTHORITY_VERSION
    )
    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    system_variant_id: str | None = Field(default=None, min_length=1)
    tolerance_profile_id: str = Field(min_length=1)
    tolerance_profile_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    checks: tuple[CommissioningCheck, ...] = ()
    note: str | None = None
    created_at_utc: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_plan(self) -> 'CommissioningPlan':
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError('commissioning check ids must be unique')
        if self.plan_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CommissioningPlan hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'plan_id': self.plan_id,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'system_variant_id': self.system_variant_id,
            'tolerance_profile_id': self.tolerance_profile_id,
            'tolerance_profile_sha256': self.tolerance_profile_sha256,
            'checks': [c.model_dump(mode='json') for c in self.checks],
            'note': self.note,
            'created_at_utc': self.created_at_utc,
        }

    def check(self, check_id: str) -> CommissioningCheck | None:
        for item in self.checks:
            if item.check_id == check_id:
                return item
        return None


def build_commissioning_plan(
    *,
    document_id: str,
    scene_revision_id: str,
    tolerance_profile: ToleranceProfile,
    created_at_utc: str,
    system_variant_id: str | None = None,
    checks: tuple[CommissioningCheck, ...] = (),
    note: str | None = None,
    plan_id: str | None = None,
) -> CommissioningPlan:
    keys = {spec.key for spec in tolerance_profile.specs}
    for check in checks:
        if check.tolerance_key not in keys:
            raise ValueError(
                f'check {check.check_id} references unknown tolerance '
                f'{check.tolerance_key}'
            )
    payload: dict[str, Any] = {
        'plan_id': plan_id or str(uuid4()),
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'system_variant_id': system_variant_id,
        'tolerance_profile_id': tolerance_profile.profile_id,
        'tolerance_profile_sha256': tolerance_profile.profile_sha256,
        'checks': tuple(checks),
        'note': note,
        'created_at_utc': created_at_utc,
    }
    provisional = CommissioningPlan.model_construct(
        **payload, plan_sha256='0' * 64
    )
    return CommissioningPlan(
        **payload,
        plan_sha256=_hash(provisional.semantic_payload()),
    )


class CommissioningObservation(BaseModel):
    """One immutable observation feeding a check.

    ``uncertainty`` is the half-width of the observation interval; ``None``
    means the uncertainty was never quantified — numeric checks then cannot
    evaluate to ``pass``.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    check_id: str = Field(min_length=1)
    value: float | None = None
    value_text: str | None = None
    uncertainty: float | None = Field(default=None, ge=0)
    evidence_id: str | None = Field(default=None, min_length=1)
    not_applicable: bool = False
    observed_at_utc: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def valid_observation(self) -> 'CommissioningObservation':
        if (
            not self.not_applicable
            and self.value is None
            and self.value_text is None
        ):
            raise ValueError(
                'observation requires a value, value_text, or '
                'not_applicable'
            )
        return self


class CommissioningCheckResult(BaseModel):
    """The evaluated status of one check inside a run."""

    model_config = ConfigDict(frozen=True)

    check_id: str
    status: CommissioningStatus
    observed_value: float | None = None
    observed_text: str | None = None
    tolerance_key: str | None = None
    reason: str = Field(min_length=1)


class AcceptedDeviation(BaseModel):
    """A recorded human decision on a non-passing check.

    A deviation is evidence of acceptance *despite* the evaluated status —
    it never rewrites the status to ``pass``.
    """

    model_config = ConfigDict(frozen=True)

    deviation_id: str = Field(min_length=1)
    check_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)


def evaluate_commissioning_check(
    check: CommissioningCheck,
    tolerance: ToleranceSpec | None,
    observation: CommissioningObservation | None,
) -> CommissioningCheckResult:
    """Apply the uncertainty-aware decision rule to one check."""

    def result(
        status: CommissioningStatus,
        reason: str,
        value: float | None = None,
        text: str | None = None,
    ) -> CommissioningCheckResult:
        return CommissioningCheckResult(
            check_id=check.check_id,
            status=status,
            observed_value=value,
            observed_text=text,
            tolerance_key=check.tolerance_key,
            reason=reason,
        )

    if observation is None:
        return result('unknown', 'no observation recorded for this check')
    if observation.not_applicable:
        return result(
            'not_applicable', 'observation marked not applicable'
        )
    if tolerance is None:
        return result(
            'unknown',
            'check tolerance key is not defined in the pinned profile',
        )
    if tolerance.operator == 'equals':
        if observation.value_text is None:
            return result('unknown', 'no textual observation recorded')
        if observation.value_text == tolerance.limit_text:
            return result(
                'pass', 'observed value equals the tolerance text',
                text=observation.value_text,
            )
        return result(
            'fail', 'observed value differs from the tolerance text',
            text=observation.value_text,
        )
    value = observation.value
    if value is None:
        return result('unknown', 'no numeric observation recorded')
    uncertainty = observation.uncertainty
    if uncertainty is None:
        return result(
            'unknown',
            'observation uncertainty was never quantified',
            value=value,
        )
    if tolerance.operator == 'abs_error':
        assert tolerance.target is not None and tolerance.limit is not None
        error = abs(value - tolerance.target)
        if error + uncertainty <= tolerance.limit:
            return result(
                'pass', 'error interval within tolerance', value=value
            )
        if error - uncertainty > tolerance.limit:
            return result(
                'fail', 'error interval exceeds tolerance', value=value
            )
        return result(
            'unknown', 'error interval straddles the tolerance',
            value=value,
        )
    if tolerance.operator == 'min':
        assert tolerance.limit is not None
        if value - uncertainty >= tolerance.limit:
            return result('pass', 'interval above minimum', value=value)
        if value + uncertainty < tolerance.limit:
            return result('fail', 'interval below minimum', value=value)
        return result(
            'unknown', 'interval straddles the minimum', value=value
        )
    if tolerance.operator == 'max':
        assert tolerance.limit is not None
        if value + uncertainty <= tolerance.limit:
            return result('pass', 'interval below maximum', value=value)
        if value - uncertainty > tolerance.limit:
            return result('fail', 'interval above maximum', value=value)
        return result(
            'unknown', 'interval straddles the maximum', value=value
        )
    # range
    assert tolerance.limit is not None and tolerance.limit_high is not None
    if (
        value - uncertainty >= tolerance.limit
        and value + uncertainty <= tolerance.limit_high
    ):
        return result('pass', 'interval inside the range', value=value)
    if (
        value + uncertainty < tolerance.limit
        or value - uncertainty > tolerance.limit_high
    ):
        return result('fail', 'interval outside the range', value=value)
    return result('unknown', 'interval straddles the range', value=value)


class CommissioningRun(BaseModel):
    """An append-only verification run against a pinned plan."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = COMMISSIONING_SCHEMA_VERSION
    authority_version: Literal['commissioning-verification-1'] = (
        COMMISSIONING_AUTHORITY_VERSION
    )
    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    observations: tuple[CommissioningObservation, ...] = ()
    results: tuple[CommissioningCheckResult, ...] = ()
    accepted_deviations: tuple[AcceptedDeviation, ...] = ()
    created_at_utc: str = Field(min_length=1)
    run_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_run(self) -> 'CommissioningRun':
        result_ids = [r.check_id for r in self.results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError('run results must be unique per check')
        result_check_ids = set(result_ids)
        status_by_check = {r.check_id: r.status for r in self.results}
        for deviation in self.accepted_deviations:
            status = status_by_check.get(deviation.check_id)
            if status is None:
                raise ValueError(
                    'accepted deviation references a check without a result'
                )
            if status == 'pass':
                raise ValueError(
                    'a passing check cannot carry an accepted deviation'
                )
        for observation in self.observations:
            if observation.check_id not in result_check_ids:
                raise ValueError(
                    'observation references a check without a result'
                )
        if self.run_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CommissioningRun hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'run_id': self.run_id,
            'document_id': self.document_id,
            'plan_id': self.plan_id,
            'plan_sha256': self.plan_sha256,
            'observations': [
                o.model_dump(mode='json') for o in self.observations
            ],
            'results': [r.model_dump(mode='json') for r in self.results],
            'accepted_deviations': [
                d.model_dump(mode='json') for d in self.accepted_deviations
            ],
            'created_at_utc': self.created_at_utc,
        }

    def result(self, check_id: str) -> CommissioningCheckResult | None:
        for item in self.results:
            if item.check_id == check_id:
                return item
        return None

    def effective_outcome(self, check_id: str) -> str:
        """Status with accepted deviations surfaced — never equal to pass."""

        result = self.result(check_id)
        if result is None:
            return 'unknown'
        if result.status == 'pass':
            return 'pass'
        if any(
            d.check_id == check_id for d in self.accepted_deviations
        ):
            return 'accepted_deviation'
        return result.status


def build_commissioning_run(
    *,
    plan: CommissioningPlan,
    tolerance_profile: ToleranceProfile,
    observations: tuple[CommissioningObservation, ...],
    created_at_utc: str,
    accepted_deviations: tuple[AcceptedDeviation, ...] = (),
    run_id: str | None = None,
) -> CommissioningRun:
    """Evaluate every plan check against the pinned profile and observations.

    The pinned profile's hash must match the plan's pin — a swapped-in newer
    profile is rejected rather than silently re-judging evidence.
    """

    if tolerance_profile.profile_sha256 != plan.tolerance_profile_sha256:
        raise ValueError(
            'tolerance profile hash differs from the plan pin'
        )
    check_ids = {check.check_id for check in plan.checks}
    for observation in observations:
        if observation.check_id not in check_ids:
            raise ValueError(
                f'observation {observation.observation_id} targets an '
                'unknown check'
            )
    observations_by_check: dict[str, CommissioningObservation] = {}
    for observation in observations:
        observations_by_check[observation.check_id] = observation
    results = tuple(
        evaluate_commissioning_check(
            check,
            tolerance_profile.spec(check.tolerance_key),
            observations_by_check.get(check.check_id),
        )
        for check in plan.checks
    )
    payload: dict[str, Any] = {
        'run_id': run_id or str(uuid4()),
        'document_id': plan.document_id,
        'plan_id': plan.plan_id,
        'plan_sha256': plan.plan_sha256,
        'observations': tuple(observations),
        'results': results,
        'accepted_deviations': tuple(accepted_deviations),
        'created_at_utc': created_at_utc,
    }
    provisional = CommissioningRun.model_construct(
        **payload, run_sha256='0' * 64
    )
    return CommissioningRun(
        **payload,
        run_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'COMMISSIONING_AUTHORITY_VERSION',
    'COMMISSIONING_SCHEMA_VERSION',
    'AcceptedDeviation',
    'CheckSubjectKind',
    'CommissioningCheck',
    'CommissioningCheckResult',
    'CommissioningObservation',
    'CommissioningPlan',
    'CommissioningRun',
    'CommissioningStatus',
    'ToleranceOperator',
    'ToleranceProfile',
    'ToleranceSpec',
    'build_commissioning_plan',
    'build_commissioning_run',
    'build_tolerance_profile',
    'evaluate_commissioning_check',
]
