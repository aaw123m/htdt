"""Safe-listening / test-exposure authority (issue #602).

"システムが鳴らせる SPL" (what the system *can* produce) and "人間が
受けてよい曝露" (what a human may be exposed to) are two different
quantities and are never conflated here. The #579 RP22 profile already
declares SPL *capability* honestly (capability is not a listening
level); this authority adds the other half — a *pinned* exposure limit
— and evaluates declared test plans against it, fail-closed.

Standards basis (see docs/reviews/rev56-ops.md):

- WHO *Global standard for safe listening venues and events* (2022,
  ISBN 978-92-4-004311-4) — max 100 dB LAeq,15min for venues,
  calibrated monitoring, quiet zones, hearing protection, staff
  information — registered via the #599 registry as
  ``who-safe-listening-venues@2022``;
- NIOSH Recommended Exposure Limit — 85 dBA 8 h TWA, 3 dB exchange
  rate, 80 dBA threshold — registered as ``niosh-noise-rel@98-126``
  (an *occupational* criterion; HTDT records the basis, it never
  silently generalizes occupational limits onto guests or children);
- CEDIA/CTA white paper *Reference Audio Level and SPL Capabilities*
  (RP22-family) — capability defines headroom, never a mandatory
  listening level;
- ISO 1999 is referenced as a basis tag only — its tables are not
  encoded here.

Contract properties:

- no exposure claim without a declared limit: an assessment with no
  :class:`ExposureLimitProfile` is ``unknown``, never "probably safe";
- the limit profile pins its *basis* (NIOSH / WHO / project-declared /
  unknown) — HTDT never fabricates a universal safe SPL;
- capability declarations are explicitly sourced
  (:class:`SplCapabilityDeclaration`) — ``rp22_parameter``,
  ``in_room_measured``, ``calculated``, ``declared_estimate`` or
  ``unknown`` — and a capability record is evidence about loudness
  *headroom*, not permission;
- a test plan that can exceed the declared limit produces
  ``gate_required`` — execution must wait for a sealed
  :class:`ExposureGateDecision`, which itself can only *approve* with
  named controls or block outright;
- a peak ceiling (e.g. 140 dBC) gates on capability even when the
  planned level stays within the LAeq limit — a "quiet" plan on a
  system that can spike is not "within limit";
- nothing here performs playback or enforces hardware limits — this is
  the declaration/evidence layer the #734 playback preflight can
  consult.
"""

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


SAFE_LISTENING_SCHEMA_VERSION = 1

EXPOSURE_LIMIT_AUTHORITY_VERSION = 'rev56-exposure-limit-1'
SPL_CAPABILITY_AUTHORITY_VERSION = 'rev56-spl-capability-1'
TEST_EXPOSURE_PLAN_AUTHORITY_VERSION = 'rev56-exposure-plan-1'
EXPOSURE_GATE_AUTHORITY_VERSION = 'rev56-exposure-gate-1'
EXPOSURE_ASSESSMENT_AUTHORITY_VERSION = 'rev56-exposure-assessment-1'
EXPOSURE_EVALUATION_VERSION = 'rev56-exposure-eval-1'

_SHA256 = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _require_finite(value: float, label: str) -> None:
    if not isfinite(value):
        raise ValueError(f'{label} must be finite')


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

ExposureBasis = Literal[
    'niosh_rel',
    'who_safe_listening_venue',
    'iso_1999_basis',
    'declared_project_policy',
    'unknown',
]
"""Which exposure framework a limit profile declares.

``unknown`` is a valid declaration — a project may declare its own
numeric limit without claiming a standards lineage; the assessment
still evaluates against the declared numbers, but reports the basis as
unknown in the reason trail.
"""

ExposureCriterion = Literal[
    'laeq_twa',
    'laeq_windowed',
    'peak_c',
    'dose_pct',
]
"""How the limit is measured.

``laeq_twa`` — time-weighted average against ``reference_window_s``
with ``exchange_rate_db`` (NIOSH 8 h / 3 dB shape).
``laeq_windowed`` — plain LAeq ceiling over the window (WHO 100 dB
LAeq,15min shape); ``exchange_rate_db`` must be unset.
``peak_c`` — instantaneous C-weighted peak ceiling.
``dose_pct`` — percentage of the allowable dose.
"""

CapabilitySource = Literal[
    'rp22_parameter',
    'in_room_measured',
    'calculated',
    'declared_estimate',
    'unknown',
]
"""Where an SPL capability claim comes from — ordered strongest to
weakest evidence; ``unknown`` never implies a low number."""

CapabilityScope = Literal[
    'reference_seat',
    'room_wide',
    'operator_position',
    'nearfield',
    'device_terminal',
    'unknown_position',
]

OccupancyDeclaration = Literal[
    'operator_only',
    'audience_present',
    'staff_present',
    'unoccupied_then_verify',
    'mixed',
    'unknown',
]

ExposureControl = Literal[
    'hearing_protection',
    'vacate_room',
    'remote_monitoring',
    'reduced_level',
    'reduced_duration',
    'physical_interlock',
    'spotter',
    'other',
]

GateDecisionKind = Literal[
    'allow',
    'allow_with_controls',
    'block',
    'manual_review_required',
]

ExposureCheck = Literal[
    'limit_profile',
    'capability_evidence',
    'level_vs_limit',
    'duration_dose',
    'peak_ceiling',
    'gate_record',
]
"""The six dimensions :func:`evaluate_exposure_assessment` reports."""

ExposureCheckResult = Literal[
    'verified', 'limited', 'failed', 'not_applicable'
]

ExposureAssessmentState = Literal[
    'not_applicable',
    'unknown',
    'within_limit',
    'gate_required',
    'approved',
    'approved_with_controls',
    'blocked',
]
"""Fail-closed exposure verdict.

``within_limit`` requires a declared limit *and* planned level/dose
within it *and* any declared peak ceiling respected by the declared
capability. ``gate_required`` means execution may exceed the limit —
a sealed :class:`ExposureGateDecision` is required before proceeding.
``blocked`` means a gate record explicitly blocked execution.
``unknown`` covers missing limit, missing level or missing capability
where the comparison cannot be made — never "probably fine".
"""


# ---------------------------------------------------------------------------
# Limit profile
# ---------------------------------------------------------------------------

class ExposureLimitProfile(BaseModel):
    """Sealed declaration of a human exposure limit.

    Pins the numeric limit *and* its basis. ``standard_ref`` points at
    the #599 external-standards registry entry when one exists.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    limit_id: str = Field(min_length=1)
    limit_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    basis: ExposureBasis
    criterion: ExposureCriterion
    standard_ref: AuthorityRef | None = None
    limit_level_db: float
    reference_window_s: int | None = Field(default=None, gt=0)
    exchange_rate_db: float | None = None
    peak_ceiling_db: float | None = None
    applies_to: str | None = None
    notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ExposureLimitProfile':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _require_finite(self.limit_level_db, 'limit_level_db')
        if self.exchange_rate_db is not None:
            _require_finite(self.exchange_rate_db, 'exchange_rate_db')
            if self.exchange_rate_db <= 0:
                raise ValueError('exchange_rate_db must be positive')
        if self.peak_ceiling_db is not None:
            _require_finite(self.peak_ceiling_db, 'peak_ceiling_db')
        if self.criterion in ('laeq_twa', 'laeq_windowed', 'dose_pct') and (
            self.reference_window_s is None
        ):
            raise ValueError(
                f'{self.criterion} requires reference_window_s — a '
                'limit without a window is not evaluable'
            )
        if self.criterion == 'laeq_twa' and self.exchange_rate_db is None:
            raise ValueError(
                'laeq_twa requires exchange_rate_db — without it the '
                'allowable-duration conversion is undefined'
            )
        if self.criterion == 'laeq_windowed' and (
            self.exchange_rate_db is not None
        ):
            raise ValueError(
                'laeq_windowed takes no exchange rate — the WHO windowed '
                'criterion is a plain ceiling over the window'
            )
        if self.basis == 'declared_project_policy' and not self.notes:
            raise ValueError(
                'a project-declared limit requires a note naming who '
                'declared it and why'
            )
        expected = _hash(self.identity_payload())
        if self.limit_sha256 != expected:
            raise ValueError('exposure limit sha256 does not match payload')
        if self.limit_id != _semantic_id('explim', expected):
            raise ValueError('exposure limit id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'limit_id', 'limit_sha256'}
        )


# ---------------------------------------------------------------------------
# SPL capability
# ---------------------------------------------------------------------------

class SplCapabilityDeclaration(BaseModel):
    """Sealed declaration of what a system *can* produce — never a
    claim about what a human may hear."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    capability_id: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    scope: CapabilityScope
    capability_source: CapabilitySource
    source_ref: AuthorityRef | None = None
    max_continuous_db_spl: float | None = None
    max_peak_db_spl: float | None = None
    basis_note: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'SplCapabilityDeclaration':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, value in (
            ('max_continuous_db_spl', self.max_continuous_db_spl),
            ('max_peak_db_spl', self.max_peak_db_spl),
        ):
            if value is not None:
                _require_finite(value, label)
        if (
            self.max_continuous_db_spl is None
            and self.max_peak_db_spl is None
        ):
            raise ValueError(
                'a capability declaration requires at least one level — '
                'a capability record with no number is not evidence'
            )
        if self.capability_source in (
            'rp22_parameter', 'in_room_measured', 'calculated',
        ) and self.source_ref is None:
            raise ValueError(
                f'capability_source {self.capability_source!r} requires '
                'source_ref — an unsourced capability is a declared '
                'estimate, not evidence'
            )
        if self.capability_source in (
            'declared_estimate', 'unknown'
        ) and not self.basis_note:
            raise ValueError(
                f'capability_source {self.capability_source!r} requires '
                'basis_note saying what the estimate rests on'
            )
        expected = _hash(self.identity_payload())
        if self.capability_sha256 != expected:
            raise ValueError('spl capability sha256 does not match payload')
        if self.capability_id != _semantic_id('splcap', expected):
            raise ValueError('spl capability id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'capability_id', 'capability_sha256'}
        )


# ---------------------------------------------------------------------------
# Test exposure plan
# ---------------------------------------------------------------------------

class TestExposurePlan(BaseModel):
    """Sealed declaration of a planned SPL-producing test/run."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    stimulus_ref: AuthorityRef | None = None
    capability_ref: AuthorityRef | None = None
    planned_level_db_spl: float | None = None
    planned_peak_db_spl: float | None = None
    planned_duration_s: int | None = Field(default=None, gt=0)
    occupancy: OccupancyDeclaration = 'unknown'
    position_scope: CapabilityScope = 'unknown_position'
    notes: str | None = None
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    declared_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'TestExposurePlan':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, value in (
            ('planned_level_db_spl', self.planned_level_db_spl),
            ('planned_peak_db_spl', self.planned_peak_db_spl),
        ):
            if value is not None:
                _require_finite(value, label)
        expected = _hash(self.identity_payload())
        if self.plan_sha256 != expected:
            raise ValueError('test exposure plan sha256 does not match')
        if self.plan_id != _semantic_id('expla', expected):
            raise ValueError('test exposure plan id does not match')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'plan_id', 'plan_sha256'}
        )


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

class ExposureGateDecision(BaseModel):
    """Sealed operator decision on an assessment that required a gate."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    gate_id: str = Field(min_length=1)
    gate_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    assessment_ref: AuthorityRef
    decision: GateDecisionKind
    controls: tuple[ExposureControl, ...] = ()
    reason: str | None = None
    decided_by: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = ()
    authority_version: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ExposureGateDecision':
        _require_iso8601(self.decided_at_utc, 'decided_at_utc')
        if self.decision == 'allow_with_controls' and not self.controls:
            raise ValueError(
                'allow_with_controls requires at least one named control'
            )
        if self.decision == 'block' and not self.reason:
            raise ValueError('a block decision requires a reason')
        if self.decision == 'allow' and self.controls:
            raise ValueError(
                'a bare allow cannot list controls — use '
                'allow_with_controls so the mitigation is on record'
            )
        expected = _hash(self.identity_payload())
        if self.gate_sha256 != expected:
            raise ValueError('exposure gate sha256 does not match payload')
        if self.gate_id != _semantic_id('expgate', expected):
            raise ValueError('exposure gate id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'gate_id', 'gate_sha256'}
        )


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------

class ExposureAssessment(BaseModel):
    """Sealed verdict comparing a test plan against a declared limit."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    assessment_id: str = Field(min_length=1)
    assessment_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    plan_ref: AuthorityRef
    limit_ref: AuthorityRef | None = None
    capability_ref: AuthorityRef | None = None
    state: ExposureAssessmentState
    projected_dose_pct: float | None = None
    allowable_duration_s: float | None = None
    checks: tuple[tuple[ExposureCheck, ExposureCheckResult], ...] = ()
    reasons: tuple[str, ...] = ()
    gate_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    evaluation_version: str = Field(min_length=1)
    evaluated_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _seal(self) -> 'ExposureAssessment':
        _require_iso8601(self.evaluated_at_utc, 'evaluated_at_utc')
        for label, value in (
            ('projected_dose_pct', self.projected_dose_pct),
            ('allowable_duration_s', self.allowable_duration_s),
        ):
            if value is not None:
                _require_finite(value, label)
        if self.state in ('within_limit', 'approved',
                          'approved_with_controls') and (
            self.limit_ref is None
        ):
            raise ValueError(
                'a permissive verdict requires the limit profile it was '
                'evaluated against — no "safe" without a declared limit'
            )
        expected = _hash(self.identity_payload())
        if self.assessment_sha256 != expected:
            raise ValueError('assessment sha256 does not match payload')
        if self.assessment_id != _semantic_id('expassess', expected):
            raise ValueError('assessment id does not match payload')
        return self

    def identity_payload(self) -> dict[str, object]:
        return self.model_dump(
            mode='json', exclude={'assessment_id', 'assessment_sha256'}
        )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _seal_model(model, payload: dict[str, object], id_field: str,
                sha_field: str, prefix: str):
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{id_field: _semantic_id(prefix, digest), sha_field: digest},
    )


def build_exposure_limit(
    *,
    document_id: str,
    label: str,
    basis: ExposureBasis,
    criterion: ExposureCriterion,
    limit_level_db: float,
    reference_window_s: int | None = None,
    exchange_rate_db: float | None = None,
    peak_ceiling_db: float | None = None,
    applies_to: str | None = None,
    standard_ref: AuthorityRef | None = None,
    notes: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    declared_at_utc: str | None = None,
) -> ExposureLimitProfile:
    """Seal an exposure-limit declaration."""
    payload = dict(
        document_id=document_id,
        label=label,
        basis=basis,
        criterion=criterion,
        standard_ref=standard_ref,
        limit_level_db=limit_level_db,
        reference_window_s=reference_window_s,
        exchange_rate_db=exchange_rate_db,
        peak_ceiling_db=peak_ceiling_db,
        applies_to=applies_to,
        notes=notes,
        provenance=provenance,
        authority_version=EXPOSURE_LIMIT_AUTHORITY_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        ExposureLimitProfile, payload,
        'limit_id', 'limit_sha256', 'explim',
    )


def build_spl_capability(
    *,
    document_id: str,
    scope: CapabilityScope,
    capability_source: CapabilitySource,
    max_continuous_db_spl: float | None = None,
    max_peak_db_spl: float | None = None,
    source_ref: AuthorityRef | None = None,
    basis_note: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    declared_at_utc: str | None = None,
) -> SplCapabilityDeclaration:
    """Seal an SPL capability declaration."""
    payload = dict(
        document_id=document_id,
        scope=scope,
        capability_source=capability_source,
        source_ref=source_ref,
        max_continuous_db_spl=max_continuous_db_spl,
        max_peak_db_spl=max_peak_db_spl,
        basis_note=basis_note,
        provenance=provenance,
        authority_version=SPL_CAPABILITY_AUTHORITY_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        SplCapabilityDeclaration, payload,
        'capability_id', 'capability_sha256', 'splcap',
    )


def build_test_exposure_plan(
    *,
    document_id: str,
    label: str,
    planned_level_db_spl: float | None = None,
    planned_peak_db_spl: float | None = None,
    planned_duration_s: int | None = None,
    occupancy: OccupancyDeclaration = 'unknown',
    position_scope: CapabilityScope = 'unknown_position',
    stimulus_ref: AuthorityRef | None = None,
    capability_ref: AuthorityRef | None = None,
    notes: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    declared_at_utc: str | None = None,
) -> TestExposurePlan:
    """Seal a test-exposure plan declaration."""
    payload = dict(
        document_id=document_id,
        label=label,
        stimulus_ref=stimulus_ref,
        capability_ref=capability_ref,
        planned_level_db_spl=planned_level_db_spl,
        planned_peak_db_spl=planned_peak_db_spl,
        planned_duration_s=planned_duration_s,
        occupancy=occupancy,
        position_scope=position_scope,
        notes=notes,
        provenance=provenance,
        authority_version=TEST_EXPOSURE_PLAN_AUTHORITY_VERSION,
        declared_at_utc=declared_at_utc or _utc_now(),
    )
    return _seal_model(
        TestExposurePlan, payload,
        'plan_id', 'plan_sha256', 'expla',
    )


def build_exposure_gate(
    *,
    document_id: str,
    assessment: ExposureAssessment,
    decision: GateDecisionKind,
    decided_by: str,
    controls: tuple[ExposureControl, ...] = (),
    reason: str | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
    decided_at_utc: str | None = None,
) -> ExposureGateDecision:
    """Seal an exposure gate decision pinning its assessment."""
    payload = dict(
        document_id=document_id,
        assessment_ref=AuthorityRef(
            kind='exposure_assessment',
            ref_id=assessment.assessment_id,
            ref_sha256=assessment.assessment_sha256,
        ),
        decision=decision,
        controls=controls,
        reason=reason,
        decided_by=decided_by,
        provenance=provenance,
        authority_version=EXPOSURE_GATE_AUTHORITY_VERSION,
        decided_at_utc=decided_at_utc or _utc_now(),
    )
    return _seal_model(
        ExposureGateDecision, payload,
        'gate_id', 'gate_sha256', 'expgate',
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _allowable_duration_s(
    limit: ExposureLimitProfile, level_db: float
) -> float | None:
    """Allowable exposure duration at ``level_db`` under the limit.

    ``laeq_twa``/``dose_pct``: NIOSH-style exchange — time halves per
    ``exchange_rate_db`` over the reference window. ``laeq_windowed``:
    the window itself if the level is under the ceiling, else 0.
    ``peak_c``: instantaneous — returns 0 when the peak ceiling is
    exceeded, else infinity-equivalent (None upstream).
    """
    if limit.criterion == 'laeq_windowed':
        assert limit.reference_window_s is not None
        return (
            float(limit.reference_window_s)
            if level_db <= limit.limit_level_db
            else 0.0
        )
    if limit.criterion in ('laeq_twa', 'dose_pct'):
        assert limit.reference_window_s is not None
        assert limit.exchange_rate_db is not None
        over = level_db - limit.limit_level_db
        return float(limit.reference_window_s) * 2.0 ** (
            -over / limit.exchange_rate_db
        )
    # peak_c — instantaneous ceiling, duration is irrelevant
    return None


def _seal_assessment(
    *,
    document_id: str,
    plan: TestExposurePlan,
    limit: ExposureLimitProfile | None,
    capability: SplCapabilityDeclaration | None,
    state: ExposureAssessmentState,
    projected_dose_pct: float | None,
    allowable_duration_s: float | None,
    checks: tuple[tuple[ExposureCheck, ExposureCheckResult], ...],
    reasons: tuple[str, ...],
    gate: ExposureGateDecision | None,
    evidence_refs: tuple[str, ...],
    evaluated_at_utc: str,
) -> ExposureAssessment:
    payload = dict(
        document_id=document_id,
        plan_ref=AuthorityRef(
            kind='test_exposure_plan',
            ref_id=plan.plan_id,
            ref_sha256=plan.plan_sha256,
        ),
        limit_ref=(
            AuthorityRef(
                kind='exposure_limit_profile',
                ref_id=limit.limit_id,
                ref_sha256=limit.limit_sha256,
            )
            if limit is not None else None
        ),
        capability_ref=(
            AuthorityRef(
                kind='spl_capability',
                ref_id=capability.capability_id,
                ref_sha256=capability.capability_sha256,
            )
            if capability is not None else None
        ),
        state=state,
        projected_dose_pct=projected_dose_pct,
        allowable_duration_s=allowable_duration_s,
        checks=checks,
        reasons=reasons,
        gate_ref=gate.gate_id if gate is not None else None,
        evidence_refs=evidence_refs,
        evaluation_version=EXPOSURE_EVALUATION_VERSION,
        evaluated_at_utc=evaluated_at_utc,
    )
    return _seal_model(
        ExposureAssessment, payload,
        'assessment_id', 'assessment_sha256', 'expassess',
    )


def evaluate_exposure_assessment(
    *,
    document_id: str,
    plan: TestExposurePlan,
    limit: ExposureLimitProfile | None = None,
    capability: SplCapabilityDeclaration | None = None,
    gate: ExposureGateDecision | None = None,
    evaluated_at_utc: str | None = None,
) -> ExposureAssessment:
    """Fail-closed verdict: may this test exposure proceed?

    ``within_limit`` only when a declared limit exists and the plan is
    inside it (including the peak ceiling vs declared capability).
    Exceeding the limit requires a sealed gate decision; no limit or no
    planned level reads ``unknown`` — never an implied "safe".
    """
    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')
    checks: list[tuple[ExposureCheck, ExposureCheckResult]] = []
    reasons: list[str] = []
    evidence: list[str] = [plan.plan_id]

    # --- limit profile ---------------------------------------------------
    if limit is None:
        checks.extend((
            ('limit_profile', 'failed'),
            ('capability_evidence', 'not_applicable'),
            ('level_vs_limit', 'not_applicable'),
            ('duration_dose', 'not_applicable'),
            ('peak_ceiling', 'not_applicable'),
            ('gate_record', 'not_applicable'),
        ))
        return _seal_assessment(
            document_id=document_id,
            plan=plan,
            limit=None,
            capability=capability,
            state='unknown',
            projected_dose_pct=None,
            allowable_duration_s=None,
            checks=tuple(checks),
            reasons=(
                'no exposure limit profile is declared — "safe" cannot '
                'be claimed without a declared limit',
            ),
            gate=gate,
            evidence_refs=tuple(evidence),
            evaluated_at_utc=evaluated_at_utc,
        )
    checks.append(('limit_profile', 'verified'))
    evidence.append(limit.limit_id)
    if limit.basis == 'unknown':
        reasons.append(
            'the limit profile declares basis=unknown — the numbers '
            'are project-declared, not a standard lineage'
        )

    # --- capability evidence ---------------------------------------------
    if capability is None:
        checks.append(('capability_evidence', 'limited'))
    else:
        checks.append((
            'capability_evidence',
            'verified'
            if capability.capability_source
            in ('rp22_parameter', 'in_room_measured', 'calculated')
            else 'limited',
        ))
        evidence.append(capability.capability_id)

    # --- level vs limit ----------------------------------------------------
    level = plan.planned_level_db_spl
    duration = plan.planned_duration_s
    allowable: float | None = None
    dose_pct: float | None = None

    if limit.criterion == 'peak_c':
        peak = plan.planned_peak_db_spl or level
        if peak is None:
            checks.append(('level_vs_limit', 'limited'))
            reasons.append('no planned level declared — unevaluable')
        elif peak <= limit.limit_level_db:
            checks.append(('level_vs_limit', 'verified'))
        else:
            checks.append(('level_vs_limit', 'failed'))
            reasons.append(
                f'planned peak {peak:g} dB exceeds the declared '
                f'{limit.limit_level_db:g} dB peak ceiling'
            )
        checks.append(('duration_dose', 'not_applicable'))
    elif level is None:
        checks.extend((
            ('level_vs_limit', 'limited'),
            ('duration_dose', 'limited'),
        ))
        reasons.append(
            'no planned level declared — exposure cannot be evaluated'
        )
    else:
        allowable = _allowable_duration_s(limit, level)
        if level <= limit.limit_level_db:
            checks.append(('level_vs_limit', 'verified'))
        else:
            checks.append(('level_vs_limit', 'failed'))
            reasons.append(
                f'planned level {level:g} dB exceeds the declared '
                f'{limit.limit_level_db:g} dB limit'
            )
        if duration is None or allowable is None:
            checks.append(('duration_dose', 'limited'))
            if duration is None:
                reasons.append(
                    'no planned duration declared — dose is unevaluable'
                )
        elif duration <= allowable:
            checks.append(('duration_dose', 'verified'))
            dose_pct = 100.0 * duration / allowable
        else:
            checks.append(('duration_dose', 'failed'))
            dose_pct = 100.0 * duration / allowable
            reasons.append(
                f'planned duration {duration}s exceeds the allowable '
                f'{allowable:.0f}s at {level:g} dB under the declared '
                'exchange basis'
            )

    # --- peak ceiling ------------------------------------------------------
    if limit.peak_ceiling_db is None:
        checks.append(('peak_ceiling', 'not_applicable'))
    else:
        candidate_peak = plan.planned_peak_db_spl
        if (
            candidate_peak is None
            and capability is not None
            and capability.max_peak_db_spl is not None
        ):
            candidate_peak = capability.max_peak_db_spl
        if candidate_peak is None:
            checks.append(('peak_ceiling', 'limited'))
            reasons.append(
                'a peak ceiling is declared but neither the plan nor '
                'the capability declares a peak level — unverified'
            )
        elif candidate_peak <= limit.peak_ceiling_db:
            checks.append(('peak_ceiling', 'verified'))
        else:
            checks.append(('peak_ceiling', 'failed'))
            reasons.append(
                f'peak capability/plan {candidate_peak:g} dB exceeds '
                f'the declared peak ceiling {limit.peak_ceiling_db:g} dB'
            )

    # --- gate ---------------------------------------------------------------
    fails = {c for c, r in checks if r == 'failed'}
    limited = {c for c, r in checks if r == 'limited'}

    if fails:
        # Gate record is required whenever anything fails.
        if gate is None:
            checks.append(('gate_record', 'failed'))
            reasons.append(
                'the plan exceeds declared limits and no exposure gate '
                'decision is on record — execution must not proceed'
            )
            state: ExposureAssessmentState = 'gate_required'
        else:
            checks.append(('gate_record', 'verified'))
            evidence.append(gate.gate_id)
            state = {
                'block': 'blocked',
                'manual_review_required': 'gate_required',
                'allow': 'approved',
                'allow_with_controls': 'approved_with_controls',
            }[gate.decision]
            if gate.decision in ('allow', 'allow_with_controls'):
                reasons.append(
                    'a gate decision approved execution despite the '
                    'declared-limit exceedance — the controls/decision '
                    'are on record'
                )
    else:
        checks.append(('gate_record', 'not_applicable'))
        if limited:
            state = 'unknown'
            reasons.append(
                'evaluation inputs are incomplete — verdict stays '
                'unknown rather than implying safety'
            )
        else:
            state = 'within_limit'

    return _seal_assessment(
        document_id=document_id,
        plan=plan,
        limit=limit,
        capability=capability,
        state=state,
        projected_dose_pct=dose_pct,
        allowable_duration_s=allowable,
        checks=tuple(checks),
        reasons=tuple(reasons),
        gate=gate,
        evidence_refs=tuple(evidence),
        evaluated_at_utc=evaluated_at_utc,
    )


__all__ = [
    'EXPOSURE_ASSESSMENT_AUTHORITY_VERSION',
    'EXPOSURE_EVALUATION_VERSION',
    'EXPOSURE_GATE_AUTHORITY_VERSION',
    'EXPOSURE_LIMIT_AUTHORITY_VERSION',
    'SAFE_LISTENING_SCHEMA_VERSION',
    'SPL_CAPABILITY_AUTHORITY_VERSION',
    'TEST_EXPOSURE_PLAN_AUTHORITY_VERSION',
    'CapabilityScope',
    'CapabilitySource',
    'ExposureAssessment',
    'ExposureAssessmentState',
    'ExposureBasis',
    'ExposureCheck',
    'ExposureCheckResult',
    'ExposureControl',
    'ExposureCriterion',
    'ExposureGateDecision',
    'ExposureLimitProfile',
    'GateDecisionKind',
    'OccupancyDeclaration',
    'SplCapabilityDeclaration',
    'TestExposurePlan',
    'build_exposure_gate',
    'build_exposure_limit',
    'build_spl_capability',
    'build_test_exposure_plan',
    'evaluate_exposure_assessment',
]
