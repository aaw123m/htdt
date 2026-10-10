"""Wave-solver numerical-fidelity authority (#683, REV58-NUMERIC).

A room-acoustic wave simulation is only as honest as its numerical
declaration: an FDTD run that satisfies its CFL bound can still carry a
dispersion error that delays arrivals, a FEM result whose element order
under-resolves the wavenumber inherits a pollution error that refinement
hides slowly, and a PML that was never characterized is an artificial
boundary whose leakage nobody measured. This module is the fail-closed
declaration layer between *the solver ran* and *the solver result can be
claimed numerically faithful for a declared band* — it organizes the
solver formulation, discretization identity, per-error-class evidence and
convergence studies and refuses to attach an accuracy claim to an
undescribed configuration.

Scope discipline (#683):

- This authority declares *what the solver configuration was* and *which
  numerical error classes were evaluated*. It never derives physical
  accuracy, never converts a converged algebraic residual into
  discretization convergence, and never lets a magnitude-only check stand
  in for phase/timing evidence.
- Error classes are tracked separately — numerical dispersion, FEM
  pollution, CFL/stability, artificial-boundary leakage, geometry
  discretization, source discretization, receiver interpolation and
  algebraic-solver tolerance are never collapsed into a single "error"
  number (#683 §17).
- A computational absorbing boundary (PML / absorbing layer / sponge) is
  not an anechoic physical assumption; the physical termination claim
  lives in :class:`AdjacentTerminationSpec` (#683 §8).
- A pinned cache identity covers every numerically material setting —
  any change to mesh, time step, boundary treatment, source injection or
  solver version produces a different sealed profile.
- Qualification is per observable and per frequency band — a solver can
  be qualified for pressure magnitude in one band and unqualified for
  phase or for a higher band (#683 §14).

Records:

- :class:`WaveNumericalFidelityProfile` — the sealed declaration of the
  solver formulation, the discretization identity (mesh/grid hash),
  family-specific numerical settings (FDTD time stepping, FEM numerics,
  BEM numerics), the artificial boundary and adjacent termination, the
  algebraic-solve evidence, source/receiver discretization and any
  measured dispersion evidence.
- :class:`NumericalConvergenceRecord` — a sealed convergence study:
  refinement levels with per-observable comparisons, fixture results
  against the WNV fixtures, or a cross-solver comparison with its
  independence limitation declared.
- :class:`WaveFidelityQualification` +
  :func:`evaluate_wave_fidelity` — the fail-closed verdict: per
  error-class state, per-observable band qualification, and the overall
  fidelity state.

Literature basis
----------------
- Li, Botts & Nocke (2022), "Time-domain room acoustic simulations with
  extended-reacting porous absorbers using the discontinuous Galerkin /
  FDTD vs. FMBEM benchmarks", Applied Acoustics — DOI
  10.1016/j.apacoust.2022.108662. Basis for the WNV fixture taxonomy and
  the solver-family separation of numerical evidence.
- Okuzono, Yoshida, Sakagami & Otsuru (2014), "An explicit time-domain
  finite element method for room acoustics simulations using dispersion
  error reduction" — DOI 10.1016/j.apacoust.2013.12.010. Basis for FEM
  pollution/dispersion separation and element-order tracking.
- Qi & Geers (1998), "Evaluation of the perfectly matched layer for
  computational acoustics", JCP — DOI 10.1006/jcph.1997.5868. Basis for
  treating artificial boundaries as measurable error sources rather than
  assumed anechoic terminations.
- Easwaran & Craggs (1995), "On further validation and use of the finite
  element method to room acoustics", JSV — DOI 10.1006/jsvi.1995.0515.
  Basis for per-band qualification on modal/presssure observables.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_resolver import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash, canonicalize_payload
from ...clock import utc_now_iso as _utc_now


WAVE_FIDELITY_SCHEMA_VERSION = 'wave-numerical-fidelity-1'
WAVE_FIDELITY_EVALUATION_VERSION = 'wave-fidelity-eval-1'

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

WaveSolverFamily = Literal[
    'fdtd',
    'fem',
    'bem',
    'fmbem',
    'dg',
    'spectral',
    'other_validated',
]
"""Numerical solver family — each family carries its own numerical
error structure and therefore its own mandatory declaration block."""

WaveSolverDomain = Literal[
    'time_domain', 'frequency_domain', 'laplace_domain', 'other_declared'
]

NumericalPrecision = Literal['float64', 'float32', 'mixed', 'other']

WaveFixtureId = Literal[
    'WNV10', 'WNV20', 'WNV30', 'WNV40', 'WNV50', 'WNV60', 'WNV70', 'WNV80'
]
"""#683 §16 fixture identities:

- WNV10 analytic free-field decay,
- WNV20 1D plane-wave propagation dispersion/timing,
- WNV30 rectangular-room eigenfrequencies,
- WNV40 impedance-tube / material benchmark,
- WNV50 low-frequency modal response,
- WNV60 wide-band room-acoustic benchmark,
- WNV70 PML outgoing-wave fixture,
- WNV80 cross-solver comparison suite.
"""

WaveErrorClass = Literal[
    'numerical_dispersion',
    'fem_pollution',
    'cfl_stability',
    'artificial_boundary',
    'geometry_discretization',
    'source_discretization',
    'receiver_interpolation',
    'algebraic_tolerance',
]
"""Separately-tracked numerical error classes (#683 §17) — never
collapsed into one number, never silently substituted for each other."""

ErrorClassState = Literal[
    'qualified', 'limited', 'unresolved', 'not_applicable'
]
"""`qualified`: declared and evidence-backed for the observable.
`limited`: declared, partially evaluated. `unresolved`: declared but
unevaluated. `not_applicable`: this class does not exist for the
formulation (e.g. CFL for a frequency-domain solve)."""

FidelityState = Literal[
    'qualified_for_declared_domain',
    'qualified_with_limitations',
    'insufficient_evidence',
    'not_qualified',
]

WaveStabilityState = Literal[
    'within_declared_stability', 'outside_declared_stability',
    'not_evaluated',
]

WaveObservable = Literal[
    'complex_pressure',
    'pressure_magnitude_spectrum',
    'arrival_time',
    'phase_response',
    'eigenfrequency',
    'energy_decay_curve',
    'frequency_response',
    'sound_pressure_level',
    'other_declared',
]
"""Observables a wave result may be qualified for — qualification is
per (observable, band) and a magnitude qualification never implies a
phase/timing qualification (#683 §14)."""

ConvergenceStudyKind = Literal[
    'refinement', 'fixture', 'cross_solver', 'analytic_reference',
    'declared_limitation',
]

ReferenceKind = Literal[
    'analytic',
    'semi_analytic',
    'high_resolution',
    'cross_solver',
    'measured',
    'not_applicable',
]


# ----------------------------------------------------------------------
# Declared solver formulation & discretization


class WaveSolverFormulation(BaseModel):
    """Which solver, solving which equation, with which basis (#683 §1).

    The formulation is the identity of the numerical method — changing
    the solver family, basis order or precision produces a different
    fidelity profile, not a tweak of the old one.
    """

    model_config = ConfigDict(frozen=True)

    solver_family: WaveSolverFamily
    equation_formulation: str = Field(min_length=1)
    """e.g. ``leapfrog_velocity_potential`` / ``helmholtz_weak_form``."""
    solver_domain: WaveSolverDomain
    implementation: str = Field(min_length=1)
    """Library or backend name — ``pffdtd``, ``comsol``, ``custom``."""
    implementation_version: str = Field(min_length=1)
    """Version string or commit identity — required; 'latest' is not a
    version."""
    element_or_basis: str = Field(min_length=1)
    """Spatial basis identity — ``cartesian_staggered_yee`` /
    ``tetrahedral_p2`` / ``constant_boundary_element``."""
    precision: NumericalPrecision = 'float64'
    parallel_mode: str | None = None
    """`cpu_numba` / `cuda` / `mpi` — recorded only when it changes the
    numerical result (different reduction order / precision)."""
    boundary_update_scheme: str | None = None
    source_discretization: str = Field(min_length=1)
    receiver_interpolation: str = Field(min_length=1)


class WaveDiscretization(BaseModel):
    """The spatial discretization identity (#683 §2).

    ``mesh_sha256`` is the content hash of the mesh/grid actually used —
    two runs on different grids never share a fidelity profile.
    """

    model_config = ConfigDict(frozen=True)

    mesh_identity: str = Field(min_length=1)
    mesh_sha256: str = Field(pattern=_SHA256_PATTERN)
    resolution_min_m: float | None = Field(default=None, gt=0.0)
    resolution_max_m: float | None = Field(default=None, gt=0.0)
    element_or_voxel_size_m: float | None = Field(default=None, gt=0.0)
    basis_order: int | None = Field(default=None, ge=0)
    geometry_approximation: str = Field(min_length=1)
    """How geometry enters the discretization — ``staircased_voxel`` /
    ``body_fitted_linear`` / ``body_fitted_high_order``."""
    boundary_representation: str = Field(min_length=1)
    points_per_wavelength_rule: str | None = None
    """Declared resolution policy — e.g. ``10_ppwl_at_f_max`` — never a
    hidden implicit rule."""
    target_frequency_hz: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'WaveDiscretization':
        if (
            self.resolution_min_m is not None
            and self.resolution_max_m is not None
            and self.resolution_min_m > self.resolution_max_m
        ):
            raise ValueError('resolution_min_m must not exceed max')
        return self


class FdtdTimeStepping(BaseModel):
    """FDTD-specific numerical declarations (#683 §4)."""

    model_config = ConfigDict(frozen=True)

    dt_seconds: float = Field(gt=0.0)
    grid_spacing_m: float = Field(gt=0.0)
    cfl_rule: str = Field(min_length=1)
    """The stability criterion actually applied, e.g.
    ``c*dt/dx <= 1/sqrt(dimensionality)``."""
    cfl_number: float = Field(gt=0.0)
    """The realized CFL number of the run (c·dt/dx or the formulation's
    equivalent)."""
    time_integration: str = Field(min_length=1)
    boundary_update: str | None = None
    duration_s: float | None = Field(default=None, gt=0.0)
    stability_state: WaveStabilityState = 'not_evaluated'

    @model_validator(mode='after')
    def _check(self) -> 'FdtdTimeStepping':
        _require_finite(self.dt_seconds, 'fdtd dt_seconds')
        _require_finite(self.grid_spacing_m, 'fdtd grid_spacing_m')
        _require_finite(self.cfl_number, 'fdtd cfl_number')
        if self.duration_s is not None:
            _require_finite(self.duration_s, 'fdtd duration_s')
        return self


class FemNumerics(BaseModel):
    """FEM-specific numerical declarations (#683 §5).

    Element type/order, mesh density and the maximum wavenumber decide
    the pollution error; the algebraic solver tolerance is kept in
    :class:`LinearSolveEvidence`, not here — discretization convergence
    and algebraic convergence are different classes.
    """

    model_config = ConfigDict(frozen=True)

    element_family: str = Field(min_length=1)
    element_order: int = Field(ge=1)
    mesh_density_descriptor: str = Field(min_length=1)
    """e.g. ``8 elements per wavelength at f_max``."""
    max_wavenumber_1_m: float | None = Field(default=None, gt=0.0)
    stabilization: str | None = None
    solver_tolerance: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def _check(self) -> 'FemNumerics':
        if self.max_wavenumber_1_m is not None:
            _require_finite(
                self.max_wavenumber_1_m, 'fem max_wavenumber_1_m'
            )
        if self.solver_tolerance is not None:
            _require_finite(
                self.solver_tolerance, 'fem solver_tolerance'
            )
        return self


class BemNumerics(BaseModel):
    """BEM/FMBEM-specific numerical declarations (#683 §6)."""

    model_config = ConfigDict(frozen=True)

    element_order: int = Field(ge=0)
    quadrature_rule: str = Field(min_length=1)
    singular_handling: str | None = None
    fmm_expansion_order: int | None = Field(default=None, ge=1)
    fmm_tolerance: float | None = Field(default=None, gt=0.0)
    iterative_tolerance: float | None = Field(default=None, gt=0.0)
    resonance_handling: str | None = None
    """Non-uniqueness / spurious-resonance treatment (CHIEF, Burton–
    Miller, none declared)."""

    @model_validator(mode='after')
    def _check(self) -> 'BemNumerics':
        for value, label in (
            (self.fmm_tolerance, 'fmm_tolerance'),
            (self.iterative_tolerance, 'iterative_tolerance'),
        ):
            if value is not None:
                _require_finite(value, f'bem {label}')
        return self


class WaveBoundaryReflectionEvidence(BaseModel):
    """Measured boundary performance on a controlled outgoing-wave
    fixture (#683 §7/§8).

    A PML that was never characterized is *not* an anechoic termination —
    this record is the only way the artificial-boundary error class can
    reach ``qualified``.
    """

    model_config = ConfigDict(frozen=True)

    fixture_description: str = Field(min_length=1)
    fixture_ref: AuthorityRef | None = None
    reflection_coefficient_max: float | None = Field(
        default=None, ge=0.0
    )
    returned_energy_fraction: float | None = Field(default=None, ge=0.0)
    reference_kind: ReferenceKind = 'not_applicable'
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveBoundaryReflectionEvidence':
        for value, label in (
            (self.reflection_coefficient_max, 'reflection_coefficient'),
            (self.returned_energy_fraction, 'returned_energy_fraction'),
        ):
            if value is not None:
                _require_finite(value, f'boundary evidence {label}')
        if self.fixture_ref is not None and (
            self.fixture_ref.ref_sha256 is None
        ):
            raise ValueError(
                'boundary evidence fixture_ref must pin its sha256'
            )
        return self


class ArtificialBoundarySpec(BaseModel):
    """The computational boundary treatment (#683 §7).

    ``method`` enumerates *computational* mechanisms only — a physical
    anechoic assumption is an :class:`AdjacentTerminationSpec`, not an
    artificial boundary.
    """

    model_config = ConfigDict(frozen=True)

    method: Literal[
        'pml',
        'absorbing_layer',
        'sponge_layer',
        'impedance_termination',
        'none',
        'other',
    ]
    layer_thickness_m: float | None = Field(default=None, gt=0.0)
    damping_profile: str | None = None
    """Stretching/damping profile identity — polynomial order, sigma_max
    rule, or implementation name."""
    frequency_domain_hz: tuple[float, float] | None = None
    incidence_assumptions: str | None = None
    distance_from_interest_m: float | None = Field(
        default=None, ge=0.0
    )
    layer_mesh_resolution: str | None = None
    measured_reflection: WaveBoundaryReflectionEvidence | None = None

    @model_validator(mode='after')
    def _check(self) -> 'ArtificialBoundarySpec':
        if self.frequency_domain_hz is not None:
            low, high = self.frequency_domain_hz
            _require_finite(low, 'boundary band low')
            _require_finite(high, 'boundary band high')
            if low >= high:
                raise ValueError(
                    'boundary frequency_domain_hz must be low < high'
                )
        for value, label in (
            (self.layer_thickness_m, 'layer_thickness_m'),
            (self.distance_from_interest_m, 'distance_from_interest_m'),
        ):
            if value is not None:
                _require_finite(value, f'boundary {label}')
        if self.method == 'none':
            if any(
                value is not None
                for value in (
                    self.layer_thickness_m,
                    self.damping_profile,
                    self.measured_reflection,
                )
            ):
                raise ValueError(
                    "method 'none' cannot carry PML parameters"
                )
        return self


class AdjacentTerminationSpec(BaseModel):
    """What lies physically beyond the modeled region (#683 §8).

    Distinct from the computational boundary: a PML is a numerical
    mechanism; 'the far field is anechoic' is a *physical* claim about the
    scene.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        'physical_anechoic_assumption',
        'explicit_adjacent_region',
        'open_domain',
        'unknown',
    ] = 'unknown'
    termination_ref: AuthorityRef | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'AdjacentTerminationSpec':
        if self.termination_ref is not None and (
            self.termination_ref.ref_sha256 is None
        ):
            raise ValueError('termination_ref must pin its sha256')
        return self


class LinearSolveEvidence(BaseModel):
    """Algebraic-solver outcome (#683 §12).

    A converged residual says the linear/iterative system was solved to
    tolerance — it says nothing about whether the discretization
    converged to the continuum solution.
    """

    model_config = ConfigDict(frozen=True)

    residual_criterion: str | None = None
    residual_achieved: float | None = Field(default=None, ge=0.0)
    preconditioner: str | None = None
    iteration_count: int | None = Field(default=None, ge=0)
    convergence_state: Literal[
        'converged', 'not_converged', 'not_applicable', 'unknown'
    ] = 'unknown'

    @model_validator(mode='after')
    def _check(self) -> 'LinearSolveEvidence':
        if self.residual_achieved is not None:
            _require_finite(
                self.residual_achieved, 'residual_achieved'
            )
        return self


class WaveSourceDiscretization(BaseModel):
    """How the source enters the discrete system (#683 §10)."""

    model_config = ConfigDict(frozen=True)

    injection_method: str = Field(min_length=1)
    """``hard_source`` / ``soft_source`` / ``volume_velocity`` etc."""
    regularization: str | None = None
    """Grid smearing / band-limiting applied to a point source."""
    directivity_coupling: str | None = None
    near_field_limitation: str | None = None


class WaveReceiverDiscretization(BaseModel):
    """How the response is extracted from the grid (#683 §11)."""

    model_config = ConfigDict(frozen=True)

    method: Literal[
        'grid_node',
        'interpolated',
        'spatial_average',
        'reconstructed',
        'other',
    ]
    interpolation_order: int | None = Field(default=None, ge=0)
    detail: str | None = None


class WaveDispersionEvidence(BaseModel):
    """Numerical-dispersion measurement on a controlled fixture
    (#683 §3).

    Phase-velocity error, arrival-time error, phase error and amplitude
    error are declared *separately* — a magnitude check never certifies
    timing.
    """

    model_config = ConfigDict(frozen=True)

    fixture_id: WaveFixtureId | None = None
    fixture_ref: AuthorityRef | None = None
    band_hz: tuple[float, float] | None = None
    phase_velocity_error_rel: float | None = None
    arrival_time_error_s: float | None = Field(default=None, ge=0.0)
    phase_error_rad: float | None = None
    amplitude_error_rel: float | None = None
    anisotropy_observed: Literal['yes', 'no', 'not_evaluated'] = (
        'not_evaluated'
    )
    frequency_dependence_notes: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveDispersionEvidence':
        for value, label in (
            (self.phase_velocity_error_rel, 'phase_velocity_error_rel'),
            (self.arrival_time_error_s, 'arrival_time_error_s'),
            (self.phase_error_rad, 'phase_error_rad'),
            (self.amplitude_error_rel, 'amplitude_error_rel'),
        ):
            if value is not None:
                _require_finite(value, f'dispersion {label}')
        if self.band_hz is not None:
            low, high = self.band_hz
            if low >= high:
                raise ValueError('dispersion band_hz must be low < high')
        if self.fixture_ref is not None and (
            self.fixture_ref.ref_sha256 is None
        ):
            raise ValueError(
                'dispersion fixture_ref must pin its sha256'
            )
        return self


class WaveUncertaintyBudget(BaseModel):
    """Contribution split by error class (#683 §17).

    Values are relative contributions in the units declared by the
    caller's convention; the point is that each class is *listed* — an
    unlisted class is an unaccounted error source, not a zero.
    """

    model_config = ConfigDict(frozen=True)

    numerical_discretization_rel: float | None = None
    algebraic_solver_tolerance_rel: float | None = None
    artificial_boundary_rel: float | None = None
    source_discretization_rel: float | None = None
    receiver_interpolation_rel: float | None = None
    geometry_discretization_rel: float | None = None
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveUncertaintyBudget':
        for label in (
            'numerical_discretization_rel',
            'algebraic_solver_tolerance_rel',
            'artificial_boundary_rel',
            'source_discretization_rel',
            'receiver_interpolation_rel',
            'geometry_discretization_rel',
        ):
            value = getattr(self, label)
            if value is not None:
                _require_finite(value, f'budget {label}')
        return self


class WaveRefinementLevel(BaseModel):
    """One mesh/grid level of a refinement study (#683 §13)."""

    model_config = ConfigDict(frozen=True)

    level_name: str = Field(min_length=1)
    discretization: WaveDiscretization
    result_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveRefinementLevel':
        if self.result_ref is not None and (
            self.result_ref.ref_sha256 is None
        ):
            raise ValueError('refinement result_ref must pin sha256')
        return self


class WaveRefinementComparison(BaseModel):
    """One observable compared between two refinement levels."""

    model_config = ConfigDict(frozen=True)

    observable: WaveObservable | str = Field(min_length=1)
    level_pair: tuple[str, str]
    max_relative_change: float | None = Field(default=None, ge=0.0)
    detail: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveRefinementComparison':
        if len(set(self.level_pair)) != 2:
            raise ValueError(
                'level_pair must name two distinct levels'
            )
        if self.max_relative_change is not None:
            _require_finite(
                self.max_relative_change, 'max_relative_change'
            )
        return self


class WaveFixtureResult(BaseModel):
    """Outcome on one #683 fixture (WNV10–WNV80)."""

    model_config = ConfigDict(frozen=True)

    fixture_id: WaveFixtureId
    verdict: Literal['pass', 'fail', 'not_executed', 'not_applicable']
    reference_kind: ReferenceKind = 'not_applicable'
    metric_summary: str | None = None
    evidence_ref: AuthorityRef | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveFixtureResult':
        if self.evidence_ref is not None and (
            self.evidence_ref.ref_sha256 is None
        ):
            raise ValueError('fixture evidence_ref must pin sha256')
        return self


class WaveErrorClassDeclaration(BaseModel):
    """Per-error-class state inside a qualification."""

    model_config = ConfigDict(frozen=True)

    error_class: WaveErrorClass
    state: ErrorClassState
    evidence: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveErrorClassDeclaration':
        if self.state == 'qualified' and not self.evidence:
            raise ValueError(
                'a qualified error class must cite its evidence'
            )
        return self


class WaveBandQualification(BaseModel):
    """One observable qualified over a declared band (#683 §14)."""

    model_config = ConfigDict(frozen=True)

    observable: WaveObservable | str = Field(min_length=1)
    band_hz: tuple[float, float] | None = None
    state: Literal[
        'qualified', 'limited', 'unqualified', 'not_evaluated'
    ]
    basis: str | None = None

    @model_validator(mode='after')
    def _check(self) -> 'WaveBandQualification':
        if self.band_hz is not None:
            low, high = self.band_hz
            _require_finite(low, 'band low')
            _require_finite(high, 'band high')
            if low >= high:
                raise ValueError('band_hz must be low < high')
        if self.state == 'qualified' and self.band_hz is None:
            raise ValueError(
                'a qualified band qualification must name its band'
            )
        return self


# ----------------------------------------------------------------------
# Sealed records


class WaveNumericalFidelityProfile(BaseModel):
    """The sealed declaration of one wave-solver configuration's
    numerical identity (#683).

    ``solver_result_ref`` pins the solver-result envelope this profile
    describes; the profile's own sha256 pins every numerically material
    setting — mesh hash, time step, boundary treatment, solver version —
    so any change is a new profile, not an edit (#683 §18).
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    solver_result_ref: AuthorityRef | None = None
    formulation: WaveSolverFormulation
    discretization: WaveDiscretization
    fdtd: FdtdTimeStepping | None = None
    fem: FemNumerics | None = None
    bem: BemNumerics | None = None
    artificial_boundary: ArtificialBoundarySpec | None = None
    adjacent_termination: AdjacentTerminationSpec | None = None
    linear_solve: LinearSolveEvidence | None = None
    source_discretization: WaveSourceDiscretization | None = None
    receiver_discretization: WaveReceiverDiscretization | None = None
    dispersion_evidence: WaveDispersionEvidence | None = None
    uncertainty_budget: WaveUncertaintyBudget | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=WAVE_FIDELITY_SCHEMA_VERSION, min_length=1
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
            'formulation': self.formulation.model_dump(mode='json'),
            'discretization': self.discretization.model_dump(mode='json'),
            'fdtd': (
                self.fdtd.model_dump(mode='json')
                if self.fdtd is not None
                else None
            ),
            'fem': (
                self.fem.model_dump(mode='json')
                if self.fem is not None
                else None
            ),
            'bem': (
                self.bem.model_dump(mode='json')
                if self.bem is not None
                else None
            ),
            'artificial_boundary': (
                self.artificial_boundary.model_dump(mode='json')
                if self.artificial_boundary is not None
                else None
            ),
            'adjacent_termination': (
                self.adjacent_termination.model_dump(mode='json')
                if self.adjacent_termination is not None
                else None
            ),
            'linear_solve': (
                self.linear_solve.model_dump(mode='json')
                if self.linear_solve is not None
                else None
            ),
            'source_discretization': (
                self.source_discretization.model_dump(mode='json')
                if self.source_discretization is not None
                else None
            ),
            'receiver_discretization': (
                self.receiver_discretization.model_dump(mode='json')
                if self.receiver_discretization is not None
                else None
            ),
            'dispersion_evidence': (
                self.dispersion_evidence.model_dump(mode='json')
                if self.dispersion_evidence is not None
                else None
            ),
            'uncertainty_budget': (
                self.uncertainty_budget.model_dump(mode='json')
                if self.uncertainty_budget is not None
                else None
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'WaveNumericalFidelityProfile':
        _require_iso8601(self.declared_at_utc, 'profile declared_at_utc')
        if self.solver_result_ref is not None and (
            self.solver_result_ref.ref_sha256 is None
        ):
            raise ValueError('solver_result_ref must pin its sha256')
        family = self.formulation.solver_family
        if family == 'fdtd' and self.fdtd is None:
            raise ValueError(
                'an FDTD formulation requires the fdtd stepping block'
            )
        if family != 'fdtd' and self.fdtd is not None:
            raise ValueError(
                'the fdtd stepping block is only valid for family fdtd'
            )
        if family == 'fem' and self.fem is None:
            raise ValueError(
                'an FEM formulation requires the fem numerics block'
            )
        if family != 'fem' and self.fem is not None:
            raise ValueError(
                'the fem numerics block is only valid for family fem'
            )
        if family in ('bem', 'fmbem') and self.bem is None:
            raise ValueError(
                'a BEM/FMBEM formulation requires the bem numerics block'
            )
        if family not in ('bem', 'fmbem') and self.bem is not None:
            raise ValueError(
                'the bem numerics block is only valid for bem/fmbem'
            )
        if self.fem is not None and self.bem is not None:
            raise ValueError(
                'fem and bem numerics are mutually exclusive'
            )
        if (
            self.artificial_boundary is not None
            and self.artificial_boundary.method == 'none'
            and self.adjacent_termination is not None
            and self.adjacent_termination.kind
            == 'physical_anechoic_assumption'
            and self.adjacent_termination.detail is None
        ):
            raise ValueError(
                'a physical anechoic assumption must carry a detail '
                'when no computational boundary is declared'
            )
        expected = _hash(self.identity_payload())
        if self.profile_sha256 != expected:
            raise ValueError('wave fidelity profile hash mismatch')
        if self.profile_id != _semantic_id('wnfprof', expected):
            raise ValueError(
                'wave fidelity profile id does not match its hash'
            )
        return self


def wave_profile_binding(
    profile: WaveNumericalFidelityProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind='wave_fidelity_profile',
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


class NumericalConvergenceRecord(BaseModel):
    """A sealed convergence study against a
    :class:`WaveNumericalFidelityProfile` (#683 §13/§15/§16).

    `refinement` requires at least two declared levels plus per-observable
    comparisons — two meshes that happen to agree are evidence of a
    trend, not convergence, and the evaluator treats two-level studies as
    partial. `cross_solver` always carries a declared independence
    limitation (implementations share theory and may share error).
    """

    model_config = ConfigDict(frozen=True)

    convergence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    study_kind: ConvergenceStudyKind
    refinement_levels: tuple[WaveRefinementLevel, ...] = ()
    comparisons: tuple[WaveRefinementComparison, ...] = ()
    fixture_results: tuple[WaveFixtureResult, ...] = ()
    cross_solver_detail: str | None = None
    reference_independence_limitation: str | None = None
    declared_at_utc: str = Field(min_length=1)
    authority_version: str = Field(
        default=WAVE_FIDELITY_SCHEMA_VERSION, min_length=1
    )
    convergence_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'study_kind': self.study_kind,
            'refinement_levels': [
                level.model_dump(mode='json')
                for level in self.refinement_levels
            ],
            'comparisons': [
                comp.model_dump(mode='json') for comp in self.comparisons
            ],
            'fixture_results': [
                result.model_dump(mode='json')
                for result in self.fixture_results
            ],
            'cross_solver_detail': self.cross_solver_detail,
            'reference_independence_limitation': (
                self.reference_independence_limitation
            ),
            'authority_version': self.authority_version,
            'declared_at_utc': self.declared_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'NumericalConvergenceRecord':
        _require_iso8601(
            self.declared_at_utc, 'convergence declared_at_utc'
        )
        if self.profile_ref.kind != 'wave_fidelity_profile':
            raise ValueError(
                "profile_ref must pin a 'wave_fidelity_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        if self.study_kind == 'refinement':
            names = [
                level.level_name for level in self.refinement_levels
            ]
            if len(self.refinement_levels) < 2:
                raise ValueError(
                    'a refinement study needs at least two levels'
                )
            if len(set(names)) != len(names):
                raise ValueError('refinement level names must be unique')
            if not self.comparisons:
                raise ValueError(
                    'a refinement study needs observable comparisons'
                )
            known = set(names)
            for comp in self.comparisons:
                for level_name in comp.level_pair:
                    if level_name not in known:
                        raise ValueError(
                            'comparison level_pair references an '
                            'undeclared level'
                        )
        if self.study_kind == 'fixture' and not self.fixture_results:
            raise ValueError(
                'a fixture study needs at least one fixture result'
            )
        if self.study_kind == 'cross_solver':
            if not self.cross_solver_detail:
                raise ValueError(
                    'a cross-solver study must name the compared solvers'
                )
            if self.reference_independence_limitation is None:
                raise ValueError(
                    'a cross-solver study must declare its independence '
                    'limitation (shared theory/implementation is never '
                    'assumed independent)'
                )
        expected = _hash(self.identity_payload())
        if self.convergence_sha256 != expected:
            raise ValueError('numerical convergence record hash mismatch')
        if self.convergence_id != _semantic_id('wnvconv', expected):
            raise ValueError(
                'convergence record id does not match its hash'
            )
        return self


def wave_convergence_binding(
    record: NumericalConvergenceRecord,
) -> AuthorityRef:
    return AuthorityRef(
        kind='wave_convergence_record',
        ref_id=record.convergence_id,
        ref_sha256=record.convergence_sha256,
    )


class WaveFidelityQualification(BaseModel):
    """The sealed fail-closed verdict on a wave profile (#683 §14).

    ``fidelity_state`` is the overall verdict;
    ``error_class_states`` lists every tracked class separately — a
    qualified overall state requires every applicable class to be
    qualified, never an average.
    """

    model_config = ConfigDict(frozen=True)

    qualification_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    profile_ref: AuthorityRef
    convergence_refs: tuple[AuthorityRef, ...] = ()
    fidelity_state: FidelityState
    error_class_states: tuple[WaveErrorClassDeclaration, ...]
    band_qualifications: tuple[WaveBandQualification, ...] = ()
    limitations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evaluated_at_utc: str = Field(min_length=1)
    evaluation_version: str = Field(
        default=WAVE_FIDELITY_EVALUATION_VERSION, min_length=1
    )
    qualification_sha256: str = Field(pattern=_SHA256_PATTERN)

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'profile_ref': self.profile_ref.model_dump(mode='json'),
            'convergence_refs': [
                ref.model_dump(mode='json')
                for ref in self.convergence_refs
            ],
            'fidelity_state': self.fidelity_state,
            'error_class_states': [
                entry.model_dump(mode='json')
                for entry in self.error_class_states
            ],
            'band_qualifications': [
                band.model_dump(mode='json')
                for band in self.band_qualifications
            ],
            'limitations': list(self.limitations),
            'reasons': list(self.reasons),
            'evaluation_version': self.evaluation_version,
            'evaluated_at_utc': self.evaluated_at_utc,
        }

    @model_validator(mode='after')
    def _check(self) -> 'WaveFidelityQualification':
        _require_iso8601(
            self.evaluated_at_utc, 'qualification evaluated_at_utc'
        )
        if self.profile_ref.kind != 'wave_fidelity_profile':
            raise ValueError(
                "profile_ref must pin a 'wave_fidelity_profile'"
            )
        if self.profile_ref.ref_sha256 is None:
            raise ValueError('profile_ref must pin its sha256')
        for ref in self.convergence_refs:
            if ref.kind != 'wave_convergence_record':
                raise ValueError(
                    "convergence_refs must pin "
                    "'wave_convergence_record' authorities"
                )
            if ref.ref_sha256 is None:
                raise ValueError(
                    'convergence_refs must pin their sha256'
                )
        seen = [entry.error_class for entry in self.error_class_states]
        if len(set(seen)) != len(seen):
            raise ValueError('error classes must be unique')
        expected = _hash(self.identity_payload())
        if self.qualification_sha256 != expected:
            raise ValueError('wave fidelity qualification hash mismatch')
        if self.qualification_id != _semantic_id('wnfqual', expected):
            raise ValueError(
                'wave qualification id does not match its hash'
            )
        return self


def wave_qualification_binding(
    qualification: WaveFidelityQualification,
) -> AuthorityRef:
    return AuthorityRef(
        kind='wave_fidelity_qualification',
        ref_id=qualification.qualification_id,
        ref_sha256=qualification.qualification_sha256,
    )


# ----------------------------------------------------------------------
# Evaluation


def _class_state(
    error_class: WaveErrorClass,
    state: ErrorClassState,
    evidence: str | None = None,
) -> WaveErrorClassDeclaration:
    return WaveErrorClassDeclaration(
        error_class=error_class, state=state, evidence=evidence
    )


def evaluate_wave_fidelity(
    document_id: str,
    profile: WaveNumericalFidelityProfile,
    convergences: Sequence[NumericalConvergenceRecord] = (),
    band_qualifications: Sequence[WaveBandQualification] = (),
    *,
    evaluated_at_utc: str | None = None,
) -> WaveFidelityQualification:
    """Fail-closed verdict on a wave-fidelity profile (#683 §14/§17).

    Rules:
    - every tracked error class is rated separately;
    - an FDTD run outside its declared stability domain, or a failed
      fixture, is ``not_qualified``;
    - ``qualified_for_declared_domain`` requires every applicable class
      qualified *and* at least one convergence study that reached a real
      reference (≥3-level refinement trend, an analytic fixture pass, or
      a cross-solver agreement with declared limitations);
    - missing evidence degrades to ``insufficient_evidence`` — never to
      ``qualified``.
    """

    evaluated_at_utc = evaluated_at_utc or _utc_now()
    _require_iso8601(evaluated_at_utc, 'evaluated_at_utc')

    states: list[WaveErrorClassDeclaration] = []
    limitations: list[str] = []
    reasons: list[str] = []
    family = profile.formulation.solver_family

    # --- numerical dispersion -------------------------------------------
    dispersion = profile.dispersion_evidence
    if dispersion is None:
        states.append(
            _class_state('numerical_dispersion', 'unresolved')
        )
        reasons.append('numerical dispersion was not evaluated')
    elif (
        dispersion.phase_velocity_error_rel is not None
        or dispersion.arrival_time_error_s is not None
        or dispersion.phase_error_rad is not None
        or dispersion.amplitude_error_rel is not None
    ):
        states.append(
            _class_state(
                'numerical_dispersion',
                'qualified',
                'controlled-fixture dispersion measurement declared',
            )
        )
    else:
        states.append(
            _class_state(
                'numerical_dispersion',
                'limited',
                'dispersion evidence declared without numeric metrics',
            )
        )
        limitations.append(
            'dispersion declared but no numeric error metric given'
        )

    # --- FEM pollution ---------------------------------------------------
    if family == 'fem':
        fem = profile.fem
        if fem is not None and (
            fem.max_wavenumber_1_m is not None
            or fem.stabilization is not None
        ):
            states.append(
                _class_state(
                    'fem_pollution',
                    'qualified',
                    'element order + wavenumber bound declared',
                )
            )
        else:
            states.append(
                _class_state(
                    'fem_pollution',
                    'limited',
                    'fem block present without wavenumber/stabilization',
                )
            )
            limitations.append(
                'FEM pollution sensitivity not bounded by wavenumber'
            )
    else:
        states.append(
            _class_state('fem_pollution', 'not_applicable')
        )

    # --- CFL / stability -------------------------------------------------
    if family == 'fdtd':
        fdtd = profile.fdtd
        if fdtd is None:
            states.append(
                _class_state('cfl_stability', 'unresolved')
            )
        elif fdtd.stability_state == 'within_declared_stability':
            states.append(
                _class_state(
                    'cfl_stability',
                    'qualified',
                    'declared within CFL stability domain',
                )
            )
        elif fdtd.stability_state == 'outside_declared_stability':
            states.append(
                _class_state(
                    'cfl_stability',
                    'limited',
                    'run is outside its declared stability domain',
                )
            )
            reasons.append(
                'FDTD run declared outside its CFL stability domain'
            )
        else:
            states.append(
                _class_state(
                    'cfl_stability',
                    'unresolved',
                    'stability not evaluated',
                )
            )
            reasons.append('FDTD stability domain was not evaluated')
    else:
        states.append(
            _class_state('cfl_stability', 'not_applicable')
        )

    # --- artificial boundary ---------------------------------------------
    boundary = profile.artificial_boundary
    if boundary is None or boundary.method == 'none':
        states.append(
            _class_state('artificial_boundary', 'not_applicable')
        )
    elif boundary.measured_reflection is not None and (
        boundary.measured_reflection.reflection_coefficient_max
        is not None
        or boundary.measured_reflection.returned_energy_fraction
        is not None
    ):
        states.append(
            _class_state(
                'artificial_boundary',
                'qualified',
                'boundary leakage measured on an outgoing-wave fixture',
            )
        )
    elif boundary.measured_reflection is not None:
        states.append(
            _class_state(
                'artificial_boundary',
                'limited',
                'boundary leakage evidence declared without numeric '
                'metrics',
            )
        )
        limitations.append(
            'PML/absorbing-layer evidence lacks a numeric leakage '
            'metric'
        )
    else:
        states.append(
            _class_state(
                'artificial_boundary',
                'unresolved',
                'absorbing boundary declared without measured leakage',
            )
        )
        limitations.append(
            'PML/absorbing-layer leakage was not characterized'
        )

    # --- geometry discretization -----------------------------------------
    discretization = profile.discretization
    if discretization.geometry_approximation and (
        discretization.boundary_representation
    ):
        states.append(
            _class_state(
                'geometry_discretization',
                'qualified',
                'geometry approximation + boundary representation declared',
            )
        )
    else:
        states.append(
            _class_state('geometry_discretization', 'unresolved')
        )

    # --- source discretization -------------------------------------------
    if profile.source_discretization is not None:
        states.append(
            _class_state(
                'source_discretization',
                'qualified',
                'injection method declared',
            )
        )
    else:
        states.append(
            _class_state('source_discretization', 'unresolved')
        )
        reasons.append('source discretization was not declared')

    # --- receiver interpolation ------------------------------------------
    if profile.receiver_discretization is not None:
        states.append(
            _class_state(
                'receiver_interpolation',
                'qualified',
                'receiver extraction method declared',
            )
        )
    else:
        states.append(
            _class_state('receiver_interpolation', 'unresolved')
        )
        reasons.append('receiver interpolation was not declared')

    # --- algebraic tolerance ---------------------------------------------
    solve = profile.linear_solve
    if solve is None:
        states.append(
            _class_state('algebraic_tolerance', 'not_applicable')
        )
    elif solve.convergence_state == 'converged':
        states.append(
            _class_state(
                'algebraic_tolerance',
                'qualified',
                'residual criterion met',
            )
        )
    elif solve.convergence_state == 'not_applicable':
        states.append(
            _class_state('algebraic_tolerance', 'not_applicable')
        )
    elif solve.convergence_state == 'not_converged':
        states.append(
            _class_state(
                'algebraic_tolerance',
                'limited',
                'algebraic solve did not converge',
            )
        )
        reasons.append('algebraic solver did not converge')
    else:
        states.append(
            _class_state('algebraic_tolerance', 'unresolved')
        )
        reasons.append('algebraic-solver convergence is unknown')

    # --- convergence evidence --------------------------------------------
    strong_convergence = False
    any_convergence = False
    failed_fixture = False
    profile_binding = wave_profile_binding(profile)
    for record in convergences:
        if (
            record.profile_ref.ref_id != profile_binding.ref_id
            or record.profile_ref.ref_sha256
            != profile_binding.ref_sha256
        ):
            raise ValueError(
                'a convergence record must pin the profile under '
                'evaluation'
            )
        if record.study_kind == 'declared_limitation':
            any_convergence = True
            limitations.append(
                record.cross_solver_detail
                or 'convergence declared as a limitation'
            )
            continue
        any_convergence = True
        for result in record.fixture_results:
            if result.verdict == 'fail':
                failed_fixture = True
            if (
                result.verdict == 'pass'
                and result.reference_kind in ('analytic', 'semi_analytic')
            ):
                strong_convergence = True
        if record.study_kind == 'refinement':
            if len(record.refinement_levels) >= 3:
                strong_convergence = True
            else:
                limitations.append(
                    'two-level refinement supports a trend, not '
                    'convergence'
                )
        if record.study_kind == 'cross_solver':
            strong_convergence = True
            limitations.append(
                record.reference_independence_limitation
                or 'cross-solver comparison is not fully independent'
            )
    if not any_convergence:
        reasons.append('no convergence study was declared')

    # --- overall verdict ---------------------------------------------------
    class_states = {entry.error_class: entry.state for entry in states}
    unresolved = any(
        entry.state == 'unresolved' for entry in states
    )
    limited = any(entry.state == 'limited' for entry in states)

    if failed_fixture or (
        family == 'fdtd'
        and profile.fdtd is not None
        and profile.fdtd.stability_state == 'outside_declared_stability'
    ) or (
        solve is not None
        and solve.convergence_state == 'not_converged'
    ):
        fidelity_state: FidelityState = 'not_qualified'
    elif unresolved or not any_convergence:
        fidelity_state = 'insufficient_evidence'
    elif limited or not strong_convergence:
        fidelity_state = 'qualified_with_limitations'
    else:
        fidelity_state = 'qualified_for_declared_domain'

    convergence_refs = tuple(
        wave_convergence_binding(record) for record in convergences
    )

    return _seal(
        WaveFidelityQualification,
        {
            'document_id': document_id,
            'profile_ref': wave_profile_binding(profile),
            'convergence_refs': list(convergence_refs),
            'fidelity_state': fidelity_state,
            'error_class_states': [
                entry.model_dump(mode='json') for entry in states
            ],
            'band_qualifications': [
                band.model_dump(mode='json')
                for band in band_qualifications
            ],
            'limitations': sorted(set(limitations)),
            'reasons': sorted(set(reasons)),
            'evaluated_at_utc': evaluated_at_utc,
            'evaluation_version': WAVE_FIDELITY_EVALUATION_VERSION,
        },
        'qualification_id',
        'qualification_sha256',
        'wnfqual',
    )


# ----------------------------------------------------------------------
# Builders


def build_wave_fidelity_profile(
    document_id: str,
    formulation: WaveSolverFormulation,
    discretization: WaveDiscretization,
    *,
    solver_result_ref: AuthorityRef | None = None,
    fdtd: FdtdTimeStepping | None = None,
    fem: FemNumerics | None = None,
    bem: BemNumerics | None = None,
    artificial_boundary: ArtificialBoundarySpec | None = None,
    adjacent_termination: AdjacentTerminationSpec | None = None,
    linear_solve: LinearSolveEvidence | None = None,
    source_discretization: WaveSourceDiscretization | None = None,
    receiver_discretization: WaveReceiverDiscretization | None = None,
    dispersion_evidence: WaveDispersionEvidence | None = None,
    uncertainty_budget: WaveUncertaintyBudget | None = None,
    declared_at_utc: str | None = None,
) -> WaveNumericalFidelityProfile:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        WaveNumericalFidelityProfile,
        {
            'document_id': document_id,
            'solver_result_ref': (
                solver_result_ref.model_dump(mode='json')
                if solver_result_ref is not None
                else None
            ),
            'formulation': formulation.model_dump(mode='json'),
            'discretization': discretization.model_dump(mode='json'),
            'fdtd': (
                fdtd.model_dump(mode='json') if fdtd is not None else None
            ),
            'fem': (
                fem.model_dump(mode='json') if fem is not None else None
            ),
            'bem': (
                bem.model_dump(mode='json') if bem is not None else None
            ),
            'artificial_boundary': (
                artificial_boundary.model_dump(mode='json')
                if artificial_boundary is not None
                else None
            ),
            'adjacent_termination': (
                adjacent_termination.model_dump(mode='json')
                if adjacent_termination is not None
                else None
            ),
            'linear_solve': (
                linear_solve.model_dump(mode='json')
                if linear_solve is not None
                else None
            ),
            'source_discretization': (
                source_discretization.model_dump(mode='json')
                if source_discretization is not None
                else None
            ),
            'receiver_discretization': (
                receiver_discretization.model_dump(mode='json')
                if receiver_discretization is not None
                else None
            ),
            'dispersion_evidence': (
                dispersion_evidence.model_dump(mode='json')
                if dispersion_evidence is not None
                else None
            ),
            'uncertainty_budget': (
                uncertainty_budget.model_dump(mode='json')
                if uncertainty_budget is not None
                else None
            ),
            'authority_version': WAVE_FIDELITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'profile_id',
        'profile_sha256',
        'wnfprof',
    )


def build_numerical_convergence_record(
    document_id: str,
    profile_ref: AuthorityRef,
    study_kind: ConvergenceStudyKind,
    *,
    refinement_levels: Sequence[WaveRefinementLevel] = (),
    comparisons: Sequence[WaveRefinementComparison] = (),
    fixture_results: Sequence[WaveFixtureResult] = (),
    cross_solver_detail: str | None = None,
    reference_independence_limitation: str | None = None,
    declared_at_utc: str | None = None,
) -> NumericalConvergenceRecord:
    declared_at_utc = declared_at_utc or _utc_now()
    return _seal(
        NumericalConvergenceRecord,
        {
            'document_id': document_id,
            'profile_ref': profile_ref.model_dump(mode='json'),
            'study_kind': study_kind,
            'refinement_levels': [
                level.model_dump(mode='json')
                for level in refinement_levels
            ],
            'comparisons': [
                comp.model_dump(mode='json') for comp in comparisons
            ],
            'fixture_results': [
                result.model_dump(mode='json')
                for result in fixture_results
            ],
            'cross_solver_detail': cross_solver_detail,
            'reference_independence_limitation': (
                reference_independence_limitation
            ),
            'authority_version': WAVE_FIDELITY_SCHEMA_VERSION,
            'declared_at_utc': declared_at_utc,
        },
        'convergence_id',
        'convergence_sha256',
        'wnvconv',
    )
