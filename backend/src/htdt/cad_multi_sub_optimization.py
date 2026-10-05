"""REV55-MULTISUB: conventional multi-subwoofer optimization qualification (#569).

The vendor-neutral evidence contract for **conventional** multi-subwoofer
optimization — placement, per-sub gain, delay/polarity and bounded EQ used
to reduce seat-to-seat low-frequency response variation across a listening
area (Welti & Devantier, JAES 54(5) 2006: optimization means the seat
responses become *as similar as possible* so a single global EQ can flatten
the whole area; Welti AES-133 2012: consistency and bass efficiency are
separate figures of merit — never one hidden "bass score").

``MULTI_SUB_SUM_OPTIMIZATION`` is deliberately separate from the active
low-frequency control authority (#533/#973 ``ActiveLowFrequencyControlPlan``,
which owns cross-channel support / MIMO / wavefront / vendor-proprietary
concepts). A conventional candidate may not be labelled ART, WaveForming,
MIMO room control or active absorption — :func:`assert_conventional_strategy`
fails closed on those labels.

Contract highlights:

* exact signal-path identity — every routing, polarity, crossover or
  placement change produces a new ``MultiSubCandidate`` hash;
* the optimization / spatial-holdout / repeatability seat partition is part
  of the candidate identity and is disjoint by construction;
* seat weighting is explicit authority (``uniform_declared`` /
  ``explicit_weights`` / ``seat_priority_profile``) — raw per-seat
  observables are always preserved, never hidden behind a weighted score;
* optimization-seat and holdout-seat metrics are reported separately and a
  ``listening_region`` claim is impossible without holdout evidence;
* finalists must be compared at one declared evaluator fidelity — a cheaper
  model can never win a comparison by existing;
* deployment verification binds the observed (read-back) DSP state and
  post-deployment remeasurement at every declared seat; a sub that cannot
  physically be installed makes the candidate *infeasible*, not
  "best theoretical".

UNKNOWN stays UNKNOWN: missing gain/delay/capability/holdout evidence is an
explicit limitation, never a silently-substituted nominal value.
"""

from __future__ import annotations

import math
from typing import Any, Literal, NamedTuple, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_scene import Position3
from .comparison import FrequencyResponse, _grid, _interpolate_many
from .canonical_json import (
    canonical_json,
    canonical_sha256 as _hash,
    canonicalize_payload,
)


# --- authority identity -------------------------------------------------

MULTI_SUB_SCHEMA_VERSION = 1
MULTI_SUB_AUTHORITY_VERSION = 'rev55-multi-sub-optimization-1'
MULTI_SUB_EVALUATION_VERSION = 'rev55-multi-sub-evaluation-1'
MULTI_SUB_QUALIFICATION_VERSION = 'rev55-multi-sub-qualification-1'
MULTI_SUB_DEPLOYMENT_VERSION = 'rev55-multi-sub-deployment-1'

#: Active-control strategy labels owned by #533 — the conventional
#: multi-sub authority must reject them (#569 section 1).
ACTIVE_CONTROL_STRATEGY_LABELS: tuple[str, ...] = (
    'cross_channel_support_control',
    'wavefront_active_control',
    'external_proprietary_control',
)

#: Default evaluation bands for the low-frequency seat-consistency problem.
#: The modal region of small rooms — where Welti & Devantier locate the
#: seat-to-seat variation problem — lives below the modal transition;
#: 20-160 Hz in half-octave bands covers the usual subwoofer/management
#: range without smearing the metric across the whole spectrum.
DEFAULT_EVALUATION_BANDS_HZ: tuple[tuple[float, float], ...] = (
    (20.0, 40.0),
    (40.0, 80.0),
    (80.0, 160.0),
)

#: Holdout regression tolerance. A candidate is a holdout regression when
#: it degrades a holdout seat-consistency metric by more than this many dB
#: relative to the baseline — tight enough that "improved at fitted seats,
#: worse elsewhere" (the classic multi-seat overfit) cannot pass.
DEFAULT_HOLDOUT_REGRESSION_TOLERANCE_DB = 0.5

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _finite(value: float, *, field_name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f'{field_name} must be finite')
    return result


# --- literals ------------------------------------------------------------

ConventionalMultiSubStrategy = Literal[
    'single_sub_baseline',
    'multi_sub_fixed_layout',
    'multi_sub_placement_optimized',
    'multi_sub_gain_delay_polarity_optimized',
    'multi_sub_with_bounded_eq',
]

#: Declared strategy vocabulary INCLUDING the active labels, used to build
#: a hard boundary — a conventional candidate may only carry a
#: ``ConventionalMultiSubStrategy``.
MultiSubStrategyLabel = Literal[
    'single_sub_baseline',
    'multi_sub_fixed_layout',
    'multi_sub_placement_optimized',
    'multi_sub_gain_delay_polarity_optimized',
    'multi_sub_with_bounded_eq',
    'cross_channel_support_control',
    'wavefront_active_control',
    'external_proprietary_control',
]

OptimizationVariable = Literal[
    'position_xyz',
    'gain_db',
    'delay_s',
    'polarity',
    'crossover',
    'peq',
]

SeatPopulationRole = Literal[
    'optimization',
    'holdout',
    'repeatability',
]

MultiSubEvidenceKind = Literal[
    'measured',
    'predicted',
    'synthetic',
    'unknown',
]

MultiSubWeightsMode = Literal[
    'uniform_declared',
    'explicit_weights',
    'seat_priority_profile',
]

QualificationClaim = Literal[
    'single_seat',
    'optimization_seats',
    'listening_region',
]

QualificationVerdict = Literal[
    'qualified',
    'qualified_with_limitations',
    'unqualified_insufficient_evidence',
    'failed_holdout_regression',
    'incomparable_fidelity',
]

DeploymentVerdict = Literal[
    'verified',
    'verification_incomplete',
    'infeasible_installation',
    'observed_state_mismatch',
]

StageLabel = Literal[
    'a_single_sub_baseline',
    'b_multi_sub_fixed_layout',
    'c_gain_delay_polarity',
    'd_bounded_eq',
]


def assert_conventional_strategy(label: str) -> ConventionalMultiSubStrategy:
    """Fail closed on #533 active-control vocabulary (#569 section 1).

    Conventional summed-sub optimization is never branded ART, WaveForming,
    MIMO room control or active absorption; those labels belong to the
    #533 authority, not to this record.
    """
    if label in ACTIVE_CONTROL_STRATEGY_LABELS:
        raise ValueError(
            f'strategy {label!r} is an active-control strategy owned by the '
            '#533 authority — conventional multi-sub candidates must not '
            'carry it'
        )
    conventional = ConventionalMultiSubStrategy.__args__  # type: ignore[attr-defined]
    if label not in conventional:
        raise ValueError(f'unknown multi-sub strategy label: {label!r}')
    return label  # type: ignore[return-value]


#: The stage each strategy is allowed to appear in (#569 section 7).
STRATEGY_TO_STAGE: dict[str, StageLabel] = {
    'single_sub_baseline': 'a_single_sub_baseline',
    'multi_sub_fixed_layout': 'b_multi_sub_fixed_layout',
    'multi_sub_placement_optimized': 'b_multi_sub_fixed_layout',
    'multi_sub_gain_delay_polarity_optimized': 'c_gain_delay_polarity',
    'multi_sub_with_bounded_eq': 'd_bounded_eq',
}


# --- sub-channel / signal-path bindings ----------------------------------


class SubChannelBinding(BaseModel):
    """One subwoofer's exact contribution path inside a candidate.

    ``gain_db``/``delay_s`` are ``None`` when UNKNOWN — an unspecified
    channel state is recorded as unspecified, never defaulted to 0 dB/0 s.
    ``installable`` records physical-feasibility evidence: ``infeasible``
    subs make the whole candidate infeasible for deployment.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    sub_entity_id: str = Field(min_length=1)
    physical_output_id: str | None = None
    output_group_ref: str | None = None
    #: 'scene_exact' = position from the pinned SceneRevision entity;
    #: 'declared' = optimized position stated on the candidate (deployment
    #: verification must prove it installable); 'unknown' = unrecorded.
    position_state: Literal['scene_exact', 'declared', 'unknown'] = 'unknown'
    position: Position3 | None = None
    orientation_deg: float | None = None
    gain_db: float | None = None
    delay_s: float | None = Field(default=None, ge=0.0)
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'
    #: Bass-management/crossover identity bound to this sub (profile/group
    #: refs — the BassManagementProfile remains the routing authority).
    crossover_ref: str | None = None
    crossover_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    per_sub_eq_ref: str | None = None
    per_sub_eq_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    usable_output_ref: str | None = None
    installable: Literal['feasible', 'infeasible', 'unknown'] = 'unknown'

    @field_validator('gain_db', 'delay_s', 'orientation_deg')
    @classmethod
    def finite_values(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='sub channel binding field')

    @model_validator(mode='after')
    def consistent(self) -> 'SubChannelBinding':
        if self.position_state == 'unknown' and self.position is not None:
            raise ValueError('a recorded position requires a declared position_state')
        if self.position_state != 'unknown' and self.position is None:
            raise ValueError(
                f'position_state {self.position_state!r} requires the position'
            )
        return self


class BoundedEqBinding(BaseModel):
    """Shared/bounded EQ state applied over the summed sub channel.

    ``delegated_qualification_ref`` lets a #568/EQP10-style correction
    qualification authority own the EQ eligibility checks — this record
    references it, it does not duplicate it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    eq_authority_id: str = Field(min_length=1)
    eq_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    formulation: Literal['peq', 'fir', 'other', 'unknown'] = 'unknown'
    filter_count: int | None = Field(default=None, ge=0)
    max_boost_db: float | None = Field(default=None, ge=0.0)
    max_cut_db: float | None = Field(default=None, ge=0.0)
    delegated_qualification_ref: str | None = None

    @field_validator('max_boost_db', 'max_cut_db')
    @classmethod
    def finite_bounds(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='bounded EQ bound')


class MultiSubSeatWeight(BaseModel):
    """One explicit seat weight — a user/design input, never a hidden
    MLP preference (#569 section 6)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_entity_id: str = Field(min_length=1)
    weight: float = Field(gt=0.0)

    @field_validator('weight')
    @classmethod
    def finite_weight(cls, value: float) -> float:
        return _finite(value, field_name='seat weight')


class MultiSubSeatWeighting(BaseModel):
    """The seat-weighting authority for one optimization identity.

    ``uniform_declared`` is itself an explicit declaration ("every
    optimization seat counts equally") — absence of a declared weighting is
    not allowed to masquerade as anything else. A ``seat_priority_profile``
    binding reuses the #513 authority by exact id+hash.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    mode: MultiSubWeightsMode
    weights: tuple[MultiSubSeatWeight, ...] = ()
    seat_priority_profile_id: str | None = None
    seat_priority_profile_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubSeatWeighting':
        if self.mode == 'explicit_weights':
            if not self.weights:
                raise ValueError('explicit_weights mode requires weights')
            ids = [item.seat_entity_id for item in self.weights]
            if len(ids) != len(set(ids)):
                raise ValueError('seat weights must not duplicate seats')
        elif self.weights:
            raise ValueError('inline weights require explicit_weights mode')
        if self.mode == 'seat_priority_profile':
            if not (
                self.seat_priority_profile_id
                and self.seat_priority_profile_sha256
            ):
                raise ValueError(
                    'seat_priority_profile mode requires profile id+hash'
                )
        elif self.seat_priority_profile_id or self.seat_priority_profile_sha256:
            raise ValueError(
                'profile binding requires seat_priority_profile mode'
            )
        return self

    def weight_map(self) -> dict[str, float]:
        if self.mode == 'explicit_weights':
            return {item.seat_entity_id: item.weight for item in self.weights}
        return {}


class MultiSubSeatPartition(BaseModel):
    """The optimization / holdout / repeatability seat partition.

    The three populations are disjoint by construction — a seat used for
    fitting can never double as holdout evidence, and repeatability
    positions (repeat captures at fitted seats) never substitute for
    spatial holdout (#569 section 5).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    optimization_seat_entity_ids: tuple[str, ...] = Field(min_length=1)
    holdout_seat_entity_ids: tuple[str, ...] = ()
    repeatability_position_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubSeatPartition':
        opt = self.optimization_seat_entity_ids
        hold = self.holdout_seat_entity_ids
        rep = self.repeatability_position_ids
        for label, ids in (('optimization', opt), ('holdout', hold), ('repeatability', rep)):
            if len(ids) != len(set(ids)):
                raise ValueError(f'{label} seats must be unique')
        if '' in opt or '' in hold or '' in rep:
            raise ValueError('seat ids must be non-empty')
        overlap = set(opt) & set(hold)
        if overlap:
            raise ValueError(
                'a seat cannot be both optimization and holdout evidence: '
                + ', '.join(sorted(overlap))
            )
        overlap_rep = set(rep) & (set(opt) | set(hold))
        if overlap_rep:
            raise ValueError(
                'repeatability positions are a separate population, not '
                'seat ids: ' + ', '.join(sorted(overlap_rep))
            )
        return self

    @property
    def partition_sha256(self) -> str:
        return _hash(
            {
                'optimization': list(self.optimization_seat_entity_ids),
                'holdout': list(self.holdout_seat_entity_ids),
                'repeatability': list(self.repeatability_position_ids),
            }
        )

    def seats_for(self, role: SeatPopulationRole) -> tuple[str, ...]:
        if role == 'optimization':
            return self.optimization_seat_entity_ids
        if role == 'holdout':
            return self.holdout_seat_entity_ids
        return self.repeatability_position_ids

    def role_of(self, seat_entity_id: str) -> SeatPopulationRole | None:
        if seat_entity_id in self.optimization_seat_entity_ids:
            return 'optimization'
        if seat_entity_id in self.holdout_seat_entity_ids:
            return 'holdout'
        if seat_entity_id in self.repeatability_position_ids:
            return 'repeatability'
        return None


class MultiSubEvaluatorIdentity(BaseModel):
    """Who produced the evaluated numbers, and at what declared fidelity.

    ``fidelity`` is the common-fidelity token of #569 section 8: finalists
    are comparable only when their evaluations share the exact fidelity
    string — a cheaper screening model can never win a final comparison.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    fidelity: str = Field(min_length=1)
    evidence_scope: Literal['owned_room', 'validated_model', 'fixture_only'] = 'fixture_only'
    fixture_only: bool = False
    synthetic: bool = False
    production_eligible: bool = False

    @model_validator(mode='after')
    def valid_scope(self) -> 'MultiSubEvaluatorIdentity':
        if self.fixture_only:
            if self.evidence_scope != 'fixture_only' or not self.synthetic:
                raise ValueError('fixture-only evaluator must be explicitly synthetic')
            if self.production_eligible:
                raise ValueError('fixture-only evaluator cannot be production eligible')
        if self.synthetic and self.production_eligible:
            raise ValueError('synthetic evaluator cannot be production eligible')
        if self.evidence_scope == 'fixture_only' and not self.fixture_only:
            raise ValueError('fixture_only evidence scope requires fixture_only=true')
        return self


class MultiSubCandidate(BaseModel):
    """Sealed conventional multi-sub optimization candidate (#569 §2-3).

    Topology, routing, per-sub signal state, seat partition, weighting,
    declared objective set, optimized-variable list, bounded constraints and
    algorithm identity — one content hash over all of it, so any placement,
    routing, polarity, crossover or weight change is a *new* candidate.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_SUB_SCHEMA_VERSION
    authority_version: Literal[
        'rev55-multi-sub-optimization-1'
    ] = MULTI_SUB_AUTHORITY_VERSION
    candidate_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    system_variant_id: str = Field(min_length=1)
    system_variant_sha256: str = Field(pattern=_SHA256_PATTERN)

    strategy: ConventionalMultiSubStrategy
    subs: tuple[SubChannelBinding, ...] = Field(min_length=1)
    partition: MultiSubSeatPartition
    weighting: MultiSubSeatWeighting = MultiSubSeatWeighting(mode='uniform_declared')
    eq_binding: BoundedEqBinding | None = None
    bass_management_profile_id: str | None = None
    bass_management_profile_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    dsp_state_ref: str | None = None
    dsp_state_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    device_capability_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    sample_rate_hz: int | None = Field(default=None, gt=0)
    optimized_variables: tuple[OptimizationVariable, ...] = ()
    #: Declared objective family ids (independent metrics — no hidden
    #: aggregate "bass score").
    objective_family_ids: tuple[str, ...] = Field(min_length=1)
    algorithm_id: str | None = None
    algorithm_version: str | None = None
    algorithm_seed: str | None = None
    #: Hardware/headroom constraint notes (amplifier/output limits); missing
    #: hardware capability stays a hard limitation recorded here, never
    #: fabricated.
    constraint_notes: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubCandidate':
        if self.strategy not in ConventionalMultiSubStrategy.__args__:  # type: ignore[attr-defined]
            raise ValueError('conventional candidates cannot carry active-control labels')
        ids = [sub.sub_entity_id for sub in self.subs]
        if len(ids) != len(set(ids)):
            raise ValueError('sub channel bindings must not duplicate entities')
        if self.strategy == 'single_sub_baseline' and len(self.subs) != 1:
            raise ValueError('single_sub_baseline requires exactly one sub')
        if self.strategy != 'single_sub_baseline' and len(self.subs) < 2:
            raise ValueError('a multi-sub strategy requires at least two subs')
        if self.strategy == 'multi_sub_with_bounded_eq' and self.eq_binding is None:
            raise ValueError('multi_sub_with_bounded_eq requires a BoundedEqBinding')
        if self.strategy != 'multi_sub_with_bounded_eq' and self.eq_binding is not None:
            raise ValueError(
                'an EQ binding only belongs on the bounded-EQ strategy — '
                'earlier stages must keep layout/alignment un-equalized'
            )
        if bool(self.bass_management_profile_id) != bool(
            self.bass_management_profile_sha256
        ):
            raise ValueError('bass management profile id/hash must pair')
        if bool(self.dsp_state_ref) != bool(self.dsp_state_sha256):
            raise ValueError('dsp state ref/hash must pair')
        # Optimized-variable list must be consistent with the strategy
        # stage: placement optimization may vary position_xyz; the gain/
        # delay/polarity stage adds those; bounded EQ adds peq. A strategy
        # must not silently optimize variables outside its stage.
        allowed: dict[str, tuple[OptimizationVariable, ...]] = {
            'single_sub_baseline': (),
            'multi_sub_fixed_layout': (),
            'multi_sub_placement_optimized': ('position_xyz',),
            'multi_sub_gain_delay_polarity_optimized': (
                'position_xyz', 'gain_db', 'delay_s', 'polarity', 'crossover'
            ),
            'multi_sub_with_bounded_eq': (
                'position_xyz', 'gain_db', 'delay_s', 'polarity', 'crossover', 'peq'
            ),
        }
        extra = set(self.optimized_variables) - set(allowed[self.strategy])
        if extra:
            raise ValueError(
                f'strategy {self.strategy} cannot optimize {sorted(extra)}'
            )
        if len(set(self.optimized_variables)) != len(self.optimized_variables):
            raise ValueError('optimized variables must be unique')
        if len(set(self.objective_family_ids)) != len(self.objective_family_ids):
            raise ValueError('objective family ids must be unique')
        if '' in self.objective_family_ids:
            raise ValueError('objective family ids must be non-empty')
        # Explicit-weight binding must cover every optimization seat.
        if self.weighting.mode == 'explicit_weights':
            covered = set(self.weighting.weight_map())
            missing = set(self.partition.optimization_seat_entity_ids) - covered
            if missing:
                raise ValueError(
                    'explicit seat weights must cover all optimization seats: '
                    + ', '.join(sorted(missing))
                )
        if self.candidate_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-sub candidate hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'candidate_sha256'})

    def sub(self, entity_id: str) -> SubChannelBinding | None:
        return next((s for s in self.subs if s.sub_entity_id == entity_id), None)


def build_multi_sub_candidate(**kwargs: Any) -> MultiSubCandidate:
    """Assemble and seal a :class:`MultiSubCandidate`."""
    payload = {'candidate_sha256': '0' * 64, **kwargs}
    provisional = MultiSubCandidate.model_construct(
        **canonicalize_payload(MultiSubCandidate, dict(payload))
    )
    payload['candidate_sha256'] = _hash(provisional.identity_payload())
    return MultiSubCandidate(**payload)


# --- evaluation ----------------------------------------------------------


class MultiSubEvaluationSpec(BaseModel):
    """The pinned evaluation semantics for one run.

    ``evaluation_bands_hz`` are the per-band seat-consistency reporting
    bands — non-overlapping, strictly rising, inside the common overlap of
    the bound responses. ``target_*`` binds the comparison target response
    verbatim (identity, not an authority pointer) so weighted/unweighted
    mean-vs-target metrics replay exactly.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    requested_band_hz: tuple[float, float]
    evaluation_bands_hz: tuple[tuple[float, float], ...] = Field(min_length=1)
    target_frequency_hz: tuple[float, ...] = ()
    target_level_db: tuple[float, ...] = ()
    evaluator: MultiSubEvaluatorIdentity

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubEvaluationSpec':
        low, high = self.requested_band_hz
        if not (
            math.isfinite(low) and math.isfinite(high) and 0 < low < high
        ):
            raise ValueError('requested band must satisfy 0 < low < high')
        previous = 0.0
        for band_low, band_high in self.evaluation_bands_hz:
            if not (
                math.isfinite(band_low)
                and math.isfinite(band_high)
                and 0 < band_low < band_high
            ):
                raise ValueError('evaluation bands must be finite and rising')
            if band_low < previous:
                raise ValueError('evaluation bands must not overlap')
            previous = band_high
        has_target = bool(self.target_frequency_hz) or bool(self.target_level_db)
        if has_target:
            if not (self.target_frequency_hz and self.target_level_db):
                raise ValueError('target frequency and level rows must pair')
            if len(self.target_frequency_hz) != len(self.target_level_db):
                raise ValueError('target rows must have equal length')
            frequencies = self.target_frequency_hz
            if any(
                not (math.isfinite(f) and f > 0.0) for f in frequencies
            ) or any(frequencies[i] >= frequencies[i + 1] for i in range(len(frequencies) - 1)):
                raise ValueError('target frequencies must be rising and positive')
            if any(not math.isfinite(float(v)) for v in self.target_level_db):
                raise ValueError('target levels must be finite')
        return self

    @property
    def has_target(self) -> bool:
        return bool(self.target_frequency_hz)


class SeatResponseBinding(BaseModel):
    """One seat's response inside an evaluation.

    Measured seats pin the exact measurement dataset; predicted seats pin a
    prediction authority ref; ``synthetic`` marks fixture-only rows so they
    can never launder into production evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    seat_entity_id: str = Field(min_length=1)
    role: SeatPopulationRole
    evidence_kind: MultiSubEvidenceKind = 'unknown'
    measurement_id: str | None = None
    dataset_id: str | None = None
    dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    prediction_ref: str | None = None
    prediction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'SeatResponseBinding':
        if bool(self.dataset_id) != bool(self.dataset_sha256):
            raise ValueError('dataset id/hash must pair')
        if bool(self.prediction_ref) != bool(self.prediction_sha256):
            raise ValueError('prediction ref/hash must pair')
        if self.evidence_kind == 'measured' and not (
            self.measurement_id and self.dataset_id
        ):
            raise ValueError('measured evidence requires measurement+dataset binding')
        if self.evidence_kind == 'synthetic' and (
            self.measurement_id or self.dataset_id or self.prediction_ref
        ):
            raise ValueError('synthetic rows must not carry persisted bindings')
        return self


class BandSeatMetrics(BaseModel):
    """Per-band seat-consistency metrics — the Welti quantities kept
    separate from target-response and effort metrics.

    * ``mean_std_db`` — mean over the band's grid points of the across-seat
      population standard deviation (Welti's per-frequency "spatial
      variation");
    * ``msv_db2`` — mean spatial variance over the band (the SFM-style
      objective: mean of per-frequency variance, dB²);
    * ``spread_db`` — max across the band of (max seat - min seat);
    * ``worst_seat_deviation_db`` — the largest per-seat RMS deviation from
      the seat-mean over the band (sampled-worst seat);
    * ``pairwise_rms_max_db`` / ``pairwise_rms_mean_db`` — inter-seat
      pairwise spread;
    * ``per_seat_mean_db`` — raw per-seat band means, preserved verbatim.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    band_hz: tuple[float, float]
    grid_points: int = Field(ge=0)
    per_seat_mean_db: tuple[float, ...] = ()
    seat_mean_db: float | None = None
    weighted_mean_db: float | None = None
    mean_std_db: float | None = None
    msv_db2: float | None = None
    spread_db: float | None = None
    worst_seat_deviation_db: float | None = None
    pairwise_rms_max_db: float | None = None
    pairwise_rms_mean_db: float | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'BandSeatMetrics':
        low, high = self.band_hz
        if not (math.isfinite(low) and math.isfinite(high) and 0 < low < high):
            raise ValueError('band must satisfy 0 < low < high')
        for values in (
            self.per_seat_mean_db,
            (self.seat_mean_db,),
            (self.weighted_mean_db,),
            (self.mean_std_db,),
            (self.msv_db2,),
            (self.spread_db,),
            (self.worst_seat_deviation_db,),
            (self.pairwise_rms_max_db,),
            (self.pairwise_rms_mean_db,),
        ):
            for value in values:
                if value is not None:
                    _finite(float(value), field_name='band metric')
        if self.grid_points > 0 and not self.per_seat_mean_db:
            raise ValueError('a populated band requires per-seat means')
        return self


class MultiSubEffort(BaseModel):
    """Efficiency / headroom observables — reported beside (never merged
    into) the response metrics, so a flatter mean bought with worse
    headroom stays visibly a trade-off (#569 section 4)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    required_gain_db: float | None = None
    max_boost_db: float | None = Field(default=None, ge=0.0)
    headroom_margin_db: float | None = None
    clipping_margin_db: float | None = None
    per_sub_effort: tuple[tuple[str, float], ...] = ()
    effort_state: Literal['declared', 'partial', 'unknown'] = 'unknown'

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubEffort':
        for label, value in (
            ('required_gain_db', self.required_gain_db),
            ('max_boost_db', self.max_boost_db),
            ('headroom_margin_db', self.headroom_margin_db),
            ('clipping_margin_db', self.clipping_margin_db),
        ):
            if value is not None:
                _finite(float(value), field_name=label)
        sub_ids = [item[0] for item in self.per_sub_effort]
        if len(sub_ids) != len(set(sub_ids)):
            raise ValueError('per-sub effort must not duplicate subs')
        for _sid, effort in self.per_sub_effort:
            _finite(float(effort), field_name='per-sub effort')
        has_values = (
            self.required_gain_db is not None
            or self.max_boost_db is not None
            or self.headroom_margin_db is not None
            or self.clipping_margin_db is not None
            or bool(self.per_sub_effort)
        )
        if self.effort_state == 'unknown' and has_values:
            raise ValueError('declared effort values require a declared/partial state')
        if self.effort_state == 'declared' and not has_values:
            raise ValueError('declared effort requires at least one value')
        return self


class MultiSubEvaluation(BaseModel):
    """Sealed seat-population evaluation of one candidate.

    ``member_levels_db`` holds every seat's grid-sampled response verbatim —
    the raw per-seat observables the weighted aggregates must never replace.
    Band metrics are derivable from those rows, so persistence replays them
    for exact equality before trusting a stored record.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_SUB_SCHEMA_VERSION
    authority_version: Literal[
        'rev55-multi-sub-evaluation-1'
    ] = MULTI_SUB_EVALUATION_VERSION
    evaluation_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    population: SeatPopulationRole
    seat_bindings: tuple[SeatResponseBinding, ...] = Field(min_length=1)
    requested_band_hz: tuple[float, float]
    actual_band_hz: tuple[float, float]
    grid_hz: tuple[float, ...]
    member_levels_db: tuple[tuple[float, ...], ...]
    band_metrics: tuple[BandSeatMetrics, ...] = Field(min_length=1)
    per_seat_rms_difference_db: tuple[float | None, ...] = ()
    mean_rms_difference_db: float | None = None
    worst_seat_rms_difference_db: float | None = None
    weighted_rms_difference_db: float | None = None
    weights: tuple[MultiSubSeatWeight, ...] = ()
    effort: MultiSubEffort
    evaluator: MultiSubEvaluatorIdentity
    algorithm_version: Literal[
        'rev55-multi-sub-evaluation-1'
    ] = MULTI_SUB_EVALUATION_VERSION
    created_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubEvaluation':
        if len(self.seat_bindings) != len(self.member_levels_db):
            raise ValueError('seat bindings must match member level rows')
        ids = [item.seat_entity_id for item in self.seat_bindings]
        if len(ids) != len(set(ids)):
            raise ValueError('evaluation seats must be unique')
        for binding in self.seat_bindings:
            if binding.role != self.population:
                raise ValueError(
                    'every seat binding role must equal the evaluation population'
                )
        count = len(self.grid_hz)
        if count == 0 or any(len(row) != count for row in self.member_levels_db):
            raise ValueError('member level rows must match the grid length')
        for row in self.member_levels_db:
            for value in row:
                _finite(float(value), field_name='member level')
        if self.per_seat_rms_difference_db:
            if len(self.per_seat_rms_difference_db) != len(self.seat_bindings):
                raise ValueError('per-seat target metrics must match the seats')
            for value in self.per_seat_rms_difference_db:
                if value is not None:
                    _finite(float(value), field_name='per-seat rms difference')
        for value in (
            self.mean_rms_difference_db,
            self.worst_seat_rms_difference_db,
            self.weighted_rms_difference_db,
        ):
            if value is not None:
                _finite(float(value), field_name='target metric')
        if self.weighted_rms_difference_db is not None and not self.weights:
            raise ValueError('a weighted aggregate requires recorded weights')
        if self.evaluation_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-sub evaluation hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'evaluation_sha256'})

    def band_metric(self, band_hz: tuple[float, float]) -> BandSeatMetrics | None:
        for metric in self.band_metrics:
            if (
                math.isclose(metric.band_hz[0], band_hz[0])
                and math.isclose(metric.band_hz[1], band_hz[1])
            ):
                return metric
        return None


def _population_std(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _band_metric(
    band_hz: tuple[float, float],
    grid: tuple[float, ...],
    rows: tuple[tuple[float, ...], ...],
    weights: dict[str, float],
    seat_ids: tuple[str, ...],
) -> BandSeatMetrics:
    indices = [
        index
        for index, frequency in enumerate(grid)
        if band_hz[0] <= frequency <= band_hz[1]
    ]
    if not indices:
        return BandSeatMetrics(band_hz=band_hz, grid_points=0)
    seat_count = len(rows)
    per_seat_mean = tuple(
        sum(row[index] for index in indices) / len(indices) for row in rows
    )
    columns = [
        tuple(row[index] for row in rows) for index in indices
    ]
    point_stds = [_population_std(column) for column in columns]
    mean_std = sum(point_stds) / len(point_stds)
    msv = sum(std * std for std in point_stds) / len(point_stds)
    spread = max(
        max(column) - min(column) for column in columns
    )
    seat_mean = sum(per_seat_mean) / seat_count
    column_means = [sum(column) / seat_count for column in columns]
    deviations = [
        math.sqrt(
            sum(
                (row[index] - column_mean) ** 2
                for index, column_mean in zip(indices, column_means)
            )
            / len(indices)
        )
        for row in rows
    ]
    pairwise: list[float] = []
    for i in range(seat_count):
        for j in range(i + 1, seat_count):
            pairwise.append(
                math.sqrt(
                    sum(
                        (rows[i][index] - rows[j][index]) ** 2
                        for index in indices
                    )
                    / len(indices)
                )
            )
    weighted_mean: float | None = None
    if weights:
        total = sum(weights.get(seat_id, 0.0) for seat_id in seat_ids)
        if total > 0.0:
            weighted_mean = sum(
                per_seat_mean[i] * weights.get(seat_ids[i], 0.0)
                for i in range(seat_count)
            ) / total
    return BandSeatMetrics(
        band_hz=band_hz,
        grid_points=len(indices),
        per_seat_mean_db=per_seat_mean,
        seat_mean_db=seat_mean,
        weighted_mean_db=weighted_mean,
        mean_std_db=mean_std,
        msv_db2=msv,
        spread_db=spread,
        worst_seat_deviation_db=max(deviations),
        pairwise_rms_max_db=max(pairwise) if pairwise else 0.0,
        pairwise_rms_mean_db=(
            sum(pairwise) / len(pairwise) if pairwise else 0.0
        ),
    )


def evaluate_multi_sub_candidate(
    candidate: MultiSubCandidate,
    population: SeatPopulationRole,
    bindings: Sequence[SeatResponseBinding],
    seat_responses: Sequence[FrequencyResponse],
    spec: MultiSubEvaluationSpec,
    *,
    effort: MultiSubEffort | None = None,
    evaluation_id: str | None = None,
    created_at_utc: str,
) -> MultiSubEvaluation:
    """Evaluate one candidate over one declared seat population.

    ``bindings``/``seat_responses`` follow the partition's declared seat
    order for ``population`` — the caller resolves each seat's response
    (measurement dataset, prediction or synthetic row) so the sealed record
    replays exactly.
    """
    partition = candidate.partition
    declared = partition.seats_for(population)
    binding_tuple = tuple(bindings)
    if len(binding_tuple) != len(seat_responses):
        raise ValueError('each seat binding requires exactly one response')
    if len(binding_tuple) != len(declared):
        raise ValueError(
            f'population {population} declares {len(declared)} seats, '
            f'got {len(binding_tuple)} bindings'
        )
    bound_ids = tuple(item.seat_entity_id for item in binding_tuple)
    if bound_ids != declared:
        raise ValueError(
            'seat bindings must cover the declared partition seats in order'
        )
    for binding in binding_tuple:
        if binding.role != population:
            raise ValueError('seat binding role must match the population')
    if not seat_responses:
        raise ValueError('an evaluation requires at least one seat response')
    for response in seat_responses:
        if len(response.frequency_hz) < 2 or len(response.frequency_hz) != len(
            response.level_db
        ):
            raise ValueError('seat responses need at least two points')
        if any(
            not (math.isfinite(float(v)))
            for v in (*response.frequency_hz, *response.level_db)
        ):
            raise ValueError('seat response values must be finite')
    low, high = spec.requested_band_hz
    overlap_low = max(low, max(r.frequency_hz[0] for r in seat_responses))
    overlap_high = min(high, min(r.frequency_hz[-1] for r in seat_responses))
    if overlap_high <= overlap_low:
        raise ValueError('seat responses do not overlap in the requested band')
    grid = _grid(overlap_low, overlap_high)
    if len(grid) < 2:
        raise ValueError('the common overlap grid is too small to evaluate')
    rows = tuple(_interpolate_many(response, grid) for response in seat_responses)

    weight_map = candidate.weighting.weight_map()
    weights = tuple(
        MultiSubSeatWeight(
            seat_entity_id=seat_id,
            weight=weight_map.get(seat_id, 1.0),
        )
        for seat_id in bound_ids
    )
    band_metrics = tuple(
        _band_metric(band, grid, rows, weight_map, bound_ids)
        for band in spec.evaluation_bands_hz
    )

    per_seat_rms: tuple[float | None, ...] = ()
    mean_rms = worst_rms = weighted_rms = None
    if spec.has_target:
        target = FrequencyResponse(
            spec.target_frequency_hz, spec.target_level_db
        )
        target_levels = tuple(
            _interpolate_many(target, grid)
        )
        diffs: list[float] = []
        for row in rows:
            per_seat = math.sqrt(
                sum(
                    (row[index] - target_levels[index]) ** 2
                    for index in range(len(grid))
                )
                / len(grid)
            )
            diffs.append(per_seat)
        per_seat_rms = tuple(diffs)
        mean_rms = sum(diffs) / len(diffs)
        worst_rms = max(diffs)
        if weight_map:
            total = sum(weight_map.get(seat_id, 0.0) for seat_id in bound_ids)
            if total > 0.0:
                weighted_rms = sum(
                    diff * weight_map.get(seat_id, 0.0)
                    for diff, seat_id in zip(diffs, bound_ids, strict=True)
                ) / total

    payload: dict[str, Any] = {
        'schema_version': MULTI_SUB_SCHEMA_VERSION,
        'authority_version': MULTI_SUB_EVALUATION_VERSION,
        'evaluation_id': evaluation_id or f'msub-eval-{_hash({"candidate": candidate.candidate_id, "population": population, "created": created_at_utc})[:16]}',
        'candidate_id': candidate.candidate_id,
        'candidate_sha256': candidate.candidate_sha256,
        'document_id': candidate.document_id,
        'scene_revision_id': candidate.scene_revision_id,
        'population': population,
        'seat_bindings': binding_tuple,
        'requested_band_hz': spec.requested_band_hz,
        'actual_band_hz': (overlap_low, overlap_high),
        'grid_hz': grid,
        'member_levels_db': rows,
        'band_metrics': band_metrics,
        'per_seat_rms_difference_db': per_seat_rms,
        'mean_rms_difference_db': mean_rms,
        'worst_seat_rms_difference_db': worst_rms,
        'weighted_rms_difference_db': weighted_rms,
        'weights': weights,
        'effort': effort or MultiSubEffort(),
        'evaluator': spec.evaluator,
        'algorithm_version': MULTI_SUB_EVALUATION_VERSION,
        'created_at_utc': created_at_utc,
    }
    provisional = MultiSubEvaluation.model_construct(
        **canonicalize_payload(MultiSubEvaluation, dict(payload)),
        evaluation_sha256='0' * 64,
    )
    payload['evaluation_sha256'] = _hash(provisional.identity_payload())
    return MultiSubEvaluation(**payload)


# --- staged comparisons ---------------------------------------------------


class MultiSubStageEntry(BaseModel):
    """One stage of the canonical A→D comparison (#569 section 7)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    stage: StageLabel
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)


class StageTransitionAttribution(BaseModel):
    """Per-family delta between two adjacent stages.

    Improvement is attributed to the stage that produced it — spatial
    distribution (A→B), timing/level alignment (B→C) or equalization
    (C→D) — never lumped into one "optimizer gain" number.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    transition: str = Field(min_length=1)
    #: 'band' rows carry per-band seat-consistency deltas; 'response' rows
    #: carry response-level deltas (target deviation, effort) and use the
    #: evaluation's actual overlap band as ``band_hz``.
    scope: Literal['band', 'response'] = 'band'
    band_hz: tuple[float, float]
    delta_mean_std_db: float | None = None
    delta_spread_db: float | None = None
    delta_worst_seat_deviation_db: float | None = None
    delta_mean_rms_difference_db: float | None = None
    delta_worst_seat_rms_difference_db: float | None = None

    @model_validator(mode='after')
    def finite_deltas(self) -> 'StageTransitionAttribution':
        for value in (
            self.delta_mean_std_db,
            self.delta_spread_db,
            self.delta_worst_seat_deviation_db,
            self.delta_mean_rms_difference_db,
            self.delta_worst_seat_rms_difference_db,
        ):
            if value is not None:
                _finite(float(value), field_name='stage delta')
        return self


class MultiSubStageComparison(BaseModel):
    """Sealed staged A→D comparison record."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_SUB_SCHEMA_VERSION
    authority_version: Literal[
        'rev55-multi-sub-optimization-1'
    ] = MULTI_SUB_AUTHORITY_VERSION
    comparison_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    partition_sha256: str = Field(pattern=_SHA256_PATTERN)
    entries: tuple[MultiSubStageEntry, ...] = Field(min_length=2)
    attributions: tuple[StageTransitionAttribution, ...] = ()
    created_at_utc: str = Field(min_length=1)
    comparison_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubStageComparison':
        stages = [entry.stage for entry in self.entries]
        order = list(StageLabel.__args__)  # type: ignore[attr-defined]
        indexes = [order.index(stage) for stage in stages]
        if indexes != sorted(indexes) or len(set(stages)) != len(stages):
            raise ValueError('stage entries must follow the canonical A→D order')
        if self.comparison_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-sub stage comparison hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'comparison_sha256'})


def _transition_label(a: StageLabel, b: StageLabel) -> str:
    return f'{a}->{b}'


def attribute_stage_transitions(
    entries: Sequence[MultiSubStageEntry],
    evaluations: Sequence[MultiSubEvaluation],
) -> tuple[StageTransitionAttribution, ...]:
    """Compute per-family deltas between adjacent stages.

    Evaluations must be the *same population* (typically the optimization
    seats — holdout comparisons are a qualification matter) and every band
    present on both sides contributes one attribution row.
    """
    by_id = {item.evaluation_id: item for item in evaluations}
    attributions: list[StageTransitionAttribution] = []
    populations = set()
    for entry in entries:
        evaluation = by_id.get(entry.evaluation_id)
        if evaluation is None:
            raise ValueError('stage entry references an unknown evaluation')
        if evaluation.evaluation_sha256 != entry.evaluation_sha256:
            raise ValueError('stage entry evaluation hash mismatch')
        populations.add(evaluation.population)
    if len(populations) != 1:
        raise ValueError('staged comparisons require a single seat population')
    for before, after in zip(entries, entries[1:]):
        left = by_id[before.evaluation_id]
        right = by_id[after.evaluation_id]
        if left.evaluator.fidelity != right.evaluator.fidelity:
            raise ValueError(
                'staged comparisons require common fidelity between stages'
            )
        bands = [
            metric.band_hz
            for metric in left.band_metrics
            if right.band_metric(metric.band_hz) is not None
        ]
        for band in bands:
            lm = left.band_metric(band)
            rm = right.band_metric(band)
            assert lm is not None and rm is not None
            def _delta(new: float | None, old: float | None) -> float | None:
                if new is None or old is None:
                    return None
                return new - old
            attributions.append(
                StageTransitionAttribution(
                    transition=_transition_label(before.stage, after.stage),
                    scope='band',
                    band_hz=band,
                    # Deltas follow ImprovementDelta convention:
                    # later-stage minus earlier — negative is improvement.
                    delta_mean_std_db=_delta(rm.mean_std_db, lm.mean_std_db),
                    delta_spread_db=_delta(rm.spread_db, lm.spread_db),
                    delta_worst_seat_deviation_db=_delta(
                        rm.worst_seat_deviation_db, lm.worst_seat_deviation_db
                    ),
                )
            )
        if (
            left.mean_rms_difference_db is not None
            and right.mean_rms_difference_db is not None
        ) or (
            left.worst_seat_rms_difference_db is not None
            and right.worst_seat_rms_difference_db is not None
        ):
            def _delta(new: float | None, old: float | None) -> float | None:
                if new is None or old is None:
                    return None
                return new - old
            attributions.append(
                StageTransitionAttribution(
                    transition=_transition_label(before.stage, after.stage),
                    scope='response',
                    band_hz=left.actual_band_hz,
                    delta_mean_rms_difference_db=_delta(
                        right.mean_rms_difference_db,
                        left.mean_rms_difference_db,
                    ),
                    delta_worst_seat_rms_difference_db=_delta(
                        right.worst_seat_rms_difference_db,
                        left.worst_seat_rms_difference_db,
                    ),
                )
            )
    return tuple(attributions)


def build_multi_sub_stage_comparison(
    *,
    comparison_id: str,
    document_id: str,
    scene_revision_id: str,
    entries: Sequence[MultiSubStageEntry],
    evaluations: Sequence[MultiSubEvaluation],
    partition_sha256: str,
    created_at_utc: str,
) -> MultiSubStageComparison:
    attributions = attribute_stage_transitions(entries, evaluations)
    payload: dict[str, Any] = {
        'schema_version': MULTI_SUB_SCHEMA_VERSION,
        'authority_version': MULTI_SUB_AUTHORITY_VERSION,
        'comparison_id': comparison_id,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'partition_sha256': partition_sha256,
        'entries': tuple(entries),
        'attributions': attributions,
        'created_at_utc': created_at_utc,
    }
    provisional = MultiSubStageComparison.model_construct(
        **canonicalize_payload(MultiSubStageComparison, dict(payload)),
        comparison_sha256='0' * 64,
    )
    payload['comparison_sha256'] = _hash(provisional.identity_payload())
    return MultiSubStageComparison(**payload)


# --- qualification ---------------------------------------------------------


class QualificationReason(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    blocking: bool = True


class ImprovementDelta(BaseModel):
    """Per-family improvement of candidate vs baseline on one seat
    population — signed deltas, never collapsed into one score."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    population: SeatPopulationRole
    #: 'band' rows carry per-band seat-consistency deltas; 'response' rows
    #: carry response-level deltas (target deviation, effort) and use the
    #: baseline evaluation's actual overlap band as ``band_hz``.
    scope: Literal['band', 'response'] = 'band'
    band_hz: tuple[float, float]
    delta_mean_std_db: float | None = None
    delta_spread_db: float | None = None
    delta_worst_seat_deviation_db: float | None = None
    delta_mean_rms_difference_db: float | None = None
    delta_worst_seat_rms_difference_db: float | None = None
    delta_required_gain_db: float | None = None
    delta_headroom_margin_db: float | None = None

    @model_validator(mode='after')
    def finite_deltas(self) -> 'ImprovementDelta':
        for value in (
            self.delta_mean_std_db,
            self.delta_spread_db,
            self.delta_worst_seat_deviation_db,
            self.delta_mean_rms_difference_db,
            self.delta_worst_seat_rms_difference_db,
            self.delta_required_gain_db,
            self.delta_headroom_margin_db,
        ):
            if value is not None:
                _finite(float(value), field_name='improvement delta')
        return self


class MultiSubQualification(BaseModel):
    """Sealed qualification verdict for candidate-vs-baseline (#569 §4-9).

    The verdict is *recomputable*: ``evaluate_qualification`` derives it
    deterministically from the bound evaluations and the declared claim, and
    the record is invalid if its stored verdict disagrees — a qualification
    cannot be pardoned by editing the payload.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_SUB_SCHEMA_VERSION
    authority_version: Literal[
        'rev55-multi-sub-qualification-1'
    ] = MULTI_SUB_QUALIFICATION_VERSION
    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    baseline_candidate_id: str = Field(min_length=1)
    baseline_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    partition_sha256: str = Field(pattern=_SHA256_PATTERN)
    claim: QualificationClaim
    baseline_optimization_evaluation_id: str = Field(min_length=1)
    baseline_optimization_evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_optimization_evaluation_id: str = Field(min_length=1)
    candidate_optimization_evaluation_sha256: str = Field(pattern=_SHA256_PATTERN)
    baseline_holdout_evaluation_id: str | None = None
    baseline_holdout_evaluation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    candidate_holdout_evaluation_id: str | None = None
    candidate_holdout_evaluation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    holdout_regression_tolerance_db: float = DEFAULT_HOLDOUT_REGRESSION_TOLERANCE_DB
    verdict: QualificationVerdict
    effective_claim: QualificationClaim
    reasons: tuple[QualificationReason, ...] = ()
    improvements: tuple[ImprovementDelta, ...] = ()
    created_at_utc: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubQualification':
        for ref, sha in (
            (self.baseline_holdout_evaluation_id, self.baseline_holdout_evaluation_sha256),
            (self.candidate_holdout_evaluation_id, self.candidate_holdout_evaluation_sha256),
        ):
            if bool(ref) != bool(sha):
                raise ValueError('holdout evaluation ref/hash must pair')
        if self.holdout_regression_tolerance_db < 0:
            raise ValueError('holdout regression tolerance must be >= 0')
        if self.claim == 'listening_region' and self.effective_claim == 'listening_region' and (
            not self.baseline_holdout_evaluation_id
            or not self.candidate_holdout_evaluation_id
        ):
            raise ValueError(
                'a listening_region claim cannot stand without holdout '
                'evidence on both sides'
            )
        if self.qualification_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-sub qualification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'qualification_sha256'})


class _ClaimGate(NamedTuple):
    verdict: QualificationVerdict
    effective_claim: QualificationClaim
    reasons: tuple[QualificationReason, ...]


def _pop_std_metric(evaluation: MultiSubEvaluation) -> float:
    """Band-aggregated seat-consistency scalar for gate decisions.

    Uses the RMS of the per-band ``mean_std_db`` so every declared band
    weighs equally — the gate never hides a degraded band inside an
    average.
    """
    values = [
        metric.mean_std_db
        for metric in evaluation.band_metrics
        if metric.mean_std_db is not None
    ]
    if not values:
        raise ValueError('evaluation has no seat-consistency bands')
    return math.sqrt(sum(v * v for v in values) / len(values))


def evaluate_qualification(
    *,
    claim: QualificationClaim,
    baseline_optimization: MultiSubEvaluation,
    candidate_optimization: MultiSubEvaluation,
    baseline_holdout: MultiSubEvaluation | None,
    candidate_holdout: MultiSubEvaluation | None,
    holdout_regression_tolerance_db: float = DEFAULT_HOLDOUT_REGRESSION_TOLERANCE_DB,
) -> tuple[QualificationVerdict, QualificationClaim, tuple[QualificationReason, ...], tuple[ImprovementDelta, ...]]:
    """Derive the qualification verdict deterministically.

    Gates, in order:

    1. populations must match their declared role on each side;
    2. common fidelity — any differing evaluator ``fidelity`` across the
       bound evaluations makes the pair incomparable (#569 §8);
    3. partition identity — every bound evaluation must belong to one
       partition (the candidate hash pins it);
    4. a ``listening_region`` claim requires holdout evidence on both
       sides; missing evidence downgrades the effective claim to
       ``optimization_seats`` with an explicit reason;
    5. holdout regression — candidate holdout seat-consistency worse than
       baseline beyond the declared tolerance fails the qualification
       (the MSB60 overfit case);
    6. otherwise qualified, or qualified_with_limitations when the claim
       had to be downgraded or per-family trade-offs remain visible.
    """
    reasons: list[QualificationReason] = []
    improvements: list[ImprovementDelta] = []

    pairs = (
        ('baseline', baseline_optimization, baseline_holdout),
        ('candidate', candidate_optimization, candidate_holdout),
    )
    for side, opt_eval, hold_eval in pairs:
        if opt_eval.population != 'optimization':
            raise ValueError(f'{side} optimization evaluation must bind the optimization population')
        if hold_eval is not None and hold_eval.population != 'holdout':
            raise ValueError(f'{side} holdout evaluation must bind the holdout population')

    evaluations = [
        e
        for e in (
            baseline_optimization,
            candidate_optimization,
            baseline_holdout,
            candidate_holdout,
        )
        if e is not None
    ]
    fidelities = {item.evaluator.fidelity for item in evaluations}
    if len(fidelities) != 1:
        reasons.append(
            QualificationReason(
                code='fidelity_mismatch',
                detail='bound evaluations were computed at different fidelities — '
                'finalists cannot be compared across models',
            )
        )
        return 'incomparable_fidelity', 'single_seat', tuple(reasons), ()

    baseline_partitions = {
        e.candidate_sha256 for e in (baseline_optimization, baseline_holdout) if e is not None
    }
    candidate_partitions = {
        e.candidate_sha256 for e in (candidate_optimization, candidate_holdout) if e is not None
    }
    if len(baseline_partitions) != 1 or len(candidate_partitions) != 1:
        raise ValueError('each side must evaluate exactly one candidate')

    def _collect_improvements(
        population: SeatPopulationRole,
        before: MultiSubEvaluation,
        after: MultiSubEvaluation,
    ) -> None:
        for metric in before.band_metrics:
            other = after.band_metric(metric.band_hz)
            if other is None:
                continue
            def _d(new: float | None, old: float | None) -> float | None:
                return None if new is None or old is None else new - old
            improvements.append(
                ImprovementDelta(
                    population=population,
                    scope='band',
                    band_hz=metric.band_hz,
                    delta_mean_std_db=_d(other.mean_std_db, metric.mean_std_db),
                    delta_spread_db=_d(other.spread_db, metric.spread_db),
                    delta_worst_seat_deviation_db=_d(
                        other.worst_seat_deviation_db,
                        metric.worst_seat_deviation_db,
                    ),
                )
            )
        response_deltas = (
            _d(after.mean_rms_difference_db, before.mean_rms_difference_db),
            _d(
                after.worst_seat_rms_difference_db,
                before.worst_seat_rms_difference_db,
            ),
            _d(after.effort.required_gain_db, before.effort.required_gain_db),
            _d(after.effort.headroom_margin_db, before.effort.headroom_margin_db),
        )
        if any(value is not None for value in response_deltas):
            improvements.append(
                ImprovementDelta(
                    population=population,
                    scope='response',
                    band_hz=before.actual_band_hz,
                    delta_mean_rms_difference_db=response_deltas[0],
                    delta_worst_seat_rms_difference_db=response_deltas[1],
                    delta_required_gain_db=response_deltas[2],
                    delta_headroom_margin_db=response_deltas[3],
                )
            )

    _collect_improvements('optimization', baseline_optimization, candidate_optimization)

    have_holdout = baseline_holdout is not None and candidate_holdout is not None
    if have_holdout:
        _collect_improvements('holdout', baseline_holdout, candidate_holdout)  # type: ignore[arg-type]
    if claim == 'listening_region' and not have_holdout:
        reasons.append(
            QualificationReason(
                code='missing_holdout_evidence',
                detail='a listening_region claim requires holdout-seat '
                'evaluations on both baseline and candidate — '
                'optimization-seat evidence alone cannot qualify a '
                'multi-seat claim',
            )
        )
    if not have_holdout and claim != 'listening_region':
        reasons.append(
            QualificationReason(
                code='no_holdout_evidence',
                detail='no holdout evaluations bound — the result is limited '
                'to fitted-seat evidence',
                blocking=False,
            )
        )

    if have_holdout:
        before = _pop_std_metric(baseline_holdout)  # type: ignore[arg-type]
        after = _pop_std_metric(candidate_holdout)  # type: ignore[arg-type]
        if after - before > holdout_regression_tolerance_db:
            reasons.append(
                QualificationReason(
                    code='holdout_seat_consistency_regression',
                    detail=(
                        f'holdout seat-to-seat std worsened by '
                        f'{after - before:.2f} dB (tolerance '
                        f'{holdout_regression_tolerance_db:.2f} dB) — '
                        'improvement at fitted seats did not generalize'
                    ),
                )
            )
            return 'failed_holdout_regression', 'optimization_seats' if claim != 'single_seat' else 'single_seat', tuple(reasons), tuple(improvements)

        # Target-response holdout regression check (worst seat, when a
        # target was bound).
        if (
            baseline_holdout.worst_seat_rms_difference_db is not None  # type: ignore[union-attr]
            and candidate_holdout.worst_seat_rms_difference_db is not None  # type: ignore[union-attr]
        ):
            delta = (
                candidate_holdout.worst_seat_rms_difference_db
                - baseline_holdout.worst_seat_rms_difference_db
            )
            if delta > holdout_regression_tolerance_db:
                reasons.append(
                    QualificationReason(
                        code='holdout_worst_seat_regression',
                        detail=(
                            f'holdout worst-seat target deviation worsened '
                            f'by {delta:.2f} dB beyond tolerance'
                        ),
                    )
                )
                return 'failed_holdout_regression', 'optimization_seats' if claim != 'single_seat' else 'single_seat', tuple(reasons), tuple(improvements)

    # Effective claim and verdict.
    if claim == 'listening_region' and have_holdout:
        effective: QualificationClaim = 'listening_region'
        verdict: QualificationVerdict = 'qualified'
    elif claim == 'listening_region' and not have_holdout:
        effective = 'optimization_seats'
        verdict = 'qualified_with_limitations'
    elif claim == 'optimization_seats':
        effective = 'optimization_seats'
        verdict = 'qualified' if have_holdout else 'qualified_with_limitations'
    else:
        effective = 'single_seat'
        verdict = 'qualified' if have_holdout else 'qualified_with_limitations'
    return verdict, effective, tuple(reasons), tuple(improvements)


def build_multi_sub_qualification(
    *,
    qualification_id: str,
    baseline: MultiSubCandidate,
    candidate: MultiSubCandidate,
    claim: QualificationClaim,
    baseline_optimization: MultiSubEvaluation,
    candidate_optimization: MultiSubEvaluation,
    baseline_holdout: MultiSubEvaluation | None = None,
    candidate_holdout: MultiSubEvaluation | None = None,
    holdout_regression_tolerance_db: float = DEFAULT_HOLDOUT_REGRESSION_TOLERANCE_DB,
    created_at_utc: str,
) -> MultiSubQualification:
    """Assemble and seal a qualification between two candidates.

    Both candidates must pin the *same* seat partition — comparing two
    different partitions is a category error, not a qualification.
    """
    if baseline.partition.partition_sha256 != candidate.partition.partition_sha256:
        raise ValueError(
            'qualification requires both candidates to declare the identical '
            'seat partition'
        )
    if baseline.candidate_id == candidate.candidate_id:
        raise ValueError('qualification requires two distinct candidates')
    for name, evaluation, expected_candidate, expected_population in (
        ('baseline_optimization', baseline_optimization, baseline, 'optimization'),
        ('candidate_optimization', candidate_optimization, candidate, 'optimization'),
        ('baseline_holdout', baseline_holdout, baseline, 'holdout'),
        ('candidate_holdout', candidate_holdout, candidate, 'holdout'),
    ):
        if evaluation is None:
            continue
        if evaluation.candidate_sha256 != expected_candidate.candidate_sha256:
            raise ValueError(f'{name} binds a different candidate')
        if evaluation.population != expected_population:
            raise ValueError(f'{name} binds the wrong seat population')
    if claim == 'single_seat' and len(
        candidate.partition.optimization_seat_entity_ids
    ) != 1:
        raise ValueError('a single_seat claim requires a one-seat optimization set')
    verdict, effective_claim, reasons, improvements = evaluate_qualification(
        claim=claim,
        baseline_optimization=baseline_optimization,
        candidate_optimization=candidate_optimization,
        baseline_holdout=baseline_holdout,
        candidate_holdout=candidate_holdout,
        holdout_regression_tolerance_db=holdout_regression_tolerance_db,
    )
    payload: dict[str, Any] = {
        'schema_version': MULTI_SUB_SCHEMA_VERSION,
        'authority_version': MULTI_SUB_QUALIFICATION_VERSION,
        'qualification_id': qualification_id,
        'document_id': candidate.document_id,
        'scene_revision_id': candidate.scene_revision_id,
        'baseline_candidate_id': baseline.candidate_id,
        'baseline_candidate_sha256': baseline.candidate_sha256,
        'candidate_id': candidate.candidate_id,
        'candidate_sha256': candidate.candidate_sha256,
        'partition_sha256': candidate.partition.partition_sha256,
        'claim': claim,
        'baseline_optimization_evaluation_id': baseline_optimization.evaluation_id,
        'baseline_optimization_evaluation_sha256': baseline_optimization.evaluation_sha256,
        'candidate_optimization_evaluation_id': candidate_optimization.evaluation_id,
        'candidate_optimization_evaluation_sha256': candidate_optimization.evaluation_sha256,
        'baseline_holdout_evaluation_id': (
            None if baseline_holdout is None else baseline_holdout.evaluation_id
        ),
        'baseline_holdout_evaluation_sha256': (
            None if baseline_holdout is None else baseline_holdout.evaluation_sha256
        ),
        'candidate_holdout_evaluation_id': (
            None if candidate_holdout is None else candidate_holdout.evaluation_id
        ),
        'candidate_holdout_evaluation_sha256': (
            None if candidate_holdout is None else candidate_holdout.evaluation_sha256
        ),
        'holdout_regression_tolerance_db': holdout_regression_tolerance_db,
        'verdict': verdict,
        'effective_claim': effective_claim,
        'reasons': reasons,
        'improvements': improvements,
        'created_at_utc': created_at_utc,
    }
    provisional = MultiSubQualification.model_construct(
        **canonicalize_payload(MultiSubQualification, dict(payload)),
        qualification_sha256='0' * 64,
    )
    payload['qualification_sha256'] = _hash(provisional.identity_payload())
    return MultiSubQualification(**payload)


# --- deployment verification -----------------------------------------------


class ObservedSubState(BaseModel):
    """The read-back state of one installed sub (#569 §9)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    sub_entity_id: str = Field(min_length=1)
    gain_db: float | None = None
    delay_s: float | None = Field(default=None, ge=0.0)
    polarity: Literal['normal', 'inverted', 'unknown'] = 'unknown'
    position: Position3 | None = None
    installed: bool = True

    @field_validator('gain_db', 'delay_s')
    @classmethod
    def finite_values(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite(value, field_name='observed sub state')


class MultiSubDeploymentVerification(BaseModel):
    """Sealed post-deployment verification (#569 §9-10).

    Binds the exact observed DSP/placement state and the post-deployment
    remeasurement covering every declared seat (optimization + holdout +
    repeatability). The verdict is recomputed on write/read — a candidate
    that cannot physically be installed is *infeasible*, never
    "best theoretical for deployment".
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = MULTI_SUB_SCHEMA_VERSION
    authority_version: Literal[
        'rev55-multi-sub-deployment-1'
    ] = MULTI_SUB_DEPLOYMENT_VERSION
    verification_id: str = Field(min_length=1)
    qualification_id: str = Field(min_length=1)
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    observed_subs: tuple[ObservedSubState, ...] = Field(min_length=1)
    observed_dsp_ref: str | None = None
    observed_dsp_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    remeasurement_seats: tuple[SeatResponseBinding, ...] = ()
    post_deployment_optimization_evaluation_id: str | None = None
    post_deployment_optimization_evaluation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    post_deployment_holdout_evaluation_id: str | None = None
    post_deployment_holdout_evaluation_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    verdict: DeploymentVerdict
    reasons: tuple[QualificationReason, ...] = ()
    created_at_utc: str = Field(min_length=1)
    verification_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode='after')
    def consistent(self) -> 'MultiSubDeploymentVerification':
        if bool(self.observed_dsp_ref) != bool(self.observed_dsp_sha256):
            raise ValueError('observed DSP ref/hash must pair')
        for ref, sha in (
            (
                self.post_deployment_optimization_evaluation_id,
                self.post_deployment_optimization_evaluation_sha256,
            ),
            (
                self.post_deployment_holdout_evaluation_id,
                self.post_deployment_holdout_evaluation_sha256,
            ),
        ):
            if bool(ref) != bool(sha):
                raise ValueError('post-deployment evaluation ref/hash must pair')
        ids = [item.sub_entity_id for item in self.observed_subs]
        if len(ids) != len(set(ids)):
            raise ValueError('observed subs must not duplicate entities')
        if self.verification_sha256 != _hash(self.identity_payload()):
            raise ValueError('multi-sub deployment verification hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'verification_sha256'})


def evaluate_deployment(
    *,
    candidate: MultiSubCandidate,
    qualification: MultiSubQualification | None,
    observed_subs: Sequence[ObservedSubState],
    remeasurement_seats: Sequence[SeatResponseBinding],
    post_deployment_optimization: MultiSubEvaluation | None,
    post_deployment_holdout: MultiSubEvaluation | None,
) -> tuple[DeploymentVerdict, tuple[QualificationReason, ...]]:
    """Derive the deployment verdict deterministically."""
    reasons: list[QualificationReason] = []
    observed = {item.sub_entity_id: item for item in observed_subs}

    for sub in candidate.subs:
        state = observed.get(sub.sub_entity_id)
        if state is None:
            reasons.append(
                QualificationReason(
                    code='sub_not_observed',
                    detail=f'sub {sub.sub_entity_id} has no observed read-back state',
                )
            )
            return 'verification_incomplete', tuple(reasons)
        if sub.installable == 'infeasible' or not state.installed:
            reasons.append(
                QualificationReason(
                    code='infeasible_installation',
                    detail=(
                        f'sub {sub.sub_entity_id} cannot be / was not '
                        'installed at the candidate position — the candidate '
                        'is infeasible rather than best-theoretical'
                    ),
                )
            )
            return 'infeasible_installation', tuple(reasons)
        # Observed state must match the candidate exactly where the
        # candidate declares it — an unmatching read-back is a mismatch,
        # never an "approximately installed" pass.
        mismatches: list[str] = []
        if sub.gain_db is not None:
            if state.gain_db is None or not math.isclose(
                state.gain_db, sub.gain_db, rel_tol=0.0, abs_tol=1e-9
            ):
                mismatches.append('gain_db')
        if sub.delay_s is not None:
            if state.delay_s is None or not math.isclose(
                state.delay_s, sub.delay_s, rel_tol=0.0, abs_tol=1e-9
            ):
                mismatches.append('delay_s')
        if sub.polarity != 'unknown' and state.polarity != sub.polarity:
            mismatches.append('polarity')
        if sub.position_state == 'declared' and sub.position is not None:
            if state.position is None:
                mismatches.append('position')
            else:
                delta = math.sqrt(
                    (state.position.x_m - sub.position.x_m) ** 2
                    + (state.position.y_m - sub.position.y_m) ** 2
                    + (state.position.z_m - sub.position.z_m) ** 2
                )
                if delta > 1e-6:
                    mismatches.append('position')
        if mismatches:
            reasons.append(
                QualificationReason(
                    code='observed_state_mismatch',
                    detail=(
                        f'sub {sub.sub_entity_id} read-back differs from the '
                        'candidate: ' + ', '.join(mismatches)
                    ),
                )
            )
            return 'observed_state_mismatch', tuple(reasons)

    # Remeasurement must cover every declared seat.
    declared = (
        set(candidate.partition.optimization_seat_entity_ids)
        | set(candidate.partition.holdout_seat_entity_ids)
    )
    remeasured = {item.seat_entity_id for item in remeasurement_seats}
    missing = declared - remeasured
    if missing:
        return 'verification_incomplete', (
            QualificationReason(
                code='remeasurement_incomplete',
                detail='no post-deployment remeasurement at seats: '
                + ', '.join(sorted(missing)),
            ),
        )
    if candidate.partition.holdout_seat_entity_ids and (
        post_deployment_optimization is None or post_deployment_holdout is None
    ):
        return 'verification_incomplete', (
            QualificationReason(
                code='missing_post_deployment_evaluation',
                detail='post-deployment evaluations for both populations are '
                'required to close the qualification loop',
            ),
        )
    if post_deployment_optimization is not None:
        if (
            post_deployment_optimization.candidate_sha256
            != candidate.candidate_sha256
            or post_deployment_optimization.population != 'optimization'
        ):
            raise ValueError('post-deployment optimization evaluation is misbound')
    if post_deployment_holdout is not None:
        if (
            post_deployment_holdout.candidate_sha256
            != candidate.candidate_sha256
            or post_deployment_holdout.population != 'holdout'
        ):
            raise ValueError('post-deployment holdout evaluation is misbound')
    if qualification is not None:
        if qualification.candidate_sha256 != candidate.candidate_sha256:
            raise ValueError('verification binds a different candidate')
        if qualification.verdict == 'failed_holdout_regression' and (
            post_deployment_holdout is not None
        ):
            if qualification.baseline_holdout_evaluation_sha256 and (
                post_deployment_holdout.evaluation_sha256
                == qualification.candidate_holdout_evaluation_sha256
            ):
                reasons.append(
                    QualificationReason(
                        code='holdout_regression_unresolved',
                        detail='post-deployment holdout evidence still shows '
                        'the regression recorded at qualification',
                        blocking=False,
                    )
                )
    return 'verified', tuple(reasons)


def build_multi_sub_deployment_verification(
    *,
    verification_id: str,
    candidate: MultiSubCandidate,
    qualification: MultiSubQualification | None,
    observed_subs: Sequence[ObservedSubState],
    remeasurement_seats: Sequence[SeatResponseBinding],
    post_deployment_optimization: MultiSubEvaluation | None,
    post_deployment_holdout: MultiSubEvaluation | None,
    observed_dsp_ref: str | None = None,
    observed_dsp_sha256: str | None = None,
    created_at_utc: str,
) -> MultiSubDeploymentVerification:
    verdict, reasons = evaluate_deployment(
        candidate=candidate,
        qualification=qualification,
        observed_subs=observed_subs,
        remeasurement_seats=remeasurement_seats,
        post_deployment_optimization=post_deployment_optimization,
        post_deployment_holdout=post_deployment_holdout,
    )
    payload: dict[str, Any] = {
        'schema_version': MULTI_SUB_SCHEMA_VERSION,
        'authority_version': MULTI_SUB_DEPLOYMENT_VERSION,
        'verification_id': verification_id,
        'qualification_id': (
            'none' if qualification is None else qualification.qualification_id
        ),
        'qualification_sha256': (
            '0' * 64 if qualification is None else qualification.qualification_sha256
        ),
        'candidate_id': candidate.candidate_id,
        'candidate_sha256': candidate.candidate_sha256,
        'document_id': candidate.document_id,
        'scene_revision_id': candidate.scene_revision_id,
        'observed_subs': tuple(observed_subs),
        'observed_dsp_ref': observed_dsp_ref,
        'observed_dsp_sha256': observed_dsp_sha256,
        'remeasurement_seats': tuple(remeasurement_seats),
        'post_deployment_optimization_evaluation_id': (
            None
            if post_deployment_optimization is None
            else post_deployment_optimization.evaluation_id
        ),
        'post_deployment_optimization_evaluation_sha256': (
            None
            if post_deployment_optimization is None
            else post_deployment_optimization.evaluation_sha256
        ),
        'post_deployment_holdout_evaluation_id': (
            None
            if post_deployment_holdout is None
            else post_deployment_holdout.evaluation_id
        ),
        'post_deployment_holdout_evaluation_sha256': (
            None
            if post_deployment_holdout is None
            else post_deployment_holdout.evaluation_sha256
        ),
        'verdict': verdict,
        'reasons': reasons,
        'created_at_utc': created_at_utc,
    }
    provisional = MultiSubDeploymentVerification.model_construct(
        **canonicalize_payload(MultiSubDeploymentVerification, dict(payload)),
        verification_sha256='0' * 64,
    )
    payload['verification_sha256'] = _hash(provisional.identity_payload())
    return MultiSubDeploymentVerification(**payload)


__all__ = [
    'ACTIVE_CONTROL_STRATEGY_LABELS',
    'BandSeatMetrics',
    'BoundedEqBinding',
    'ConventionalMultiSubStrategy',
    'DEFAULT_EVALUATION_BANDS_HZ',
    'DEFAULT_HOLDOUT_REGRESSION_TOLERANCE_DB',
    'DeploymentVerdict',
    'ImprovementDelta',
    'MULTI_SUB_AUTHORITY_VERSION',
    'MULTI_SUB_DEPLOYMENT_VERSION',
    'MULTI_SUB_EVALUATION_VERSION',
    'MULTI_SUB_QUALIFICATION_VERSION',
    'MULTI_SUB_SCHEMA_VERSION',
    'MultiSubCandidate',
    'MultiSubDeploymentVerification',
    'MultiSubEffort',
    'MultiSubEvaluation',
    'MultiSubEvaluationSpec',
    'MultiSubEvaluatorIdentity',
    'MultiSubEvidenceKind',
    'MultiSubQualification',
    'MultiSubSeatPartition',
    'MultiSubSeatWeight',
    'MultiSubSeatWeighting',
    'MultiSubStageComparison',
    'MultiSubStageEntry',
    'MultiSubStrategyLabel',
    'MultiSubWeightsMode',
    'ObservedSubState',
    'OptimizationVariable',
    'QualificationClaim',
    'QualificationReason',
    'QualificationVerdict',
    'STRATEGY_TO_STAGE',
    'SeatPopulationRole',
    'SeatResponseBinding',
    'StageLabel',
    'StageTransitionAttribution',
    'SubChannelBinding',
    'assert_conventional_strategy',
    'attribute_stage_transitions',
    'build_multi_sub_candidate',
    'build_multi_sub_deployment_verification',
    'build_multi_sub_qualification',
    'build_multi_sub_stage_comparison',
    'evaluate_deployment',
    'evaluate_multi_sub_candidate',
    'evaluate_qualification',
]
