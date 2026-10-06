"""Geometrical-acoustics solver numerical-fidelity authority
(#685, REV58-NUMERIC).

A geometrical-acoustics run is honest only when its *estimator* is
declared: how many rays were launched, on what distribution, into what
receiver model, truncated at which order, with which visibility
algorithm, and which convergence evidence exists. A receiver sphere is a
numerical estimator radius — not a microphone radius — and its spatial
resolution and multiplicity behavior change with ray density. A pinned
seed does not prove convergence. An image-source engine that found no
path did not prove none exists. This module is the fail-closed
declaration layer between *the GA solver ran* and *the GA result can be
claimed for a declared domain*.

Scope discipline (#685):

- The authority declares what the solver was and what was evaluated;
  it never derives physical accuracy and never substitutes a pinned seed
  or a large ray count for a convergence study.
- Deterministic evidence (image-source / beam visibility) and stochastic
  evidence (ray sampling) are different classes — one never certifies
  the other.
- A missed valid path, a duplicated path and an invalid accepted path
  are different failure classes and are declared separately (#685 §8).
- Seed-to-seed variance is *estimator* uncertainty, kept distinct from
  physical-parameter uncertainty (#604), measurement repeatability
  (#573) and optimizer spread (#675).
- A receiver-radius stability domain must be swept, not assumed — the
  radius trades missed rays against merged/double-counted paths
  (#685 §6/§7).

Records:

- :class:`GeometricalNumericalFidelityProfile` — the sealed solver
  declaration: algorithm family, truncation, visibility, ray launch,
  receiver estimator, path accounting, scattering sampling, termination
  and energy accounting.
- :class:`RaySamplingConvergence` — a sealed per-observable convergence
  study: ray-count sweeps and seed-to-seed spreads.
- :class:`PathEnumerationQualification` — a sealed deterministic-path
  enumeration qualification: which visibility cases are covered, to
  which order, against which exactness fixtures — the gate a downstream
  path-level consumer (#677) uses before treating a named path as
  'exact'.
- :class:`GeometricFidelityQualification` +
  :func:`evaluate_geometric_fidelity` — the fail-closed verdict.

Literature basis
----------------
- Lehnert (1993), "Systematic measurement errors of ray tracing room
  acoustic simulation" — systematic enumeration/estimator error classes
  this authority tracks separately.
- Zeng, Christensen & Sun (2003), "On the accuracy of the ray-tracing
  algorithms based on various receiver models", Applied Acoustics — DOI
  10.1016/S0003-682X(02)00108-1. Basis for receiver-model declarations
  and radius-sensitivity sweeps.
- Laine, Siltanen, Lokki & Savioja (2009), "Accelerated room acoustic
  simulations with the beam tracing method" — deterministic beam
  visibility class.
- Naylor (1993), "ODEON — a hybrid algorithm for room acoustic
  simulation" — early-deterministic / late-stochastic hybrid class.
- Polack (1993), "Playing billiards in the concert hall: the
  mathematical foundations of geometrical room acoustics" — ray-density
  and sphere-receiver foundations.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash, canonicalize_payload
from .clock import utc_now_iso as _utc_now


GA_FIDELITY_SCHEMA_VERSION = 'ga-numerical-fidelity-1'
GA_FIDELITY_EVALUATION_VERSION = 'ga-fidelity-eval-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


def _require_iso8601(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')


def _require_finite(value: float, label: str) -> None:
    if not isfinite(float(value)):
        raise ValueError(f'{label} must be finite')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal(
    model: type[BaseModel],
    payload: dict[str, Any],
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    probe = model.model_construct(
        **canonicalize_payload(model, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return model(
        **probe.model_dump(mode='python', exclude={id_field, sha_field}),
        **{
            sha_field: digest,
            id_field: _semantic_id(prefix, digest),
        },
    )


# ----------------------------------------------------------------------
# Taxonomies

GaAlgorithmFamily = Literal[
    'image_source',
    'beam_tracing',
    'ray_tracing',
    'pyramid_cone_frustum',
    'hybrid_early_deterministic_late_stochastic',
    'radiosity',
    'stochastic_secondary_source',
    'custom_validated',
]
"""Algorithm family — decides which declaration blocks are required
(#685 §1)."""

GaPathEvidenceClass = Literal[
    'deterministic_path_enumeration',
    'deterministic_beam_visibility',
    'stochastic_ray_sample',
    'statistical_late_field',
    'derived_hybrid',
]
"""What kind of evidence a produced path/energy record is (#685 §2)."""

GaReceiverModel = Literal[
    'spherical_capture',
    'adaptive_radius',
    'path_intersection_exact',
    'beam_footprint',
    'kernel_density_estimator',
    'other',
]
"""The receiver is a *numerical estimator* — its radius trades missed
rays against merged/double-counted paths and sets spatial resolution;
it is not a physical microphone (#685 §6)."""

GaVisibilityCase = Literal[
    'concave_polygon',
    'portal_opening',
    'grazing_path',
    'finite_reflector',
    'self_intersection',
    'occluding_obstruction',
    'epsilon_boundary',
]
"""The visibility/path-correctness cases a deterministic engine must
declare coverage for (#685 §4)."""

PathMissClass = Literal[
    'miss_valid_path',
    'count_path_multiple_times',
    'count_invalid_path',
    'merge_path_cluster',
]
"""Distinct path-accounting failure classes (#685 §8) — never
collapsed into 'count error'."""

GaFixtureId = Literal[
    'GAN10', 'GAN20', 'GAN30', 'GAN40', 'GAN50', 'GAN60', 'GAN70',
    'GAN80', 'GAN90',
]
"""#685 §17 fixtures:

- GAN10 analytic free-field decay,
- GAN20 image-source exact path set (rectangular room),
- GAN30 non-convex / occlusion correctness,
- GAN40 boundary specular correctness,
- GAN50 path enumeration against analytic list,
- GAN60 ray-count convergence,
- GAN70 receiver-radius sensitivity,
- GAN80 seed-to-seed variance,
- GAN90 energy-conservation accounting.
"""

ConvergenceStatus = Literal[
    'converged', 'not_converged', 'insufficient_evidence', 'not_evaluated'
]
"""Per-observable convergence status — a pinned seed alone never
reaches 'converged' (#685 §12)."""

GaFidelityState = Literal[
    'qualified_for_declared_domain',
    'qualified_with_limitations',
    'insufficient_evidence',
    'not_qualified',
]

GaNamedPathClass = Literal[
    'exact_ga_path_eligible', 'monte_carlo_only', 'unqualified'
]
"""Whether a named path may be treated as an exact GA path by a
downstream consumer (#677): only deterministic engines with the
relevant visibility cases covered qualify."""

GaObservable = Literal[
    'edc_shape',
    'band_energy',
    'arrival_density',
    'directional_distribution',
    'seat_to_seat',
    'rt60',
    'named_path_set',
    'other_declared',
]


# ----------------------------------------------------------------------
# Declared solver blocks


class GaAlgorithmIdentity(BaseModel):
    """Which GA algorithm produced the result (#685 §1/§2)."""

    model_config = ConfigDict(frozen=True)

    family: GaAlgorithmFamily
    implementation: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    path_classes: tuple[GaPathEvidenceClass, ...] = Field(
        min_length=1
    )
    """Which evidence classes this solver emits — e.g. an image-source
    engine emits ``deterministic_path_enumeration``, a ray tracer emits
    ``stochastic_ray_sample`` and usually ``statistical_late_field``."""

    @model_validator(mode='after')
    def _check(self) -> 'GaAlgorithmIdentity':
        if len(set(self.path_classes)) != len(self.path_classes):
            raise ValueError('path_classes must be unique')
        deterministic_families = {
            'image_source',
            'beam_tracing',
            'pyramid_cone_frustum',
        }
        stochastic_families = {
            'ray_tracing',
            'radiosity',
            'stochastic_secondary_source',
        }
        classes = set(self.path_classes)
        if self.family in deterministic_families and not (
            classes
            & {
                'deterministic_path_enumeration',
                'deterministic_beam_visibility',
            }
        ):
            raise ValueError(
                'a deterministic family must declare a deterministic '
                'path evidence class'
            )
        if self.family in stochastic_families and not (
            classes & {'stochastic_ray_sample', 'statistical_late_field'}
        ):
            raise ValueError(
                'a stochastic family must declare a stochastic path '
                'evidence class'
            )
        if self.family == 'custom_validated' and not classes:
            raise ValueError(
                'a custom GA family must name its evidence classes'
            )
        return self


class PathTruncationSpec(BaseModel):
    """When deterministic enumeration stops (#685 §3)."""

    model_config = ConfigDict(frozen=True)

    max_reflection_order: int | None = Field(default=None, ge=0)
    max_path_time_s: float | None = Field(default=None, gt=0.0)
    max_path_length_m: float | None = Field(default=None, gt=0.0)
    energy_cutoff: float | None = Field(default=None, gt=0.0)
    pruning_rules: str | None = None
    visibility_algorithm: str = Field(min_length=1)
    """Visibility/intersection algorithm name + version identity."""
    order_sensitivity_evaluated: Literal[
        'evaluated', 'not_evaluated'
    ] = 'not_evaluated'

    @model_validator(mode='after')
    def _check(self) -> 'PathTruncationSpec':
        if not any(
            value is not None
            for value in (
                self.max_reflection_order,
                self.max_path_time_s,
                self.max_path_length_m,
                self.energy_cutoff,
            )
        ):
            raise ValueError(
                'a truncation spec must declare at least one '
                'truncation rule'
            )
        for value, label in (
            (self.max_path_time_s, 'max_path_time_s'),
            (self.max_path_length_m, 'max_path_length_m'),
            (self.energy_cutoff, 'energy_cutoff'),
        ):
            if value is not None:
                _require_finite(value, f'truncation {label}')
        return self


class GaVisibilityCaseResult(BaseModel):
    """Declared outcome of one visibility correctness case."""

    model_config = ConfigDict(frozen=True)

    case: GaVisibilityCase
    state: Literal['tested', 'untested', 'not_applicable']
    fixture_ref: AuthorityRef | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'GaVisibilityCaseResult':
        if self.fixture_ref is not None and (
            self.fixture_ref.ref_sha256 is None
        ):
            raise ValueError('visibility fixture_ref must pin sha256')
        if self.state == 'tested' and self.fixture_ref is None and (
            not self.detail
        ):
            raise ValueError(
                'a tested visibility case must cite a fixture or '
                'detail'
            )
        return self


class VisibilitySpec(BaseModel):
    """How the engine decides path existence (#685 §4).

    Path correctness is upstream of physical modeling: a path the engine
    reports as absent is not 'the material absorbed it'.
    """

    model_config = ConfigDict(frozen=True)

    intersection_tolerance_m: float = Field(gt=0.0)
    geometry_version: str = Field(min_length=1)
    """The scene/shape revision the intersections ran against."""
    case_results: tuple[GaVisibilityCaseResult, ...] = ()

    @model_validator(mode='after')
    def _check(self) -> 'VisibilitySpec':
        _require_finite(
            self.intersection_tolerance_m, 'intersection_tolerance_m'
        )
        cases = [entry.case for entry in self.case_results]
        if len(set(cases)) != len(cases):
            raise ValueError('visibility case results must be unique')
        return self

    def case_state(self, case: GaVisibilityCase) -> str:
        for entry in self.case_results:
            if entry.case == case:
                return entry.state
        return 'untested'


class RayLaunchSpec(BaseModel):
    """The stochastic launch declaration (#685 §5).

    A pinned seed makes a run reproducible — it never proves the
    estimator converged.
    """

    model_config = ConfigDict(frozen=True)

    launched_ray_count: int = Field(ge=1)
    launch_distribution: str = Field(min_length=1)
    """``uniform_sphere`` / ``cosine_weighted`` / declared other."""
    directivity_sampling: str | None = None
    low_discrepancy: str | None = None
    """``none`` / ``halton`` / ``sobol`` / declared other."""
    rng_name: str | None = None
    seed: int | None = None
    band_sampling: str | None = None
    """Per-band vs full-band launch policy."""

    @model_validator(mode='after')
    def _check(self) -> 'RayLaunchSpec':
        if self.seed is not None and not self.rng_name:
            raise ValueError(
                'a pinned seed must also name its RNG'
            )
        return self


class ReceiverRadiusStudy(BaseModel):
    """Swept receiver-radius stability domain (#685 §7)."""

    model_config = ConfigDict(frozen=True)

    swept_radii_m: tuple[float, ...] = Field(min_length=2)
    observable: str = Field(min_length=1)
    stable_band_m: tuple[float, float] | None = None
    """The radius range over which the observable stayed within the
    declared tolerance."""
    state: Literal[
        'stable_domain_found', 'variance_dominated',
        'insufficient_evidence',
    ]

    @model_validator(mode='after')
    def _check(self) -> 'ReceiverRadiusStudy':
        for radius in self.swept_radii_m:
            _require_finite(radius, 'swept radius')
            if radius <= 0.0:
                raise ValueError('swept radii must be positive')
        if len(set(self.swept_radii_m)) != len(self.swept_radii_m):
            raise ValueError('swept radii must be unique')
        if self.stable_band_m is not None:
            low, high = self.stable_band_m
            if low >= high:
                raise ValueError('stable_band_m must be low < high')
        if (
            self.state == 'stable_domain_found'
            and self.stable_band_m is None
        ):
            raise ValueError(
                'a found stability domain must state its band'
            )
        return self


class ReceiverEstimatorSpec(BaseModel):
    """The receiver/estimator declaration (#685 §6)."""

    model_config = ConfigDict(frozen=True)

    model: GaReceiverModel
    radius_m: float | None = Field(default=None, gt=0.0)
    radius_dependence: str | None = None
    """How the radius varies — ``fixed`` / ``path_time_scaled`` /
    ``ray_density_scaled``."""
    kernel_bandwidth: str | None = None
    normalization: str = Field(min_length=1)
    """How detected hits are converted to energy/pressure."""
    duplicate_hit_policy: str = Field(min_length=1)
    """What one ray hitting the receiver multiple times counts as."""
    estimator_version: str = Field(min_length=1)
    radius_sensitivity: ReceiverRadiusStudy | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ReceiverEstimatorSpec':
        if self.model in ('spherical_capture', 'adaptive_radius') and (
            self.radius_m is None
        ):
            raise ValueError(
                'spherical/adaptive receiver models must declare a '
                'radius'
            )
        if self.model == 'path_intersection_exact' and (
            self.radius_m is not None
        ):
            raise ValueError(
                'an exact path-intersection receiver does not carry a '
                'capture radius'
            )
        if self.radius_m is not None:
            _require_finite(self.radius_m, 'receiver radius_m')
        return self


class PathAccountingSpec(BaseModel):
    """Which path-accounting failure modes the estimator can produce
    (#685 §8)."""

    model_config = ConfigDict(frozen=True)

    declared_miss_classes: tuple[PathMissClass, ...] = ()
    normalization_corrects_multiplicity: bool = False
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'PathAccountingSpec':
        if len(set(self.declared_miss_classes)) != len(
            self.declared_miss_classes
        ):
            raise ValueError('miss classes must be unique')
        return self


class TerminationPolicy(BaseModel):
    """How stochastic paths end (#685 §10)."""

    model_config = ConfigDict(frozen=True)

    rule: Literal[
        'russian_roulette',
        'deterministic_cutoff',
        'energy_threshold',
        'max_bounces',
        'none',
        'other',
    ]
    probability_or_threshold: str | None = None
    weight_compensation: str | None = None
    """How terminated paths' energy is preserved or deliberately not."""
    max_bounces: int | None = Field(default=None, ge=0)
    max_time_s: float | None = Field(default=None, gt=0.0)
    unbiasedness_checked: Literal[
        'checked', 'not_checked', 'not_applicable'
    ] = 'not_checked'


class ScatteringSamplingSpec(BaseModel):
    """Scattering stochasticity (#685 §9).

    The physical scattering input (coefficients, correlation lengths)
    belongs to #252 — this block declares only the *sampling* of that
    model: distribution applied, secondary-ray count, seeds, split
    rules and termination.
    """

    model_config = ConfigDict(frozen=True)

    distribution: str = Field(min_length=1)
    """The applied angular distribution — ``lambert`` /
    ``uniform_hemisphere`` / declared other."""
    secondary_ray_count: int | None = Field(default=None, ge=0)
    seed: int | None = None
    energy_split_rule: str | None = None
    termination: TerminationPolicy | None = None
    variance_note: str | None = None


class EnergyAccountingRecord(BaseModel):
    """Conservation check across the launch (#685 §11).

    Emitted, absorbed, escaped, receiver-estimated and terminated
    contributions are tracked separately; an untracked sink must be
    declared as a limitation, not absorbed silently.
    """

    model_config = ConfigDict(frozen=True)

    emitted: float | None = None
    absorbed_boundary: float | None = None
    escaped_portal: float | None = None
    receiver_estimated: float | None = None
    remaining_terminated: float | None = None
    balance_residual: float | None = None
    tolerance: float | None = Field(default=None, ge=0.0)
    untracked_sink_limitation: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'EnergyAccountingRecord':
        for label in (
            'emitted',
            'absorbed_boundary',
            'escaped_portal',
            'receiver_estimated',
            'remaining_terminated',
            'balance_residual',
            'tolerance',
        ):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, f'energy {label}')
        return self


class SeedSpreadStudy(BaseModel):
    """Seed-to-seed estimator spread for one observable (#685 §12).

    Separate from physical-parameter uncertainty (#604), measurement
    repeatability (#573) and optimizer spread (#675).
    """

    model_config = ConfigDict(frozen=True)

    observable: str = Field(min_length=1)
    seeds: tuple[int, ...] = Field(min_length=2)
    relative_spread: float | None = Field(default=None, ge=0.0)
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'SeedSpreadStudy':
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError('seeds must be distinct')
        if self.relative_spread is not None:
            _require_finite(self.relative_spread, 'relative_spread')
        return self


class GaConvergenceEvidence(BaseModel):
    """Per-observable convergence declaration (#685 §12)."""

    model_config = ConfigDict(frozen=True)

    observable: GaObservable | str = Field(min_length=1)
    status: ConvergenceStatus
    ray_counts_tested: tuple[int, ...] = ()
    seed_spread: SeedSpreadStudy | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'GaConvergenceEvidence':
        if len(set(self.ray_counts_tested)) != len(
            self.ray_counts_tested
        ):
            raise ValueError('ray_counts_tested must be unique')
        if self.status == 'converged' and not (
            len(self.ray_counts_tested) >= 2
            or self.seed_spread is not None
            or self.detail
        ):
            raise ValueError(
                'a converged status must cite ray-count or '
                'seed-spread evidence'
            )
        return self


class GaFixtureResult(BaseModel):
    """Outcome on one #685 fixture (GAN10–GAN90)."""

    model_config = ConfigDict(frozen=True)

    fixture_id: GaFixtureId
    verdict: Literal['pass', 'fail', 'not_executed', 'not_applicable']
    metric_summary: str | None = None
    evidence_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _check(self) -> 'GaFixtureResult':
        if self.evidence_ref is not None and (
            self.evidence_ref.ref_sha256 is None
        ):
            raise ValueError('fixture evidence_ref must pin sha256')
        return self


# ----------------------------------------------------------------------
# Sealed records


class GeometricalNumericalFidelityProfile(BaseModel):
    """The sealed declaration of one GA-solver configuration (#685).

    ``solver_result_ref`` pins the solver-result envelope this profile
    describes; the profile sha256 pins every numerically material
    estimator setting — family, truncation, visibility, launch,
    receiver model, termination — so any change is a new profile.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    solver_result_ref: AuthorityRef | None = None
    algorithm: GaAlgorithmIdentity
    truncation: PathTruncationSpec | None = None
    visibility: VisibilitySpec | None = None
    ray_launch: RayLaunchSpec | None = None
    receiver: ReceiverEstimatorSpec
    path_accounting: PathAccountingSpec | None = None
    scattering_sampling: ScatteringSamplingSpec | None = None
    termination: TerminationPolicy | None = None
    energy_accounting: EnergyAccountingRecord | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=GA_FIDELITY_SCHEMA_VERSION, min_length=1
    )
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'solver_result_ref': (
                self.solver_result_ref.model_dump(mode='json')
                if self.solver_result_ref is not None
                else None
            ),
            'algorithm': self.algorithm.model_dump(mode='json'),
            'truncation': (
                self.truncation.model_dump(mode='json')
                if self.truncation is not None
                else None
            ),
            'visibility': (
                self.visibility.model_dump(mode='json')
                if self.visibility is not None
                else None
            ),
            'ray_launch': (
                self.ray_launch.model_dump(mode='json')
                if self.ray_launch is not None
                else None
            ),
            'receiver': self.receiver.model_dump(mode='json'),
            'path_accounting': (
                self.path_accounting.model_dump(mode='json')
                if self.path_accounting is not None
                else None
            ),
            'scattering_sampling': (
                self.scattering_sampling.model_dump(mode='json')
                if self.scattering_sampling is not None
                else None
            ),
            'termination': (
                self.termination.model_dump(mode='json')
                if self.termination is not None
                else None
            ),
            'energy_accounting': (
                self.energy_accounting.model_dump(mode='json')
                if self.energy_accounting is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'GeometricalNumericalFidelityProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.solver_result_ref is not None and (
            self.solver_result_ref.ref_sha256 is None
        ):
            raise ValueError('solver_result_ref must pin its sha256')
        deterministic = {
            'image_source',
            'beam_tracing',
            'pyramid_cone_frustum',
        }
        stochastic = {
            'ray_tracing',
            'radiosity',
            'stochastic_secondary_source',
        }
        family = self.algorithm.family
        needs_deterministic = family in deterministic or family == (
            'hybrid_early_deterministic_late_stochastic'
        )
        needs_stochastic = family in stochastic or family == (
            'hybrid_early_deterministic_late_stochastic'
        )
        if needs_deterministic:
            if self.truncation is None:
                raise ValueError(
                    'a deterministic-capable GA family requires the '
                    'truncation block'
                )
            if self.visibility is None:
                raise ValueError(
                    'a deterministic-capable GA family requires the '
                    'visibility block'
                )
        if needs_stochastic and self.ray_launch is None:
            raise ValueError(
                'a stochastic-capable GA family requires the ray '
                'launch block'
            )
        if family in deterministic and self.ray_launch is not None:
            raise ValueError(
                'a purely deterministic family does not carry a ray '
                'launch block'
            )
        if (
            family == 'custom_validated'
            and self.truncation is None
            and self.ray_launch is None
        ):
            raise ValueError(
                'a custom GA family must declare truncation and/or '
                'ray launch'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('geometric fidelity profile hash mismatch')
        if self.profile_id != _semantic_id('gnfprof', expected):
            raise ValueError(
                'geometric fidelity profile id does not match its hash'
            )
        return self


def geometric_profile_binding(
    profile: GeometricalNumericalFidelityProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='geometric_fidelity_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class RaySamplingConvergence(BaseModel):
    """A sealed per-observable convergence study (#685 §12/§13).

    A pinned seed alone never counts as convergence — evidence requires
    a ray-count sweep or an explicit seed-spread study.
    """

    model_config = ConfigDict(frozen=True)

    convergence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    evidence: tuple[GaConvergenceEvidence, ...] = Field(min_length=1)
    fixture_results: tuple[GaFixtureResult, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=GA_FIDELITY_SCHEMA_VERSION, min_length=1
    )
    convergence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'evidence': [
                entry.model_dump(mode='json') for entry in self.evidence
            ],
            'fixture_results': [
                result.model_dump(mode='json')
                for result in self.fixture_results
            ],
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'RaySamplingConvergence':
        _require_iso8601(
            self.declared_at_utc, 'convergence declared_at_utc'
        )
        if self.profile_ref.kind != 'geometric_fidelity_profile':
            raise ValueError(
                "profile_ref must pin a 'geometric_fidelity_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        observables = [str(entry.observable) for entry in self.evidence]
        if len(set(observables)) != len(observables):
            raise ValueError(
                'convergence evidence must be unique per observable'
            )
        expected = _hash(self.identity_payload())
        if self.convergence_sha256 != expected:
            raise ValueError('ray sampling convergence hash mismatch')
        if self.convergence_id != _semantic_id('raysconv', expected):
            raise ValueError(
                'ray convergence id does not match its hash'
            )
        return self


def ray_convergence_binding(
    record: RaySamplingConvergence,
) -> AuthorityRef:
    return AuthorityRef(
        kind='ray_sampling_convergence',
        ref_id=record.convergence_id,
        ref_sha256=record.convergence_sha256,
    )


class PathEnumerationQualification(BaseModel):
    """A sealed deterministic path-enumeration qualification
    (#685 §4/§8/§15).

    This is the record a downstream consumer (#677 reflection
    correspondence) consults before treating a named path as 'the exact
    GA path' — only a deterministic engine whose visibility cases are
    covered to a declared order qualifies.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    deterministic_state: Literal[
        'qualified', 'limited', 'unqualified', 'not_applicable'
    ]
    max_qualified_order: int | None = Field(default=None, ge=0)
    covered_cases: tuple[GaVisibilityCase, ...] = ()
    untested_cases: tuple[GaVisibilityCase, ...] = ()
    exactness_fixtures: tuple[GaFixtureResult, ...] = ()
    named_path_evidence_class: GaNamedPathClass
    reasons: tuple[str, ...] = ()
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=GA_FIDELITY_SCHEMA_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'deterministic_state': self.deterministic_state,
            'max_qualified_order': self.max_qualified_order,
            'covered_cases': list(self.covered_cases),
            'untested_cases': list(self.untested_cases),
            'exactness_fixtures': [
                result.model_dump(mode='json')
                for result in self.exactness_fixtures
            ],
            'named_path_evidence_class': self.named_path_evidence_class,
            'reasons': list(self.reasons),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'PathEnumerationQualification':
        _require_iso8601(
            self.declared_at_utc, 'enumeration declared_at_utc'
        )
        if self.profile_ref.kind != 'geometric_fidelity_profile':
            raise ValueError(
                "profile_ref must pin a 'geometric_fidelity_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        overlap = set(self.covered_cases) & set(self.untested_cases)
        if overlap:
            raise ValueError(
                'a visibility case cannot be both covered and untested'
            )
        if (
            self.named_path_evidence_class == 'exact_ga_path_eligible'
            and self.deterministic_state != 'qualified'
        ):
            raise ValueError(
                "exact path eligibility requires a 'qualified' "
                'enumeration state'
            )
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('path enumeration hash mismatch')
        if self.qualification_id != _semantic_id('pathqual', expected):
            raise ValueError(
                'path enumeration id does not match its hash'
            )
        return self


def path_enumeration_binding(
    qualification: PathEnumerationQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='path_enumeration_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


class GeometricFidelityQualification(BaseModel):
    """The sealed fail-closed verdict on a GA profile (#685 §12–§17)."""

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    convergence_ref: AuthorityRef | None = None
    enumeration_ref: AuthorityRef | None = None
    fidelity_state: GaFidelityState
    deterministic_state: Literal[
        'qualified', 'limited', 'unqualified', 'not_applicable'
    ]
    stochastic_state: Literal[
        'qualified', 'limited', 'unqualified', 'not_applicable'
    ]
    receiver_domain_state: Literal[
        'stable_domain_declared',
        'radius_unevaluated',
        'not_applicable',
    ]
    energy_accounting_state: Literal[
        'accounted', 'declared_limitation', 'unevaluated',
        'not_applicable',
    ]
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=GA_FIDELITY_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'convergence_ref': (
                self.convergence_ref.model_dump(mode='json')
                if self.convergence_ref is not None
                else None
            ),
            'enumeration_ref': (
                self.enumeration_ref.model_dump(mode='json')
                if self.enumeration_ref is not None
                else None
            ),
            'fidelity_state': self.fidelity_state,
            'deterministic_state': self.deterministic_state,
            'stochastic_state': self.stochastic_state,
            'receiver_domain_state': self.receiver_domain_state,
            'energy_accounting_state': self.energy_accounting_state,
            'reasons': list(self.reasons),
            'limitations': list(self.limitations),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'GeometricFidelityQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.kind != 'geometric_fidelity_profile':
            raise ValueError(
                "profile_ref must pin a 'geometric_fidelity_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        for ref, kind in (
            (self.convergence_ref, 'ray_sampling_convergence'),
            (self.enumeration_ref, 'path_enumeration_qualification'),
        ):
            if ref is not None:
                if ref.kind != kind:
                    raise ValueError(
                        f'expected an authority ref of kind {kind!r}'
                    )
                if ref.ref_sha256 is None:
                    raise ValueError('authority ref must pin its sha256')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError(
                'geometric fidelity qualification hash mismatch'
            )
        if self.qualification_id != _semantic_id('gnfqual', expected):
            raise ValueError(
                'geometric qualification id does not match its hash'
            )
        return self


def geometric_qualification_binding(
    qualification: GeometricFidelityQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='geometric_fidelity_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ----------------------------------------------------------------------
# Evaluation


_DETERMINISTIC_FAMILIES = {
    'image_source',
    'beam_tracing',
    'pyramid_cone_frustum',
}
_STOCHASTIC_FAMILIES = {
    'ray_tracing',
    'radiosity',
    'stochastic_secondary_source',
}


def evaluate_geometric_fidelity(
    document_id: str,
    profile: GeometricalNumericalFidelityProfile,
    sampling: RaySamplingConvergence | None = None,
    enumeration: PathEnumerationQualification | None = None,
    *,
    evaluated_at_utc: str | None = None,
) -> GeometricFidelityQualification:
    """Fail-closed verdict on a GA-fidelity profile (#685 §12–§17).

    - A deterministic family without a path-enumeration qualification
      stays at most ``qualified_with_limitations`` on the deterministic
      axis.
    - A stochastic family without a sampling-convergence record stays
      ``unqualified`` on the stochastic axis — a pinned seed is not
      evidence.
    - A spherical/adaptive receiver without a swept stability domain is
      ``radius_unevaluated`` — the verdict can at best be
      ``qualified_with_limitations``.
    - Missing energy accounting on a stochastic run is a declared
      limitation, not an assumption of conservation.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    profile_binding = geometric_profile_binding(profile)
    for record, label in (
        (sampling, 'ray-sampling convergence'),
        (enumeration, 'path-enumeration qualification'),
    ):
        if record is not None and (
            record.profile_ref.ref_id != profile_binding.ref_id
            or record.profile_ref.ref_sha256
            != profile_binding.ref_sha256
        ):
            raise ValueError(
                f'a {label} record must pin the profile under '
                'evaluation'
            )

    reasons: list[str] = []
    limitations: list[str] = []
    family = profile.algorithm.family
    is_deterministic = family in _DETERMINISTIC_FAMILIES or family == (
        'hybrid_early_deterministic_late_stochastic'
    )
    is_stochastic = family in _STOCHASTIC_FAMILIES or family == (
        'hybrid_early_deterministic_late_stochastic'
    )

    # --- deterministic axis ------------------------------------------------
    if not is_deterministic:
        deterministic_state = 'not_applicable'
    elif enumeration is None:
        deterministic_state = 'unqualified'
        reasons.append(
            'deterministic path enumeration was not qualified'
        )
    else:
        deterministic_state = enumeration.deterministic_state
        if enumeration.untested_cases:
            limitations.append(
                'visibility cases untested: '
                + ', '.join(enumeration.untested_cases)
            )

    # --- stochastic axis ---------------------------------------------------
    if not is_stochastic:
        stochastic_state = 'not_applicable'
    elif sampling is None:
        stochastic_state = 'unqualified'
        reasons.append(
            'stochastic ray sampling convergence was not declared'
        )
    else:
        statuses = {entry.status for entry in sampling.evidence}
        if 'not_converged' in statuses:
            stochastic_state = 'unqualified'
            reasons.append(
                'a declared observable failed to converge'
            )
        elif statuses and statuses <= {'converged'}:
            stochastic_state = 'qualified'
        elif 'insufficient_evidence' in statuses or (
            'not_evaluated' in statuses
        ):
            stochastic_state = 'limited'
            limitations.append(
                'some observables have incomplete convergence evidence'
            )
        else:
            stochastic_state = 'limited'

    # --- receiver stability domain -----------------------------------------
    receiver = profile.receiver
    if receiver.model in ('spherical_capture', 'adaptive_radius'):
        if (
            receiver.radius_sensitivity is not None
            and receiver.radius_sensitivity.state
            == 'stable_domain_found'
        ):
            receiver_domain_state = 'stable_domain_declared'
        else:
            receiver_domain_state = 'radius_unevaluated'
            limitations.append(
                'receiver-radius stability domain was not swept'
            )
    else:
        receiver_domain_state = 'not_applicable'

    # --- energy accounting ---------------------------------------------------
    energy = profile.energy_accounting
    if not is_stochastic:
        energy_accounting_state = 'not_applicable'
    elif energy is None:
        energy_accounting_state = 'unevaluated'
        limitations.append(
            'energy conservation was not tracked'
        )
    elif energy.untracked_sink_limitation:
        energy_accounting_state = 'declared_limitation'
        limitations.append(energy.untracked_sink_limitation)
    elif energy.balance_residual is not None and (
        energy.tolerance is not None
    ) and energy.balance_residual <= energy.tolerance:
        energy_accounting_state = 'accounted'
    else:
        energy_accounting_state = 'unevaluated'
        limitations.append(
            'energy balance was recorded without a closed residual'
        )

    # --- overall verdict -----------------------------------------------------
    component_states = [
        state
        for state in (deterministic_state, stochastic_state)
        if state != 'not_applicable'
    ]
    if 'unqualified' in component_states:
        fidelity_state: GaFidelityState = 'insufficient_evidence'
    elif (
        all(state == 'qualified' for state in component_states)
        and component_states
        and receiver_domain_state != 'radius_unevaluated'
        and energy_accounting_state in ('accounted', 'not_applicable')
    ):
        fidelity_state = 'qualified_for_declared_domain'
    elif 'unqualified' not in component_states and (
        'limited' in component_states
        or receiver_domain_state == 'radius_unevaluated'
        or energy_accounting_state
        in ('declared_limitation', 'unevaluated')
    ):
        fidelity_state = 'qualified_with_limitations'
    else:
        fidelity_state = 'insufficient_evidence'

    return _seal(
        GeometricFidelityQualification,
        {
            'document_id': document_id,
            'profile_ref': geometric_profile_binding(profile),
            'convergence_ref': (
                ray_convergence_binding(sampling)
                if sampling is not None
                else None
            ),
            'enumeration_ref': (
                path_enumeration_binding(enumeration)
                if enumeration is not None
                else None
            ),
            'fidelity_state': fidelity_state,
            'deterministic_state': deterministic_state,
            'stochastic_state': stochastic_state,
            'receiver_domain_state': receiver_domain_state,
            'energy_accounting_state': energy_accounting_state,
            'reasons': sorted(set(reasons)),
            'limitations': sorted(set(limitations)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': GA_FIDELITY_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'gnfqual',
    )


# ----------------------------------------------------------------------
# Builders


def build_geometric_fidelity_profile(
    document_id: str,
    algorithm: GaAlgorithmIdentity,
    receiver: ReceiverEstimatorSpec,
    *,
    solver_result_ref: AuthorityRef | None = None,
    truncation: PathTruncationSpec | None = None,
    visibility: VisibilitySpec | None = None,
    ray_launch: RayLaunchSpec | None = None,
    path_accounting: PathAccountingSpec | None = None,
    scattering_sampling: ScatteringSamplingSpec | None = None,
    termination: TerminationPolicy | None = None,
    energy_accounting: EnergyAccountingRecord | None = None,
    declared_at_utc: str | None = None,
) -> GeometricalNumericalFidelityProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        GeometricalNumericalFidelityProfile,
        {
            'document_id': document_id,
            'solver_result_ref': (
                solver_result_ref.model_dump(mode='json')
                if solver_result_ref is not None
                else None
            ),
            'algorithm': algorithm.model_dump(mode='json'),
            'truncation': (
                truncation.model_dump(mode='json')
                if truncation is not None
                else None
            ),
            'visibility': (
                visibility.model_dump(mode='json')
                if visibility is not None
                else None
            ),
            'ray_launch': (
                ray_launch.model_dump(mode='json')
                if ray_launch is not None
                else None
            ),
            'receiver': receiver.model_dump(mode='json'),
            'path_accounting': (
                path_accounting.model_dump(mode='json')
                if path_accounting is not None
                else None
            ),
            'scattering_sampling': (
                scattering_sampling.model_dump(mode='json')
                if scattering_sampling is not None
                else None
            ),
            'termination': (
                termination.model_dump(mode='json')
                if termination is not None
                else None
            ),
            'energy_accounting': (
                energy_accounting.model_dump(mode='json')
                if energy_accounting is not None
                else None
            ),
            'authority_version': GA_FIDELITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'gnfprof',
    )


def build_ray_sampling_convergence(
    document_id: str,
    profile_ref: AuthorityRef,
    evidence: Sequence[GaConvergenceEvidence],
    *,
    fixture_results: Sequence[GaFixtureResult] = (),
    declared_at_utc: str | None = None,
) -> RaySamplingConvergence:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        RaySamplingConvergence,
        {
            'document_id': document_id,
            'profile_ref': profile_ref.model_dump(mode='json'),
            'evidence': [
                entry.model_dump(mode='json') for entry in evidence
            ],
            'fixture_results': [
                result.model_dump(mode='json')
                for result in fixture_results
            ],
            'authority_version': GA_FIDELITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'convergence_id',
        'convergence_sha256',
        'raysconv',
    )


def build_path_enumeration_qualification(
    document_id: str,
    profile_ref: AuthorityRef,
    deterministic_state: Literal[
        'qualified', 'limited', 'unqualified', 'not_applicable'
    ],
    named_path_evidence_class: GaNamedPathClass,
    *,
    max_qualified_order: int | None = None,
    covered_cases: Sequence[GaVisibilityCase] = (),
    untested_cases: Sequence[GaVisibilityCase] = (),
    exactness_fixtures: Sequence[GaFixtureResult] = (),
    reasons: Sequence[str] = (),
    declared_at_utc: str | None = None,
) -> PathEnumerationQualification:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        PathEnumerationQualification,
        {
            'document_id': document_id,
            'profile_ref': profile_ref.model_dump(mode='json'),
            'deterministic_state': deterministic_state,
            'max_qualified_order': max_qualified_order,
            'covered_cases': list(covered_cases),
            'untested_cases': list(untested_cases),
            'exactness_fixtures': [
                result.model_dump(mode='json')
                for result in exactness_fixtures
            ],
            'named_path_evidence_class': named_path_evidence_class,
            'reasons': list(reasons),
            'authority_version': GA_FIDELITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'qualification_id',
        'qualification_sha256',
        'pathqual',
    )
