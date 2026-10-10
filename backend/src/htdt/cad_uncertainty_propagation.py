"""Input-uncertainty propagation + robust design assessment authority (#604).

The O90 robustness stack perturbs declared *geometric/installation* axes of a
SceneDocument. This module is the cross-cutting layer the research review
asked for: uncertain inputs of any kind — material coefficients, source
directivity, seat state, measurement alignment, calibration parameters —
declared with provenance, propagated through an exact evaluator, and
summarized as intervals/expectations per observable so a candidate ranking
never rests on one nominal point estimate.

Research basis (verified 2026-10-05):

- Thydal et al. 2021 (Appl. Acoustics 178:107939) — aleatory/epistemic input
  uncertainty propagated through a wave solver; boundary-condition
  uncertainty can dominate predictive precision; p-box style bounds are more
  defensible than invented Gaussians for poorly known materials.
- Pilch 2020 (Appl. Acoustics 170:107495) — calibration parameters chosen
  from plausible uncertainty sources; parameter identifiability matters.
- Kouvelis & Yu interval-scenario robustness; Bertsimas & Sim "price of
  robustness" — worst-case and regret semantics over plausible input states.
- Morris screening / Sobol decomposition are the canonical sensitivity
  families; this implementation contributes only what its deterministic
  samples honestly support — one-at-a-time corner ranges and deterministic
  sample correlations, labeled as such, never branded "Sobol".

Authority boundary:

- this engine evaluates candidates through a caller-supplied evaluator —
  it never invents physics, never converts an engineering tolerance into a
  Gaussian sigma, and never claims a finite sweep is converged evidence;
- aleatory/epistemic/mixed/unknown classes stay distinct;
- declared correlation groups share one deterministic coordinate (a
  sampling-design dependence, not an estimated correlation coefficient);
- solver numerical error, measurement uncertainty and model-form error
  remain owned by #566/#572/#564 — this layer composes them as named
  sources inside a #577 decision manifest instead of folding every spread
  into "input uncertainty";
- sampling metadata (method, seed, sample count, algorithm version) is
  sealed into every spec so a propagated result is reproducible.
"""

from __future__ import annotations

from math import isfinite
from statistics import NormalDist
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical_json import canonical_sha256 as _hash, canonicalize_payload


PROPAGATION_SCHEMA_VERSION = 1
UNCERTAIN_INPUT_SET_VERSION = 'uncertain-input-set-1'
PROPAGATION_SPEC_VERSION = 'propagation-spec-1'
PROPAGATION_ALGORITHM_VERSION = 'rev56-propagation-1'
ROBUST_DESIGN_VERSION = 'robust-design-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'

#: Hard bound on the evidence a single study may carry — an exploratory
#: sweep stays small and is never mistaken for converged probabilistic data.
MAX_PROPAGATION_SAMPLES = 256


UncertainInputKind = Literal[
    'geometry_dimension',
    'source_position',
    'source_aim',
    'receiver_position',
    'installation_tolerance',
    'material_absorption',
    'surface_impedance',
    'scattering_diffusion',
    'speaker_directivity',
    'speaker_output',
    'screen_transfer',
    'seat_occupancy',
    'environment_condition',
    'measurement_alignment',
    'model_calibration_parameter',
    'other',
]
"""The uncertain-input taxonomy from issue §1. ``other`` exists so a real
input is never squeezed into a kind it is not."""

UncertaintyClass = Literal['aleatory', 'epistemic', 'mixed', 'unknown']
"""Aleatory variability vs epistemic lack-of-knowledge stay distinct
(issue §2). ``unknown`` is honest absence of classification, not silence."""

InputRepresentation = Literal[
    'bounded_interval',
    'empirical_samples',
    'discrete_scenarios',
    'distribution',
    'credible_interval',
    'pbox',
    'correlated_samples',
]
"""Representations are kept — a tolerance interval is never silently
promoted to a standard deviation (issue §3)."""

DeltaSemantics = Literal['additive_delta', 'absolute', 'scale_factor']
"""How a drawn state value applies to the target quantity:
``additive_delta`` adds to the nominal, ``absolute`` replaces it,
``scale_factor`` multiplies it. The evaluator applies it; the engine only
carries the declared semantics."""

PropagationMethod = Literal[
    'deterministic_corner_pairs',
    'deterministic_low_discrepancy',
    'explicit_states',
]

SensitivityMethod = Literal[
    'deterministic_oat_range',
    'deterministic_sample_correlation',
]

DominanceState = Literal[
    'robustly_better',
    'robustly_worse',
    'overlapping',
    'nominal_only',
    'unresolved',
]


class UncertainInput(BaseModel):
    """One declared uncertain design/model input with provenance."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    input_id: str = Field(min_length=1)
    kind: UncertainInputKind
    uncertainty_class: UncertaintyClass
    representation: InputRepresentation
    target_kind: Literal[
        'entity',
        'material',
        'construction',
        'source',
        'environment',
        'measurement',
        'model_parameter',
        'other',
    ]
    target_ref: str = Field(min_length=1)
    unit: str | None = None
    delta_semantics: DeltaSemantics = 'additive_delta'
    bound_low: float | None = None
    bound_high: float | None = None
    empirical_samples: tuple[float, ...] | None = None
    discrete_values: tuple[float, ...] | None = None
    discrete_weights: tuple[float, ...] | None = None
    distribution: Literal['uniform', 'normal'] | None = None
    mean_delta: float = 0.0
    stddev: float | None = Field(default=None, gt=0.0)
    coverage_probability: float | None = None
    correlation_group_id: str | None = Field(default=None, min_length=1)
    applicability: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_input(self) -> 'UncertainInput':
        numeric = (
            self.bound_low,
            self.bound_high,
            self.mean_delta,
            self.stddev,
            self.coverage_probability,
        )
        if any(value is not None and not isfinite(float(value)) for value in numeric):
            raise ValueError('uncertain input values must be finite')

        interval_like = {'bounded_interval', 'credible_interval', 'pbox'}
        if self.representation in interval_like:
            if self.bound_low is None or self.bound_high is None:
                raise ValueError(
                    f'{self.representation} requires bound_low/bound_high'
                )
            if float(self.bound_high) < float(self.bound_low):
                raise ValueError('input bound_high must be >= bound_low')
        if self.representation == 'credible_interval':
            if self.coverage_probability is None or not (
                0.0 < float(self.coverage_probability) <= 1.0
            ):
                raise ValueError(
                    'credible_interval requires coverage_probability in (0, 1]'
                )
        if self.representation == 'pbox':
            # A p-box is an outer epistemic bound; an optional inner declared
            # distribution may tighten sampling. Without one it propagates as
            # a pure interval — the honest reading, not a hidden Gaussian.
            if self.distribution is not None and self.distribution == 'normal' and (
                self.stddev is None
            ):
                raise ValueError('pbox inner normal distribution requires stddev')
        if self.representation == 'empirical_samples':
            if not self.empirical_samples:
                raise ValueError('empirical_samples requires at least one sample')
            if any(
                not isfinite(float(value)) for value in self.empirical_samples
            ):
                raise ValueError('empirical samples must be finite')
        if self.representation == 'correlated_samples':
            if not self.correlation_group_id:
                raise ValueError(
                    'correlated_samples requires a correlation_group_id'
                )
            if not self.empirical_samples:
                raise ValueError(
                    'correlated_samples requires joint empirical_samples '
                    '(one row per shared scenario)'
                )
            if any(
                not isfinite(float(value)) for value in self.empirical_samples
            ):
                raise ValueError('correlated samples must be finite')
        if self.representation == 'discrete_scenarios':
            if not self.discrete_values:
                raise ValueError('discrete_scenarios requires discrete_values')
            if any(
                not isfinite(float(value)) for value in self.discrete_values
            ):
                raise ValueError('discrete scenario values must be finite')
            if self.discrete_weights is not None:
                if len(self.discrete_weights) != len(self.discrete_values):
                    raise ValueError('discrete weights must match the values')
                if any(
                    not isfinite(float(w)) or float(w) < 0.0
                    for w in self.discrete_weights
                ):
                    raise ValueError('discrete weights must be finite and >= 0')
                total = sum(float(w) for w in self.discrete_weights)
                if abs(total - 1.0) > 1e-9:
                    raise ValueError('discrete weights must sum to 1')
        if self.representation == 'distribution':
            if self.distribution is None:
                raise ValueError('distribution representation requires a kind')
            if self.distribution == 'uniform':
                if self.bound_low is None or self.bound_high is None:
                    raise ValueError(
                        'uniform distribution requires bound bounds as min/max'
                    )
            else:
                if self.stddev is None:
                    raise ValueError('normal distribution requires stddev')
                if (self.bound_low is None) != (self.bound_high is None):
                    raise ValueError(
                        'truncated normal requires both bound_low/bound_high'
                    )
        return self


class UncertainInputSet(BaseModel):
    """Sealed set of declared uncertain inputs for one exact scene/model."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PROPAGATION_SCHEMA_VERSION
    authority_version: Literal['uncertain-input-set-1'] = (
        UNCERTAIN_INPUT_SET_VERSION
    )
    input_set_id: str = Field(pattern=r'^uncertain-input-set:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    model_ref: str | None = None
    inputs: tuple[UncertainInput, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_set(self) -> 'UncertainInputSet':
        ids = [item.input_id for item in self.inputs]
        if len(ids) != len(set(ids)):
            raise ValueError('uncertain input ids must be unique')
        groups: dict[str, list[UncertainInput]] = {}
        for item in self.inputs:
            if item.correlation_group_id:
                groups.setdefault(item.correlation_group_id, []).append(item)
        for group_id, members in groups.items():
            correlated = [
                item for item in members
                if item.representation == 'correlated_samples'
            ]
            if correlated and len(correlated) != len(members):
                raise ValueError(
                    f'correlation group {group_id!r} mixes correlated_samples '
                    'with other representations'
                )
            if correlated:
                length = len(correlated[0].empirical_samples or ())
                if any(
                    len(item.empirical_samples or ()) != length
                    for item in correlated
                ):
                    raise ValueError(
                        f'correlation group {group_id!r} members must carry '
                        'the same number of joint samples'
                    )
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('uncertain input set semantic hash mismatch')
        if self.input_set_id != f'uncertain-input-set:{expected}':
            raise ValueError('uncertain input set id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'input_set_id', 'semantic_sha256'},
        )


def build_uncertain_input_set(**kwargs: Any) -> UncertainInputSet:
    payload = {
        'input_set_id': 'uncertain-input-set:' + '0' * 64,
        'semantic_sha256': '0' * 64,
        **kwargs,
    }
    provisional = UncertainInputSet.model_construct(
        **canonicalize_payload(UncertainInputSet, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['input_set_id'] = f'uncertain-input-set:{digest}'
    return UncertainInputSet(**payload)


class ExplicitInputState(BaseModel):
    """One supplied joint input state for ``explicit_states`` propagation."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    state_id: str = Field(min_length=1)
    state_values: dict[str, float] = Field(min_length=1)
    probability_weight: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def valid_state(self) -> 'ExplicitInputState':
        if any(
            not isfinite(float(value)) for value in self.state_values.values()
        ):
            raise ValueError('explicit state values must be finite')
        return self


class PropagationSpec(BaseModel):
    """Sealed propagation method + reproducibility metadata (issue §5)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PROPAGATION_SCHEMA_VERSION
    authority_version: Literal['propagation-spec-1'] = PROPAGATION_SPEC_VERSION
    algorithm_version: Literal['rev56-propagation-1'] = (
        PROPAGATION_ALGORITHM_VERSION
    )
    propagation_spec_id: str = Field(pattern=r'^propagation-spec:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    input_set_id: str = Field(min_length=1)
    input_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    method: PropagationMethod
    sampling_seed: int | None = None
    sample_count: int = Field(ge=1, le=MAX_PROPAGATION_SAMPLES)
    explicit_states: tuple[ExplicitInputState, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_spec(self) -> 'PropagationSpec':
        if self.method == 'deterministic_low_discrepancy':
            if self.sampling_seed is None:
                raise ValueError('low-discrepancy propagation requires a seed')
            if self.sample_count < 3:
                raise ValueError(
                    'low-discrepancy propagation needs nominal plus >= 2 samples'
                )
            if self.explicit_states:
                raise ValueError('sampled methods do not take explicit states')
        elif self.method == 'explicit_states':
            if self.sampling_seed is not None:
                raise ValueError('explicit enumeration has no sampling seed')
            if not self.explicit_states:
                raise ValueError('explicit_states method requires the states')
            if self.sample_count != 1 + len(self.explicit_states):
                raise ValueError(
                    'explicit_states sample_count equals nominal plus supplied states'
                )
            ids = [state.state_id for state in self.explicit_states]
            if len(ids) != len(set(ids)):
                raise ValueError('explicit state ids must be unique')
            weighted = [
                state.probability_weight is not None
                for state in self.explicit_states
            ]
            if any(weighted) and not all(weighted):
                raise ValueError('explicit weights must cover every state or none')
            if all(weighted):
                total = sum(
                    float(state.probability_weight or 0.0)
                    for state in self.explicit_states
                )
                if abs(total - 1.0) > 1e-9:
                    raise ValueError('explicit probability weights must sum to 1')
        else:
            if self.sampling_seed is not None or self.explicit_states:
                raise ValueError(
                    'corner-pair propagation takes neither seed nor explicit states'
                )
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('propagation spec semantic hash mismatch')
        if self.propagation_spec_id != f'propagation-spec:{expected}':
            raise ValueError('propagation spec id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'propagation_spec_id', 'semantic_sha256'},
        )


def build_propagation_spec(**kwargs: Any) -> PropagationSpec:
    payload = {
        'propagation_spec_id': 'propagation-spec:' + '0' * 64,
        'semantic_sha256': '0' * 64,
        **kwargs,
    }
    provisional = PropagationSpec.model_construct(
        **canonicalize_payload(PropagationSpec, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['propagation_spec_id'] = f'propagation-spec:{digest}'
    return PropagationSpec(**payload)


class PropagatedState(BaseModel):
    """One deterministic input state — shared verbatim across candidates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    sample_index: int = Field(ge=0)
    step: Literal['nominal', 'minus', 'plus', 'sampled', 'explicit']
    focus_input_id: str | None = None
    state_values: dict[str, float]
    probability_weight: float | None = Field(default=None, ge=0.0, le=1.0)


class PropagatedSample(BaseModel):
    """Evaluated evidence for one candidate at one input state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    sample_index: int = Field(ge=0)
    step: Literal['nominal', 'minus', 'plus', 'sampled', 'explicit']
    state_values: dict[str, float]
    probability_weight: float | None = None
    feasible: bool
    observable_values: dict[str, float] = {}
    failure_reason: str | None = None

    @model_validator(mode='after')
    def valid_sample(self) -> 'PropagatedSample':
        if not self.feasible and self.observable_values:
            raise ValueError('infeasible samples carry no observable values')
        if any(
            not isfinite(float(value)) for value in self.observable_values.values()
        ):
            raise ValueError('observable values must be finite')
        if any(
            not isfinite(float(value)) for value in self.state_values.values()
        ):
            raise ValueError('state values must be finite')
        return self


class ObservablePropagationSummary(BaseModel):
    """Per-observable propagated summary — never nominal-only (issue §7)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    observable_id: str = Field(min_length=1)
    nominal: float
    sampled_min: float
    sampled_max: float
    expected: float | None = None
    expected_semantics: Literal['weighted_mean', 'unweighted_mean'] | None = None
    quantiles: dict[str, float] = {}
    quantile_semantics: Literal[
        'deterministic_sample_quantiles', 'weighted_sample_quantiles'
    ] | None = None
    feasible_fraction: float
    evaluated_count: int = Field(ge=0)

    @model_validator(mode='after')
    def valid_summary(self) -> 'ObservablePropagationSummary':
        for value in (self.nominal, self.sampled_min, self.sampled_max, self.expected):
            if value is not None and not isfinite(float(value)):
                raise ValueError('summary values must be finite')
        if self.sampled_max < self.sampled_min:
            raise ValueError('sampled_max must be >= sampled_min')
        if not (0.0 <= float(self.feasible_fraction) <= 1.0):
            raise ValueError('feasible_fraction must lie in [0, 1]')
        if self.expected is not None and self.expected_semantics is None:
            raise ValueError('expected requires explicit semantics')
        if self.quantiles and self.quantile_semantics is None:
            raise ValueError('quantiles require explicit semantics')
        return self


class SensitivityContribution(BaseModel):
    """One input's influence on one observable under a declared method."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    input_id: str = Field(min_length=1)
    observable_id: str = Field(min_length=1)
    method: SensitivityMethod
    value: float
    share: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def valid_contribution(self) -> 'SensitivityContribution':
        if not isfinite(float(self.value)):
            raise ValueError('sensitivity value must be finite')
        return self


class SensitivityStudy(BaseModel):
    """Sealed sensitivity ranking — which inputs dominate each observable."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    study_id: str = Field(pattern=r'^sensitivity-study:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)
    method: SensitivityMethod
    contributions: tuple[SensitivityContribution, ...]
    limitations: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_study(self) -> 'SensitivityStudy':
        keys = [
            (item.input_id, item.observable_id) for item in self.contributions
        ]
        if len(keys) != len(set(keys)):
            raise ValueError('sensitivity contributions must be unique per pair')
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('sensitivity study semantic hash mismatch')
        if self.study_id != f'sensitivity-study:{expected}':
            raise ValueError('sensitivity study id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude={'study_id', 'semantic_sha256'})


class CandidatePropagationProfile(BaseModel):
    """Per-candidate robust evidence across the propagated input domain."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_id: str = Field(min_length=1)
    summaries: tuple[ObservablePropagationSummary, ...] = Field(min_length=1)
    infeasible_sample_count: int = Field(ge=0)
    failed_sample_count: int = Field(ge=0)

    @model_validator(mode='after')
    def valid_profile(self) -> 'CandidatePropagationProfile':
        ids = [item.observable_id for item in self.summaries]
        if len(ids) != len(set(ids)):
            raise ValueError('profile observable ids must be unique')
        return self


class PairwiseRobustOutcome(BaseModel):
    """Paired candidate comparison over the shared sampled states (§10)."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    candidate_a: str = Field(min_length=1)
    candidate_b: str = Field(min_length=1)
    observable_id: str = Field(min_length=1)
    nominal_difference: float
    dominance_state: DominanceState
    paired_reversal_fraction: float | None = None
    shared_evaluated_count: int = Field(ge=0)
    interval_overlap: bool

    @model_validator(mode='after')
    def valid_outcome(self) -> 'PairwiseRobustOutcome':
        if not isfinite(float(self.nominal_difference)):
            raise ValueError('pairwise difference must be finite')
        if self.paired_reversal_fraction is not None and not (
            0.0 <= float(self.paired_reversal_fraction) <= 1.0
        ):
            raise ValueError('paired reversal fraction must lie in [0, 1]')
        return self


class RobustDesignAssessment(BaseModel):
    """Sealed robust-design assessment across propagated candidates."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = PROPAGATION_SCHEMA_VERSION
    authority_version: Literal['robust-design-1'] = ROBUST_DESIGN_VERSION
    assessment_id: str = Field(pattern=r'^robust-design:[0-9a-f]{64}$')
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    input_set_id: str = Field(min_length=1)
    input_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    propagation_spec_id: str = Field(min_length=1)
    propagation_spec_sha256: str = Field(pattern=_SHA256_PATTERN)

    candidates: tuple[CandidatePropagationProfile, ...] = Field(min_length=1)
    pairwise: tuple[PairwiseRobustOutcome, ...] = ()
    sensitivity: SensitivityStudy | None = None
    dominant_input_ids: tuple[str, ...] = ()
    samples: tuple[PropagatedSample, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_requests: tuple[str, ...] = ()
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_assessment(self) -> 'RobustDesignAssessment':
        ids = [item.candidate_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError('assessment candidates must be unique')
        known = set(ids)
        for pair in self.pairwise:
            if pair.candidate_a not in known or pair.candidate_b not in known:
                raise ValueError('pairwise outcome references unknown candidate')
        if self.sensitivity is not None:
            observable_ids = {
                s.observable_id
                for c in self.candidates
                for s in c.summaries
            }
            if any(
                c.observable_id not in observable_ids
                for c in self.sensitivity.contributions
            ):
                raise ValueError(
                    'sensitivity references an unobserved observable'
                )
        expected = _hash(self.identity_payload())
        if self.semantic_sha256 != expected:
            raise ValueError('robust design assessment semantic hash mismatch')
        if self.assessment_id != f'robust-design:{expected}':
            raise ValueError('robust design assessment id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'assessment_id', 'semantic_sha256'},
        )


def _stable_coordinate(
    spec: PropagationSpec, *, sample_index: int, key: str
) -> float:
    """Deterministic [0, 1) coordinate — a sampling design, never randomness."""
    digest = _hash(
        {
            'propagation_spec_sha256': spec.semantic_sha256,
            'sampling_seed': spec.sampling_seed,
            'sample_index': sample_index,
            'coordinate_key': key,
            'algorithm_version': PROPAGATION_ALGORITHM_VERSION,
        }
    )
    return int(digest[:16], 16) / float(1 << 64)


def _pick(values: tuple[float, ...], u: float) -> float:
    index = min(len(values) - 1, int(u * len(values)))
    return float(values[index])


def _sample_input(item: UncertainInput, u: float) -> float:
    """Map a unit coordinate to a state value under the declared semantics."""
    rep = item.representation
    if rep in ('bounded_interval', 'credible_interval', 'pbox'):
        if rep == 'pbox' and item.distribution is not None:
            return _distribution_value(item, u)
        low, high = float(item.bound_low), float(item.bound_high)  # type: ignore[arg-type]
        return low + u * (high - low)
    if rep == 'distribution':
        return _distribution_value(item, u)
    if rep == 'empirical_samples' or rep == 'correlated_samples':
        return _pick(tuple(item.empirical_samples or ()), u)
    if rep == 'discrete_scenarios':
        values = tuple(item.discrete_values or ())
        if item.discrete_weights is not None:
            cumulative: list[float] = []
            total = 0.0
            for weight in item.discrete_weights:
                total += float(weight)
                cumulative.append(total)
            for edge, value in zip(cumulative, values, strict=True):
                if u < edge:
                    return float(value)
            return float(values[-1])
        return _pick(values, u)
    raise ValueError(f'unsupported input representation: {rep}')


def _distribution_value(item: UncertainInput, u: float) -> float:
    if item.distribution == 'uniform':
        low, high = float(item.bound_low), float(item.bound_high)  # type: ignore[arg-type]
        return low + u * (high - low)
    assert item.stddev is not None
    normal = NormalDist(mu=float(item.mean_delta), sigma=float(item.stddev))
    if item.bound_low is None:
        return float(normal.inv_cdf(min(max(u, 1e-12), 1.0 - 1e-12)))
    low_cdf = normal.cdf(float(item.bound_low))
    high_cdf = normal.cdf(float(item.bound_high))
    mapped = low_cdf + u * (high_cdf - low_cdf)
    return float(normal.inv_cdf(min(max(mapped, 1e-12), 1.0 - 1e-12)))


def build_propagation_plan(
    input_set: UncertainInputSet,
    spec: PropagationSpec,
) -> tuple[PropagatedState, ...]:
    """Deterministic joint input states, index 0 = nominal."""
    if (
        spec.input_set_id != input_set.input_set_id
        or spec.input_set_sha256 != input_set.semantic_sha256
    ):
        raise ValueError('propagation spec does not pin this input set')

    inputs = tuple(sorted(input_set.inputs, key=lambda item: item.input_id))
    correlated = {
        item.input_id
        for item in inputs
        if item.representation == 'correlated_samples'
    }
    group_members: dict[str, list[UncertainInput]] = {}
    for item in inputs:
        if item.correlation_group_id:
            group_members.setdefault(item.correlation_group_id, []).append(item)
    joint_lengths = {
        item.input_id: len(item.empirical_samples or ())
        for item in inputs
        if item.representation == 'correlated_samples'
    }

    states: list[PropagatedState] = [
        PropagatedState(
            sample_index=0, step='nominal', state_values={}
        )
    ]

    if spec.method == 'deterministic_corner_pairs':
        index = 1
        for item in inputs:
            for step, u in (('minus', 0.0), ('plus', 1.0)):
                if item.representation == 'empirical_samples':
                    samples = sorted(item.empirical_samples or ())
                    value = float(samples[0] if step == 'minus' else samples[-1])
                elif item.representation == 'correlated_samples':
                    samples = sorted(item.empirical_samples or ())
                    value = float(samples[0] if step == 'minus' else samples[-1])
                elif item.representation == 'discrete_scenarios':
                    values = sorted(item.discrete_values or ())
                    value = float(values[0] if step == 'minus' else values[-1])
                elif item.representation == 'distribution' and (
                    item.distribution == 'normal' and item.bound_low is None
                ):
                    # unbounded normal: corners have no finite value
                    continue
                else:
                    value = _sample_input(item, u)
                if index >= spec.sample_count:
                    break
                states.append(
                    PropagatedState(
                        sample_index=index,
                        step=step,  # type: ignore[arg-type]
                        focus_input_id=item.input_id,
                        state_values={item.input_id: value},
                    )
                )
                index += 1
        return tuple(states)

    if spec.method == 'explicit_states':
        input_ids = {item.input_id for item in inputs}
        for position, state in enumerate(
            sorted(spec.explicit_states, key=lambda item: item.state_id),
            start=1,
        ):
            unknown = set(state.state_values) - input_ids
            if unknown:
                raise ValueError(
                    f'explicit state {state.state_id!r} references unknown '
                    f'inputs: {sorted(unknown)}'
                )
            states.append(
                PropagatedState(
                    sample_index=position,
                    step='explicit',
                    state_values={
                        key: float(value)
                        for key, value in sorted(state.state_values.items())
                    },
                    probability_weight=state.probability_weight,
                )
            )
        return tuple(states)

    # deterministic_low_discrepancy — every input draws every sample; a
    # correlated group shares one coordinate (a declared sampling-design
    # dependence, not an estimated correlation).
    for index in range(1, spec.sample_count):
        values: dict[str, float] = {}
        group_u: dict[str, float] = {}
        joint_row: dict[str, int] = {}
        for item in inputs:
            if item.input_id in correlated:
                assert item.correlation_group_id is not None
                group_id = item.correlation_group_id
                if group_id not in joint_row:
                    u = _stable_coordinate(
                        spec,
                        sample_index=index,
                        key=f'correlated-group:{group_id}',
                    )
                    length = joint_lengths[item.input_id]
                    joint_row[group_id] = min(
                        length - 1, int(u * length)
                    )
                row = joint_row[group_id]
                values[item.input_id] = float(
                    (item.empirical_samples or (0.0,))[row]
                )
                continue
            if item.correlation_group_id:
                group_id = item.correlation_group_id
                u = group_u.setdefault(
                    group_id,
                    _stable_coordinate(
                        spec,
                        sample_index=index,
                        key=f'group:{group_id}',
                    ),
                )
            else:
                u = _stable_coordinate(
                    spec,
                    sample_index=index,
                    key=f'input:{item.input_id}',
                )
            values[item.input_id] = _sample_input(item, u)
        states.append(
            PropagatedState(
                sample_index=index,
                step='sampled',
                state_values=values,
            )
        )
    return tuple(states)


PropagationEvaluator = Callable[[str, PropagatedState], Mapping[str, float] | None]
"""``evaluator(candidate_id, state) -> {observable_id: value}`` or None when
the state is infeasible/failed for that candidate. The evaluator owns the
physics; the engine owns the deterministic sampling and summarization."""


class PropagationExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal['completed', 'cancelled']
    samples: tuple[PropagatedSample, ...]
    computed_count: int = Field(ge=0)


def execute_propagation(
    *,
    input_set: UncertainInputSet,
    spec: PropagationSpec,
    evaluator: PropagationEvaluator,
    candidate_ids: Sequence[str],
    cancel_requested: Callable[[], bool] | None = None,
) -> PropagationExecutionResult:
    """Evaluate every candidate over the *same* deterministic state plan —
    shared state indices are what make paired rank-reversal honest."""
    if not candidate_ids or len(set(candidate_ids)) != len(tuple(candidate_ids)):
        raise ValueError('propagation requires unique candidate ids')
    plan = build_propagation_plan(input_set, spec)
    samples: list[PropagatedSample] = []
    computed = 0
    for candidate_id in candidate_ids:
        for state in plan:
            if cancel_requested is not None and cancel_requested():
                return PropagationExecutionResult(
                    status='cancelled',
                    samples=tuple(samples),
                    computed_count=computed,
                )
            values: dict[str, float] = {}
            feasible = True
            failure: str | None = None
            try:
                result = evaluator(candidate_id, state)
            except Exception as exc:  # error-boundary: evaluation lane — an evaluator failure is honest evidence: result=None and the reason recorded in failure, never hidden (noqa: BLE001)
                result = None
                failure = f'evaluation_failed:{exc}'
            if result is None:
                feasible = False
                if failure is None:
                    failure = 'evaluator_returned_infeasible'
            else:
                for key, value in result.items():
                    number = float(value)
                    if not isfinite(number):
                        continue
                    values[key] = number
            samples.append(
                PropagatedSample(
                    candidate_id=candidate_id,
                    sample_index=state.sample_index,
                    step=state.step,
                    state_values=state.state_values,
                    probability_weight=state.probability_weight,
                    feasible=feasible,
                    observable_values=values,
                    failure_reason=failure,
                )
            )
            computed += 1
    return PropagationExecutionResult(
        status='completed',
        samples=tuple(samples),
        computed_count=computed,
    )


def _sorted_quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        raise ValueError('quantile requires values')
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(len(sorted_values) - 1, low + 1)
    fraction = position - low
    return float(
        sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction
    )


def summarize_observable(
    *,
    observable_id: str,
    samples: Sequence[PropagatedSample],
) -> ObservablePropagationSummary | None:
    """Interval/expected/quantile summary over feasible evaluated samples."""
    evaluated = [
        sample
        for sample in samples
        if observable_id in sample.observable_values
    ]
    if not evaluated:
        return None
    nominal_sample = next(
        (sample for sample in evaluated if sample.step == 'nominal'), None
    )
    if nominal_sample is None:
        return None
    nominal = float(nominal_sample.observable_values[observable_id])
    values = [float(sample.observable_values[observable_id]) for sample in evaluated]
    # The nominal state is the reference point, not a declared scenario —
    # when the plan carries explicit probability weights the expectation and
    # quantiles run over the weight-bearing states only; min/max still
    # include the nominal because it is evaluated evidence.
    weighted_evaluated = [
        sample for sample in evaluated if sample.probability_weight is not None
    ]
    weighted = bool(weighted_evaluated) and len(weighted_evaluated) == len(
        [sample for sample in evaluated if sample.step != 'nominal']
    )

    ordered = sorted(values)
    if weighted:
        pairs = sorted(
            (
                (
                    float(sample.observable_values[observable_id]),
                    float(sample.probability_weight),  # type: ignore[arg-type]
                )
                for sample in weighted_evaluated
            ),
            key=lambda pair: pair[0],
        )
        total = sum(weight for _value, weight in pairs)
        expected = (
            sum(value * weight for value, weight in pairs) / total
            if total > 0
            else None
        )
        cumulative = 0.0
        quantiles: dict[str, float] = {}
        for q in (0.05, 0.5, 0.95):
            threshold = q * total
            cumulative = 0.0
            picked = pairs[-1][0]
            for value, weight in pairs:
                cumulative += weight
                if cumulative >= threshold:
                    picked = value
                    break
            quantiles[f'p{int(q * 100):02d}'] = picked
        quantile_semantics = 'weighted_sample_quantiles'
        expected_semantics = 'weighted_mean' if expected is not None else None
    else:
        expected = sum(values) / len(values) if len(values) > 1 else None
        expected_semantics = 'unweighted_mean' if expected is not None else None
        quantiles = {
            f'p{int(q * 100):02d}': _sorted_quantile(ordered, q)
            for q in (0.05, 0.5, 0.95)
        } if len(ordered) > 1 else {}
        quantile_semantics = (
            'deterministic_sample_quantiles' if quantiles else None
        )

    return ObservablePropagationSummary(
        observable_id=observable_id,
        nominal=nominal,
        sampled_min=ordered[0],
        sampled_max=ordered[-1],
        expected=expected,
        expected_semantics=expected_semantics,  # type: ignore[arg-type]
        quantiles=quantiles,
        quantile_semantics=quantile_semantics,  # type: ignore[arg-type]
        feasible_fraction=len(evaluated) / len(samples) if samples else 0.0,
        evaluated_count=len(evaluated),
    )


def compute_sensitivity(
    *,
    input_set: UncertainInputSet,
    spec: PropagationSpec,
    samples: Sequence[PropagatedSample],
    candidate_id: str,
    created_quantile_labels: tuple[str, ...] = ('p05', 'p50', 'p95'),
) -> SensitivityStudy | None:
    """Declared-method sensitivity ranking from the propagated samples.

    ``deterministic_corner_pairs`` → per-input OAT range (issue §8 interval
    influence); ``deterministic_low_discrepancy`` → signed sample correlation
    between each input's drawn value and the observable. Neither is branded
    a variance decomposition.
    """
    own = [
        sample for sample in samples if sample.candidate_id == candidate_id
    ]
    observable_ids = sorted(
        {
            key
            for sample in own
            for key in sample.observable_values
        }
    )
    if not observable_ids:
        return None

    if spec.method == 'deterministic_corner_pairs':
        method: SensitivityMethod = 'deterministic_oat_range'
        by_step: dict[tuple[str, str], float] = {}
        for sample in own:
            if (
                sample.step in ('minus', 'plus')
                and sample.feasible
                and len(sample.state_values) == 1
            ):
                input_id = next(iter(sample.state_values))
                for observable_id, value in sample.observable_values.items():
                    by_step[(input_id, sample.step, observable_id)] = float(value)
        pairs: dict[tuple[str, str], tuple[float, float]] = {}
        for (input_id, step, observable_id), value in by_step.items():
            key = (input_id, observable_id)
            minus, plus = pairs.get(key, (float('nan'), float('nan')))
            if step == 'minus':
                minus = value
            else:
                plus = value
            pairs[key] = (minus, plus)
        contributions: list[SensitivityContribution] = []
        totals: dict[str, float] = {}
        for (input_id, observable_id), (minus, plus) in pairs.items():
            if not (isfinite(minus) and isfinite(plus)):
                continue
            influence = abs(plus - minus)
            contributions.append(
                SensitivityContribution(
                    input_id=input_id,
                    observable_id=observable_id,
                    method=method,
                    value=influence,
                )
            )
            totals[observable_id] = totals.get(observable_id, 0.0) + influence
        resolved: list[SensitivityContribution] = []
        for item in contributions:
            total = totals[item.observable_id]
            resolved.append(
                SensitivityContribution(
                    input_id=item.input_id,
                    observable_id=item.observable_id,
                    method=method,
                    value=item.value,
                    share=(
                        item.value / total if total > 0 else 0.0
                    ),
                )
            )
        payload = {
            'study_id': 'sensitivity-study:' + '0' * 64,
            'semantic_sha256': '0' * 64,
            'method': method,
            'contributions': tuple(
                sorted(
                    resolved,
                    key=lambda item: (item.observable_id, item.input_id),
                )
            ),
            'limitations': (
                'one-at-a-time corner influence — interaction effects are '
                'not separable under this design',
            ),
        }
    elif spec.method == 'deterministic_low_discrepancy':
        method = 'deterministic_sample_correlation'
        sampled = [
            sample
            for sample in own
            if sample.step == 'sampled' and sample.feasible
        ]
        input_ids = sorted(item.input_id for item in input_set.inputs)
        contributions = []
        totals = {}
        for observable_id in observable_ids:
            ys = [
                sample.observable_values[observable_id]
                for sample in sampled
                if observable_id in sample.observable_values
            ]
            xs_rows = [
                sample for sample in sampled
                if observable_id in sample.observable_values
            ]
            if len(ys) < 3:
                continue
            mean_y = sum(ys) / len(ys)
            var_y = sum((value - mean_y) ** 2 for value in ys)
            for input_id in input_ids:
                xs = [
                    float(sample.state_values.get(input_id, 0.0))
                    for sample in xs_rows
                ]
                mean_x = sum(xs) / len(xs)
                var_x = sum((value - mean_x) ** 2 for value in xs)
                if var_x <= 0.0 or var_y <= 0.0:
                    corr = 0.0
                else:
                    corr = sum(
                        (x - mean_x) * (y - mean_y)
                        for x, y in zip(xs, ys, strict=True)
                    ) / (var_x ** 0.5 * var_y ** 0.5)
                contributions.append(
                    SensitivityContribution(
                        input_id=input_id,
                        observable_id=observable_id,
                        method=method,
                        value=corr,
                    )
                )
                totals[observable_id] = totals.get(
                    observable_id, 0.0
                ) + abs(corr)
        resolved = []
        for item in contributions:
            total = totals[item.observable_id]
            resolved.append(
                SensitivityContribution(
                    input_id=item.input_id,
                    observable_id=item.observable_id,
                    method=method,
                    value=item.value,
                    share=(abs(item.value) / total if total > 0 else 0.0),
                )
            )
        payload = {
            'study_id': 'sensitivity-study:' + '0' * 64,
            'semantic_sha256': '0' * 64,
            'method': method,
            'contributions': tuple(
                sorted(
                    resolved,
                    key=lambda item: (item.observable_id, item.input_id),
                )
            ),
            'limitations': (
                'deterministic sample correlation — statistical association '
                'under the declared sampler, not a causal decomposition',
            ),
        }
    else:
        return None

    provisional = SensitivityStudy.model_construct(
        **canonicalize_payload(SensitivityStudy, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['study_id'] = f'sensitivity-study:{digest}'
    return SensitivityStudy(**payload)


def _pairwise_outcomes(
    *,
    candidate_ids: Sequence[str],
    samples: Sequence[PropagatedSample],
    objective_directions: Mapping[str, Literal['minimize', 'maximize']],
) -> tuple[PairwiseRobustOutcome, ...]:
    """Paired outcomes: per observable, shared-state reversal + interval."""
    by_candidate: dict[str, list[PropagatedSample]] = {
        candidate_id: [] for candidate_id in candidate_ids
    }
    for sample in samples:
        by_candidate[sample.candidate_id].append(sample)

    summaries: dict[tuple[str, str], ObservablePropagationSummary] = {}
    for candidate_id, own in by_candidate.items():
        for observable_id in objective_directions:
            summary = summarize_observable(
                observable_id=observable_id, samples=own
            )
            if summary is not None:
                summaries[(candidate_id, observable_id)] = summary

    outcomes: list[PairwiseRobustOutcome] = []
    for position, candidate_a in enumerate(candidate_ids):
        for candidate_b in candidate_ids[position + 1:]:
            states_a = {
                sample.sample_index: sample
                for sample in by_candidate[candidate_a]
            }
            states_b = {
                sample.sample_index: sample
                for sample in by_candidate[candidate_b]
            }
            for observable_id, direction in objective_directions.items():
                summary_a = summaries.get((candidate_a, observable_id))
                summary_b = summaries.get((candidate_b, observable_id))
                if summary_a is None or summary_b is None:
                    continue
                nominal_diff = summary_a.nominal - summary_b.nominal
                overlap = not (
                    summary_a.sampled_max < summary_b.sampled_min
                    or summary_b.sampled_max < summary_a.sampled_min
                )
                sign = -1.0 if direction == 'minimize' else 1.0
                a_worst = (
                    summary_a.sampled_max if sign < 0 else summary_a.sampled_min
                )
                a_best = (
                    summary_a.sampled_min if sign < 0 else summary_a.sampled_max
                )
                b_worst = (
                    summary_b.sampled_max if sign < 0 else summary_b.sampled_min
                )
                b_best = (
                    summary_b.sampled_min if sign < 0 else summary_b.sampled_max
                )
                if a_worst < b_best:
                    state: DominanceState = 'robustly_better'
                elif a_best > b_worst:
                    state = 'robustly_worse'
                elif overlap:
                    state = 'overlapping'
                else:
                    state = 'nominal_only'

                shared = [
                    index
                    for index in states_a.keys() & states_b.keys()
                    if index != 0
                    and observable_id in states_a[index].observable_values
                    and observable_id in states_b[index].observable_values
                ]
                reversals = 0
                reversal_fraction: float | None = None
                if shared and nominal_diff != 0.0:
                    for index in shared:
                        diff = (
                            states_a[index].observable_values[observable_id]
                            - states_b[index].observable_values[observable_id]
                        )
                        if diff * nominal_diff < 0.0:
                            reversals += 1
                    reversal_fraction = reversals / len(shared)
                elif not shared:
                    reversal_fraction = None

                outcomes.append(
                    PairwiseRobustOutcome(
                        candidate_a=candidate_a,
                        candidate_b=candidate_b,
                        observable_id=observable_id,
                        nominal_difference=nominal_diff,
                        dominance_state=state,
                        paired_reversal_fraction=reversal_fraction,
                        shared_evaluated_count=len(shared),
                        interval_overlap=overlap,
                    )
                )
    return tuple(outcomes)


def build_robust_design_assessment(
    *,
    input_set: UncertainInputSet,
    spec: PropagationSpec,
    samples: Sequence[PropagatedSample],
    candidate_ids: Sequence[str],
    objective_ids: Sequence[str],
    objective_directions: Mapping[str, Literal['minimize', 'maximize']] | None = None,
    evidence_requests: Sequence[str] = (),
    limitations: Sequence[str] = (),
    created_at_utc: str,
) -> RobustDesignAssessment:
    """Seal the propagated evidence into one assessment record (§9/§10)."""
    directions = dict(objective_directions or {})
    if not objective_ids:
        raise ValueError('a robust design assessment requires observables')
    for observable_id in objective_ids:
        directions.setdefault(observable_id, 'minimize')

    profiles: list[CandidatePropagationProfile] = []
    for candidate_id in candidate_ids:
        own = [
            sample for sample in samples if sample.candidate_id == candidate_id
        ]
        if not own:
            raise ValueError(f'no propagated samples for candidate {candidate_id!r}')
        summaries = tuple(
            summary
            for observable_id in objective_ids
            if (
                summary := summarize_observable(
                    observable_id=observable_id, samples=own
                )
            )
            is not None
        )
        if not summaries:
            raise ValueError(
                f'candidate {candidate_id!r} produced no observable evidence'
            )
        profiles.append(
            CandidatePropagationProfile(
                candidate_id=candidate_id,
                summaries=summaries,
                infeasible_sample_count=sum(
                    1 for sample in own if not sample.feasible
                ),
                failed_sample_count=sum(
                    1 for sample in own if sample.failure_reason is not None
                ),
            )
        )

    pairwise = _pairwise_outcomes(
        candidate_ids=candidate_ids,
        samples=samples,
        objective_directions=directions,
    )

    studies: list[SensitivityStudy] = []
    for candidate_id in candidate_ids:
        study = compute_sensitivity(
            input_set=input_set,
            spec=spec,
            samples=samples,
            candidate_id=candidate_id,
        )
        if study is not None:
            studies.append(study)
    sensitivity = studies[0] if len(studies) == 1 else None
    dominant: list[str] = []
    if len(studies) == 1 and studies[0] is not None:
        aggregate: dict[str, float] = {}
        for item in studies[0].contributions:
            aggregate[item.input_id] = aggregate.get(item.input_id, 0.0) + abs(
                item.value
            )
        dominant = [
            input_id
            for input_id, _value in sorted(
                aggregate.items(), key=lambda pair: (-pair[1], pair[0])
            )[:3]
            if aggregate[input_id] > 0.0
        ]
    elif len(studies) > 1:
        limitations = tuple(limitations) + (
            'per-candidate sensitivity studies differ — reporting none at '
            'the assessment level rather than averaging incompatible methods',
        )

    payload: dict[str, Any] = {
        'schema_version': PROPAGATION_SCHEMA_VERSION,
        'authority_version': ROBUST_DESIGN_VERSION,
        'assessment_id': 'robust-design:' + '0' * 64,
        'semantic_sha256': '0' * 64,
        'document_id': input_set.document_id,
        'scene_revision_id': input_set.scene_revision_id,
        'scene_content_hash': input_set.scene_content_hash,
        'input_set_id': input_set.input_set_id,
        'input_set_sha256': input_set.semantic_sha256,
        'propagation_spec_id': spec.propagation_spec_id,
        'propagation_spec_sha256': spec.semantic_sha256,
        'candidates': tuple(profiles),
        'pairwise': pairwise,
        'sensitivity': sensitivity,
        'dominant_input_ids': tuple(dominant),
        'samples': tuple(samples),
        'limitations': tuple(limitations),
        'evidence_requests': tuple(evidence_requests),
        'created_at_utc': created_at_utc,
    }
    provisional = RobustDesignAssessment.model_construct(
        **canonicalize_payload(RobustDesignAssessment, dict(payload))
    )
    digest = _hash(provisional.identity_payload())
    payload['semantic_sha256'] = digest
    payload['assessment_id'] = f'robust-design:{digest}'
    return RobustDesignAssessment(**payload)


__all__ = [
    'CandidatePropagationProfile',
    'DeltaSemantics',
    'DominanceState',
    'ExplicitInputState',
    'InputRepresentation',
    'MAX_PROPAGATION_SAMPLES',
    'ObservablePropagationSummary',
    'PairwiseRobustOutcome',
    'PropagatedSample',
    'PropagatedState',
    'PropagationEvaluator',
    'PropagationExecutionResult',
    'PropagationMethod',
    'PropagationSpec',
    'PROPAGATION_ALGORITHM_VERSION',
    'PROPAGATION_SCHEMA_VERSION',
    'RobustDesignAssessment',
    'ROBUST_DESIGN_VERSION',
    'SensitivityContribution',
    'SensitivityMethod',
    'SensitivityStudy',
    'UncertainInput',
    'UncertainInputKind',
    'UncertainInputSet',
    'UNCERTAIN_INPUT_SET_VERSION',
    'PROPAGATION_SPEC_VERSION',
    'UncertaintyClass',
    'build_propagation_plan',
    'build_propagation_spec',
    'build_robust_design_assessment',
    'build_uncertain_input_set',
    'compute_sensitivity',
    'execute_propagation',
    'summarize_observable',
]
