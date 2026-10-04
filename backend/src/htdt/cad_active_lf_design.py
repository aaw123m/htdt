"""Active low-frequency control design-space authorities (#533).

The sealed :class:`~htdt.cad_active_lf_control.ActiveLowFrequencyControlPlan`
(#973) is the transfer-matrix *plan* authority. This module supplies the
remaining design-space slices the issue prescribes — all deterministic,
all fail-closed on missing evidence:

* **Capability labels** — :func:`classify_lf_control_strategy` distinguishes
  bass-management-only, multi-sub sum optimization (per-sub
  gain/delay/polarity/crossover), cross-channel support control, wavefront
  array control and external proprietary control. The labels are separate
  authorities: a bass-management profile never upgrades itself into
  "active treatment", and a vendor-opaque plan never accrues
  HTDT-generated credit.
* **Array/wavefront geometry eligibility** —
  :func:`evaluate_source_group_eligibility` /
  :func:`evaluate_plan_eligibility` decide which declared speaker
  groups/arrays qualify for support control vs only independent treatment,
  from geometry (member count, positions, boundary relationship) plus
  declared capability evidence (usable band, headroom). Missing evidence
  resolves to ``unknown``, never to a pass.
* **Target DSP resource feasibility** — :func:`evaluate_dsp_feasibility`
  checks a plan's matrix against a :class:`DspResourceEnvelope` of
  *declared* resource rows (outputs, cross terms, FIR/IIR budgets,
  routing simultaneity, gain/delay/latency limits). An undeclared limit
  is ``UNKNOWN``, not "unlimited".
* **Multi-seat LF objective plumbing** — :func:`lf_control_objectives`
  composes the existing independent objective authority
  (:func:`seat_pairwise_objectives` for seat-to-seat spread plus
  per-seat target-response objectives) restricted to the plan's control
  band, with every produced metric bound to the plan hash so the O30
  evaluation/Pareto records trace to the exact design.

No proprietary ART/WaveForming algorithm is emulated; vendor-labelled
plans stay opaque bindings.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_active_lf_control import (
    ActiveLowFrequencyControlPlan,
    ControlMatrixEntry,
)
from .cad_calibration import CadDeviceCapabilityConstraints
from .cad_equipment import EquipmentDataProvenance
from .cad_video_geometry import EvaluationStatus, _combine_status
from .canonical_json import canonical_sha256 as _hash
from .comparison import FrequencyResponse
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveValidDomain,
    ObjectiveVector,
    seat_pairwise_objectives,
    target_response_objectives,
    ResponseObjectiveSpec,
    merge_objective_vectors,
)
from .cad_seat_priority import SeatPriorityProfile


_SHA256_PATTERN = r'^[0-9a-f]{64}$'



# ---------------------------------------------------------------------------
# Capability labels
# ---------------------------------------------------------------------------

ActiveControlStrategy = Literal[
    'none',
    'independent_channel_eq',
    'bass_management_only',
    'multi_sub_sum_optimization',
    'cross_channel_support_control',
    'wavefront_active_control',
    'external_proprietary_control',
    'unknown',
]
"""Honest strategy labels — the issue taxonomy plus the
``bass_management_only`` distinction the remaining-slice list prescribes.
``multi_sub_sum_optimization`` is per-sub gain/delay/polarity/crossover
coordination *without* cross-channel support terms; it must never be
presented as active treatment."""

ACTIVE_CONTROL_LABEL_JA: dict[str, str] = {
    'none': '低域アクティブ制御なし',
    'independent_channel_eq': '独立チャンネル EQ',
    'bass_management_only': 'ベースマネージメントのみ',
    'multi_sub_sum_optimization': 'マルチサブ合成最適化',
    'cross_channel_support_control': 'クロスチャンネル協調制御',
    'wavefront_active_control': '波面アクティブ制御',
    'external_proprietary_control': '外部独自制御（インポート）',
    'unknown': '低域制御戦略 不明',
}
"""User-facing Japanese labels for each strategy — UI surfaces render the
label, never a conflated 'room correction' badge."""


# ---------------------------------------------------------------------------
# Source-group declarations and capability evidence
# ---------------------------------------------------------------------------

GroupBoundary = Literal['front', 'rear', 'side', 'ceiling', 'free_standing', 'unknown']
"""Declared room-boundary relationship for an emitter group. ``unknown``
means unrecorded — it is honest missing evidence, never a free pass."""


class SpeakerGroupDecl(BaseModel):
    """Declared speaker/emitter group for eligibility evaluation.

    ``member_entity_ids`` binds the group to exact scene entities.
    ``member_positions_m`` carries the resolved world positions aligned to
    the member ids — an empty tuple means geometry was not resolved, which
    evaluates as UNKNOWN rather than an assumed layout.
    """

    model_config = ConfigDict(frozen=True)

    group_id: str = Field(min_length=1)
    member_entity_ids: tuple[str, ...] = ()
    member_positions_m: tuple[tuple[float, float, float], ...] = ()
    boundary: GroupBoundary = 'unknown'
    role_id: str | None = None

    @model_validator(mode='after')
    def valid_group(self) -> 'SpeakerGroupDecl':
        ids = list(self.member_entity_ids)
        if len(set(ids)) != len(ids):
            raise ValueError('speaker group member ids must be unique')
        if self.member_positions_m and len(self.member_positions_m) != len(ids):
            raise ValueError(
                'member_positions_m must be empty or aligned to member ids'
            )
        for position in self.member_positions_m:
            if len(position) != 3 or not all(
                isfinite(float(value)) for value in position
            ):
                raise ValueError('member positions must be finite xyz triples')
        return self


class SourceCapabilityEvidence(BaseModel):
    """Evidence for one emitter's usable band and headroom.

    Every field is optional; absence means "not evidenced" and evaluates
    to UNKNOWN downstream — HTDT never infers usable bandwidth or headroom
    from marketing channel counts or cabinet size.
    """

    model_config = ConfigDict(frozen=True)

    usable_band_hz: tuple[float, float] | None = None
    declared_headroom_db: float | None = Field(default=None, ge=0.0)
    capability_ref: str | None = None

    @model_validator(mode='after')
    def valid_evidence(self) -> 'SourceCapabilityEvidence':
        if self.usable_band_hz is not None:
            low, high = self.usable_band_hz
            if not (isfinite(low) and isfinite(high)) or not (0 < low < high):
                raise ValueError('usable_band_hz must satisfy 0 < low < high')
        if self.declared_headroom_db is not None and not isfinite(
            float(self.declared_headroom_db)
        ):
            raise ValueError('declared_headroom_db must be finite')
        return self


class EligibilityCheck(BaseModel):
    """One deterministic check row inside a group/path verdict."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    status: EvaluationStatus
    detail: str = ''


class SourceGroupEligibility(BaseModel):
    """Eligibility verdict of one declared group for one strategy tier."""

    model_config = ConfigDict(frozen=True)

    group_id: str = Field(min_length=1)
    strategy: Literal[
        'independent_eq',
        'multi_sub_optimization',
        'cross_channel_support',
        'wavefront_array',
    ]
    verdict: Literal['eligible', 'ineligible', 'unknown']
    checks: tuple[EligibilityCheck, ...]


def _group_geometry_checks(
    group: SpeakerGroupDecl,
) -> tuple[list[EligibilityCheck], tuple[tuple[float, float, float], ...] | None]:
    checks: list[EligibilityCheck] = []
    checks.append(
        EligibilityCheck(
            check='members_declared',
            status='PASS' if group.member_entity_ids else 'FAIL',
            detail=(
                f'{len(group.member_entity_ids)} declared member(s)'
                if group.member_entity_ids
                else 'group declares no member entities'
            ),
        )
    )
    if not group.member_positions_m:
        checks.append(
            EligibilityCheck(
                check='positions_recorded',
                status='UNKNOWN',
                detail='member positions were not resolved from the scene',
            )
        )
        return checks, None
    checks.append(
        EligibilityCheck(
            check='positions_recorded',
            status='PASS',
            detail='member positions resolved for all members',
        )
    )
    return checks, group.member_positions_m


def _band_cover(
    usable: tuple[float, float] | None, band: tuple[float, float]
) -> EvaluationStatus | None:
    """None when the member carries no evidence at all."""
    if usable is None:
        return 'UNKNOWN'
    return 'PASS' if usable[0] <= band[0] + 1e-9 and usable[1] >= band[1] - 1e-9 else 'FAIL'


def _capability_checks(
    group: SpeakerGroupDecl,
    evidence: Mapping[str, SourceCapabilityEvidence],
    control_band_hz: tuple[float, float],
    *,
    require_headroom: bool,
) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    band_statuses: list[EvaluationStatus] = []
    headroom_statuses: list[EvaluationStatus] = []
    for member in group.member_entity_ids:
        record = evidence.get(member)
        if record is None:
            band_statuses.append('UNKNOWN')
            headroom_statuses.append('UNKNOWN')
            continue
        band_statuses.append(_band_cover(record.usable_band_hz, control_band_hz))
        headroom_statuses.append(
            'PASS' if record.declared_headroom_db is not None else 'UNKNOWN'
        )
    checks.append(
        EligibilityCheck(
            check='usable_band_covers_control_band',
            status=_combine_status(tuple(band_statuses)) if band_statuses else 'FAIL',
            detail=(
                'every member usable band covers the control band'
                if band_statuses and all(s == 'PASS' for s in band_statuses)
                else 'member usable-band evidence missing or narrower than '
                f'{control_band_hz[0]:g}-{control_band_hz[1]:g} Hz'
            ),
        )
    )
    status = _combine_status(tuple(headroom_statuses)) if headroom_statuses else 'FAIL'
    detail = (
        'every member carries declared headroom evidence'
        if status == 'PASS'
        else 'headroom evidence missing for one or more members'
    )
    if not require_headroom:
        # Advisory only: multi-sub/independent strategies do not raise
        # demand on a member the way support paths do, so missing headroom
        # is reported but never gates the verdict.
        detail += ' (advisory for this strategy)'
        status = 'NOT_APPLICABLE'
    checks.append(
        EligibilityCheck(
            check='headroom_declared',
            status=status,
            detail=detail,
        )
    )
    return checks


def _array_geometry_checks(
    positions: tuple[tuple[float, float, float], ...] | None,
    group: SpeakerGroupDecl,
) -> tuple[list[EligibilityCheck], float | None, float | None]:
    """Span/spacing checks for array-tier eligibility."""
    checks: list[EligibilityCheck] = []
    if positions is None or len(positions) < 2:
        checks.append(
            EligibilityCheck(
                check='array_geometry',
                status='UNKNOWN' if positions is None else 'FAIL',
                detail=(
                    'array geometry unresolved'
                    if positions is None
                    else 'an array requires at least two positioned members'
                ),
            )
        )
        return checks, None, None
    xs = sorted(p[0] for p in positions)
    ys = sorted(p[1] for p in positions)
    span = max(xs[-1] - xs[0], ys[-1] - ys[0])
    # nearest-neighbour mean spacing over the dominant horizontal axis
    axis = xs if (xs[-1] - xs[0]) >= (ys[-1] - ys[0]) else ys
    gaps = [b - a for a, b in zip(axis, axis[1:])]
    mean_spacing = sum(gaps) / len(gaps) if gaps else 0.0
    checks.append(
        EligibilityCheck(
            check='array_geometry',
            status='PASS',
            detail=(
                f'array span {span:g} m, mean member spacing '
                f'{mean_spacing:g} m'
            ),
        )
    )
    checks.append(
        EligibilityCheck(
            check='boundary_declared',
            status='PASS' if group.boundary != 'unknown' else 'UNKNOWN',
            detail=(
                f'boundary relationship recorded: {group.boundary}'
                if group.boundary != 'unknown'
                else 'array boundary relationship not recorded'
            ),
        )
    )
    return checks, span, mean_spacing


def evaluate_source_group_eligibility(
    group: SpeakerGroupDecl,
    evidence: Mapping[str, SourceCapabilityEvidence],
    control_band_hz: tuple[float, float],
    strategy: Literal[
        'independent_eq',
        'multi_sub_optimization',
        'cross_channel_support',
        'wavefront_array',
    ],
) -> SourceGroupEligibility:
    """Deterministic eligibility of one group for one strategy tier.

    - ``independent_eq``: per-channel treatment; only requires that the
      group declares members.
    - ``multi_sub_optimization``: joint gain/delay/polarity/crossover over
      multiple emitters of one content group — needs >=2 members,
      positions and usable-band evidence.
    - ``cross_channel_support``: emitters supporting a *different*
      channel — adds declared headroom (support raises demand on the
      supporter) and positions.
    - ``wavefront_array``: a boundary-declared emitter/absorber array —
      needs >=2 positioned members, a recorded boundary relationship,
      usable band and headroom.
    """
    base_checks, positions = _group_geometry_checks(group)
    checks = list(base_checks)

    required: list[EligibilityCheck] = []
    if strategy == 'independent_eq':
        required = [c for c in checks if c.check == 'members_declared']
    elif strategy == 'multi_sub_optimization':
        required = list(checks)
        required.append(
            EligibilityCheck(
                check='multi_emitter_count',
                status='PASS' if len(group.member_entity_ids) >= 2 else 'FAIL',
                detail=(
                    'multi-sub optimization requires at least two emitters '
                    'sharing one content group'
                ),
            )
        )
        required.extend(
            _capability_checks(
                group, evidence, control_band_hz, require_headroom=False
            )
        )
    elif strategy == 'cross_channel_support':
        required = list(checks)
        required.extend(
            _capability_checks(
                group, evidence, control_band_hz, require_headroom=True
            )
        )
    else:  # wavefront_array
        required = list(checks)
        required.append(
            EligibilityCheck(
                check='multi_emitter_count',
                status='PASS' if len(group.member_entity_ids) >= 2 else 'FAIL',
                detail='a wavefront array requires at least two emitters',
            )
        )
        geometry_checks, _span, _spacing = _array_geometry_checks(positions, group)
        required.extend(geometry_checks)
        required.extend(
            _capability_checks(
                group, evidence, control_band_hz, require_headroom=True
            )
        )

    combined = _combine_status(tuple(c.status for c in required))
    verdict = (
        'eligible'
        if combined == 'PASS'
        else ('ineligible' if combined == 'FAIL' else 'unknown')
    )
    return SourceGroupEligibility(
        group_id=group.group_id,
        strategy=strategy,
        verdict=verdict,
        checks=tuple(required),
    )


# ---------------------------------------------------------------------------
# Plan-level path classification and eligibility
# ---------------------------------------------------------------------------

MatrixPathKind = Literal['diagonal', 'content_feed', 'cross_channel_support', 'unknown']


def classify_matrix_path(
    entry: ControlMatrixEntry,
    input_group: SpeakerGroupDecl | None,
    output_group: SpeakerGroupDecl | None,
) -> MatrixPathKind:
    """Classify one matrix path by physical source ownership.

    - ``diagonal``: the output group's members are exactly the input
      group's members — the channel's own emitters reproducing their own
      channel (independent treatment).
    - ``content_feed``: the input is a pure content channel (no member
      entities — e.g. an LFE feed into a sub group). Feeding one content
      channel into multiple emitters is multi-sub territory, not support.
    - ``cross_channel_support``: emitters belonging to a different
      physical group reinforce/cancel the input channel — the ART-style
      support relationship.
    - ``unknown``: either endpoint's membership is undeclared.
    """
    if input_group is None or output_group is None:
        return 'unknown'
    if not input_group.member_entity_ids:
        return 'content_feed'
    if set(input_group.member_entity_ids) == set(output_group.member_entity_ids):
        return 'diagonal'
    return 'cross_channel_support'


class PathEligibility(BaseModel):
    """Eligibility of one enabled matrix path."""

    model_config = ConfigDict(frozen=True)

    input_group: str = Field(min_length=1)
    output_group: str = Field(min_length=1)
    path_kind: MatrixPathKind
    required_strategy: str = Field(min_length=1)
    verdict: Literal['eligible', 'ineligible', 'unknown']
    checks: tuple[EligibilityCheck, ...]


class PlanEligibilityReport(BaseModel):
    """Path-level eligibility over one sealed control plan."""

    model_config = ConfigDict(frozen=True)

    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    paths: tuple[PathEligibility, ...]
    overall: Literal['eligible', 'ineligible', 'unknown']
    report_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_report(self) -> 'PlanEligibilityReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('plan eligibility report hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'report_sha256'})


def evaluate_plan_eligibility(
    plan: ActiveLowFrequencyControlPlan,
    groups: Sequence[SpeakerGroupDecl],
    evidence: Mapping[str, SourceCapabilityEvidence],
) -> PlanEligibilityReport:
    """Bind every enabled matrix path to its required-strategy verdict.

    A path whose endpoint group is undeclared is ``unknown`` — an
    unresolvable group is never silently eligible.
    """
    by_id = {g.group_id: g for g in groups}
    paths: list[PathEligibility] = []
    for entry in plan.matrix:
        if not entry.enabled:
            continue
        input_decl = by_id.get(entry.input_group)
        output_decl = by_id.get(entry.output_group)
        path_kind = classify_matrix_path(entry, input_decl, output_decl)
        if path_kind == 'unknown':
            paths.append(
                PathEligibility(
                    input_group=entry.input_group,
                    output_group=entry.output_group,
                    path_kind=path_kind,
                    required_strategy='unknown',
                    verdict='unknown',
                    checks=(
                        EligibilityCheck(
                            check='group_undeclared',
                            status='UNKNOWN',
                            detail='a path endpoint group is not declared',
                        ),
                    ),
                )
            )
            continue
        if path_kind == 'cross_channel_support':
            required_strategy = 'cross_channel_support'
            # The *supporter* (output) group must be support-eligible; the
            # supported (input) group only has to be a real declared group.
            group_eval = evaluate_source_group_eligibility(
                output_decl, evidence, plan.control_band_hz,
                strategy='cross_channel_support',
            )
            extra: list[EligibilityCheck] = []
            shared = set(input_decl.member_entity_ids) & set(
                output_decl.member_entity_ids
            )
            extra.append(
                EligibilityCheck(
                    check='support_members_distinct',
                    status='FAIL' if shared == set(input_decl.member_entity_ids) else 'PASS',
                    detail=(
                        'support output members are identical to the '
                        'supported channel members — not a support path'
                        if shared == set(input_decl.member_entity_ids)
                        else 'support output carries members distinct from '
                        'the supported channel'
                    ),
                )
            )
            checks = tuple(list(group_eval.checks) + extra)
            verdict = (
                'eligible'
                if group_eval.verdict == 'eligible'
                and extra[0].status == 'PASS'
                else (
                    'ineligible'
                    if group_eval.verdict == 'ineligible'
                    or extra[0].status == 'FAIL'
                    else 'unknown'
                )
            )
        elif path_kind == 'content_feed':
            required_strategy = (
                'multi_sub_optimization'
                if len(output_decl.member_entity_ids) >= 2
                else 'independent_eq'
            )
            group_eval = evaluate_source_group_eligibility(
                output_decl, evidence, plan.control_band_hz,
                strategy=required_strategy,
            )
            checks, verdict = group_eval.checks, group_eval.verdict
        else:  # diagonal
            required_strategy = (
                'multi_sub_optimization'
                if len(output_decl.member_entity_ids) >= 2
                else 'independent_eq'
            )
            group_eval = evaluate_source_group_eligibility(
                output_decl, evidence, plan.control_band_hz,
                strategy=required_strategy,
            )
            checks, verdict = group_eval.checks, group_eval.verdict
        paths.append(
            PathEligibility(
                input_group=entry.input_group,
                output_group=entry.output_group,
                path_kind=path_kind,
                required_strategy=required_strategy,
                verdict=verdict,
                checks=checks,
            )
        )
    combined = _combine_status(
        tuple(
            'PASS' if p.verdict == 'eligible'
            else ('FAIL' if p.verdict == 'ineligible' else 'UNKNOWN')
            for p in paths
        )
    )
    overall = (
        'eligible'
        if combined == 'PASS'
        else ('ineligible' if combined == 'FAIL' else 'unknown')
    )
    probe = {
        'plan_sha256': plan.plan_sha256,
        'paths': [p.model_dump(mode='json') for p in paths],
        'overall': overall,
    }
    return PlanEligibilityReport(
        plan_sha256=plan.plan_sha256,
        paths=tuple(paths),
        overall=overall,
        report_sha256=_hash(probe),
    )


# ---------------------------------------------------------------------------
# Target DSP resource feasibility
# ---------------------------------------------------------------------------

class DspResourceEnvelope(BaseModel):
    """Declared target-processor resource rows (#533 §3).

    ``None`` means the limit is *unknown* — never "unlimited". Rows should
    be evidenced by the equipment/device authority (compatibility matrix,
    capability constraints, qualification records) via ``provenance``;
    rows the device does not disclose stay ``None`` and evaluate UNKNOWN.
    """

    model_config = ConfigDict(frozen=True)

    envelope_id: str = Field(min_length=1)
    capability_ref: str | None = None
    adapter_ref: str | None = None
    available_output_count: int | None = Field(default=None, ge=0)
    allowed_output_groups: tuple[str, ...] = ()
    max_cross_terms: int | None = Field(default=None, ge=0)
    max_processing_paths: int | None = Field(default=None, ge=0)
    max_fir_taps_total: int | None = Field(default=None, ge=0)
    max_fir_taps_per_path: int | None = Field(default=None, ge=0)
    max_iir_biquads_total: int | None = Field(default=None, ge=0)
    max_simultaneous_routes: int | None = Field(default=None, ge=0)
    min_gain_db: float | None = None
    max_gain_db: float | None = None
    max_delay_s: float | None = Field(default=None, ge=0.0)
    latency_budget_s: float | None = Field(default=None, ge=0.0)
    supported_sample_rates_hz: tuple[int, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = ()

    @model_validator(mode='after')
    def valid_envelope(self) -> 'DspResourceEnvelope':
        if len(set(self.allowed_output_groups)) != len(self.allowed_output_groups):
            raise ValueError('allowed output groups must be unique')
        if len(set(self.supported_sample_rates_hz)) != len(self.supported_sample_rates_hz):
            raise ValueError('supported sample rates must be unique')
        for name in ('min_gain_db', 'max_gain_db', 'max_delay_s', 'latency_budget_s'):
            value = getattr(self, name)
            if value is not None and not isfinite(float(value)):
                raise ValueError(f'{name} must be finite')
        if (
            self.min_gain_db is not None
            and self.max_gain_db is not None
            and self.max_gain_db < self.min_gain_db
        ):
            raise ValueError('gain range is invalid')
        return self


class FilterPathDemand(BaseModel):
    """Declared filter resource demand for one matrix path.

    Only populated when the filter authority actually knows the demand —
    an FIR path with no tap count can never be proven to fit a tap budget.
    """

    model_config = ConfigDict(frozen=True)

    input_group: str = Field(min_length=1)
    output_group: str = Field(min_length=1)
    fir_taps: int | None = Field(default=None, ge=0)
    iir_biquads: int | None = Field(default=None, ge=0)


class DspFeasibilityReport(BaseModel):
    """Feasibility verdict for one plan against one envelope."""

    model_config = ConfigDict(frozen=True)

    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    envelope_id: str = Field(min_length=1)
    verdict: Literal['compatible', 'incompatible', 'unknown']
    checks: tuple[EligibilityCheck, ...]
    report_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def valid_report(self) -> 'DspFeasibilityReport':
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('dsp feasibility report hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'report_sha256'})


def envelope_from_capability_constraints(
    constraints: CadDeviceCapabilityConstraints,
    *,
    envelope_id: str,
    cross_term_limit: int | None = None,
    provenance: tuple[EquipmentDataProvenance, ...] = (),
) -> DspResourceEnvelope:
    """Map a declared calibration capability set onto an envelope.

    This composes with the existing device-capability authority instead of
    inventing a parallel device stack: rows the constraint model does not
    express (cross terms, FIR/IIR budgets, routing simultaneity, latency)
    stay ``None``/UNKNOWN unless separately evidenced.
    """
    return DspResourceEnvelope(
        envelope_id=envelope_id,
        capability_ref=constraints.capability_id,
        allowed_output_groups=tuple(constraints.allowed_physical_outputs),
        max_cross_terms=cross_term_limit,
        max_iir_biquads_total=(
            None
            if constraints.max_filters_per_channel is None
            else None  # per-channel cap is not a total budget — stays UNKNOWN
        ),
        min_gain_db=constraints.min_gain_db,
        max_gain_db=constraints.max_gain_db,
        max_delay_s=constraints.max_delay_s,
        supported_sample_rates_hz=tuple(constraints.supported_sample_rates_hz),
        provenance=provenance,
    )


def evaluate_dsp_feasibility(
    plan: ActiveLowFrequencyControlPlan,
    envelope: DspResourceEnvelope,
    *,
    filter_demands: Sequence[FilterPathDemand] = (),
) -> DspFeasibilityReport:
    """Check a control plan against declared target resources.

    Each row is a separate check: outputs, cross terms, processing paths,
    FIR/IIR budgets, routing simultaneity, gain/delay range and latency
    budget. A ``None`` envelope row is UNKNOWN — the device did not
    disclose the limit, so feasibility there is unproven, never assumed.
    """
    enabled = [e for e in plan.matrix if e.enabled]
    checks: list[EligibilityCheck] = []

    # ---- outputs ----------------------------------------------------
    output_ids = set(plan.physical_output_groups)
    if envelope.allowed_output_groups:
        unresolved = sorted(output_ids - set(envelope.allowed_output_groups))
        checks.append(
            EligibilityCheck(
                check='allowed_outputs',
                status='FAIL' if unresolved else 'PASS',
                detail=(
                    'plan output groups not in the declared set: '
                    + ', '.join(unresolved)
                    if unresolved
                    else 'all output groups are declared on the target'
                ),
            )
        )
    if envelope.available_output_count is None:
        checks.append(
            EligibilityCheck(
                check='output_count',
                status='UNKNOWN',
                detail='target output capacity is not declared',
            )
        )
    else:
        ok = len(output_ids) <= envelope.available_output_count
        checks.append(
            EligibilityCheck(
                check='output_count',
                status='PASS' if ok else 'FAIL',
                detail=(
                    f'{len(output_ids)} output group(s) vs '
                    f'{envelope.available_output_count} available'
                ),
            )
        )

    # ---- cross terms -------------------------------------------------
    cross_terms = [e for e in enabled if e.input_group != e.output_group]
    if envelope.max_cross_terms is None:
        checks.append(
            EligibilityCheck(
                check='cross_terms',
                status='UNKNOWN' if cross_terms else 'PASS',
                detail=(
                    'target cross-term capacity is not declared'
                    if cross_terms
                    else 'plan carries no cross-channel terms'
                ),
            )
        )
    else:
        ok = len(cross_terms) <= envelope.max_cross_terms
        checks.append(
            EligibilityCheck(
                check='cross_terms',
                status='PASS' if ok else 'FAIL',
                detail=(
                    f'{len(cross_terms)} cross term(s) vs '
                    f'{envelope.max_cross_terms} supported'
                ),
            )
        )

    # ---- processing paths --------------------------------------------
    if envelope.max_processing_paths is None:
        checks.append(
            EligibilityCheck(
                check='processing_paths',
                status='UNKNOWN' if enabled else 'PASS',
                detail=(
                    'target processing-path capacity is not declared'
                    if enabled
                    else 'plan carries no enabled paths'
                ),
            )
        )
    else:
        ok = len(enabled) <= envelope.max_processing_paths
        checks.append(
            EligibilityCheck(
                check='processing_paths',
                status='PASS' if ok else 'FAIL',
                detail=(
                    f'{len(enabled)} enabled path(s) vs '
                    f'{envelope.max_processing_paths} supported'
                ),
            )
        )

    # ---- routing simultaneity ----------------------------------------
    route_targets = {e.output_group for e in enabled}
    if envelope.max_simultaneous_routes is None:
        checks.append(
            EligibilityCheck(
                check='routing_simultaneity',
                status='UNKNOWN' if route_targets else 'PASS',
                detail=(
                    'target routing-simultaneity limit is not declared'
                    if route_targets
                    else 'plan carries no routes'
                ),
            )
        )
    else:
        ok = len(route_targets) <= envelope.max_simultaneous_routes
        checks.append(
            EligibilityCheck(
                check='routing_simultaneity',
                status='PASS' if ok else 'FAIL',
                detail=(
                    f'{len(route_targets)} routed output(s) vs '
                    f'{envelope.max_simultaneous_routes} simultaneous'
                ),
            )
        )

    # ---- filter budgets ----------------------------------------------
    demand_map = {(d.input_group, d.output_group): d for d in filter_demands}
    fir_paths = [e for e in enabled if e.filter_kind == 'fir']
    iir_paths = [e for e in enabled if e.filter_kind in ('iir', 'peq')]

    def _fir_check() -> EligibilityCheck:
        if not fir_paths:
            return EligibilityCheck(
                check='fir_budget', status='PASS', detail='plan carries no FIR paths'
            )
        if (
            envelope.max_fir_taps_total is None
            and envelope.max_fir_taps_per_path is None
        ):
            return EligibilityCheck(
                check='fir_budget',
                status='UNKNOWN',
                detail='target FIR tap budget is not declared',
            )
        unknown_demand = [
            f'{e.input_group}->{e.output_group}'
            for e in fir_paths
            if demand_map.get((e.input_group, e.output_group)) is None
            or demand_map[(e.input_group, e.output_group)].fir_taps is None
        ]
        if unknown_demand:
            return EligibilityCheck(
                check='fir_budget',
                status='UNKNOWN',
                detail='FIR tap demand undeclared for: ' + ', '.join(unknown_demand),
            )
        if envelope.max_fir_taps_per_path is not None:
            over = [
                f'{e.input_group}->{e.output_group}'
                for e in fir_paths
                if demand_map[(e.input_group, e.output_group)].fir_taps
                > envelope.max_fir_taps_per_path
            ]
            if over:
                return EligibilityCheck(
                    check='fir_budget',
                    status='FAIL',
                    detail='per-path FIR tap budget exceeded by: ' + ', '.join(over),
                )
        if envelope.max_fir_taps_total is not None:
            total = sum(
                demand_map[(e.input_group, e.output_group)].fir_taps
                for e in fir_paths
            )
            if total > envelope.max_fir_taps_total:
                return EligibilityCheck(
                    check='fir_budget',
                    status='FAIL',
                    detail=(
                        f'FIR tap demand {total} exceeds total budget '
                        f'{envelope.max_fir_taps_total}'
                    ),
                )
        return EligibilityCheck(
            check='fir_budget', status='PASS', detail='FIR demand fits declared budget'
        )

    def _iir_check() -> EligibilityCheck:
        if not iir_paths:
            return EligibilityCheck(
                check='iir_budget', status='PASS', detail='plan carries no IIR paths'
            )
        if envelope.max_iir_biquads_total is None:
            return EligibilityCheck(
                check='iir_budget',
                status='UNKNOWN',
                detail='target IIR biquad budget is not declared',
            )
        unknown_demand = [
            f'{e.input_group}->{e.output_group}'
            for e in iir_paths
            if demand_map.get((e.input_group, e.output_group)) is None
            or demand_map[(e.input_group, e.output_group)].iir_biquads is None
        ]
        if unknown_demand:
            return EligibilityCheck(
                check='iir_budget',
                status='UNKNOWN',
                detail='IIR biquad demand undeclared for: ' + ', '.join(unknown_demand),
            )
        total = sum(
            demand_map[(e.input_group, e.output_group)].iir_biquads
            for e in iir_paths
        )
        if total > envelope.max_iir_biquads_total:
            return EligibilityCheck(
                check='iir_budget',
                status='FAIL',
                detail=(
                    f'IIR biquad demand {total} exceeds budget '
                    f'{envelope.max_iir_biquads_total}'
                ),
            )
        return EligibilityCheck(
            check='iir_budget', status='PASS', detail='IIR demand fits declared budget'
        )

    checks.append(_fir_check())
    checks.append(_iir_check())

    # ---- gain / delay / latency ---------------------------------------
    def _range_check(
        name: str,
        values: list[float | None],
        minimum: float | None,
        maximum: float | None,
    ) -> EligibilityCheck:
        concrete = [v for v in values if v is not None]
        if not concrete:
            return EligibilityCheck(
                check=name, status='PASS', detail=f'plan declares no {name} demands'
            )
        if minimum is None and maximum is None:
            return EligibilityCheck(
                check=name,
                status='UNKNOWN',
                detail=f'target {name} limits are not declared',
            )
        violations = [
            v
            for v in concrete
            if (minimum is not None and v < minimum)
            or (maximum is not None and v > maximum)
        ]
        return EligibilityCheck(
            check=name,
            status='FAIL' if violations else 'PASS',
            detail=(
                f'{len(violations)} demand(s) outside the declared range'
                if violations
                else f'all {name} demands inside the declared range'
            ),
        )

    checks.append(
        _range_check(
            'gain_range',
            [e.gain_db for e in enabled],
            envelope.min_gain_db,
            envelope.max_gain_db,
        )
    )
    checks.append(
        _range_check(
            'delay_range',
            [e.delay_s for e in enabled],
            None,
            envelope.max_delay_s,
        )
    )
    checks.append(
        _range_check(
            'latency_budget',
            [plan.total_latency_s],
            None,
            envelope.latency_budget_s,
        )
    )

    combined = _combine_status(tuple(c.status for c in checks))
    verdict = (
        'compatible'
        if combined == 'PASS'
        else ('incompatible' if combined == 'FAIL' else 'unknown')
    )
    probe = {
        'plan_sha256': plan.plan_sha256,
        'envelope_id': envelope.envelope_id,
        'verdict': verdict,
        'checks': [c.model_dump(mode='json') for c in checks],
    }
    return DspFeasibilityReport(
        plan_sha256=plan.plan_sha256,
        envelope_id=envelope.envelope_id,
        verdict=verdict,
        checks=tuple(checks),
        report_sha256=_hash(probe),
    )


# ---------------------------------------------------------------------------
# Strategy classification
# ---------------------------------------------------------------------------

def classify_lf_control_strategy(
    *,
    bass_management_present: bool = False,
    control_plan: ActiveLowFrequencyControlPlan | None = None,
    groups: Sequence[SpeakerGroupDecl] = (),
    evidence: Mapping[str, SourceCapabilityEvidence] | None = None,
) -> ActiveControlStrategy:
    """Classify the LF-control authority actually present.

    Distinctions that must never blur:

    - ``bass_management_only`` — crossover/redirect authority with no
      control plan at all.
    - ``multi_sub_sum_optimization`` — diagonal/content-feed paths into
      multi-emitter groups (per-sub gain/delay/polarity/crossover). Not
      "active treatment".
    - ``cross_channel_support_control`` — at least one path whose emitters
      belong to a different physical group than the supported channel.
    - ``wavefront_active_control`` — cross-channel support *plus* at least
      one group actually eligible as a boundary-declared array and a plan
      objective of ``wavefront_control``. Both conditions are required;
      neither alone is sufficient.
    - ``external_proprietary_control`` — vendor-opaque bindings only;
      HTDT did not generate the internals.
    """
    if control_plan is None:
        return 'bass_management_only' if bass_management_present else 'none'
    if control_plan.representation == 'vendor_opaque':
        return 'external_proprietary_control'
    if control_plan.representation == 'unknown':
        return 'unknown'

    by_id = {g.group_id: g for g in groups}
    enabled = [e for e in control_plan.matrix if e.enabled]
    path_kinds = {
        e: classify_matrix_path(e, by_id.get(e.input_group), by_id.get(e.output_group))
        for e in enabled
    }
    kinds = set(path_kinds.values())
    has_support = 'cross_channel_support' in kinds or (
        'support_speaker' in control_plan.objectives
        and any(e.input_group != e.output_group for e in enabled)
    )
    has_multi = any(
        path_kinds[e] in ('diagonal', 'content_feed')
        and len(by_id[e.output_group].member_entity_ids) >= 2
        for e in enabled
        if e.output_group in by_id
    )
    if has_support and 'wavefront_control' in control_plan.objectives and evidence:
        array_ok = any(
            evaluate_source_group_eligibility(
                g, evidence, control_plan.control_band_hz,
                strategy='wavefront_array',
            ).verdict == 'eligible'
            for g in groups
        )
        if array_ok:
            return 'wavefront_active_control'
    if has_support:
        return 'cross_channel_support_control'
    if has_multi:
        return 'multi_sub_sum_optimization'
    if enabled:
        return 'independent_channel_eq'
    return 'bass_management_only' if bass_management_present else 'none'


# ---------------------------------------------------------------------------
# Multi-seat LF objective plumbing
# ---------------------------------------------------------------------------

LF_OBJECTIVE_MODEL_ID = 'lf-control-plan-objective-1'


def lf_control_evaluation_spec(
    plan: ActiveLowFrequencyControlPlan,
    seat_entity_ids: Sequence[str],
) -> dict[str, Any]:
    """Canonical spec identifying an LF objective evaluation — embed as
    ``evaluation_spec_json`` in an O30 ``CadObjectiveEvaluation`` so the
    persisted vector replays against the exact plan/band/seats."""
    return {
        'plan_sha256': plan.plan_sha256,
        'control_band_hz': list(plan.control_band_hz),
        'seat_entity_ids': list(seat_entity_ids),
        'model_id': LF_OBJECTIVE_MODEL_ID,
    }


def _bind_plan_definition(
    metric: ObjectiveMetric,
    binding_sha256: str,
) -> ObjectiveMetric:
    definition = ObjectiveDefinition(
        objective_id=metric.objective_id,
        quantity=metric.objective_id,
        unit=metric.unit,
        direction=metric.direction,
        valid_domain=ObjectiveValidDomain(kind='bounded_real', minimum=0.0),
        comparison_model_id=LF_OBJECTIVE_MODEL_ID,
        comparison_model_version=binding_sha256,
    )
    return ObjectiveMetric(
        objective_id=metric.objective_id,
        value=metric.value,
        unit=metric.unit,
        direction=metric.direction,
        state=metric.state,
        definition=definition,
    )


def lf_control_objectives(
    plan: ActiveLowFrequencyControlPlan,
    candidate_id: str,
    seat_responses: Sequence[FrequencyResponse],
    *,
    seat_entity_ids: Sequence[str] | None = None,
    target_response: FrequencyResponse | None = None,
    priority_profile: SeatPriorityProfile | None = None,
) -> ObjectiveVector:
    """Multi-seat LF objectives for one control-plan design candidate.

    Restricts the existing independent objective authority to the plan's
    control band:

    - seat-to-seat spread via :func:`seat_pairwise_objectives`
      (``lf.seat.pairwise_*`` metrics — only when >=2 seat responses);
    - per-seat target-response objectives when ``target_response`` is
      supplied (``lf.seat.<seat>.rms_difference_db`` etc.), plus
      ``lf.mean_seat_rms_difference_db`` / ``lf.max_seat_rms_difference_db``
      aggregates — average response error and seat spread stay separate
      metrics, never one opaque score;
    - every produced metric's ``ObjectiveDefinition`` pins the plan hash,
      control band and seat identities, so O30 evaluations and Pareto
      comparisons trace to the exact design.
    """
    spec = ResponseObjectiveSpec(
        low_hz=plan.control_band_hz[0],
        high_hz=plan.control_band_hz[1],
        reference_band_hz=plan.control_band_hz,
    )
    ids = tuple(seat_entity_ids) if seat_entity_ids is not None else None

    binding_sha256 = _hash(
        lf_control_evaluation_spec(plan, list(ids) if ids else [])
    )
    vectors: list[ObjectiveVector] = []

    if len(seat_responses) >= 2:
        vectors.append(
            seat_pairwise_objectives(
                candidate_id,
                seat_responses,
                spec,
                prefix='lf.seat',
                seat_entity_ids=ids,
                priority_profile=priority_profile,
            )
        )

    if target_response is not None:
        per_seat: list[ObjectiveVector] = []
        seat_ids = ids or tuple(
            f'seat-{index}' for index in range(len(seat_responses))
        )
        rms_values: list[float] = []
        for seat_id, response in zip(seat_ids, seat_responses, strict=True):
            vector = target_response_objectives(
                candidate_id,
                response,
                target_response,
                spec,
                prefix=f'lf.seat.{seat_id}',
            )
            per_seat.append(vector)
            rms_values.append(
                float(vector.metric(f'lf.seat.{seat_id}.rms_difference_db').value)
            )
        merged = merge_objective_vectors(candidate_id, *vectors, *per_seat)
        metrics = list(merged.metrics)
        metrics.extend(
            (
                ObjectiveMetric(
                    objective_id='lf.mean_seat_rms_difference_db',
                    value=sum(rms_values) / len(rms_values),
                    unit='dB',
                ),
                ObjectiveMetric(
                    objective_id='lf.max_seat_rms_difference_db',
                    value=max(rms_values),
                    unit='dB',
                ),
            )
        )
        return ObjectiveVector(
            candidate_id=candidate_id,
            metrics=tuple(
                _bind_plan_definition(metric, binding_sha256)
                for metric in metrics
            ),
        )

    if not vectors:
        raise ValueError(
            'lf_control_objectives requires at least two seat responses, '
            'or a target response for per-seat objectives'
        )
    merged = merge_objective_vectors(candidate_id, *vectors)
    return ObjectiveVector(
        candidate_id=candidate_id,
        metrics=tuple(
            _bind_plan_definition(metric, binding_sha256)
            for metric in merged.metrics
        ),
    )
